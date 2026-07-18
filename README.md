# 🦞 pincer

![version](https://img.shields.io/badge/version-0.2.5-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![tests](https://img.shields.io/badge/tests-pytest-brightgreen)
![python](https://img.shields.io/badge/python-3-blue)
![built for OpenClaw](https://img.shields.io/badge/built%20for-OpenClaw-e8632c)

**An autonomous maintainer loop that earns the right to open a pull request.**

**Contents:** [The problem](#the-problem-it-solves) · [Watch it work](#watch-it-work) · [Install](#install) · [Quick start](#quick-start) · [The four skills](#the-four-skills) · [Configuration](#configuration) · [The sandbox gate](#the-sandbox-gate) · [Safety model](#safety-model) · [Benchmarking](#benchmarking-and-why-pincer-never-grades-itself) · [Under the hood](#whats-under-the-hood) · [Credits](#standing-on-the-shoulders-of)

Pincer keeps a fleet of repositories healthy without a human in the loop — it audits, plans, triages the issue queue, writes the fix, and opens the PR. The catch, and the whole point: **it is not allowed to open that PR until the change has passed its own test suite inside a clean throwaway VM.** No green sandbox, no PR. No exceptions.

That single rule is what separates a tool you'd actually run against your repos from a party trick.

```text
MISSION   (Opus,    daily)    audit-and-plan  ─┐
GOAL      (Sonnet,  hourly)   triage           │
CONTROL   (Sonnet,  5m)       orchestrate      │  pincer
AGENT     (Codex,   per item) worker subagent  │
SANDBOX   (Crabbox, per task) clean VM verdict  │
TOOL      (Haiku,   per call) gh / git / test  ─┘
```

Each tier is matched to the cost and cadence of its job: the expensive frontier model plans once a day, the cheap model runs every five minutes, and the sandbox has the final say before anything reaches a human.

---

## The problem it solves

Everyone can now wire an LLM to `gh` and `git` and watch it open pull requests. Almost nobody can trust the result. The failure mode is always the same — the model is confident, the diff looks plausible, the tests were never actually run, and you find out in review (or worse, in production).

Pincer's design starts from that distrust and builds outward. Three principles fall out of it:

1. **Plan and execute are different jobs, priced differently.** Deciding *what* to do is worth a frontier model called sparingly. Doing it is worth a mid-tier model called constantly. Collapsing the two is how you go broke or go dumb.
2. **The model never grades its own homework.** Verification runs in a sandbox the worker doesn't control, on a suite the worker can't quietly edit around. The orchestrator reads a verdict, not a vibe.
3. **Everything leaves structured evidence.** Run outcomes, issue attempts, dispatch cost, memory samples, driver decisions, and per-run stage logs are written to separate on-disk artifacts. If Pincer did something, the relevant subsystem leaves a bounded record explaining what happened — the difference between an autonomous system and an unaccountable one.

---

## Watch it work

A single continuous run against a repo looks conceptually like this when the typed artifacts are joined:

```text
09:00  MISSION   alfredjbclaw/pincer-testbed  → plan written (7 priorities, 3 autonomous)
09:03  GOAL      triage: 3 autonomous · 2 needs-owner · 4 defer
09:03  CONTROL   claim #142 "flaky retry test on slow CI"  → dispatch AGENT (codex)
09:11  AGENT     branch fix/142-retry-backoff · 4 files · tests added
09:11  SANDBOX   crabbox lease applevz-7f3a · running suite…
09:14  SANDBOX   RED  (2 failing: test_backoff_jitter)      → return to worker
09:19  AGENT     revision 2 · jitter clamp fixed
09:19  SANDBOX   crabbox lease applevz-1c8e · running suite…
09:22  SANDBOX   GREEN (231 passed)                         → clear to PR
09:22  CONTROL   opened PR #148  (allowMerge=false → owner review)
```

The red verdict at 09:14 is the feature, not the bug. The worker got it wrong, the sandbox caught it, and it iterated — all without a human being pulled in. A human only appears at 09:22, looking at a PR that already passes.

---

## Install

```sh
# 1. Install Crabbox (the sandbox control plane)
brew install openclaw/tap/crabbox
crabbox doctor --provider applevz   # Apple Silicon default; 60+ cloud providers also supported

# 2. Clone pincer and link its skills into your OpenClaw workspace
git clone https://github.com/alfredjbclaw/pincer ~/Projects/pincer
for s in audit-and-plan triage orchestrator keeper; do
  ln -s ~/Projects/pincer/skills/$s ~/.openclaw/workspace/skills/$s
done

# 3. Configure
cp ~/Projects/pincer/config.toml.example ~/.openclaw/pincer.toml
$EDITOR ~/.openclaw/pincer.toml
```

## Quick start

```sh
keeper audit owner/repo      # one-time pass: audit → plan → triage
keeper run   owner/repo      # continuous orchestration (5-minute cadence)
python3 tools/pincer_diagnostics.py       # bounded system health snapshot
python3 tools/dispatch_telemetry.py       # model/token/retry summary
tail ~/.openclaw/pincer/logs/loop-driver.log
```

`keeper` is the ergonomic front door — one command drives the whole Mission → Goal → Control pipeline against your configured repo allowlist.

---

## The four skills

| Skill | Tier | Cadence | What it does |
|---|---|---|---|
| **audit-and-plan** | Mission | daily | Frontier model reads code, CI, dependencies, and changelog, then writes a structured plan of priorities and verification paths to `plans/<repo>-<date>.toml`. |
| **triage** | Goal | hourly | Merges the current plan with the live GitHub queue and emits a URL-first report bucketed **Autonomous / Needs-owner / Defer**. |
| **orchestrator** | Control | 5 min | Dispatches autonomous items to worker subagents, gates every output through a `crabbox` verdict, and opens PRs only on green. |
| **keeper** | Meta | on demand | Single-command runner over the whole pipeline with sane config defaults. |

Each skill is a standalone `SKILL.md` — readable, forkable, and credited to the pattern it came from (see below).

## Configuration

`~/.openclaw/pincer.toml` — the model assignment *is* the architecture:

```toml
owner = "alfredjbclaw"

[models]
mission = "anthropic/claude-opus-4-7"      # plan  — expensive, daily
goal    = "anthropic/claude-sonnet-4-6"    # triage — mid-tier, hourly
control = "anthropic/claude-sonnet-4-6"    # orchestrate — mid-tier, every 5m
agent   = "openai/gpt-5.5"                 # execute — Codex CLI worker
tool    = "anthropic/claude-haiku-4-5"     # gh/git/test — cheap, per call

[runtime]
primary  = "codex"            # codex | claude-code
fallback = "claude-code"      # graceful degrade on credit-exhausted / auth_expired / 3-strikes

[sandbox]
provider = "applevz"          # Apple Silicon default; any crabbox provider works
gate     = "every"            # every | tagged-needs-isolation

[[repos]]
name          = "alfredjbclaw/pincer-testbed"
allowDelegate = true
allowMerge    = false         # PR-only by default; merge stays a human decision
allowRelease  = false
```

## The sandbox gate

Every worker runs the AGENT loop locally — implement, write tests, commit on a branch. Before the orchestrator opens a PR, the diff is validated by:

```sh
crabbox run --provider applevz -- <repo-test-suite>
```

Crabbox leases a clean throwaway VM (Apple VZ by default, configurable to any of [60+ supported providers](https://github.com/openclaw/crabbox/tree/main/internal/providers)), rsyncs the dirty checkout, runs the suite, streams output, and releases the VM. Green → PR. Red → back to the worker for another iteration. The maintainer never sees a red diff.

## Safety model

Autonomy is only useful if it's bounded. Pincer's defaults assume it should never surprise you:

- **Human-in-the-loop by default.** `allowMerge` and `allowRelease` are `false` out of the box — Pincer opens PRs, humans merge them. Turning either on is a deliberate per-repo choice, not a default you back into.
- **Explicit repo allowlist.** Pincer only touches repositories you list in `[[repos]]`. There is no "discover and fix everything" mode.
- **Sandboxed verification the worker can't tamper with.** Verdicts come from a clean VM running the repo's real suite — not from the model that wrote the diff.
- **Typed, private observability.** Run outcomes, work attempts, dispatch usage, memory samples, driver ticks, and detailed run stages use separate artifacts under `~/.openclaw/pincer/`. Files are owner-only and diagnostics report bounded metadata instead of prompts or raw model output.
- **Graceful degradation, not silent failure.** Runtime failover (Codex → Claude Code on credit-exhausted / auth-expired / three-strikes) and orphan-VM reaping are built in; a stuck run stops, it doesn't thrash.

## Benchmarking (and why Pincer never grades itself)

Pincer ships a separate SWE-bench harness (`tools/bench/`) that turns a run into official-format predictions. It enforces a hard separation most agent benchmarks blur:

- **Crabbox** runs candidate tests so the selection cascade can *rank* fixes — it helps Pincer **pick**.
- **The official Docker harness** produces the only number that counts — it **scores**.

The worker's own sandbox is never the grader. `preflight()` reports exactly which prerequisites are missing before any grade runs, and `run_lite` refuses to grade without Docker. Details in [BENCH.md](BENCH.md).

## Observability

Pincer deliberately avoids a single ambiguous activity log. The principal artifacts are:

| Artifact | Purpose |
|---|---|
| `run-ledger.jsonl` | One classified outcome per loop run; drives infrastructure-failure auto-pause. |
| `work-history.jsonl` | Per-issue attempt/outcome history, patch hashes, cooldowns, and escalation state. |
| `dispatch-telemetry.jsonl` | One private attempt receipt and one outcome per model dispatch: runtime, model, effort, duration, token counters, retry/resume/fallback flags, and normalized failure class. Prompts and model text are never stored. |
| `mem-samples.jsonl` | Host-memory samples used for concurrency and leak analysis. |
| `logs/loop-driver.log` | Rotating driver decisions, watchdog events, and per-loop results. |
| `run-logs/*.md` | Detailed stage-by-stage record for an individual run, retained to the most recent 50. |

`python3 tools/pincer_diagnostics.py` provides a read-only, redacted health snapshot. It reports missing/stale evidence, aggregate failure streaks, dispatch cost and cache ratios, allowlisted fleet state, active gates, and whether the driver is loaded. Arbitrary persisted labels and identifiers are never emitted, and the command never enables a loop or changes state.

## What's under the hood

- **Pytest suite** covering orchestration logic, selection cascade, runtime adapters, sandbox gate, diagnostics, and telemetry—not just smoke tests.
- **28 tool modules** — audit, preflight, sandbox gate, runtime adapter (Codex↔Claude Code failover), reproduction-test generation, selection cascade, publication gate, and typed observability.
- **v0.2.5**, MIT-licensed, manifest-conformant OpenClaw skill bundle.

---

## Standing on the shoulders of

Pincer is a composition of other people's good ideas. The credit is load-bearing:

- **Peter Steinberger** ([@steipete](https://github.com/steipete)) — [`agent-scripts`](https://github.com/steipete/agent-scripts). The `triage` and `orchestrator` skills adapt his `github-project-triage` and `maintainer-orchestrator` patterns; the hard "decision-ready PR" rule and per-repo ledger are his.
- **shadcn** ([@shadcn](https://github.com/shadcn)) — [`/improve`](https://github.com/shadcn-ui/ui). The plan-then-execute split (frontier plan, mid-tier execution) is his.
- **Nathan Wilbanks** ([@nathanwilbanks](https://x.com/nathanwilbanks_)) — [`agnt`](https://github.com/agnt-gg/agnt). The mission → goal → control → agent → tool loop hierarchy is his.
- **OpenClaw** ([@openclaw](https://github.com/openclaw)) — [`crabbox`](https://github.com/openclaw/crabbox). The sandbox gate that makes the whole loop trustable.

## License

MIT. See [LICENSE](LICENSE).
