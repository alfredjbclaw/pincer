#!/usr/bin/env python3
"""Self-healing-workdir tests with REAL git (no network): a local bare repo acts
as the remote. Covers the failure that sank the sql-metadata loop runs — a clone
whose default branch no longer resolves -> 'invalid reference: master'."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import parallel_orchestrator as po


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _make_remote(tmp_path, branch="master"):
    """A bare repo with one commit on `branch`, usable as a clone URL."""
    work = tmp_path / "seed"
    work.mkdir()
    _git("init", "-q", "-b", branch, cwd=work)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "app.py").write_text("def f():\n    return 1\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "init", cwd=work)
    bare = tmp_path / "remote.git"
    _git("clone", "-q", "--bare", str(work), str(bare))
    return str(bare)


def test_resolves_and_detect_default(tmp_path):
    remote = _make_remote(tmp_path, branch="master")
    wd = tmp_path / "clone"
    _git("clone", "-q", str(remote), str(wd))
    assert po._resolves(wd, "origin/master")
    assert not po._resolves(wd, "nope")
    assert po._detect_default_branch(wd) == "master"


def test_ensure_clone_fresh_when_missing(tmp_path):
    remote = _make_remote(tmp_path, branch="master")
    wd = tmp_path / "fresh"  # does not exist yet
    branch = po.ensure_clone("o/r", wd, clone_url=remote)
    assert branch == "master"
    assert (wd / ".git").exists()
    assert po._resolves(wd, "master")  # LOCAL branch exists and resolves


def test_ensure_clone_reclones_when_broken(tmp_path):
    remote = _make_remote(tmp_path, branch="master")
    wd = tmp_path / "broken"
    wd.mkdir()
    (wd / "stale.txt").write_text("not a git repo")  # exists but no .git
    branch = po.ensure_clone("o/r", wd, clone_url=remote)
    assert branch == "master"
    assert (wd / ".git").exists()
    assert (wd / "app.py").exists()  # real content from the remote


def test_ensure_clone_heals_repo_with_no_resolvable_default(tmp_path):
    # A .git dir with no resolvable default branch (empty init, no remote) is the
    # broken state — _detect returns None and ensure_clone must re-clone, not
    # crash. This is the shape of the corrupted /tmp clone that failed the loop.
    remote = _make_remote(tmp_path, branch="master")
    wd = tmp_path / "empty"
    wd.mkdir()
    _git("init", "-q", cwd=wd)  # has .git, but nothing resolves
    assert po._detect_default_branch(wd) is None  # genuinely broken
    branch = po.ensure_clone("o/r", wd, clone_url=remote)
    assert branch == "master"
    assert po._resolves(wd, "origin/master")
    assert (wd / "app.py").exists()


def test_make_worktree_succeeds_after_ensure(tmp_path):
    remote = _make_remote(tmp_path, branch="master")
    wd = tmp_path / "clone"
    branch = po.ensure_clone("o/r", wd, clone_url=remote)
    base = tmp_path / "pincer-worktrees"
    base.mkdir()
    worktree, br = po.make_worktree(wd, base, "5", branch)
    assert Path(worktree).exists()
    assert br == "fix/issue-5"
    assert (Path(worktree) / "app.py").exists()


def test_make_worktree_falls_back_to_origin_ref(tmp_path):
    # If only the remote-tracking ref exists (no local branch), make_worktree
    # must resolve via origin/<branch> instead of dying on 'invalid reference'.
    remote = _make_remote(tmp_path, branch="main")
    wd = tmp_path / "clone"
    _git("clone", "-q", str(remote), str(wd))
    _git("checkout", "-q", "--detach", cwd=wd)
    _git("branch", "-q", "-D", "main", cwd=wd)  # drop local branch; keep origin/main
    base = tmp_path / "pincer-worktrees"
    base.mkdir()
    worktree, br = po.make_worktree(wd, base, "7", "main")
    assert Path(worktree).exists()
