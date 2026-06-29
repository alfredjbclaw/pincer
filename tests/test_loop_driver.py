import sys, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import loop_driver as ld
from loop_spec import LoopSpec

NOW = datetime.datetime(2026, 6, 28, 12, 0, 0)

def _spec(**kw):
    base = dict(name="t", schedule="6h", enabled=True, last_run=None)
    base.update(kw); return LoopSpec(**base)

def test_manual_never_due():
    assert ld.is_due(_spec(schedule="manual"), NOW) is False

def test_disabled_never_due():
    assert ld.is_due(_spec(enabled=False), NOW) is False

def test_never_run_is_due():
    assert ld.is_due(_spec(last_run=None), NOW) is True

def test_interval_not_yet_due():
    last = (NOW - datetime.timedelta(hours=2)).isoformat()
    assert ld.is_due(_spec(schedule="6h", last_run=last), NOW) is False

def test_interval_due_after_window():
    last = (NOW - datetime.timedelta(hours=7)).isoformat()
    assert ld.is_due(_spec(schedule="6h", last_run=last), NOW) is True

def test_always_is_due():
    assert ld.is_due(_spec(schedule="always", last_run=NOW.isoformat()), NOW) is True

def test_interval_parsing():
    assert ld._interval_seconds("6h") == 21600
    assert ld._interval_seconds("30m") == 1800
    assert ld._interval_seconds("2d") == 172800
    assert ld._interval_seconds("manual") is None
