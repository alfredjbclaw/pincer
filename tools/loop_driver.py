#!/usr/bin/env python3
"""loop_driver — the autonomy layer. Wakes (via cron), finds which deployed
loops are due, runs them through the proven pipeline, talks to Jacob only on
the signals that matter (start, done, held-on-budget, crash).

This is what turns "works when I crank it" into "runs itself." Invoked
periodically by a cron/LaunchAgent; each tick services the due loops
sequentially (so codex concurrency + token budget stay respected).

  python3 tools/loop_driver.py            # one tick: run all due loops
  python3 tools/loop_driver.py --dry-run  # show what's due, run nothing
"""
from __future__ import annotations
import argparse, datetime, sys
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))
import parallel_orchestrator as po
import run_ledger
from loop_spec import LoopSpec, run_spec
from notify import send_alert


def _interval_seconds(schedule: str) -> int | None:
    s = schedule.strip().lower()
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("d"):
        return int(s[:-1]) * 86400
    return None


def is_due(spec: LoopSpec, now: datetime.datetime) -> bool:
    if not spec.enabled or spec.schedule == "manual":
        return False
    if spec.schedule == "always":
        return True
    iv = _interval_seconds(spec.schedule)
    if iv is None:
        return False
    if not spec.last_run:
        return True
    try:
        last = datetime.datetime.fromisoformat(spec.last_run)
    except Exception:
        return True
    return (now - last).total_seconds() >= iv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    now = datetime.datetime.now()
    due = [s for s in LoopSpec.all() if is_due(s, now)]

    # Single-runner lock: a tick can run long pipelines; never let the next
    # cron tick overlap it.
    lock = Path("/tmp/pincer-loop-driver.lock")
    if not a.dry_run and lock.exists():
        try:
            pid = int(lock.read_text().strip())
            import os
            os.kill(pid, 0)  # raises if not alive
            print("loop driver already running; skipping tick")
            return
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale lock

    if a.dry_run:
        print(f"{len(due)} loop(s) due:", [s.name for s in due])
        for s in LoopSpec.all():
            print(f"  {s.name}: enabled={s.enabled} schedule={s.schedule} last_run={s.last_run} "
                  f"due={is_due(s, now)}")
        return

    if not due:
        return  # quiet tick — nothing to do, nothing to say

    import os
    lock.write_text(str(os.getpid()))
    # One alert root for the whole tick (routed to the muteable Pincer topic at
    # the configured verbosity): every loop's messages — and the orchestrator's
    # stage alerts inside them — reply to this root, so the entire run is a
    # single Telegram reply-chain instead of a scatter of standalone messages.
    thread = po.make_alert_thread("⏰ pincer")

    def post(msg, level="progress"):
        if thread is not None:
            thread.post(msg, level=level)
        else:
            send_alert(msg)

    try:
        # Tick framing is detail (the per-loop START/DONE milestones carry the
        # signal); a crash is critical.
        post(f"⏰ Loop driver: {len(due)} loop(s) due — {', '.join(s.name for s in due)}")
        results = []
        for spec in due:
            try:
                r = run_spec(spec, thread=thread)
            except Exception as e:
                import traceback
                traceback.print_exc()
                r = {"name": spec.name, "result": "exception", "detail": str(e)}
                post(f"💥 Loop '{spec.name}' crashed: {e}", "critical")
            spec.last_run = now.isoformat()
            spec.save()
            # Record the outcome, then auto-pause a loop that keeps failing for
            # infrastructure reasons (broken env) — stop burning credits + page.
            run_ledger.record(spec.name, spec.repo, r.get("result", "?"),
                              r.get("scorecard", {}), now.isoformat())
            rows = run_ledger.read(spec.name)
            if spec.enabled and run_ledger.should_pause(rows):
                spec.enabled = False
                spec.save()
                streak = run_ledger.consecutive_infra_failures(rows)
                post(f"⏸️🚨 Auto-PAUSED loop '{spec.name}' after {streak} consecutive "
                     f"infrastructure failures (broken env, not 'no fix found'). "
                     f"Re-enable once fixed.", "critical")
            results.append(r)
        merged = sum(len(r.get("scorecard", {}).get("merged", []) or []) for r in results)
        post(f"⏰ Loop driver tick done — {len(results)} loop(s) run, {merged} change(s) merged. "
             + " | ".join(f"{r['name']}:{r.get('result')}" for r in results))
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
