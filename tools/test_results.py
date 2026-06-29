#!/usr/bin/env python3
"""pincer structured test-result parsing — turn a test run's raw stdout/stderr
into counts the selection cascade can *rank* on, not just a binary pass/fail.

The sandbox gate answers "did the suite go green?" (a gate). The selection
cascade needs a finer signal: *how many* tests regressed, and *which* named
tests failed, so it can prefer the candidate that breaks the fewest
previously-passing tests (the PASS_TO_PASS analog from the SWE-bench harness).

pytest is the primary target (every SWE-bench Python repo, and Pincer's own
suite). Output that doesn't look like pytest degrades gracefully to an
exit-code-only verdict rather than guessing — `parsed=False` tells the caller
"trust the gate verdict, I couldn't extract counts."
"""
from __future__ import annotations

import dataclasses
import re
from typing import List, Optional

# pytest summary line, e.g.:
#   "1 failed, 2 passed in 0.04s"
#   "===== 3 passed, 1 warning in 1.23s ====="
#   "10 passed in 0.51s"
#   "1 failed, 1 error in 0.10s"
_SUMMARY_TOKEN = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)\b")
# A pytest FAILED/ERROR line names the test, e.g.:
#   "FAILED tests/test_x.py::test_foo - AssertionError: ..."
#   "ERROR tests/test_x.py::test_bar"
_NAMED = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)


@dataclasses.dataclass(frozen=True)
class TestResults:
    """Structured outcome of a single test run."""
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failed_names: tuple = ()      # tuple[str, ...] of nodeids that FAILED/ERROR'd
    parsed: bool = False          # True iff we recognized a real summary line
    exit_code: Optional[int] = None

    @property
    def green(self) -> bool:
        """No failures and no errors. If we couldn't parse counts, fall back to
        the exit code (0 == green); if neither is available, not green."""
        if self.parsed:
            return self.failed == 0 and self.errors == 0
        if self.exit_code is not None:
            return self.exit_code == 0
        return False

    @property
    def regressions(self) -> int:
        """Total failing + erroring tests — the cascade's primary rank key
        (fewer is better). Mirrors the harness PASS_TO_PASS check."""
        return self.failed + self.errors

    def summary(self) -> str:
        if not self.parsed and self.exit_code is not None:
            return f"unparsed (exit {self.exit_code})"
        bits = [f"{self.passed} passed"]
        if self.failed:
            bits.append(f"{self.failed} failed")
        if self.errors:
            bits.append(f"{self.errors} error{'s' if self.errors != 1 else ''}")
        if self.skipped:
            bits.append(f"{self.skipped} skipped")
        return ", ".join(bits)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["failed_names"] = list(self.failed_names)
        d["green"] = self.green
        d["regressions"] = self.regressions
        return d


def parse(output: str, exit_code: Optional[int] = None) -> TestResults:
    """Parse a (possibly truncated) test-run blob into TestResults.

    Strategy: scan every recognized summary token (the *last* occurrence of each
    kind wins, since pytest prints the authoritative summary last), and collect
    named FAILED/ERROR nodeids. If no summary token is found at all, return an
    unparsed result carrying the exit code so the caller can fall back to it.
    """
    text = output or ""
    counts = {"passed": 0, "failed": 0, "error": 0, "errors": 0,
              "skipped": 0, "xfailed": 0, "xpassed": 0}
    found = False
    for m in _SUMMARY_TOKEN.finditer(text):
        found = True
        counts[m.group(2)] = int(m.group(1))

    failed_names = tuple(dict.fromkeys(_NAMED.findall(text)))  # de-dup, keep order

    if not found:
        # No pytest summary. If there are named failures we can still report
        # them; otherwise this is a non-pytest / unparseable run.
        if failed_names:
            return TestResults(
                failed=len(failed_names), failed_names=failed_names,
                parsed=True, exit_code=exit_code,
            )
        return TestResults(parsed=False, exit_code=exit_code)

    errors = counts["error"] + counts["errors"]
    return TestResults(
        passed=counts["passed"],
        failed=counts["failed"],
        errors=errors,
        skipped=counts["skipped"],
        failed_names=failed_names,
        parsed=True,
        exit_code=exit_code,
    )


def names(results: TestResults) -> List[str]:
    return list(results.failed_names)
