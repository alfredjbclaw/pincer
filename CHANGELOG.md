# Changelog

## Unreleased — loop health: run ledger, auto-pause, preflight, cascade validation

Staged on `feat/buildouts`; not yet merged.

### New

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
