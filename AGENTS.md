# pincer: rules for builders

Project rules live in `CLAUDE.md`; this file carries the test budget.

## Tests (~/.openclaw/workspace/memory/decisions/test-budget.md)
Default check: `python3 -m pytest -q` — measured 4.1s (2026-11-20)
Full suite: `python3 -m pytest -q` (the default check is the whole suite; it is already well under the 2-minute budget).

- While working, run only the touched test file; run the default check once at the end.
- Check speed with `~/.openclaw/workspace/tools/ci-speed/measure.py pincer`.
