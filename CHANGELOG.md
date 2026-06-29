# Changelog

## Unreleased — pre-bench selection cascade

Test-grounded candidate **selection** added to the parallel orchestrator — the
lever the SWE-bench literature identifies as the binding constraint (coverage
~70-80% vs realized ~57-66%; the gap is *which candidate gets picked*). All new
behavior is **opt-in**; the defaults reproduce the original one-candidate loop
exactly. See `SELECTION.md`.

### New

- **Per-issue candidate multiplicity** — `--samples K` (config `[selection].samples`)
  fans out K independent coders for the *same* issue (own worktree/branch each).
  The original loop was issue-parallel with one candidate per issue; a selection
  cascade needs >1 candidate to choose among.
- **Selection cascade** (`tools/selection.py`) — picks one winner per issue with
  execution-grounded signals first, the LLM judge last: regression rank →
  reproduction-test flip → AST-normalized majority vote → Opus reviewer
  tie-break. Each stage narrows the tier and is never allowed to empty it. The
  winning `stage` is recorded as the selection-gap diagnostic.
- **Structured test results** (`tools/test_results.py`) — pytest output parsed
  into pass/fail/error counts + failed-test names so candidates are *ranked* by
  how many previously-passing tests they break (PASS_TO_PASS analog), not just
  gated pass/fail. Threaded through `sandbox_gate.SandboxVerdict.results`.
- **Reproduction tests** (`tools/repro_test.py`) — generate a fail-to-pass test
  per issue, **validate it actually fails on the unpatched base**, then prefer
  candidates that flip it. Off by default (`[selection].repro_tests`); heavy
  (one extra sandbox run per candidate). Noisy tests are discarded, never a hard
  gate — falls back to regression-only ranking.
- **Hierarchical localization** (`tools/localization.py`) — the flat grep-rank is
  now layered with an AST symbol skeleton (def/class signatures ranked by
  issue-term overlap, camelCase-aware), feeding the worker brief file *and*
  symbol leads.
- **Bounded execution-feedback loop** — `--max-revise-iters N` (default 1)
  generalizes the single-shot revise into an N-round fix→sandbox→review loop,
  each round prepending an Opus root-cause reading of the failure
  (`reviewer.interpret_failure`) instead of echoing raw stderr.

### Fixed

- Localization test-file filter applied to the absolute path, so a repo checked
  out under a path containing `/test` filtered the entire tree. Now filters on
  the path relative to the workdir, and only matches real test dirs/modules.

### Internal

- `pytest.ini` scopes discovery to `tests/` (helper modules under `tools/` that
  match `test_*.py` are no longer mis-collected).

## 0.1.0 — 2026-06-15

First public release. Renamed from `openclaw-maintainer-skills` to `pincer` and expanded from a 2-skill steipete port into a 4-skill composed pipeline implementing a five-tier autonomous maintainer loop with Crabbox-gated sandbox validation.

### New skills

- **`audit-and-plan`** (Mission tier, Opus, daily) — Frontier-model repo audit that reads code, CI, dependencies, and changelog, then writes a structured TOML plan to `plans/<owner-repo>-<YYYY-MM-DD>.toml`. Pattern credit: [shadcn `/improve`](https://github.com/shadcn-ui/ui).
- **`keeper`** (meta-runner) — One-liner `keeper run <repo>` invocation that drives the full Mission → Goal → Control pipeline against a configured repo allowlist.

### Renamed + expanded skills

- **`triage`** (was `gh-triage`) — Now classifies against an audit-and-plan output, not just a live queue. Adds a "consistent with mission plan" check before bucketing autonomous candidates.
- **`orchestrator`** (was `repo-orchestrator`) — Now:
  - Dispatches workers via a Codex-primary / Claude-Code-fallback runtime adapter (`tools/runtime-adapter.py`).
  - Gates every worker output through `crabbox run --provider applevz -- <test>` on a clean throwaway VM.
  - Opens PRs only when Crabbox returns a green verdict.
  - Persists state to `~/.openclaw/pincer/{log.md,plans/,state.json}` instead of `<workspace>/repo-orchestrator-log.md`.

### Sandbox primitive

- Hard dependency on [`openclaw/crabbox`](https://github.com/openclaw/crabbox) `>=0.31.0`.
- Default provider: Apple VZ (no cloud credentials needed on Apple Silicon).
- Configurable to any of 60+ Crabbox providers.

### Runtime adapter

- Primary: Codex CLI (`>=0.131.0`) via OpenClaw's existing ChatGPT Pro Lite auth path.
- Fallback: Claude Code wrapper (`tools/claude-code-wrapper.py`) on credit exhaustion, `auth_expired`, `429`, or three-strikes failure.
- Returns structured `STATUS:` / `FILES:` / `VALIDATION:` / `NEXT:` per workspace AGENTS.md spawn contract.

### Architectural notes

- Five-tier model split: Mission (Opus) → Goal (Sonnet) → Control (Sonnet) → Agent (worker LLM) → Sandbox (Crabbox) → Tool (Haiku).
- TOML plans (chosen over JSON for diff readability and human edits).
- Persistent ledger in append-only Markdown (steipete's pattern, preserved).

### Standing on the shoulders of

| Reference | Insight | Tier in pincer |
|---|---|---|
| shadcn `/improve` | Frontier plans, mid-tier executes | Mission |
| steipete `github-project-triage` | URL-first triage buckets | Goal |
| steipete `maintainer-orchestrator` | Decision-ready PRs + live-proof gate | Control |
| nathan `agnt` | Mission → goal → agent → tool cadence | Cadence framing |
| openclaw `crabbox` | Sandboxed test execution control plane | Sandbox |

### Upstream PR candidates

These changes are not pincer-specific and may be PR'd back to upstream:

- `steipete/agent-scripts`: parameterized owner allowlist; decouple from RepoBar; pluggable credential manager.
- `openclaw/crabbox`: docs PR documenting orchestrator-driven usage patterns; testbed for Node+Postgres coordinator validation.

## 0.0.1 — 2026-06-11 (private)

Initial port of `steipete/agent-scripts#github-project-triage` and `maintainer-orchestrator` as `openclaw-maintainer-skills`. Held from public release pending composed-pipeline design.
