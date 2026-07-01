#!/usr/bin/env python3
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import global_gate


def test_acquire_allows_up_to_max_slots(tmp_path, monkeypatch):
    registry = tmp_path / "global-gate.json"
    monkeypatch.setattr(global_gate, "_pid_alive", lambda pid: True)

    assert global_gate.acquire(
        "h1", label="one", pid=101, ts="2026-07-01T00:00:00+00:00",
        max_slots=2, path=registry)
    assert global_gate.acquire(
        "h2", label="two", pid=102, ts="2026-07-01T00:00:01+00:00",
        max_slots=2, path=registry)

    rows = json.loads(registry.read_text())
    assert sorted(rows) == ["h1", "h2"]


def test_acquire_blocks_when_full(tmp_path, monkeypatch):
    registry = tmp_path / "global-gate.json"
    monkeypatch.setattr(global_gate, "_pid_alive", lambda pid: True)

    assert global_gate.acquire(
        "h1", label="one", pid=101, ts="2026-07-01T00:00:00+00:00",
        max_slots=1, path=registry)
    assert not global_gate.acquire(
        "h2", label="two", pid=102, ts="2026-07-01T00:00:01+00:00",
        max_slots=1, path=registry)

    rows = json.loads(registry.read_text())
    assert sorted(rows) == ["h1"]


def test_reap_stale_removes_dead_and_old_entries(tmp_path, monkeypatch):
    registry = tmp_path / "global-gate.json"
    registry.write_text(json.dumps({
        "live": {
            "holder_id": "live",
            "label": "live",
            "pid": 201,
            "started_at": "2026-07-01T00:09:30+00:00",
            "heartbeat": "2026-07-01T00:09:30+00:00",
        },
        "dead": {
            "holder_id": "dead",
            "label": "dead",
            "pid": 202,
            "started_at": "2026-07-01T00:09:30+00:00",
            "heartbeat": "2026-07-01T00:09:30+00:00",
        },
        "old": {
            "holder_id": "old",
            "label": "old",
            "pid": 203,
            "started_at": "2026-07-01T00:00:00+00:00",
            "heartbeat": "2026-07-01T00:00:00+00:00",
        },
    }))
    monkeypatch.setattr(global_gate, "_pid_alive", lambda pid: pid != 202)

    reaped = global_gate.reap_stale(
        "2026-07-01T00:10:00+00:00", max_age_s=120, path=registry)

    assert set(reaped) == {"dead", "old"}
    assert sorted(json.loads(registry.read_text())) == ["live"]


def test_release_frees_slot(tmp_path, monkeypatch):
    registry = tmp_path / "global-gate.json"
    monkeypatch.setattr(global_gate, "_pid_alive", lambda pid: True)

    assert global_gate.acquire(
        "h1", label="one", pid=101, ts="2026-07-01T00:00:00+00:00",
        max_slots=1, path=registry)
    global_gate.release("h1", path=registry)
    assert global_gate.acquire(
        "h2", label="two", pid=102, ts="2026-07-01T00:00:01+00:00",
        max_slots=1, path=registry)

    assert sorted(json.loads(registry.read_text())) == ["h2"]
