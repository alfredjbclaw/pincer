# Selection cascade — turning coverage into realized score

Pincer's parallel orchestrator originally ran **one candidate per issue**: code →
sandbox → review → publish. That is the right posture for a cheap autonomous
maintainer loop, but it leaves the single biggest lever on the table.

The SWE-bench literature is unanimous: once a base agent works, **selection — not
generation — is the ceiling.** Every rigorous 2025–2026 study shows a 10–20 point
gap between *coverage* (some candidate is correct, ~70–80%) and *realized* score
(the right one gets picked, ~57–66%). Adding more coders barely moves the number;
**picking the right candidate with execution-grounded signals** does.

This module adds that selection layer. **Everything here is opt-in** — with the
default config Pincer behaves exactly as before (one candidate, one revise pass,
no reproduction tests).

## The pipeline, before and after

```
before:   issue ── coder ── sandbox ── review ── gate ── publish
                   (1 candidate)

after:    issue ─┬─ coder s0 ─┐
                 ├─ coder s1 ─┤   K candidates, each own worktree/branch
                 └─ coder s2 ─┘
                       │
                   sandbox each  (structured pass/fail counts)
                       │
                  [reproduction]  generate F→P test, mark flippers   (optional)
                       │
                  ── SELECT ──   cascade picks ONE winner per issue
                       │
                 finalize winner  (review + bounded revise loop)
                       │
                   gate ── publish
```

## The cascade (`tools/selection.py`)

Execution-grounded signals first, the LLM judge last. Each stage **narrows** the
surviving tier; no stage is ever allowed to empty it.

1. **Regression rank** — fewest previously-passing tests broken. This is the
   PASS_TO_PASS analog from the official harness and directly predicts
   "resolved." Reads the structured counts from `tools/test_results.py`.
2. **Reproduction flip** — among the regression-best tier, prefer candidates that
   flip a *valid* fail-to-pass reproduction test (one proven to fail on the
   unpatched base). Skipped if no valid repro test or nothing flips.
3. **AST-normalized majority vote** — canonicalize each patch (drop
   comments/whitespace, order-independent) and take the consensus edit. Only
   decisive when ≥2 candidates agree.
4. **Opus reviewer tie-break** — only if still tied, and only on the survivors,
   so the expensive judge runs at most once per finalist.

The winning stage is recorded per issue in the run state (`selection.<issue>.stage`)
— that is the **selection-gap diagnostic**: on a bench run you can compare which
stage made the cut against ground truth to see where to invest next.

## Enabling bench-grade selection

In `~/.openclaw/pincer.toml`:

```toml
[selection]
samples          = 5      # K candidates per issue
max_revise_iters = 5      # deeper execution-feedback loop
repro_tests      = true   # generate + F→P-filter reproduction tests (heavy)
```

…or per-run on the orchestrator:

```bash
python3 tools/parallel_orchestrator.py --repo OWNER/NAME --workdir /clone \
    --issues 1,2,3 --samples 5 --max-revise-iters 5 --repro-tests
```

Cost scales ~linearly with `samples`, and `repro_tests` adds one extra sandbox
VM run per candidate (serialized — Apple VZ is `max_concurrent=1`). Leave both at
their defaults for the production maintainer loop; turn them up for SWE-bench.

## Cost / quality knobs

| Knob | Default | Bench | Effect |
|---|---|---|---|
| `samples` | 1 | 5–10 | candidates per issue (the thing selection chooses among) |
| `max_revise_iters` | 1 | 5–8 | execution-feedback loop depth, critic-interpreted |
| `repro_tests` | off | on | strongest single verifier; heavy |
| `interpret_failures` | on | on | Opus reads the failure before the coder retries |

## What is deliberately *not* here yet

- True multi-sample localization diversity (LLM-sampled location sets). The
  current localization is grep-rank + AST skeleton; the agentic coders navigate
  from there, which the research (Augment) finds is usually sufficient.
- Cross-model candidate ensembling (a second coder model). Slots in as another
  `samples` source; cost-sensitive, left for a follow-up.
- A trained/learned critic for trajectory selection (OpenHands TD-critic). Only
  worth it after the test-grounded cascade is in — which it now is.
