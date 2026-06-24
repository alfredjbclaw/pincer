from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import runtime_adapter  # noqa: E402


def test_run_codex_prepends_ulw_when_ultrawork_enabled(monkeypatch, tmp_path) -> None:
    captured_prompt: list[str] = []

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured_prompt.append(cmd[-1])
        output_index = cmd.index("--output-last-message") + 1
        Path(cmd[output_index]).write_text("STATUS: done\nFILES: none\nVALIDATION: ok\nNEXT: none")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # Given: Codex is available and ultrawork mode is enabled by default.
    monkeypatch.setattr(runtime_adapter.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(runtime_adapter.subprocess, "run", fake_run)

    # When: the Codex runtime is invoked.
    runtime_adapter._run_codex("implement task", tmp_path, runtime_adapter.RuntimeConfig())

    # Then: only the prompt sent to Codex starts with the ultrawork trigger.
    assert captured_prompt[0].startswith("ulw: implement task\n\n")


def test_run_codex_does_not_prepend_ulw_when_disabled(monkeypatch, tmp_path) -> None:
    captured_prompt: list[str] = []

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured_prompt.append(cmd[-1])
        output_index = cmd.index("--output-last-message") + 1
        Path(cmd[output_index]).write_text("STATUS: done\nFILES: none\nVALIDATION: ok\nNEXT: none")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # Given: Codex is available and ultrawork mode is disabled.
    monkeypatch.setattr(runtime_adapter.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(runtime_adapter.subprocess, "run", fake_run)
    config = runtime_adapter.RuntimeConfig(ultrawork=False)

    # When: the Codex runtime is invoked.
    runtime_adapter._run_codex("implement task", tmp_path, config)

    # Then: the prompt remains unchanged before the worker contract.
    assert captured_prompt[0].startswith("implement task\n\n")
    assert not captured_prompt[0].startswith("ulw: ")


def test_run_claude_code_never_prepends_ulw(monkeypatch, tmp_path) -> None:
    captured_prompt: list[str] = []
    wrapper = tmp_path / "claude-code-wrapper.py"
    wrapper.write_text("")

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured_prompt.append(cmd[-1])
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="STATUS: done\nFILES: none\nVALIDATION: ok\nNEXT: none",
            stderr="",
        )

    # Given: Claude Code is invoked with ultrawork enabled on the shared config.
    monkeypatch.setattr(runtime_adapter, "CLAUDE_CODE_WRAPPER", wrapper)
    monkeypatch.setattr(runtime_adapter.subprocess, "run", fake_run)
    config = runtime_adapter.RuntimeConfig(ultrawork=True)

    # When: the Claude fallback runtime is invoked.
    runtime_adapter._run_claude_code("implement task", tmp_path, config)

    # Then: the prompt sent to Claude Code does not include the Codex-only trigger.
    assert captured_prompt[0].startswith("implement task\n\n")
    assert not captured_prompt[0].startswith("ulw: ")


def test_unwrap_wrapper_output_extracts_final_text():
    import json as _json
    import runtime_adapter as ra
    env = _json.dumps({"ok": True, "returncode": 1, "timed_out": False,
                       "last_result": {"is_error": False},
                       "final_text": "STATUS: done\nFILES: src/calc.py\nVALIDATION: pytest\nNEXT: none"})
    text, rc = ra._unwrap_wrapper_output(env, 1)
    assert "STATUS: done" in text
    assert rc == 0  # benign wrapper non-zero exit must not mask a real completion
    c = ra._extract_contract(text)
    assert c["status"] == "done" and c["files"] == ["src/calc.py"]


def test_unwrap_wrapper_output_preserves_timeout():
    import json as _json
    import runtime_adapter as ra
    env = _json.dumps({"timed_out": True, "final_text": "STATUS: done"})
    text, rc = ra._unwrap_wrapper_output(env, 124)
    assert rc == 124  # genuine timeout stays a failure


def test_unwrap_wrapper_output_passthrough_raw_text():
    import runtime_adapter as ra
    text, rc = ra._unwrap_wrapper_output("STATUS: done\nFILES: none", 0)
    assert "STATUS: done" in text and rc == 0
