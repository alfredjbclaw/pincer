#!/usr/bin/env python3
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.bench.dataset as ds


def test_as_list_handles_json_string_and_list():
    assert ds._as_list('["a::b", "c::d"]') == ["a::b", "c::d"]
    assert ds._as_list(["x"]) == ["x"]
    assert ds._as_list(None) == []
    assert ds._as_list("not json") == ["not json"]


def test_instance_from_row_with_string_encoded_splits():
    row = {
        "instance_id": "sympy__sympy-20590",
        "repo": "sympy/sympy",
        "base_commit": "abc123",
        "problem_statement": "Symbol instances has __dict__ ...",
        "FAIL_TO_PASS": '["test_x::test_a"]',
        "PASS_TO_PASS": '["test_x::test_b", "test_x::test_c"]',
        "version": "1.7",
    }
    inst = ds.Instance.from_row(row)
    assert inst.repo == "sympy/sympy"
    assert inst.clone_url == "https://github.com/sympy/sympy.git"
    assert inst.fail_to_pass == ("test_x::test_a",)
    assert inst.pass_to_pass == ("test_x::test_b", "test_x::test_c")
    assert inst.version == "1.7"


def test_load_local_jsonl(tmp_path):
    rows = [
        {"instance_id": "a-1", "repo": "o/a", "base_commit": "c1",
         "problem_statement": "p1", "FAIL_TO_PASS": ["t::a"], "PASS_TO_PASS": []},
        {"instance_id": "a-2", "repo": "o/a", "base_commit": "c2",
         "problem_statement": "p2", "FAIL_TO_PASS": ["t::b"], "PASS_TO_PASS": []},
    ]
    f = tmp_path / "insts.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows))
    insts = ds.load_local(str(f))
    assert len(insts) == 2
    assert insts[0].instance_id == "a-1"
    assert ds.load_local(str(f), limit=1)[0].instance_id == "a-1"


def test_load_local_json_list(tmp_path):
    f = tmp_path / "insts.json"
    f.write_text(json.dumps([
        {"instance_id": "a-1", "repo": "o/a", "base_commit": "c1", "problem_statement": "p"}
    ]))
    insts = ds.load_local(str(f))
    assert len(insts) == 1
    assert insts[0].hints_text == ""  # defaulted


def test_load_dispatches_to_local_for_file_path(tmp_path):
    f = tmp_path / "insts.jsonl"
    f.write_text(json.dumps({"instance_id": "a-1", "repo": "o/a", "base_commit": "c"}))
    insts = ds.load(str(f))
    assert insts[0].instance_id == "a-1"
