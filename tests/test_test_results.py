#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import test_results as tr


def test_all_passed():
    r = tr.parse("===== 10 passed in 0.51s =====", exit_code=0)
    assert r.parsed
    assert r.passed == 10
    assert r.regressions == 0
    assert r.green
    assert r.failed_names == ()


def test_failed_and_passed_with_names():
    out = (
        "FAILED tests/test_x.py::test_foo - AssertionError: nope\n"
        "FAILED tests/test_x.py::test_bar - ValueError\n"
        "1 warning\n"
        "===== 2 failed, 8 passed in 1.20s =====\n"
    )
    r = tr.parse(out, exit_code=1)
    assert r.parsed
    assert r.failed == 2
    assert r.passed == 8
    assert r.regressions == 2
    assert not r.green
    assert r.failed_names == (
        "tests/test_x.py::test_foo",
        "tests/test_x.py::test_bar",
    )


def test_error_counted_as_regression():
    r = tr.parse("1 failed, 1 error in 0.10s", exit_code=1)
    assert r.failed == 1
    assert r.errors == 1
    assert r.regressions == 2
    assert not r.green


def test_errors_plural_token():
    r = tr.parse("===== 3 errors in 0.10s =====", exit_code=1)
    assert r.errors == 3
    assert r.regressions == 3


def test_named_failures_without_summary():
    # Truncated tail: lost the summary line but kept the FAILED lines.
    out = "FAILED a.py::test_one\nFAILED a.py::test_two\n"
    r = tr.parse(out, exit_code=1)
    assert r.parsed
    assert r.regressions == 2
    assert set(r.failed_names) == {"a.py::test_one", "a.py::test_two"}


def test_unparseable_falls_back_to_exit_code():
    r = tr.parse("Segmentation fault\nmake: *** [test] Error 139", exit_code=139)
    assert not r.parsed
    assert not r.green
    r0 = tr.parse("build succeeded, all good", exit_code=0)
    assert not r0.parsed
    assert r0.green  # exit-code fallback


def test_dedup_repeated_failed_names():
    out = "FAILED a.py::test_one\nFAILED a.py::test_one\n1 failed, 0 passed in 0.01s"
    r = tr.parse(out, exit_code=1)
    assert r.failed_names == ("a.py::test_one",)


def test_last_summary_wins():
    # A progress line then the authoritative final summary.
    out = "5 passed\n===== 1 failed, 9 passed in 2.0s ====="
    r = tr.parse(out, exit_code=1)
    assert r.passed == 9
    assert r.failed == 1


def test_to_dict_shape():
    r = tr.parse("2 failed, 8 passed in 1.20s", exit_code=1)
    d = r.to_dict()
    assert d["green"] is False
    assert d["regressions"] == 2
    assert isinstance(d["failed_names"], list)
