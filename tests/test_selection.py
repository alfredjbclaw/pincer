#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import selection as sel


def _cand(issue, *, committed=True, sandbox="pass", regressions=0, parsed=True,
          repro_flip=None, patch="+    return fixed\n"):
    return {
        "issue": issue,
        "committed": committed,
        "sandbox": sandbox,
        "results": {"parsed": parsed, "regressions": regressions, "green": regressions == 0},
        "repro_flip": repro_flip,
        "patch": patch,
    }


def test_no_eligible_candidates():
    r = sel.select([_cand(1, committed=False)])
    assert r.chosen is None
    assert r.stage == "none"


def test_single_candidate_short_circuits():
    c = _cand(1)
    r = sel.select([c])
    assert r.chosen is c
    assert r.stage == "only_candidate"


def test_regression_rank_picks_fewest_failures():
    a = _cand(1, sandbox="fail", regressions=3, patch="+a\n")
    b = _cand(2, sandbox="pass", regressions=0, patch="+b\n")
    c = _cand(3, sandbox="fail", regressions=1, patch="+c\n")
    r = sel.select([a, b, c])
    assert r.chosen is b
    assert r.stage == "regression"


def test_reproduction_flip_breaks_regression_tie():
    # two green candidates, only one flips the repro test
    a = _cand(1, regressions=0, repro_flip=False, patch="+a\n")
    b = _cand(2, regressions=0, repro_flip=True, patch="+b\n")
    r = sel.select([a, b], has_repro=True)
    assert r.chosen is b
    assert r.stage == "reproduction"


def test_reproduction_skipped_when_none_flip():
    # neither flips -> stage must fall back, never empty the tier
    a = _cand(1, regressions=0, repro_flip=False, patch="+same\n")
    b = _cand(2, regressions=0, repro_flip=False, patch="+same\n")
    r = sel.select([a, b], has_repro=True)
    assert r.chosen is not None
    # both identical patches -> majority vote consensus of 2
    assert r.stage == "majority_vote"


def test_majority_vote_consensus():
    # three green, tied on regression; two share a normalized patch
    a = _cand(1, regressions=0, patch="+    x = 1\n")
    b = _cand(2, regressions=0, patch="+x=1\n")       # same after normalize
    c = _cand(3, regressions=0, patch="+    y = 2\n")  # different
    r = sel.select([a, b, c])
    assert r.stage == "majority_vote"
    assert r.chosen in (a, b)
    assert r.diagnostics["vote_group_size"] == 2


def test_reviewer_tiebreak_last_resort():
    # two distinct green patches, no consensus -> reviewer decides
    a = _cand(1, regressions=0, patch="+    x = 1\n")
    b = _cand(2, regressions=0, patch="+    y = 2\n")

    calls = {"n": 0}

    def reviewer(c):
        calls["n"] += 1
        return c is b  # only approve b

    r = sel.select([a, b], reviewer=reviewer)
    assert r.chosen is b
    assert r.stage == "reviewer"
    assert calls["n"] >= 1


def test_reviewer_not_called_when_consensus_resolves():
    a = _cand(1, regressions=0, patch="+x=1\n")
    b = _cand(2, regressions=0, patch="+    x = 1\n")  # consensus with a

    calls = {"n": 0}

    def reviewer(c):
        calls["n"] += 1
        return True

    r = sel.select([a, b], reviewer=reviewer)
    assert r.stage == "majority_vote"
    assert calls["n"] == 0  # tie already resolved, judge never consulted


def test_infra_error_deprioritized():
    a = _cand(1, sandbox="error", regressions=sel._UNKNOWN_REGRESSIONS, parsed=False)
    b = _cand(2, sandbox="pass", regressions=0, patch="+b\n")
    r = sel.select([a, b])
    assert r.chosen is b


def test_all_errors_still_returns_a_candidate():
    a = _cand(1, sandbox="error", parsed=False)
    b = _cand(2, sandbox="error", parsed=False)
    r = sel.select([a, b])
    assert r.chosen is not None  # never returns None when candidates committed


def test_normalize_patch_order_independent():
    p1 = "@@ -1 +1 @@\n+    a = 1\n+    b = 2\n"
    p2 = "diff --git a b\n+b=2\n+a=1\n"
    assert sel.normalize_patch(p1) == sel.normalize_patch(p2)


def test_normalize_patch_drops_comments_and_headers():
    p = "+++ b/x.py\n+    # a comment\n+\n+    real = 1\n"
    assert sel.normalize_patch(p) == "+real=1"  # whitespace-insensitive
