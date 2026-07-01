#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import inflight


def test_claim_refuses_live_entry_until_released(tmp_path):
    # Given: an empty registry path.
    registry = tmp_path / "inflight.json"

    # When: a key is claimed twice while the first process is still live.
    claimed = inflight.claim("repo", "run-1", os.getpid(), "2026-06-29T00:00:00", path=registry)
    claimed_again = inflight.claim(
        "repo",
        "run-2",
        os.getpid(),
        "2026-06-29T00:00:01",
        path=registry,
    )

    # Then: the live claim is preserved until release removes it.
    assert claimed is True
    assert claimed_again is False

    inflight.release("repo", path=registry)
    assert inflight.claim("repo", "run-3", os.getpid(), "2026-06-29T00:00:02", path=registry)


def test_is_inflight_requires_live_pid_and_fresh_heartbeat(tmp_path):
    # Given: a live entry, a dead-pid entry, and a stale entry.
    registry = tmp_path / "inflight.json"
    now_ts = "2026-06-29T02:00:00"
    assert inflight.claim("live", "run-1", os.getpid(), "2026-06-29T01:59:00", path=registry)
    assert inflight.claim("dead", "run-2", 999999, "2026-06-29T01:59:00", path=registry)
    assert inflight.claim("stale", "run-3", os.getpid(), "2026-06-29T00:00:00", path=registry)

    # When / Then: only the live pid with a fresh heartbeat is considered in-flight.
    assert inflight.is_inflight("live", now_ts=now_ts, path=registry) is True
    assert inflight.is_inflight("dead", now_ts=now_ts, path=registry) is False
    assert inflight.is_inflight("stale", now_ts=now_ts, path=registry) is False


def test_reap_stale_removes_dead_and_old_entries(tmp_path):
    # Given: one live, one dead, and one stale entry in the registry.
    registry = tmp_path / "inflight.json"
    now_ts = "2026-06-29T02:00:00"
    assert inflight.claim("live", "run-1", os.getpid(), "2026-06-29T01:59:00", path=registry)
    assert inflight.claim("dead", "run-2", 999999, "2026-06-29T01:59:00", path=registry)
    assert inflight.claim("stale", "run-3", os.getpid(), "2026-06-29T00:00:00", path=registry)

    # When: stale entries are reaped.
    reaped = inflight.reap_stale(now_ts, path=registry)

    # Then: dead and stale keys are returned and removed, while the live key remains.
    assert set(reaped) == {"dead", "stale"}
    assert inflight.is_inflight("live", now_ts=now_ts, path=registry) is True
    assert inflight.is_inflight("dead", now_ts=now_ts, path=registry) is False
    assert inflight.is_inflight("stale", now_ts=now_ts, path=registry) is False


def test_run_with_timeout_returns_fast_result():
    # Given: a callable that finishes inside the timeout.
    def fast_fn():
        return "ok"

    # When: it runs under the watchdog.
    result = inflight.run_with_timeout(fast_fn, timeout_s=0.2)

    # Then: its value is returned.
    assert result == "ok"


def test_run_with_timeout_raises_and_calls_timeout_hook():
    # Given: a callable that exceeds the timeout and a hook recording the breach.
    timed_out = []

    def slow_fn():
        time.sleep(2)
        return "late"

    def on_timeout():
        timed_out.append(True)

    # When / Then: the watchdog raises and invokes the timeout hook.
    with pytest.raises(inflight.RunTimeout):
        inflight.run_with_timeout(slow_fn, timeout_s=0.2, on_timeout=on_timeout)

    assert timed_out == [True]
