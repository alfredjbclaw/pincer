#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional, TypedDict, TypeVar

INFLIGHT_DEFAULT = Path.home() / ".openclaw" / "pincer" / "inflight.json"
DEFAULT_MAX_AGE_S = 5400

T = TypeVar("T")


class Entry(TypedDict):
    run_id: str
    pid: int
    started_at: str
    heartbeat: str


class RunTimeout(Exception):
    pass


def _inflight_path() -> Path:
    return Path(os.environ.get("PINCER_INFLIGHT", INFLIGHT_DEFAULT))


def _load(path: Optional[Path] = None) -> Dict[str, Entry]:
    p = path or _inflight_path()
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
        run_id = entry.get("run_id")
        pid = entry.get("pid")
        started_at = entry.get("started_at")
        heartbeat_value = entry.get("heartbeat")
        if (
            isinstance(run_id, str)
            and isinstance(pid, int)
            and isinstance(started_at, str)
            and isinstance(heartbeat_value, str)
        ):
            rows[key] = {
                "run_id": run_id,
                "pid": pid,
                "started_at": started_at,
                "heartbeat": heartbeat_value,
            }
    return rows


def _save(rows: Dict[str, Entry], path: Optional[Path] = None) -> None:
    p = path or _inflight_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(rows, f)
    except OSError:
        pass


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


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


def claim(key, run_id, pid, ts, path=None) -> bool:
    rows = _load(path)
    entry = rows.get(key)
    if entry is not None and _is_live(entry, ts, DEFAULT_MAX_AGE_S):
        return False

    rows[key] = {
        "run_id": run_id,
        "pid": pid,
        "started_at": ts,
        "heartbeat": ts,
    }
    _save(rows, path)
    return True


def is_inflight(key, *, now_ts, max_age_s=DEFAULT_MAX_AGE_S, path=None) -> bool:
    entry = _load(path).get(key)
    return entry is not None and _is_live(entry, now_ts, max_age_s)


def heartbeat(key, ts, path=None) -> None:
    rows = _load(path)
    entry = rows.get(key)
    if entry is None:
        return
    entry["heartbeat"] = ts
    _save(rows, path)


def release(key, path=None) -> None:
    rows = _load(path)
    if key not in rows:
        return
    rows.pop(key)
    _save(rows, path)


def reap_stale(now_ts, max_age_s=DEFAULT_MAX_AGE_S, path=None) -> list[str]:
    rows = _load(path)
    reaped = [key for key, entry in rows.items() if not _is_live(entry, now_ts, max_age_s)]
    if not reaped:
        return []
    for key in reaped:
        rows.pop(key, None)
    _save(rows, path)
    return reaped


def run_with_timeout(
    fn: Callable[[], T],
    timeout_s: float,
    on_timeout: Optional[Callable[[], None]] = None,
) -> T:
    results: queue.Queue[T] = queue.Queue(maxsize=1)
    errors: queue.Queue[BaseException] = queue.Queue(maxsize=1)
    done = threading.Event()
    timed_out = threading.Event()

    def worker() -> None:
        try:
            results.put(fn())
        except BaseException as exc:
            errors.put(exc)
        finally:
            done.set()

    def watchdog() -> None:
        if done.wait(timeout_s):
            return
        timed_out.set()
        try:
            if on_timeout is not None:
                on_timeout()
        finally:
            done.set()

    worker_thread = threading.Thread(target=worker, daemon=True)
    watchdog_thread = threading.Thread(target=watchdog, daemon=True)
    worker_thread.start()
    watchdog_thread.start()
    done.wait()

    if timed_out.is_set():
        raise RunTimeout()
    if not errors.empty():
        raise errors.get()
    return results.get()
