#!/usr/bin/env python3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import loop_spec as ls
import parallel_orchestrator as po


class FakeThread:
    def __init__(self):
        self.posts = []

    def post(self, body, level="progress"):
        self.posts.append((body, level))
        return True


def test_run_spec_filters_skipped_issues_and_logs_reasons(monkeypatch):
    captured = {}

    monkeypatch.setattr(ls, "budget_ok", lambda spec: (True, "ok"))
    monkeypatch.setattr(ls.preflight, "preflight", lambda: [])
    monkeypatch.setattr(ls.preflight, "has_blockers", lambda problems: False)
    monkeypatch.setattr(ls, "_resolve_issues", lambda spec: [1, 2, 3, 4])
    monkeypatch.setattr(ls, "_history_rows", lambda repo, issue: [{"issue": issue}])

    def fake_should_skip(rows, **kwargs):
        issue = rows[0]["issue"]
        if issue == 2:
            return True, "cooldown"
        if issue == 3:
            return True, "max_attempts"
        return False, ""

    def fake_run(repo, workdir, issues, max_coders, allow_merge, thread=None):
        captured["issues"] = issues
        return {"result": "done", "scorecard": {"merged": [1], "prd": []}}

    monkeypatch.setattr(ls.work_history, "should_skip", fake_should_skip)
    monkeypatch.setattr(ls.po, "run", fake_run)

    spec = ls.LoopSpec(name="learn", repo="owner/repo", workdir="/tmp/repo", target="1,2,3,4")
    thread = FakeThread()
    result = ls.run_spec(spec, thread=thread)

    assert captured["issues"] == [1, 4]
    assert result["result"] == "done"
    posted = "\n".join(body for body, _ in thread.posts)
    assert "owner/repo#2" in posted and "cooldown" in posted
    assert "owner/repo#3" in posted and "max_attempts" in posted


def test_run_spec_returns_no_work_when_history_skips_everything(monkeypatch):
    monkeypatch.setattr(ls, "budget_ok", lambda spec: (True, "ok"))
    monkeypatch.setattr(ls.preflight, "preflight", lambda: [])
    monkeypatch.setattr(ls.preflight, "has_blockers", lambda problems: False)
    monkeypatch.setattr(ls, "_resolve_issues", lambda spec: [8])
    monkeypatch.setattr(ls, "_history_rows", lambda repo, issue: [{"issue": issue}])
    monkeypatch.setattr(ls.work_history, "should_skip", lambda rows, **kwargs: (True, "cooldown"))

    called = {"run": False}
    monkeypatch.setattr(ls.po, "run", lambda *a, **k: called.__setitem__("run", True))

    result = ls.run_spec(ls.LoopSpec(name="empty", repo="owner/repo", workdir="/tmp/repo"),
                         thread=FakeThread())

    assert result == {"name": "empty", "result": "no_work"}
    assert called["run"] is False


def test_failure_context_is_injected_into_issue_and_revision_briefs(monkeypatch, tmp_path):
    context = "Prior attempts failed: rejected patch=abc: over-broad parser edit."
    prompts = []

    class FakeHints:
        def hint_block(self):
            return "Hints: tools/foo.py\n\n"

    monkeypatch.setattr(po, "_issue_text", lambda repo, n: ("Broken parser", "Body"))
    monkeypatch.setattr(po.lz, "localize", lambda workdir, text: FakeHints())
    monkeypatch.setattr(po, "_failure_context", lambda repo, issue: context)
    monkeypatch.setattr(po.ra, "dispatch",
                        lambda prompt, workdir, config: prompts.append(prompt) or SimpleNamespace(runtime="codex"))
    monkeypatch.setattr(po, "_stage_changes", lambda wt: ["tools/foo.py"])
    monkeypatch.setattr(po, "sh", lambda *a, **k: ("", "", 0))
    monkeypatch.setattr(po, "_capture_patch", lambda wt, base_branch: "+fixed = True")
    monkeypatch.setattr(po, "stage_sandbox", lambda cand, test_cmd: cand)
    monkeypatch.setattr(po, "stage_review", lambda cand, repo, base_branch: cand)

    _, brief = po.issue_brief("owner/repo", 9, workdir=str(tmp_path))
    assert context in brief

    cand = {"issue": 9, "worktree": str(tmp_path), "changed": [], "patch": ""}
    assert po._revise_once(cand, "owner/repo", "main", "pytest", object(), "Fix blocker")
    assert prompts and context in prompts[0]


class FakeSelection:
    def __init__(self, chosen):
        self.chosen = chosen
        self.stage = "test"
        self.diagnostics = {"n_eligible": 1}

    def to_dict(self):
        return {"chosen_issue": (self.chosen or {}).get("issue"), "stage": self.stage}


def test_run_records_work_history_once_per_issue_with_outcome_and_patch_hash(monkeypatch, tmp_path):
    records = []

    monkeypatch.setattr(po, "_start_run_log", lambda repo, ts: None)
    monkeypatch.setattr(po, "_init_sandbox_gate", lambda: None)
    monkeypatch.setattr(po, "ensure_clone", lambda repo, workdir, alert_fn=None: "main")
    monkeypatch.setattr(po, "repo_test_cmd", lambda workdir: "python3 -m pytest -q")
    monkeypatch.setattr(po, "usage_ok", lambda: True)
    monkeypatch.setattr(po.ra.RuntimeConfig, "from_pincer_toml",
                        classmethod(lambda cls: po.ra.RuntimeConfig()))
    monkeypatch.setattr(po.work_history, "record",
                        lambda **kwargs: records.append(kwargs))

    def fake_stage_code(repo, main_workdir, base, n, cfg, base_branch, sample=0, samples=1):
        common = {"issue": n, "sample": sample, "title": f"Issue {n}",
                  "worktree": str(tmp_path / f"wt-{n}"), "branch": f"fix/{n}",
                  "worker_status": "done", "runtime": "codex", "changed": ["x.py"]}
        if n == 3:
            return {**common, "committed": False, "changed": [], "patch": ""}
        if n == 5:
            return {**common, "worker_status": "error", "committed": False,
                    "changed": [], "patch": "", "error": "worktree add failed"}
        return {**common, "committed": True, "patch": f"diff --git a/x.py b/x.py\n+value = {n}"}

    def fake_stage_sandbox(cand, test_cmd):
        cand["sandbox"] = "fail" if cand["issue"] == 2 else "pass"
        if cand["issue"] == 2:
            cand["sandbox_fail"] = "FAILED test_regression"
        return cand

    def fake_finalize(cand, repo, base_branch, test_cmd, cfg, tuning):
        verdict = "reject" if cand["issue"] == 4 else "approve"
        cand["review"] = {"verdict": verdict, "blockers": ["too broad"] if verdict == "reject" else []}
        return cand

    def fake_stage_gate(cand, repo, main_workdir, allow_merge, base_branch):
        if cand["issue"] == 1:
            cand["published"] = "auto_merged"
        return cand

    monkeypatch.setattr(po, "stage_code", fake_stage_code)
    monkeypatch.setattr(po, "stage_sandbox", fake_stage_sandbox)
    monkeypatch.setattr(po.sel, "select", lambda issue_cands, **kwargs: FakeSelection(issue_cands[0]))
    monkeypatch.setattr(po, "finalize_chosen", fake_finalize)
    monkeypatch.setattr(po, "stage_gate", fake_stage_gate)

    state = po.run(
        "owner/repo",
        str(tmp_path / "repo"),
        [1, 2, 3, 4, 5],
        max_coders=1,
        allow_merge=True,
        tuning=po.SelectionTuning(),
        thread=FakeThread(),
    )

    assert state["scorecard"]["merged"] == [1]
    assert [r["issue"] for r in records] == [1, 2, 3, 4, 5]
    by_issue = {r["issue"]: r for r in records}
    assert by_issue[1]["outcome"] == "shipped"
    assert by_issue[1]["patch_hash"] == "+value=1"
    assert by_issue[2]["outcome"] == "failed"
    assert by_issue[2]["patch_hash"] == "+value=2"
    assert by_issue[3]["outcome"] == "no_winner"
    assert by_issue[3]["patch_hash"] == ""
    assert by_issue[4]["outcome"] == "rejected"
    assert by_issue[4]["patch_hash"] == "+value=4"
    assert by_issue[5]["outcome"] == "error"
