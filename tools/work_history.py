#!/usr/bin/env python3
"""Per-issue attempt history for pincer loop workers.

This is append-only learning state for a specific `(repo, issue)` pair. The
classification and skip logic is pure over row lists so callers can unit-test
policy without files; `record`, `record_escalation`, `clear_escalation`, and
`read` are the disk-touching functions. Callers stamp `ts` so this module has
no clock side effects.

Escalation is represented append-only as an `outcome="escalated"` row. A later
`outcome="cleared"` row is the human reset marker that allows the loop to try
again without rewriting prior attempts.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

HISTORY_DEFAULT = Path.home() / ".openclaw" / "pincer" / "work-history.jsonl"
FAILED_PATCH_OUTCOMES = {"rejected", "failed"}
ESCALATED_OUTCOME = "escalated"
CLEARED_OUTCOME = "cleared"
RESET_OUTCOMES = {"shipped", CLEARED_OUTCOME}
CONTROL_OUTCOMES = {ESCALATED_OUTCOME, CLEARED_OUTCOME}


def _history_path() -> Path:
    import os
    return Path(os.environ.get("PINCER_WORK_HISTORY", HISTORY_DEFAULT))


def record(repo, issue, run_id, runtime, patch_hash, sandbox, review, outcome,
           reason, ts, path=None) -> None:
    row = {
        "repo": repo,
        "issue": issue,
        "run_id": run_id,
        "runtime": runtime,
        "patch_hash": patch_hash,
        "sandbox": sandbox,
        "review": review,
        "outcome": outcome,
        "reason": reason,
        "ts": ts,
    }
    p = path or _history_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def read(repo=None, issue=None, path=None) -> List[dict]:
    p = path or _history_path()
    rows: List[dict] = []
    try:
        for line in Path(p).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if repo is not None and row.get("repo") != repo:
                continue
            if issue is not None and row.get("issue") != issue:
                continue
            rows.append(row)
    except FileNotFoundError:
        return []
    except Exception:
        return rows
    return rows


def attempts(repo, issue, path=None) -> List[dict]:
    return read(repo=repo, issue=issue, path=path)


def attempt_count(repo, issue, path=None) -> int:
    return len(attempts(repo, issue, path=path))


def active_escalation(rows) -> Optional[dict]:
    for row in reversed(rows):
        outcome = row.get("outcome")
        if outcome == ESCALATED_OUTCOME:
            return row
        if outcome in RESET_OUTCOMES:
            return None
    return None


def is_escalated(rows) -> bool:
    return active_escalation(rows) is not None


def _active_rows(rows) -> list[dict]:
    if is_escalated(rows):
        return []
    for idx in range(len(rows) - 1, -1, -1):
        if rows[idx].get("outcome") in RESET_OUTCOMES | {ESCALATED_OUTCOME}:
            return rows[idx + 1:]
    return list(rows)


def _failed_attempts(rows) -> list[dict]:
    return [
        row for row in _active_rows(rows)
        if row.get("outcome") not in RESET_OUTCOMES | CONTROL_OUTCOMES
    ]


def consecutive_failures(rows) -> int:
    n = 0
    for row in reversed(rows):
        if row.get("outcome") in RESET_OUTCOMES | {ESCALATED_OUTCOME}:
            break
        n += 1
    return n


def seen_patch(rows, patch_hash) -> bool:
    return any(row.get("patch_hash") == patch_hash for row in rows)


def should_escalate(rows, *, max_attempts=3,
                    consecutive_failure_threshold: int | None = None) -> tuple[bool, str, int]:
    if is_escalated(rows):
        escalation = active_escalation(rows) or {}
        return False, "escalated", int(escalation.get("attempts") or 0)

    failed_attempts = _failed_attempts(rows)
    failed_count = len(failed_attempts)
    if len(failed_attempts) >= max_attempts:
        return True, "max_attempts", failed_count

    failure_threshold = (
        max_attempts
        if consecutive_failure_threshold is None
        else consecutive_failure_threshold
    )
    failure_streak = consecutive_failures(_active_rows(rows))
    if failure_streak >= failure_threshold:
        return True, "consecutive_failures", failure_streak

    return False, "", failed_count


def record_escalation(repo, issue, run_id, reason, attempts, ts, path=None) -> None:
    row = {
        "repo": repo,
        "issue": issue,
        "run_id": run_id,
        "runtime": "",
        "patch_hash": "",
        "sandbox": "",
        "review": "",
        "outcome": ESCALATED_OUTCOME,
        "reason": reason,
        "attempts": attempts,
        "ts": ts,
    }
    p = path or _history_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def clear_escalation(repo, issue, run_id, reason, ts, path=None) -> None:
    record(
        repo=repo,
        issue=issue,
        run_id=run_id,
        runtime="",
        patch_hash="",
        sandbox="",
        review="",
        outcome=CLEARED_OUTCOME,
        reason=reason,
        ts=ts,
        path=path,
    )


def should_skip(rows, *, max_attempts=3, cooldown_hours=24, now_ts,
                patch_hash=None) -> tuple[bool, str]:
    if is_escalated(rows):
        return True, "escalated"

    failed_attempts = _failed_attempts(rows)
    if len(failed_attempts) >= max_attempts:
        return True, "max_attempts"

    if failed_attempts:
        last_failure = failed_attempts[-1]
        age = datetime.fromisoformat(now_ts) - datetime.fromisoformat(
            last_failure["ts"],
        )
        if age <= timedelta(hours=cooldown_hours):
            return True, "cooldown"

    if patch_hash is not None:
        for row in rows:
            if (
                row.get("patch_hash") == patch_hash
                and row.get("outcome") in FAILED_PATCH_OUTCOMES
            ):
                return True, "seen_patch"

    return False, ""


def failure_context(rows, limit=3) -> str:
    failures = _failed_attempts(rows)
    if not failures:
        return ""

    recent = failures[-limit:]
    parts = []
    for row in recent:
        outcome = row.get("outcome", "failed")
        patch_hash = row.get("patch_hash", "")
        reason = row.get("reason", "")
        parts.append(f"{outcome} patch={patch_hash}: {reason}")
    return (
        "Prior attempts failed: "
        + "; ".join(parts)
        + "; do NOT repeat these approaches; try a materially different fix."
    )
