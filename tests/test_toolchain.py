#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import toolchain as tc


def test_resolve_known_aliases():
    assert tc.resolve_packages(["node"]) == ["nodejs", "npm"]
    assert tc.resolve_packages(["go"]) == ["golang-go"]
    assert tc.resolve_packages(["python"]) == ["python3", "python3-pip", "python3-venv"]


def test_resolve_dedups_and_preserves_order():
    # node + js both map to nodejs/npm -> deduped; go appended after.
    assert tc.resolve_packages(["node", "js", "go"]) == ["nodejs", "npm", "golang-go"]


def test_resolve_unknown_passthrough_as_apt_package():
    # Unknown names are treated as raw apt packages (mix with known aliases).
    assert tc.resolve_packages(["node", "libpq-dev"]) == ["nodejs", "npm", "libpq-dev"]


def test_resolve_case_insensitive_and_blanks():
    assert tc.resolve_packages(["GO", "  ", "Node"]) == ["golang-go", "nodejs", "npm"]


def test_build_prelude_empty():
    assert tc.build_prelude([]) == ""
    assert tc.build_prelude(None) == ""


def test_build_prelude_shape():
    p = tc.build_prelude(["node"])
    assert p == "sudo apt-get update -qq && sudo apt-get install -y -qq nodejs npm"


def test_build_prelude_is_crabbox_argv_safe():
    # The crabbox `run -- <argv>` contract drops pipes, redirects, and builtins.
    # The prelude must contain NONE of them — only `&&`-chained real commands.
    p = tc.build_prelude(["node", "go", "python", "rust"])
    for forbidden in ("|", ">", "<", "2>&1", "export ", "$(", "`"):
        assert forbidden not in p, f"prelude leaked shell construct: {forbidden!r}"


def test_apply_no_tools_is_passthrough():
    assert tc.apply("npm test", []) == "npm test"
    assert tc.apply("go test ./...", None) == "go test ./..."


def test_apply_prepends_prelude():
    out = tc.apply("npm ci && npm test", ["node"])
    assert out == ("sudo apt-get update -qq && sudo apt-get install -y -qq nodejs npm "
                   "&& npm ci && npm test")


def test_parse_list_forms():
    assert tc.parse_list(None) == []
    assert tc.parse_list(["node", "go"]) == ["node", "go"]
    assert tc.parse_list("node,go") == ["node", "go"]
    assert tc.parse_list("node go") == ["node", "go"]
    assert tc.parse_list("node, go ,python") == ["node", "go", "python"]
