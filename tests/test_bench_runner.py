#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.bench.runner as rn
from tools.bench.dataset import Instance


def _inst():
    return Instance(instance_id="o__a-1", repo="o/a", base_commit="abc",
                    problem_statement="The parser drops trailing commas.",
                    hints_text="maybe in parse.py")


def test_build_brief_includes_problem_and_localization():
    brief = rn.build_brief(_inst(), hint_block="Likely files: parse.py\n\n")
    assert "o/a" in brief
    assert "trailing commas" in brief
    assert "Likely files: parse.py" in brief
    assert "REQUIRED" in brief


def test_build_brief_hints_off_by_default():
    brief = rn.build_brief(_inst())
    assert "maybe in parse.py" not in brief  # hints_text excluded by default


def test_build_brief_hints_on_when_requested():
    brief = rn.build_brief(_inst(), use_hints=True)
    assert "maybe in parse.py" in brief


def test_build_brief_sample_diversity_note():
    base = rn.build_brief(_inst(), sample=0)
    s2 = rn.build_brief(_inst(), sample=1)
    assert "Independent attempt" not in base
    assert "Independent attempt #2" in s2
