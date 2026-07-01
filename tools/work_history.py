#!/usr/bin/env python3
"""Per-issue attempt history for pincer loop workers.

This is append-only learning state for a specific `(repo, issue)` pair. The
classification and skip logic is pure over row lists so callers can unit-test
policy without files; `record` and `read` are the only disk-touching functions.
Callers stamp `ts` so this module has no clock side effects.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

HISTORY_DEFAULT = Path.home() / ".openclaw" / "pincer" / "work-history.jsonl"
FAILED_PATCH_OUTCOMES = {"rejected", "failed"}


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


def consecutive_failures(rows) -> int:
    n = 0
    for row in reversed(rows):
        if row.get("outcome") == "shipped":
            break
        n += 1
    return n


def seen_patch(rows, patch_hash) -> bool:
    return any(row.get("patch_hash") == patch_hash for row in rows)


def should_skip(rows, *, max_attempts=3, cooldown_hours=24, now_ts,
                patch_hash=None) -> tuple[bool, str]:
    failed_attempts = [row for row in rows if row.get("outcome") != "shipped"]
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
    failures = [row for row in rows if row.get("outcome") != "shipped"]
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
