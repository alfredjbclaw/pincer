#!/usr/bin/env python3
"""Pure-logic coverage for the orchestrator's new selection plumbing. The
VM/subprocess stages are not exercised here — only the deterministic helpers
that decide control flow."""
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, "/Users/alfred/.openclaw/workspace/tools")

import parallel_orchestrator as po
from publication_gate import ReviewVerdict


def test_cand_id_single_and_multi_sample():
    assert po.cand_id({"issue": 7, "sample": 0}) == "7"
    assert po.cand_id({"issue": 7, "sample": 2}) == "7-s2"
    assert po.cand_id({"issue": 7}) == "7"  # missing sample defaults to 0


def test_selection_tuning_defaults_match_legacy_behavior():
    t = po.SelectionTuning()
    assert t.samples == 1
    assert t.max_revise_iters == 1
    assert t.repro_tests is False


def test_selection_tuning_load_from_toml(tmp_path):
    cfg = tmp_path / "pincer.toml"
    cfg.write_text(
        "[selection]\n"
        "samples = 5\n"
        "max_revise_iters = 6\n"
        "repro_tests = true\n"
        "interpret_failures = false\n"
    )
    t = po.SelectionTuning.load(str(cfg))
    assert t.samples == 5
    assert t.max_revise_iters == 6
    assert t.repro_tests is True
    assert t.interpret_failures is False


def test_selection_tuning_load_missing_file_is_default(tmp_path):
    t = po.SelectionTuning.load(str(tmp_path / "nope.toml"))
    assert t == po.SelectionTuning()


def test_revise_feedback_regression_branch():
    cand = {"sandbox": "fail", "sandbox_fail": "FAILED test_x.py::test_a", "title": "x"}
    fb = po._revise_feedback(cand, "owner/repo", interpret=False)
    assert fb is not None
    assert "broke existing tests" in fb
    assert "test_a" in fb


def test_revise_feedback_none_when_green_and_approved():
    cand = {"sandbox": "pass",
            "_review_obj": ReviewVerdict("approve", [])}
    assert po._revise_feedback(cand, "owner/repo", interpret=False) is None


def test_revise_feedback_blocker_branch_when_rejected():
    cand = {"sandbox": "pass",
            "_review_obj": ReviewVerdict("reject", ["uses a hardcoded path", "no test"])}
    fb = po._revise_feedback(cand, "owner/repo", interpret=False)
    assert fb is not None
    assert "REJECTED" in fb
    assert "hardcoded path" in fb


def test_revise_feedback_none_when_rejected_without_blockers():
    cand = {"sandbox": "pass", "_review_obj": ReviewVerdict("reject", [])}
    # no actionable blockers -> nothing to feed back
    assert po._revise_feedback(cand, "owner/repo", interpret=False) is None
