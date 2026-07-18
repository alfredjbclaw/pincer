#!/usr/bin/env python3
"""Privacy-safe execution receipts for Pincer model dispatches.

Each real ``runtime_adapter.dispatch`` gets one outcome record and each provider
attempt gets one attempt record. Only bounded operational fields and numeric
token usage are retained; prompts, final text, stderr, commands, paths, session
IDs, credentials, and model reasoning are never written.

Tests are fail-closed against the live ledger: under pytest, recording is
disabled unless ``PINCER_DISPATCH_TELEMETRY`` explicitly names a fixture file.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import threading
import uuid
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


TELEMETRY_DEFAULT = Path.home() / ".openclaw" / "pincer" / "dispatch-telemetry.jsonl"
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,95}$")
_SAFE_STATUSES = {"done", "no_changes", "blocked", "error", "interrupted"}
_SAFE_FAILURE_CLASSES = {
    "none",
    "credit_exhausted",
    "auth_expired",
    "rate_limit_429",
    "timeout",
    "nonzero_exit",
    "contract_status",
    "missing_runtime",
}
_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
_tls = threading.local()
MAX_READ_RECORDS = 100_000
MAX_READ_BYTES = 4_000_000


def _path() -> Path:
    return Path(os.environ.get("PINCER_DISPATCH_TELEMETRY", TELEMETRY_DEFAULT))


def _enabled() -> bool:
    if os.environ.get("PINCER_TELEMETRY_DISABLE") == "1":
        return False
    if "PYTEST_CURRENT_TEST" in os.environ and "PINCER_DISPATCH_TELEMETRY" not in os.environ:
        return False
    return True


def enabled() -> bool:
    """Whether telemetry is expected for the current process."""
    return _enabled()


def _label(value: object, default: str = "unknown") -> str:
    if not isinstance(value, str):
        return default
    value = value.strip()
    if not value or not _SAFE_LABEL.fullmatch(value):
        return default
    return value[:96]


def _status(value: object) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    return normalized if normalized in _SAFE_STATUSES else "unknown"


def _failure_class(value: object) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    return normalized if normalized in _SAFE_FAILURE_CLASSES else "unknown"


def _bounded_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 10_000_000_000 else None


def _usage_candidate(value: Any) -> Optional[dict[str, int]]:
    if not isinstance(value, dict) or not any(field in value for field in _TOKEN_FIELDS):
        return None
    usage: dict[str, int] = {}
    for field in _TOKEN_FIELDS:
        number = _bounded_int(value.get(field))
        if number is not None:
            usage[field] = number
    if not usage:
        return None
    if "total_tokens" not in usage:
        usage["total_tokens"] = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    return usage


def parse_codex_usage(stdout: str) -> dict[str, int]:
    """Return the final numeric usage object found in Codex JSONL output."""
    final: dict[str, int] = {}

    def visit(value: Any) -> None:
        nonlocal final
        candidate = _usage_candidate(value)
        if candidate is not None:
            final = candidate
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for line in (stdout or "").splitlines():
        if not line.strip() or len(line) > 1_000_000:
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        visit(value)
    return final


def start_dispatch(*, attempt: int, resumed: bool, workdir: Path) -> str:
    dispatch_id = uuid.uuid4().hex
    _tls.context = {
        "dispatch_id": dispatch_id,
        "dispatch_attempt": max(1, int(attempt)),
        "resumed": bool(resumed),
        "transport_attempt": 0,
    }
    return dispatch_id


def clear_dispatch() -> None:
    _tls.context = None


def _context() -> Optional[dict]:
    value = getattr(_tls, "context", None)
    return value if isinstance(value, dict) else None


def _prepare_parent(target: Path) -> None:
    """Create a private ledger directory without chmoding caller-owned overrides."""
    parent = target.parent
    created = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink():
        raise OSError(f"ledger parent is a symlink: {parent}")
    if created or os.path.abspath(parent) == os.path.abspath(TELEMETRY_DEFAULT.parent):
        os.chmod(parent, 0o700)


def _fsync_parent(target: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target.parent, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _append(record: dict, path: Optional[Path] = None) -> bool:
    if not _enabled() and path is None:
        return False
    target = path or _path()
    try:
        _prepare_parent(target)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(target, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            start_offset = os.lseek(fd, 0, os.SEEK_END)
            payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            try:
                while payload:
                    written = os.write(fd, payload)
                    if written <= 0:
                        raise OSError("short write for dispatch telemetry")
                    payload = payload[written:]
                os.fsync(fd)
            except BaseException:
                try:
                    os.ftruncate(fd, start_offset)
                    os.fsync(fd)
                except OSError:
                    pass
                raise
            _fsync_parent(target)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
    except OSError:
        return False
    return True


def record_attempt(
    *,
    runtime: str,
    model: str,
    effort: str,
    status: str,
    exit_code: int,
    duration_seconds: float,
    failure_class: str,
    stdout: str = "",
) -> bool:
    context = _context()
    if context is None or not _enabled():
        return False
    try:
        context["transport_attempt"] += 1
        usage = parse_codex_usage(stdout) if runtime == "codex" else {}
        record = {
            "schema_version": 1,
            "kind": "attempt",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dispatch_id": context["dispatch_id"],
            "dispatch_attempt": context["dispatch_attempt"],
            "transport_attempt": context["transport_attempt"],
            "resumed": context["resumed"],
            "runtime": _label(runtime),
            "model": _label(model),
            "effort": _label(effort),
            "status": _status(status),
            "exit_code": int(exit_code),
            "duration_seconds": round(max(0.0, float(duration_seconds)), 3),
            "failure_class": _failure_class(failure_class),
            "usage_known": bool(usage),
            **{field: usage.get(field) for field in _TOKEN_FIELDS},
        }
        return _append(record)
    except Exception:
        return False


def record_outcome(
    *,
    runtime: str,
    status: str,
    exit_code: int,
    duration_seconds: float,
    fallback_used: bool,
    fallback_reason: Optional[str],
    empty_diff: bool,
) -> bool:
    context = _context()
    if context is None or not _enabled():
        return False
    try:
        record = {
            "schema_version": 1,
            "kind": "outcome",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dispatch_id": context["dispatch_id"],
            "dispatch_attempt": context["dispatch_attempt"],
            "transport_attempts": context["transport_attempt"],
            "resumed": context["resumed"],
            "runtime": _label(runtime),
            "status": _status(status),
            "exit_code": int(exit_code),
            "duration_seconds": round(max(0.0, float(duration_seconds)), 3),
            "fallback_used": bool(fallback_used),
            "fallback_reason": _label(fallback_reason or "none"),
            "empty_diff": bool(empty_diff),
        }
        return _append(record)
    except Exception:
        return False


def read_records(
    path: Optional[Path] = None,
    limit: int = 20_000,
    errors: Optional[list[str]] = None,
    stats: Optional[dict] = None,
) -> list[dict]:
    target = path or _path()
    bound = max(1, min(int(limit), MAX_READ_RECORDS))
    records: deque[dict] = deque(maxlen=bound)
    valid_records = 0
    bytes_total = 0
    bytes_scanned = 0
    byte_window_truncated = False
    failed = False

    def note(error_class: str) -> None:
        nonlocal failed
        failed = True
        if errors is not None and error_class not in errors:
            errors.append(error_class)

    try:
        with target.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                bytes_total = os.fstat(handle.fileno()).st_size
                start = max(0, bytes_total - MAX_READ_BYTES)
                byte_window_truncated = start > 0
                if start:
                    handle.seek(start - 1)
                    encoded = handle.read(MAX_READ_BYTES + 1)
                    if encoded[:1] == b"\n":
                        encoded = encoded[1:]
                    else:
                        boundary = encoded.find(b"\n")
                        if boundary < 0:
                            note("LineTooLong")
                            encoded = b""
                        else:
                            encoded = encoded[boundary + 1:]
                else:
                    encoded = handle.read(MAX_READ_BYTES)
                bytes_scanned = len(encoded)
                for encoded_line in encoded.splitlines():
                    if len(encoded_line) > 1_000_000:
                        note("LineTooLong")
                        continue
                    try:
                        value = json.loads(encoded_line)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        note(type(exc).__name__)
                        continue
                    if isinstance(value, dict):
                        valid_records += 1
                        records.append(value)
                    else:
                        note("InvalidShape")
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return []
    except UnicodeDecodeError:
        note("UnicodeDecodeError")
    except OSError as exc:
        note(type(exc).__name__)
    safe_records = [] if failed else list(records)
    if stats is not None:
        stats.update({
            "valid_records": valid_records,
            "summarized_records": len(safe_records),
            "bytes_total": bytes_total,
            "bytes_scanned": bytes_scanned,
            "window_truncated": (
                byte_window_truncated or (not failed and valid_records > len(safe_records))
            ),
        })
    return safe_records


def summarize(records: list[dict]) -> dict:
    attempts = [record for record in records if record.get("kind") == "attempt"]
    outcomes = [record for record in records if record.get("kind") == "outcome"]
    known = [record for record in attempts if record.get("usage_known") is True]

    def counter(record: dict, field: str) -> int:
        return _bounded_int(record.get(field)) or 0

    invalid_usage = sum(
        any(record.get(field) is not None and _bounded_int(record.get(field)) is None
            for field in _TOKEN_FIELDS)
        for record in known
    )
    total_input = sum(counter(record, "input_tokens") for record in known)
    total_cached = sum(counter(record, "cached_input_tokens") for record in known)
    failure_classes = [
        _failure_class(record.get("failure_class"))
        for record in attempts
        if record.get("failure_class") is not None
    ]
    return {
        "attempts": len(attempts),
        "outcomes": len(outcomes),
        "usage_known_attempts": len(known),
        "invalid_usage_records": invalid_usage,
        "input_tokens": total_input,
        "cached_input_tokens": total_cached,
        "output_tokens": sum(counter(record, "output_tokens") for record in known),
        "reasoning_output_tokens": sum(
            counter(record, "reasoning_output_tokens") for record in known
        ),
        "cache_read_ratio": round(total_cached / total_input, 4) if total_input else None,
        "resumed_outcomes": sum(record.get("resumed") is True for record in outcomes),
        "fallback_outcomes": sum(record.get("fallback_used") is True for record in outcomes),
        "empty_diff_outcomes": sum(record.get("empty_diff") is True for record in outcomes),
        "outcome_statuses": dict(
            sorted(Counter(_status(record.get("status")) for record in outcomes).items())
        ),
        "failure_classes": dict(
            sorted(Counter(value for value in failure_classes if value != "none").items())
        ),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize privacy-safe Pincer dispatch telemetry.")
    parser.add_argument("--path", type=Path, default=None, help="ledger override")
    parser.add_argument("--limit", type=int, default=20_000)
    args = parser.parse_args(argv)
    errors: list[str] = []
    stats: dict = {}
    result = summarize(read_records(args.path, args.limit, errors=errors, stats=stats))
    result.update({
        "records": stats.get("valid_records", 0),
        "summarized_records": stats.get("summarized_records", 0),
        "bytes_total": stats.get("bytes_total", 0),
        "bytes_scanned": stats.get("bytes_scanned", 0),
        "window_truncated": stats.get("window_truncated", False),
    })
    if errors:
        result["read_errors"] = sorted({_label(error) for error in errors})
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
