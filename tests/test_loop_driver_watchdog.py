import datetime
import os
import sys
import threading
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import loop_driver as ld


class FakeThread:
    def __init__(self):
        self.posts = []

    def post(self, msg, level="progress"):
        self.posts.append((msg, level))


class FakeSpec:
    def __init__(self, name="loop", repo="owner/repo"):
        self.name = name
        self.repo = repo
        self.enabled = True
        self.schedule = "always"
        self.last_run = None
        self.saved = 0

    def save(self):
        self.saved += 1


def _isolate_driver(monkeypatch, tmp_path, specs):
    thread = FakeThread()
    records = []
    monkeypatch.setattr(sys, "argv", ["loop_driver"])
    monkeypatch.setattr(ld.LoopSpec, "all", staticmethod(lambda: specs))
    monkeypatch.setattr(ld.po, "make_alert_thread", lambda tag: thread)
    monkeypatch.setattr(ld.run_ledger, "record", lambda *args, **kwargs: records.append(args))
    monkeypatch.setattr(ld.run_ledger, "read", lambda *args, **kwargs: [])
    monkeypatch.setattr(ld.run_ledger, "should_pause", lambda *args, **kwargs: False)
    monkeypatch.setenv("PINCER_LOOP_LOCK", str(tmp_path / "loop-driver.lock"))
    monkeypatch.setenv("PINCER_INFLIGHT", str(tmp_path / "inflight.json"))
    monkeypatch.setenv("PINCER_LOOP_LOG", str(tmp_path / "loop-driver.log"))
    return thread, records


def test_wedged_run_spec_times_out_and_on_timeout_fires(monkeypatch, tmp_path):
    spec = FakeSpec(name="wedged", repo="owner/wedged")
    thread, records = _isolate_driver(monkeypatch, tmp_path, [spec])
    monkeypatch.setenv("PINCER_RUN_TIMEOUT_S", "1")

    entered = threading.Event()
    unblock = threading.Event()

    def fake_run_spec(spec, thread=None):
        entered.set()
        unblock.wait(5)
        return {"name": spec.name, "result": "late", "scorecard": {}}

    real_reap = ld.inflight.reap_stale
    reap_calls = []

    def tracking_reap(now_ts, max_age_s=ld.inflight.DEFAULT_MAX_AGE_S, path=None):
        reap_calls.append(max_age_s)
        return real_reap(now_ts, max_age_s=max_age_s, path=path)

    monkeypatch.setattr(ld, "run_spec", fake_run_spec)
    monkeypatch.setattr(ld.inflight, "reap_stale", tracking_reap)

    ld.main()
    unblock.set()

    assert entered.is_set()
    assert spec.saved == 1
    assert records[0][2] == "timeout"
    assert 0 in reap_calls
    assert any("timed out" in msg for msg, _level in thread.posts)


def test_already_inflight_spec_is_skipped_without_double_run(monkeypatch, tmp_path):
    spec = FakeSpec(name="busy", repo="owner/busy")
    thread, records = _isolate_driver(monkeypatch, tmp_path, [spec])
    now_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    assert ld.inflight.claim(spec.repo, "existing-run", os.getpid(), now_ts)

    def fail_if_called(spec, thread=None):
        raise AssertionError("run_spec should not run for an inflight spec")

    monkeypatch.setattr(ld, "run_spec", fail_if_called)

    ld.main()

    assert spec.saved == 0
    assert records == []
    assert any("already in flight" in msg for msg, _level in thread.posts)


def test_reap_stale_invoked_at_tick_start_before_claim(monkeypatch, tmp_path):
    spec = FakeSpec(name="ordered", repo="owner/ordered")
    _thread, records = _isolate_driver(monkeypatch, tmp_path, [spec])
    events = []

    def fake_reap(now_ts, max_age_s=ld.inflight.DEFAULT_MAX_AGE_S, path=None):
        events.append("reap")
        return []

    def fake_claim(key, run_id, pid, ts):
        events.append("claim")
        return True

    def fake_heartbeat(key, ts):
        events.append("heartbeat")

    def fake_release(key):
        events.append("release")

    def fake_run_with_timeout(fn, timeout_s, on_timeout=None):
        events.append("watchdog")
        return fn()

    def fake_run_spec(spec, thread=None):
        events.append("run_spec")
        return {"name": spec.name, "result": "done", "scorecard": {}}

    monkeypatch.setattr(ld.inflight, "reap_stale", fake_reap)
    monkeypatch.setattr(ld.inflight, "claim", fake_claim)
    monkeypatch.setattr(ld.inflight, "heartbeat", fake_heartbeat)
    monkeypatch.setattr(ld.inflight, "release", fake_release)
    monkeypatch.setattr(ld.inflight, "run_with_timeout", fake_run_with_timeout)
    monkeypatch.setattr(ld, "run_spec", fake_run_spec)

    ld.main()

    assert events[:2] == ["reap", "claim"]
    assert "run_spec" in events
    assert records[0][2] == "done"
