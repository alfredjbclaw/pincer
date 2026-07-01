#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import prior_art as pa


class FakeGitHub:
    def __init__(self):
        self.calls = []

    def search_repositories(self, query, limit):
        self.calls.append(("search_repositories", query, limit))
        return [
            {
                "full_name": "old/popular",
                "html_url": "https://github.com/old/popular",
                "description": "Older raw-relevance hit.",
                "stargazers_count": 50,
                "pushed_at": "2001-01-01T00:00:00Z",
                "default_branch": "main",
            },
            {
                "full_name": "fresh/tested",
                "html_url": "https://github.com/fresh/tested",
                "description": "Fresh implementation with regression tests.",
                "stargazers_count": 20,
                "pushed_at": "2999-01-01T00:00:00Z",
                "default_branch": "main",
            },
        ][:limit]

    def search_code(self, query, limit):
        self.calls.append(("search_code", query, limit))
        return [
            {
                "path": "src/solver.py",
                "repository": {
                    "full_name": "fresh/tested",
                    "html_url": "https://github.com/fresh/tested",
                    "description": "Fresh implementation with regression tests.",
                    "stargazers_count": 20,
                    "pushed_at": "2999-01-01T00:00:00Z",
                    "default_branch": "main",
                },
            },
        ]

    def get_repo(self, full_name):
        self.calls.append(("get_repo", full_name))
        return None

    def get_tree(self, full_name, ref):
        self.calls.append(("get_tree", full_name, ref))
        if full_name == "fresh/tested":
            return [
                {"type": "blob", "path": "README.md"},
                {"type": "blob", "path": "src/solver.py"},
                {"type": "blob", "path": "tests/test_solver.py"},
            ]
        return [
            {"type": "blob", "path": "README.md"},
            {"type": "blob", "path": "src/main.py"},
        ]

    def get_file(self, full_name, path, ref):
        self.calls.append(("get_file", full_name, path, ref))
        if path == "src/solver.py":
            return "\n".join([
                "def copied_example():",
                "    return 'source body'",
                "# Solver coordinates parsing, ranking, and report generation.",
                "The module keeps search, ranking, and rendering isolated for testability.",
            ])
        if path == "tests/test_solver.py":
            return "Regression tests cover stale repositories, fresh repositories, and cache hits."
        return "This project documents a compact prior art harvesting architecture."


def _cfg(tmp_path, **kw):
    data = dict(
        enabled=True,
        max_repos=2,
        search_limit=5,
        max_files_per_repo=3,
        cache_dir=str(tmp_path),
        refresh=False,
    )
    data.update(kw)
    return pa.PriorArtConfig(**data)


def test_config_defaults_off_when_file_missing(tmp_path):
    cfg = pa.PriorArtConfig.load(tmp_path / "missing.toml")

    assert cfg.enabled is False


def test_prior_art_block_disabled_does_not_touch_github(tmp_path):
    fake = FakeGitHub()

    block = pa.prior_art_block(
        "rank prior art for parser errors",
        "owner/repo",
        config=_cfg(tmp_path, enabled=False),
        github=fake,
    )

    assert block == ""
    assert fake.calls == []


def test_harvest_ranks_by_health_not_raw_search_order(tmp_path):
    candidates = pa.harvest(
        "rank candidate repositories with tests",
        _cfg(tmp_path, max_repos=2),
        FakeGitHub(),
    )

    assert [cand.name for cand in candidates] == ["fresh/tested", "old/popular"]
    assert candidates[0].has_tests is True


def test_brief_distills_patterns_and_cites_sources_without_code(tmp_path):
    brief = pa.build_prior_art_brief(
        "rank candidate repositories with tests",
        "owner/repo",
        config=_cfg(tmp_path),
        github=FakeGitHub(),
    )

    assert "# Prior Art" in brief
    assert "Reference only: do not copy source code" in brief
    assert "https://github.com/fresh/tested" in brief
    assert "https://github.com/fresh/tested/blob/main/src/solver.py" in brief
    assert "def copied_example" not in brief
    assert "source body" not in brief


def test_build_prior_art_uses_cache_per_project_and_problem(tmp_path):
    fake = FakeGitHub()
    cfg = _cfg(tmp_path)
    first = pa.build_prior_art_brief("parser ranking problem", "owner/repo", cfg, fake)
    calls_after_first = list(fake.calls)

    second = pa.build_prior_art_brief("parser ranking problem", "owner/repo", cfg, fake)

    assert second == first
    assert fake.calls == calls_after_first
    assert (tmp_path / "owner-repo" / "prior-art.md").exists()


def test_query_from_problem_keeps_useful_terms():
    query = pa.query_from_problem("Given a problem: cache GitHub prior-art per project.")

    assert query == "cache github prior-art"
