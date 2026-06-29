#!/usr/bin/env python3
"""Grade predictions with the OFFICIAL SWE-bench harness — never Pincer's own
sandbox. The harness owns env setup (the per-repo conda/pip/test specs) and runs
each instance in its own Docker container; an instance is resolved iff its
FAIL_TO_PASS all pass AND its PASS_TO_PASS all still pass.

Two hard preconditions the research calls out:
  - Docker must be installed and running (the grader is Docker-only).
  - Canonical numbers require x86_64 images. On Apple Silicon the arm64 images
    are experimental and results are non-canonical — fine for dev, not for a
    leaderboard claim.

Always gold-sanity-check first (`--predictions_path gold` must resolve ~100%);
a low gold score means the environment is broken, not the model.

The argv builders are pure (unit-tested); `run_evaluation` is the thin shell.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from typing import List, Optional

from .dataset import LITE


def preflight() -> List[str]:
    """Return a list of human-readable problems blocking a canonical grade.
    Empty list == good to go for canonical (x86_64 + Docker) numbers."""
    problems: List[str] = []
    if shutil.which("docker") is None:
        problems.append(
            "Docker not found — the official grader is Docker-only. Install "
            "Docker Desktop or `brew install colima docker && colima start`.")
    else:
        ok = subprocess.run(["docker", "info"], capture_output=True).returncode == 0
        if not ok:
            problems.append("Docker is installed but the daemon isn't running "
                            "(start Docker Desktop / `colima start`).")
    if _is_arm():
        problems.append(
            "Host is arm64 (Apple Silicon). Official SWE-bench images are "
            "x86_64; arm64 images are experimental and scores are NON-CANONICAL. "
            "Grade on an x86_64 box for leaderboard-comparable numbers.")
    try:
        import swebench  # noqa: F401
    except ImportError:
        problems.append("`swebench` package not importable — `pip install "
                        "swebench` in the python that will run the grade.")
    return problems


def _is_arm() -> bool:
    return platform.machine().lower() in ("arm64", "aarch64")


def gold_sanity_argv(dataset_name: str = LITE, *, run_id: str = "sanity_gold",
                     max_workers: int = 8, cache_level: str = "env") -> List[str]:
    """`--predictions_path gold` should resolve ~100% — proves the env stack."""
    return [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", dataset_name,
        "--predictions_path", "gold",
        "--run_id", run_id,
        "--max_workers", str(max_workers),
        "--cache_level", cache_level,
    ]


def grade_argv(predictions_path: str, *, dataset_name: str = LITE,
               run_id: str = "pincer_lite", max_workers: int = 8,
               cache_level: str = "env") -> List[str]:
    return [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", dataset_name,
        "--predictions_path", predictions_path,
        "--run_id", run_id,
        "--max_workers", str(max_workers),
        "--cache_level", cache_level,
    ]


def cap_workers(cores: int, hard_cap: int = 24) -> int:
    """Harness guidance: min(0.75 * cores, 24) to avoid OOM/timeout thrash."""
    return max(1, min(int(cores * 0.75), hard_cap))


def run_evaluation(argv: List[str], *, dry_run: bool = False) -> int:
    if dry_run:
        print("DRY RUN:", " ".join(argv))
        return 0
    return subprocess.run(argv).returncode
