#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.bench.grade as gr


def test_gold_sanity_argv_uses_gold_predictions():
    argv = gr.gold_sanity_argv("princeton-nlp/SWE-bench_Lite", max_workers=4)
    assert "swebench.harness.run_evaluation" in argv
    i = argv.index("--predictions_path")
    assert argv[i + 1] == "gold"
    assert "--max_workers" in argv and argv[argv.index("--max_workers") + 1] == "4"


def test_grade_argv_points_at_predictions_file():
    argv = gr.grade_argv("preds.jsonl", run_id="pincer_x", cache_level="env")
    i = argv.index("--predictions_path")
    assert argv[i + 1] == "preds.jsonl"
    assert argv[argv.index("--run_id") + 1] == "pincer_x"
    assert argv[argv.index("--cache_level") + 1] == "env"


def test_cap_workers_caps_and_floors():
    assert gr.cap_workers(16) == 12          # 0.75 * 16
    assert gr.cap_workers(100) == 24         # hard cap
    assert gr.cap_workers(1) == 1            # floor
    assert gr.cap_workers(0) == 1


def test_preflight_returns_problem_list():
    # On this dev box (arm64, no docker, no swebench) preflight must flag issues,
    # and always returns a list of human-readable strings.
    problems = gr.preflight()
    assert isinstance(problems, list)
    assert all(isinstance(p, str) for p in problems)


def test_run_evaluation_dry_run_does_not_execute(capsys):
    rc = gr.run_evaluation(["echo", "should-not-run"], dry_run=True)
    assert rc == 0
    assert "DRY RUN" in capsys.readouterr().out
