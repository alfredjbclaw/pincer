#!/usr/bin/env python3
"""pincer oneshot — hand it a repo, it works the whole queue in one shot.

Chains: (audit OR existing issues) -> parallel fix-all -> ship bugfixes,
PR features -> report. This is the "give it a project and it finds/fixes the
bugs autonomously" entry point.

Modes:
  --all-issues          work every open issue in the repo
  --issues 1,2,3        work a specific set
  --audit               run the bug-finder, file each finding as an issue, work them

Usage:
  python3 tools/oneshot.py --repo OWNER/NAME --workdir /path/to/clone --audit
  python3 tools/oneshot.py --repo OWNER/NAME --workdir /path/to/clone --all-issues
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))
sys.path.insert(0, "/Users/alfred/.openclaw/workspace/tools")

import parallel_orchestrator as po
import audit as audit_mod

try:
    from telegram_alert import send_alert
except Exception:
    def send_alert(msg): print("[alert]", msg)


def sh(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.stdout, p.stderr, p.returncode


def open_issue_numbers(repo: str) -> list[int]:
    out, _, _ = sh(["gh", "issue", "list", "-R", repo, "--state", "open",
                    "--limit", "100", "--json", "number"])
    try:
        return sorted(it["number"] for it in json.loads(out))
    except Exception:
        return []


def file_findings_as_issues(repo: str, findings: list[dict]) -> list[int]:
    nums = []
    for f in findings:
        body = (f"{f['brief']}\n\n"
                f"_Filed by pincer audit. type={f['type']} severity={f.get('severity')} "
                f"confidence={f.get('confidence')}._")
        out, err, rc = sh(["gh", "issue", "create", "-R", repo,
                           "--title", f["title"], "--body", body,
                           "--label", "pincer-audit"])
        # gh prints the issue URL; extract the trailing number.
        url = (out or "").strip().splitlines()[-1] if out.strip() else ""
        if url and url.rsplit("/", 1)[-1].isdigit():
            nums.append(int(url.rsplit("/", 1)[-1]))
    return nums


def ensure_label(repo: str):
    sh(["gh", "label", "create", "pincer-audit", "-R", repo, "--color", "5319e7",
        "--description", "Found by pincer audit", "--force"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--workdir", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all-issues", action="store_true")
    g.add_argument("--issues", help="comma-separated issue numbers")
    g.add_argument("--audit", action="store_true")
    ap.add_argument("--max-coders", type=int, default=6)
    ap.add_argument("--max-findings", type=int, default=12)
    ap.add_argument("--no-merge", action="store_true")
    a = ap.parse_args()

    if a.audit:
        send_alert(f"🔍 Oneshot AUDIT — scanning {a.repo} for real bugs…")
        findings = audit_mod.audit_repo(a.workdir, a.max_findings)
        if not findings:
            send_alert(f"✅ Oneshot: audit of {a.repo} found no fixable defects. Nothing to do.")
            print("audit found no findings")
            return 0
        ensure_label(a.repo)
        issues = file_findings_as_issues(a.repo, findings)
        send_alert(f"🔍 Audit filed {len(issues)} issue(s) on {a.repo}: "
                   + ", ".join(f"#{n}" for n in issues) + ". Dispatching fixers…")
    elif a.all_issues:
        issues = open_issue_numbers(a.repo)
        if not issues:
            send_alert(f"✅ Oneshot: {a.repo} has no open issues.")
            return 0
    else:
        issues = [int(x) for x in a.issues.split(",") if x.strip()]

    state = po.run(a.repo, a.workdir, issues, a.max_coders, allow_merge=not a.no_merge)

    cands = state.get("candidates", {})
    merged = [k for k, c in cands.items() if c.get("published") == "auto_merged"]
    prd = [k for k, c in cands.items() if str(c.get("published", "")).startswith("pr")]
    failed = [k for k, c in cands.items() if not c.get("committed")]
    print(json.dumps({"repo": a.repo, "issues": issues, "merged": merged,
                      "prd": prd, "failed": failed}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
