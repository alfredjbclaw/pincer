"""Tests for tools/host_build.py — fast, offline, no real launchctl."""
from __future__ import annotations

import plistlib
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from host_build import HostBuildConfig, HostVerdict, host_build  # noqa: E402


class FakeLaunchctl:
    """Records launchctl calls and returns scripted responses."""

    def __init__(self, bootstrap_rc: int = 0):
        self.calls: List[Tuple[str, list]] = []
        self._bootstrap_rc = bootstrap_rc

    def __call__(self, argv: list) -> Tuple[int, str, str]:
        cmd = argv[0] if argv else ""
        self.calls.append((cmd, argv))
        if cmd == "bootstrap":
            return (self._bootstrap_rc, "", "" if self._bootstrap_rc == 0 else "failed")
        # bootout always succeeds
        return (0, "", "")

    @property
    def bootout_calls(self) -> list:
        return [(cmd, args) for cmd, args in self.calls if cmd == "bootout"]


def _make_cfg(**overrides) -> HostBuildConfig:
    defaults = dict(label="test-build", build_cmd="make all", timeout_s=30, poll_interval_s=0.0)
    defaults.update(overrides)
    return HostBuildConfig(**defaults)


def _fake_clock(ticks: list):
    """Return a clock that yields successive values from ticks."""
    it = iter(ticks)
    return lambda: next(it)


def _noop_sleep(_seconds: float) -> None:
    pass


class TestHostBuildPass:
    def test_pass_path(self, tmp_path: Path):
        """Status file transitions RUNNING → DONE; verdict is pass with exit_code 0."""
        cfg = _make_cfg()
        status_written = []

        def fake_launchctl(argv: list) -> Tuple[int, str, str]:
            cmd = argv[0] if argv else ""
            if cmd == "bootstrap":
                # Simulate: after bootstrap, status file is written by wrapper
                # Find status file in the wrapper script
                _write_status_in_workdir(tmp_path, "DONE")
            return (0, "", "")

        # Pre-write status as DONE (simulating the wrapper completed instantly)
        _write_status_in_workdir(tmp_path, "DONE")

        clock_ticks = [0.0, 0.1, 0.2, 0.3]
        verdict = host_build(
            tmp_path, cfg,
            launchctl=fake_launchctl,
            clock=_fake_clock(clock_ticks),
            sleep=_noop_sleep,
        )
        assert verdict.verdict == "pass"
        assert verdict.exit_code == 0
        assert verdict.error_kind is None

    def test_pass_with_log_tail(self, tmp_path: Path, monkeypatch):
        """Log tail is read from the build log file."""
        cfg = _make_cfg()

        call_count = [0]

        def fake_launchctl(argv: list) -> Tuple[int, str, str]:
            call_count[0] += 1
            return (0, "", "")

        _write_status_in_workdir(tmp_path, "DONE")

        verdict = host_build(
            tmp_path, cfg,
            launchctl=fake_launchctl,
            clock=_fake_clock([0.0, 0.1, 0.2, 0.3]),
            sleep=_noop_sleep,
        )
        assert verdict.verdict == "pass"
        assert isinstance(verdict.log_tail, str)


class TestHostBuildFail:
    def test_fail_path(self, tmp_path: Path):
        """Status file reads FAIL rc=2; verdict is fail with exit_code 2."""
        cfg = _make_cfg()
        _write_status_in_workdir(tmp_path, "FAIL rc=2")

        verdict = host_build(
            tmp_path, cfg,
            launchctl=FakeLaunchctl(),
            clock=_fake_clock([0.0, 0.1, 0.2, 0.3]),
            sleep=_noop_sleep,
        )
        assert verdict.verdict == "fail"
        assert verdict.exit_code == 2
        assert verdict.error_kind is None


class TestHostBuildBootstrapFailure:
    def test_bootstrap_error(self, tmp_path: Path):
        """If launchctl bootstrap returns non-zero, verdict is error/bootstrap_failed."""
        cfg = _make_cfg()
        fake_lctl = FakeLaunchctl(bootstrap_rc=1)

        verdict = host_build(
            tmp_path, cfg,
            launchctl=fake_lctl,
            clock=_fake_clock([0.0, 0.5]),
            sleep=_noop_sleep,
        )
        assert verdict.verdict == "error"
        assert verdict.error_kind == "bootstrap_failed"
        assert verdict.exit_code is None
        assert "bootstrap failed" in verdict.log_tail


class TestHostBuildTimeout:
    def test_timeout_triggers_bootout_and_error(self, tmp_path: Path):
        """When status never becomes terminal, timeout fires and cleanup bootout is called."""
        cfg = _make_cfg(timeout_s=10, poll_interval_s=0.0)
        fake_lctl = FakeLaunchctl()

        # Status file never written → stays empty → never terminal
        # Clock jumps past timeout on third read
        clock_ticks = [0.0, 0.0, 5.0, 11.0, 11.0, 11.0]

        verdict = host_build(
            tmp_path, cfg,
            launchctl=fake_lctl,
            clock=_fake_clock(clock_ticks),
            sleep=_noop_sleep,
        )
        assert verdict.verdict == "error"
        assert verdict.error_kind == "timeout"
        # Cleanup bootout must have been called (both inline timeout + finally)
        assert len(fake_lctl.bootout_calls) >= 2


class TestPlistGeneration:
    def test_plist_contains_expected_keys(self, tmp_path: Path, monkeypatch):
        """Generated plist has RunAtLoad, correct Label, no setsid."""
        cfg = _make_cfg(label="xcode-app")
        plist_contents = []

        def capturing_launchctl(argv: list) -> Tuple[int, str, str]:
            cmd = argv[0] if argv else ""
            if cmd == "bootstrap" and len(argv) >= 3:
                plist_path = Path(argv[2])
                if plist_path.exists():
                    plist_contents.append(plist_path.read_bytes())
            return (0, "", "")

        _write_status_in_workdir(tmp_path, "DONE")

        host_build(
            tmp_path, cfg,
            launchctl=capturing_launchctl,
            clock=_fake_clock([0.0, 0.1, 0.2, 0.3]),
            sleep=_noop_sleep,
        )

        assert plist_contents, "plist was never captured"
        plist = plistlib.loads(plist_contents[0])
        assert plist["RunAtLoad"] is True
        assert "ai.pincer.hostbuild.xcode-app" == plist["Label"]
        # Must not contain setsid anywhere in program arguments
        all_args = " ".join(plist.get("ProgramArguments", []))
        assert "setsid" not in all_args
        # The wrapper script itself must not contain setsid
        wrapper_path = Path(plist["ProgramArguments"][0])
        if wrapper_path.exists():
            assert "setsid" not in wrapper_path.read_text()


class TestCleanup:
    def test_bootout_called_on_success(self, tmp_path: Path):
        """Cleanup bootout is always called even on success."""
        cfg = _make_cfg()
        fake_lctl = FakeLaunchctl()
        _write_status_in_workdir(tmp_path, "DONE")

        host_build(
            tmp_path, cfg,
            launchctl=fake_lctl,
            clock=_fake_clock([0.0, 0.1, 0.2, 0.3]),
            sleep=_noop_sleep,
        )
        # At minimum: initial bootout (prior instance) + finally bootout
        assert len(fake_lctl.bootout_calls) >= 2

    def test_real_launchctl_never_invoked(self, tmp_path: Path):
        """The fake launchctl is what gets called, not the real binary."""
        cfg = _make_cfg()
        fake_lctl = FakeLaunchctl()
        _write_status_in_workdir(tmp_path, "DONE")

        host_build(
            tmp_path, cfg,
            launchctl=fake_lctl,
            clock=_fake_clock([0.0, 0.1, 0.2, 0.3]),
            sleep=_noop_sleep,
        )
        # If the real launchctl were called, calls list would be empty
        assert len(fake_lctl.calls) > 0


# ─── helpers ────────────────────────────────────────────────────────────────


def _write_status_in_workdir(tmp_path: Path, content: str) -> None:
    """Pre-write a status file where host_build will look for it.

    host_build creates its own tmpdir for the status file, so we monkeypatch
    tempfile.mkdtemp to return our controlled path.
    """
    # We exploit the fact that host_build uses tempfile.mkdtemp — we patch it
    # at import time would be complex; instead we patch the module-level reference.
    import host_build as hb
    import tempfile as _tf

    _original_mkdtemp = _tf.mkdtemp

    def _patched_mkdtemp(*args, **kwargs):
        # Return tmp_path on first call, restore after
        _tf.mkdtemp = _original_mkdtemp
        status = tmp_path / "status"
        status.write_text(content)
        return str(tmp_path)

    _tf.mkdtemp = _patched_mkdtemp
