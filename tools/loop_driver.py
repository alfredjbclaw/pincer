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
import argparse, datetime, logging, os, sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))
import inflight
import parallel_orchestrator as po
import run_ledger
from loop_spec import LoopSpec, run_spec
from notify import send_alert

LOG = logging.getLogger("pincer")
DEFAULT_RUN_TIMEOUT_S = 1800


def _utc_ts() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _loop_log_path() -> Path:
    return Path(
        os.environ.get(
            "PINCER_LOOP_LOG",
            Path.home() / ".openclaw" / "pincer" / "logs" / "loop-driver.log",
        )
    )


def _configure_logging() -> None:
    path = _loop_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False

    for handler in list(LOG.handlers):
        if getattr(handler, "_pincer_loop_driver", False):
            if getattr(handler, "baseFilename", None) == str(path):
                return
            LOG.removeHandler(handler)
            handler.close()

    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5)
    handler._pincer_loop_driver = True
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    ))
    LOG.addHandler(handler)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        LOG.exception("Invalid integer for %s=%r; using default %s", name, raw, default)
        return default


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
        LOG.exception("Invalid last_run for loop %s: %r", spec.name, spec.last_run)
        return True
    return (now - last).total_seconds() >= iv


def main():
    _configure_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    now = datetime.datetime.now()
    due = [s for s in LoopSpec.all() if is_due(s, now)]
    LOG.info(
        "Loop driver tick start dry_run=%s due=%s loops=%s",
        a.dry_run,
        len(due),
        [s.name for s in due],
    )

    # Single-runner lock: a tick can run long pipelines; never let the next
    # cron tick overlap it.
    lock = Path(os.environ.get("PINCER_LOOP_LOCK", "/tmp/pincer-loop-driver.lock"))
    if not a.dry_run and lock.exists():
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)  # raises if not alive
            LOG.info("Loop driver already running; skipping tick pid=%s lock=%s", pid, lock)
            print("loop driver already running; skipping tick")
            return
        except (ValueError, ProcessLookupError, PermissionError):
            LOG.exception("Ignoring stale loop driver lock at %s", lock)

    if a.dry_run:
        print(f"{len(due)} loop(s) due:", [s.name for s in due])
        for s in LoopSpec.all():
            print(f"  {s.name}: enabled={s.enabled} schedule={s.schedule} last_run={s.last_run} "
                  f"due={is_due(s, now)}")
        return

    if not due:
        LOG.info("Loop driver tick quiet; no due loops")
        return  # quiet tick — nothing to do, nothing to say

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
        stale_max_age_s = _env_int("PINCER_INFLIGHT_MAX_AGE_S", inflight.DEFAULT_MAX_AGE_S)
        timeout_s = _env_int("PINCER_RUN_TIMEOUT_S", DEFAULT_RUN_TIMEOUT_S)
        try:
            reaped = inflight.reap_stale(_utc_ts(), max_age_s=stale_max_age_s)
            if reaped:
                LOG.info("Reaped stale inflight claims at tick start: %s", reaped)
        except Exception:
            LOG.exception("Failed to reap stale inflight claims at tick start")

        # Tick framing is detail (the per-loop START/DONE milestones carry the
        # signal); a crash is critical.
        post(f"⏰ Loop driver: {len(due)} loop(s) due — {', '.join(s.name for s in due)}")
        results = []
        for spec in due:
            key = spec.repo or spec.name
            run_id = f"{spec.name}:{now.isoformat()}"
            claimed = inflight.claim(key, run_id, os.getpid(), _utc_ts())
            if not claimed:
                detail = f"already in flight for key {key}"
                LOG.info("Skipping loop %s: %s", spec.name, detail)
                post(f"⏭️ Loop '{spec.name}' skipped: {detail}.")
                continue

            try:
                inflight.heartbeat(key, _utc_ts())

                def _run():
                    return run_spec(spec, thread=thread)

                def _on_timeout():
                    LOG.error("Loop %s timed out after %ss; reaping stale claims",
                              spec.name, timeout_s)
                    try:
                        inflight.reap_stale(_utc_ts(), max_age_s=0)
                    except Exception:
                        LOG.exception("Failed best-effort inflight reap after timeout")

                r = inflight.run_with_timeout(_run, timeout_s, _on_timeout)
                LOG.info("Loop %s result: %s", spec.name, r)
            except inflight.RunTimeout:
                detail = f"timed out after {timeout_s}s"
                r = {"name": spec.name, "result": "timeout", "detail": detail}
                LOG.error("Loop %s timed out after %ss", spec.name, timeout_s)
                post(f"💥 Loop '{spec.name}' timed out after {timeout_s}s.", "critical")
            except Exception as e:
                LOG.exception("Loop %s crashed", spec.name)
                r = {"name": spec.name, "result": "exception", "detail": str(e)}
                post(f"💥 Loop '{spec.name}' crashed: {e}", "critical")
            finally:
                try:
                    inflight.release(key)
                except Exception:
                    LOG.exception("Failed to release inflight claim for %s", key)
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
