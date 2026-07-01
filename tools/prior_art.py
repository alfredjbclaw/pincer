#!/usr/bin/env python3
"""Prior-art harvesting for worker briefs.

This module intentionally distills public examples into structure and citation
notes only. It never returns source code for workers to paste.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import math
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

LOG = logging.getLogger("pincer.prior_art")


@dataclasses.dataclass(frozen=True)
class PriorArtConfig:
    enabled: bool = False
    max_repos: int = 3
    search_limit: int = 10
    max_files_per_repo: int = 3
    cache_dir: str = "~/.openclaw/pincer/prior-art"
    refresh: bool = False

    @classmethod
    def load(cls, path=None) -> "PriorArtConfig":
        import os

        cfg_path = path or os.environ.get(
            "PINCER_CONFIG",
            str(Path.home() / ".openclaw" / "pincer.toml"),
        )
        p = Path(cfg_path)
        if not p.exists():
            return cls()
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore
            except ImportError:
                return cls()
        try:
            data = tomllib.loads(p.read_text())
        except Exception:
            LOG.exception("Failed to load prior-art config from %s", p)
            return cls()
        cfg = data.get("prior_art", {})
        return cls(
            enabled=bool(cfg.get("enabled", False)),
            max_repos=int(cfg.get("max_repos", 3)),
            search_limit=int(cfg.get("search_limit", 10)),
            max_files_per_repo=int(cfg.get("max_files_per_repo", 3)),
            cache_dir=str(cfg.get("cache_dir", "~/.openclaw/pincer/prior-art")),
            refresh=bool(cfg.get("refresh", False)),
        )

    @property
    def cache_root(self) -> Path:
        return Path(self.cache_dir).expanduser()


@dataclasses.dataclass
class RepoCandidate:
    name: str
    html_url: str
    description: str = ""
    stars: int = 0
    pushed_at: str = ""
    default_branch: str = "main"
    has_tests: bool = False
    matched_paths: list[str] = dataclasses.field(default_factory=list)
    key_files: list[tuple[str, str, str]] = dataclasses.field(default_factory=list)
    health_score: float = 0.0


class GitHubClient(Protocol):
    def search_repositories(self, query: str, limit: int) -> list[dict]:
        ...

    def search_code(self, query: str, limit: int) -> list[dict]:
        ...

    def get_repo(self, full_name: str) -> dict | None:
        ...

    def get_tree(self, full_name: str, ref: str) -> list[dict]:
        ...

    def get_file(self, full_name: str, path: str, ref: str) -> str:
        ...


class GhCliGitHubClient:
    """Small `gh api` wrapper; failures degrade to empty prior-art output."""

    def _api(self, endpoint: str, fields: dict[str, str] | None = None) -> dict | list | None:
        cmd = ["gh", "api", "--method", "GET", endpoint]
        for key, value in (fields or {}).items():
            cmd += ["-f", f"{key}={value}"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            LOG.info("GitHub CLI unavailable for prior-art lookup")
            return None
        if p.returncode != 0:
            LOG.info("GitHub API lookup failed for %s: %s", endpoint, p.stderr.strip())
            return None
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError:
            LOG.info("GitHub API returned non-JSON for %s", endpoint)
            return None

    def search_repositories(self, query: str, limit: int) -> list[dict]:
        data = self._api(
            "search/repositories",
            {"q": query, "sort": "stars", "order": "desc", "per_page": str(limit)},
        )
        return list(data.get("items", [])) if isinstance(data, dict) else []

    def search_code(self, query: str, limit: int) -> list[dict]:
        data = self._api(
            "search/code",
            {"q": f"{query} in:file", "per_page": str(limit)},
        )
        return list(data.get("items", [])) if isinstance(data, dict) else []

    def get_repo(self, full_name: str) -> dict | None:
        data = self._api(f"repos/{full_name}")
        return data if isinstance(data, dict) else None

    def get_tree(self, full_name: str, ref: str) -> list[dict]:
        data = self._api(f"repos/{full_name}/git/trees/{ref}", {"recursive": "1"})
        tree = data.get("tree", []) if isinstance(data, dict) else []
        return list(tree) if isinstance(tree, list) else []

    def get_file(self, full_name: str, path: str, ref: str) -> str:
        data = self._api(f"repos/{full_name}/contents/{path}", {"ref": ref})
        if not isinstance(data, dict):
            return ""
        content = data.get("content", "")
        if data.get("encoding") != "base64" or not isinstance(content, str):
            return ""
        try:
            return base64.b64decode(content).decode("utf-8", errors="replace")
        except Exception:
            LOG.info("Failed to decode GitHub file %s:%s", full_name, path)
            return ""


def prior_art_block(
    problem: str,
    project_key: str,
    config: PriorArtConfig | None = None,
    github: GitHubClient | None = None,
) -> str:
    cfg = config or PriorArtConfig.load()
    if not cfg.enabled:
        return ""
    brief = build_prior_art_brief(problem, project_key, cfg, github=github)
    if not brief:
        return ""
    return "\n\n" + brief


def build_prior_art_brief(
    problem: str,
    project_key: str,
    config: PriorArtConfig | None = None,
    github: GitHubClient | None = None,
) -> str:
    cfg = config or PriorArtConfig.load()
    if not cfg.enabled:
        return ""

    cache_path = _cache_path(cfg, project_key, problem)
    if cache_path.exists() and not cfg.refresh:
        return cache_path.read_text()

    client = github or GhCliGitHubClient()
    try:
        candidates = harvest(problem, cfg, client)
    except Exception:
        LOG.exception("Prior-art harvest failed")
        return ""
    if not candidates:
        return ""

    brief = render_brief(problem, candidates)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(brief)
    (cache_path.parent / "prior-art.md").write_text(brief)
    return brief


def harvest(problem: str, cfg: PriorArtConfig, github: GitHubClient) -> list[RepoCandidate]:
    query = query_from_problem(problem)
    if not query:
        return []

    repos: dict[str, RepoCandidate] = {}
    for row in github.search_repositories(query, cfg.search_limit):
        name = str(row.get("full_name") or "")
        if not name:
            continue
        repos[name] = _candidate_from_repo(row)

    for row in github.search_code(query, cfg.search_limit):
        repo_row = row.get("repository") if isinstance(row.get("repository"), dict) else {}
        name = str(repo_row.get("full_name") or "")
        if not name:
            continue
        cand = repos.get(name)
        if cand is None:
            meta = github.get_repo(name) or repo_row
            cand = _candidate_from_repo(meta)
            repos[name] = cand
        path = str(row.get("path") or "")
        if path and path not in cand.matched_paths:
            cand.matched_paths.append(path)

    enriched = [_enrich_candidate(cand, cfg, github) for cand in repos.values()]
    for cand in enriched:
        cand.health_score = _health_score(cand)
    enriched.sort(key=lambda c: c.health_score, reverse=True)
    return enriched[: max(0, cfg.max_repos)]


def render_brief(problem: str, candidates: list[RepoCandidate]) -> str:
    lines = [
        "# Prior Art",
        "",
        "_Reference only: do not copy source code. Use these as pattern context and",
        "cite the sources when they shape an implementation._",
        "",
        "## Search Frame",
        f"- Problem: {_one_line(problem, 220)}",
        "- Ranking: repository health first (stars, recent pushes, visible tests), not raw search rank.",
        "",
    ]
    for i, cand in enumerate(candidates, 1):
        files = cand.key_files[:]
        sources = [cand.html_url] + [url for _, url, _ in files]
        lines += [
            f"## {i}. {cand.name}",
            f"- Health: {_format_health(cand)}",
            f"- Approach: {_approach(cand)}",
            f"- Structure: {_structure(cand)}",
            f"- Patterns to adapt: {_patterns(cand)}",
            "- Sources:",
        ]
        lines += [f"  - {url}" for url in sources if url]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def query_from_problem(problem: str, max_terms: int = 8) -> str:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", problem.lower())
    kept: list[str] = []
    for token in tokens:
        if token in _STOPWORDS or token in kept:
            continue
        kept.append(token)
        if len(kept) >= max_terms:
            break
    return " ".join(kept)


def _candidate_from_repo(row: dict) -> RepoCandidate:
    return RepoCandidate(
        name=str(row.get("full_name") or ""),
        html_url=str(row.get("html_url") or ""),
        description=str(row.get("description") or ""),
        stars=int(row.get("stargazers_count") or 0),
        pushed_at=str(row.get("pushed_at") or ""),
        default_branch=str(row.get("default_branch") or "main"),
    )


def _enrich_candidate(
    cand: RepoCandidate,
    cfg: PriorArtConfig,
    github: GitHubClient,
) -> RepoCandidate:
    tree = github.get_tree(cand.name, cand.default_branch)
    paths = [str(row.get("path") or "") for row in tree if row.get("type") == "blob"]
    cand.has_tests = _has_tests(paths)
    key_paths = _key_paths(cand.matched_paths, paths, cfg.max_files_per_repo)
    for path in key_paths:
        text = github.get_file(cand.name, path, cand.default_branch)
        url = _file_url(cand, path)
        cand.key_files.append((path, url, _summarize_file(text)))
    return cand


def _health_score(cand: RepoCandidate) -> float:
    stars = math.log10(max(cand.stars, 0) + 1) * 3.0
    recency = _recency_score(cand.pushed_at)
    tests = 2.0 if cand.has_tests else 0.0
    return stars + recency + tests


def _recency_score(pushed_at: str) -> float:
    if not pushed_at:
        return 0.0
    try:
        pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    days = max(0, (datetime.now(timezone.utc) - pushed).days)
    if days <= 30:
        return 3.0
    if days <= 180:
        return 2.0
    if days <= 730:
        return 1.0
    return 0.0


def _has_tests(paths: list[str]) -> bool:
    return any(
        "/test" in f"/{path.lower()}"
        or path.lower().startswith("test")
        or path.lower().endswith(("_test.py", ".test.ts", ".spec.ts", "_test.go"))
        for path in paths
    )


def _key_paths(matched_paths: list[str], tree_paths: list[str], max_files: int) -> list[str]:
    selected: list[str] = []
    for path in matched_paths:
        if _likely_source_or_doc(path) and path not in selected:
            selected.append(path)
    for path in tree_paths:
        low = path.lower()
        if low in {"readme.md", "docs/readme.md"} and path not in selected:
            selected.insert(0, path)
        elif _is_test_path(path) and path not in selected:
            selected.append(path)
        if len(selected) >= max_files:
            break
    return selected[: max(0, max_files)]


def _likely_source_or_doc(path: str) -> bool:
    low = path.lower()
    return low.endswith(
        (".py", ".go", ".rs", ".js", ".ts", ".tsx", ".java", ".rb", ".md", ".rst")
    )


def _is_test_path(path: str) -> bool:
    low = path.lower()
    return (
        "/test" in f"/{low}"
        or low.startswith("test")
        or low.endswith(("_test.py", ".test.ts", ".spec.ts", "_test.go"))
    )


def _summarize_file(text: str) -> str:
    prose: list[str] = []
    in_fence = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        if _looks_like_code(line):
            continue
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"^[-*]\s+", "", line)
        if len(line) < 24:
            continue
        prose.append(_one_line(line, 160))
        if len(prose) >= 2:
            break
    return " ".join(prose)


def _looks_like_code(line: str) -> bool:
    code_markers = (
        "def ",
        "class ",
        "function ",
        "import ",
        "from ",
        "const ",
        "let ",
        "var ",
        "return ",
        "package ",
        "func ",
    )
    low = line.lower()
    return (
        low.startswith(code_markers)
        or line.startswith(("{", "}", "@", "<", "#!"))
        or line.count("{") + line.count("}") + line.count(";") >= 2
    )


def _approach(cand: RepoCandidate) -> str:
    parts = [cand.description]
    parts += [summary for _, _, summary in cand.key_files if summary]
    return _one_line(" ".join(p for p in parts if p), 260) or "Use repository structure and cited files as architecture references."


def _structure(cand: RepoCandidate) -> str:
    paths = [path for path, _, _ in cand.key_files]
    if not paths:
        return "No key files were fetched; use the repository-level citation only."
    role_bits = []
    if any(path.lower().startswith("readme") or "/readme" in path.lower() for path in paths):
        role_bits.append("README/docs introduce the design")
    if any(_is_test_path(path) for path in paths):
        role_bits.append("tests document expected behavior")
    source_paths = [path for path in paths if not _is_test_path(path) and not path.lower().endswith((".md", ".rst"))]
    if source_paths:
        role_bits.append("source files show module boundaries: " + ", ".join(source_paths[:3]))
    return "; ".join(role_bits) if role_bits else "Key files: " + ", ".join(paths)


def _patterns(cand: RepoCandidate) -> str:
    patterns = []
    if cand.has_tests:
        patterns.append("mirror the visible test-first shape without copying cases")
    if cand.matched_paths:
        patterns.append("study matched file placement for boundaries and naming")
    if cand.description:
        patterns.append("adapt the stated approach in project-specific terms")
    return "; ".join(patterns) if patterns else "extract only high-level organization and tradeoffs"


def _format_health(cand: RepoCandidate) -> str:
    tests = "tests visible" if cand.has_tests else "tests not obvious"
    pushed = cand.pushed_at[:10] if cand.pushed_at else "unknown push date"
    return f"{cand.stars} stars, pushed {pushed}, {tests}, score {cand.health_score:.2f}"


def _file_url(cand: RepoCandidate, path: str) -> str:
    return f"{cand.html_url}/blob/{cand.default_branch}/{path}" if cand.html_url else ""


def _cache_path(cfg: PriorArtConfig, project_key: str, problem: str) -> Path:
    project = re.sub(r"[^A-Za-z0-9_.-]+", "-", project_key).strip("-") or "project"
    digest = hashlib.sha256(problem.encode("utf-8")).hexdigest()[:16]
    return cfg.cache_root / project / f"{digest}.prior-art.md"


def _one_line(text: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)].rstrip() + "..."


_STOPWORDS = {
    "about",
    "after",
    "against",
    "already",
    "also",
    "before",
    "build",
    "change",
    "code",
    "does",
    "existing",
    "fails",
    "feature",
    "from",
    "given",
    "issue",
    "make",
    "must",
    "need",
    "needs",
    "per",
    "problem",
    "project",
    "repo",
    "should",
    "that",
    "this",
    "using",
    "when",
    "with",
}
