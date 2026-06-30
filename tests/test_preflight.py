#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import preflight as pf


def test_preflight_aggregates_problems():
    checks = [
        lambda: None,
        lambda: pf.Problem("warn", "minor"),
        lambda: pf.Problem("block", "fatal"),
    ]
    problems = pf.preflight(checks)
    assert len(problems) == 2
    assert pf.has_blockers(problems)


def test_preflight_all_clear():
    problems = pf.preflight([lambda: None, lambda: None])
    assert problems == []
    assert not pf.has_blockers(problems)


def test_preflight_check_that_raises_is_caught():
    def boom():
        raise RuntimeError("nope")
    problems = pf.preflight([boom])
    assert len(problems) == 1
    assert problems[0].severity == "warn"  # crash downgraded, never propagates


def test_has_blockers_only_on_block_severity():
    assert not pf.has_blockers([pf.Problem("warn", "x"), pf.Problem("warn", "y")])
    assert pf.has_blockers([pf.Problem("warn", "x"), pf.Problem("block", "z")])


def test_summary_format():
    s = pf.summary([pf.Problem("block", "no gh"), pf.Problem("warn", "no crabbox")])
    assert "[block] no gh" in s and "[warn] no crabbox" in s


def test_check_crabbox_warns_when_missing(monkeypatch):
    monkeypatch.setattr(pf.shutil, "which", lambda name: None)
    p = pf.check_crabbox()
    assert p is not None and p.severity == "warn"


def test_check_gh_auth_blocks_when_gh_absent(monkeypatch):
    monkeypatch.setattr(pf.shutil, "which", lambda name: None)
    p = pf.check_gh_auth()
    assert p is not None and p.severity == "block"


def test_check_gh_auth_blocks_when_unauthenticated(monkeypatch):
    monkeypatch.setattr(pf.shutil, "which", lambda name: "/usr/bin/gh")

    class R:
        returncode = 1
    monkeypatch.setattr(pf.subprocess, "run", lambda *a, **k: R())
    p = pf.check_gh_auth()
    assert p is not None and p.severity == "block"


def test_check_gh_auth_ok_when_authenticated(monkeypatch):
    monkeypatch.setattr(pf.shutil, "which", lambda name: "/usr/bin/gh")

    class R:
        returncode = 0
    monkeypatch.setattr(pf.subprocess, "run", lambda *a, **k: R())
    assert pf.check_gh_auth() is None
