#!/usr/bin/env python3
"""
pincer sandbox gate — runs the repo's test command on a clean Crabbox VM.

The orchestrator calls this between AGENT and PR open. Green verdict → PR may
open. Red verdict → return failure to the worker for another loop iteration.

Returns a SandboxVerdict with: verdict (pass|fail|error), exit_code, duration,
stdout tail, stderr tail, provider used. Errors (Crabbox unavailable, broker
unreachable) are distinct from test failures.

Usage (Python):
    from sandbox_gate import gate, SandboxConfig
    verdict = gate(workdir="/path/to/repo",
                   test_command="make test",
                   config=SandboxConfig(provider="applevz"))
    if verdict.verdict == "pass":
        ...

Usage (CLI):
    python3 tools/sandbox_gate.py --workdir /path/to/repo --test "make test" \\
        [--provider applevz] [--timeout 1800]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

PINCER_CONFIG_DEFAULT = Path.home() / ".openclaw" / "pincer.toml"
TAIL_BYTES = 4000


@dataclasses.dataclass(frozen=True)
class SandboxConfig:
    provider: str = "applevz"
    timeout_seconds: int = 1800
    default_test: str = "make test"

    @classmethod
    def from_pincer_toml(cls, path: Optional[Path] = None) -> "SandboxConfig":
        cfg_path = path or Path(os.environ.get("PINCER_CONFIG", PINCER_CONFIG_DEFAULT))
        if not cfg_path.exists():
            return cls()
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore
            except ImportError:
                return cls()
        data = tomllib.loads(cfg_path.read_text())
        sandbox = data.get("sandbox", {})
        return cls(
            provider=sandbox.get("provider", "applevz"),
            timeout_seconds=int(sandbox.get("timeout_seconds", 1800)),
            default_test=sandbox.get("default_test", "make test"),
        )


@dataclasses.dataclass
class SandboxVerdict:
    verdict: str           # "pass" | "fail" | "error"
    exit_code: int
    duration_seconds: float
    provider: str
    test_command: str
    stdout_tail: str
    stderr_tail: str
    error_kind: Optional[str]  # only set when verdict=="error"

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _tail(s: str, n: int = TAIL_BYTES) -> str:
    if len(s) <= n:
        return s
    return "...[truncated]...\n" + s[-n:]


def gate(
    workdir: Path | str,
    test_command: Optional[str] = None,
    config: Optional[SandboxConfig] = None,
) -> SandboxVerdict:
    """Run `crabbox run --provider <p> -- <test>` against workdir, return verdict."""
    cfg = config or SandboxConfig.from_pincer_toml()
    workdir = Path(workdir).resolve()
    test_command = test_command or cfg.default_test
    started = time.monotonic()

    if shutil.which("crabbox") is None:
        return SandboxVerdict(
            verdict="error",
            exit_code=127,
            duration_seconds=0,
            provider=cfg.provider,
            test_command=test_command,
            stdout_tail="",
            stderr_tail="crabbox not found on PATH; install with `brew install openclaw/tap/crabbox`",
            error_kind="crabbox_not_installed",
        )

    if not workdir.is_dir():
        return SandboxVerdict(
            verdict="error",
            exit_code=64,
            duration_seconds=0,
            provider=cfg.provider,
            test_command=test_command,
            stdout_tail="",
            stderr_tail=f"workdir not a directory: {workdir}",
            error_kind="workdir_invalid",
        )

    # `crabbox run --provider <p> -- <test_command>` from within the working tree.
    cmd = ["crabbox", "run", "--provider", cfg.provider, "--"] + shlex.split(test_command)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=cfg.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return SandboxVerdict(
            verdict="error",
            exit_code=124,
            duration_seconds=time.monotonic() - started,
            provider=cfg.provider,
            test_command=test_command,
            stdout_tail=_tail((exc.stdout or b"").decode() if exc.stdout else ""),
            stderr_tail=f"crabbox run timed out after {cfg.timeout_seconds}s",
            error_kind="timeout",
        )

    duration = time.monotonic() - started
    stdout_tail = _tail(proc.stdout)
    stderr_tail = _tail(proc.stderr)

    if proc.returncode == 0:
        return SandboxVerdict(
            verdict="pass",
            exit_code=0,
            duration_seconds=duration,
            provider=cfg.provider,
            test_command=test_command,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            error_kind=None,
        )

    # Distinguish "test failed" (verdict=fail) from "crabbox itself failed"
    # (verdict=error). crabbox exits non-zero when the broker / lease / sync
    # itself fails. We detect this by looking for a "test command exited" line
    # in stdout/stderr — if absent, the failure was infra, not the test.
    looks_like_test_ran = any(
        marker in (proc.stdout + proc.stderr)
        for marker in ("command exited with status", "exit code", "test failed", "FAILED")
    )
    if looks_like_test_ran:
        return SandboxVerdict(
            verdict="fail",
            exit_code=proc.returncode,
            duration_seconds=duration,
            provider=cfg.provider,
            test_command=test_command,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            error_kind=None,
        )

    return SandboxVerdict(
        verdict="error",
        exit_code=proc.returncode,
        duration_seconds=duration,
        provider=cfg.provider,
        test_command=test_command,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        error_kind="crabbox_infra_failure",
    )


def _cli() -> int:
    parser = argparse.ArgumentParser(description="pincer sandbox gate")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--test", default=None, help="test command (defaults to config.default_test)")
    parser.add_argument("--provider", default=None, help="crabbox provider override")
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = SandboxConfig.from_pincer_toml()
    overrides = {}
    if args.provider:
        overrides["provider"] = args.provider
    if args.timeout:
        overrides["timeout_seconds"] = args.timeout
    cfg = dataclasses.replace(cfg, **overrides)

    verdict = gate(workdir=args.workdir, test_command=args.test, config=cfg)
    if args.json:
        print(json.dumps(verdict.to_dict(), indent=2))
    else:
        print(f"verdict:  {verdict.verdict.upper()}")
        print(f"provider: {verdict.provider}")
        print(f"test:     {verdict.test_command}")
        print(f"exit:     {verdict.exit_code}")
        print(f"duration: {verdict.duration_seconds:.1f}s")
        if verdict.error_kind:
            print(f"error:    {verdict.error_kind}")
        if verdict.stderr_tail:
            print("--- stderr tail ---")
            print(verdict.stderr_tail)
    return 0 if verdict.verdict == "pass" else 1


if __name__ == "__main__":
    sys.exit(_cli())
