#!/usr/bin/env python3
"""Validate the selection cascade end-to-end.

Two modes:
  --offline (default): drive `selection.select()` through realistic multi-stage
    candidate scenarios and assert each is resolved by the EXPECTED stage
    (regression → reproduction → majority-vote → reviewer). Runs instantly, no
    VM/credits — proves the cascade wiring picks correctly across stages.
  --live: run the real pipeline (`po.run`) with `--samples N` on a repo and
    report, per issue, which stage chose and whether the winner shipped. Heavy
    (coders + Crabbox); refuses to start if another pincer/crabbox run is in
    flight, so it never contends with a Command Center run.

    python3 tools/validate_cascade.py                       # offline assertions
    python3 tools/validate_cascade.py --live --repo O/R --issues 5,7 --samples 3
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))

import selection as sel


def _c(issue, *, sandbox="pass", regressions=0, repro_flip=None, patch="+x\n", committed=True):
    return {"issue": issue, "committed": committed, "sandbox": sandbox,
            "results": {"parsed": True, "regressions": regressions, "green": regressions == 0},
            "repro_flip": repro_flip, "patch": patch}


# (name, candidates, has_repro, reviewer, expected_issue, expected_stage)
def _scenarios():
    approve_2 = lambda c: c["issue"] == 2  # noqa: E731 — reviewer for the tie-break case
    return [
        ("regression_winner",
         [_c(1, sandbox="fail", regressions=3, patch="+a\n"),
          _c(2, regressions=0, patch="+b\n"),
          _c(3, sandbox="fail", regressions=1, patch="+c\n")],
         False, None, 2, "regression"),
        ("reproduction_breaks_tie",
         [_c(1, regressions=0, repro_flip=False, patch="+a\n"),
          _c(2, regressions=0, repro_flip=True, patch="+b\n")],
         True, None, 2, "reproduction"),
        ("majority_vote_consensus",
         [_c(1, regressions=0, patch="+    x = 1\n"),
          _c(2, regressions=0, patch="+x=1\n"),       # same after normalize
          _c(3, regressions=0, patch="+    y = 2\n")],
         False, None, 1, "majority_vote"),
        ("reviewer_tiebreak",
         [_c(1, regressions=0, patch="+    x = 1\n"),
          _c(2, regressions=0, patch="+    y = 2\n")],
         False, approve_2, 2, "reviewer"),
        ("single_candidate",
         [_c(9, regressions=0, patch="+z\n")],
         False, None, 9, "only_candidate"),
    ]


def run_offline() -> list:
    """Returns [(name, ok, detail)]. ok == picked the expected issue AND stage."""
    out = []
    for name, cands, has_repro, reviewer, exp_issue, exp_stage in _scenarios():
        r = sel.select(cands, has_repro=has_repro, reviewer=reviewer)
        chosen = r.chosen.get("issue") if r.chosen else None
        ok = (chosen == exp_issue and r.stage == exp_stage)
        out.append((name, ok, f"chose #{chosen} via {r.stage} (want #{exp_issue}/{exp_stage})"))
    return out


def _runner_in_flight() -> bool:
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-f", "parallel_orchestrator|crabbox|loop_driver"],
                             capture_output=True, text=True).stdout
        return bool(out.strip())
    except Exception:
        return False


def run_live(repo: str, issues: list, samples: int) -> int:
    if _runner_in_flight():
        print("REFUSING: a pincer/crabbox run is in flight — would contend. Try later.")
        return 2
    import dataclasses
    import parallel_orchestrator as po
    tuning = dataclasses.replace(po.SelectionTuning.load(), samples=samples)
    state = po.run(repo, f"/tmp/validate-{repo.split('/')[-1]}", issues,
                   max_coders=samples, allow_merge=False, tuning=tuning)
    print("\n=== selection per issue ===")
    for n, seldata in (state.get("selection") or {}).items():
        print(f"  #{n}: stage={seldata.get('stage')} reason={seldata.get('reason')}")
    sc = state.get("scorecard", {})
    print(f"scorecard: merged={sc.get('merged')} prd={sc.get('prd')} "
          f"infra={sc.get('infra_failures')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--repo")
    ap.add_argument("--issues", default="")
    ap.add_argument("--samples", type=int, default=3)
    a = ap.parse_args()

    if a.live:
        if not a.repo or not a.issues:
            print("--live needs --repo and --issues")
            return 2
        issues = [int(x) for x in a.issues.split(",") if x.strip()]
        return run_live(a.repo, issues, a.samples)

    results = run_offline()
    for name, ok, detail in results:
        print(f"  {'✓' if ok else '✗'} {name}: {detail}")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} cascade scenarios correct")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
