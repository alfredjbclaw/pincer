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


def test_log_sample_does_not_chmod_existing_override_parent(tmp_path, monkeypatch):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    ledger = parent / "mem.jsonl"
    monkeypatch.setattr(mm, "available_gb", lambda *a, **k: 20.0)
    monkeypatch.setattr(mm, "total_gb", lambda: 48.0)
    monkeypatch.setattr(mm, "crabbox_vm_count", lambda *a, **k: 0)

    assert mm.log_sample("vm_start", "20260718", path=ledger)

    assert parent.stat().st_mode & 0o777 == 0o755
    assert ledger.stat().st_mode & 0o777 == 0o600


def test_log_sample_completes_short_writes(tmp_path, monkeypatch):
    ledger = tmp_path / "mem.jsonl"
    monkeypatch.setattr(mm, "sample", lambda: {"available_gb": 20.0})
    real_write = mm.os.write

    def short_write(fd, payload):
        return real_write(fd, payload[: max(1, len(payload) // 2)])

    monkeypatch.setattr(mm.os, "write", short_write)
    assert mm.log_sample("vm_start", "20260718", path=ledger)
    assert __import__("json").loads(ledger.read_text())["event"] == "vm_start"


def test_log_sample_keeps_existing_ledger_when_rewrite_fails(tmp_path, monkeypatch):
    ledger = tmp_path / "mem.jsonl"
    ledger.write_text('{"event":"existing"}\n')
    original = ledger.read_bytes()
    real_write = mm.os.write
    calls = 0

    def fail_after_partial(fd, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, payload[: max(1, len(payload) // 2)])
        raise OSError("fixture write failure")

    monkeypatch.setattr(mm, "sample", lambda: {"available_gb": 20.0})
    monkeypatch.setattr(mm.os, "write", fail_after_partial)
    assert not mm.log_sample("vm_end", "20260718", path=ledger)
    assert ledger.read_bytes() == original
    assert list(tmp_path.glob(".mem.jsonl.*")) == [tmp_path / ".mem.jsonl.lock"]


def test_log_sample_refuses_oversize_existing_ledger_without_modifying_it(tmp_path, monkeypatch):
    ledger = tmp_path / "mem.jsonl"
    ledger.write_bytes(b"x" * 64)
    original = ledger.read_bytes()
    monkeypatch.setattr(mm, "_SAMPLES_MAX_BYTES", 32)
    monkeypatch.setattr(mm, "sample", lambda: {"available_gb": 20.0})

    assert not mm.log_sample("vm_end", "20260718", path=ledger)
    assert ledger.read_bytes() == original


def test_log_sample_cleanup_failure_does_not_escape_after_commit(tmp_path, monkeypatch):
    ledger = tmp_path / "mem.jsonl"
    monkeypatch.setattr(mm, "sample", lambda: {"available_gb": 20.0})
    real_flock = mm.fcntl.flock

    def fail_unlock(fd, operation):
        if operation == mm.fcntl.LOCK_UN:
            raise OSError("fixture unlock failure")
        return real_flock(fd, operation)

    monkeypatch.setattr(mm.fcntl, "flock", fail_unlock)

    assert mm.log_sample("vm_start", "20260718", path=ledger)
    assert __import__("json").loads(ledger.read_text())["event"] == "vm_start"


def test_log_sample_fsyncs_file_and_parent_directory(tmp_path, monkeypatch):
    ledger = tmp_path / "mem.jsonl"
    monkeypatch.setattr(mm, "sample", lambda: {"available_gb": 20.0})
    real_fsync = mm.os.fsync
    fsynced_modes = []

    def observe_fsync(fd):
        fsynced_modes.append(mm.os.fstat(fd).st_mode)
        return real_fsync(fd)

    monkeypatch.setattr(mm.os, "fsync", observe_fsync)

    assert mm.log_sample("vm_start", "20260718", path=ledger)
    stat_module = __import__("stat")
    assert any(stat_module.S_ISREG(mode) for mode in fsynced_modes)
    assert any(stat_module.S_ISDIR(mode) for mode in fsynced_modes)
