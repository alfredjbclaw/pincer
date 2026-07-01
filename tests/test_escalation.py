#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import loop_spec as ls
import work_history as wh


NOW_TS = "2026-06-30T12:00:00"


def _row(outcome: str, *, ts: str = NOW_TS) -> dict:
    return {
        "repo": "owner/repo",
        "issue": 7,
        "run_id": "run-1",
        "runtime": "codex",
        "patch_hash": "patch",
        "sandbox": "fail",
        "review": "reject",
        "outcome": outcome,
        "reason": "tests failed",
        "ts": ts,
    }


class FakeThread:
    def __init__(self):
        self.posts = []

    def post(self, body, level="progress"):
        self.posts.append((body, level))
        return True


def test_should_escalate_when_max_attempts_are_exhausted():
    rows = [_row("failed"), _row("rejected"), _row("no_winner")]

    assert wh.should_escalate(rows, max_attempts=3) == (True, "max_attempts", 3)


def test_should_escalate_when_consecutive_failure_threshold_is_hit():
    rows = [_row("failed"), _row("rejected")]

    assert wh.should_escalate(
        rows,
        max_attempts=5,
        consecutive_failure_threshold=2,
    ) == (True, "consecutive_failures", 2)


def test_escalation_marker_is_terminal_until_cleared(tmp_path):
    history_path = tmp_path / "history.jsonl"
    wh.record("owner/repo", 7, "run-1", "codex", "a", "fail", "reject",
              "failed", "tests failed", "2026-06-30T10:00:00", history_path)
    wh.record("owner/repo", 7, "run-2", "codex", "b", "fail", "reject",
              "rejected", "too broad", "2026-06-30T11:00:00", history_path)
    wh.record_escalation(
        "owner/repo",
        7,
        "run-3",
        "max_attempts after 2 failed attempts",
        attempts=2,
        ts=NOW_TS,
        path=history_path,
    )

    rows = wh.attempts("owner/repo", 7, path=history_path)
    escalation = wh.active_escalation(rows)
    assert escalation is not None
    assert escalation["outcome"] == wh.ESCALATED_OUTCOME
    assert escalation["attempts"] == 2
    assert wh.should_skip(rows, now_ts=NOW_TS) == (True, "escalated")

    wh.clear_escalation(
        "owner/repo",
        7,
        "human-clear",
        "operator approved another attempt",
        "2026-06-30T13:00:00",
        path=history_path,
    )
    cleared_rows = wh.attempts("owner/repo", 7, path=history_path)

    assert wh.active_escalation(cleared_rows) is None
    assert wh.should_skip(cleared_rows, now_ts="2026-06-30T14:00:00") == (False, "")


def test_run_spec_records_and_alerts_when_issue_first_escalates(monkeypatch):
    records = []
    thread = FakeThread()

    monkeypatch.setattr(ls, "budget_ok", lambda spec: (True, "ok"))
    monkeypatch.setattr(ls.preflight, "preflight", lambda: [])
    monkeypatch.setattr(ls.preflight, "has_blockers", lambda problems: False)
    monkeypatch.setattr(ls, "_resolve_issues", lambda spec: [7])
    monkeypatch.setattr(ls, "_history_rows",
                        lambda repo, issue: [_row("failed"), _row("rejected"), _row("no_winner")])
    monkeypatch.setattr(ls.work_history, "record_escalation",
                        lambda *args, **kwargs: records.append((args, kwargs)))

    called = {"run": False}
    monkeypatch.setattr(ls.po, "run", lambda *a, **k: called.__setitem__("run", True))

    result = ls.run_spec(
        ls.LoopSpec(name="escalate", repo="owner/repo", workdir="/tmp/repo"),
        thread=thread,
    )

    assert result == {"name": "escalate", "result": "escalated", "escalated": [7]}
    assert called["run"] is False
    assert len(records) == 1
    args, kwargs = records[0]
    assert args[:2] == ("owner/repo", 7)
    assert kwargs["reason"] == "max_attempts after 3 failed attempts"
    assert kwargs["attempts"] == 3
    assert any(
        "NEEDS HUMAN: issue owner/repo#7 escalated after 3 failed attempts" in body
        and level == "critical"
        for body, level in thread.posts
    )


def test_run_spec_skips_already_escalated_issue_without_rerunning(monkeypatch):
    thread = FakeThread()
    rows = [
        _row("failed"),
        {
            **_row(wh.ESCALATED_OUTCOME),
            "reason": "max_attempts after 1 failed attempts",
            "attempts": 1,
        },
    ]

    monkeypatch.setattr(ls, "budget_ok", lambda spec: (True, "ok"))
    monkeypatch.setattr(ls.preflight, "preflight", lambda: [])
    monkeypatch.setattr(ls.preflight, "has_blockers", lambda problems: False)
    monkeypatch.setattr(ls, "_resolve_issues", lambda spec: [7])
    monkeypatch.setattr(ls, "_history_rows", lambda repo, issue: rows)
    monkeypatch.setattr(
        ls.work_history,
        "record_escalation",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not record twice")),
    )

    called = {"run": False}
    monkeypatch.setattr(ls.po, "run", lambda *a, **k: called.__setitem__("run", True))

    result = ls.run_spec(
        ls.LoopSpec(name="escalate", repo="owner/repo", workdir="/tmp/repo"),
        thread=thread,
    )

    assert result == {"name": "escalate", "result": "escalated", "escalated": [7]}
    assert called["run"] is False
    assert any("already escalated" in body for body, _ in thread.posts)
