#!/usr/bin/env python3
"""
pincer host-build lane — runs a build+verify step on the macOS host via LaunchAgent.

Pincer's existing verifier (tools/sandbox_gate.py) runs tests in a Crabbox Apple-VZ VM —
git-only, Go-only. Some builds must run on the **host** with the real toolchain (e.g. a
macOS .app needing Xcode). Critically, such builds hang when run detached via `setsid`
(as tools/durable-job.py does): detaching from the Aqua login session strips
Keychain/mach-service access, so e.g. `swift build` blocks forever in
SecKeychainItemCopyContent while fetching a GitHub artifact.

The fix — and the whole point of this lane — is to run the build **inside the Aqua GUI
session** via a one-shot LaunchAgent (never setsid), which restores Keychain/mach access
while still surviving turn boundaries (launchd owns the process).

This module is opt-in and standalone. It is NOT wired into loop_driver.py or the
orchestrator.

Usage (Python):
    from host_build import host_build, HostBuildConfig
    verdict = host_build("/path/to/repo", HostBuildConfig(
        label="myapp", build_cmd="xcodebuild -scheme MyApp"))
"""
from __future__ import annotations

import dataclasses
import os
import plistlib
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

TAIL_BYTES = 4000


@dataclasses.dataclass(frozen=True)
class HostBuildConfig:
    label: str
    build_cmd: str
    timeout_s: int = 1800
    path_env: str = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
    poll_interval_s: float = 3.0
    prewarm_cmd: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class HostVerdict:
    verdict: str  # "pass" | "fail" | "error"
    exit_code: Optional[int]
    duration_s: float
    log_tail: str
    error_kind: Optional[str]  # "timeout" | "bootstrap_failed" | None


LaunchctlFn = Callable[[list], Tuple[int, str, str]]


def _default_launchctl(argv: list) -> Tuple[int, str, str]:
    proc = subprocess.run(
        ["launchctl"] + argv, capture_output=True, text=True, timeout=60
    )
    return (proc.returncode, proc.stdout, proc.stderr)


def _sanitize_label(label: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", label)


def _tail(data: bytes, n: int = TAIL_BYTES) -> str:
    chunk = data[-n:] if len(data) > n else data
    return chunk.decode("utf-8", errors="replace")


def host_build(
    workdir: str | Path,
    cfg: HostBuildConfig,
    *,
    launchctl: LaunchctlFn = _default_launchctl,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> HostVerdict:
    """Execute a host-side build via a one-shot LaunchAgent inside the Aqua session."""
    workdir = Path(workdir).resolve()
    sanitized = _sanitize_label(cfg.label)
    agent_label = f"ai.pincer.hostbuild.{sanitized}"
    uid = os.getuid()
    domain_target = f"gui/{uid}"
    service_target = f"gui/{uid}/{agent_label}"

    tmpdir = Path(tempfile.mkdtemp(prefix="pincer_hostbuild_"))
    status_file = tmpdir / "status"
    log_file = tmpdir / "build.log"
    launchd_file = tmpdir / "launchd.log"
    wrapper_script = tmpdir / "run.sh"

    # Write wrapper script
    prewarm_block = ""
    if cfg.prewarm_cmd:
        prewarm_block = f"""
# Prewarm step (keychain-safe pre-fetch so the build itself runs offline)
{cfg.prewarm_cmd}
_pw=$?
if [ $_pw -ne 0 ]; then
    echo "FAIL rc=$_pw" > "{status_file}"
    exit $_pw
fi
"""
    wrapper_script.write_text(f"""#!/bin/bash
set -u
cd "{workdir}" || exit 1
{prewarm_block}
echo "RUNNING" > "{status_file}"
{cfg.build_cmd} > "{log_file}" 2>&1
_rc=$?
if [ $_rc -eq 0 ]; then
    echo "DONE" > "{status_file}"
else
    echo "FAIL rc=$_rc" > "{status_file}"
fi
exit $_rc
""")
    wrapper_script.chmod(0o755)

    # Generate LaunchAgent plist
    plist_dir = Path(os.path.expanduser("~/Library/LaunchAgents"))
    plist_path = plist_dir / f"{agent_label}.plist"
    plist_data = {
        "Label": agent_label,
        "ProgramArguments": [str(wrapper_script)],
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": cfg.path_env},
        "StandardOutPath": str(launchd_file),
        "StandardErrorPath": str(launchd_file),
    }
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plistlib.dumps(plist_data))

    started = clock()

    try:
        # Bootout any prior instance (ignore failure)
        launchctl(["bootout", service_target])

        # Bootstrap the agent
        rc, stdout, stderr = launchctl(["bootstrap", domain_target, str(plist_path)])
        if rc != 0:
            return HostVerdict(
                verdict="error",
                exit_code=None,
                duration_s=clock() - started,
                log_tail=f"bootstrap failed: rc={rc} stderr={stderr}",
                error_kind="bootstrap_failed",
            )

        # Poll status file
        while True:
            elapsed = clock() - started
            if elapsed >= cfg.timeout_s:
                # Timeout — clean up and report
                launchctl(["bootout", service_target])
                log_tail = _read_log_tail(log_file)
                return HostVerdict(
                    verdict="error",
                    exit_code=None,
                    duration_s=elapsed,
                    log_tail=log_tail,
                    error_kind="timeout",
                )

            content = _read_status(status_file)
            if content == "DONE":
                log_tail = _read_log_tail(log_file)
                return HostVerdict(
                    verdict="pass",
                    exit_code=0,
                    duration_s=clock() - started,
                    log_tail=log_tail,
                    error_kind=None,
                )
            match = re.match(r"FAIL rc=(\d+)", content) if content else None
            if match:
                exit_code = int(match.group(1))
                log_tail = _read_log_tail(log_file)
                return HostVerdict(
                    verdict="fail",
                    exit_code=exit_code,
                    duration_s=clock() - started,
                    log_tail=log_tail,
                    error_kind=None,
                )
            sleep(cfg.poll_interval_s)
    finally:
        # Always clean up: bootout + remove plist
        launchctl(["bootout", service_target])
        try:
            plist_path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_status(path: Path) -> str:
    try:
        return path.read_text().strip()
    except (FileNotFoundError, OSError):
        return ""


def _read_log_tail(path: Path) -> str:
    try:
        data = path.read_bytes()
        return _tail(data)
    except (FileNotFoundError, OSError):
        return ""
