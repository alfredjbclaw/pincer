# Loop Reliability & Learning Overhaul

**Author:** Opus (audit + plan). **Implementation:** gpt-5.5/codex. **Target:** v0.2.3.
**Trigger:** the sql-metadata hourly loop wedged ~57 min on issue #8 and kept re-trying
the same issues every hour with no memory or improvement (2026-06-29).

## Audit findings (logs + code)

1. **Wedge / orphan-on-error (root cause of #8).** `runtime_adapter._codex_once` runs
   `subprocess.run(codex exec, timeout=1800)`. On timeout/parent-death only the *direct*
   codex child is signalled; codex's descendant processes and `~/.codex/tmp/arg0/*.lock`
   are **orphaned**. Later codex attempts contend on that lock → the run wedges. There is
   **no run-level watchdog**, and the 30-min per-worker timeout is very long.
2. **No per-issue history.** `run_ledger` is per-*loop*. Nothing records what was tried on
   a given issue, so each hourly tick re-attempts the same issues with the same approach —
   never learning, never improving.
3. **No issue-level selection.** The loop works the full configured issue list every tick.
   An issue rejected N times in a row (e.g. #13) is never deprioritized, cooled-down, or
   escalated/given-up.
4. **No per-(repo,issue) in-progress tracking.** Only a global tick PID-lock exists. It
   prevents overlapping *ticks*, but there's no record of *what* is in flight, and a run
   that runs >1h is handled only by that coarse lock.
5. **Silent failures.** Many bare `except Exception:`; driver stdout/stderr → an empty
   `/tmp/pincer-loop-driver.log`, so crashes/tracebacks are invisible. Real logging needed.

## Workstreams

### A. `tools/work_history.py` — per-issue attempt history + learning
Append-only JSONL at `~/.openclaw/pincer/work-history.jsonl` (env `PINCER_WORK_HISTORY`).
Mirror `run_ledger` style: pure query/classify logic (unit-tested w/o files) + thin
`record`/`read` disk layer.
- `record(repo, issue, run_id, runtime, patch_hash, sandbox, review, outcome, reason, ts)`
  — append one attempt. `outcome` ∈ {shipped, rejected, failed, no_winner, error}.
- `attempts(repo, issue)` / `attempt_count` / `consecutive_failures`.
- `seen_patch(repo, issue, patch_hash)` — did we already try this exact normalized fix?
- `should_skip(repo, issue, max_attempts=3, cooldown_hours=24, now_ts)` → (bool, reason):
  skip if ≥max_attempts non-shipping attempts, OR last failure within cooldown, OR the same
  patch_hash has already been tried and rejected.
- `failure_context(repo, issue, limit=3)` → str: a brief for the worker — "Prior attempts
  failed: <reasons>. Do NOT repeat these approaches: <hashes/summaries>. Try a materially
  different fix." Empty string when no history.

### B. `tools/inflight.py` — in-progress registry + run watchdog
Registry JSON at `~/.openclaw/pincer/inflight.json` (env `PINCER_INFLIGHT`).
- `claim(key, run_id, pid, ts)` → bool; refuse if a *live* (PID alive AND heartbeat fresh)
  claim for `key` exists. `key` = repo or `repo#issue`.
- `is_inflight(key)` → bool (live + fresh).  `heartbeat(key, ts)`.  `release(key)`.
- `reap_stale(max_age_s)` — drop entries whose PID is dead or heartbeat older than max_age.
- `run_with_timeout(fn, timeout_s, on_timeout)` helper: run a callable under a wall-clock
  cap in a watchdog thread; on breach call `on_timeout` (reap workers) and raise
  `RunTimeout`. The driver wraps each `run_spec` in this so a wedged run can't hold forever.

### C. Selection + learning wiring
- `loop_spec`/`triage`/`parallel_orchestrator`: before working the issue list, filter via
  `work_history.should_skip` (drop exhausted/cooled-down/already-tried issues); log what was
  skipped and why (no silent truncation).
- Inject `work_history.failure_context(repo, issue)` into the build + revise briefs so the
  worker sees prior failures and avoids repeating them.
- At finalize, `work_history.record(...)` each issue's outcome (patch_hash from
  `selection.normalize_patch`).

### D. Orphan-safe codex dispatch (the #8 fix)
- `runtime_adapter._codex_once`: launch codex via `Popen(..., start_new_session=True)` (own
  process group); enforce timeout; on timeout/exception `os.killpg(pgid, SIGKILL)` to reap
  ALL descendants, then best-effort remove stale `~/.codex/tmp/arg0/*.lock`. Same treatment
  for the claude-code wrapper path.
- Lower the default per-worker `timeout_seconds` and make the loop pass a tighter cap.

### E. Observability
- Driver: write real logs (rotating file under `~/.openclaw/pincer/logs/`), not the empty
  `/tmp` path; log each tick, each spec result, each skip, each timeout/kill.
- Replace the most damaging bare `except Exception:` swallows in the loop path with logged
  exceptions (don't change control flow, just stop hiding).

## Acceptance
- New modules fully unit-tested; **full suite stays green**.
- A simulated wedged worker is killed by the watchdog within the cap and leaves no orphan
  process or `.codex` lock.
- A re-run of a previously-exhausted issue is skipped with a logged reason.
- Version bumped to 0.2.3; pushed to GitHub.
