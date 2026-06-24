from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from publication_gate import (  # noqa: E402
    DiffStats,
    GateInputs,
    RepoMeta,
    ReviewVerdict,
    decide,
    is_danger_surface,
)


def clean_inputs(
    *,
    repo: RepoMeta | None = None,
    files: list[str] | None = None,
    worker_status: str = "done",
    tests_green: bool = True,
    lint_clean: bool = True,
    build_clean: bool = True,
    has_secrets: bool = False,
    docs_updated_if_needed: bool = True,
    review: ReviewVerdict | None = None,
) -> GateInputs:
    return GateInputs(
        repo=repo or RepoMeta(owner="alfredjbclaw", name="pincer", is_owned=True),
        diff=DiffStats(lines_changed=10, files=files or ["tools/publication_gate.py"]),
        worker_status=worker_status,
        tests_green=tests_green,
        lint_clean=lint_clean,
        build_clean=build_clean,
        has_secrets=has_secrets,
        docs_updated_if_needed=docs_updated_if_needed,
        review=review or ReviewVerdict(verdict="approve", blockers=[]),
    )


def test_decide_auto_merges_owned_clean_safe_diff() -> None:
    # Given: an owned repository with a production-ready, non-danger diff.
    inputs = clean_inputs(files=["tools/publication_gate.py", "README.md"])

    # When: the gate decides publication.
    decision = decide(inputs)

    # Then: the change may auto-merge with an explanatory reason.
    assert decision.action == "auto_merge"
    assert decision.danger_surface is False
    assert decision.reasons == ["all checks passed; owned; non-danger"]


def test_decide_escalates_owned_clean_danger_diff() -> None:
    # Given: an owned repository whose clean diff touches CI.
    inputs = clean_inputs(files=[".github/workflows/test.yml"])

    # When: the gate decides publication.
    decision = decide(inputs)

    # Then: danger surface blocks auto-merge in this version.
    assert decision.action == "escalate"
    assert decision.danger_surface is True
    assert "danger surface" in decision.reasons[0]


def test_decide_opens_pr_when_tests_fail() -> None:
    # Given: an owned repository with failing tests.
    inputs = clean_inputs(tests_green=False)

    # When: the gate decides publication.
    decision = decide(inputs)

    # Then: the failed check is named.
    assert decision.action == "open_pr"
    assert decision.reasons == ["tests not green"]


def test_decide_opens_pr_for_unowned_repo_even_when_clean() -> None:
    # Given: a clean result on a repository outside the merge allowlist.
    inputs = clean_inputs(repo=RepoMeta(owner="other", name="repo", is_owned=False))

    # When: the gate decides publication.
    decision = decide(inputs)

    # Then: it refuses autonomous merge because ownership is not trusted.
    assert decision.action == "open_pr"
    assert decision.reasons == ["not an owned repo"]


def test_decide_opens_pr_when_secrets_present() -> None:
    # Given: a diff containing secrets according to the caller's scan.
    inputs = clean_inputs(has_secrets=True)

    # When: the gate decides publication.
    decision = decide(inputs)

    # Then: the secret finding is a publication blocker.
    assert decision.action == "open_pr"
    assert decision.reasons == ["secrets present in diff"]


def test_decide_opens_pr_when_reviewer_rejects() -> None:
    # Given: the independent reviewer rejects the change.
    inputs = clean_inputs(review=ReviewVerdict(verdict="reject", blockers=["missing live proof"]))

    # When: the gate decides publication.
    decision = decide(inputs)

    # Then: reviewer status and blockers are surfaced.
    assert decision.action == "open_pr"
    assert decision.reasons == ["review rejected", "review blocker: missing live proof"]


def test_decide_opens_pr_when_worker_status_is_not_done() -> None:
    # Given: a worker did not produce a completed change.
    inputs = clean_inputs(worker_status="no_changes")

    # When: the gate decides publication.
    decision = decide(inputs)

    # Then: it explains the status instead of pretending there is a mergeable diff.
    assert decision.action == "open_pr"
    assert decision.reasons == ["worker status is no_changes"]


def test_decide_reports_all_failed_production_ready_checks() -> None:
    # Given: multiple production-readiness checks fail together.
    inputs = clean_inputs(
        lint_clean=False,
        build_clean=False,
        docs_updated_if_needed=False,
        review=ReviewVerdict(verdict="approve", blockers=["needs owner confirmation"]),
    )

    # When: the gate decides publication.
    decision = decide(inputs)

    # Then: every failed check is returned in a stable, human-readable list.
    assert decision.action == "open_pr"
    assert decision.reasons == [
        "lint not clean",
        "build not clean",
        "docs not updated if needed",
        "review blocker: needs owner confirmation",
    ]


def test_is_danger_surface_detects_each_reviewed_pattern() -> None:
    # Given: one representative file for every reviewable danger pattern.
    files = [
        ".github/workflows/ci.yml",
        "Dockerfile",
        "docker-compose.yml",
        "infra/main.tf",
        "app/auth.py",
        "services/billing_client.py",
        "payments/payment_gateway.py",
        "migrations/001_create_users.sql",
        "pyproject.toml",
        "web/package.json",
        "requirements-dev.txt",
        "Makefile",
        "deploy/pincer.service",
        "launch/com.example.pincer.plist",
        "config.toml",
        "src/settings.py",
        ".env.local",
    ]

    # When: danger detection inspects the paths.
    danger_surface, matched_files = is_danger_surface(files)

    # Then: every representative pattern is considered dangerous.
    assert danger_surface is True
    assert matched_files == files


def test_is_danger_surface_is_case_insensitive() -> None:
    # Given: danger names arrive with mixed case from a filesystem.
    files = ["Services/AuthProvider.py", "REQUIREMENTS.txt"]

    # When: danger detection inspects the paths.
    danger_surface, matched_files = is_danger_surface(files)

    # Then: matching is case-insensitive.
    assert danger_surface is True
    assert matched_files == files
