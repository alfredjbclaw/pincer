---
name: keeper
description: "Meta-runner for pincer. Single-command `keeper run <repo>` invocation that drives the full Mission → Goal → Control pipeline against a configured repo allowlist using sane defaults from ~/.openclaw/pincer.toml. The ergonomic surface for daily use."
triggers:
  - 'keeper'
  - 'keeper run'
  - 'keeper audit'
  - 'keep this repo healthy'
  - 'pincer keeper'
---

# keeper (Meta-runner)

`keeper` is the one-liner surface for pincer. It composes the three pipeline skills with sane defaults from `~/.openclaw/pincer.toml`, so day-to-day operation is a single command instead of three skill invocations.

This is **not** a new tier. It's the operator's ergonomic wrapper around the existing Mission → Goal → Control loop.

## Commands

```sh
keeper audit <repo>       # Mission tier only — run audit-and-plan, write the TOML, exit
keeper triage <repo>      # Goal tier only — run triage against the most recent plan, exit
keeper run <repo>         # Full continuous loop — orchestrator runs every 5 minutes until stopped
keeper run --all          # Same, against every [[repos]] entry in the config
keeper status             # Show current state.json, recent log entries, active workers
keeper stop               # Stop continuous run, drain in-flight workers cleanly
```

All commands respect `~/.openclaw/pincer.toml`. To use a different config:

```sh
PINCER_CONFIG=/path/to/other.toml keeper run owner/repo
```

## Default cadence

When you type `keeper run owner/repo`:

1. **Now**: Run [`audit-and-plan`](../audit-and-plan/SKILL.md) if no plan TOML exists or the existing one is more than 24 hours old.
2. **Now**: Run [`triage`](../triage/SKILL.md) against the plan.
3. **Now**: Start [`orchestrator`](../orchestrator/SKILL.md) in continuous mode (5-minute polling).
4. **Daily at the cadence in `[cadence].mission`** (default `09:00 local`): re-run audit-and-plan.
5. **Hourly at the cadence in `[cadence].goal`** (default top of every hour): re-run triage.
6. **Every 5 minutes** (or whatever `[cadence].control` says): orchestrator polls workers, sandbox-gates completions, opens PRs on green, returns red verdicts to workers.

The orchestrator runs as a persistent OpenClaw session. `keeper stop` sends it a shutdown signal; in-flight workers complete their current task before draining.

## Implementation

The keeper command is a thin shell wrapper (`scripts/keeper.sh`) that:

1. Reads `~/.openclaw/pincer.toml`.
2. Validates Crabbox installation (`crabbox doctor` exit code).
3. Validates runtime availability (`codex --version` or fallback Claude Code wrapper).
4. For `audit` / `triage` subcommands, invokes the corresponding skill via the OpenClaw runtime.
5. For `run`, spawns a persistent orchestrator session via `sessions_spawn` with a stable `taskName=pincer-<owner>-<repo>`, then exits.

The session continues running until explicitly stopped via `keeper stop` or the OpenClaw runtime is killed. State persists across restarts via `~/.openclaw/pincer/state.json`.

## Status output

```sh
keeper status
```

Returns a compact summary:

```text
pincer keeper — state @ 2026-06-15T14:32:00-04:00

Repos under management: 1
  alfredjbclaw/pincer-testbed  [allowDelegate=true allowMerge=false allowRelease=false]
    Mission: ran 2026-06-15 09:00 (plan: 5 items, leverage range 0.5–2.0)
    Goal:    ran 2026-06-15 14:00 (3 autonomous, 1 needs-owner, 1 defer)
    Control: orchestrator session active (taskName=pincer-alfredjbclaw-pincer-testbed)
             workers: 2 active, 1 awaiting-sandbox, 0 blocked
             last sandbox verdict: PASS (applevz, 47s, ITEM-001)
             last PR opened:       #42 (https://github.com/alfredjbclaw/pincer-testbed/pull/42)

Runtime adapter health: codex 100%, claude-code 0% (no fallbacks triggered today)
Crabbox health:         applevz green; last 10 runs: 9 PASS, 1 FAIL (ITEM-003 reverted)

See ~/.openclaw/pincer/log.md for the full event log.
```

## Sane defaults

`keeper` exists because the three-skill pipeline shouldn't require knowing the wiring. Everything is in the config:

- Models — `[models]`
- Runtime — `[runtime]`
- Sandbox — `[sandbox]`
- Cadence — `[cadence]`
- Allowlist — `[[repos]]`

If a field is missing from the config, `keeper` uses the published default (see `config.toml.example`). To override one-off:

```sh
keeper run owner/repo --provider hetzner --no-fallback
```

CLI flag overrides apply to the current invocation only — they do not write back to the config.

## When NOT to use keeper

- One-off issue investigation that is not part of the pipeline → use `triage` directly.
- Manual code review of an existing PR → use the workspace `code-review` skill.
- Anything outside the configured repo allowlist → `keeper` will refuse to dispatch workers. Add the repo to `~/.openclaw/pincer.toml` first.
