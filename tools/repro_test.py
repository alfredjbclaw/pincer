#!/usr/bin/env python3
"""pincer reproduction-test generation + fail-to-pass (F->P) filtering.

The single highest-value verifier in the SWE-bench literature: generate a test
that *reproduces* the reported bug (fails on the buggy code), then keep/prefer
the candidate patches that flip it to passing. Roughly doubles selection
precision (SWT-bench 47.8%; ~70% top-1 at Google).

Two responsibilities, deliberately separated so the load-bearing guardrail is
pure and unit-tested:

  generate(...)      -- LLM writes a single self-contained pytest. Injectable
                        `_runner` so tests don't shell out.
  is_valid_repro(..) -- a generated test is only TRUSTED if it actually FAILS on
                        the unpatched base. Generated tests are individually
                        noisy; an invalid one is discarded and the cascade falls
                        back to regression-only ranking (never a hard gate).
  flips(base, cand)  -- did this candidate turn the (valid) repro test green?

The orchestrator wires these to the sandbox: run the repro test once on the
base checkout (validity), then once per candidate (flip), feeding the resulting
`test_results.TestResults` back into these pure predicates.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Optional

import test_results as tr

DEFAULT_WRAPPER = Path(
    os.environ.get(
        "PINCER_CLAUDE_CODE_WRAPPER",
        Path.home() / ".openclaw" / "workspace" / "tools" / "claude-code-wrapper.py",
    )
)
DEFAULT_PATH = "test_pincer_repro.py"
_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


@dataclasses.dataclass
class ReproTest:
    source: str
    path: str = DEFAULT_PATH
    valid: Optional[bool] = None    # set once run against the base checkout

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def extract_test_code(text: str) -> Optional[str]:
    """Pull the python test source out of the model's reply. Prefers a fenced
    block; falls back to the raw text if it already looks like a test module."""
    if not text:
        return None
    blocks = _FENCE.findall(text)
    # Prefer a block that actually contains a test (def test_ / import).
    for b in blocks:
        if "def test" in b or "assert" in b:
            return b.strip() + "\n"
    if blocks:
        return blocks[0].strip() + "\n"
    if "def test" in text and "assert" in text:
        return text.strip() + "\n"
    return None


def _prompt(issue_text: str, hint_block: str) -> str:
    return "\n".join([
        "Write ONE self-contained pytest test that REPRODUCES the bug described "
        "below. The test must FAIL on the current (buggy) code and PASS once the "
        "bug is fixed. Assert the CORRECT expected behavior — do not encode the "
        "current buggy behavior.",
        "",
        "Rules:",
        "- A single file. Import from the repository under test as a normal user "
        "would. No network, no new dependencies, no fixtures beyond stdlib+pytest.",
        "- Name the test function test_repro_* and keep it minimal and "
        "deterministic.",
        "- Output ONLY a single ```python code block with the test. No prose.",
        "",
        (hint_block + "\n" if hint_block else ""),
        "BUG REPORT:",
        issue_text,
    ])


def _default_runner(prompt: str, workdir: str, model: str, timeout: int) -> str:
    argv = (
        "python3", str(DEFAULT_WRAPPER),
        "--workdir", workdir,
        "--read-only",
        "--model", model,
        "--timeout", str(timeout),
        "--no-default-contract",
        prompt,
    )
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return proc.stdout


def _final_text(stdout: str) -> Optional[str]:
    """Mirror reviewer._final_text: handle the wrapper JSON envelope and raw
    text; return None on a genuine timeout/agent error."""
    stripped = (stdout or "").strip()
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return stdout
    if not isinstance(obj, dict):
        return stdout
    if obj.get("timed_out"):
        return None
    last = obj.get("last_result")
    if isinstance(last, dict) and last.get("is_error"):
        return None
    return obj.get("final_text") or ""


def generate(
    issue_text: str,
    repo_workdir: str,
    *,
    hint_block: str = "",
    model: str = "claude-opus-4-8",
    timeout: int = 600,
    path: str = DEFAULT_PATH,
    _runner: Optional[Callable[[str, str, str, int], str]] = None,
) -> Optional[ReproTest]:
    """Generate a reproduction test. Returns None if the model produced nothing
    parseable (best-effort: the caller falls back to regression-only ranking)."""
    runner = _runner or _default_runner
    try:
        raw = runner(_prompt(issue_text, hint_block), repo_workdir, model, timeout)
    except Exception:
        return None
    text = _final_text(raw)
    if text is None:
        return None
    code = extract_test_code(text)
    if not code:
        return None
    return ReproTest(source=code, path=path)


# --- pure F->P predicates (the trusted, tested part) ------------------------

def is_valid_repro(base_results: tr.TestResults) -> bool:
    """A generated test reproduces the bug iff it FAILS on the unpatched base.
    If it passes on base, it doesn't exercise the bug -> untrusted, discard."""
    return base_results.regressions > 0


def flips(base_results: tr.TestResults, cand_results: tr.TestResults) -> bool:
    """The candidate flips a *valid* repro test: red on base, green on candidate."""
    return is_valid_repro(base_results) and cand_results.green
