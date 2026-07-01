#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, TypedDict

GLOBAL_GATE_DEFAULT = Path.home() / ".openclaw" / "pincer" / "global-gate.json"
DEFAULT_MAX_AGE_S = 600
DEFAULT_POLL_S = 5.0


class Entry(TypedDict):
    holder_id: str
    label: str
    pid: int
    started_at: str
    heartbeat: str


class GateTimeout(Exception):
    pass


def _gate_path() -> Path:
    return Path(os.environ.get("PINCER_GLOBAL_GATE", GLOBAL_GATE_DEFAULT))


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


@contextlib.contextmanager
def _locked(path: Path):
    lock = _lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _load(path: Optional[Path] = None) -> Dict[str, Entry]:
    p = path or _gate_path()
    try:
        raw = json.loads(Path(p).read_text())
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        return {}

    if not isinstance(raw, dict):
        return {}

    rows: Dict[str, Entry] = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        holder_id = entry.get("holder_id")
        label = entry.get("label")
        pid = entry.get("pid")
        started_at = entry.get("started_at")
        heartbeat_value = entry.get("heartbeat")
        if (
            isinstance(holder_id, str)
            and isinstance(label, str)
            and isinstance(pid, int)
            and isinstance(started_at, str)
            and isinstance(heartbeat_value, str)
        ):
            rows[key] = {
                "holder_id": holder_id,
                "label": label,
                "pid": pid,
                "started_at": started_at,
                "heartbeat": heartbeat_value,
            }
    return rows


def _save(rows: Dict[str, Entry], path: Optional[Path] = None) -> None:
    p = path or _gate_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(rows, f)
    except OSError:
        pass


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _is_live(entry, now_ts, max_age_s) -> bool:
    try:
        age_s = (_parse_ts(now_ts) - _parse_ts(entry["heartbeat"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return False
    return _pid_alive(entry.get("pid")) and age_s <= max_age_s


def _reap_stale_unlocked(rows: Dict[str, Entry], now_ts: str, max_age_s: int) -> list[str]:
    reaped = [key for key, entry in rows.items() if not _is_live(entry, now_ts, max_age_s)]
    for key in reaped:
        rows.pop(key, None)
    return reaped


def acquire(
    holder_id: str,
    *,
    label: str,
    pid: int,
    ts: str,
    max_slots: int,
    max_age_s: int = DEFAULT_MAX_AGE_S,
    path: Optional[Path] = None,
) -> bool:
    p = path or _gate_path()
    slots = max(1, int(max_slots))
    with _locked(p):
        rows = _load(p)
        _reap_stale_unlocked(rows, ts, max_age_s)
        entry = rows.get(holder_id)
        if entry is not None and _is_live(entry, ts, max_age_s):
            entry["heartbeat"] = ts
            _save(rows, p)
            return True
        if len(rows) >= slots:
            _save(rows, p)
            return False
        rows[holder_id] = {
            "holder_id": holder_id,
            "label": label,
            "pid": pid,
            "started_at": ts,
            "heartbeat": ts,
        }
        _save(rows, p)
        return True


def heartbeat(holder_id: str, ts: str, path: Optional[Path] = None) -> None:
    p = path or _gate_path()
    with _locked(p):
        rows = _load(p)
        entry = rows.get(holder_id)
        if entry is None:
            return
        entry["heartbeat"] = ts
        _save(rows, p)


def release(holder_id: str, path: Optional[Path] = None) -> None:
    p = path or _gate_path()
    with _locked(p):
        rows = _load(p)
        if holder_id not in rows:
            return
        rows.pop(holder_id, None)
        _save(rows, p)


def reap_stale(
    now_ts: str,
    *,
    max_age_s: int = DEFAULT_MAX_AGE_S,
    path: Optional[Path] = None,
) -> list[str]:
    p = path or _gate_path()
    with _locked(p):
        rows = _load(p)
        reaped = _reap_stale_unlocked(rows, now_ts, max_age_s)
        if reaped:
            _save(rows, p)
        return reaped


def wait_acquire(
    holder_id: str,
    *,
    label: str,
    pid: int,
    max_slots: int,
    timeout_s: float,
    poll_s: float = DEFAULT_POLL_S,
    max_age_s: int = DEFAULT_MAX_AGE_S,
    path: Optional[Path] = None,
    now_fn: Callable[[], str] = _utc_ts,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if acquire(
            holder_id,
            label=label,
            pid=pid,
            ts=now_fn(),
            max_slots=max_slots,
            max_age_s=max_age_s,
            path=path,
        ):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        sleep_fn(min(max(0.0, poll_s), remaining))


@contextlib.contextmanager
def slot(
    *,
    max_slots: int,
    wait_timeout_s: float,
    label: str = "dispatch",
    max_age_s: int = DEFAULT_MAX_AGE_S,
    heartbeat_interval_s: float = 30.0,
    path: Optional[Path] = None,
):
    holder_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
    if not wait_acquire(
        holder_id,
        label=label,
        pid=os.getpid(),
        max_slots=max_slots,
        timeout_s=wait_timeout_s,
        max_age_s=max_age_s,
        path=path,
    ):
        raise GateTimeout(f"global dispatch gate unavailable after {wait_timeout_s:.0f}s")

    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(heartbeat_interval_s):
            heartbeat(holder_id, _utc_ts(), path=path)

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield holder_id
    finally:
        stop.set()
        release(holder_id, path=path)
