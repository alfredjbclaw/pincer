#!/usr/bin/env python3
"""Warm-box provisioning: install the toolchain onto a reusable crabbox box
once, then run every later fix-round bare (no apt prelude). This removes the
~2m40s per-round toolchain reinstall that dominated multi-round builds."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import sandbox_gate as sg  # noqa: E402


def _fake_crabbox(record: list, *, returncode: int = 0, stdout: str = "", stderr: str = ""):
    def run(cmd, **kwargs):
        record.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
    return run


def _isolate_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sg, "PROVISION_STATE_PATH", tmp_path / "provisioned-boxes.json")
    monkeypatch.setattr(sg.shutil, "which", lambda name: "/usr/bin/crabbox")


def test_first_run_provisions_then_runs_bare_and_records(monkeypatch, tmp_path) -> None:
    # Given: a warm box that has never been provisioned.
    _isolate_state(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(sg.subprocess, "run", _fake_crabbox(calls, stdout="1 passed"))
    cfg = sg.SandboxConfig(box_id="blue-lobster")

    # When: the gate runs a node build on it.
    v = sg.gate(workdir=tmp_path, test_command="npm test", config=cfg, toolchain=["node"])

    # Then: exactly two crabbox invocations — a provision, then a BARE test run.
    assert v.verdict == "pass"
    assert len(calls) == 2
    provision, test_run = calls
    assert provision[:4] == ["crabbox", "run", "--id", "blue-lobster"]
    assert "apt-get" in " ".join(provision)          # provision installed the toolchain
    assert test_run[:4] == ["crabbox", "run", "--id", "blue-lobster"]
    assert "apt-get" not in " ".join(test_run)        # the test run carries NO apt prelude
    # And the box is now recorded as having nodejs+npm.
    assert set(sg._load_provision_state()["blue-lobster"]) >= {"nodejs", "npm"}


def test_second_run_skips_provision_entirely(monkeypatch, tmp_path) -> None:
    # Given: the box already has the node toolchain recorded.
    _isolate_state(monkeypatch, tmp_path)
    sg._record_provisioned("blue-lobster", ["nodejs", "npm"])
    calls: list[list[str]] = []
    monkeypatch.setattr(sg.subprocess, "run", _fake_crabbox(calls, stdout="1 passed"))
    cfg = sg.SandboxConfig(box_id="blue-lobster")

    # When: a later fix-round runs the same node build.
    v = sg.gate(workdir=tmp_path, test_command="npm test", config=cfg, toolchain=["node"])

    # Then: a single bare test run, no provision, no apt cost.
    assert v.verdict == "pass"
    assert len(calls) == 1
    assert "apt-get" not in " ".join(calls[0])


def test_failed_provision_never_records_and_surfaces_error(monkeypatch, tmp_path) -> None:
    # Given: the apt provision step exits nonzero (e.g. mirror hiccup).
    _isolate_state(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(sg.subprocess, "run",
                        _fake_crabbox(calls, returncode=100, stderr="E: Unable to fetch"))
    cfg = sg.SandboxConfig(box_id="blue-lobster")

    # When: the gate runs.
    v = sg.gate(workdir=tmp_path, test_command="npm test", config=cfg, toolchain=["node"])

    # Then: it stops at provision (no bare run), reports a provision error, and
    # records nothing — so the next attempt reprovisions rather than assuming.
    assert v.verdict == "error" and v.error_kind == "provision_failed"
    assert len(calls) == 1
    assert "blue-lobster" not in sg._load_provision_state()


def test_no_box_id_keeps_legacy_prelude_path(monkeypatch, tmp_path) -> None:
    # Given: no warm box configured.
    _isolate_state(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(sg.subprocess, "run", _fake_crabbox(calls, stdout="1 passed"))
    cfg = sg.SandboxConfig(box_id=None)

    # When: the gate runs a node build.
    v = sg.gate(workdir=tmp_path, test_command="npm test", config=cfg, toolchain=["node"])

    # Then: one fresh-lease run with the apt prelude inlined (unchanged behavior).
    assert v.verdict == "pass"
    assert len(calls) == 1
    joined = " ".join(calls[0])
    assert "--provider" in joined and "apt-get" in joined and "--id" not in joined
