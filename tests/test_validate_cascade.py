#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import validate_cascade as vc


def test_all_offline_scenarios_pass():
    results = vc.run_offline()
    failures = [(name, detail) for name, ok, detail in results if not ok]
    assert not failures, f"cascade scenarios failed: {failures}"
    assert len(results) == 5  # all stages covered


def test_each_stage_is_exercised():
    stages = {name for name, _, _ in vc.run_offline()}
    assert {"regression_winner", "reproduction_breaks_tie", "majority_vote_consensus",
            "reviewer_tiebreak", "single_candidate"} <= stages
