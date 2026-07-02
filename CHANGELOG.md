# Changelog

## 0.2.4 — 2026-07-01

Learn from others' solutions, don't vanish exhausted issues, don't jointly
saturate codex across loops, and stop paying the toolchain install on every
fix-round. Hardening pass ahead of the first real greenfield build (Project
Command Center).

### New

- **Prior-art harvester** (`tools/prior_art.py`, opt-in, default OFF). GitHub
  code/repo search for a problem statement → rank top-N by repository **health**
  (stars / recency / visible tests, not raw relevance) → fetch a few key files →
  distill into a compact, **cited** pattern brief that feeds both the greenfield
  planner (`fleet_build`) and the maintainer coder briefs. **Reference, not
  transplant:** it extracts structure and approach with source attribution and
  never emits license-encumbered source for copy-paste. Enable with
  `[prior_art].enabled = true`; knobs for repo/file/search limits and cache dir.
- **Terminal "needs human" escalation** (`tools/work_history.py`, `loop_spec`).
  An issue that exhausts attempts / crosses the consecutive-failure threshold is
  now marked **escalated**: `run_spec` surfaces `result=escalated` with the
  issue ids and posts a loud "NEEDS HUMAN" alert, instead of silently cooling the
  issue down forever. Clear with `work_history.clear_escalation(...)`.
- **Cross-loop dispatch ceiling** (`tools/global_gate.py`). A shared, file-backed
  global slot gate caps concurrent codex/VM dispatch **across independent loop
  processes** — `mem_monitor` only guarded RAM per-host. Waits for a slot, then
  defers the dispatch if the wait expires rather than bursting past the cap;
  crashed holders are stale-reaped. Knobs: `global_max_concurrent`,
  `global_gate_wait_seconds`, `global_gate_stale_seconds`.
- **Warm-box toolchain reuse** (`tools/sandbox_gate.py`). Optional reusable
  ("warm") crabbox box via `[sandbox].box_id` (or `PINCER_SANDBOX_BOX_ID`). The
  language toolchain is provisioned onto it **once** (host-recorded in
  `~/.openclaw/pincer/provisioned-boxes.json`), then every later fix-round runs
  the **bare** test command on the reused box with no apt prelude — removing the
  ~2m40s per-round toolchain reinstall that dominated multi-round builds. The
  Crabbox argv contract forbids `||`/`command -v` guards, so the skip decision
  lives host-side. Unset `box_id` keeps the legacy fresh-lease-per-run behavior.

### Hardened

- **Orphan-reap on the codex *error* path** (`tools/runtime_adapter.py`). Codex
  lock reaping previously fired only on timeout / exception. A clean nonzero exit
  (credit/auth/error) that falls back to Claude Code could still leave a stale
  lock — the exact #8 wedge class. `_run_codex` now reaps on any nonzero exit and
  on exhausted rate-limit retries, before the caller falls back.

## 0.2.3 — 2026-07-01

Loop-reliability overhaul: the loop now learns from its own past attempts and
can no longer wedge, double-run, or silently swallow errors. Triggered by the
sql-metadata loop that wedged ~57 min on a single issue (#8).

### New

- **Per-issue work history + anti-repeat learning** (`tools/work_history.py`).
  Every issue attempt is recorded (runtime, normalized patch hash, sandbox
  result, review verdict, outcome, reason). Before dispatch, `run_spec` skips
  issues that are exhausted (`max_attempts`), cooling down after a recent
  failure (`cooldown_hours`), or that would re-submit an already-failed patch
  (`seen_patch`). Prior-failure context is injected into both the initial and
  revision coder briefs so a fresh attempt starts from what already didn't work.
- **In-flight registry + run watchdog** (`tools/inflight.py`). The loop driver
  claims a per-repo/-loop key before running, heartbeats during, and releases in
  a `finally`. Stale claims are reaped at each tick start; a run exceeding
  `PINCER_RUN_TIMEOUT_S` (default 1800s) raises `RunTimeout`, unblocks the tick,
  and fires a best-effort reap — so one wedged issue can never stall the driver.
- **Orphan-safe codex dispatch** (`tools/runtime_adapter.py`). Codex runs are
  dispatched so that an errored/fallback path can no longer leave a dead process
  holding its lock — the direct fix for the #8 wedge.

### Hardened

- **Real rotating driver logs** (`~/.openclaw/pincer/logs/loop-driver.log`,
  1 MB × 5, override via `PINCER_LOOP_LOG`). Tick start/quiet/skip/timeout/crash
  and per-loop results are now logged.
- **De-swallowed exceptions.** Bare `except: pass` swallows across the driver,
  orchestrator, and loop spec now log via `LOG.exception` instead of vanishing.

### Tuning

- New env knobs: `PINCER_RUN_TIMEOUT_S` (run watchdog timeout),
  `PINCER_INFLIGHT_MAX_AGE_S` (stale-claim reap age), `PINCER_LOOP_LOG`
  (driver log path).

## 0.2.2 — 2026-06-29

Multi-language sandbox provisioning, loop health, and memory-aware concurrency.

### New

- **Declarative multi-language toolchain provisioning** (`tools/toolchain.py`).
  The sandbox can prepend an apt-only install prelude to the test command so the
  Crabbox VM has the right runtime for any language (node/go/python/rust/…),
  driven by a declarative toolchain list; `sandbox_gate.gate()` gains `toolchain`
  and `reap_stale` params (stale-lease reaping to prevent Apple VZ VM stacking).
  Also threaded into `fleet_build`.
- **Memory-aware sandbox concurrency** (`tools/mem_monitor.py` + orchestrator
  `sandbox_slot`). The hard single-VM lock becomes a memory-aware gate: pincer
  runs up to `[sandbox].max_concurrent` Crabbox VMs in parallel, but starts
  another only while host free RAM can absorb it above a floor (`min_free_gb`) —
  so it uses the box's capacity when idle and backs off when memory is tight,
  even across multiple pincer processes (the check reads host RAM, not a
  per-process counter). RAM is sampled around each VM to
  `~/.openclaw/pincer/mem-samples.jsonl` for usage/leak tracking
  (`detect_leak`). New `[sandbox]` knobs: `max_concurrent` (default raised to 2),
  `min_free_gb`, `vm_memory_gb`, `vm_gate_max_wait_s`. Complements the existing
  host `memory-watchdog` cron (alerting safety net). At `max_concurrent=1` the
  behavior is identical to the old single lock.

- **Run ledger + auto-pause** (`tools/run_ledger.py`). Every loop run's outcome is
  appended to `~/.openclaw/pincer/run-ledger.jsonl`. The driver classifies each
  run (shipped / infra-failure / held / no-fix) and **auto-pauses a loop after 3
  consecutive infrastructure failures** (broken env, like the corrupted-clone
  case) with a critical alert — so it stops burning credits and pings a human,
  instead of failing silently every few hours. A "no fix found" run proves the
  engine works and resets the streak; a budget-held run is neutral.
- **Preflight** (`tools/preflight.py`). Before dispatching coders, verify the
  basics (git, GitHub auth, crabbox). A blocker (e.g. unauthenticated `gh`) halts
  the run with one clear reason; warnings (e.g. crabbox missing) surface but don't
  stop it. Wired into `loop_spec.run_spec`.
- **Cascade validation harness** (`tools/validate_cascade.py`). `--offline`
  (default) drives the selection cascade through realistic multi-stage scenarios
  and asserts each is resolved by the expected stage (regression → reproduction →
  majority-vote → reviewer → single). `--live` runs the real pipeline with
  `--samples N` and reports the per-issue selection stage — guarded to refuse if a
  pincer/crabbox run is already in flight (never contends).

## 0.2.1 — 2026-06-29

Quieter, smarter run alerts.

### Changed

- **Edit-in-place status board (default).** A run now posts ONE status message
  that edits itself in place as it progresses, sent silently — so a normal run
  never buzzes the phone. A `critical` event (🚨 INFRA FAILURE / crash) still
  sends one discrete notification. Set `[alerts].style = "thread"` for the old
  reply-chain of separate messages. New `notify.LiveBoard` (drop-in for
  `AlertThread`); backend gains additive `edit_message` + `silent=`.
- **Local run logs.** The full blow-by-blow (every stage + candidate, including
  lines the quiet board hides) is written to
  `~/.openclaw/pincer/run-logs/<ts>-<repo>.md`, one per run, auto-pruned to the
  last 50 — a debug feed independent of Telegram.

## 0.2.0 — 2026-06-29

Standalone portability, live-run reliability, a pre-bench selection cascade, and SWE-bench harness plumbing.

### Standalone portability (public release)

- **Removed every hardcoded workspace path and made the two private integrations optional plugins**, so Pincer now runs from a fresh clone with no external setup.
  - **Alerting** loads through a new `tools/notify.py` shim: it uses a real backend if `$PINCER_NOTIFY_MODULE` (or an importable `telegram_alert`) is present, otherwise falls back to a stdout no-op with the same `send_alert` / `AlertThread` interface — alert call sites are unchanged.
  - **Codex usage gate** is now opt-in via `$PINCER_USAGE_GATE` (path to the gate script); when unset or missing it is skipped and treated as safe-to-dispatch (fail-open), never blocking a run.

### Reliability: self-healing workdir + single alert thread

Fixes two issues from the live sql-metadata loop runs.

### Fixed

- **Loop runs failed with `fatal: invalid reference: master` and nothing
  started.** Root cause was a corrupted/purged `/tmp` clone whose default branch
  no longer resolved — not model routing. Added `ensure_clone()`: before a run,
  re-clone if the workdir is missing/broken, else fetch + hard-reset, returning a
  default branch guaranteed to resolve. `default_branch()` now only returns a
  name whose `origin/<name>` resolves, and `make_worktree()` falls back to
  `origin/<branch>` if no local branch resolves. The orchestrator (and the audit
  paths in loop_spec/oneshot) self-heal the workdir at the top of every run.
- **Loop workdir moved off `/tmp`** to `~/.openclaw/pincer/clones/<name>` so a
  `/tmp` purge can't break it (self-heal covers it regardless).

### Changed

- **Alert reply-threading across the board.** Every alert in a run now replies to
  one root (the process's start message) instead of being its own message. A
  single `AlertThread` is created at the outermost entry (loop driver tick /
  oneshot) and threaded down through `loop_spec.run_spec` and
  `parallel_orchestrator.run` (new `thread=` param) — so the driver tick, each
  loop's START/done, and all orchestrator stage alerts form one Telegram
  reply-chain.

### Pre-bench selection cascade

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

### SWE-bench harness plumbing (`tools/bench/`)

Turns a Pincer run into official-harness predictions and grades them with the
real SWE-bench evaluator (never Pincer's own sandbox). See `BENCH.md`.

- `predictions.py` — `model_patch` = the literal `git diff` vs the checked-out
  `base_commit` (the #1 apply-failure fix), test-file edits stripped, trailing
  newline preserved; JSONL emit/read.
- `dataset.py` — load instances from a local `.json/.jsonl` (no deps) or Hugging
  Face (`datasets`, optional); handles the string-encoded FAIL/PASS_TO_PASS.
- `runner.py` — one instance → clone @ `base_commit` → localization → `--samples`
  coders → sandbox-ranked selection cascade → `model_patch`.
- `grade.py` — official-harness argv builders + `preflight()` (flags missing
  Docker, arm64 non-canonical images, missing `swebench`) + gold sanity run.
- `run_lite.py` — CLI; writes predictions incrementally, grades only when Docker
  preflight passes.

Grading is Docker-only and canonical numbers need x86_64 — both flagged by
preflight; this dev box (arm64, no Docker) can produce predictions but not a
canonical graded score.

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
