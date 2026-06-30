#!/usr/bin/env python3
"""notify — optional alerting shim.

Pincer can post run progress to an external alerting tool, but it must work
fine when none is present (the public, standalone case). This module tries to
load a real alert backend and otherwise falls back to no-ops that print to
stdout, so callers can always `from notify import send_alert, AlertThread`.

Backend resolution:
  1. $PINCER_NOTIFY_MODULE — import that module (must expose `send_alert` and,
     ideally, `AlertThread`).
  2. plain `import telegram_alert` — the historical default (a workspace tool).
  3. neither importable -> built-in no-op fallback.

The no-op `AlertThread` mirrors the real interface (a `.post(body, level=...)`
method) so threading code works unchanged whether or not a backend exists.
"""
from __future__ import annotations

import importlib
import os


def _load_backend():
    name = os.environ.get("PINCER_NOTIFY_MODULE")
    candidates = [name] if name else []
    candidates.append("telegram_alert")
    for mod in candidates:
        if not mod:
            continue
        try:
            return importlib.import_module(mod)
        except Exception:
            continue
    return None


_BACKEND = _load_backend()


if _BACKEND is not None and hasattr(_BACKEND, "send_alert"):
    send_alert = _BACKEND.send_alert
else:
    def send_alert(msg, **kwargs):  # type: ignore[misc]
        """No-op fallback: log the alert to stdout instead of sending it."""
        print("[alert]", msg)
        return None


if _BACKEND is not None and hasattr(_BACKEND, "AlertThread"):
    AlertThread = _BACKEND.AlertThread
else:
    class AlertThread:  # type: ignore[no-redef]
        """No-op stand-in matching the real AlertThread interface.

        Posts print to stdout (subject to the same min_level floor) so a run's
        progress is still visible locally without any external backend.
        """

        _ORDER = {"progress": 0, "milestone": 1, "critical": 2}

        def __init__(self, tag=None, topic_id=None, min_level="progress"):
            self.tag = tag
            self.topic_id = topic_id
            self.min_level = min_level

        def post(self, body, level="progress"):
            if self._ORDER.get(level, 0) < self._ORDER.get(self.min_level, 0):
                return None
            print("[alert]", body)
            return None


if _BACKEND is not None and hasattr(_BACKEND, "LiveBoard"):
    LiveBoard = _BACKEND.LiveBoard
else:
    class LiveBoard:  # type: ignore[no-redef]
        """No-op stand-in for the real LiveBoard (a single live-updating status
        message). Same `post(body, level=...)` interface as AlertThread, so it's
        a drop-in; without a backend it just prints the board lines."""

        _ORDER = {"progress": 0, "milestone": 1, "critical": 2}

        def __init__(self, tag=None, topic_id=None, min_level="milestone", silent=True):
            self.tag = tag
            self.topic_id = topic_id
            self.min_level = min_level
            self.silent = silent
            self.lines = []

        def post(self, body, level="progress"):
            if self._ORDER.get(level, 0) < self._ORDER.get(self.min_level, 0):
                return True
            self.lines.append(body)
            print("[board]", body)
            return True
