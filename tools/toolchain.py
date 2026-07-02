#!/usr/bin/env python3
"""pincer toolchain — declarative, multi-language sandbox provisioning.

WHY THIS EXISTS
---------------
Crabbox runs the test command as `crabbox run -- <argv>`, where the command is
`shlex.split` into argv segments joined by `&&` (the only shell operator Crabbox
honors). Shell **redirects** (`>`, `2>&1`), **pipes** (`|`), and **builtins**
(`export`) do NOT survive — they get parsed as literal arguments (an `apt-get`
package named `>/dev/null`, etc.). So any in-VM toolchain provisioning must be a
plain `&&`-chain of real binaries that land on the default PATH.

This module lets a plan/caller declare what a build needs as a list of language
or tool names (or raw apt package names), and prepends an apt-only install
prelude to the test command. apt-only by design: every package lands in
`/usr/bin`, so no `export PATH` is needed, and the whole thing is a clean
`&&`-chain that survives the Crabbox argv contract.

Replaces the old per-plan hand-rolled `curl | sudo tar` / `export PATH` Go
bootstrap, which silently failed under that contract.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

# Language / tool alias -> apt packages. apt-only on purpose (survives the
# crabbox argv contract; binaries land on /usr/bin, no PATH export needed).
# Unknown names are treated as raw apt package names, so `["node", "make",
# "libpq-dev"]` works: known aliases expand, unknowns pass through.
LANGUAGE_PACKAGES = {
    # JavaScript / TypeScript / Node
    "node": ["nodejs", "npm"],
    "nodejs": ["nodejs", "npm"],
    "js": ["nodejs", "npm"],
    "javascript": ["nodejs", "npm"],
    "ts": ["nodejs", "npm"],
    "typescript": ["nodejs", "npm"],
    # Go
    "go": ["golang-go"],
    "golang": ["golang-go"],
    # Python
    "python": ["python3", "python3-pip", "python3-venv"],
    "py": ["python3", "python3-pip", "python3-venv"],
    "python3": ["python3", "python3-pip", "python3-venv"],
    # Rust
    "rust": ["rustc", "cargo"],
    "cargo": ["rustc", "cargo"],
    # JVM
    "java": ["default-jdk"],
    "jvm": ["default-jdk"],
    # Ruby
    "ruby": ["ruby", "ruby-dev"],
    # PHP
    "php": ["php", "php-cli"],
    # C / C++ / make
    "c": ["build-essential"],
    "cpp": ["build-essential"],
    "c++": ["build-essential"],
    "make": ["build-essential"],
    "build-essential": ["build-essential"],
    # Common utilities
    "git": ["git"],
    "curl": ["curl", "ca-certificates"],
}


def resolve_packages(tools: Optional[Iterable[str]]) -> List[str]:
    """Expand a list of language/tool names (or raw apt packages) into a
    deduped, order-preserving list of apt package names. Unknown names pass
    through unchanged so callers can request arbitrary apt packages."""
    pkgs: List[str] = []
    for t in tools or []:
        key = str(t).strip().lower()
        if not key:
            continue
        mapped = LANGUAGE_PACKAGES.get(key, [key])
        for p in mapped:
            if p not in pkgs:
                pkgs.append(p)
    return pkgs


def prelude_for_packages(pkgs: Optional[Iterable[str]]) -> str:
    """Return an apt-only `&&`-chain that installs an explicit apt package list.

    Unlike build_prelude, the input is already-resolved apt package names (e.g.
    the *missing* subset a warm box still needs). Empty -> empty string. Output
    contains NO pipes, redirects, or shell builtins, so it is safe under the
    Crabbox `run -- <argv>` contract.
    """
    pkgs = [p for p in (pkgs or []) if p]
    if not pkgs:
        return ""
    return "sudo apt-get update -qq && sudo apt-get install -y -qq " + " ".join(pkgs)


def build_prelude(tools: Optional[Iterable[str]]) -> str:
    """Return an apt-only `&&`-chain that installs the requested toolchain.

    Empty/None -> empty string. Output contains NO pipes, redirects, or shell
    builtins, so it is safe under the Crabbox `run -- <argv>` contract.
    """
    return prelude_for_packages(resolve_packages(tools))


def apply(test_command: str, tools: Optional[Iterable[str]]) -> str:
    """Prepend the toolchain install prelude to `test_command`.

    No tools -> `test_command` unchanged (backward compatible). The result is a
    single `&&`-chain: `<apt prelude> && <test_command>`.
    """
    prelude = build_prelude(tools)
    if not prelude:
        return test_command
    return f"{prelude} && {test_command}"


def parse_list(value) -> List[str]:
    """Normalize a toolchain spec from CLI/plan into a list of names.

    Accepts a list, a comma/space-separated string, or None.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [tok for tok in str(value).replace(",", " ").split() if tok]
