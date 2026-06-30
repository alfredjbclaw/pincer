#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import mem_monitor as mm

# Apple Silicon uses 16 KiB pages; the parser reads the size from the header.
VMSTAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               65536.
Pages active:                            200000.
Pages inactive:                           32768.
Pages speculative:                         2048.
Pages wired down:                        100000.
"""


def test_available_gb_parses_vmstat():
    # (65536 + 32768 + 2048) pages * 16384 bytes = 100352 * 16384 = ~1.53 GiB
    gb = mm.available_gb(VMSTAT)
    assert 1.4 < gb < 1.7


def test_available_gb_empty_is_zero():
    assert mm.available_gb("") == 0.0


def test_can_start_vm_allows_when_headroom():
    ok, why = mm.can_start_vm(min_free_gb=12, vm_memory_gb=8, avail=30.0)
    assert ok
    assert "30.0GB" in why


def test_can_start_vm_blocks_when_tight():
    ok, why = mm.can_start_vm(min_free_gb=12, vm_memory_gb=8, avail=18.0)
    assert not ok  # 18 - 8 = 10 < 12 floor
    assert "floor" in why


def test_can_start_vm_exact_boundary():
    # avail - vm == floor exactly -> allowed
    ok, _ = mm.can_start_vm(min_free_gb=12, vm_memory_gb=8, avail=20.0)
    assert ok


def test_crabbox_vm_count_counts_markers():
    ps = ("/usr/bin/python3 something\n"
          "crabbox run --provider applevz -- pytest\n"
          "/opt/apple-vz/vz-helper --id abc\n"
          "/usr/sbin/syslogd\n")
    assert mm.crabbox_vm_count(ps) == 2


def test_detect_leak_true_on_monotonic_decline():
    samples = [
        {"event": "vm_end", "available_gb": 30.0},
        {"event": "vm_start", "available_gb": 22.0},
        {"event": "vm_end", "available_gb": 26.0},
        {"event": "vm_end", "available_gb": 22.0},
    ]
    assert mm.detect_leak(samples, drop_gb=6.0)  # 30 -> 26 -> 22, never recovers


def test_detect_leak_false_when_recovers():
    samples = [
        {"event": "vm_end", "available_gb": 30.0},
        {"event": "vm_end", "available_gb": 24.0},
        {"event": "vm_end", "available_gb": 31.0},  # recovered
    ]
    assert not mm.detect_leak(samples)


def test_detect_leak_false_too_few_samples():
    assert not mm.detect_leak([{"event": "vm_end", "available_gb": 30.0}])


def test_log_sample_roundtrip_and_bounded(tmp_path, monkeypatch):
    led = tmp_path / "mem.jsonl"
    monkeypatch.setattr(mm, "available_gb", lambda *a, **k: 20.0)
    monkeypatch.setattr(mm, "total_gb", lambda: 48.0)
    monkeypatch.setattr(mm, "crabbox_vm_count", lambda *a, **k: 1)
    mm.log_sample("vm_start", "20260629-120000", path=led)
    mm.log_sample("vm_end", "20260629-120500", path=led)
    rows = [__import__("json").loads(x) for x in led.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["event"] == "vm_start" and rows[0]["available_gb"] == 20.0
    assert rows[1]["event"] == "vm_end"
