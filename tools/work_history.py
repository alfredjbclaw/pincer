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

import fcntl
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

HISTORY_DEFAULT = Path.home() / ".openclaw" / "pincer" / "work-history.jsonl"
FAILED_PATCH_OUTCOMES = {"rejected", "failed"}
ESCALATED_OUTCOME = "escalated"
CLEARED_OUTCOME = "cleared"
RESET_OUTCOMES = {"shipped", CLEARED_OUTCOME}
CONTROL_OUTCOMES = {ESCALATED_OUTCOME, CLEARED_OUTCOME}
LOG = logging.getLogger("pincer")


def _history_path() -> Path:
    import os
    return Path(os.environ.get("PINCER_WORK_HISTORY", HISTORY_DEFAULT))


def _prepare_parent(path: Path) -> None:
    parent = path.parent
    created = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink():
        raise OSError(f"history parent is a symlink: {parent}")
    if created or os.path.abspath(parent) == os.path.abspath(HISTORY_DEFAULT.parent):
        os.chmod(parent, 0o700)


def _fsync_parent(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path.parent, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_row(row: dict, path: Path) -> bool:
    try:
        _prepare_parent(path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            start_offset = os.lseek(fd, 0, os.SEEK_END)
            payload = (json.dumps(row) + "\n").encode()
            try:
                while payload:
                    written = os.write(fd, payload)
                    if written <= 0:
                        raise OSError("short write for work history")
                    payload = payload[written:]
                os.fsync(fd)
            except BaseException:
                try:
                    os.ftruncate(fd, start_offset)
                    os.fsync(fd)
                except OSError:
                    pass
                raise
            _fsync_parent(path)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
    except Exception:
        LOG.exception("Failed to append work history at %s", path)
        return False
    return True


def record(repo, issue, run_id, runtime, patch_hash, sandbox, review, outcome,
           reason, ts, path=None) -> bool:
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
    return _append_row(row, path or _history_path())


def read(repo=None, issue=None, path=None) -> List[dict]:
    p = path or _history_path()
    rows: List[dict] = []
    try:
        with Path(p).open(encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                for line_number, line in enumerate(handle, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        LOG.warning("Skipping malformed work-history row %s in %s", line_number, p)
                        continue
                    if repo is not None and row.get("repo") != repo:
                        continue
                    if issue is not None and row.get("issue") != issue:
                        continue
                    rows.append(row)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return []
    except Exception:
        LOG.exception("Failed to read work history at %s", p)
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


def record_escalation(repo, issue, run_id, reason, attempts, ts, path=None) -> bool:
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
    return _append_row(row, path or _history_path())


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
