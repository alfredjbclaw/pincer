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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_results as tr
import toolchain as tc

PINCER_CONFIG_DEFAULT = Path.home() / ".openclaw" / "pincer.toml"
TAIL_BYTES = 4000

# Host-side record of which apt packages a *reusable* crabbox box already has,
# keyed by box id. A warm box keeps its installed toolchain between runs, so
# after a one-time provision we run the bare test command with no apt prelude —
# removing the ~2m40s apt reinstall from every fix-round (rounds 2..N). The
# argv contract forbids `||`/`command -v` guards, so this skip decision must
# live host-side, not in the in-VM prelude.
PROVISION_STATE_PATH = Path.home() / ".openclaw" / "pincer" / "provisioned-boxes.json"


def _load_provision_state() -> dict:
    try:
        return json.loads(PROVISION_STATE_PATH.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _record_provisioned(box_id: str, pkgs: list) -> None:
    """Merge `pkgs` into the recorded install set for `box_id` (best-effort)."""
    state = _load_provision_state()
    have = set(state.get(box_id, []))
    have.update(pkgs)
    state[box_id] = sorted(have)
    try:
        PROVISION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROVISION_STATE_PATH.write_text(json.dumps(state, indent=2))
    except OSError:
        pass  # best-effort cache; a miss just means we reprovision next time


def _missing_packages(box_id: str, pkgs: list) -> list:
    """apt packages `box_id` has not been recorded as having, order-preserving."""
    have = set(_load_provision_state().get(box_id, []))
    return [p for p in pkgs if p not in have]


def reap_stale_leases(provider: str) -> int:
    """Stop any pre-existing crabbox leases for `provider` before a fresh run.

    A run that is SIGTERM'd/SIGKILL'd (wrapping timeout, OOM/jetsam) is killed
    before crabbox's graceful auto-release fires, orphaning its VM. On Apple VZ
    each orphan holds 8 GiB; stacking a few reproduces the memory-pressure
    jetsam that then kills the next run. Reaping stale leases up front breaks
    that loop. Safe under the `max_concurrent=1` policy. Returns count reaped.
    """
    import re
    if shutil.which("crabbox") is None:
        return 0
    try:
        out = subprocess.run(
            ["crabbox", "list", "--provider", provider],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return 0
    reaped = 0
    for slug in re.findall(r"slug=([A-Za-z0-9-]+)", out):
        try:
            subprocess.run(
                ["crabbox", "stop", "--provider", provider,
                 "--target", "linux", "--id", slug],
                capture_output=True, text=True, timeout=60,
            )
            reaped += 1
        except Exception:
            pass
    return reaped


@dataclasses.dataclass(frozen=True)
class SandboxConfig:
    provider: str = "applevz"
    timeout_seconds: int = 1800
    default_test: str = "make test"
    # Reusable crabbox box slug (from `crabbox warmup`/`prewarm`). When set, the
    # toolchain is provisioned onto it once and every later run reuses it with
    # no apt prelude. None -> a fresh ephemeral lease per run (legacy behavior).
    box_id: Optional[str] = None

    @classmethod
    def from_pincer_toml(cls, path: Optional[Path] = None) -> "SandboxConfig":
        cfg_path = path or Path(os.environ.get("PINCER_CONFIG", PINCER_CONFIG_DEFAULT))
        env_box = os.environ.get("PINCER_SANDBOX_BOX_ID") or None
        if not cfg_path.exists():
            return cls(box_id=env_box)
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore
            except ImportError:
                return cls(box_id=env_box)
        data = tomllib.loads(cfg_path.read_text())
        sandbox = data.get("sandbox", {})
        return cls(
            provider=sandbox.get("provider", "applevz"),
            timeout_seconds=int(sandbox.get("timeout_seconds", 1800)),
            default_test=sandbox.get("default_test", "make test"),
            # env override wins so a build session can pin a warm box without
            # editing config.
            box_id=env_box or (sandbox.get("box_id") or None),
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
    # Structured test counts parsed from the run, for regression-aware ranking
    # in the selection cascade. parsed=False on infra errors / non-pytest output.
    results: Optional["tr.TestResults"] = None

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["results"] = self.results.to_dict() if self.results is not None else None
        return d


def _tail(s: str, n: int = TAIL_BYTES) -> str:
    if len(s) <= n:
        return s
    return "...[truncated]...\n" + s[-n:]


def provision_box(box_id: str, packages: list, cfg: SandboxConfig) -> tuple[bool, str]:
    """Install `packages` onto a reusable crabbox box once, recording success.

    Runs the apt prelude as its own `crabbox run --id <box_id>` so success is
    unambiguous (apt chain exit 0) — unlike folding apt into the test command,
    where a red test verdict can't be told apart from a failed install. Only a
    clean exit records the packages as present. Returns (ok, detail).
    """
    prelude = tc.prelude_for_packages(packages)
    if not prelude:
        return (True, "no packages to provision")
    cmd = ["crabbox", "run", "--id", box_id, "--"] + shlex.split(prelude)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.timeout_seconds)
    except subprocess.TimeoutExpired:
        return (False, f"provision timed out after {cfg.timeout_seconds}s")
    except Exception as exc:  # crabbox missing / broker error
        return (False, f"provision failed to launch: {exc}")
    if proc.returncode == 0:
        _record_provisioned(box_id, packages)
        return (True, "provisioned")
    return (False, _tail(proc.stderr or proc.stdout))


def gate(
    workdir: Path | str,
    test_command: Optional[str] = None,
    config: Optional[SandboxConfig] = None,
    toolchain: Optional[list] = None,
    reap_stale: bool = False,
) -> SandboxVerdict:
    """Run `crabbox run --provider <p> -- <test>` against workdir, return verdict.

    `toolchain`: optional list of language/tool names (or raw apt packages); an
    apt-only install prelude is prepended to `test_command` so the VM has the
    right runtime for ANY language (node/go/python/rust/...). See toolchain.py.
    `reap_stale`: stop orphaned leases for the provider before running (prevents
    Apple VZ VM stacking from killed prior runs).
    """
    cfg = config or SandboxConfig.from_pincer_toml()
    workdir = Path(workdir).resolve()
    test_command = test_command or cfg.default_test
    started = time.monotonic()

    if reap_stale:
        reap_stale_leases(cfg.provider)

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

    if cfg.box_id:
        # Warm-box path: provision only the packages this box still lacks (a
        # one-time apt cost the first fix-round pays), then run the bare test
        # command on the reused box every round after — no apt prelude at all.
        pkgs = tc.resolve_packages(toolchain)
        missing = _missing_packages(cfg.box_id, pkgs)
        if missing:
            ok, detail = provision_box(cfg.box_id, missing, cfg)
            if not ok:
                return SandboxVerdict(
                    verdict="error",
                    exit_code=1,
                    duration_seconds=time.monotonic() - started,
                    provider=cfg.provider,
                    test_command=test_command,
                    stdout_tail="",
                    stderr_tail=f"toolchain provision failed on box {cfg.box_id}: {detail}",
                    error_kind="provision_failed",
                )
        cmd = ["crabbox", "run", "--id", cfg.box_id, "--"] + shlex.split(test_command)
    else:
        # Legacy path: fresh ephemeral lease, apt prelude prepended every run
        # (no-op when toolchain is empty).
        test_command = tc.apply(test_command, toolchain)
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
    # Parse structured counts from the *full* output (the summary line lives at
    # the very end, so even a tail keeps it — but parse the full blob to be safe).
    results = tr.parse(proc.stdout + "\n" + proc.stderr, exit_code=proc.returncode)

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
            results=results,
        )

    # Distinguish "test failed" (verdict=fail) from "crabbox itself failed"
    # (verdict=error). Crabbox emits a "command complete" / "remote command
    # exited" line once it has finished the user command. If we see those
    # markers, infra was fine and the user command itself is what failed —
    # that's a `fail` verdict (return to worker). If we don't see them,
    # Crabbox itself failed at the broker / lease / sync layer — that's
    # an `error` (surface to owner).
    combined = proc.stdout + proc.stderr
    infra_completed = any(
        marker in combined
        for marker in (
            "command complete in",
            "remote command exited",
            "command exited with status",
            "command exited code",
            "exit code",
            "test failed",
            "FAILED",
        )
    )
    if infra_completed:
        return SandboxVerdict(
            verdict="fail",
            exit_code=proc.returncode,
            duration_seconds=duration,
            provider=cfg.provider,
            test_command=test_command,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            error_kind=None,
            results=results,
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
    parser.add_argument("--toolchain", default=None,
                        help="comma/space-separated languages or apt packages to install "
                             "in the VM before the test (e.g. 'node', 'go', 'python rust')")
    parser.add_argument("--reap", action="store_true",
                        help="stop orphaned leases for the provider before running "
                             "(prevents Apple VZ VM stacking from killed prior runs)")
    parser.add_argument("--box-id", default=None,
                        help="reusable crabbox box slug (from `crabbox warmup`); the "
                             "toolchain is provisioned onto it once, then every run "
                             "reuses it with no apt prelude (skips the per-round install)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = SandboxConfig.from_pincer_toml()
    overrides = {}
    if args.provider:
        overrides["provider"] = args.provider
    if args.timeout:
        overrides["timeout_seconds"] = args.timeout
    if args.box_id:
        overrides["box_id"] = args.box_id
    cfg = dataclasses.replace(cfg, **overrides)

    verdict = gate(workdir=args.workdir, test_command=args.test, config=cfg,
                   toolchain=tc.parse_list(args.toolchain), reap_stale=args.reap)
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
