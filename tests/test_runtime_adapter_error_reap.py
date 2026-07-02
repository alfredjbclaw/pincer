#!/usr/bin/env python3
"""The #8 wedge was codex *erroring* (clean nonzero exit) and falling back to
claude-code while a stale codex lock lingered. _codex_once only reaps on timeout
or exception, so these tests pin the reap onto the error/fallback path too."""
from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import runtime_adapter  # noqa: E402


def _spy_reap(monkeypatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(runtime_adapter, "_reap_codex_locks", lambda: calls.append(1))
    return calls


def test_error_exit_reaps_before_fallback(monkeypatch, tmp_path) -> None:
    # Given: codex exits nonzero with a non-rate-limit error (no timeout, no exception).
    calls = _spy_reap(monkeypatch)
    monkeypatch.setattr(
        runtime_adapter, "_codex_once",
        lambda prompt, workdir, cfg: ("", "codex exec failed: credit_exhausted", 1),
    )

    # When: the concurrency-capped runner returns the failure to the caller.
    text, stderr, code = runtime_adapter._run_codex("task", tmp_path, runtime_adapter.RuntimeConfig())

    # Then: the reaper fired on the error path so no lock lingers into the fallback.
    assert code == 1
    assert len(calls) == 1


def test_success_exit_does_not_reap(monkeypatch, tmp_path) -> None:
    # Given: codex succeeds cleanly.
    calls = _spy_reap(monkeypatch)
    monkeypatch.setattr(
        runtime_adapter, "_codex_once",
        lambda prompt, workdir, cfg: ("STATUS: done", "", 0),
    )

    # When: the runner returns success.
    runtime_adapter._run_codex("task", tmp_path, runtime_adapter.RuntimeConfig())

    # Then: a healthy run never touches the shared lock dir.
    assert calls == []


def test_exhausted_rate_limit_retries_reap_before_fallback(monkeypatch, tmp_path) -> None:
    # Given: every attempt is rate-limited, so all retries are exhausted.
    calls = _spy_reap(monkeypatch)
    monkeypatch.setattr(runtime_adapter.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        runtime_adapter, "_codex_once",
        lambda prompt, workdir, cfg: ("", "429 too many requests", 1),
    )

    # When: the runner gives up after the retry budget.
    text, stderr, code = runtime_adapter._run_codex("task", tmp_path, runtime_adapter.RuntimeConfig())

    # Then: it reaps once before handing the caller the failure to fall back on.
    assert code == 1
    assert len(calls) == 1
