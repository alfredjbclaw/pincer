#!/usr/bin/env python3
"""pincer run ledger — a durable history of every loop run + auto-pause.

Today's broken-clone failure was invisible: the loop failed silently every few
hours and nobody noticed. This records each run's outcome to an append-only
ledger, and lets the driver AUTO-PAUSE a loop that keeps failing for
infrastructure reasons (so it stops burning credits on a broken setup and pings
for a human) — while leaving a loop that simply finds no fix to keep running.

The classification + streak logic is pure over a list of records, so it is fully
unit-tested without files; `record`/`read` are the only disk-touching parts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

LEDGER_DEFAULT = Path.home() / ".openclaw" / "pincer" / "run-ledger.jsonl"


def _ledger_path() -> Path:
    import os
    return Path(os.environ.get("PINCER_RUN_LEDGER", LEDGER_DEFAULT))


def record(name: str, repo: str, result: str, scorecard: dict, ts: str,
           path: Optional[Path] = None) -> None:
    """Append one run's outcome. `ts` is passed in (caller stamps) so this stays
    free of clock side-effects. Best-effort: never raises."""
    sc = scorecard or {}
    row = {
        "ts": ts, "name": name, "repo": repo, "result": result,
        "merged": list(sc.get("merged", []) or []),
        "prd": list(sc.get("prd", []) or []),
        "failed_verification": list(sc.get("failed_verification", []) or []),
        "infra_failures": list(sc.get("infra_failures", []) or []),
        "no_winner": list(sc.get("no_winner", []) or []),
    }
    p = path or _ledger_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def read(name: Optional[str] = None, path: Optional[Path] = None) -> List[dict]:
    """All ledger rows (optionally filtered to one loop), oldest first."""
    p = path or _ledger_path()
    rows: List[dict] = []
    try:
        for line in Path(p).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if name is None or r.get("name") == name:
                rows.append(r)
    except FileNotFoundError:
        return []
    except Exception:
        return rows
    return rows


def classify(row: dict) -> str:
    """One run's outcome class:
      shipped — merged or PR'd something (engine worked, delivered)
      infra   — shipped nothing AND had infrastructure failures (broken setup)
      held    — budget/usage gate held the run (neutral, not a failure)
      no_fix  — shipped nothing, no infra failure (engine worked, found no fix)
    """
    if row.get("merged") or row.get("prd"):
        return "shipped"
    if row.get("result") in ("held_budget", "halted_usage"):
        return "held"
    if row.get("infra_failures"):
        return "infra"
    return "no_fix"


def consecutive_infra_failures(rows: List[dict]) -> int:
    """Trailing run of pure infrastructure failures. `held` runs are neutral
    (skipped); a `shipped` or `no_fix` run proves the engine works and resets
    the streak."""
    n = 0
    for r in reversed(rows):
        c = classify(r)
        if c == "infra":
            n += 1
        elif c == "held":
            continue
        else:
            break
    return n


def should_pause(rows: List[dict], threshold: int = 3) -> bool:
    """Pause a loop after `threshold` consecutive infra failures — a persistent
    environment/pincer problem, not a transient miss."""
    return consecutive_infra_failures(rows) >= threshold
