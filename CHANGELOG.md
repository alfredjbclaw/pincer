# Changelog

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
