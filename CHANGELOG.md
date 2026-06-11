# Changelog

## 0.1.0 — 2026-06-11

Initial port of `steipete/agent-scripts` maintainer skills to the OpenClaw runtime.

### `gh-triage` (adapted from `github-project-triage`)

- Replaced hard-coded RepoBar dependency with vanilla `gh` CLI for broad-queue discovery.
- Parameterized owner scope; removed `steipete` / `openclaw` defaults.
- Removed Peter-specific file paths (`~/Projects/RepoBar`, `~/Projects/clawdbot/...`, `~/Projects/maintainers/...`).
- Removed `$one-password` / `op` credential discovery section; left a generic "credential manager" hook.
- Replaced "Peter/owner" language with "owner" throughout.
- Kept the URL-first output rule, three-bucket classification, trust signals, and autonomous-fit rules verbatim.

### `repo-orchestrator` (adapted from `maintainer-orchestrator`)

- Replaced Codex thread delegation with OpenClaw subagent spawns (`sessions_spawn` / `subagents`).
- Generalized repository scope from "Peter-majority commits" to a configurable owner allowlist.
- Replaced `~/oss-orchestrator.md` with a configurable log path (default `<workspace>/repo-orchestrator-log.md`).
- Kept the decision-ready queue rule, owner decision brief format, monitoring protocol, live-proof gate, release gate, and authorization separation verbatim.

### Upstream PR candidates

The following changes are general improvements, not OpenClaw-specific, and may be PR'd back to `steipete/agent-scripts`:

- Make owner allowlist a skill argument instead of hard-coded.
- Decouple from RepoBar — vanilla `gh` fallback for portability.
- Split credential manager hook so non-1Password setups can use it.
