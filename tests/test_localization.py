#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import localization as loc


def test_extract_terms_drops_stopwords_and_short():
    terms = loc.extract_terms("The parser should return the correct timezone value")
    assert "parser" in terms
    assert "timezone" in terms
    assert "correct" in terms
    # stopwords / short tokens dropped
    assert "the" not in terms
    assert "should" not in terms
    assert "value" not in terms  # in stoplist


def test_rank_symbols_name_match_beats_docstring_match():
    src = {
        "a.py": (
            "def parse_timezone(s):\n"
            "    'convert a string'\n"
            "    return s\n"
            "\n"
            "def unrelated():\n"
            "    'this mentions timezone in its docstring only'\n"
            "    return 1\n"
            "\n"
            "class TimezoneCache:\n"
            "    'holds zones'\n"
            "    pass\n"
        )
    }
    syms = loc.rank_symbols(["timezone", "parse"], src)
    names = [s.name for s in syms]
    # parse_timezone matches both 'parse' and 'timezone' in its name -> top
    assert names[0] == "parse_timezone"
    assert "TimezoneCache" in names
    assert "unrelated" in names  # docstring-only match still surfaces
    # name match (>=2) outranks docstring-only match (1)
    assert syms[0].score > syms[-1].score


def test_rank_symbols_zero_overlap_dropped():
    src = {"a.py": "def totally_different():\n    return 0\n"}
    assert loc.rank_symbols(["timezone"], src) == []


def test_rank_symbols_skips_non_python_and_unparseable():
    src = {
        "a.go": "func parseTimezone() {}",          # not python -> skipped
        "b.py": "def parse_timezone(:::\n",          # syntax error -> skipped
        "c.py": "def parse_timezone():\n    pass\n",  # ok
    }
    syms = loc.rank_symbols(["parse", "timezone"], src)
    assert [s.path for s in syms] == ["c.py"]


def test_localization_hint_block_and_empty():
    empty = loc.Localization()
    assert empty.is_empty()
    assert empty.hint_block() == ""

    l = loc.Localization(
        files=("src/tz.py",),
        symbols=(loc.Symbol("src/tz.py", "parse_timezone", "function", 12, 4),),
        terms=("timezone",),
    )
    hb = l.hint_block()
    assert "src/tz.py" in hb
    assert "parse_timezone" in hb
    assert "src/tz.py:12" in hb
    assert hb.endswith("\n\n")


def test_localize_endtoend_on_temp_repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "tz.py").write_text(
        "def parse_timezone(s):\n    'parse a timezone'\n    return s\n"
    )
    (tmp_path / "src" / "other.py").write_text("def noop():\n    return 0\n")
    out = loc.localize(str(tmp_path),
                       "parse_timezone returns wrong timezone for DST input")
    assert "src/tz.py" in out.files
    assert any(s.name == "parse_timezone" for s in out.symbols)
