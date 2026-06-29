#!/usr/bin/env python3
"""Map one SWE-bench instance -> a Pincer run -> a model_patch prediction.

Bench mode differs from the GitHub maintainer loop in three ways:
  - the task comes from `problem_statement`, not `gh issue view`;
  - worktrees are seeded at the instance's `base_commit` (detached), so the
    diff is exactly against the graded base;
  - there is no publish/gate — the deliverable is the patch, graded later by the
    official harness.

Everything else is the SAME pipeline: hierarchical localization -> fan-out
coders (`--samples`) -> sandbox (best-effort ranking) -> selection cascade. The
chosen candidate's `git diff base_commit` becomes the prediction.

`build_brief` is pure and tested; the live orchestration composes already-tested
modules (runtime_adapter, sandbox_gate, selection, localization, predictions).
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS.parent))  # pincer/tools on the path

import runtime_adapter as ra
import localization as lz
import selection as sel
import parallel_orchestrator as po
from . import predictions as pr
from .dataset import Instance


def build_brief(instance: Instance, *, hint_block: str = "", sample: int = 0,
                use_hints: bool = False) -> str:
    """The worker brief for a bench instance. `use_hints` controls whether the
    dataset's `hints_text` (pre-PR discussion) is included — some leaderboards
    consider that borderline, so it's off by default for clean numbers."""
    diversify = ""
    if sample > 0:
        diversify = (f"\n(Independent attempt #{sample + 1} — explore a different "
                     "approach if the obvious one looks fragile.)\n")
    hints = f"\nMaintainer hints:\n{instance.hints_text}\n" if (use_hints and instance.hints_text) else ""
    return (f"Resolve this issue in {instance.repo}.\n\n"
            f"{instance.problem_statement}\n\n{hints}{hint_block}{diversify}"
            "Make the minimal, idiomatic source change that fixes the issue. "
            "REQUIRED: add or update a test that fails on the current code and "
            "passes after your fix. You do NOT need to run the suite yourself.")


def prepare_clone(repo: str, cache_root: Path) -> Path:
    """Clone `repo` once into the cache (fetch if already present)."""
    cache_root.mkdir(parents=True, exist_ok=True)
    dest = cache_root / repo.replace("/", "__")
    if dest.exists():
        po.sh(["git", "-C", str(dest), "fetch", "--quiet", "--all"])
    else:
        po.sh(["git", "clone", "--quiet", f"https://github.com/{repo}.git", str(dest)],
              timeout=1800)
    return dest


def _base_worktree(clone: Path, base_commit: str, dest: Path) -> Path:
    po.sh(["git", "-C", str(clone), "worktree", "remove", "--force", str(dest)])
    _, err, rc = po.sh(["git", "-C", str(clone), "worktree", "add", "--detach",
                        str(dest), base_commit])
    if rc != 0:
        raise RuntimeError(f"worktree add at {base_commit[:8]} failed: {err}")
    return dest


def run_instance(instance: Instance, *, work_root: Path, tuning: po.SelectionTuning,
                 cfg=None, test_cmd: str = "python3 -m pytest -q",
                 use_sandbox: bool = True, use_hints: bool = False) -> pr.Prediction:
    """Produce one prediction. Best-effort throughout: any candidate that errors
    is dropped; if no candidate produces a patch, an empty patch is emitted (the
    harness scores it unresolved, which is the honest outcome)."""
    cfg = cfg or dataclasses.replace(
        ra.RuntimeConfig.from_pincer_toml(), ultrawork=False)
    samples = max(1, tuning.samples)
    clone = prepare_clone(instance.repo, work_root / "clones")
    hint_block = lz.localize(str(clone), instance.problem_statement).hint_block()
    inst_dir = work_root / "instances" / instance.instance_id
    inst_dir.mkdir(parents=True, exist_ok=True)

    cands = []
    for s in range(samples):
        wt = _base_worktree(clone, instance.base_commit, inst_dir / f"s{s}")
        brief = build_brief(instance, hint_block=hint_block, sample=s, use_hints=use_hints)
        try:
            res = ra.dispatch(brief, workdir=wt, config=cfg)
        except Exception as e:
            cands.append({"issue": instance.instance_id, "sample": s,
                          "committed": False, "error": str(e)})
            continue
        changed = po._stage_changes(str(wt))
        committed = False
        patch = ""
        if changed:
            po.sh(["git", "-C", str(wt), "commit", "-q", "-m",
                   f"Fix {instance.instance_id}"])
            committed = True
            patch = po.sh(["git", "-C", str(wt), "diff", instance.base_commit])[0]
        cands.append({"issue": instance.instance_id, "sample": s, "worktree": str(wt),
                      "committed": committed, "patch": patch, "runtime": res.runtime})

    committed = [c for c in cands if c.get("committed")]
    if use_sandbox:
        for c in committed:
            try:
                po.stage_sandbox(c, test_cmd)
            except Exception:
                c["sandbox"] = "error"

    result = sel.select(committed, has_repro=False) if committed else None
    chosen = result.chosen if result else None
    if chosen is None:
        return pr.Prediction(instance.instance_id, "", "pincer")

    raw = pr.git_diff(chosen["worktree"], instance.base_commit)
    return pr.Prediction(instance.instance_id, pr.extract_model_patch(raw), "pincer")
