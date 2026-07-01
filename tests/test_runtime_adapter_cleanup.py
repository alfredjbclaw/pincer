#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import runtime_adapter  # noqa: E402


def test_reap_codex_locks_removes_stale_lock_file(monkeypatch, tmp_path) -> None:
    # Given: a stale Codex arg0 lock file and a fresh lock file.
    stale_lock = tmp_path / "stale.lock"
    fresh_lock = tmp_path / "fresh.lock"
    stale_lock.write_text("")
    fresh_lock.write_text("")
    old_timestamp = time.time() - 10
    os.utime(stale_lock, (old_timestamp, old_timestamp))
    monkeypatch.setattr(runtime_adapter, "CODEX_ARG0_DIR", tmp_path)

    # When: the best-effort lock reaper runs.
    runtime_adapter._reap_codex_locks()

    # Then: only the stale lock is removed.
    assert not stale_lock.exists()
    assert fresh_lock.exists()


def test_reap_codex_locks_removes_empty_lock_dir(monkeypatch, tmp_path) -> None:
    # Given: an empty stale lock directory under Codex arg0.
    stale_dir = tmp_path / "worker.lock"
    stale_dir.mkdir()
    old_timestamp = time.time() - 10
    os.utime(stale_dir, (old_timestamp, old_timestamp))
    monkeypatch.setattr(runtime_adapter, "CODEX_ARG0_DIR", tmp_path)

    # When: the best-effort lock reaper runs.
    runtime_adapter._reap_codex_locks()

    # Then: the empty stale lock directory is removed.
    assert not stale_dir.exists()


def test_reap_codex_locks_keeps_fresh_lock_dir(monkeypatch, tmp_path) -> None:
    # Given: a fresh lock directory under Codex arg0.
    fresh_dir = tmp_path / "worker.lock"
    fresh_dir.mkdir()
    monkeypatch.setattr(runtime_adapter, "CODEX_ARG0_DIR", tmp_path)

    # When: the best-effort lock reaper runs.
    runtime_adapter._reap_codex_locks()

    # Then: the fresh lock directory is left alone.
    assert fresh_dir.exists()


def test_reap_codex_locks_missing_dir_never_raises(monkeypatch, tmp_path) -> None:
    # Given: the Codex arg0 directory does not exist.
    missing_dir = tmp_path / "missing"
    monkeypatch.setattr(runtime_adapter, "CODEX_ARG0_DIR", missing_dir)

    # When / Then: the best-effort lock reaper is a no-op.
    runtime_adapter._reap_codex_locks()
