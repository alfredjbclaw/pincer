#!/usr/bin/env python3
"""loop_spec — the portable "loop spec" that makes a project deployable.

Design-the-loop-before-you-run-it (per the LOOPER idea): a small artifact per
project that pins WHAT to work, what "done" means, the autonomy level, and a
token budget guardrail. The autonomy driver reads these and runs the proven
pipeline; `deploy` manages them.

Spec lives at ~/.openclaw/pincer/loops/<name>.json:
{
  "name": "sqlmeta-maintain",
  "mode": "fix" | "audit",          # fix existing issues, or audit->file->fix
  "repo": "owner/name",
  "workdir": "/abs/path/to/clone",
  "target": "all-issues" | "1,2,3", # fix: which issues (audit ignores)
  "autonomy": "auto" | "pr-only",   # auto = merge bugfix / PR feature; pr-only = never merge
  "max_coders": 4,
  "max_findings": 8,                 # audit cap
  "budget": {"window_pct": 80, "week_pct": 80},  # halt if codex usage >= these
  "schedule": "manual" | "6h" | "<cron>",
  "enabled": true,
  "last_run": "<iso>|null"
}
"""
from __future__ import annotations
import os, json, subprocess, sys, dataclasses
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))
import parallel_orchestrator as po
import audit as audit_mod

LOOPS_DIR = Path.home() / ".openclaw" / "pincer" / "loops"
# Optional external usage gate; set $PINCER_USAGE_GATE to its path to enable.
USAGE_GATE = os.environ.get("PINCER_USAGE_GATE")

from notify import send_alert


@dataclasses.dataclass
class LoopSpec:
    name: str
    mode: str = "fix"               # fix | audit
    repo: str = ""
    workdir: str = ""
    target: str = "all-issues"
    autonomy: str = "auto"          # auto | pr-only
    max_coders: int = 4
    max_findings: int = 8
    budget: dict = dataclasses.field(default_factory=lambda: {"window_pct": 80, "week_pct": 80})
    schedule: str = "manual"
    enabled: bool = True
    last_run: str | None = None

    @property
    def path(self) -> Path:
        return LOOPS_DIR / f"{self.name}.json"

    def save(self):
        LOOPS_DIR.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(dataclasses.asdict(self), indent=2))

    @classmethod
    def load(cls, name: str) -> "LoopSpec":
        return cls(**json.loads((LOOPS_DIR / f"{name}.json").read_text()))

    @classmethod
    def all(cls) -> list["LoopSpec"]:
        if not LOOPS_DIR.exists():
            return []
        return [cls(**json.loads(p.read_text())) for p in sorted(LOOPS_DIR.glob("*.json"))]


def budget_ok(spec: LoopSpec) -> tuple[bool, str]:
    """Token-budget guardrail: halt the loop if codex usage crosses the spec's
    thresholds, so a run can't burn the window away."""
    if not USAGE_GATE or not os.path.exists(USAGE_GATE):
        return True, "budget check skipped (PINCER_USAGE_GATE unset/missing)"
    try:
        p = subprocess.run(["python3", USAGE_GATE, "--provider", "codex"],
                           capture_output=True, text=True, timeout=60)
        import re
        m = re.search(r"window=(\d+)%\s+week=(\d+)%", p.stdout + p.stderr)
        if m:
            w, wk = int(m.group(1)), int(m.group(2))
            if w >= spec.budget.get("window_pct", 80) or wk >= spec.budget.get("week_pct", 80):
                return False, f"usage window={w}% week={wk}% >= budget {spec.budget}"
            return True, f"usage window={w}% week={wk}% (within budget)"
    except Exception as e:
        return True, f"budget check skipped ({e})"
    return True, "budget unknown (allowing)"


def _resolve_issues(spec: LoopSpec) -> list[int]:
    if spec.mode == "audit":
        findings = audit_mod.audit_repo(spec.workdir, spec.max_findings)
        if not findings:
            return []
        po.sh(["gh", "label", "create", "pincer-audit", "-R", spec.repo,
               "--color", "5319e7", "--force"])
        nums = []
        for f in findings:
            body = f"{f['brief']}\n\n_pincer audit: type={f['type']} severity={f.get('severity')}_"
            out, _, _ = po.sh(["gh", "issue", "create", "-R", spec.repo, "--title", f["title"],
                               "--body", body, "--label", "pincer-audit"])
            url = (out or "").strip().splitlines()[-1] if out.strip() else ""
            if url and url.rsplit("/", 1)[-1].isdigit():
                nums.append(int(url.rsplit("/", 1)[-1]))
        return nums
    # mode == fix
    if spec.target == "all-issues":
        out, _, _ = po.sh(["gh", "issue", "list", "-R", spec.repo, "--state", "open",
                           "--limit", "100", "--json", "number"])
        try:
            return sorted(i["number"] for i in json.loads(out))
        except Exception:
            return []
    return [int(x) for x in spec.target.split(",") if x.strip()]


def run_spec(spec: LoopSpec, thread=None) -> dict:
    """Run one loop spec once: budget-gate -> resolve work -> proven pipeline -> report.

    `thread` (AlertThread): when the loop driver passes its thread, every alert
    here AND inside the orchestrator replies to the driver's start message, so
    the whole run is one Telegram reply-chain. Standalone, we start our own root."""
    if thread is None:
        thread = po.make_alert_thread(f"🔁 {spec.name}")

    def post(msg, level="progress"):
        if thread is not None:
            thread.post(msg, level=level)
        else:
            send_alert(msg)

    ok, why = budget_ok(spec)
    if not ok:
        post(f"⏸️ Loop '{spec.name}' held — {why}", "milestone")
        return {"name": spec.name, "result": "held_budget", "detail": why}
    # The orchestrator emits its own START milestone; keep this one as detail so
    # quiet mode isn't redundant.
    post(f"🔁 Loop '{spec.name}' START — {spec.mode} on {spec.repo} ({why}).")

    # Audit reads the workdir before the orchestrator does, so heal the clone now
    # (idempotent with the orchestrator's own ensure_clone for the fix path).
    if spec.mode == "audit":
        po.ensure_clone(spec.repo, spec.workdir, alert_fn=post)
    issues = _resolve_issues(spec)
    if not issues:
        post(f"✅ Loop '{spec.name}': nothing to do ({spec.mode} found no work).", "milestone")
        return {"name": spec.name, "result": "no_work"}

    state = po.run(spec.repo, spec.workdir, issues, spec.max_coders,
                   allow_merge=(spec.autonomy == "auto"), thread=thread)
    return {"name": spec.name, "result": state.get("result"),
            "scorecard": state.get("scorecard", {})}
