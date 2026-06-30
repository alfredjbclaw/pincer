#!/usr/bin/env python3
"""Edit-in-place LiveBoard + local run-logging.

LiveBoard is a drop-in for AlertThread (same post(body, level=...) interface)
that edits ONE message in place instead of sending many — so a run buzzes the
phone at most once, and criticals still break through with a discrete ping.

Backend-specific tests importorskip `telegram_alert` so the STANDALONE pincer
suite stays green without it; pincer's own logic (make_alert_thread, run-log,
the no-op board) always runs."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import notify
import parallel_orchestrator as po


# --- pincer's own logic (no backend needed) --------------------------------

def test_noop_board_respects_level_floor(capsys):
    # Only meaningful when notify resolves to the print-only no-op board; if a
    # real backend is importable, notify.LiveBoard would actually SEND, so skip.
    try:
        import telegram_alert  # noqa: F401
        pytest.skip("real backend present — no-op board inactive")
    except ImportError:
        pass
    b = notify.LiveBoard(min_level="milestone")
    b.post("chatter", "progress")   # below floor -> not shown
    b.post("started", "milestone")  # shown
    out = capsys.readouterr().out
    assert "started" in out
    assert "chatter" not in out


def test_make_alert_thread_defaults_to_live_board(monkeypatch, tmp_path):
    monkeypatch.setenv("PINCER_CONFIG", str(tmp_path / "absent.toml"))
    surface = po.make_alert_thread("x")
    assert surface.__class__.__name__ == "LiveBoard"
    assert surface.topic_id == po.PINCER_ALERTS_TOPIC


def test_make_alert_thread_thread_style_opt_out(monkeypatch, tmp_path):
    cfg = tmp_path / "pincer.toml"
    cfg.write_text('[alerts]\nstyle = "thread"\n')
    monkeypatch.setenv("PINCER_CONFIG", str(cfg))
    assert po.make_alert_thread("x").__class__.__name__ == "AlertThread"


def test_run_log_writes_and_prunes(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".openclaw" / "pincer" / "run-logs"
    d.mkdir(parents=True)
    for i in range(55):  # 55 stale logs; prune keeps 50
        (d / f"old{i:02d}.md").write_text("x")

    po._start_run_log("owner/repo", "20260629-120000")
    po._run_log("milestone line", "milestone")
    po._run_log("progress detail", "progress")

    assert po._RUN_LOG is not None and po._RUN_LOG.exists()
    text = po._RUN_LOG.read_text()
    assert "milestone line" in text and "progress detail" in text  # full detail on disk
    assert len(list(d.glob("*.md"))) <= 51  # 50 kept + the new one


# --- real backend behaviour (skips cleanly when telegram_alert absent) -----

def test_liveboard_first_post_sends_rest_edit(monkeypatch):
    ta = pytest.importorskip("telegram_alert")
    sends, edits = [], []
    monkeypatch.setattr(ta, "send_alert", lambda body, **kw: (sends.append((body, kw)), (True, "ok"))[1])
    monkeypatch.setattr(ta, "edit_message", lambda mid, body, **kw: (edits.append((mid, body)), True)[1])
    monkeypatch.setattr(ta, "last_message_id", lambda: 555)

    b = ta.LiveBoard("🔧 demo", topic_id=1101, min_level="milestone", silent=True)
    b.post("started", "milestone")     # first -> send
    b.post("stage done", "milestone")  # -> edit in place

    assert len(sends) == 1                       # only the first is a fresh message
    assert sends[0][1].get("silent") is True     # silent so it never buzzes
    assert sends[0][1].get("topic_id") == 1101
    assert len(edits) == 1 and edits[0][0] == 555
    assert "started" in edits[0][1] and "stage done" in edits[0][1]  # board grows


def test_liveboard_critical_buzzes_separately(monkeypatch):
    ta = pytest.importorskip("telegram_alert")
    sends = []
    monkeypatch.setattr(ta, "send_alert", lambda body, **kw: (sends.append(kw.get("silent")), (True, "ok"))[1])
    monkeypatch.setattr(ta, "edit_message", lambda *a, **k: True)
    monkeypatch.setattr(ta, "last_message_id", lambda: 9)

    b = ta.LiveBoard(min_level="milestone")
    b.post("started", "milestone")  # silent board send
    b.post("🚨 boom", "critical")    # edit + a discrete NON-silent ping
    assert True in sends and False in sends  # one silent (board), one notifying (critical)
