#!/usr/bin/env python3
"""pincer memory monitor — make multi-VM sandboxing safe and observable.

Each Apple VZ Crabbox VM pre-allocates ~8 GiB up front (the 2026-06-15 incident:
stacked VMs → swap/jetsam). To use more of the Mac Mini (48 GiB) without that
risk, the orchestrator asks this module BEFORE starting each VM:

    can_start_vm(min_free_gb, vm_memory_gb) -> (ok, why)

It allows up to `max_concurrent` VMs *only while* there's enough free RAM to
absorb another one above a safety floor — so 2-3 builds run in parallel when the
box is idle and it backs off to fewer when memory is tight. Because the check
reads HOST free RAM (not a per-process counter), it stays safe even with several
pincer processes running at once.

It also samples RAM around each VM to a JSONL log so usage and leaks can be
tracked and addressed directly. Complements (does not replace) the host-level
`ai.openclaw.memory-watchdog` cron, which is the alerting safety net.

Readers shell out (`vm_stat`, `ps`, `sysctl`); the decision/parse logic is pure
and unit-tested via injected inputs.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

SAMPLES_DEFAULT = Path.home() / ".openclaw" / "pincer" / "mem-samples.jsonl"
_SAMPLES_KEEP = 2000
_SAMPLES_MAX_BYTES = 4_000_000
LOG = logging.getLogger("pincer")


def _prepare_parent(path: Path) -> None:
    parent = path.parent
    created = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink():
        raise OSError(f"sample parent is a symlink: {parent}")
    if created or os.path.abspath(parent) == os.path.abspath(SAMPLES_DEFAULT.parent):
        os.chmod(parent, 0o700)


def _vm_stat() -> str:
    try:
        return subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def available_gb(vmstat: Optional[str] = None) -> float:
    """RAM that can be handed to a new VM without paging: free + inactive +
    speculative pages (matching the host watchdog's definition)."""
    out = vmstat if vmstat is not None else _vm_stat()
    if not out:
        return 0.0
    m = re.search(r"page size of (\d+) bytes", out)
    page = int(m.group(1)) if m else 4096

    def pages(label: str) -> int:
        mm = re.search(rf"{re.escape(label)}:\s+(\d+)\.", out)
        return int(mm.group(1)) if mm else 0

    avail_pages = pages("Pages free") + pages("Pages inactive") + pages("Pages speculative")
    return avail_pages * page / (1024 ** 3)


def total_gb() -> float:
    try:
        b = int(subprocess.run(["sysctl", "-n", "hw.memsize"],
                               capture_output=True, text=True, timeout=10).stdout.strip())
        return b / (1024 ** 3)
    except Exception:
        return 0.0


def crabbox_vm_count(ps_output: Optional[str] = None) -> int:
    """Approximate count of live Crabbox/Apple-VZ VM processes (informational —
    the RAM check is the real gate)."""
    out = ps_output
    if out is None:
        try:
            out = subprocess.run(["ps", "-axo", "command="], capture_output=True,
                                 text=True, timeout=10).stdout
        except Exception:
            return 0
    markers = ("crabbox run", "apple-vz", "vz-helper", "VZVirtualMachine", "vfkit")
    return sum(1 for ln in out.splitlines() if any(k in ln for k in markers))


def can_start_vm(min_free_gb: float, vm_memory_gb: float,
                 avail: Optional[float] = None) -> Tuple[bool, str]:
    """True iff starting one more `vm_memory_gb` VM still leaves at least
    `min_free_gb` free (the headroom for the OS + gateway + a spike)."""
    a = avail if avail is not None else available_gb()
    headroom = a - vm_memory_gb
    if headroom < min_free_gb:
        return False, (f"{a:.1f}GB free — a {vm_memory_gb:.0f}GB VM would drop to "
                       f"{headroom:.1f}GB, below the {min_free_gb:.0f}GB floor")
    return True, f"{a:.1f}GB free (ok above {min_free_gb:.0f}GB floor)"


def sample(avail: Optional[float] = None, vm_count: Optional[int] = None) -> dict:
    a = avail if avail is not None else available_gb()
    return {"available_gb": round(a, 1), "total_gb": round(total_gb(), 1),
            "vm_count": vm_count if vm_count is not None else crabbox_vm_count()}


def _write_all(fd: int, payload: bytes) -> None:
    while payload:
        written = os.write(fd, payload)
        if written <= 0:
            raise OSError("short write for memory sample")
        payload = payload[written:]


def log_sample(event: str, ts: str, path: Optional[Path] = None, **extra) -> bool:
    """Append one RAM sample (best-effort), keeping the file bounded for leak
    tracking. `ts` is passed in so this stays clock-free."""
    p = path or SAMPLES_DEFAULT
    lock_fd: Optional[int] = None
    temp_fd: Optional[int] = None
    temp_path: Optional[str] = None
    try:
        _prepare_parent(p)
        row = {"ts": ts, "event": event, **sample(), **extra}
        lock_path = p.with_name(f".{p.name}.lock")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock_fd = os.open(lock_path, flags, 0o600)
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        original = b""
        try:
            read_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                read_flags |= os.O_NOFOLLOW
            source_fd = os.open(p, read_flags)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(source_fd, "rb") as source:
                original = source.read(_SAMPLES_MAX_BYTES + 1)
            if len(original) > _SAMPLES_MAX_BYTES:
                raise OSError("memory sample ledger exceeds bounded rewrite size")

        lines: List[str] = original.decode("utf-8").splitlines()
        lines.append(json.dumps(row))
        payload = ("\n".join(lines[-_SAMPLES_KEEP:]) + "\n").encode("utf-8")

        temp_fd, temp_path = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent)
        os.fchmod(temp_fd, 0o600)
        _write_all(temp_fd, payload)
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        os.replace(temp_path, p)
        temp_path = None
        dir_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            dir_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            dir_flags |= os.O_NOFOLLOW
        parent_fd = os.open(p.parent, dir_flags)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        LOG.exception("Failed to write memory sample at %s", p)
        return False
    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            except OSError:
                LOG.exception("Failed to remove temporary memory sample at %s", temp_path)
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                LOG.exception("Failed to unlock memory sample ledger at %s", p)
            finally:
                try:
                    os.close(lock_fd)
                except OSError:
                    LOG.exception("Failed to close memory sample lock at %s", p)
    return True


def detect_leak(samples: List[dict], drop_gb: float = 6.0) -> bool:
    """Heuristic: across `vm_end` samples, if available RAM never recovers and
    has fallen more than `drop_gb` from the first to the last VM-end, memory is
    likely not being released (a leak / orphaned VM) — worth a human's eyes."""
    ends = [s for s in samples if s.get("event") == "vm_end" and "available_gb" in s]
    if len(ends) < 3:
        return False
    first, last = ends[0]["available_gb"], ends[-1]["available_gb"]
    monotonic_down = all(ends[i]["available_gb"] >= ends[i + 1]["available_gb"]
                         for i in range(len(ends) - 1))
    return monotonic_down and (first - last) >= drop_gb
