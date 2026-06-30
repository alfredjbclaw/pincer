#!/usr/bin/env python3
"""Memory-aware sandbox concurrency gate in the orchestrator."""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import parallel_orchestrator as po


def test_init_gate_reads_config_and_sizes_semaphore(monkeypatch, tmp_path):
    cfg = tmp_path / "pincer.toml"
    cfg.write_text("[sandbox]\nmax_concurrent = 3\nmin_free_gb = 10\nvm_memory_gb = 8\n")
    monkeypatch.setenv("PINCER_CONFIG", str(cfg))
    po._init_sandbox_gate()
    assert po._SANDBOX_GATE["max_concurrent"] == 3
    assert po._SANDBOX_GATE["min_free_gb"] == 10.0

    # semaphore allows exactly 3 concurrent holders
    acquired = [po._SANDBOX_SEM.acquire(blocking=False) for _ in range(4)]
    assert acquired == [True, True, True, False]
    for ok in acquired:
        if ok:
            po._SANDBOX_SEM.release()


def test_default_is_serial_safe(monkeypatch, tmp_path):
    # No config -> conservative default (behaves like the old single lock at 1,
    # or whatever the example ships) and never crashes.
    monkeypatch.setenv("PINCER_CONFIG", str(tmp_path / "absent.toml"))
    po._init_sandbox_gate()
    assert po._SANDBOX_GATE["max_concurrent"] >= 1


def test_sandbox_slot_yields_and_logs_start_end(monkeypatch):
    po._SANDBOX_SEM = threading.BoundedSemaphore(1)
    po._SANDBOX_GATE = {"max_concurrent": 1, "min_free_gb": 12.0,
                        "vm_memory_gb": 8.0, "max_wait_s": 600}
    monkeypatch.setattr(po.mm, "can_start_vm", lambda *a, **k: (True, "ok"))
    events = []
    monkeypatch.setattr(po.mm, "log_sample", lambda ev, ts, **k: events.append(ev))
    with po.sandbox_slot():
        events.append("body")
    assert events == ["vm_start", "body", "vm_end"]


def test_sandbox_slot_proceeds_after_wait_timeout(monkeypatch):
    # RAM never frees, but max_wait_s=0 -> proceed immediately (no hang), warn.
    po._SANDBOX_SEM = threading.BoundedSemaphore(1)
    po._SANDBOX_GATE = {"max_concurrent": 1, "min_free_gb": 99.0,
                        "vm_memory_gb": 8.0, "max_wait_s": 0}
    monkeypatch.setattr(po.mm, "can_start_vm", lambda *a, **k: (False, "tight"))
    monkeypatch.setattr(po.mm, "log_sample", lambda *a, **k: None)
    warned = []
    monkeypatch.setattr(po, "alert", lambda msg, level="progress": warned.append(msg))
    with po.sandbox_slot():
        pass
    assert any("RAM gate" in w for w in warned)  # surfaced the caution


def test_sandbox_slot_releases_on_exception(monkeypatch):
    po._SANDBOX_SEM = threading.BoundedSemaphore(1)
    po._SANDBOX_GATE = {"max_concurrent": 1, "min_free_gb": 12.0,
                        "vm_memory_gb": 8.0, "max_wait_s": 600}
    monkeypatch.setattr(po.mm, "can_start_vm", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(po.mm, "log_sample", lambda *a, **k: None)
    try:
        with po.sandbox_slot():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # slot must have been released despite the exception
    assert po._SANDBOX_SEM.acquire(blocking=False) is True
    po._SANDBOX_SEM.release()
