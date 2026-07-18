#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import work_history as wh


NOW_TS = "2026-06-29T23:00:00"


def _row(
    outcome: str,
    *,
    ts: str = "2026-06-29T20:00:00",
    patch_hash: str = "patch-a",
    reason: str = "verification failed",
) -> dict:
    return {
        "repo": "owner/repo",
        "issue": "13",
        "run_id": "run-1",
        "runtime": "codex",
        "patch_hash": patch_hash,
        "sandbox": "workspace-write",
        "review": "rejected",
        "outcome": outcome,
        "reason": reason,
        "ts": ts,
    }


def test_record_and_read_roundtrip_when_env_path_is_set(tmp_path, monkeypatch):
    history_path = tmp_path / "work-history.jsonl"
    monkeypatch.setenv("PINCER_WORK_HISTORY", str(history_path))

    wh.record(
        repo="owner/repo",
        issue="13",
        run_id="run-1",
        runtime="codex",
        patch_hash="abc123",
        sandbox="workspace-write",
        review="needs tests",
        outcome="failed",
        reason="pytest failed",
        ts=NOW_TS,
    )

    assert wh.read() == [
        {
            "repo": "owner/repo",
            "issue": "13",
            "run_id": "run-1",
            "runtime": "codex",
            "patch_hash": "abc123",
            "sandbox": "workspace-write",
            "review": "needs tests",
            "outcome": "failed",
            "reason": "pytest failed",
            "ts": NOW_TS,
        }
    ]


def test_read_filters_by_repo_and_issue(tmp_path):
    history_path = tmp_path / "work-history.jsonl"
    wh.record("owner/repo", "13", "run-1", "codex", "a", "sandbox", "review", "failed", "a", NOW_TS, history_path)
    wh.record("owner/repo", "14", "run-2", "codex", "b", "sandbox", "review", "failed", "b", NOW_TS, history_path)
    wh.record("other/repo", "13", "run-3", "codex", "c", "sandbox", "review", "failed", "c", NOW_TS, history_path)

    rows = wh.read(repo="owner/repo", issue="13", path=history_path)

    assert [row["run_id"] for row in rows] == ["run-1"]


def test_attempt_count_counts_issue_attempts(tmp_path):
    history_path = tmp_path / "work-history.jsonl"
    wh.record("owner/repo", "13", "run-1", "codex", "a", "sandbox", "review", "failed", "a", NOW_TS, history_path)
    wh.record("owner/repo", "13", "run-2", "codex", "b", "sandbox", "review", "rejected", "b", NOW_TS, history_path)

    assert wh.attempt_count("owner/repo", "13", path=history_path) == 2


def test_consecutive_failures_resets_on_shipped_row():
    rows = [
        _row("failed"),
        _row("shipped"),
        _row("rejected"),
        _row("error"),
    ]

    assert wh.consecutive_failures(rows) == 2


def test_seen_patch_detects_exact_patch_hash():
    rows = [_row("failed", patch_hash="abc"), _row("rejected", patch_hash="def")]

    assert wh.seen_patch(rows, "abc")
    assert not wh.seen_patch(rows, "xyz")


def test_should_skip_after_max_non_shipped_attempts():
    rows = [_row("failed"), _row("rejected"), _row("no_winner")]

    assert wh.should_skip(rows, max_attempts=3, cooldown_hours=24, now_ts=NOW_TS) == (
        True,
        "max_attempts",
    )


def test_should_skip_during_cooldown_after_recent_failure():
    rows = [_row("failed", ts="2026-06-29T22:30:00")]

    assert wh.should_skip(rows, max_attempts=3, cooldown_hours=24, now_ts=NOW_TS) == (
        True,
        "cooldown",
    )


def test_should_skip_when_rejected_or_failed_patch_repeats():
    rows = [_row("rejected", patch_hash="abc")]

    assert wh.should_skip(
        rows,
        max_attempts=3,
        cooldown_hours=0,
        now_ts=NOW_TS,
        patch_hash="abc",
    ) == (True, "seen_patch")


def test_should_not_skip_clean_case():
    rows = [_row("failed", ts="2026-06-27T22:00:00", patch_hash="abc")]

    assert wh.should_skip(
        rows,
        max_attempts=3,
        cooldown_hours=24,
        now_ts=NOW_TS,
        patch_hash="xyz",
    ) == (False, "")


def test_failure_context_summarizes_recent_failures():
    rows = [
        _row("failed", patch_hash="a", reason="tests failed"),
        _row("shipped", patch_hash="ship", reason="merged"),
        _row("rejected", patch_hash="b", reason="same approach"),
        _row("error", patch_hash="c", reason="worker crashed"),
    ]

    context = wh.failure_context(rows, limit=2)

    assert "Prior attempts failed:" in context
    assert "same approach" in context
    assert "worker crashed" in context
    assert "tests failed" not in context
    assert "do NOT repeat" in context


def test_failure_context_empty_when_no_rows():
    assert wh.failure_context([]) == ""


def test_record_does_not_chmod_existing_override_parent(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    history = parent / "work.jsonl"

    assert wh.record("owner/repo", "13", "run", "codex", "a", "sandbox", "review",
                     "failed", "fixture", NOW_TS, history)

    assert parent.stat().st_mode & 0o777 == 0o755
    assert history.stat().st_mode & 0o777 == 0o600


def test_record_fsyncs_file_and_parent_directory(tmp_path, monkeypatch):
    history = tmp_path / "work.jsonl"
    real_fsync = wh.os.fsync
    fsynced_modes = []

    def observe_fsync(fd):
        fsynced_modes.append(wh.os.fstat(fd).st_mode)
        return real_fsync(fd)

    monkeypatch.setattr(wh.os, "fsync", observe_fsync)

    assert wh.record(
        "owner/repo", "13", "run", "codex", "a", "sandbox", "review",
        "failed", "fixture", NOW_TS, history,
    )
    stat_module = __import__("stat")
    assert any(stat_module.S_ISREG(mode) for mode in fsynced_modes)
    assert any(stat_module.S_ISDIR(mode) for mode in fsynced_modes)


def test_record_completes_short_os_writes(tmp_path, monkeypatch):
    history = tmp_path / "work.jsonl"
    real_write = wh.os.write

    def short_write(fd, payload):
        return real_write(fd, payload[: max(1, len(payload) // 2)])

    monkeypatch.setattr(wh.os, "write", short_write)
    assert wh.record(
        "owner/repo", "13", "run", "codex", "a", "sandbox", "review",
        "failed", "fixture", NOW_TS, history,
    )
    assert wh.read(path=history)[0]["repo"] == "owner/repo"


def test_record_rolls_back_partial_row_on_write_failure(tmp_path, monkeypatch):
    history = tmp_path / "work.jsonl"
    history.write_text('{"repo":"existing"}\n')
    original = history.read_bytes()
    real_write = wh.os.write
    calls = 0

    def fail_after_partial(fd, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, payload[: max(1, len(payload) // 2)])
        raise OSError("fixture write failure")

    monkeypatch.setattr(wh.os, "write", fail_after_partial)
    assert not wh.record(
        "owner/repo", "13", "run", "codex", "a", "sandbox", "review",
        "failed", "fixture", NOW_TS, history,
    )
    assert history.read_bytes() == original
