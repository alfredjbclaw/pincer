#!/usr/bin/env python3
"""SWE-bench prediction extraction + JSONL I/O.

The #1 spurious-failure cause on SWE-bench is a model patch that doesn't apply
cleanly at `base_commit` — wrong base, hand-assembled diffs, trailing-newline
mangling. The official harness tries `git apply` three ways then gives up and
scores the instance 0 regardless of correctness. So the rule is absolute:

    model_patch = the literal `git diff` of the chosen candidate against the
    checked-out base_commit. Never hand-assembled.

Test-file edits are stripped: the harness resets test files and re-applies the
gold `test_patch`, so any model edit to a graded test is discarded anyway, and
stripping avoids a needless apply conflict.

The pure functions (split/strip/format) are unit-tested; `git_diff` is the only
shell-touching part.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

_DIFF_HEADER = "diff --git "


@dataclasses.dataclass(frozen=True)
class Prediction:
    instance_id: str
    model_patch: str
    model_name_or_path: str = "pincer"

    def to_dict(self) -> dict:
        return {"instance_id": self.instance_id,
                "model_name_or_path": self.model_name_or_path,
                "model_patch": self.model_patch}


def is_test_file(path: str) -> bool:
    """SWE-bench grades by resetting test files to the gold test_patch, so a
    model patch should carry source edits only."""
    low = path.lower()
    parts = low.split("/")
    base = parts[-1]
    return (any(p in ("test", "tests") for p in parts)
            or base.startswith("test_")
            or "_test." in base
            or base == "conftest.py")


def split_diff_by_file(diff: str) -> List[tuple]:
    """Split a unified diff into [(b_path, section_text), ...] preserving order
    and exact bytes (trailing newline of each section kept)."""
    if not diff:
        return []
    out: List[tuple] = []
    cur: List[str] = []

    def flush():
        if not cur:
            return
        text = "\n".join(cur)
        if not text.endswith("\n"):
            text += "\n"
        out.append((_section_path(cur[0]), text))

    for line in diff.splitlines():
        if line.startswith(_DIFF_HEADER):
            flush()
            cur = [line]
        elif cur:
            cur.append(line)
    flush()
    return out


def _section_path(header: str) -> str:
    # "diff --git a/foo/bar.py b/foo/bar.py" -> "foo/bar.py" (the b-path)
    for tok in header.split():
        if tok.startswith("b/"):
            return tok[2:]
    return ""


def strip_test_sections(diff: str) -> str:
    """Drop the diff sections that touch test files."""
    kept = [text for (path, text) in split_diff_by_file(diff) if not is_test_file(path)]
    return "".join(kept)


def extract_model_patch(diff: str, exclude_tests: bool = True) -> str:
    """Normalize a raw `git diff` into the submitted model_patch. Preserves the
    trailing newline the apply ladder is picky about; empty stays empty."""
    patch = strip_test_sections(diff) if exclude_tests else diff
    if patch and not patch.endswith("\n"):
        patch += "\n"
    return patch


def git_diff(worktree: str, base_commit: str) -> str:
    """The literal diff of the worktree's committed state vs base_commit."""
    p = subprocess.run(["git", "-C", worktree, "diff", base_commit],
                       capture_output=True, text=True, timeout=120)
    return p.stdout


def write_jsonl(predictions: List[Prediction], path: str) -> str:
    lines = [json.dumps(p.to_dict()) for p in predictions]
    Path(path).write_text("\n".join(lines) + ("\n" if lines else ""))
    return path


def read_jsonl(path: str) -> List[dict]:
    out: List[dict] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out
