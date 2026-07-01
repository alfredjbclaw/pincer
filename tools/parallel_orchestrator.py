#!/usr/bin/env python3
"""pincer parallel orchestrator — fan-out coders, serialize the sandbox.

The Crabbox VM (~8 GiB each) is only needed at final verification, NOT during
coding. So we parallelize the cheap-on-memory coding stage and gate the sandbox
by a MEMORY-AWARE limit (up to [sandbox].max_concurrent VMs, started only while
host RAM can absorb another — see sandbox_slot / mem_monitor):

    N issues
      → [coders in parallel, each in its own git worktree]   (bounded by usage gate)
      → [Crabbox sandbox, ≤max_concurrent VMs, RAM-gated]    (memory-aware)
      → [Opus-4.8 review, in parallel]                       (no VM)
      → [publication gate → auto-merge | PR, per candidate]

Each coder runs the no-git WORKER_CONTRACT (leaves changes in its worktree's
working tree); the orchestrator owns every git operation.

Usage:
    python3 tools/parallel_orchestrator.py --repo OWNER/NAME \
        --workdir /path/to/clone --issues 3,4 [--max-coders 4] [--no-merge]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import dataclasses
import json
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))

import os

import runtime_adapter as ra
import publication_gate as pg
import reviewer as rv
import localization as lz
import selection as sel
import repro_test as rp
import work_history

from notify import send_alert, AlertThread, LiveBoard

LOG = logging.getLogger("pincer.orchestrator")

# A run's alert surface (a LiveBoard or AlertThread), set in run().
_THREAD = None
# Path to the current run's local detail log (set in run()).
_RUN_LOG = None

# Dedicated, muteable 'Pincer' forum topic — keeps pincer's run chatter out of
# the shared Alerts topic so it never spams the phone. Override via [alerts] in
# pincer.toml.
PINCER_ALERTS_TOPIC = 1101


def _alerts_config():
    """(topic_id, verbosity, style) from [alerts] in pincer.toml. Defaults: the
    dedicated Pincer topic, 'quiet' (milestones + criticals), 'live' (one
    edit-in-place status message instead of many — no phone spam)."""
    topic, verbosity, style = PINCER_ALERTS_TOPIC, "quiet", "live"
    try:
        import os
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore
        p = Path(os.environ.get("PINCER_CONFIG", Path.home() / ".openclaw" / "pincer.toml"))
        if p.exists():
            a = tomllib.loads(p.read_text()).get("alerts", {})
            topic = int(a.get("topic_id", topic))
            verbosity = str(a.get("verbosity", verbosity))
            style = str(a.get("style", style))
    except Exception:
        LOG.exception("Failed to load alerts config")
        pass
    return topic, verbosity, style


def make_alert_thread(tag):
    """Build a run's alert surface routed to the Pincer topic at the configured
    verbosity + style. style 'live' -> a single edit-in-place LiveBoard (default;
    one silent message that updates, criticals buzz separately); 'thread' -> the
    classic reply-thread of separate messages. Returns None if unavailable."""
    if LiveBoard is None and AlertThread is None:
        return None
    topic, verbosity, style = _alerts_config()
    min_level = "progress" if verbosity == "verbose" else "milestone"
    if style == "thread" and AlertThread is not None:
        return AlertThread(tag, topic_id=topic, min_level=min_level)
    return LiveBoard(tag, topic_id=topic, min_level=min_level)


def _start_run_log(repo: str, ts: str) -> None:
    """Open a per-run local detail log (full progress, including lines the quiet
    board hides). Pruned to the most recent runs so it never grows unbounded.
    `ts` is passed in (caller stamps it) to keep this side-effect-free of clocks."""
    global _RUN_LOG
    try:
        d = Path.home() / ".openclaw" / "pincer" / "run-logs"
        d.mkdir(parents=True, exist_ok=True)
        _prune_run_logs(d, keep=50)
        _RUN_LOG = d / f"{ts}-{repo.replace('/', '_')}.md"
        _RUN_LOG.write_text(f"# Pincer run — {repo} — {ts}\n\n")
    except Exception:
        LOG.exception("Failed to start run log for %s", repo)
        _RUN_LOG = None


def _prune_run_logs(d: Path, keep: int = 50) -> None:
    """Keep only the `keep` most-recent run logs (each is a few KB)."""
    try:
        logs = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in logs[keep:]:
            old.unlink(missing_ok=True)
    except Exception:
        LOG.exception("Failed to prune run logs in %s", d)
        pass


def _run_log(msg: str, level: str) -> None:
    if _RUN_LOG is None:
        return
    try:
        with open(_RUN_LOG, "a") as f:
            f.write(f"- `{level}` {msg}\n")
    except Exception:
        LOG.exception("Failed to write run log message")
        pass

PUBLISH_LOCK = threading.Lock()   # git merge to the shared main must serialize
WORKTREE_LOCK = threading.Lock()  # `git worktree add` touches shared .git; serialize setup only

# --- Memory-aware sandbox concurrency ---------------------------------------
# Each Apple VZ VM pre-allocates ~8 GiB. Instead of a hard single-VM lock, allow
# up to `max_concurrent` VMs but only start one while host RAM can absorb it
# (back-pressure across processes via mem_monitor). Defaults stay conservative;
# tune via [sandbox] in pincer.toml.
import contextlib  # noqa: E402
import mem_monitor as mm  # noqa: E402

_SANDBOX_SEM = threading.BoundedSemaphore(1)
_SANDBOX_GATE = {"max_concurrent": 1, "min_free_gb": 12.0, "vm_memory_gb": 8.0,
                 "max_wait_s": 600}


def _sandbox_gate_config():
    """[sandbox] concurrency knobs. Read here (not via sandbox_gate) so this file
    stays independent. Defaults are safe on a 48 GiB box."""
    cfg = dict(_SANDBOX_GATE)
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore
        p = Path(os.environ.get("PINCER_CONFIG", Path.home() / ".openclaw" / "pincer.toml"))
        if p.exists():
            s = tomllib.loads(p.read_text()).get("sandbox", {})
            cfg["max_concurrent"] = int(s.get("max_concurrent", cfg["max_concurrent"]))
            cfg["min_free_gb"] = float(s.get("min_free_gb", cfg["min_free_gb"]))
            cfg["vm_memory_gb"] = float(s.get("vm_memory_gb", cfg["vm_memory_gb"]))
            cfg["max_wait_s"] = int(s.get("vm_gate_max_wait_s", cfg["max_wait_s"]))
    except Exception:
        LOG.exception("Failed to load sandbox gate config")
        pass
    return cfg


def _init_sandbox_gate():
    global _SANDBOX_SEM, _SANDBOX_GATE
    _SANDBOX_GATE = _sandbox_gate_config()
    _SANDBOX_SEM = threading.BoundedSemaphore(max(1, _SANDBOX_GATE["max_concurrent"]))


def _now_stamp():
    import datetime
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


@contextlib.contextmanager
def sandbox_slot():
    """Acquire a sandbox VM slot: the per-process semaphore cap AND enough host
    RAM (waits with back-off, then proceeds cautiously after `max_wait_s`).
    Samples RAM around the VM for leak tracking. Replaces the old single lock —
    with max_concurrent=1 it behaves exactly like before."""
    c = _SANDBOX_GATE
    _SANDBOX_SEM.acquire()
    try:
        deadline = time.monotonic() + c["max_wait_s"]
        while True:
            ok, why = mm.can_start_vm(c["min_free_gb"], c["vm_memory_gb"])
            if ok:
                break
            if time.monotonic() >= deadline:
                alert(f"⚠️ sandbox RAM gate: proceeding after wait — {why}", "milestone")
                break
            time.sleep(5)
        mm.log_sample("vm_start", _now_stamp())
        yield
    finally:
        mm.log_sample("vm_end", _now_stamp())
        _SANDBOX_SEM.release()


def sh(cmd, cwd=None, timeout=1800):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.stdout, p.stderr, p.returncode


def alert(msg, level="progress"):
    # Full detail always goes to the local run log (free, on disk); the board
    # shows only milestones/criticals (no phone spam).
    _run_log(msg, level)
    try:
        if _THREAD is not None:
            _THREAD.post(msg, level=level)
        else:
            send_alert(msg)
    except Exception as e:
        LOG.exception("Failed to send alert")
        print("alert failed:", e)


def repo_test_cmd(workdir: Path) -> str:
    p = workdir / ".pincer.toml"
    if p.exists():
        import re
        txt = p.read_text()
        m = re.search(r'test_command\s*=\s*"""(.*?)"""', txt, re.DOTALL) or \
            re.search(r'test_command\s*=\s*"([^"]*)"', txt)
        if m:
            return m.group(1).strip()
    return "python3 -m pytest -q"


def _issue_text(repo: str, n: int) -> tuple[str, str]:
    """(title, body) for an issue — also the raw text fed to localization/repro."""
    out, _, _ = sh(["gh", "issue", "view", str(n), "-R", repo, "--json", "title,body"])
    data = json.loads(out)
    return data["title"], data.get("body") or ""


def issue_brief(repo: str, n: int, workdir: str | None = None,
                sample: int = 0) -> tuple[str, str]:
    """Build the worker brief for issue #n. Hierarchical localization (files +
    AST symbol skeleton) points the worker at the likely fix sites. `sample` is
    the candidate index when fanning out >1 coder per issue — it only nudges
    exploration diversity; it does not change what's asked."""
    title, body = _issue_text(repo, n)
    hint_block = ""
    if workdir:
        hint_block = lz.localize(workdir, f"{title} {body}").hint_block()
    diversify = ""
    if sample > 0:
        diversify = (f"\n(Independent attempt #{sample + 1} at this fix — explore a "
                     "different approach if the obvious one looks fragile.)\n")
    history_context = _failure_context(repo, n)
    history_block = f"\n\n{history_context}\n\n" if history_context else ""
    brief = (f"Fix {repo} issue #{n}: {title}\n\n{body}\n\n{hint_block}{diversify}"
             f"{history_block}"
             "Keep the change minimal and idiomatic. REQUIRED: add a test that captures "
             "this bug — one that FAILS on the current code and PASSES after your fix. If "
             "a matching xfail test exists, remove the xfail marker. You do NOT need to "
             "run the suite yourself — a sandbox with the project's deps installed will "
             "run it. Just make the fix and include the failing-first test.")
    return title, brief


def _resolves(workdir, ref: str) -> bool:
    """True iff `ref` resolves to an actual commit in `workdir`."""
    _, _, rc = sh(["git", "-C", str(workdir), "rev-parse", "--verify", "--quiet",
                   f"{ref}^{{commit}}"])
    return rc == 0


def _detect_default_branch(workdir: Path) -> str | None:
    """The repo's default branch *as a name whose origin/<name> actually
    resolves to a commit*. Returns None if the clone is unhealthy (no resolvable
    default) — that's the signal to re-clone. The old version returned a name
    from origin/HEAD that could be a dangling ref (the cause of the
    'invalid reference: master' crash on a corrupted /tmp clone)."""
    out, _, rc = sh(["git", "-C", str(workdir), "symbolic-ref", "--short",
                     "refs/remotes/origin/HEAD"])
    if rc == 0 and out.strip():
        name = out.strip().split("/", 1)[-1]
        if _resolves(workdir, f"origin/{name}"):
            return name
    for cand in ("main", "master"):
        if _resolves(workdir, f"origin/{cand}"):
            return cand
        if _resolves(workdir, cand):
            return cand
    return None


def default_branch(main_workdir: Path) -> str:
    """Detect the repo's default branch (main vs master vs ...), guaranteed to
    resolve. Falls back to 'main' only when nothing resolves (callers should
    have run ensure_clone first, which repairs that case)."""
    return _detect_default_branch(main_workdir) or "main"


def ensure_clone(repo: str, workdir, alert_fn=None, clone_url=None) -> str:
    """Guarantee `workdir` is a healthy clone of `repo` with a resolvable
    default branch, and return that branch name. Self-heals the failure that
    sank the sql-metadata loop runs: a /tmp clone that got purged/corrupted so
    `master` no longer resolved and every `git worktree add … master` died with
    'invalid reference: master'.

      - missing / not-a-repo / no resolvable default -> fresh clone
      - otherwise -> fetch, then hard-reset the local default branch to origin

    After this, a LOCAL branch named after the default exists and resolves, so
    make_worktree can branch off it. Idempotent and cheap when already healthy."""
    def say(m):
        if alert_fn:
            try:
                alert_fn(m)
            except Exception:
                pass

    wd = Path(workdir)
    healthy = False
    if (wd / ".git").exists():
        sh(["git", "-C", str(wd), "fetch", "--quiet", "--prune", "origin"], timeout=600)
        if _detect_default_branch(wd) is not None:
            healthy = True

    if not healthy:
        say(f"🧹 Workdir {wd} is missing/broken — re-cloning {repo} fresh.")
        import shutil
        shutil.rmtree(wd, ignore_errors=True)
        wd.parent.mkdir(parents=True, exist_ok=True)
        url = clone_url or f"https://github.com/{repo}.git"
        _, err, rc = sh(["git", "clone", "--quiet", url, str(wd)], timeout=1800)
        if rc != 0:
            raise RuntimeError(f"clone of {repo} failed: {err}")

    branch = _detect_default_branch(wd)
    if branch is None:
        raise RuntimeError(f"{repo} clone at {wd} has no resolvable default branch")

    # Make sure a local branch exists, points at origin, and is checked out, so
    # worktrees can branch off it. Prune any stale worktree registrations.
    sh(["git", "-C", str(wd), "checkout", "-B", branch, f"origin/{branch}"])
    sh(["git", "-C", str(wd), "reset", "--hard", f"origin/{branch}"])
    sh(["git", "-C", str(wd), "worktree", "prune"])
    return branch


def make_worktree(main_workdir: Path, base: Path, slug: str, base_branch: str) -> tuple[Path, str]:
    """Create an isolated worktree + branch for a candidate. `slug` is the issue
    number for a single-sample run ("3"), or "3-s1" etc. when fanning out >1
    candidate per issue. Returns (worktree_path, branch_name)."""
    wt = base / f"wt-issue-{slug}"
    branch = f"fix/issue-{slug}"
    # Pick a base that actually resolves: the local branch, else origin/<branch>.
    # Guards against the 'invalid reference: master' failure when only the
    # remote-tracking ref exists.
    base_ref = base_branch
    if not _resolves(main_workdir, base_ref):
        if _resolves(main_workdir, f"origin/{base_branch}"):
            base_ref = f"origin/{base_branch}"
        else:
            raise RuntimeError(
                f"base branch '{base_branch}' does not resolve in {main_workdir} "
                "(clone unhealthy — ensure_clone should have repaired it)")
    with WORKTREE_LOCK:  # serialize only the shared-.git setup, not the coding
        sh(["git", "-C", str(main_workdir), "worktree", "remove", "--force", str(wt)])
        sh(["git", "-C", str(main_workdir), "branch", "-D", branch])
        _, err, rc = sh(["git", "-C", str(main_workdir), "worktree", "add", "-b",
                         branch, str(wt), base_ref])
        if rc != 0:
            raise RuntimeError(f"worktree add failed for {slug}: {err}")
    return wt, branch


def _capture_patch(wt: str, base_branch: str) -> str:
    """The candidate's source diff vs the base branch — used by the selection
    cascade (AST-normalized vote) and the publication report."""
    diff, _, _ = sh(["git", "-C", wt, "diff", f"{base_branch}...HEAD"])
    return diff


# --- Stage 1: code (parallel) ----------------------------------------------

# Worker scratch that must never enter a commit/diff (ulw notepad + evidence,
# codegraph caches, ulw note files).
_SCRATCH_PREFIXES = (".omo", ".codegraph", ".openclaw-ulw", ".specify", ".pytest_cache")
# Dependency lockfiles a worker regenerates as a side effect of setting up its
# env (e.g. codex runs `uv`, dropping a stray uv.lock). A bug fix should never
# touch deps; if it genuinely needs a dep change, that's a bigger change that
# goes to a PR anyway. Reviewer flagged these as the #1 blocker on every reject.
_SCRATCH_BASENAMES = ("uv.lock", "__pycache__")


def _is_scratch(path: str) -> bool:
    p = path.lower()
    base = p.rsplit("/", 1)[-1]
    return (any(p.startswith(pre) for pre in _SCRATCH_PREFIXES)
            or base in _SCRATCH_BASENAMES
            or "__pycache__" in p
            or ("ulw" in p and p.endswith("notes.md"))
            or p.endswith(".pyc"))


def usage_ok() -> bool:
    """True if the Codex subscription window/week is under the halt threshold.

    The gate is an optional external tool: set $PINCER_USAGE_GATE to its path.
    If unset or absent, the gate is skipped (treated as safe to dispatch)."""
    gate = os.environ.get("PINCER_USAGE_GATE")
    if not gate or not os.path.exists(gate):
        print("[debug] usage gate not configured (PINCER_USAGE_GATE unset/missing) — skipping")
        return True
    try:
        rc = subprocess.run(
            ["python3", gate, "--provider", "codex"],
            capture_output=True, timeout=60).returncode
        return rc != 2  # 2 == window/week >= 80%
    except Exception:
        LOG.exception("Usage gate failed")
        return True  # fail open — never block on a gate error


def stage_code(repo: str, main_workdir: Path, base: Path, n: int, cfg, base_branch: str,
               sample: int = 0, samples: int = 1) -> dict:
    title, brief = issue_brief(repo, n, workdir=str(main_workdir), sample=sample)
    slug = str(n) if samples == 1 else f"{n}-s{sample}"
    wt, branch = make_worktree(main_workdir, base, slug, base_branch)
    res = ra.dispatch(brief, workdir=wt, config=cfg)
    # Worker left changes uncommitted (no-git contract). _stage_changes stages
    # everything and drops worker scratch so it never enters the diff. (Do NOT
    # touch .gitignore — that reads as scope creep and trips the reviewer.)
    changed = _stage_changes(str(wt))
    committed = False
    patch = ""
    if changed:
        sh(["git", "-C", str(wt), "commit", "-q", "-m", f"Fix #{n}: {title}"])
        committed = True
        patch = _capture_patch(str(wt), base_branch)
    return {"issue": n, "sample": sample, "title": title, "worktree": str(wt),
            "branch": branch, "worker_status": res.status, "runtime": res.runtime,
            "fallback": res.fallback_used, "changed": changed, "committed": committed,
            "patch": patch}


# --- Stage 2: sandbox (SERIAL via lock) ------------------------------------

def stage_sandbox(cand: dict, test_cmd: str) -> dict:
    if not cand.get("committed"):
        cand["sandbox"] = "skip_no_changes"
        return cand
    with sandbox_slot():  # memory-aware: up to max_concurrent VMs, RAM-gated
        out, err, rc = sh(["python3", str(THIS / "sandbox_gate.py"), "--workdir",
                           cand["worktree"], "--test", test_cmd, "--json"], timeout=1800)
    try:
        j = json.loads(out)
        cand["sandbox"] = j.get("verdict", "error")
        cand["results"] = j.get("results")  # structured counts for regression ranking
        cand["sandbox_fail"] = _extract_test_failure(j.get("stdout_tail", "") + "\n" + j.get("stderr_tail", ""))
    except Exception:
        LOG.exception("Failed to parse sandbox output for issue #%s", cand.get("issue"))
        cand["sandbox"] = "error"
        cand["results"] = None
        cand["sandbox_fail"] = (err or out)[-500:]
    return cand


def _extract_test_failure(blob: str) -> str:
    """Pull the meaningful test-failure lines out of the sandbox output (the
    `N failed, M passed` summary + the FAILED test names + the first assertion),
    skipping the crabbox provisioning/cleanup boilerplate that dominates the tail."""
    lines = blob.splitlines()
    keep = [ln for ln in lines if any(m in ln for m in
            ("FAILED", "ERROR", "passed", "failed", "AssertionError", "Error:", "assert "))
            and not any(noise in ln for noise in ("crabbox", "lease", "provision", "rsync", "debconf"))]
    summary = keep[-12:] if keep else lines[-8:]
    return "\n".join(summary)[:1200]


# --- Stage 3: review (parallel) --------------------------------------------

def stage_review(cand: dict, repo: str, base_branch: str) -> dict:
    if cand.get("sandbox") != "pass":
        cand["review"] = {"verdict": "reject", "blockers": ["sandbox not green"]}
        return cand
    wt = cand["worktree"]
    diff, _, _ = sh(["git", "-C", wt, "diff", f"{base_branch}...HEAD"])
    criteria = ("Eloquent, generalizable, PII-free, novel, builds clean, minimal/scoped, "
                "docs updated if needed. CRITICAL: the change must include a test that "
                "would FAIL on the original buggy code and PASS with this fix — judge "
                "whether the added/changed test actually exercises the reported bug, not "
                "just that some test exists.")
    v = rv.review(diff, cand["title"], criteria, repo_workdir=wt, timeout=600)
    cand["review"] = {"verdict": v.verdict, "blockers": v.blockers}
    cand["_review_obj"] = v
    return cand


def _stage_changes(wt: str) -> list[str]:
    """Stage everything, drop scratch, return the real changed files."""
    sh(["git", "-C", wt, "add", "-A"])
    co, _, _ = sh(["git", "-C", wt, "diff", "--cached", "--name-only"])
    staged = [f for f in co.split("\n") if f.strip()]
    scratch = [f for f in staged if _is_scratch(f)]
    if scratch:
        sh(["git", "-C", wt, "reset", "-q", "--"] + scratch)
    return [f for f in staged if not _is_scratch(f)]


# --- Stage 3.5: revise rejected fixes once with the reviewer's blockers -----

def _revise_once(cand: dict, repo: str, base_branch: str, test_cmd: str, cfg,
                 feedback: str) -> bool:
    """One revision pass: hand the worker concrete feedback, re-sandbox,
    re-review, refresh the patch. Returns True iff the worker actually changed
    something (False == no-op: identical code, no point re-verifying)."""
    wt = cand["worktree"]
    _, brief = issue_brief(repo, cand["issue"], workdir=cand["worktree"])
    revision = (brief + "\n\n" + feedback
                + "\nKeep the change minimal and narrow, and make sure the failing-first "
                "test still proves the fix without breaking other tests.")
    res = ra.dispatch(revision, workdir=wt, config=cfg)
    cand["revise_runtime"] = res.runtime
    changed = _stage_changes(wt)
    if not changed:
        cand["revise_noop"] = True
        return False
    sh(["git", "-C", wt, "commit", "-q", "-m", f"Address review feedback: #{cand['issue']}"])
    cand["changed"] = sorted(set(cand.get("changed", []) + changed))
    cand["patch"] = _capture_patch(wt, base_branch)
    stage_sandbox(cand, test_cmd)
    stage_review(cand, repo, base_branch)
    return True


def _revise_feedback(cand: dict, repo: str, interpret: bool) -> str | None:
    """Build the revision feedback for the candidate's CURRENT failing reason
    (a sandbox regression or a reviewer rejection). Returns None when there is
    nothing to fix (green + approved). When `interpret`, an Opus reading of the
    failure is prepended — critic-interpreted feedback beats raw stderr."""
    if cand.get("sandbox") != "pass":
        raw = cand.get("sandbox_fail") or ""
        base = ("Your change broke existing tests in the sandbox. Fix the regression — "
                "make the fix narrower so it doesn't break unrelated tests:\n" + raw)
        if interpret:
            note = rv.interpret_failure(raw, cand.get("title", ""),
                                        repo_workdir=cand["worktree"])
            if note:
                base = "Root-cause analysis of the failure:\n" + note + "\n\n" + base
        return base
    rvobj = cand.get("_review_obj")
    if rvobj is not None and rvobj.verdict != "approve" and rvobj.blockers:
        return ("An independent reviewer REJECTED your fix. Address ALL these blockers:\n"
                + "\n".join(f"- {b}" for b in rvobj.blockers))
    return None  # green + approved (or rejected with no actionable blockers)


def revise_loop(cand: dict, repo: str, base_branch: str, test_cmd: str, cfg,
                max_iters: int = 1, interpret: bool = True) -> dict:
    """Bounded execution-feedback loop (research #8): iterate fix→sandbox→review
    up to `max_iters` times, each round feeding the worker critic-interpreted
    feedback, until the candidate is green + approved or we stop making progress.
    max_iters=1 reproduces Pincer's original single-shot revise behavior."""
    cand["revised"] = True
    cand["revise_iters"] = 0
    for i in range(max_iters):
        feedback = _revise_feedback(cand, repo, interpret)
        if feedback is None:
            break  # nothing left to fix
        progressed = _revise_once(cand, repo, base_branch, test_cmd, cfg, feedback)
        cand["revise_iters"] = i + 1
        if not progressed:
            break
        rvobj = cand.get("_review_obj")
        if cand.get("sandbox") == "pass" and rvobj is not None and rvobj.verdict == "approve":
            break  # fully resolved
    return cand


_SIGNIFICANCE_LABEL = {
    "important": "🔴 Important — user-facing / high impact",
    "inconsequential": "🟡 Inconsequential — minor effect",
    "background": "⚪ Background — internal / low visibility",
}


def _pr_report(cand: dict, d, rvobj, lines: int, files: list[str]) -> str:
    """Owner-facing PR report: why it needs your eyes + significance metrics."""
    why = "; ".join(d.reasons)
    sig = _SIGNIFICANCE_LABEL.get(rvobj.significance, rvobj.significance)
    return "\n".join([
        f"Closes #{cand['issue']}.",
        "",
        "## Why this is a PR (not auto-merged)",
        f"{why}",
        "",
        "## Significance",
        f"- **Importance:** {sig}",
        f"- **Change type:** `{rvobj.change_type}`",
        f"- **Size:** {lines} lines across {len(files)} file(s)",
        "",
        "## Verification (already done autonomously)",
        f"- Sandbox (clean VM): **{cand.get('sandbox')}**",
        f"- Independent Opus-4.8 review: **{rvobj.verdict}**"
        + (f" (blockers: {', '.join(rvobj.blockers)})" if rvobj.blockers else ""),
        f"- Ships a test: **{'yes' if any('test' in f.lower() for f in files) else 'no'}**"
        " (failing-first discipline; sandbox runs the full suite with real deps).",
        "",
        f"## Files\n" + "\n".join(f"- `{f}`" for f in files),
        "",
        "_Bug fixes ship automatically; features and bigger changes come to you first._",
    ])


# --- Stage 4: gate + publish (serial publish) ------------------------------

def stage_gate(cand: dict, repo: str, main_workdir: Path, allow_merge: bool, base_branch: str) -> dict:
    if cand.get("sandbox") != "pass" or "_review_obj" not in cand:
        cand["gate"] = {"action": "open_pr", "reasons": ["did not reach review"]}
        return cand
    wt = cand["worktree"]
    diff, _, _ = sh(["git", "-C", wt, "diff", f"{base_branch}...HEAD"])
    files = [f for f in cand["changed"]]
    lines = sum(1 for ln in diff.splitlines()
                if ln[:1] in "+-" and not ln.startswith(("+++", "---")))
    secrets = any(k in diff.lower() for k in
                  ("api_key", "secret", "token=", "password", "-----begin"))
    # Build/lint verification is language-agnostic: the sandbox stage already
    # ran the repo's real test_command in a clean VM (which compiles TS, builds
    # Go/Rust, etc.) — reaching here means it passed. So build/lint are clean.
    # Real lint issues (dead code, style) are caught by the Opus reviewer.
    rvobj = cand["_review_obj"]
    has_test = any("test" in f.lower() for f in files)
    gi = pg.GateInputs(
        repo=pg.RepoMeta(owner=repo.split("/")[0], name=repo.split("/")[1], is_owned=True),
        diff=pg.DiffStats(lines_changed=lines, files=files),
        worker_status="done", tests_green=True, lint_clean=True,
        build_clean=True, has_secrets=secrets,
        docs_updated_if_needed=True, review=rvobj,
        change_type=rvobj.change_type, has_test=has_test)
    d = pg.decide(gi)
    cand["gate"] = {"action": d.action, "reasons": d.reasons, "danger": d.danger_surface}

    branch = cand.get("branch") or f"fix/issue-{cand['issue']}"
    if d.action == "auto_merge" and allow_merge:
        with PUBLISH_LOCK:  # serialize merges to shared default branch
            sh(["git", "-C", str(main_workdir), "checkout", base_branch])
            sh(["git", "-C", str(main_workdir), "merge", "--no-ff", branch, "-m",
                f"Merge #{cand['issue']}: {cand['title']} (auto-merged by pincer hybrid)"])
            _, _, prc = sh(["git", "-C", str(main_workdir), "push", "origin", base_branch])
            if prc == 0:
                sh(["gh", "issue", "close", str(cand["issue"]), "-R", repo, "-c",
                    "Fixed and auto-merged to main by the pincer parallel loop."])
                cand["published"] = "auto_merged"
            else:
                cand["published"] = "merge_push_failed"
    else:
        # push branch + open PR with a decision report + significance metrics
        sh(["git", "-C", wt, "push", "-q", "origin", branch])
        title = f"Fix #{cand['issue']}: {cand['title']}"
        body = _pr_report(cand, d, rvobj, lines, files)
        sh(["gh", "pr", "create", "-R", repo, "--head", branch, "--base", base_branch,
            "--title", title, "--body", body])
        cand["published"] = f"pr_{d.action}"
        cand["significance"] = rvobj.significance
        cand["change_type"] = rvobj.change_type
    cand.pop("_review_obj", None)
    return cand


@dataclasses.dataclass(frozen=True)
class SelectionTuning:
    """Pre-bench knobs. Defaults reproduce Pincer's original cheap loop exactly
    (one candidate per issue, single-shot revise, no reproduction tests)."""
    samples: int = 1              # candidate coders fanned out PER ISSUE
    max_revise_iters: int = 1     # bounded execution-feedback loop depth
    repro_tests: bool = False     # generate + F->P-filter reproduction tests
    interpret_failures: bool = True
    repro_model: str = "claude-opus-4-8"

    @classmethod
    def load(cls, path=None):
        import os
        cfg_path = path or os.environ.get("PINCER_CONFIG",
                                          str(Path.home() / ".openclaw" / "pincer.toml"))
        p = Path(cfg_path)
        if not p.exists():
            return cls()
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore
            except ImportError:
                return cls()
        try:
            data = tomllib.loads(p.read_text())
        except Exception:
            LOG.exception("Failed to load selection tuning from %s", p)
            return cls()
        s = data.get("selection", {})
        return cls(
            samples=int(s.get("samples", 1)),
            max_revise_iters=int(s.get("max_revise_iters", 1)),
            repro_tests=bool(s.get("repro_tests", False)),
            interpret_failures=bool(s.get("interpret_failures", True)),
            repro_model=str(s.get("repro_model", "claude-opus-4-8")),
        )


def cand_id(c: dict) -> str:
    """Unique state key for a candidate (issue + sample), so multiple candidates
    per issue don't collide in the state map."""
    s = c.get("sample", 0)
    return str(c["issue"]) if not s else f"{c['issue']}-s{s}"


def infra_failures(cands: list) -> list:
    """Candidates that failed for INFRASTRUCTURE reasons — couldn't be built or
    tested at all — as opposed to 'a fix was attempted but didn't resolve it'.

    worker_status == 'error'  -> worktree/clone/dispatch crash (e.g. the
                                 'invalid reference: master' failure).
    sandbox     == 'error'    -> VM/dep/network problem (not a test failure a
                                 code edit could fix).

    A whole loop of these means a pincer/env problem worth a human's eyes, not a
    quiet 'no winner'."""
    return [c for c in cands
            if c.get("worker_status") == "error" or c.get("sandbox") == "error"]


def _cascade_reviewer(repo: str, base_branch: str):
    """Adapter: the selection cascade's tie-breaker. Runs the real Opus review
    (populating _review_obj for reuse downstream) and returns approve? — called
    at most once per finalist, only when execution signals leave a tie."""
    def _r(cand: dict) -> bool:
        if not cand.get("_review_obj"):
            stage_review(cand, repo, base_branch)
        rvobj = cand.get("_review_obj")
        return bool(rvobj is not None and rvobj.verdict == "approve")
    return _r


def stage_repro(issue: int, issue_cands: list[dict], repo: str, main_workdir: Path,
                base: Path, base_branch: str, tuning: SelectionTuning) -> bool:
    """Reproduction-test stage (research #1): generate one F->P test for the
    issue, VALIDATE it actually fails on the unpatched base, then mark which
    green candidates flip it. Sets cand['repro_flip']; returns whether a valid
    repro test exists (has_repro). Best-effort: any failure returns False so the
    cascade falls back to regression-only ranking. Heavy (VM per candidate),
    serialized under sandbox_slot() — off by default."""
    import sandbox_gate as sg
    green = [c for c in issue_cands if c.get("sandbox") == "pass"]
    if len(green) < 2:
        return False  # nothing to disambiguate
    try:
        title, body = _issue_text(repo, issue)
        hint = lz.localize(str(main_workdir), f"{title} {body}").hint_block()
        repro = rp.generate(f"{title}\n\n{body}", str(main_workdir),
                            hint_block=hint, model=tuning.repro_model)
        if repro is None:
            return False
        sgcfg = sg.SandboxConfig.from_pincer_toml()
        test_one = f"python3 -m pytest -q {repro.path}"

        # Validate on a clean base checkout: the test must FAIL on buggy code.
        base_wt, _ = make_worktree(main_workdir, base, f"repro-{issue}", base_branch)
        (Path(base_wt) / repro.path).write_text(repro.source)
        with SANDBOX_LOCK:
            base_v = sg.gate(workdir=base_wt, test_command=test_one, config=sgcfg)
        base_res = base_v.results
        if base_res is None or not rp.is_valid_repro(base_res):
            sh(["git", "-C", str(main_workdir), "worktree", "remove", "--force", str(base_wt)])
            return False  # didn't reproduce -> untrusted, discard

        # Mark each green candidate that flips it (red base -> green candidate).
        for c in green:
            wt = c["worktree"]
            repro_file = Path(wt) / repro.path
            repro_file.write_text(repro.source)
            try:
                with sandbox_slot():
                    cv = sg.gate(workdir=wt, test_command=test_one, config=sgcfg)
                c["repro_flip"] = bool(cv.results is not None and rp.flips(base_res, cv.results))
            finally:
                repro_file.unlink(missing_ok=True)  # never pollute the candidate diff
        sh(["git", "-C", str(main_workdir), "worktree", "remove", "--force", str(base_wt)])
        return True
    except Exception as e:
        LOG.exception("Reproduction stage failed on issue #%s", issue)
        alert(f"  ⚠️ repro stage error on #{issue}: {e}")
        return False


def finalize_chosen(cand: dict, repo: str, base_branch: str, test_cmd: str, cfg,
                    tuning: SelectionTuning) -> dict:
    """Drive one selected candidate to a terminal state: ensure it has a review
    verdict, then run the bounded revise loop against whatever's still failing
    (a regression or a reviewer rejection). max_revise_iters=1 == original
    single-shot behavior."""
    if cand.get("sandbox") == "pass" and not cand.get("_review_obj"):
        stage_review(cand, repo, base_branch)
    if _revise_feedback(cand, repo, interpret=False) is not None and not cand.get("revised"):
        revise_loop(cand, repo, base_branch, test_cmd, cfg,
                    max_iters=tuning.max_revise_iters,
                    interpret=tuning.interpret_failures)
    if cand.get("sandbox") == "pass" and not cand.get("_review_obj"):
        stage_review(cand, repo, base_branch)
    return cand


def _history_rows(repo: str, issue: int) -> list[dict]:
    rows = work_history.attempts(repo, issue)
    if not isinstance(issue, str):
        rows += work_history.attempts(repo, str(issue))
    return rows


def _failure_context(repo: str, issue: int) -> str:
    return work_history.failure_context(_history_rows(repo, issue))


def _review_summary(cand: dict | None) -> str:
    if not cand:
        return ""
    review = cand.get("review")
    if isinstance(review, dict):
        verdict = review.get("verdict", "")
        blockers = review.get("blockers") or []
        if blockers:
            return f"{verdict}: {', '.join(str(b) for b in blockers)}"
        return str(verdict)
    rvobj = cand.get("_review_obj")
    if rvobj is not None:
        blockers = getattr(rvobj, "blockers", None) or []
        verdict = getattr(rvobj, "verdict", "")
        if blockers:
            return f"{verdict}: {', '.join(str(b) for b in blockers)}"
        return str(verdict)
    return str(review or "")


def _candidate_reason(cand: dict | None, outcome: str) -> str:
    if cand is None:
        return outcome
    if outcome == "shipped":
        return str(cand.get("published") or "published")
    if outcome == "failed":
        return str(cand.get("sandbox_fail") or cand.get("sandbox") or "verification failed")[:240]
    if outcome == "rejected":
        return _review_summary(cand)[:240] or "review rejected"
    if outcome == "error":
        return str(cand.get("error") or cand.get("sandbox_fail") or "infrastructure error")[:240]
    return outcome


def _record_work_history(repo: str, issues: list[int], cands: list[dict], chosen: list[dict],
                         scorecard: dict, run_id: str, ts: str) -> None:
    by_issue: dict[int, list[dict]] = {}
    for cand in cands:
        by_issue.setdefault(cand["issue"], []).append(cand)
    chosen_by_issue = {cand["issue"]: cand for cand in chosen}
    infra_issues = set(scorecard.get("infra_failures", []) or [])
    failed_issues = set(scorecard.get("failed_verification", []) or [])
    shipped_issues = set(scorecard.get("merged", []) or []) | set(scorecard.get("prd", []) or [])
    no_winner_issues = set(scorecard.get("no_winner", []) or [])

    for issue in issues:
        cand = chosen_by_issue.get(issue)
        if cand is None and by_issue.get(issue):
            cand = by_issue[issue][0]
        if issue in shipped_issues:
            outcome = "shipped"
        elif issue in infra_issues:
            outcome = "error"
        elif issue in failed_issues:
            outcome = "failed"
        elif issue in no_winner_issues:
            outcome = "no_winner"
        elif cand and (cand.get("review") or {}).get("verdict") == "reject":
            outcome = "rejected"
        else:
            outcome = "failed"
        try:
            work_history.record(
                repo=repo,
                issue=issue,
                run_id=run_id,
                runtime=(cand or {}).get("runtime", ""),
                patch_hash=sel.normalize_patch((cand or {}).get("patch", "") or ""),
                sandbox=(cand or {}).get("sandbox", ""),
                review=_review_summary(cand),
                outcome=outcome,
                reason=_candidate_reason(cand, outcome),
                ts=ts,
            )
        except Exception:
            LOG.exception("Failed to record work history for %s#%s", repo, issue)


def run(repo: str, workdir: str, issues: list[int], max_coders: int, allow_merge: bool,
        tuning: "SelectionTuning | None" = None, thread=None):
    tuning = tuning or SelectionTuning.load()
    main_workdir = Path(workdir).resolve()
    samples = max(1, tuning.samples)
    # Thread every alert under one root. If a parent (loop driver / spec) handed
    # us its thread, reuse it so the whole run is one Telegram reply-chain;
    # otherwise start our own root here.
    global _THREAD
    _THREAD = thread if thread is not None else make_alert_thread(f"🔧 {repo.split('/')[-1]}")
    _start_run_log(repo, datetime.now().strftime("%Y%m%d-%H%M%S"))
    _init_sandbox_gate()  # size the memory-aware VM semaphore from [sandbox] config

    # Self-heal the workdir BEFORE anything reads it: re-clone if missing/broken,
    # else fetch + hard-reset. Returns a default branch guaranteed to resolve.
    # This is the fix for the 'invalid reference: master' run failures.
    base_branch = ensure_clone(repo, main_workdir, alert_fn=alert)
    base = main_workdir.parent / "pincer-worktrees"
    base.mkdir(exist_ok=True)
    # ulw OFF for orchestrator workers: ulw insists on running the suite to prove
    # RED->GREEN, but the worker sandbox can't install a real project's deps
    # (sqlglot/pytest), so codex always blocks -> falls back to claude-code. Our
    # Crabbox sandbox (installs deps) + independent reviewer do the real
    # verification, so the worker just makes the change + writes a test fast.
    cfg = dataclasses.replace(ra.RuntimeConfig.from_pincer_toml(), ultrawork=False)
    test_cmd = repo_test_cmd(main_workdir)
    state = {"repo": repo, "issues": issues, "base_branch": base_branch,
             "tuning": dataclasses.asdict(tuning), "candidates": {}, "selection": {}}
    state_path = Path("/tmp/parallel-orchestrator-state.json")

    def save():
        state_path.write_text(json.dumps(state, indent=2, default=str))

    if not usage_ok():
        alert("⏸️ Usage gate ≥80% — holding the loop before dispatch. Will not burn the window.",
              level="milestone")
        state["result"] = "halted_usage"
        save()
        return state

    mode = (f"{samples}× candidates/issue, cascade-select"
            if samples > 1 else "1 candidate/issue")
    alert(f"🧵 Parallel loop START — {repo} issues {issues}, up to {max_coders} coders "
          f"in parallel ({mode}; revise≤{tuning.max_revise_iters}; "
          f"repro={'on' if tuning.repro_tests else 'off'}; base: {base_branch}).",
          level="milestone")

    # Stage 1: code — parallel over (issue × sample). Each candidate gets its own
    # worktree; multiple samples per issue feed the selection cascade.
    jobs = [(n, s) for n in issues for s in range(samples)]
    cands = []
    with cf.ThreadPoolExecutor(max_workers=max_coders) as ex:
        futs = {ex.submit(stage_code, repo, main_workdir, base, n, cfg, base_branch, s, samples): (n, s)
                for (n, s) in jobs}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            n, s = futs[fut]
            try:
                c = fut.result()
            except Exception as e:
                LOG.exception("Coding stage failed for %s#%s sample %s", repo, n, s)
                c = {"issue": n, "sample": s, "worker_status": "error",
                     "error": str(e), "committed": False}
            cands.append(c)
            state["candidates"][cand_id(c)] = c
            save()
            mark = "✓" if c.get("committed") else "∅"
            alert(f"  {mark} coded #{n}{'·s%d' % s if samples > 1 else ''} [{i}/{len(jobs)}] — "
                  f"{c.get('worker_status','?')} ({c.get('runtime','?')}), "
                  f"{len(c.get('changed', []))} file(s)")
    done = [c for c in cands if c.get("committed")]
    alert(f"⌨️ Stage 1 CODE done — {len(done)}/{len(jobs)} candidate(s) produced changes.")

    # Stage 2: sandbox — SERIAL, structured results for regression-aware ranking.
    for c in done:
        stage_sandbox(c, test_cmd)
        state["candidates"][cand_id(c)] = c
        save()
    alert(f"📦 Stage 2 SANDBOX done — {sum(1 for c in done if c.get('sandbox')=='pass')}/{len(done)} green.")

    by_issue: dict[int, list] = {}
    for c in done:
        by_issue.setdefault(c["issue"], []).append(c)

    # Stage 2.5: reproduction tests (optional) — generate one F->P test per
    # issue and mark candidates that flip it. Gated; heavy; off by default.
    has_repro: dict[int, bool] = {}
    if tuning.repro_tests:
        alert("🧪 Reproduction-test stage — generating F→P tests + flip-checking candidates.")
        for n, issue_cands in by_issue.items():
            has_repro[n] = stage_repro(n, issue_cands, repo, main_workdir, base, base_branch, tuning)
            if has_repro[n]:
                flips = [c["issue"] for c in issue_cands if c.get("repro_flip")]
                alert(f"  🧪 #{n}: valid repro test — {len(flips)} candidate(s) flip it.")

    # Stage 2.6: SELECT — cascade one winner per issue (regression → reproduction
    # → AST-vote → reviewer). For samples==1 this is a no-op pass-through.
    reviewer = _cascade_reviewer(repo, base_branch)
    chosen = []
    for n, issue_cands in by_issue.items():
        result = sel.select(issue_cands, reviewer=reviewer, has_repro=has_repro.get(n, False))
        state["selection"][str(n)] = result.to_dict()
        if result.chosen is not None:
            result.chosen["selected"] = True
            result.chosen["selection_stage"] = result.stage
            chosen.append(result.chosen)
        if samples > 1:
            alert(f"  🎯 #{n}: chose {cand_id(result.chosen) if result.chosen else 'none'} "
                  f"via {result.stage} (from {result.diagnostics.get('n_eligible', 0)} eligible).")
        save()

    # Stage 2.7 + 3: finalize each chosen candidate — review + bounded revise loop.
    alert(f"🔎 Finalizing {len(chosen)} selected candidate(s) (review + revise≤{tuning.max_revise_iters}).")
    for c in chosen:
        finalize_chosen(c, repo, base_branch, test_cmd, cfg, tuning)
        state["candidates"][cand_id(c)] = c
        save()
        rev = c["_review_obj"].verdict if c.get("_review_obj") else "-"
        alert(f"  ✅ #{c['issue']} → sandbox={c.get('sandbox')}, review={rev}, "
              f"revise_iters={c.get('revise_iters', 0)}")

    # Stage 4: gate + publish (serial publish via lock) — only chosen+green.
    publishable = [c for c in chosen if c.get("sandbox") == "pass"]
    for c in publishable:
        stage_gate(c, repo, main_workdir, allow_merge, base_branch)
        state["candidates"][cand_id(c)] = c
        save()

    merged = [c for c in publishable if c.get("published") == "auto_merged"]
    prd = [c for c in publishable if str(c.get("published", "")).startswith("pr")]
    failed = [c for c in chosen if c.get("sandbox") not in ("pass", None)]
    no_winner = [n for n in issues if n not in {c["issue"] for c in chosen}]
    infra = infra_failures(cands)
    state["result"] = "done"
    state["scorecard"] = {"merged": [c["issue"] for c in merged],
                          "prd": [c["issue"] for c in prd],
                          "failed_verification": [c["issue"] for c in failed],
                          "no_winner": no_winner,
                          "infra_failures": sorted({c["issue"] for c in infra})}
    _record_work_history(
        repo,
        issues,
        cands,
        chosen,
        state["scorecard"],
        run_id=_RUN_LOG.stem if _RUN_LOG is not None else f"{repo}-{_now_stamp()}",
        ts=datetime.now().isoformat(timespec="seconds"),
    )
    # Loud escalation: if NOTHING shipped and the cause was infrastructure (not
    # "no fix found"), this is a pincer/env problem — say so plainly instead of a
    # quiet 'no winner'. This is exactly what the sql-metadata clone failure
    # needed: an immediate, unmistakable signal rather than a silent 6-hourly no-op.
    if (len(merged) + len(prd)) == 0 and infra:
        first = next((c.get("error") or c.get("sandbox_fail") for c in infra
                      if c.get("error") or c.get("sandbox_fail")), "")
        alert(f"🚨 INFRA FAILURE — {len(infra)}/{len(cands)} candidate(s) couldn't even be "
              f"built/tested (not 'no fix found'). Likely a pincer/env problem worth a look. "
              f"First error: {str(first)[:300]}", level="critical")
    alert(f"🏁 Parallel loop DONE — {len(merged)} merged, {len(prd)} PR'd, "
          f"{len(failed)} failed verification, {len(no_winner)} with no winning candidate"
          + (f", {len(infra)} INFRA failure(s)" if infra else "") + ". "
          + " | ".join("#%s:%s" % (c["issue"], c.get("published") or c.get("sandbox", "?"))
                       for c in chosen), level="milestone")
    save()
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--issues", required=True, help="comma-separated issue numbers")
    ap.add_argument("--max-coders", type=int, default=6)
    ap.add_argument("--no-merge", action="store_true", help="PR everything, never auto-merge")
    # Pre-bench selection knobs. Omitted -> values from [selection] in pincer.toml
    # (which themselves default to the original 1-candidate behavior).
    ap.add_argument("--samples", type=int, default=None,
                    help="candidate coders fanned out PER ISSUE (selection cascade picks one)")
    ap.add_argument("--max-revise-iters", type=int, default=None,
                    help="bounded execution-feedback loop depth (default 1)")
    ap.add_argument("--repro-tests", dest="repro_tests", action="store_true", default=None,
                    help="generate + F->P-filter reproduction tests (heavy)")
    ap.add_argument("--no-repro-tests", dest="repro_tests", action="store_false",
                    help="disable reproduction tests")
    a = ap.parse_args()
    issues = [int(x) for x in a.issues.split(",") if x.strip()]

    base_tuning = SelectionTuning.load()
    tuning = dataclasses.replace(
        base_tuning,
        samples=a.samples if a.samples is not None else base_tuning.samples,
        max_revise_iters=(a.max_revise_iters if a.max_revise_iters is not None
                          else base_tuning.max_revise_iters),
        repro_tests=(a.repro_tests if a.repro_tests is not None else base_tuning.repro_tests),
    )
    run(a.repo, a.workdir, issues, a.max_coders, allow_merge=not a.no_merge, tuning=tuning)


if __name__ == "__main__":
    main()
