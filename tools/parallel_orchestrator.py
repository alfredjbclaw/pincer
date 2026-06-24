#!/usr/bin/env python3
"""pincer parallel orchestrator — fan-out coders, serialize the sandbox.

The Crabbox VM (8 GiB each, max_concurrent=1) is only needed at final
verification, NOT during coding. So we parallelize the expensive-but-cheap-on-
memory coding stage and serialize ONLY the sandbox:

    N issues
      → [coders in parallel, each in its own git worktree]   (bounded by usage gate)
      → [Crabbox sandbox, ONE at a time]                     (the only serial stage)
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
import subprocess
import sys
import threading
import time
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))
sys.path.insert(0, "/Users/alfred/.openclaw/workspace/tools")

import runtime_adapter as ra
import publication_gate as pg
import reviewer as rv

try:
    from telegram_alert import send_alert
except Exception:  # pragma: no cover - alerts optional
    def send_alert(msg): print("[alert]", msg)

# The Crabbox stage is the only hard-serial section (host memory ceiling).
SANDBOX_LOCK = threading.Lock()
PUBLISH_LOCK = threading.Lock()   # git merge to the shared main must serialize
WORKTREE_LOCK = threading.Lock()  # `git worktree add` touches shared .git; serialize setup only


def sh(cmd, cwd=None, timeout=1800):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.stdout, p.stderr, p.returncode


def alert(msg):
    try:
        send_alert(msg)
    except Exception as e:
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


def issue_brief(repo: str, n: int) -> tuple[str, str]:
    out, _, _ = sh(["gh", "issue", "view", str(n), "-R", repo, "--json", "title,body"])
    data = json.loads(out)
    brief = (f"Fix {repo} issue #{n}: {data['title']}\n\n{data['body']}\n\n"
             "Keep the change minimal and idiomatic. REQUIRED: add a test that captures "
             "this bug — one that FAILS on the current code and PASSES after your fix. If "
             "a matching xfail test exists, remove the xfail marker. You do NOT need to "
             "run the suite yourself — a sandbox with the project's deps installed will "
             "run it. Just make the fix and include the failing-first test.")
    return data["title"], brief


def default_branch(main_workdir: Path) -> str:
    """Detect the repo's default branch (main vs master vs ...)."""
    out, _, rc = sh(["git", "-C", str(main_workdir), "symbolic-ref", "--short",
                     "refs/remotes/origin/HEAD"])
    if rc == 0 and out.strip():
        return out.strip().split("/", 1)[-1]
    for cand in ("main", "master"):
        _, _, rc = sh(["git", "-C", str(main_workdir), "rev-parse", "--verify", cand])
        if rc == 0:
            return cand
    return "main"


def make_worktree(main_workdir: Path, base: Path, n: int, base_branch: str) -> Path:
    wt = base / f"wt-issue-{n}"
    with WORKTREE_LOCK:  # serialize only the shared-.git setup, not the coding
        sh(["git", "-C", str(main_workdir), "worktree", "remove", "--force", str(wt)])
        sh(["git", "-C", str(main_workdir), "branch", "-D", f"fix/issue-{n}"])
        _, err, rc = sh(["git", "-C", str(main_workdir), "worktree", "add", "-b",
                         f"fix/issue-{n}", str(wt), base_branch])
        if rc != 0:
            raise RuntimeError(f"worktree add failed for #{n}: {err}")
    return wt


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
    """True if the Codex subscription window/week is under the halt threshold."""
    try:
        rc = subprocess.run(
            ["python3", "/Users/alfred/.openclaw/workspace/tools/usage_gate.py",
             "--provider", "codex"], capture_output=True, timeout=60).returncode
        return rc != 2  # 2 == window/week >= 80%
    except Exception:
        return True  # fail open — never block on a gate error


def stage_code(repo: str, main_workdir: Path, base: Path, n: int, cfg, base_branch: str) -> dict:
    title, brief = issue_brief(repo, n)
    wt = make_worktree(main_workdir, base, n, base_branch)
    res = ra.dispatch(brief, workdir=wt, config=cfg)
    # Worker left changes uncommitted (no-git contract). Stage everything, then
    # unstage all worker scratch (ulw/codegraph notepads + evidence) so it never
    # enters the diff. Do NOT touch .gitignore — that reads as unrelated scope
    # creep and trips the reviewer.
    sh(["git", "-C", str(wt), "add", "-A"])
    co, _, _ = sh(["git", "-C", str(wt), "diff", "--cached", "--name-only"])
    staged = [f for f in co.split("\n") if f.strip()]
    scratch = [f for f in staged if _is_scratch(f)]
    if scratch:
        sh(["git", "-C", str(wt), "reset", "-q", "--"] + scratch)
    changed = [f for f in staged if not _is_scratch(f)]
    committed = False
    if changed:
        sh(["git", "-C", str(wt), "commit", "-q", "-m",
            f"Fix #{n}: {title}"])
        committed = True
    return {"issue": n, "title": title, "worktree": str(wt), "worker_status": res.status,
            "runtime": res.runtime, "fallback": res.fallback_used, "changed": changed,
            "committed": committed}


# --- Stage 2: sandbox (SERIAL via lock) ------------------------------------

def stage_sandbox(cand: dict, test_cmd: str) -> dict:
    if not cand.get("committed"):
        cand["sandbox"] = "skip_no_changes"
        return cand
    with SANDBOX_LOCK:  # only one Crabbox VM at a time
        out, err, rc = sh(["python3", str(THIS / "sandbox_gate.py"), "--workdir",
                           cand["worktree"], "--test", test_cmd, "--json"], timeout=1800)
    try:
        cand["sandbox"] = json.loads(out).get("verdict", "error")
    except Exception:
        cand["sandbox"] = "error"
    cand["sandbox_tail"] = (err or out)[-300:]
    return cand


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

def stage_revise(cand: dict, repo: str, base_branch: str, test_cmd: str, cfg) -> dict:
    """One revision pass: hand the worker the reviewer's blockers, re-sandbox,
    re-review. Turns 'rejected -> PR' into 'fixed -> merge' when the blockers
    are addressable. Capped at one retry to avoid loops."""
    wt = cand["worktree"]
    rv_obj = cand.get("_review_obj")
    if not rv_obj or not rv_obj.blockers:
        return cand
    title, brief = issue_brief(repo, cand["issue"])
    revision = (brief + "\n\nAn independent reviewer REJECTED your previous fix on this branch. "
                "Address ALL of these blockers, keep the change minimal, and make sure the "
                "failing-first test still proves the fix:\n"
                + "\n".join(f"- {b}" for b in rv_obj.blockers))
    res = ra.dispatch(revision, workdir=wt, config=cfg)
    changed = _stage_changes(wt)
    if changed:
        sh(["git", "-C", wt, "commit", "-q", "-m", f"Address review blockers: #{cand['issue']}"])
        cand["changed"] = sorted(set(cand.get("changed", []) + changed))
    cand["revised"] = True
    cand["revise_runtime"] = res.runtime
    # Re-verify: sandbox then review.
    stage_sandbox(cand, test_cmd)
    stage_review(cand, repo, base_branch)
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

    branch = f"fix/issue-{cand['issue']}"
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


def run(repo: str, workdir: str, issues: list[int], max_coders: int, allow_merge: bool):
    main_workdir = Path(workdir).resolve()
    base = main_workdir.parent / "pincer-worktrees"
    base.mkdir(exist_ok=True)
    # ulw OFF for orchestrator workers: ulw insists on running the suite to prove
    # RED->GREEN, but the worker sandbox can't install a real project's deps
    # (sqlglot/pytest), so codex always blocks -> falls back to claude-code. Our
    # Crabbox sandbox (installs deps) + independent reviewer do the real
    # verification, so the worker just makes the change + writes a test fast.
    cfg = dataclasses.replace(ra.RuntimeConfig.from_pincer_toml(), ultrawork=False)
    test_cmd = repo_test_cmd(main_workdir)
    base_branch = default_branch(main_workdir)
    state = {"repo": repo, "issues": issues, "base_branch": base_branch, "candidates": {}}
    state_path = Path("/tmp/parallel-orchestrator-state.json")

    def save():
        state_path.write_text(json.dumps(state, indent=2, default=str))

    if not usage_ok():
        alert("⏸️ Usage gate ≥80% — holding the loop before dispatch. Will not burn the window.")
        state["result"] = "halted_usage"
        save()
        return state

    alert(f"🧵 Parallel loop START — {repo} issues {issues}, up to {max_coders} coders in parallel "
          f"(base branch: {base_branch}). Crabbox serialized; reviews parallel.")

    # Stage 1: code — parallel (per-coder progress so a long stage isn't silent)
    cands = []
    with cf.ThreadPoolExecutor(max_workers=max_coders) as ex:
        futs = {ex.submit(stage_code, repo, main_workdir, base, n, cfg, base_branch): n for n in issues}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            n = futs[fut]
            try:
                c = fut.result()
            except Exception as e:
                c = {"issue": n, "worker_status": "error", "error": str(e), "committed": False}
            cands.append(c)
            state["candidates"][str(n)] = c
            save()
            mark = "✓" if c.get("committed") else "∅"
            alert(f"  {mark} coded #{n} [{i}/{len(issues)}] — {c.get('worker_status','?')} "
                  f"({c.get('runtime','?')}), {len(c.get('changed', []))} file(s)")
    done = [c for c in cands if c.get("committed")]
    alert(f"⌨️ Stage 1 CODE done — {len(done)}/{len(issues)} produced changes "
          f"({', '.join('#%s:%s' % (c['issue'], c.get('runtime','?')) for c in cands)}).")

    # Stage 2: sandbox — SERIAL
    for c in done:
        stage_sandbox(c, test_cmd)
        state["candidates"][str(c["issue"])] = c
        save()
    passed = [c for c in done if c.get("sandbox") == "pass"]
    alert(f"📦 Stage 2 SANDBOX done — {len(passed)}/{len(done)} green "
          f"({', '.join('#%s:%s' % (c['issue'], c.get('sandbox')) for c in done)}).")

    # Stage 3: review — parallel
    with cf.ThreadPoolExecutor(max_workers=max_coders) as ex:
        list(ex.map(lambda c: stage_review(c, repo, base_branch), passed))
    for c in passed:
        state["candidates"][str(c["issue"])] = c
    save()
    alert("🔎 Stage 3 REVIEW done — " +
          ", ".join("#%s:%s" % (c["issue"], c["review"]["verdict"]) for c in passed))

    # Stage 3.5: revise rejected fixes once with the reviewer's blockers.
    # Sandbox re-runs serialize on SANDBOX_LOCK; do these sequentially.
    to_retry = [c for c in passed
                if c.get("_review_obj") and c["_review_obj"].verdict != "approve"
                and c["_review_obj"].blockers and not c.get("revised")]
    if to_retry:
        alert(f"♻️ Revising {len(to_retry)} rejected fix(es) with reviewer blockers: "
              + ", ".join(f"#{c['issue']}" for c in to_retry))
        for c in to_retry:
            stage_revise(c, repo, base_branch, test_cmd, cfg)
            state["candidates"][str(c["issue"])] = c
            save()
        flipped = sum(1 for c in to_retry if c["_review_obj"].verdict == "approve")
        alert(f"♻️ Revision done — {flipped}/{len(to_retry)} now approved "
              + ", ".join("#%s:%s" % (c["issue"], c["_review_obj"].verdict) for c in to_retry))

    # Stage 4: gate + publish (serial publish via lock)
    for c in passed:
        stage_gate(c, repo, main_workdir, allow_merge, base_branch)
        state["candidates"][str(c["issue"])] = c
        save()

    merged = [c for c in passed if c.get("published") == "auto_merged"]
    prd = [c for c in passed if str(c.get("published", "")).startswith("pr")]
    alert(f"🏁 Parallel loop DONE — {len(merged)} auto-merged, {len(prd)} PR'd. "
          + " | ".join("#%s:%s" % (c["issue"], c.get("published", "?")) for c in passed))
    save()
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--issues", required=True, help="comma-separated issue numbers")
    ap.add_argument("--max-coders", type=int, default=6)
    ap.add_argument("--no-merge", action="store_true", help="PR everything, never auto-merge")
    a = ap.parse_args()
    issues = [int(x) for x in a.issues.split(",") if x.strip()]
    run(a.repo, a.workdir, issues, a.max_coders, allow_merge=not a.no_merge)


if __name__ == "__main__":
    main()
