#!/usr/bin/env python3
"""pincer run ledger — a durable history of every loop run + auto-pause.

Today's broken-clone failure was invisible: the loop failed silently every few
hours and nobody noticed. This records each run's outcome to an append-only
ledger, and lets the driver AUTO-PAUSE a loop that keeps failing for
infrastructure reasons (so it stops burning credits on a broken setup and pings
for a human) — while leaving a loop that simply finds no fix to keep running.

The classification + streak logic is pure over a list of records, so it is fully
unit-tested without files; `record`/`read` are the only disk-touching parts.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
from typing import List, Optional

LEDGER_DEFAULT = Path.home() / ".openclaw" / "pincer" / "run-ledger.jsonl"
LOG = logging.getLogger("pincer")


def _ledger_path() -> Path:
    import os
    return Path(os.environ.get("PINCER_RUN_LEDGER", LEDGER_DEFAULT))


def _prepare_parent(path: Path) -> None:
    parent = path.parent
    created = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink():
        raise OSError(f"ledger parent is a symlink: {parent}")
    if created or os.path.abspath(parent) == os.path.abspath(LEDGER_DEFAULT.parent):
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


def record(name: str, repo: str, result: str, scorecard: dict, ts: str,
           path: Optional[Path] = None) -> bool:
    """Append one run's outcome. `ts` is passed in (caller stamps) so this stays
    free of clock side-effects. Best-effort: never raises."""
    sc = scorecard or {}
    row = {
        "ts": ts, "name": name, "repo": repo, "result": result,
        "merged": list(sc.get("merged", []) or []),
        "prd": list(sc.get("prd", []) or []),
        "failed_verification": list(sc.get("failed_verification", []) or []),
        "infra_failures": list(sc.get("infra_failures", []) or []),
        "no_winner": list(sc.get("no_winner", []) or []),
    }
    p = path or _ledger_path()
    try:
        _prepare_parent(p)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(p, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            start_offset = os.lseek(fd, 0, os.SEEK_END)
            payload = (json.dumps(row) + "\n").encode()
            try:
                while payload:
                    written = os.write(fd, payload)
                    if written <= 0:
                        raise OSError("short write for run ledger")
                    payload = payload[written:]
                os.fsync(fd)
            except BaseException:
                try:
                    os.ftruncate(fd, start_offset)
                    os.fsync(fd)
                except OSError:
                    pass
                raise
            _fsync_parent(p)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
    except Exception:
        LOG.exception("Failed to append run ledger at %s", p)
        return False
    return True


def read(name: Optional[str] = None, path: Optional[Path] = None) -> List[dict]:
    """All ledger rows (optionally filtered to one loop), oldest first."""
    p = path or _ledger_path()
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
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        LOG.warning("Skipping malformed run-ledger row %s in %s", line_number, p)
                        continue
                    if name is None or r.get("name") == name:
                        rows.append(r)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return []
    except Exception:
        LOG.exception("Failed to read run ledger at %s", p)
        return rows
    return rows


def classify(row: dict) -> str:
    """One run's outcome class:
      shipped — merged or PR'd something (engine worked, delivered)
      infra   — shipped nothing AND had infrastructure failures (broken setup)
      held    — budget/usage gate held the run (neutral, not a failure)
      no_fix  — shipped nothing, no infra failure (engine worked, found no fix)
    """
    if row.get("merged") or row.get("prd"):
        return "shipped"
    if row.get("result") in ("held_budget", "halted_usage"):
        return "held"
    if row.get("infra_failures"):
        return "infra"
    return "no_fix"


def consecutive_infra_failures(rows: List[dict]) -> int:
    """Trailing run of pure infrastructure failures. `held` runs are neutral
    (skipped); a `shipped` or `no_fix` run proves the engine works and resets
    the streak."""
    n = 0
    for r in reversed(rows):
        c = classify(r)
        if c == "infra":
            n += 1
        elif c == "held":
            continue
        else:
            break
    return n


def should_pause(rows: List[dict], threshold: int = 3) -> bool:
    """Pause a loop after `threshold` consecutive infra failures — a persistent
    environment/pincer problem, not a transient miss."""
    return consecutive_infra_failures(rows) >= threshold
