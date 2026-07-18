#!/usr/bin/env python3
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import run_ledger as rl


def _row(**kw):
    base = {"merged": [], "prd": [], "infra_failures": [], "result": "done"}
    base.update(kw)
    return base


def test_classify():
    assert rl.classify(_row(merged=[5])) == "shipped"
    assert rl.classify(_row(prd=[7])) == "shipped"
    assert rl.classify(_row(infra_failures=[5, 7])) == "infra"
    assert rl.classify(_row(result="held_budget")) == "held"
    assert rl.classify(_row(result="halted_usage")) == "held"
    assert rl.classify(_row()) == "no_fix"  # ran, found nothing, no infra problem


def test_consecutive_infra_counts_trailing():
    rows = [_row(merged=[1]), _row(infra_failures=[1]), _row(infra_failures=[1])]
    assert rl.consecutive_infra_failures(rows) == 2


def test_shipped_resets_streak():
    rows = [_row(infra_failures=[1]), _row(merged=[2]), _row(infra_failures=[1])]
    assert rl.consecutive_infra_failures(rows) == 1  # only the trailing one


def test_no_fix_resets_streak():
    # a 'no fix found' run proves the engine works -> not a broken-env streak
    rows = [_row(infra_failures=[1]), _row(infra_failures=[1]), _row()]
    assert rl.consecutive_infra_failures(rows) == 0


def test_held_is_neutral():
    # a budget-held run neither counts nor breaks the streak
    rows = [_row(infra_failures=[1]), _row(result="held_budget"), _row(infra_failures=[1])]
    assert rl.consecutive_infra_failures(rows) == 2


def test_should_pause_threshold():
    three = [_row(infra_failures=[1]) for _ in range(3)]
    assert rl.should_pause(three, threshold=3)
    assert not rl.should_pause(three[:2], threshold=3)


def test_record_and_read_roundtrip(tmp_path, monkeypatch):
    led = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("PINCER_RUN_LEDGER", str(led))
    rl.record("sqlmeta", "o/r", "done",
              {"merged": [5], "prd": [], "infra_failures": []}, "20260629-120000")
    rl.record("sqlmeta", "o/r", "done",
              {"merged": [], "prd": [], "infra_failures": [7]}, "20260629-180000")
    rl.record("other", "o/x", "done", {"merged": [1]}, "20260629-190000")

    rows = rl.read("sqlmeta")
    assert len(rows) == 2  # filtered to the named loop
    assert rl.classify(rows[0]) == "shipped"
    assert rl.classify(rows[1]) == "infra"
    assert len(rl.read()) == 3  # unfiltered


def test_read_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("PINCER_RUN_LEDGER", str(tmp_path / "nope.jsonl"))
    assert rl.read("x") == []


def test_record_does_not_chmod_existing_override_parent(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    ledger = parent / "run.jsonl"

    assert rl.record("loop", "owner/repo", "done", {}, "20260718", path=ledger)

    assert parent.stat().st_mode & 0o777 == 0o755
    assert ledger.stat().st_mode & 0o777 == 0o600


def test_record_fsyncs_file_and_parent_directory(tmp_path, monkeypatch):
    ledger = tmp_path / "run.jsonl"
    real_fsync = rl.os.fsync
    fsynced_modes = []

    def observe_fsync(fd):
        fsynced_modes.append(rl.os.fstat(fd).st_mode)
        return real_fsync(fd)

    monkeypatch.setattr(rl.os, "fsync", observe_fsync)

    assert rl.record("loop", "owner/repo", "done", {}, "20260718", path=ledger)
    stat_module = __import__("stat")
    assert any(stat_module.S_ISREG(mode) for mode in fsynced_modes)
    assert any(stat_module.S_ISDIR(mode) for mode in fsynced_modes)


def test_record_completes_short_os_writes(tmp_path, monkeypatch):
    ledger = tmp_path / "run.jsonl"
    real_write = rl.os.write

    def short_write(fd, payload):
        return real_write(fd, payload[: max(1, len(payload) // 2)])

    monkeypatch.setattr(rl.os, "write", short_write)
    assert rl.record("loop", "owner/repo", "done", {}, "20260718", path=ledger)
    assert json.loads(ledger.read_text())["name"] == "loop"


def test_record_rolls_back_partial_row_on_write_failure(tmp_path, monkeypatch):
    ledger = tmp_path / "run.jsonl"
    ledger.write_text('{"name":"existing"}\n')
    original = ledger.read_bytes()
    real_write = rl.os.write
    calls = 0

    def fail_after_partial(fd, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, payload[: max(1, len(payload) // 2)])
        raise OSError("fixture write failure")

    monkeypatch.setattr(rl.os, "write", fail_after_partial)
    assert not rl.record("loop", "owner/repo", "done", {}, "20260718", path=ledger)
    assert ledger.read_bytes() == original
