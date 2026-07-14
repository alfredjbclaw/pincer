"""Leash features: effort ladder, resume retries, empty-diff guard, lessons."""
from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import runtime_adapter  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.invalid")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("baseline\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _null_gate(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_adapter.global_gate, "slot",
        lambda **_kwargs: contextlib.nullcontext(),
    )


def test_effort_ladder_escalates_but_never_auto_max() -> None:
    assert runtime_adapter.effort_for_attempt("medium", 1) == "medium"
    assert runtime_adapter.effort_for_attempt("medium", 2) == "high"
    assert runtime_adapter.effort_for_attempt("medium", 3) == "xhigh"
    assert runtime_adapter.effort_for_attempt("medium", 5) == "xhigh"
    assert runtime_adapter.effort_for_attempt("high", 2) == "high"
    assert runtime_adapter.effort_for_attempt("xhigh", 4) == "xhigh"
    assert runtime_adapter.effort_for_attempt("max", 3) == "max"


def test_fresh_codex_command_carries_effort_json_and_plugin_kill(monkeypatch, tmp_path) -> None:
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        output_index = cmd.index("--output-last-message") + 1
        Path(cmd[output_index]).write_text("STATUS: done\nFILES: a\nVALIDATION: ok\nNEXT: none")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(runtime_adapter.shutil, "which", lambda _n: "/usr/bin/codex")
    monkeypatch.setattr(runtime_adapter.subprocess, "run", fake_run)

    runtime_adapter._run_codex("task", tmp_path, runtime_adapter.RuntimeConfig(), effort="medium")

    cmd = captured[0]
    assert "--json" in cmd
    assert "--cd" in cmd
    assert 'model_reasoning_effort="medium"' in cmd
    assert "features.plugins=false" in cmd
    assert "features.plugin_hooks=false" in cmd


def test_resume_codex_command_keeps_cage_without_profile_flags(monkeypatch, tmp_path) -> None:
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        output_index = cmd.index("--output-last-message") + 1
        Path(cmd[output_index]).write_text("STATUS: done\nFILES: a\nVALIDATION: ok\nNEXT: none")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(runtime_adapter.shutil, "which", lambda _n: "/usr/bin/codex")
    monkeypatch.setattr(runtime_adapter.subprocess, "run", fake_run)

    runtime_adapter._run_codex(
        "fix only this", tmp_path, runtime_adapter.RuntimeConfig(),
        effort="high", resume_thread="t-42",
    )

    cmd = captured[0]
    assert cmd[:4] == ["codex", "exec", "resume", "t-42"]
    assert "--cd" not in cmd
    assert "--sandbox" not in cmd
    assert 'sandbox_mode="workspace-write"' in cmd
    assert 'approval_policy="never"' in cmd
    assert 'model_reasoning_effort="high"' in cmd
    # Resume prompts carry only feedback: no ulw prefix, no re-sent contract.
    assert cmd[-1] == "fix only this"


def test_thread_id_parsed_from_json_events(monkeypatch, tmp_path) -> None:
    def fake_run(cmd, **kwargs):
        output_index = cmd.index("--output-last-message") + 1
        Path(cmd[output_index]).write_text("STATUS: done\nFILES: a\nVALIDATION: ok\nNEXT: none")
        stdout = json.dumps({"type": "thread.started", "thread_id": "019f-xyz"}) + "\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(runtime_adapter.shutil, "which", lambda _n: "/usr/bin/codex")
    monkeypatch.setattr(runtime_adapter.subprocess, "run", fake_run)

    _text, _err, code, thread = runtime_adapter._run_codex(
        "task", tmp_path, runtime_adapter.RuntimeConfig()
    )
    assert code == 0
    assert thread == "019f-xyz"


def test_final_text_falls_back_to_jsonl_agent_message() -> None:
    stdout = "\n".join([
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "early note"}}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "STATUS: blocked"}}),
    ])
    assert runtime_adapter._final_from_jsonl(stdout) == "STATUS: blocked"


def test_dispatch_empty_diff_guard_downgrades_done_on_clean_tree(monkeypatch, tmp_path) -> None:
    repo = _make_repo(tmp_path)
    _null_gate(monkeypatch)
    monkeypatch.setattr(
        runtime_adapter, "_execute",
        lambda *_a, **_k: ("STATUS: done\nFILES: a.txt\nVALIDATION: ok\nNEXT: none", "", 0, "t-1"),
    )
    cfg = runtime_adapter.RuntimeConfig(fallback_enabled=False)

    result = runtime_adapter.dispatch("task", workdir=repo, config=cfg)

    assert result.status == "error"
    assert result.empty_diff is True
    assert "empty_diff" in result.next
    assert result.thread_id == "t-1"


def test_dispatch_honest_no_changes_is_not_penalized(monkeypatch, tmp_path) -> None:
    repo = _make_repo(tmp_path)
    _null_gate(monkeypatch)
    monkeypatch.setattr(
        runtime_adapter, "_execute",
        lambda *_a, **_k: ("STATUS: no_changes\nFILES: none\nVALIDATION: ok\nNEXT: none", "", 0, None),
    )
    cfg = runtime_adapter.RuntimeConfig(fallback_enabled=False)

    result = runtime_adapter.dispatch("task", workdir=repo, config=cfg)

    assert result.status == "no_changes"
    assert result.empty_diff is False


def test_dispatch_done_with_dirty_tree_passes(monkeypatch, tmp_path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "a.txt").write_text("changed\n")
    _null_gate(monkeypatch)
    monkeypatch.setattr(
        runtime_adapter, "_execute",
        lambda *_a, **_k: ("STATUS: done\nFILES: a.txt\nVALIDATION: ok\nNEXT: none", "", 0, None),
    )
    cfg = runtime_adapter.RuntimeConfig(fallback_enabled=False)

    result = runtime_adapter.dispatch("task", workdir=repo, config=cfg)

    assert result.status == "done"
    assert result.empty_diff is False


def test_dispatch_resume_never_vendor_falls_back(monkeypatch, tmp_path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "a.txt").write_text("changed\n")
    _null_gate(monkeypatch)
    calls: list[str] = []

    def fake_execute(runtime, *_a, **_k):
        calls.append(runtime)
        return ("STATUS: blocked\nFILES: none\nVALIDATION: none\nNEXT: stuck", "", 0, "t-9")

    monkeypatch.setattr(runtime_adapter, "_execute", fake_execute)
    cfg = runtime_adapter.RuntimeConfig(fallback_enabled=True)

    result = runtime_adapter.dispatch("fix", workdir=repo, config=cfg, attempt=2,
                                      resume_thread="t-9")

    assert calls == ["codex"]
    assert result.fallback_used is False
    assert result.status == "blocked"


def test_dispatch_ladder_passes_escalated_effort(monkeypatch, tmp_path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "a.txt").write_text("changed\n")
    _null_gate(monkeypatch)
    seen: list[str] = []

    def fake_execute(_runtime, _prompt, _workdir, _cfg, effort, _resume):
        seen.append(effort)
        return ("STATUS: done\nFILES: a.txt\nVALIDATION: ok\nNEXT: none", "", 0, None)

    monkeypatch.setattr(runtime_adapter, "_execute", fake_execute)
    cfg = runtime_adapter.RuntimeConfig(fallback_enabled=False)

    runtime_adapter.dispatch("task", workdir=repo, config=cfg, attempt=3)
    assert seen == ["xhigh"]

    runtime_adapter.dispatch(
        "task", workdir=repo,
        config=runtime_adapter.RuntimeConfig(fallback_enabled=False, effort_ladder=False),
        attempt=3,
    )
    assert seen[-1] == "medium"


def test_worker_contract_names_scope_request_and_empty_diff() -> None:
    assert "SCOPE-REQUEST:" in runtime_adapter.WORKER_CONTRACT
    assert "no_changes" in runtime_adapter.WORKER_CONTRACT


def test_lessons_block_is_optional_and_bounded(monkeypatch, tmp_path) -> None:
    missing = tmp_path / "missing.md"
    monkeypatch.setenv("PINCER_LESSONS", str(missing))
    assert runtime_adapter.worker_contract() == runtime_adapter.WORKER_CONTRACT

    lessons = tmp_path / "LESSONS.md"
    lessons.write_text("1. never invent fixtures\n" + "x" * 100_000)
    monkeypatch.setenv("PINCER_LESSONS", str(lessons))
    contract = runtime_adapter.worker_contract()
    assert "never invent fixtures" in contract
    assert len(contract) <= len(runtime_adapter.WORKER_CONTRACT) + runtime_adapter.LESSONS_LIMIT + 64
