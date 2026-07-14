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
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Final, Iterable, Optional

import global_gate

PINCER_CONFIG_DEFAULT = Path.home() / ".openclaw" / "pincer.toml"
PINCER_STATE_DIR = Path.home() / ".openclaw" / "pincer"
CLAUDE_CODE_WRAPPER = Path.home() / ".openclaw" / "workspace" / "tools" / "claude-code-wrapper.py"
CODEX_ARG0_DIR: Final = Path.home() / ".codex" / "tmp" / "arg0"
_CODEX_LOCK_STALE_SECONDS: Final = 5.0

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
- Reporting done without changing any files is treated as a failure; when no
  change is genuinely needed, use STATUS: no_changes.
- If the task requires changing files outside your permitted scope, do not
  route around it: STATUS: blocked with NEXT: SCOPE-REQUEST: <path> -- <why>.
""".strip()

# Effort tiers accepted by codex for model_reasoning_effort. The ladder never
# auto-selects max — that tier is a deliberate operator decision (quota).
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
LESSONS_LIMIT = 4_000


def effort_for_attempt(base: str, attempt: int) -> str:
    """Effort ladder: base on attempt 1, >=high on attempt 2, >=xhigh on 3+."""
    if attempt <= 1 or base not in EFFORTS:
        return base
    step = "high" if attempt == 2 else "xhigh"
    return base if EFFORTS.index(base) >= EFFORTS.index(step) else step


def _lessons_block() -> str:
    """Bounded operating-lessons block appended to the worker contract.

    Optional: reads $PINCER_LESSONS (default ~/.openclaw/pincer/LESSONS.md).
    Keep the file stable while runs are in flight — it is part of the resume
    prompt-cache prefix."""
    path = Path(os.environ.get("PINCER_LESSONS", PINCER_STATE_DIR / "LESSONS.md"))
    try:
        text = path.read_text().strip()
    except OSError:
        return ""
    if not text:
        return ""
    return "\n\nOperating lessons from prior runs:\n" + text[:LESSONS_LIMIT]


def worker_contract() -> str:
    return WORKER_CONTRACT + _lessons_block()


@dataclasses.dataclass(frozen=True)
class RuntimeConfig:
    primary: str = "codex"
    fallback: str = "claude-code"
    fallback_enabled: bool = True
    ultrawork: bool = True
    codex_model: str = "gpt-5.5"
    claude_model: str = "claude-opus-4-6"
    codex_sandbox: str = "workspace-write"
    codex_effort: str = "medium"
    effort_ladder: bool = True
    timeout_seconds: int = 1800
    max_strikes: int = 3
    global_max_concurrent: int = 4
    global_gate_wait_seconds: int = 600
    global_gate_stale_seconds: int = 600

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
            codex_effort=str(rt.get("codex_effort", "medium")),
            effort_ladder=bool(rt.get("effort_ladder", True)),
            global_max_concurrent=int(rt.get("global_max_concurrent", 4)),
            global_gate_wait_seconds=int(rt.get("global_gate_wait_seconds", 600)),
            global_gate_stale_seconds=int(rt.get("global_gate_stale_seconds", 600)),
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
    thread_id: Optional[str] = None  # codex session handle for resume retries
    empty_diff: bool = False         # done-with-clean-tree guard fired

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
_SUBPROCESS_RUN = subprocess.run


def _is_rate_limited(stderr: str, text: str) -> bool:
    haystack = f"{stderr}\n{text}".lower()
    return any(pat in haystack for pat in RATE_LIMIT_PATTERNS)


def _kill_process_group(proc: Optional[subprocess.Popen[str]]) -> None:
    if proc is None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return


def _reap_codex_locks() -> None:
    now = time.time()
    try:
        locks = list(CODEX_ARG0_DIR.glob("*.lock"))
    except OSError:
        return
    for lock in locks:
        try:
            stat = lock.stat()
        except (FileNotFoundError, OSError):
            continue
        if now - stat.st_mtime < _CODEX_LOCK_STALE_SECONDS:
            continue
        try:
            if lock.is_dir():
                lock.rmdir()
            else:
                lock.unlink()
        except (FileNotFoundError, OSError):
            continue


def _parse_thread_id(stdout: str) -> Optional[str]:
    """Extract the codex session/thread id from --json event output."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            value = event.get("thread_id")
            if isinstance(value, str):
                return value
    return None


def _final_from_jsonl(stdout: str) -> str:
    """Last agent_message from --json output — fallback when the last-message
    file was not written (e.g. hard error mid-turn)."""
    final = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            value = item.get("text") or item.get("content")
            if isinstance(value, str):
                final = value
    return final


def _run_codex(prompt: str, workdir: Path, cfg: RuntimeConfig,
               effort: Optional[str] = None,
               resume_thread: Optional[str] = None) -> tuple[str, str, int, Optional[str]]:
    """Invoke codex exec (concurrency-capped, 429-retried).

    Returns (last_message, stderr, exit_code, thread_id)."""
    if shutil.which("codex") is None:
        return ("", "codex CLI not found on PATH", 127, None)
    result = ("", "no codex attempt", 1, None)
    for attempt in range(_CODEX_MAX_ATTEMPTS):
        with _CODEX_SEMAPHORE:  # never exceed the plan's concurrency
            result = _codex_once(prompt, workdir, cfg, effort, resume_thread)
        text, stderr, returncode, _thread = result
        if not _is_rate_limited(stderr, text):
            # A clean nonzero exit (credit/auth/error) does not raise, so
            # _codex_once's timeout/exception reap never fires — yet the failed
            # run may have orphaned a codex lock right before we fall back to
            # claude-code. Reap stale locks here so the #8 wedge class can't
            # linger on the error path, not just the timeout path.
            if returncode != 0:
                _reap_codex_locks()
            return result  # success or a non-rate-limit failure: don't retry
        if attempt < _CODEX_MAX_ATTEMPTS - 1:
            time.sleep(_CODEX_BACKOFF_S * (attempt + 1))  # 8s, 16s
    _reap_codex_locks()  # exhausted rate-limit retries -> fall back; clear stale lock
    return result  # exhausted retries -> caller falls back to claude-code


def _codex_once(prompt: str, workdir: Path, cfg: RuntimeConfig,
                effort: Optional[str] = None,
                resume_thread: Optional[str] = None) -> tuple[str, str, int, Optional[str]]:
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as last_msg_file:
        last_msg_path = Path(last_msg_file.name)
    proc: Optional[subprocess.Popen[str]] = None
    # Config that must hold on EVERY dispatch: effort (the thinking dial) and
    # plugin kill-switches (plugin daemons leak processes and ~4x token
    # overhead in unattended fleets).
    explicit_cfg = [
        "-c", f'model_reasoning_effort="{effort or cfg.codex_effort}"',
        "-c", "features.plugins=false",
        "-c", "features.plugin_hooks=false",
    ]
    try:
        if resume_thread:
            # Resume the same session: context intact, cached input ~10x
            # cheaper. `codex exec resume` has NO --cd/--sandbox/--profile
            # flags — cwd comes from the subprocess, and sandbox/model must be
            # passed explicitly or the session inherits the user's global
            # config (which may be full-access).
            cmd = [
                "codex", "exec", "resume", resume_thread,
                "--json",
                "--model", cfg.codex_model,
                "--skip-git-repo-check",
                "--output-last-message", str(last_msg_path),
                "-c", f'sandbox_mode="{cfg.codex_sandbox}"',
                "-c", 'approval_policy="never"',
                *explicit_cfg,
                prompt,
            ]
        else:
            worker_prompt = f"ulw: {prompt}" if cfg.ultrawork else prompt
            cmd = [
                "codex", "exec",
                "--cd", str(workdir),
                "--sandbox", cfg.codex_sandbox,
                "--model", cfg.codex_model,
                "--skip-git-repo-check",
                "--json",
                "--output-last-message", str(last_msg_path),
                *explicit_cfg,
                worker_prompt + "\n\n" + worker_contract(),
            ]
        if subprocess.run is not _SUBPROCESS_RUN:
            completed = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.timeout_seconds)
            stdout, stderr = completed.stdout, completed.stderr
            returncode = completed.returncode
        else:
            proc = subprocess.Popen(
                cmd,
                cwd=str(workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = proc.communicate(timeout=cfg.timeout_seconds)
            returncode = proc.returncode if proc.returncode is not None else 1
        last_message = last_msg_path.read_text() if last_msg_path.exists() else ""
        thread_id = _parse_thread_id(stdout) or resume_thread
        return (last_message or _final_from_jsonl(stdout) or stdout, stderr,
                returncode, thread_id)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        _reap_codex_locks()
        return ("", f"codex exec timed out after {cfg.timeout_seconds}s", 124, resume_thread)
    except Exception as exc:
        _kill_process_group(proc)
        _reap_codex_locks()
        return ("", f"codex exec failed: {exc}", 1, resume_thread)
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
        prompt + "\n\n" + worker_contract(),
    ]
    proc: Optional[subprocess.Popen[str]] = None
    try:
        if subprocess.run is not _SUBPROCESS_RUN:
            completed = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.timeout_seconds)
            stdout, stderr = completed.stdout, completed.stderr
            returncode = completed.returncode
        else:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = proc.communicate(timeout=cfg.timeout_seconds)
            returncode = proc.returncode if proc.returncode is not None else 1
        # The wrapper emits a JSON envelope; the worker's contract block lives in
        # its `final_text`, not at the top level of stdout. Hand the contract
        # parser the final_text (else STATUS:/FILES: never match -> false error).
        text, exit_code = _unwrap_wrapper_output(stdout, returncode)
        return (text, stderr, exit_code)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        _reap_codex_locks()
        return ("", f"claude-code wrapper timed out after {cfg.timeout_seconds}s", 124)
    except Exception as exc:
        _kill_process_group(proc)
        _reap_codex_locks()
        return ("", f"claude-code wrapper failed: {exc}", 1)


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


def _execute(runtime: str, prompt: str, workdir: Path, cfg: RuntimeConfig,
             effort: Optional[str] = None,
             resume_thread: Optional[str] = None) -> tuple[str, str, int, Optional[str]]:
    if runtime == "codex":
        return _run_codex(prompt, workdir, cfg, effort, resume_thread)
    if runtime == "claude-code":
        text, stderr, code = _run_claude_code(prompt, workdir, cfg)
        return (text, stderr, code, None)
    return ("", f"unknown runtime: {runtime}", 64, None)


def _git_tree_clean(workdir: Path) -> Optional[bool]:
    """True/False for a git worktree's cleanliness; None when not a git repo
    (or git itself fails) — the guard only acts on a definite True."""
    try:
        probe = _SUBPROCESS_RUN(
            ["git", "-C", str(workdir), "status", "--porcelain"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return None
    if probe.returncode != 0:
        return None
    return not probe.stdout.strip()


def _apply_empty_diff_guard(contract: dict, workdir: Path) -> bool:
    """GPT-5.6 documented early-exit mode (openai/codex#32162): a worker that
    reports done without touching the tree did not do the work. Downgrade to
    error so strike/fallback/retry machinery reacts; an honest no-op must use
    STATUS: no_changes."""
    if contract["status"] != "done":
        return False
    if _git_tree_clean(workdir) is not True:
        return False
    contract["status"] = "error"
    contract["next"] = ("empty_diff guard: STATUS done with a clean tree — the "
                        "work was not performed (use no_changes for honest no-ops)")
    return True


def dispatch(
    prompt: str,
    workdir: Path | str,
    config: Optional[RuntimeConfig] = None,
    attempt: int = 1,
    resume_thread: Optional[str] = None,
) -> RunResult:
    """Dispatch a worker task. Returns a structured RunResult.

    Tries `config.primary` first. On a fallback-trigger pattern or non-zero exit,
    retries on `config.fallback` (if enabled). Only one fallback attempt per call.

    `attempt` drives the effort ladder (base → high → xhigh; never auto-max).
    `resume_thread` (codex primary only) resumes the prior worker session so a
    retry keeps its context and pays cached-input rates; the prompt should then
    carry only the new feedback. A resumed call never vendor-falls-back — the
    fallback runtime has none of the session context, so the caller should
    re-dispatch fresh instead.
    """
    cfg = config or RuntimeConfig.from_pincer_toml()
    workdir = Path(workdir).resolve()
    if not workdir.is_dir():
        raise ValueError(f"workdir does not exist or is not a directory: {workdir}")
    effort = (effort_for_attempt(cfg.codex_effort, attempt)
              if cfg.effort_ladder else cfg.codex_effort)
    resume_thread = resume_thread if cfg.primary == "codex" else None
    started = time.monotonic()

    # Match the VM RAM gate's backpressure posture: wait for capacity instead of
    # over-dispatching. Unlike RAM, this is a hard global ceiling, so timeout
    # returns a blocked dispatch rather than proceeding past the cap.
    try:
        gate = global_gate.slot(
            max_slots=cfg.global_max_concurrent,
            wait_timeout_s=cfg.global_gate_wait_seconds,
            label=f"{cfg.primary}:{workdir.name}",
            max_age_s=cfg.global_gate_stale_seconds,
        )
        with gate:
            # Primary attempt.
            primary_text, primary_stderr, primary_exit, primary_thread = _execute(
                cfg.primary, prompt, workdir, cfg, effort, resume_thread)
            primary_contract = _extract_contract(primary_text)
            primary_empty = _apply_empty_diff_guard(primary_contract, workdir)
            trigger = _detect_fallback_trigger(primary_stderr, primary_text)
            primary_ok = primary_exit == 0 and primary_contract["status"] in SUCCESS_STATUSES

            if (primary_ok or not cfg.fallback_enabled or cfg.primary == cfg.fallback
                    or resume_thread is not None):
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
                    thread_id=primary_thread,
                    empty_diff=primary_empty,
                )

            # Fallback decision: trigger detected, or hard error.
            reason = trigger or (
                "empty_diff" if primary_empty
                else "nonzero_exit" if primary_exit != 0
                else "blocked_status")
            fb_text, fb_stderr, fb_exit, _fb_thread = _execute(
                cfg.fallback, prompt, workdir, cfg, effort, None)
            fb_contract = _extract_contract(fb_text)
            fb_empty = _apply_empty_diff_guard(fb_contract, workdir)
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
                thread_id=primary_thread,
                empty_diff=fb_empty,
            )
    except global_gate.GateTimeout as exc:
        return RunResult(
            runtime=cfg.primary,
            status="blocked",
            files=[],
            validation="none",
            next=str(exc),
            final_text=str(exc),
            fallback_used=False,
            fallback_reason="global_gate_timeout",
            duration_seconds=time.monotonic() - started,
            exit_code=75,
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
    parser.add_argument("--effort", choices=EFFORTS, default=None,
                        help="codex reasoning-effort base (ladder still applies)")
    parser.add_argument("--attempt", type=int, default=1,
                        help="retry round number; drives the effort ladder")
    parser.add_argument("--resume-thread", default=None,
                        help="codex thread id to resume (retry with context intact)")
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
    if args.effort:
        overrides["codex_effort"] = args.effort
    cfg = dataclasses.replace(cfg, **overrides)

    result = dispatch(prompt, workdir=args.workdir, config=cfg,
                      attempt=args.attempt, resume_thread=args.resume_thread)
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
