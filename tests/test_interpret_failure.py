#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import reviewer as rv


def test_empty_failure_returns_none():
    assert rv.interpret_failure("", "issue") is None
    assert rv.interpret_failure("   \n ", "issue") is None


def test_interpret_returns_guidance_from_runner():
    def fake(argv):
        # the prompt (last argv element) must carry the failing output
        assert "FAILING OUTPUT:" in argv[-1]
        return '{"final_text": "The fix forgot to handle the empty-list case."}'
    out = rv.interpret_failure("E   IndexError: list index out of range",
                               "list handling bug", _runner=fake)
    assert out == "The fix forgot to handle the empty-list case."


def test_interpret_none_on_timeout_envelope():
    out = rv.interpret_failure("boom", "x", _runner=lambda a: '{"timed_out": true}')
    assert out is None


def test_interpret_none_on_runner_exception():
    def boom(argv):
        raise RuntimeError("wrapper died")
    assert rv.interpret_failure("boom", "x", _runner=boom) is None


def test_interpret_truncates_long_guidance():
    big = "x" * 5000
    out = rv.interpret_failure("boom", "x",
                               _runner=lambda a: '{"final_text": "' + big + '"}')
    assert out is not None
    assert len(out) <= 1200
