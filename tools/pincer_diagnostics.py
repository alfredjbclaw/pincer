#!/usr/bin/env python3
"""Read-only health report for Pincer loops, fleets, ledgers, and dispatches.

The report emits only counts, fixed classifications, timestamps, modes, and
numeric usage. It never emits prompts, commands, log lines, arbitrary persisted
labels, error bodies, repository paths, credentials, or session identifiers.
"""
from __future__ import annotations

import argparse
import fcntl
import itertools
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import dispatch_telemetry
import mem_monitor
import run_ledger
import work_history


STATE_DEFAULT = Path.home() / ".openclaw" / "pincer"
DRIVER_LABEL = "ai.openclaw.pincer-loop-driver"
MAX_LOG_BYTES = 1_000_000
MAX_JSONL_ROWS = 20_000
MAX_JSONL_BYTES = 4_000_000
MAX_JSON_BYTES = 1_000_000
MAX_STATE_FILES = 1_000
_WORK_OUTCOMES = {
    "shipped", "error", "failed", "no_winner", "rejected", "escalated", "cleared",
}
_FLEET_PHASES = {"start", "build", "verify", "done", "exception"}
_FLEET_VERDICTS = {"pass", "fail", "error"}
_ERROR_CLASSES = {
    "ArtifactTooLarge",
    "InvalidShape",
    "IsADirectoryError",
    "JSONDecodeError",
    "LineTooLong",
    "NotADirectoryError",
    "OSError",
    "PermissionError",
    "UnicodeDecodeError",
}


def _enum(value: object, allowed: set[str]) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    return normalized if normalized in allowed else "unknown"


def _error_class(value: object) -> str:
    return value if isinstance(value, str) and value in _ERROR_CLASSES else "unknown"


def _identity_key(value: object) -> str:
    """Canonical internal identity for a persisted JSON value; never emitted."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def probe_driver() -> dict:
    try:
        proc = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{DRIVER_LABEL}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"available": False, "loaded": None}
    return {"available": True, "loaded": proc.returncode == 0}


def _file_info(path: Path, now: float, errors: Optional[list[str]] = None) -> dict:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": False}
    except OSError as exc:
        if errors is not None:
            errors.append(type(exc).__name__)
        return {"exists": None}
    return {
        "exists": True,
        "bytes": stat.st_size,
        "age_seconds": max(0, int(now - stat.st_mtime)),
        "mode": f"{stat.st_mode & 0o777:03o}",
    }


def _read_json(path: Path, default, errors: Optional[list[str]] = None):
    try:
        with path.open("rb") as handle:
            encoded = handle.read(MAX_JSON_BYTES + 1)
        if len(encoded) > MAX_JSON_BYTES:
            if errors is not None:
                errors.append("ArtifactTooLarge")
            return default
        value = json.loads(encoded)
    except FileNotFoundError:
        return default
    except OSError as exc:
        if errors is not None:
            errors.append(type(exc).__name__)
        return default
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if errors is not None:
            errors.append(type(exc).__name__)
        return default
    return value


def _read_jsonl(
    path: Path,
    limit: int = MAX_JSONL_ROWS,
    *,
    errors: Optional[list[str]] = None,
    stats: Optional[dict] = None,
) -> list[dict]:
    rows: deque[dict] = deque(maxlen=max(1, min(int(limit), MAX_JSONL_ROWS)))
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
        with path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                bytes_total = os.fstat(handle.fileno()).st_size
                start = max(0, bytes_total - MAX_JSONL_BYTES)
                byte_window_truncated = start > 0
                if start:
                    handle.seek(start - 1)
                    encoded = handle.read(MAX_JSONL_BYTES + 1)
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
                    encoded = handle.read(MAX_JSONL_BYTES)
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
                        rows.append(value)
                    else:
                        note("InvalidShape")
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        pass
    except UnicodeDecodeError:
        note("UnicodeDecodeError")
    except OSError as exc:
        note(type(exc).__name__)
    safe_rows = [] if failed else list(rows)
    if stats is not None:
        stats.update({
            "valid_records": valid_records,
            "summarized_records": len(safe_rows),
            "bytes_total": bytes_total,
            "bytes_scanned": bytes_scanned,
            "window_truncated": (
                byte_window_truncated or (not failed and valid_records > len(safe_rows))
            ),
        })
    return safe_rows


def _driver_log(
    path: Path,
    errors: Optional[list[str]] = None,
    configured_names: Optional[set[str]] = None,
) -> dict:
    try:
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            start = max(0, size - MAX_LOG_BYTES)
            if start:
                handle.seek(start - 1)
                encoded = handle.read(MAX_LOG_BYTES + 1)
                if encoded[:1] == b"\n":
                    encoded = encoded[1:]
                else:
                    boundary = encoded.find(b"\n")
                    encoded = b"" if boundary < 0 else encoded[boundary + 1:]
            else:
                encoded = handle.read(MAX_LOG_BYTES)
            text = encoded.decode("utf-8", "replace")
    except FileNotFoundError:
        return {"ticks": 0, "loop_results": 0, "distinct_loop_results": 0,
                "bytes_scanned": 0, "window_truncated": False, "synthetic_only": False}
    except OSError as exc:
        if errors is not None:
            errors.append(type(exc).__name__)
        return {"ticks": 0, "loop_results": 0, "distinct_loop_results": 0,
                "bytes_scanned": 0, "window_truncated": False, "synthetic_only": False}
    names = re.findall(r"\bLoop ([A-Za-z0-9_.-]+) result:", text)
    known = configured_names or set()
    return {
        "ticks": len(re.findall(r"Loop driver tick start", text)),
        "loop_results": len(names),
        "distinct_loop_results": min(len(set(names)), 50),
        "bytes_scanned": len(encoded),
        "window_truncated": start > 0,
        "synthetic_only": (
            bool(names)
            and set(names).issubset({"a", "b"})
            and set(names).isdisjoint(known)
        ),
    }


def _loop_configs(state_dir: Path, errors: list[str]) -> tuple[dict, set[str]]:
    candidates = list(itertools.islice(
        (state_dir / "loops").glob("*.json"), MAX_STATE_FILES + 1
    ))
    window_truncated = len(candidates) > MAX_STATE_FILES
    paths = sorted(candidates[:MAX_STATE_FILES])
    invalid_files = 0
    configured = 0
    enabled = 0
    schedule_present = 0
    last_run_present = 0
    configured_names: set[str] = set()
    for path in paths:
        error_count = len(errors)
        value = _read_json(path, {}, errors)
        if len(errors) != error_count:
            invalid_files += 1
            continue
        if not isinstance(value, dict):
            if "InvalidShape" not in errors:
                errors.append("InvalidShape")
            invalid_files += 1
            continue
        configured += 1
        enabled += value.get("enabled") is True
        schedule_present += isinstance(value.get("schedule"), str) and bool(value["schedule"])
        last_run_present += isinstance(value.get("last_run"), str) and bool(value["last_run"])
        raw_name = value.get("name", path.stem)
        if isinstance(raw_name, str):
            configured_names.add(raw_name)
    return {
        "files_seen": len(paths),
        "configured": configured,
        "invalid_files": invalid_files,
        "window_truncated": window_truncated,
        "enabled": enabled,
        "disabled": configured - enabled,
        "schedule_present": schedule_present,
        "last_run_present": last_run_present,
    }, configured_names


def _run_ledger_summary(path: Path, errors: list[str]) -> dict:
    stats: dict = {}
    rows = _read_jsonl(path, errors=errors, stats=stats)
    classes = Counter(run_ledger.classify(row) for row in rows)
    by_name: dict[str, tuple[object, list[dict]]] = {}
    for row in rows:
        raw_name = row.get("name")
        key = _identity_key(raw_name)
        if key not in by_name:
            by_name[key] = (raw_name, [])
        by_name[key][1].append(row)
    infra_streaks = []
    for _raw_name, items in by_name.values():
        streak = run_ledger.consecutive_infra_failures(items)
        if streak:
            infra_streaks.append(streak)
    return {
        "records": stats.get("valid_records", 0),
        "summarized_records": stats.get("summarized_records", 0),
        "bytes_total": stats.get("bytes_total", 0),
        "bytes_scanned": stats.get("bytes_scanned", 0),
        "window_truncated": stats.get("window_truncated", False),
        "classes": dict(sorted(classes.items())),
        "infra_streak_groups": len(infra_streaks),
        "max_infra_streak": max(infra_streaks, default=0),
    }


def _work_history_summary(path: Path, errors: list[str]) -> dict:
    stats: dict = {}
    rows = _read_jsonl(path, errors=errors, stats=stats)
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[
            (_identity_key(row.get("repo")), _identity_key(str(row.get("issue", "unknown"))))
        ].append(row)
    return {
        "records": stats.get("valid_records", 0),
        "summarized_records": stats.get("summarized_records", 0),
        "bytes_total": stats.get("bytes_total", 0),
        "bytes_scanned": stats.get("bytes_scanned", 0),
        "window_truncated": stats.get("window_truncated", False),
        "outcomes": dict(sorted(Counter(
            _enum(row.get("outcome"), _WORK_OUTCOMES) for row in rows
        ).items())),
        "active_escalations": sum(work_history.is_escalated(items) for items in grouped.values()),
    }


def _memory_summary(path: Path, errors: list[str]) -> dict:
    stats: dict = {}
    rows = _read_jsonl(path, errors=errors, stats=stats)
    safe_rows = [
        {"event": row.get("event"), "available_gb": row.get("available_gb")}
        for row in rows
        if isinstance(row.get("available_gb"), (int, float))
    ]
    return {
        "records": stats.get("valid_records", 0),
        "summarized_records": stats.get("summarized_records", 0),
        "bytes_total": stats.get("bytes_total", 0),
        "bytes_scanned": stats.get("bytes_scanned", 0),
        "window_truncated": stats.get("window_truncated", False),
        "vm_start": sum(row.get("event") == "vm_start" for row in rows),
        "vm_end": sum(row.get("event") == "vm_end" for row in rows),
        "leak_signal": mem_monitor.detect_leak(safe_rows),
    }


def _fleet_states(state_dir: Path, errors: list[str]) -> dict:
    phase = Counter()
    verdict = Counter()
    committed_items = 0
    files = 0
    candidates = list(itertools.islice(state_dir.glob("*-state.json"), MAX_STATE_FILES + 1))
    window_truncated = len(candidates) > MAX_STATE_FILES
    paths = sorted(candidates[:MAX_STATE_FILES])
    invalid_files = 0
    for path in paths:
        error_count = len(errors)
        value = _read_json(path, {}, errors)
        if len(errors) != error_count:
            invalid_files += 1
            continue
        if not isinstance(value, dict):
            if "InvalidShape" not in errors:
                errors.append("InvalidShape")
            invalid_files += 1
            continue
        files += 1
        phase[_enum(value.get("phase"), _FLEET_PHASES)] += 1
        verdict[_enum(value.get("final_verdict"), _FLEET_VERDICTS)] += 1
        if isinstance(value.get("committed"), list):
            committed_items += len(value["committed"])
    return {
        "files_seen": len(paths),
        "files": files,
        "invalid_files": invalid_files,
        "window_truncated": window_truncated,
        "phases": dict(sorted(phase.items())),
        "verdicts": dict(sorted(verdict.items())),
        "committed_items": committed_items,
    }


def collect(
    state_dir: Path = STATE_DEFAULT,
    *,
    now: Optional[float] = None,
    driver_probe: Callable[[], dict] = probe_driver,
) -> dict:
    state_dir = Path(state_dir)
    now = time.time() if now is None else now
    paths = {
        "driver_log": state_dir / "logs" / "loop-driver.log",
        "run_ledger": state_dir / "run-ledger.jsonl",
        "work_history": state_dir / "work-history.jsonl",
        "memory_samples": state_dir / "mem-samples.jsonl",
        "dispatch_telemetry": state_dir / "dispatch-telemetry.jsonl",
        "inflight": state_dir / "inflight.json",
        "global_gate": state_dir / "global-gate.json",
    }
    read_errors: dict[str, list[str]] = {name: [] for name in paths}
    read_errors["loop_configs"] = []
    read_errors["fleet_states"] = []
    artifacts = {
        name: _file_info(path, now, read_errors[name])
        for name, path in paths.items()
    }
    driver = driver_probe()
    loops, configured_names = _loop_configs(state_dir, read_errors["loop_configs"])
    driver_log = _driver_log(
        paths["driver_log"], read_errors["driver_log"], configured_names
    )
    inflight = _read_json(paths["inflight"], {}, read_errors["inflight"])
    gate = _read_json(paths["global_gate"], {}, read_errors["global_gate"])
    run_summary = _run_ledger_summary(paths["run_ledger"], read_errors["run_ledger"])
    work_summary = _work_history_summary(paths["work_history"], read_errors["work_history"])
    memory_summary = _memory_summary(paths["memory_samples"], read_errors["memory_samples"])
    dispatch_stats: dict = {}
    dispatch_records = dispatch_telemetry.read_records(
        paths["dispatch_telemetry"],
        errors=read_errors["dispatch_telemetry"],
        stats=dispatch_stats,
    )
    fleet_states = _fleet_states(state_dir, read_errors["fleet_states"])
    warnings = []

    for name in ("run_ledger", "work_history", "memory_samples", "dispatch_telemetry"):
        if artifacts[name]["exists"] is False:
            warnings.append({"code": "missing_artifact", "artifact": name})
    if driver_log["synthetic_only"]:
        warnings.append({"code": "driver_log_synthetic_only", "artifact": "driver_log"})
    for name, errors in read_errors.items():
        if errors:
            warnings.append({
                "code": "unreadable_artifact",
                "artifact": name,
                "error_class": _error_class(errors[0]),
            })
    if driver.get("loaded") is False and loops["enabled"]:
        warnings.append({"code": "enabled_loops_driver_unloaded", "count": loops["enabled"]})
    insecure = sorted(name for name, info in artifacts.items() if info.get("exists") and int(info["mode"], 8) & 0o077)
    if insecure:
        warnings.append({"code": "insecure_artifact_permissions", "artifacts": insecure})

    dispatch_summary = dispatch_telemetry.summarize(dispatch_records)
    dispatch_summary.update({
        "records": dispatch_stats.get("valid_records", 0),
        "summarized_records": dispatch_stats.get("summarized_records", 0),
        "bytes_total": dispatch_stats.get("bytes_total", 0),
        "bytes_scanned": dispatch_stats.get("bytes_scanned", 0),
        "window_truncated": dispatch_stats.get("window_truncated", False),
    })

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "state_dir_exists": state_dir.is_dir(),
        "driver": {**driver, **driver_log},
        "loops": loops,
        "artifacts": artifacts,
        "run_ledger": run_summary,
        "work_history": work_summary,
        "memory": memory_summary,
        "dispatch": dispatch_summary,
        "fleet_states": fleet_states,
        "inflight_entries": len(inflight) if isinstance(inflight, dict) else None,
        "global_gate_entries": len(gate) if isinstance(gate, dict) else None,
        "warnings": warnings,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Emit a privacy-safe Pincer health report.")
    parser.add_argument("--state-dir", type=Path, default=STATE_DEFAULT)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    result = collect(args.state_dir)
    encoded = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.output.parent.is_symlink():
            raise OSError("output parent must not be a symlink")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        elif args.output.is_symlink():
            raise OSError("output must not be a symlink")
        fd = os.open(args.output, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            payload = (encoded + "\n").encode("utf-8")
            while payload:
                written = os.write(fd, payload)
                if written <= 0:
                    raise OSError("short write for diagnostic output")
                payload = payload[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    print(encoded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
