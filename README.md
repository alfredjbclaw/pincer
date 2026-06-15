# 🦞 pincer

**A four-tier autonomous maintainer loop for OpenClaw.**

Frontier-model plans, mid-tier executes, sandboxed verification before every PR.

```text
MISSION   (Opus,    daily)    audit-and-plan   ──┐
GOAL      (Sonnet,  hourly)   triage             │
CONTROL   (Sonnet,  5m)       orchestrator       │  pincer
AGENT     (Codex,   per item) worker subagent    │
SANDBOX   (Crabbox, per task) clean throwaway VM │
TOOL      (Haiku,   per call) gh/git/test        ──┘
```

The orchestrator opens a PR only when Crabbox returns a green verdict on a clean throwaway VM. No exceptions.

## Why this exists

Steipete's `maintainer-orchestrator` showed that a single Codex thread could maintain a fleet of repositories if you enforce a hard "decision-ready PR" rule. shadcn's `/improve` showed that the right move is to split planning (expensive frontier model, called sparingly) from execution (cheaper mid-tier, called often). Nathan Wilbanks' loop hierarchy gave the cadence: mission is daily, goal is hourly, control is every five minutes.

`pincer` is the composition: each tier matches the cost and cadence of its loop level, and every worker output passes through a sandboxed test execution before the maintainer ever sees a PR.

## Install

```sh
# 1. Install Crabbox (the sandbox control plane)
brew install openclaw/tap/crabbox
crabbox doctor --provider applevz   # Apple Silicon default; cloud providers also supported

# 2. Install pincer to your OpenClaw skills directory
git clone https://github.com/alfredjbclaw/pincer ~/Projects/pincer
ln -s ~/Projects/pincer/skills/audit-and-plan ~/.openclaw/workspace/skills/audit-and-plan
ln -s ~/Projects/pincer/skills/triage         ~/.openclaw/workspace/skills/triage
ln -s ~/Projects/pincer/skills/orchestrator   ~/.openclaw/workspace/skills/orchestrator
ln -s ~/Projects/pincer/skills/keeper         ~/.openclaw/workspace/skills/keeper

# 3. Configure
cp ~/Projects/pincer/config.toml.example ~/.openclaw/pincer.toml
$EDITOR ~/.openclaw/pincer.toml
```

## Quick start

```sh
# Once-over a repo
keeper audit owner/repo

# Continuous orchestration (every 5 minutes)
keeper run owner/repo

# Inspect the ledger
cat ~/.openclaw/pincer/log.md
```

## Configuration

`~/.openclaw/pincer.toml`:

```toml
owner = "alfredjbclaw"

[models]
mission = "anthropic/claude-opus-4-7"      # audit-and-plan (daily)
goal    = "anthropic/claude-sonnet-4-6"    # triage (hourly)
control = "anthropic/claude-sonnet-4-6"    # orchestrator (every 5m)
agent   = "openai/gpt-5.5"                 # worker — Codex CLI
tool    = "anthropic/claude-haiku-4-5"     # individual invocations

[runtime]
primary  = "codex"            # codex | claude-code
fallback = "claude-code"      # graceful degrade on credit-exhausted / auth_expired / 3-strikes failure

[sandbox]
provider = "applevz"          # Apple Silicon default; see crabbox provider list for alternatives
gate     = "every"            # every | tagged-needs-isolation

[cadence]
mission = "daily"
goal    = "hourly"
control = "5m"

[[repos]]
name           = "alfredjbclaw/pincer-testbed"
allowDelegate  = true
allowMerge     = false
allowRelease   = false
```

## State

`pincer` keeps three pieces of persistent state under `~/.openclaw/pincer/`:

- `log.md` — append-only event log (every decision, dispatch, sandbox verdict, PR action)
- `plans/<owner-repo>-<YYYY-MM-DD>.toml` — per-repo Mission output
- `state.json` — active claims, last-poll timestamps, runtime-adapter health

## The sandbox gate

Every worker subagent runs the AGENT loop locally — implement, write tests, commit on a branch. Before the orchestrator opens a PR, the diff is validated by:

```sh
crabbox run --provider applevz -- <repo-test-suite>
```

Crabbox leases a clean throwaway VM (Apple VZ by default, configurable to any of [60+ supported providers](https://github.com/openclaw/crabbox/tree/main/internal/providers)), rsyncs the dirty checkout, runs the suite, streams output, and releases the VM. The orchestrator opens a PR **only** when the verdict is green. Red verdicts return to the worker subagent for another iteration.

## Standing on the shoulders of

- **Peter Steinberger** ([@steipete](https://github.com/steipete)) — [`agent-scripts`](https://github.com/steipete/agent-scripts). The `triage` and `orchestrator` skills are direct adaptations of his `github-project-triage` and `maintainer-orchestrator` patterns. The hard "decision-ready PR" rule and the per-repo ledger structure are his.
- **shadcn** ([@shadcn](https://github.com/shadcn)) — [`/improve`](https://github.com/shadcn-ui/ui). The Mission tier (frontier-model plan, mid-tier execution) is shadcn's plan-then-execute split.
- **Nathan Wilbanks** ([@nathanwilbanks](https://x.com/nathanwilbanks_)) — [`agnt`](https://github.com/agnt-gg/agnt). The mission → goal → control → agent → tool loop hierarchy is his.
- **OpenClaw** ([@openclaw](https://github.com/openclaw)) — [`crabbox`](https://github.com/openclaw/crabbox). The sandbox gate that makes the whole loop trustable.

## License

MIT. See [LICENSE](LICENSE).
