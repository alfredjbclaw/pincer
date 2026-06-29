#!/usr/bin/env python3
"""pincer selection cascade — choose the best of N candidate patches for the
SAME issue with execution-grounded signals first, the LLM judge last.

This is the lever the research identifies as Pincer's biggest: coverage (some
candidate is correct) runs ~70-80%, but realized score is ~57-66% — the gap is
*selection*. Pincer's reviewer is an LLM judge with no execution grounding,
exactly the weak selector the literature warns against. The fix (Agentless +
CodeMonkeys) is a cascade:

  1. Regression rank   — fewest previously-passing tests broken (PASS_TO_PASS
                         analog; directly predicts "resolved").
  2. Reproduction flip — among the regression-best tier, prefer candidates that
                         flip a *valid* fail-to-pass reproduction test.
  3. AST-normalized    — canonicalize each patch (drop comments/whitespace/order)
     majority vote       and pick the consensus edit.
  4. Reviewer tie-break— only if still tied, the Opus-4.8 judge breaks the tie.

Each stage NARROWS the tier; none is ever allowed to empty it (if a stage would
eliminate every candidate, it is skipped and we fall back to the prior tier).
The `stage` field on the result records which stage made the final cut — that is
the selection-gap diagnostic the bench run reads.

Pure over candidate dicts; `normalize` and `reviewer` are injectable so the
cascade is unit-tested without VMs or model calls.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Callable, Dict, List, Optional

# Sentinel "worst possible" regression count for a candidate whose tests we
# could not score (infra error / unparsed) — ranks below any real count but
# still keeps the candidate as a last resort.
_UNKNOWN_REGRESSIONS = 10 ** 9

_SKIP_DIFF_PREFIXES = ("+++", "---", "@@", "diff --git", "index ", "rename ",
                       "new file", "deleted file", "similarity ", "Binary files")


@dataclasses.dataclass
class SelectionResult:
    chosen: Optional[dict]
    ranked: List[dict]
    stage: str            # which stage made the final cut
    reason: str
    diagnostics: dict

    def to_dict(self) -> dict:
        return {"chosen_issue": (self.chosen or {}).get("issue"),
                "stage": self.stage, "reason": self.reason,
                "diagnostics": self.diagnostics}


def normalize_patch(diff: str) -> str:
    """Canonicalize a unified diff to a vote key: keep only the +/- code lines,
    strip the sign-preserving comment/blank lines, collapse whitespace, and sort
    so two candidates making the same edits in a different hunk order vote
    together. Empty (no code change) -> "" which is treated as non-collapsing."""
    if not diff:
        return ""
    changes = []
    for ln in diff.splitlines():
        if any(ln.startswith(p) for p in _SKIP_DIFF_PREFIXES):
            continue
        sign = ln[:1]
        if sign not in "+-":
            continue
        body = ln[1:].strip()
        if not body or body.startswith("#"):
            continue
        # Whitespace-insensitive: `x = 1` and `x=1` are the same edit. Removing
        # all whitespace is the standard cheap canonicalization for a vote key.
        body = re.sub(r"\s+", "", body)
        changes.append(sign + body)
    if not changes:
        return ""
    return "\n".join(sorted(changes))


def _regressions(cand: dict) -> int:
    """How many tests this candidate breaks. Reads the structured sandbox
    results; pass-with-no-counts -> 0; error/unknown -> sentinel worst."""
    res = cand.get("results")
    if isinstance(res, dict) and res.get("parsed"):
        return int(res.get("regressions", 0))
    if hasattr(res, "regressions") and getattr(res, "parsed", False):
        return int(res.regressions)
    sb = cand.get("sandbox")
    if sb == "pass":
        return 0
    return _UNKNOWN_REGRESSIONS


def _largest_group(tier: List[dict], normalize: Callable[[str], str]) -> List[dict]:
    """Group the tier by normalized patch and return the largest group,
    preserving input order. Candidates with an empty key (no parseable code
    change) never collapse together — each stands alone. The caller treats a
    returned group of size 1 as 'no consensus' (inconclusive vote)."""
    groups: Dict[str, List[dict]] = {}
    singletons: List[List[dict]] = []
    for c in tier:
        key = normalize(c.get("patch", "") or "")
        if not key:
            singletons.append([c])
            continue
        groups.setdefault(key, []).append(c)
    buckets = list(groups.values()) + singletons
    if not buckets:
        return tier
    best = max(len(b) for b in buckets)
    for b in buckets:  # first bucket (input order) that hits the max size
        if len(b) == best:
            return b
    return tier


def select(
    candidates: List[dict],
    *,
    normalize: Callable[[str], str] = normalize_patch,
    reviewer: Optional[Callable[[dict], bool]] = None,
    has_repro: bool = False,
) -> SelectionResult:
    """Run the cascade over candidates for one issue. `reviewer(cand) -> bool`
    (True == approve) is consulted ONLY to break a final tie, so the expensive
    LLM judge runs at most once per surviving finalist."""
    eligible = [c for c in candidates if c.get("committed")]
    diag: dict = {"n_candidates": len(candidates), "n_eligible": len(eligible)}

    if not eligible:
        return SelectionResult(None, [], "none",
                               "no candidate produced changes", diag)
    if len(eligible) == 1:
        return SelectionResult(eligible[0], eligible, "only_candidate",
                               "single eligible candidate", diag)

    # Prefer candidates whose sandbox actually ran (drop infra errors unless
    # that would leave nothing).
    pool = [c for c in eligible if c.get("sandbox") != "error"] or eligible
    pool = sorted(pool, key=_regressions)
    diag["regressions"] = {str(c.get("issue", i)): _regressions(c)
                           for i, c in enumerate(pool)}

    best_reg = _regressions(pool[0])
    tier = [c for c in pool if _regressions(c) == best_reg]
    stage, reason = "regression", f"fewest regressions ({best_reg})"
    if len(tier) == 1:
        return SelectionResult(tier[0], pool, stage, reason, diag)

    # Reproduction flip — narrow to flippers if any flip the valid repro test.
    if has_repro:
        flippers = [c for c in tier if c.get("repro_flip")]
        diag["n_repro_flippers"] = len(flippers)
        if flippers:
            tier = flippers
            stage, reason = "reproduction", "flips the reproduction test"
            if len(tier) == 1:
                return SelectionResult(tier[0], pool, stage, reason, diag)

    # AST-normalized majority vote. A vote is only DECISIVE when ≥2 candidates
    # agree; an all-singletons tier is inconclusive and falls through to the
    # reviewer rather than arbitrarily picking the first.
    group = _largest_group(tier, normalize)
    if len(group) > 1:
        diag["vote_group_size"] = len(group)
        # Every member is the same edit modulo formatting — equivalent, so the
        # first is as good as any and the reviewer is unnecessary.
        return SelectionResult(group[0], pool, "majority_vote",
                               f"consensus of {len(group)} candidates", diag)

    # Reviewer tie-break — last, and only on the survivors.
    if reviewer is not None and len(tier) > 1:
        approved = []
        for c in tier:
            try:
                if reviewer(c):
                    approved.append(c)
            except Exception:
                continue
        diag["n_reviewer_approved"] = len(approved)
        if approved:
            return SelectionResult(approved[0], pool, "reviewer",
                                   "approved by reviewer tie-break", diag)

    # Still tied — take the best-ranked (regression order is stable here).
    return SelectionResult(tier[0], pool, stage,
                           reason + " (tie broken by rank)", diag)
