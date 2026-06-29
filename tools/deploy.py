#!/usr/bin/env python3
"""deploy — the "point Alfred at a project" button.

Create/manage/run portable loop specs. Deploying a new project is one command:

  deploy add --name sqlmeta --mode fix --repo alfredjbclaw/sql-metadata \
      --workdir ~/.openclaw/pincer/clones/sqlmeta --target all-issues \
      --autonomy auto --schedule 6h
  # Use a persistent workdir, NOT /tmp — /tmp gets purged and the clone breaks.
  # (The orchestrator self-heals via ensure_clone, but a stable path is cleaner.)
  deploy run sqlmeta          # fire once now (detached, Telegram-tracked)
  deploy list                 # see all deployed loops
  deploy enable/disable/remove sqlmeta

The autonomy driver (loop_driver.py, on cron) then runs the enabled, scheduled
ones unattended.
"""
from __future__ import annotations
import argparse, subprocess, sys, dataclasses
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))
from loop_spec import LoopSpec, LOOPS_DIR


def cmd_add(a):
    spec = LoopSpec(
        name=a.name, mode=a.mode, repo=a.repo, workdir=a.workdir, target=a.target,
        autonomy=("pr-only" if a.pr_only else "auto"), max_coders=a.max_coders,
        max_findings=a.max_findings, schedule=a.schedule, enabled=True,
        budget={"window_pct": a.budget_window, "week_pct": a.budget_week})
    spec.save()
    print(f"deployed loop '{spec.name}' -> {spec.path}")
    print(f"  {spec.mode} | {spec.repo} | autonomy={spec.autonomy} | schedule={spec.schedule}")


def cmd_list(a):
    specs = LoopSpec.all()
    if not specs:
        print("no deployed loops")
        return
    for s in specs:
        flag = "on " if s.enabled else "off"
        print(f"[{flag}] {s.name:22} {s.mode:5} {s.repo:30} autonomy={s.autonomy:7} "
              f"sched={s.schedule:8} last_run={s.last_run or 'never'}")


def cmd_run(a):
    spec = LoopSpec.load(a.name)
    # fire detached so it survives, with a reporter that Telegrams the scorecard
    runner = (f"import sys; sys.path.insert(0,'{THIS}'); sys.path.insert(0,'/Users/alfred/.openclaw/workspace/tools');"
              f"from loop_spec import LoopSpec, run_spec; "
              f"r=run_spec(LoopSpec.load('{a.name}')); "
              f"from telegram_alert import send_alert; "
              f"send_alert('🏁 Loop '+repr('{a.name}')+' done: '+str(r))")
    log = f"/tmp/loop-{a.name}.log"
    subprocess.Popen(["nohup", "python3", "-c", runner],
                     stdout=open(log, "w"), stderr=subprocess.STDOUT, start_new_session=True)
    print(f"loop '{a.name}' running detached -> {log} (Telegram on completion)")


def cmd_toggle(a, enabled):
    spec = LoopSpec.load(a.name)
    spec.enabled = enabled
    spec.save()
    print(f"loop '{a.name}' -> {'enabled' if enabled else 'disabled'}")


def cmd_remove(a):
    p = LOOPS_DIR / f"{a.name}.json"
    if p.exists():
        p.unlink()
        print(f"removed loop '{a.name}'")
    else:
        print(f"no loop '{a.name}'")


def main():
    ap = argparse.ArgumentParser(prog="deploy")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add")
    p.add_argument("--name", required=True)
    p.add_argument("--mode", choices=["fix", "audit"], default="fix")
    p.add_argument("--repo", required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--target", default="all-issues")
    p.add_argument("--pr-only", action="store_true", help="never auto-merge; PR everything")
    p.add_argument("--max-coders", type=int, default=4)
    p.add_argument("--max-findings", type=int, default=8)
    p.add_argument("--schedule", default="manual", help="manual | Nh | Nm | always")
    p.add_argument("--budget-window", type=int, default=80)
    p.add_argument("--budget-week", type=int, default=80)
    p.set_defaults(func=cmd_add)

    sub.add_parser("list").set_defaults(func=cmd_list)

    p = sub.add_parser("run"); p.add_argument("name"); p.set_defaults(func=cmd_run)
    p = sub.add_parser("enable"); p.add_argument("name"); p.set_defaults(func=lambda a: cmd_toggle(a, True))
    p = sub.add_parser("disable"); p.add_argument("name"); p.set_defaults(func=lambda a: cmd_toggle(a, False))
    p = sub.add_parser("remove"); p.add_argument("name"); p.set_defaults(func=cmd_remove)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
