from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import reviewer  # noqa: E402


def test_review_parses_approval(monkeypatch) -> None:
    def fake_run(cmd) -> tuple[str, int]:
        return (
            "\n".join(
                [
                    "VERDICT: approve",
                    "REASONS: tests and diff satisfy the criteria",
                    "BLOCKERS: none",
                ]
            ),
            0,
        )

    # Given: the reviewer wrapper returns an approval contract.
    monkeypatch.setattr(reviewer, "_run_reviewer", fake_run)

    # When: review parses the wrapper output.
    verdict = reviewer.review("diff", "issue", "criteria")

    # Then: approval and an empty blocker list are returned.
    assert verdict.verdict == "approve"
    assert verdict.blockers == []


def test_review_parses_rejection_blockers(monkeypatch) -> None:
    def fake_run(cmd) -> tuple[str, int]:
        return (
            "\n".join(
                [
                    "VERDICT: reject",
                    "REASONS:",
                    "- lacks failing-first proof",
                    "BLOCKERS:",
                    "- missing tests",
                    "- unsafe publication decision",
                ]
            ),
            0,
        )

    # Given: the reviewer wrapper returns a rejection with blockers.
    monkeypatch.setattr(reviewer, "_run_reviewer", fake_run)

    # When: review parses the wrapper output.
    verdict = reviewer.review("diff", "issue", "criteria")

    # Then: rejection blockers are preserved without list markers.
    assert verdict.verdict == "reject"
    assert verdict.blockers == ["missing tests", "unsafe publication decision"]


def test_review_fails_closed_on_unparseable_output(monkeypatch) -> None:
    def fake_run(cmd) -> tuple[str, int]:
        return ("looks fine", 0)

    # Given: the reviewer wrapper returns garbage.
    monkeypatch.setattr(reviewer, "_run_reviewer", fake_run)

    # When: review attempts to parse the output.
    verdict = reviewer.review("diff", "issue", "criteria")

    # Then: the gate rejects by default.
    assert verdict.verdict == "reject"
    assert verdict.blockers == ["reviewer did not return a parseable verdict"]


def test_review_fails_closed_on_timeout(monkeypatch) -> None:
    def fake_run(cmd) -> tuple[str, int]:
        raise subprocess.TimeoutExpired(cmd, cmd.timeout)

    # Given: the reviewer wrapper times out.
    monkeypatch.setattr(reviewer, "_run_reviewer", fake_run)

    # When: review handles the subprocess timeout.
    verdict = reviewer.review("diff", "issue", "criteria")

    # Then: the gate rejects by default.
    assert verdict.verdict == "reject"
    assert verdict.blockers == ["reviewer did not return a parseable verdict"]


def test_review_uses_overridable_mcp_config(monkeypatch, tmp_path) -> None:
    seen_cmd: list[str] = []
    mcp_config = tmp_path / "mcp.json"

    def fake_run(cmd) -> tuple[str, int]:
        seen_cmd.extend(cmd.argv)
        return ("VERDICT: approve\nREASONS: ok\nBLOCKERS: none", 0)

    # Given: a caller provides a scoped MCP config path.
    monkeypatch.setattr(reviewer, "_run_reviewer", fake_run)

    # When: review builds the reviewer command.
    verdict = reviewer.review("diff", "issue", "criteria", mcp_config_path=str(mcp_config))

    # Then: the command includes the override and still parses.
    assert verdict.verdict == "approve"
    assert "--read-only" in seen_cmd
    assert "--mcp-config" not in seen_cmd
    assert any(str(mcp_config) in part for part in seen_cmd)
