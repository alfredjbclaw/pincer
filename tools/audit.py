#!/usr/bin/env python3
"""pincer audit — executable bug-finder (the Mission tier, made runnable).

Runs a frontier read-pass over a repo and emits a structured list of REAL,
fixable work items (bugs, missing validation, broken edge cases). Output is
strict JSON the parallel orchestrator can execute.

This is deliberately conservative: high-precision findings only. A false bug
wastes a whole code→sandbox→review→gate cycle, so we'd rather miss a marginal
issue than invent one.

Usage:
    python3 tools/audit.py --workdir /path/to/repo [--max 12] [--json]
    from audit import audit_repo; findings = audit_repo("/path/to/repo")
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

FINDINGS_SCHEMA = """[
  {
    "title": "<short imperative title>",
    "brief": "<what is wrong, where (file/function), the expected behavior, and the acceptance check>",
    "type": "bugfix | adjustment | feature",
    "severity": "high | medium | low",
    "confidence": "high | medium"
  }
]"""

AUDIT_PROMPT = """Audit this repository for REAL, fixable defects. Read the source and the tests.

Find genuine problems only — wrong behavior, crashes, unhandled edge cases,
missing input validation, off-by-one, resource leaks, incorrect error types,
logic bugs, and clearly-missing tests for existing behavior. Do NOT invent
features, do NOT suggest stylistic preferences, do NOT speculate. If you are not
confident it is a real defect a maintainer would accept a fix for, leave it out.

For each finding, classify `type`:
- bugfix: corrects wrong/broken behavior
- adjustment: small correctness/robustness tweak
- feature: genuinely new capability (only if an existing test or doc implies it
  is expected but missing)

Write a STRICT JSON array (no prose, no markdown fence) to the file: {out}
matching exactly this schema:
{schema}

Cap at {max} findings, highest-severity first. If the repo looks clean, write [].
"""


def audit_repo(workdir: str, max_findings: int = 12, timeout: int = 900) -> list[dict]:
    workdir = str(Path(workdir).resolve())
    if not Path(workdir).is_dir():
        raise ValueError(f"not a directory: {workdir}")
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as f:
        out_path = Path(f.name)
        f.write("[]")
    prompt = AUDIT_PROMPT.format(out=out_path, schema=FINDINGS_SCHEMA, max=max_findings)
    cmd = [
        "codex", "exec", "--cd", workdir, "--sandbox", "workspace-write",
        "--skip-git-repo-check", prompt,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
    try:
        data = json.loads(out_path.read_text().strip() or "[]")
    except (json.JSONDecodeError, ValueError):
        data = []
    finally:
        out_path.unlink(missing_ok=True)
    # Normalize + keep only well-formed, high/medium-confidence findings.
    clean = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict) or not it.get("title") or not it.get("brief"):
            continue
        clean.append({
            "title": str(it["title"])[:120],
            "brief": str(it["brief"]),
            "type": it.get("type", "bugfix") if it.get("type") in
                    ("bugfix", "adjustment", "feature") else "bugfix",
            "severity": it.get("severity", "medium"),
            "confidence": it.get("confidence", "medium"),
        })
    return clean[:max_findings]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--max", type=int, default=12)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    findings = audit_repo(a.workdir, a.max)
    if a.json:
        print(json.dumps(findings, indent=2))
    else:
        print(f"{len(findings)} finding(s):")
        for i, f in enumerate(findings, 1):
            print(f"  {i}. [{f['type']}/{f['severity']}] {f['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
