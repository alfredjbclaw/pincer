#!/usr/bin/env python3
"""pincer preflight — verify the basics are healthy BEFORE dispatching coders,
so a run fails fast with one clear reason instead of getting halfway and breaking
(and burning credits/VM time on a doomed run).

Checks return a `Problem(severity, message)` or None:
  - "block": the run can't function (e.g. no GitHub auth → can't read issues).
  - "warn":  degraded but the run can proceed and surface it later (e.g. crabbox
             missing → the sandbox stage will error gracefully per-candidate).

`preflight()` aggregates over an injectable list of checks, so the orchestration
is unit-tested without touching the environment; the individual checks shell out.
"""
from __future__ import annotations

import dataclasses
import shutil
import subprocess
from typing import Callable, List, Optional


@dataclasses.dataclass(frozen=True)
class Problem:
    severity: str   # "block" | "warn"
    message: str


def check_gh_auth() -> Optional[Problem]:
    """GitHub CLI must be installed and authenticated — without it pincer can't
    read issues or open PRs. Blocking."""
    if shutil.which("gh") is None:
        return Problem("block", "GitHub CLI `gh` not on PATH")
    try:
        rc = subprocess.run(["gh", "auth", "status"], capture_output=True,
                            timeout=30).returncode
    except Exception as e:
        return Problem("block", f"`gh auth status` failed: {e}")
    if rc != 0:
        return Problem("block", "GitHub CLI not authenticated (`gh auth login`)")
    return None


def check_crabbox() -> Optional[Problem]:
    """Crabbox is the sandbox; without it the gate can't verify fixes. Warn —
    the sandbox stage already reports a clean per-candidate error."""
    if shutil.which("crabbox") is None:
        return Problem("warn", "crabbox not on PATH — sandbox verification will error")
    return None


def check_git() -> Optional[Problem]:
    if shutil.which("git") is None:
        return Problem("block", "git not on PATH")
    return None


DEFAULT_CHECKS: List[Callable[[], Optional[Problem]]] = [
    check_git, check_gh_auth, check_crabbox,
]


def preflight(checks: Optional[List[Callable[[], Optional[Problem]]]] = None) -> List[Problem]:
    """Run all checks; return the problems found (empty == all clear)."""
    checks = checks if checks is not None else DEFAULT_CHECKS
    problems: List[Problem] = []
    for c in checks:
        try:
            p = c()
        except Exception as e:  # a check must never crash the run
            p = Problem("warn", f"preflight check {getattr(c, '__name__', '?')} errored: {e}")
        if p is not None:
            problems.append(p)
    return problems


def has_blockers(problems: List[Problem]) -> bool:
    return any(p.severity == "block" for p in problems)


def summary(problems: List[Problem]) -> str:
    return "; ".join(f"[{p.severity}] {p.message}" for p in problems)
