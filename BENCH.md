# Running Pincer on SWE-bench

This is the plumbing that turns a Pincer run into official-harness predictions
and grades them. It lives in `tools/bench/` and is intentionally separate from
the GitHub maintainer loop.

## The two sandboxes (don't confuse them)

| | What it is | Used for | Status |
|---|---|---|---|
| **Crabbox** | Pincer's own throwaway-VM sandbox (Apple VZ) | running candidate fixes' tests so the **selection cascade can rank** them | installed (v0.31.0), works |
| **Docker + SWE-bench images** | the official benchmark grader | the **authoritative score** — the only number that counts | requires Docker (not installed here) |

The golden rule from the research: **Pincer never grades itself.** Crabbox helps
*pick* the best candidate; the official Docker harness *scores* the final patch.

## Prerequisites to actually grade

1. **Docker** — the official grader is Docker-only. `brew install colima docker && colima start` (lightweight) or Docker Desktop.
2. **x86_64 for canonical numbers.** This Mac is arm64; the official images are
   Intel, and arm64 images are experimental → scores are **non-canonical** (fine
   for dev, not for a leaderboard claim). For comparable numbers, grade on a
   cheap cloud Intel box.
3. `pip install swebench datasets` in the Python that runs the grade.
4. ~120 GB free disk at `--cache_level env` (you have plenty).

The `preflight()` check reports exactly which of these are missing before any
grade runs — and `run_lite` refuses to grade without Docker.

## Pipeline

```
SWE-bench instance (problem_statement + base_commit)
   → clone @ base_commit (detached worktree)
   → hierarchical localization
   → --samples K coders (the cascade picks among them)
   → crabbox sandbox (best-effort ranking)
   → selection cascade → ONE candidate
   → git diff vs base_commit  →  model_patch   (test edits stripped)
   → predictions.jsonl
   → OFFICIAL harness grades it in Docker
```

## Commands

Produce predictions only (no Docker needed):

```bash
python3 -m tools.bench.run_lite \
    --dataset-name princeton-nlp/SWE-bench_Lite --limit 5 \
    --work-root /tmp/pincer-bench --out preds.jsonl \
    --samples 3 --max-revise-iters 3
```

…or from a local instance file (no `datasets` dep):

```bash
python3 -m tools.bench.run_lite --dataset-file my_subset.jsonl --out preds.jsonl
```

Produce **and grade** (needs Docker + swebench), gold sanity first:

```bash
python3 -m tools.bench.run_lite \
    --dataset-name princeton-nlp/SWE-bench_Lite --limit 300 \
    --out preds.jsonl --samples 5 --max-revise-iters 5 --repro-tests \
    --grade --run-id pincer_lite_v1
```

`--dry-run` prints the grade commands without executing (inspect the exact
official-harness invocation). `--skip-gold` skips the gold sanity run.

## Reading the result

The official harness writes a `report.json` with `submitted / completed /
resolved` counts. `resolved / total` is your score. Per-instance `resolved`
flags let you diff Pincer's pick against ground truth — and the run's
`selection.<issue>.stage` (from the cascade) tells you which signal made the
call, so you can see whether you're leaking points at generation (low coverage)
or selection (coverage high, realized low). That's the diagnostic the whole
cascade exists to surface.

## Order of operations (recommended)

1. **Lite, predictions-only** on a handful (`--limit 5`) to shake out the live
   crabbox path on real repos (the one part with no unit-test coverage).
2. **Gold sanity** once Docker is up (`--predictions_path gold` ~100%) — proves
   the environment, not the model.
3. **Lite full (300)**, graded, on x86_64 for canonical numbers.
4. Only then Verified (500) / Pro.
