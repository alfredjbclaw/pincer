#!/usr/bin/env python3
"""Verbosity floor + topic routing for pincer alerts: quiet drops progress
chatter, the dedicated topic is applied, and config drives the level."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import parallel_orchestrator as po

# The first two tests exercise the real external alert backend's AlertThread
# (verbosity floor + topic routing) and skip cleanly when it isn't installed
# (the standalone public case). The make_alert_thread tests below cover pincer's
# OWN config logic via the no-op shim and always run.


def test_quiet_thread_suppresses_progress_keeps_milestone(monkeypatch):
    ta = pytest.importorskip("telegram_alert")
    sent = []
    monkeypatch.setattr(ta, "send_alert", lambda body, **kw: (sent.append(kw), (True, "ok"))[1])
    th = ta.AlertThread("tag", topic_id=99, min_level="milestone")

    th.post("stage chatter", level="progress")
    assert sent == []  # below the floor — dropped

    th.post("🧵 START", level="milestone")
    th.post("🚨 boom", level="critical")
    assert len(sent) == 2
    assert all(kw.get("topic_id") == 99 for kw in sent)  # routed to the dedicated topic


def test_verbose_thread_sends_everything(monkeypatch):
    ta = pytest.importorskip("telegram_alert")
    sent = []
    monkeypatch.setattr(ta, "send_alert", lambda body, **kw: (sent.append(body), (True, "ok"))[1])
    th = ta.AlertThread("t", min_level="progress")
    th.post("a")  # default progress
    th.post("b", level="milestone")
    assert sent == ["a", "b"]


def test_make_alert_thread_quiet_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("PINCER_CONFIG", str(tmp_path / "absent.toml"))
    th = po.make_alert_thread("x")
    assert th.min_level == "milestone"          # quiet
    assert th.topic_id == po.PINCER_ALERTS_TOPIC  # dedicated Pincer topic


def test_make_alert_thread_verbose_from_config(monkeypatch, tmp_path):
    cfg = tmp_path / "pincer.toml"
    cfg.write_text('[alerts]\nverbosity = "verbose"\ntopic_id = 222\n')
    monkeypatch.setenv("PINCER_CONFIG", str(cfg))
    th = po.make_alert_thread("x")
    assert th.min_level == "progress"  # everything
    assert th.topic_id == 222
