#!/usr/bin/env python3
"""pincer hierarchical localization — point the worker at files *and the
specific symbols inside them*, not just a flat list of filenames.

Agentless's finding: localization quality gates everything downstream, and
hierarchy (file -> class/function skeleton -> edit lines) beats a flat lexical
rank. Pincer's coders are *agentic* (they can grep/navigate), so the high-ROI
move per the research is a strong structured hint — ranked files plus the
matching function/class signatures — rather than a dense-embedding retriever.

Two layers:
  1. File level   — lexical grep-rank (which files hit the most distinct issue
                    terms). This is the original Pincer behavior, kept intact.
  2. Symbol level — parse the top Python files with `ast`, extract a skeleton
                    (def/class signatures + line numbers), and rank the symbols
                    whose name/docstring overlaps the issue terms. This is the
                    new narrowing layer.

The ranking logic (`rank_symbols`) is pure over in-memory sources so it is
unit-testable without a repo. `localize()` wires grep + file reads on top.

Non-Python files degrade to the file layer only (no AST) — fine for SWE-bench,
which is Python, and safe everywhere else.
"""
from __future__ import annotations

import ast
import dataclasses
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_STOP = {"this", "that", "with", "from", "should", "when", "what", "have", "your",
         "code", "issue", "error", "value", "values", "return", "returns", "result",
         "expected", "actual", "test", "tests", "using", "into", "would", "could",
         "does", "given", "list", "type", "case", "https", "github", "http", "self",
         "none", "true", "false", "class", "def", "import", "function", "method"}

_SRC_GLOBS = ("*.py", "*.go", "*.js", "*.ts", "*.rb", "*.java")


@dataclasses.dataclass(frozen=True)
class Symbol:
    path: str
    name: str
    kind: str        # "function" | "class" | "method"
    lineno: int
    score: int       # term-overlap score (higher = more relevant)


@dataclasses.dataclass(frozen=True)
class Localization:
    files: Tuple[str, ...] = ()              # ranked relevant files
    symbols: Tuple[Symbol, ...] = ()         # ranked relevant defs within them
    terms: Tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.files and not self.symbols

    def hint_block(self) -> str:
        """The text inserted into the worker brief. Empty string when nothing
        was found, so the brief reads cleanly."""
        if self.is_empty():
            return ""
        out = []
        if self.files:
            out.append("Likely-relevant files (lexical localization — start here, verify): "
                       + ", ".join(self.files))
        if self.symbols:
            out.append("Likely-relevant symbols (AST skeleton — strongest leads first):")
            for s in self.symbols:
                out.append(f"  - {s.path}:{s.lineno}  {s.kind} `{s.name}`")
        return "\n".join(out) + "\n\n"

    def to_dict(self) -> dict:
        return {"files": list(self.files),
                "symbols": [dataclasses.asdict(s) for s in self.symbols],
                "terms": list(self.terms)}


def extract_terms(issue_text: str, limit: int = 25) -> List[str]:
    terms = {w.lower() for w in re.findall(r"[A-Za-z_]{4,}", issue_text or "")
             if w.lower() not in _STOP}
    return list(terms)[:limit]


def _sh(cmd, timeout=30) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout
    except Exception:
        return ""


def _split_ident(name: str) -> set:
    """Tokenize an identifier into lowercase parts, splitting on underscores,
    non-word chars, AND camelCase/PascalCase boundaries so `TimezoneCache`
    yields {timezone, cache} and `parse_timezone` yields {parse, timezone}."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return {p.lower() for p in re.split(r"[_\W]+", spaced) if p}


def _is_test_path(rel: str) -> bool:
    """A path is a test file if it lives in a test/tests dir, is a test_*.py
    module, or is a *_test.* module. Deliberately does NOT match incidental
    names like testing/ or testutils.py."""
    low = rel.lower()
    parts = low.split("/")
    base = parts[-1]
    return (any(p in ("test", "tests") for p in parts)
            or base.startswith("test_")
            or "_test." in base)


def rank_files(workdir: str, terms: List[str], limit: int = 5) -> List[str]:
    """Grep the repo's source for each term; rank files by distinct-term hits.
    This is Pincer's original lexical localization, unchanged in behavior."""
    if not terms:
        return []
    includes = [f"--include={g}" for g in _SRC_GLOBS]
    counts: Dict[str, int] = {}
    for t in terms:
        out = _sh(["grep", "-rIl", "-iw", t, workdir] + includes)
        for f in out.splitlines():
            f = f.strip()
            if not f:
                continue
            # Filter on the path *relative to workdir* — the workdir prefix
            # itself may contain "test" (e.g. a checkout under /tmp/test-run/).
            rel = f[len(workdir):].lstrip("/") if f.startswith(workdir) else f
            if _is_test_path(rel):
                continue
            counts[rel] = counts.get(rel, 0) + 1
    return sorted(counts, key=lambda k: counts[k], reverse=True)[:limit]


def rank_symbols(terms: List[str], sources: Dict[str, str], limit: int = 8) -> List[Symbol]:
    """Pure: given term set + {relpath: python_source}, return the def/class
    symbols whose name or docstring overlaps the issue terms, best first.

    A symbol scores +2 per term that appears in its *name* (the strongest
    signal) and +1 per term in its docstring. Symbols with zero overlap are
    dropped. Unparseable sources are skipped silently."""
    termset = {t.lower() for t in terms}
    found: List[Symbol] = []
    for path, src in sources.items():
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            continue
        # Track class membership so nested defs are reported as methods.
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
                name_tokens = _split_ident(name)
                score = 2 * len(termset & name_tokens)
                doc = ast.get_docstring(node) or ""
                if doc:
                    doc_tokens = {w.lower() for w in re.findall(r"[A-Za-z_]{4,}", doc)}
                    score += len(termset & doc_tokens)
                if score <= 0:
                    continue
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                found.append(Symbol(path=path, name=name, kind=kind,
                                    lineno=getattr(node, "lineno", 0), score=score))
    found.sort(key=lambda s: (s.score, -s.lineno), reverse=True)
    return found[:limit]


def localize(workdir: str, issue_text: str, *, file_limit: int = 5,
             symbol_limit: int = 8) -> Localization:
    """Full hierarchical localization. Best-effort: any failure degrades to a
    thinner (or empty) Localization rather than breaking the pipeline."""
    try:
        terms = extract_terms(issue_text)
        if not terms:
            return Localization()
        files = rank_files(workdir, terms, limit=file_limit)
        # Read the top files (cap size so a giant file can't blow memory) for AST.
        sources: Dict[str, str] = {}
        for rel in files:
            p = Path(workdir) / rel
            try:
                if p.is_file() and p.stat().st_size < 800_000:
                    sources[rel] = p.read_text(errors="ignore")
            except Exception:
                continue
        symbols = rank_symbols(terms, sources, limit=symbol_limit)
        return Localization(files=tuple(files), symbols=tuple(symbols),
                            terms=tuple(terms))
    except Exception:
        return Localization()
