#!/usr/bin/env python3
"""
pincer runtime adapter — Codex primary, Claude Code fallback.

Used by the orchestrator skill to dispatch worker tasks. Returns a structured
RunResult with STATUS / FILES / VALIDATION / NEXT lines extracted from the
worker's completion contract.

Failure modes that trigger automatic fallback (one task at a time):
  - credit_exhausted    (ChatGPT Pro Lite / API credit / billing)
  - auth_expired        (token refresh failed)
  - rate_limit_429      (provider rate limit)
  - three_strikes       (three consecutive non-recoverable failures on this task)

Usage from Python:

    from runtime_adapter import dispatch, RuntimeConfig
    result = dispatch(prompt, workdir="/path/to/repo",
                      config=RuntimeConfig.from_pincer_toml())
    if result.status == "done":
        ...

Usage from shell:

    python3 tools/runtime_adapter.py \\
        --workdir /path/to/repo \\
        --prompt-file /tmp/brief.md \\
        [--primary codex|claude-code] [--no-fallback]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

PINCER_CONFIG_DEFAULT = Path.home() / ".openclaw" / "pincer.toml"
PINCER_STATE_DIR = Path.home() / ".openclaw" / "pincer"
CLAUDE_CODE_WRAPPER = Path.home() / ".openclaw" / "workspace" / "tools" / "claude-code-wrapper.py"

COMPLETION_MARKERS = ("STATUS:", "FILES:", "VALIDATION:", "NEXT:")
SUCCESS_STATUSES = {"done", "no_changes"}
FAILURE_STATUSES = {"blocked"}

# Patterns matched against stderr/last-message to detect fallback triggers.
CREDIT_EXHAUSTED_PATTERNS = (
    "insufficient_quota",
    "credit_exhausted",
    "billing_hard_limit",
    "you have insufficient credits",
    "balance is too low",
    "monthly limit",
    "free trial expired",
)
AUTH_EXPIRED_PATTERNS = (
    "invalid_api_key",
    "auth_expired",
    "token expired",
    "unauthorized",
    "401 unauthorized",
    "please log in again",
)
RATE_LIMIT_PATTERNS = (
    "rate_limit_exceeded",
    "429",
    "too many requests",
    "ratelimited",
)

WORKER_CONTRACT = """
Completion contract for this unattended pincer worker run:
- Do the actual work — implement and write tests, and prove the tests pass.
- Leave your changes UNCOMMITTED in the working tree. Do NOT commit, branch,
  push, stage, open PRs, or merge — the orchestrator owns every git operation
  and handles publication after the sandbox verdict. (Your sandbox cannot write
  .git anyway; attempting git is what makes a clean run look blocked.)
- If you decide no changes are needed, say that explicitly and explain why.
- Before you finish, include a final block with these exact markers on their
  own lines:

STATUS: <done|no_changes|blocked>
FILES: <comma-separated paths changed, or none>
VALIDATION: <commands run / checks performed, or none>
NEXT: <none or concise remaining gap>

- Do not stop after saying you will inspect, read, or investigate. Finish the
  task or declare a real blocker.
""".strip()


@dataclasses.dataclass(frozen=True)
class RuntimeConfig:
    primary: str = "codex"
    fallback: str = "claude-code"
    fallback_enabled: bool = True
    ultrawork: bool = True
    codex_model: str = "gpt-5.5"
    claude_model: str = "claude-opus-4-6"
    codex_sandbox: str = "workspace-write"
    timeout_seconds: int = 1800
    max_strikes: int = 3

    @classmethod
    def from_pincer_toml(cls, path: Optional[Path] = None) -> "RuntimeConfig":
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
        rt = data.get("runtime", {})
        models = data.get("models", {})
        agent_model = models.get("agent", "openai/gpt-5.5")
        codex_model = agent_model.split("/", 1)[-1] if "/" in agent_model else agent_model
        return cls(
            primary=rt.get("primary", "codex"),
            fallback=rt.get("fallback", "claude-code"),
            fallback_enabled=bool(rt.get("fallback", "claude-code")),
            ultrawork=bool(rt.get("ultrawork", True)),
            codex_model=codex_model,
        )


@dataclasses.dataclass
class RunResult:
    runtime: str
    status: str            # "done" | "no_changes" | "blocked" | "error"
    files: list[str]
    validation: str
    next: str
    final_text: str
    fallback_used: bool
    fallback_reason: Optional[str]
    duration_seconds: float
    exit_code: int

    def is_success(self) -> bool:
        return self.status in SUCCESS_STATUSES

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _detect_fallback_trigger(stderr: str, final_text: str) -> Optional[str]:
    haystack = f"{stderr}\n{final_text}".lower()
    for pat in CREDIT_EXHAUSTED_PATTERNS:
        if pat in haystack:
            return "credit_exhausted"
    for pat in AUTH_EXPIRED_PATTERNS:
        if pat in haystack:
            return "auth_expired"
    for pat in RATE_LIMIT_PATTERNS:
        if pat in haystack:
            return "rate_limit_429"
    return None


def _extract_contract(final_text: str) -> dict:
    """Parse STATUS/FILES/VALIDATION/NEXT lines from the worker's final output."""
    out = {"status": "error", "files": [], "validation": "none", "next": "none"}
    for marker in COMPLETION_MARKERS:
        m = re.search(rf"^\s*{re.escape(marker)}\s*(.+?)\s*$", final_text, re.MULTILINE)
        if not m:
            continue
        value = m.group(1).strip()
        key = marker.rstrip(":").lower()
        if key == "files":
            files = [f.strip() for f in value.split(",") if f.strip() and f.strip().lower() != "none"]
            out["files"] = files
        elif key == "status":
            out["status"] = value.lower()
        else:
            out[key] = value
    return out


# The ChatGPT (Codex) subscription throttles concurrent requests: firing 7+ at
# once trips 429s and we'd dump good work onto claude-code. Cap simultaneous
# codex calls (env-overridable) and back off + retry on a 429 before falling
# back. Keeps codex primary under high fan-out.
_CODEX_SEMAPHORE = threading.Semaphore(int(os.environ.get("PINCER_CODEX_CONCURRENCY", "4")))
_CODEX_MAX_ATTEMPTS = 3
_CODEX_BACKOFF_S = 8


def _is_rate_limited(stderr: str, text: str) -> bool:
    haystack = f"{stderr}\n{text}".lower()
    return any(pat in haystack for pat in RATE_LIMIT_PATTERNS)


def _run_codex(prompt: str, workdir: Path, cfg: RuntimeConfig) -> tuple[str, str, int]:
    """Invoke codex exec (concurrency-capped, 429-retried). Returns (last_message, stderr, exit_code)."""
    if shutil.which("codex") is None:
        return ("", "codex CLI not found on PATH", 127)
    result = ("", "no codex attempt", 1)
    for attempt in range(_CODEX_MAX_ATTEMPTS):
        with _CODEX_SEMAPHORE:  # never exceed the plan's concurrency
            result = _codex_once(prompt, workdir, cfg)
        text, stderr, _ = result
        if not _is_rate_limited(stderr, text):
            return result  # success or a non-rate-limit failure: don't retry
        if attempt < _CODEX_MAX_ATTEMPTS - 1:
            time.sleep(_CODEX_BACKOFF_S * (attempt + 1))  # 8s, 16s
    return result  # exhausted retries -> caller falls back to claude-code


def _codex_once(prompt: str, workdir: Path, cfg: RuntimeConfig) -> tuple[str, str, int]:
    worker_prompt = f"ulw: {prompt}" if cfg.ultrawork else prompt
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as last_msg_file:
        last_msg_path = Path(last_msg_file.name)
    try:
        cmd = [
            "codex", "exec",
            "--cd", str(workdir),
            "--sandbox", cfg.codex_sandbox,
            "--model", cfg.codex_model,
            "--skip-git-repo-check",
            "--output-last-message", str(last_msg_path),
            worker_prompt + "\n\n" + WORKER_CONTRACT,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.timeout_seconds)
        last_message = last_msg_path.read_text() if last_msg_path.exists() else ""
        return (last_message or proc.stdout, proc.stderr, proc.returncode)
    except subprocess.TimeoutExpired:
        return ("", f"codex exec timed out after {cfg.timeout_seconds}s", 124)
    finally:
        last_msg_path.unlink(missing_ok=True)


def _run_claude_code(prompt: str, workdir: Path, cfg: RuntimeConfig) -> tuple[str, str, int]:
    """Invoke the workspace-local claude-code-wrapper.py, return (last_message, stderr, exit_code)."""
    if not CLAUDE_CODE_WRAPPER.exists():
        return ("", f"claude-code wrapper not found at {CLAUDE_CODE_WRAPPER}", 127)
    cmd = [
        "python3", str(CLAUDE_CODE_WRAPPER),
        "--workdir", str(workdir),
        "--model", cfg.claude_model,
        prompt + "\n\n" + WORKER_CONTRACT,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=cfg.timeout_seconds,
        )
        # The wrapper emits a JSON envelope; the worker's contract block lives in
        # its `final_text`, not at the top level of stdout. Hand the contract
        # parser the final_text (else STATUS:/FILES: never match -> false error).
        text, exit_code = _unwrap_wrapper_output(proc.stdout, proc.returncode)
        return (text, proc.stderr, exit_code)
    except subprocess.TimeoutExpired:
        return ("", f"claude-code wrapper timed out after {cfg.timeout_seconds}s", 124)


def _unwrap_wrapper_output(stdout: str, returncode: int) -> tuple[str, int]:
    """Pull final_text from the claude-code-wrapper JSON envelope.

    Returns (text_to_parse, effective_exit). A genuine timeout/agent-error keeps
    a non-zero exit so dispatch can react; otherwise the wrapper's own non-zero
    exit on benign conditions is not allowed to mask a real completion contract.
    """
    try:
        obj = json.loads(stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return (stdout, returncode)
    if not isinstance(obj, dict):
        return (stdout, returncode)
    final_text = obj.get("final_text") or ""
    if obj.get("timed_out"):
        return (final_text, 124)
    last_result = obj.get("last_result")
    if isinstance(last_result, dict) and last_result.get("is_error"):
        return (final_text, returncode or 1)
    # Benign: trust the contract block inside final_text; let _extract_contract
    # decide done/blocked. Report exit 0 so a STATUS:done isn't masked.
    return (final_text, 0)


def _execute(runtime: str, prompt: str, workdir: Path, cfg: RuntimeConfig) -> tuple[str, str, int]:
    if runtime == "codex":
        return _run_codex(prompt, workdir, cfg)
    if runtime == "claude-code":
        return _run_claude_code(prompt, workdir, cfg)
    return ("", f"unknown runtime: {runtime}", 64)


def dispatch(
    prompt: str,
    workdir: Path | str,
    config: Optional[RuntimeConfig] = None,
) -> RunResult:
    """Dispatch a worker task. Returns a structured RunResult.

    Tries `config.primary` first. On a fallback-trigger pattern or non-zero exit,
    retries on `config.fallback` (if enabled). Only one fallback attempt per call.
    """
    cfg = config or RuntimeConfig.from_pincer_toml()
    workdir = Path(workdir).resolve()
    if not workdir.is_dir():
        raise ValueError(f"workdir does not exist or is not a directory: {workdir}")
    started = time.monotonic()

    # Primary attempt.
    primary_text, primary_stderr, primary_exit = _execute(cfg.primary, prompt, workdir, cfg)
    primary_contract = _extract_contract(primary_text)
    trigger = _detect_fallback_trigger(primary_stderr, primary_text)
    primary_ok = primary_exit == 0 and primary_contract["status"] in SUCCESS_STATUSES

    if primary_ok or not cfg.fallback_enabled or cfg.primary == cfg.fallback:
        return RunResult(
            runtime=cfg.primary,
            status=primary_contract["status"],
            files=primary_contract["files"],
            validation=primary_contract["validation"],
            next=primary_contract["next"],
            final_text=primary_text,
            fallback_used=False,
            fallback_reason=None,
            duration_seconds=time.monotonic() - started,
            exit_code=primary_exit,
        )

    # Fallback decision: trigger detected, or hard error.
    reason = trigger or ("nonzero_exit" if primary_exit != 0 else "blocked_status")
    fb_text, fb_stderr, fb_exit = _execute(cfg.fallback, prompt, workdir, cfg)
    fb_contract = _extract_contract(fb_text)
    return RunResult(
        runtime=cfg.fallback,
        status=fb_contract["status"],
        files=fb_contract["files"],
        validation=fb_contract["validation"],
        next=fb_contract["next"],
        final_text=fb_text,
        fallback_used=True,
        fallback_reason=reason,
        duration_seconds=time.monotonic() - started,
        exit_code=fb_exit,
    )


def _cli() -> int:
    parser = argparse.ArgumentParser(description="pincer runtime adapter")
    parser.add_argument("--workdir", required=True, help="repo working directory")
    parser.add_argument("--prompt-file", help="file containing the worker prompt (else use --prompt)")
    parser.add_argument("--prompt", help="inline worker prompt")
    parser.add_argument("--primary", choices=["codex", "claude-code"], default=None)
    parser.add_argument("--fallback", choices=["codex", "claude-code"], default=None)
    parser.add_argument("--no-fallback", action="store_true")
    parser.add_argument("--model", default=None, help="primary-runtime model override")
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="emit RunResult as JSON")
    args = parser.parse_args()

    if not args.prompt and not args.prompt_file:
        parser.error("one of --prompt or --prompt-file is required")
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text()
    else:
        prompt = args.prompt

    cfg = RuntimeConfig.from_pincer_toml()
    overrides = {}
    if args.primary:
        overrides["primary"] = args.primary
    if args.fallback:
        overrides["fallback"] = args.fallback
    if args.no_fallback:
        overrides["fallback_enabled"] = False
    if args.model:
        if (args.primary or cfg.primary) == "codex":
            overrides["codex_model"] = args.model
        else:
            overrides["claude_model"] = args.model
    if args.timeout:
        overrides["timeout_seconds"] = args.timeout
    cfg = dataclasses.replace(cfg, **overrides)

    result = dispatch(prompt, workdir=args.workdir, config=cfg)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"runtime: {result.runtime}")
        print(f"status:  {result.status}")
        print(f"files:   {', '.join(result.files) or 'none'}")
        print(f"validation: {result.validation}")
        print(f"next: {result.next}")
        print(f"fallback_used: {result.fallback_used} ({result.fallback_reason or '-'})")
        print(f"duration: {result.duration_seconds:.1f}s exit={result.exit_code}")
    return 0 if result.is_success() else 1


if __name__ == "__main__":
    sys.exit(_cli())
