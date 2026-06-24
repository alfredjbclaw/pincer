#!/usr/bin/env python3
from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

from publication_gate import ReviewVerdict

DEFAULT_WRAPPER: Final[Path] = Path(
    os.environ.get(
        "PINCER_CLAUDE_CODE_WRAPPER",
        Path.home() / ".openclaw" / "workspace" / "tools" / "claude-code-wrapper.py",
    )
)
DEFAULT_MCP_BINARY: Final[Path] = Path(
    os.environ.get("PINCER_CODEBASE_MEMORY_MCP", Path.home() / ".local" / "bin" / "codebase-memory-mcp")
)
PARSE_FAILURE = "reviewer did not return a parseable verdict"


@dataclasses.dataclass(frozen=True)
class ReviewerCommand:
    argv: tuple[str, ...]
    timeout: int


@dataclasses.dataclass(frozen=True)
class ScopedMcpConfig:
    path: Path
    workdir: Path
    cleanup_dir: Path | None
    # If set, (config_path, prior_bytes_or_None) — restore the repo's .mcp.json
    # after the run: rewrite prior bytes, or unlink if there was no prior file.
    restore: tuple[Path, bytes | None] | None = None


def review(
    diff: str,
    issue: str,
    criteria: str,
    *,
    repo_workdir: str | None = None,
    model: str = "claude-opus-4-8",
    mcp_config_path: str | None = None,
    timeout: int = 900,
) -> ReviewVerdict:
    # repo_workdir is the repo under review. Pass it in real use so the reviewer
    # (and codebase-memory-mcp, discovered via .mcp.json in the cwd) can see the
    # code. Without it, the reviewer runs in an empty temp dir with no graph —
    # acceptable only for unit tests that mock the subprocess.
    scoped_config = _create_scoped_mcp_config(mcp_config_path, repo_workdir)
    try:
        command = ReviewerCommand(
            argv=_build_command(
                model=model,
                scoped_config=scoped_config,
                prompt=_review_prompt(diff=diff, issue=issue, criteria=criteria, mcp_config_path=scoped_config.path),
                timeout=timeout,
            ),
            timeout=timeout,
        )
        stdout, exit_code = _run_reviewer(command)
    except subprocess.TimeoutExpired:
        return ReviewVerdict("reject", [PARSE_FAILURE])
    finally:
        if scoped_config.cleanup_dir is not None:
            shutil.rmtree(scoped_config.cleanup_dir, ignore_errors=True)
        if scoped_config.restore is not None:
            restore_path, prior = scoped_config.restore
            if prior is None:
                restore_path.unlink(missing_ok=True)
            else:
                restore_path.write_bytes(prior)

    if exit_code != 0:
        return ReviewVerdict("reject", [PARSE_FAILURE])
    verdict = _extract_verdict(stdout)
    if verdict is None:
        return ReviewVerdict("reject", [PARSE_FAILURE])
    return verdict


def _run_reviewer(cmd: ReviewerCommand) -> tuple[str, int]:
    proc = subprocess.run(
        cmd.argv,
        capture_output=True,
        text=True,
        timeout=cmd.timeout,
    )
    return (proc.stdout, proc.returncode)


def _build_command(*, model: str, scoped_config: ScopedMcpConfig, prompt: str, timeout: int) -> tuple[str, ...]:
    return (
        "python3",
        str(DEFAULT_WRAPPER),
        "--workdir",
        str(scoped_config.workdir),
        "--read-only",
        "--model",
        model,
        "--timeout",
        str(timeout),
        "--no-default-contract",
        "--require-marker",
        "VERDICT:",
        "--require-marker",
        "REASONS:",
        "--require-marker",
        "BLOCKERS:",
        prompt,
    )


def _scoped_mcp_payload() -> str:
    return json.dumps(
        {"mcpServers": {"codebase-memory": {"command": str(DEFAULT_MCP_BINARY), "args": []}}}
    )


def _create_scoped_mcp_config(mcp_config_path: str | None, repo_workdir: str | None) -> ScopedMcpConfig:
    # Caller-supplied config path: run in the repo (if given) or the config's dir.
    if mcp_config_path is not None:
        config_path = Path(mcp_config_path)
        workdir = Path(repo_workdir) if repo_workdir is not None else config_path.parent
        return ScopedMcpConfig(path=config_path, workdir=workdir, cleanup_dir=None)

    # Real use: place .mcp.json INTO the repo so claude (cwd=repo) discovers it
    # and the memory graph sees the actual code. Back up any existing file.
    if repo_workdir is not None:
        workdir = Path(repo_workdir)
        config_path = workdir / ".mcp.json"
        prior = config_path.read_bytes() if config_path.exists() else None
        config_path.write_text(_scoped_mcp_payload())
        return ScopedMcpConfig(path=config_path, workdir=workdir, cleanup_dir=None,
                               restore=(config_path, prior))

    # No repo context (unit tests / probes): isolated temp dir, no graph.
    workdir = Path(tempfile.mkdtemp(prefix="pincer-reviewer-mcp-"))
    config_path = workdir / ".mcp.json"
    config_path.write_text(_scoped_mcp_payload())
    return ScopedMcpConfig(path=config_path, workdir=workdir, cleanup_dir=workdir)


def _review_prompt(*, diff: str, issue: str, criteria: str, mcp_config_path: Path) -> str:
    return "\n".join(
        [
            "Review only the diff and issue criteria below. Do not rely on the worker's reasoning.",
            f"Scoped MCP config: {mcp_config_path}",
            "Return exactly this contract:",
            "VERDICT: <approve|reject>",
            "REASONS: <one per line>",
            "BLOCKERS: <list or none>",
            "",
            "ISSUE:",
            issue,
            "",
            "CRITERIA:",
            criteria,
            "",
            "DIFF:",
            diff,
        ]
    )


def _extract_verdict(stdout: str) -> ReviewVerdict | None:
    verdict_match = re.search(r"^\s*VERDICT:\s*(approve|reject)\s*$", stdout, re.IGNORECASE | re.MULTILINE)
    reasons_match = re.search(r"^\s*REASONS:\s*", stdout, re.IGNORECASE | re.MULTILINE)
    blockers_match = re.search(r"^\s*BLOCKERS:\s*", stdout, re.IGNORECASE | re.MULTILINE)
    if verdict_match is None or reasons_match is None or blockers_match is None:
        return None

    blockers = _extract_blockers(stdout)
    if blockers is None:
        return None
    return ReviewVerdict(verdict=verdict_match.group(1).casefold(), blockers=blockers)


def _extract_blockers(stdout: str) -> list[str] | None:
    match = re.search(r"^\s*BLOCKERS:\s*(.*?)(?=^\s*(?:VERDICT|REASONS):|\Z)", stdout, re.IGNORECASE | re.MULTILINE | re.DOTALL)
    if match is None:
        return None

    raw_lines = match.group(1).splitlines()
    blockers: list[str] = []
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line:
            continue
        cleaned = _clean_list_item(line)
        if cleaned.casefold() == "none":
            return []
        blockers.append(cleaned)
    return blockers


def _clean_list_item(line: str) -> str:
    return re.sub(r"^[-*]\s*", "", line).strip()
