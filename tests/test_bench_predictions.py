#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # pincer root

import tools.bench.predictions as pr

SAMPLE = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    "-    return wrong\n"
    "+    return right\n"
    "diff --git a/tests/test_app.py b/tests/test_app.py\n"
    "index 3333333..4444444 100644\n"
    "--- a/tests/test_app.py\n"
    "+++ b/tests/test_app.py\n"
    "@@ -1,2 +1,3 @@\n"
    "+    assert right\n"
)


def test_split_diff_by_file():
    sections = pr.split_diff_by_file(SAMPLE)
    assert [p for p, _ in sections] == ["src/app.py", "tests/test_app.py"]
    assert sections[0][1].startswith("diff --git a/src/app.py")
    assert sections[0][1].endswith("\n")


def test_is_test_file():
    assert pr.is_test_file("tests/test_app.py")
    assert pr.is_test_file("pkg/test_thing.py")
    assert pr.is_test_file("a/b/conftest.py")
    assert not pr.is_test_file("src/app.py")
    assert not pr.is_test_file("src/testing_utils.py")  # not a real test file


def test_strip_test_sections_keeps_source_only():
    out = pr.strip_test_sections(SAMPLE)
    assert "src/app.py" in out
    assert "tests/test_app.py" not in out
    assert "return right" in out


def test_extract_model_patch_preserves_trailing_newline():
    out = pr.extract_model_patch(SAMPLE)
    assert out.endswith("\n")
    assert "tests/test_app.py" not in out  # tests stripped by default


def test_extract_model_patch_can_keep_tests():
    out = pr.extract_model_patch(SAMPLE, exclude_tests=False)
    assert "tests/test_app.py" in out


def test_empty_patch_stays_empty():
    assert pr.extract_model_patch("") == ""
    assert pr.split_diff_by_file("") == []


def test_jsonl_roundtrip(tmp_path):
    preds = [pr.Prediction("a__b-1", "diff...\n"), pr.Prediction("a__b-2", "")]
    path = str(tmp_path / "preds.jsonl")
    pr.write_jsonl(preds, path)
    rows = pr.read_jsonl(path)
    assert len(rows) == 2
    assert rows[0] == {"instance_id": "a__b-1", "model_name_or_path": "pincer",
                       "model_patch": "diff...\n"}
    assert rows[1]["instance_id"] == "a__b-2"
