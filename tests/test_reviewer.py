from __future__ import annotations

import json
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


def test_review_parses_wrapper_json_envelope_despite_nonzero_exit(monkeypatch) -> None:
    # The real claude-code-wrapper returns a JSON envelope and a non-zero exit
    # on read-only runs (acpx exit 5 / missing STATUS marker) even when the
    # model answered. The verdict lives in final_text and must be honored.
    envelope = json.dumps({
        "ok": False,
        "returncode": 5,
        "timed_out": False,
        "last_result": {"is_error": False},
        "final_text": "VERDICT: approve\nREASONS: meets criteria\nBLOCKERS: none",
    })

    def fake_run(cmd) -> tuple[str, int]:
        return (envelope, 5)  # non-zero exit, valid verdict inside

    monkeypatch.setattr(reviewer, "_run_reviewer", fake_run)
    verdict = reviewer.review("diff", "issue", "criteria")
    assert verdict.verdict == "approve"
    assert verdict.blockers == []


def test_review_fails_closed_on_wrapper_timeout_envelope(monkeypatch) -> None:
    envelope = json.dumps({"ok": False, "timed_out": True, "final_text": "VERDICT: approve\nREASONS: x\nBLOCKERS: none"})
    monkeypatch.setattr(reviewer, "_run_reviewer", lambda cmd: (envelope, 124))
    verdict = reviewer.review("diff", "issue", "criteria")
    # Genuine timeout -> reject even though final_text looks approving.
    assert verdict.verdict == "reject"
    assert verdict.blockers == ["reviewer did not return a parseable verdict"]


def test_review_runs_in_repo_workdir_with_scoped_config(monkeypatch, tmp_path) -> None:
    seen_cmd: list[str] = []
    config_present_at_runtime: dict[str, bool] = {}
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(cmd) -> tuple[str, int]:
        seen_cmd.extend(cmd.argv)
        # The scoped .mcp.json must exist IN the repo while the reviewer runs.
        config_present_at_runtime["present"] = (repo / ".mcp.json").exists()
        return ("VERDICT: approve\nREASONS: ok\nBLOCKERS: none", 0)

    monkeypatch.setattr(reviewer, "_run_reviewer", fake_run)

    verdict = reviewer.review("diff", "issue", "criteria", repo_workdir=str(repo))

    # Then: ran with the repo as workdir, config present during the run...
    assert verdict.verdict == "approve"
    idx = seen_cmd.index("--workdir")
    assert seen_cmd[idx + 1] == str(repo)
    assert config_present_at_runtime["present"] is True
    # ...and the repo is left clean afterward (no leftover .mcp.json).
    assert not (repo / ".mcp.json").exists()


def test_review_restores_preexisting_mcp_config(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".mcp.json").write_text('{"original": true}')

    monkeypatch.setattr(reviewer, "_run_reviewer",
                        lambda cmd: ("VERDICT: approve\nREASONS: ok\nBLOCKERS: none", 0))

    reviewer.review("diff", "issue", "criteria", repo_workdir=str(repo))

    # Then: the caller's original .mcp.json is restored byte-for-byte.
    assert (repo / ".mcp.json").read_text() == '{"original": true}'


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
