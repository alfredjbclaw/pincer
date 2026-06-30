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

import json
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

SAMPLES_DEFAULT = Path.home() / ".openclaw" / "pincer" / "mem-samples.jsonl"
_SAMPLES_KEEP = 2000


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


def log_sample(event: str, ts: str, path: Optional[Path] = None, **extra) -> None:
    """Append one RAM sample (best-effort), keeping the file bounded for leak
    tracking. `ts` is passed in so this stays clock-free."""
    p = path or SAMPLES_DEFAULT
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": ts, "event": event, **sample(), **extra}
        lines: List[str] = []
        if p.exists():
            lines = p.read_text().splitlines()
        lines.append(json.dumps(row))
        p.write_text("\n".join(lines[-_SAMPLES_KEEP:]) + "\n")
    except Exception:
        pass


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
