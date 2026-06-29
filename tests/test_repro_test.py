#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import repro_test as rt
import test_results as tr


def test_extract_fenced_python_block():
    text = (
        "Here is the test:\n"
        "```python\n"
        "def test_repro_bug():\n"
        "    assert add(1, 2) == 3\n"
        "```\n"
    )
    code = rt.extract_test_code(text)
    assert code is not None
    assert "def test_repro_bug" in code
    assert code.endswith("\n")


def test_extract_prefers_block_with_test():
    text = (
        "```python\nimport os\n```\n"
        "```python\ndef test_repro_x():\n    assert True\n```\n"
    )
    code = rt.extract_test_code(text)
    assert "def test_repro_x" in code


def test_extract_raw_fallback():
    text = "def test_repro_y():\n    assert 1 == 1\n"
    assert rt.extract_test_code(text) is not None


def test_extract_none_on_prose():
    assert rt.extract_test_code("I could not write a test, sorry.") is None
    assert rt.extract_test_code("") is None


def test_generate_with_injected_runner():
    def fake_runner(prompt, workdir, model, timeout):
        assert "REPRODUCES" in prompt
        return '{"final_text": "```python\\ndef test_repro_z():\\n    assert True\\n```"}'
    out = rt.generate("bug: add() is wrong", "/tmp/repo", _runner=fake_runner)
    assert out is not None
    assert "test_repro_z" in out.source
    assert out.path == rt.DEFAULT_PATH


def test_generate_none_on_timeout_envelope():
    def fake_runner(prompt, workdir, model, timeout):
        return '{"timed_out": true}'
    assert rt.generate("bug", "/tmp/repo", _runner=fake_runner) is None


def test_generate_none_on_agent_error():
    def fake_runner(prompt, workdir, model, timeout):
        return '{"last_result": {"is_error": true}}'
    assert rt.generate("bug", "/tmp/repo", _runner=fake_runner) is None


def test_generate_none_on_unparseable_reply():
    def fake_runner(prompt, workdir, model, timeout):
        return '{"final_text": "no code here"}'
    assert rt.generate("bug", "/tmp/repo", _runner=fake_runner) is None


def test_is_valid_repro():
    red = tr.parse("1 failed, 0 passed in 0.01s", exit_code=1)
    green = tr.parse("1 passed in 0.01s", exit_code=0)
    assert rt.is_valid_repro(red)
    assert not rt.is_valid_repro(green)   # passes on base -> doesn't reproduce


def test_flips_requires_valid_base_and_green_candidate():
    base_red = tr.parse("1 failed in 0.01s", exit_code=1)
    base_green = tr.parse("1 passed in 0.01s", exit_code=0)
    cand_green = tr.parse("1 passed in 0.01s", exit_code=0)
    cand_red = tr.parse("1 failed in 0.01s", exit_code=1)

    assert rt.flips(base_red, cand_green)       # red->green: flipped
    assert not rt.flips(base_red, cand_red)     # still red: not flipped
    assert not rt.flips(base_green, cand_green)  # base never reproduced -> untrusted
