#!/usr/bin/env python3
"""The alert-threading contract: one root per process, every layer reuses it.

Verifies the wiring (driver -> spec -> orchestrator) passes a single AlertThread
through, so all of a run's messages reply to one start message instead of each
being its own. The AlertThread mechanism itself is exercised live elsewhere."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import loop_spec as ls
import loop_driver as ld


class FakeThread:
    def __init__(self, tag=None):
        self.tag = tag
        self.posts = []

    def post(self, body, **kw):
        self.posts.append(body)
        return True


def test_run_spec_uses_thread_and_passes_it_down(monkeypatch):
    captured = {}

    def fake_run(repo, workdir, issues, max_coders, allow_merge, thread=None):
        captured["thread"] = thread
        return {"result": "done", "scorecard": {}}

    monkeypatch.setattr(ls.po, "run", fake_run)
    monkeypatch.setattr(ls, "budget_ok", lambda s: (True, "within budget"))
    monkeypatch.setattr(ls, "_resolve_issues", lambda s: [1])

    spec = ls.LoopSpec(name="demo", mode="fix", repo="o/r", workdir="/tmp/x",
                       target="1", max_coders=2)
    ft = FakeThread()
    ls.run_spec(spec, thread=ft)

    # The orchestrator received the SAME thread (one root, no new one).
    assert captured["thread"] is ft
    # The loop's START message went through the thread, not a bare send.
    assert any("START" in p for p in ft.posts)


def test_run_spec_starts_own_thread_when_standalone(monkeypatch):
    captured = {}

    def fake_run(*a, thread=None, **k):
        captured["thread"] = thread
        return {"result": "done", "scorecard": {}}

    monkeypatch.setattr(ls.po, "run", fake_run)
    monkeypatch.setattr(ls, "budget_ok", lambda s: (True, "ok"))
    monkeypatch.setattr(ls, "_resolve_issues", lambda s: [1])
    monkeypatch.setattr(ls.po, "make_alert_thread", lambda tag: FakeThread(tag))

    spec = ls.LoopSpec(name="solo", mode="fix", repo="o/r", workdir="/tmp/x",
                       target="1", max_coders=2)
    ls.run_spec(spec)  # no thread passed
    assert isinstance(captured["thread"], FakeThread)  # created its own root


class FakeSpec:
    def __init__(self, name):
        self.name = name
        self.repo = f"owner/{name}"
        self.enabled = True
        self.schedule = "6h"
        self.last_run = None

    def save(self):
        pass


def test_loop_driver_threads_all_loops_under_one_root(monkeypatch):
    seen_threads = []

    def fake_run_spec(spec, thread=None):
        seen_threads.append(thread)
        return {"name": spec.name, "result": "done", "scorecard": {}}

    specs = [FakeSpec("a"), FakeSpec("b")]
    monkeypatch.setattr(ld, "run_spec", fake_run_spec)
    monkeypatch.setattr(ld.po, "make_alert_thread", lambda tag: FakeThread(tag))
    monkeypatch.setattr(ld.LoopSpec, "all", staticmethod(lambda: specs))
    monkeypatch.setattr(ld, "is_due", lambda s, now: True)
    monkeypatch.setattr(sys, "argv", ["loop_driver"])
    # Isolate the run-ledger/auto-pause hook from this threading test.
    monkeypatch.setattr(ld.run_ledger, "record", lambda *a, **k: None)
    monkeypatch.setattr(ld.run_ledger, "read", lambda *a, **k: [])
    monkeypatch.setattr(ld.run_ledger, "should_pause", lambda *a, **k: False)

    ld.main()

    # Both loops ran with the SAME thread object => one Telegram root.
    assert len(seen_threads) == 2
    assert seen_threads[0] is seen_threads[1]
    assert isinstance(seen_threads[0], FakeThread)
