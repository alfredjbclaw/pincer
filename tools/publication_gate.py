#!/usr/bin/env python3
from __future__ import annotations

import dataclasses
import fnmatch
from typing import Final

DANGER_SURFACE_PATTERNS: Final[tuple[str, ...]] = (
    ".github/workflows/",
    "Dockerfile",
    "docker-compose",
    "*.tf",
    "**/auth*",
    "**/billing*",
    "**/payment*",
    "*.sql",
    "pyproject.toml",
    "package.json",
    "requirements*.txt",
    "Makefile",
    "*.service",
    "*.plist",
    "config.toml",
    "settings.py",
    ".env*",
)


@dataclasses.dataclass(frozen=True)
class DiffStats:
    lines_changed: int
    files: list[str]


@dataclasses.dataclass(frozen=True)
class RepoMeta:
    owner: str
    name: str
    is_owned: bool


@dataclasses.dataclass(frozen=True)
class ReviewVerdict:
    verdict: str
    blockers: list[str]


@dataclasses.dataclass(frozen=True)
class GateInputs:
    repo: RepoMeta
    diff: DiffStats
    worker_status: str
    tests_green: bool
    lint_clean: bool
    build_clean: bool
    has_secrets: bool
    docs_updated_if_needed: bool
    review: ReviewVerdict


@dataclasses.dataclass(frozen=True)
class GateDecision:
    action: str
    reasons: list[str]
    danger_surface: bool


def is_danger_surface(files: list[str]) -> tuple[bool, list[str]]:
    matched_files = [file_path for file_path in files if _matches_danger_pattern(file_path)]
    return (bool(matched_files), matched_files)


def decide(inputs: GateInputs) -> GateDecision:
    danger_surface, matched_files = is_danger_surface(inputs.diff.files)
    if not inputs.repo.is_owned:
        return GateDecision(action="open_pr", reasons=["not an owned repo"], danger_surface=danger_surface)
    if inputs.worker_status != "done":
        return GateDecision(
            action="open_pr",
            reasons=[f"worker status is {inputs.worker_status}"],
            danger_surface=danger_surface,
        )

    failed_reasons = _production_ready_failures(inputs)
    if failed_reasons:
        return GateDecision(action="open_pr", reasons=failed_reasons, danger_surface=danger_surface)
    if danger_surface:
        return GateDecision(
            action="escalate",
            reasons=[f"all checks passed; owned; danger surface: {', '.join(matched_files)}"],
            danger_surface=True,
        )
    return GateDecision(action="auto_merge", reasons=["all checks passed; owned; non-danger"], danger_surface=False)


def _production_ready_failures(inputs: GateInputs) -> list[str]:
    reasons: list[str] = []
    if not inputs.tests_green:
        reasons.append("tests not green")
    if not inputs.lint_clean:
        reasons.append("lint not clean")
    if not inputs.build_clean:
        reasons.append("build not clean")
    if inputs.has_secrets:
        reasons.append("secrets present in diff")
    if not inputs.docs_updated_if_needed:
        reasons.append("docs not updated if needed")
    if inputs.review.verdict != "approve":
        reasons.append("review rejected")
    reasons.extend(f"review blocker: {blocker}" for blocker in inputs.review.blockers)
    return reasons


def _matches_danger_pattern(file_path: str) -> bool:
    """Match one path against DANGER_SURFACE_PATTERNS.

    Pattern semantics (so the module constant is the single source of truth —
    editing it actually changes behavior):
      - trailing "/"  → directory substring match on the full path
      - "**/X"        → glob X against any path segment
      - contains * ?  → glob against basename or any segment
      - plain literal → basename equals, or starts with, the literal
    """
    normalized = file_path.replace("\\", "/").casefold()
    basename = normalized.rsplit("/", 1)[-1]
    segments = normalized.split("/")
    for raw in DANGER_SURFACE_PATTERNS:
        pat = raw.casefold()
        if pat.endswith("/"):
            if pat in normalized:
                return True
        elif pat.startswith("**/"):
            sub = pat[3:]
            if any(fnmatch.fnmatchcase(seg, sub) for seg in segments):
                return True
        elif "*" in pat or "?" in pat:
            if fnmatch.fnmatchcase(basename, pat) or any(fnmatch.fnmatchcase(seg, pat) for seg in segments):
                return True
        else:
            if basename == pat or basename.startswith(pat):
                return True
    return False
