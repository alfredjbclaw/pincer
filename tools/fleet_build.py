#!/usr/bin/env python3
"""fleet_build — greenfield multi-agent construction.

Opus plans the decomposition (a shared interface contract); a fleet of codex
subagents build the modules in parallel against that contract; we integrate,
run the real test suite in a clean VM (env we control — no bespoke-CI friction),
and run a bounded fix-loop on any failures. The "recreate from scratch with a
fleet" mode — sidesteps real-repo env quirks and tests construction, not just
bug-fixing.

Models: codex (gpt-5.5) for ALL coding; Opus orchestrates + checks (this driver
+ the fix-loop reviewer). ulw is off (the VM is the verifier, not the worker).
"""
from __future__ import annotations
import sys, json, subprocess, dataclasses
import concurrent.futures as cf
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))
import runtime_adapter as ra
from notify import send_alert, AlertThread

_THREAD = None

# Plan-driven: pass --plan <json> to build any project; defaults to cfgcheck.
# Plan schema: {repo_dir, state_path, test_cmd, modules: [[name, files, task], ...]}
import argparse as _argparse
_p = _argparse.ArgumentParser()
_p.add_argument("--plan")
_args, _ = _p.parse_known_args()

if _args.plan:
    _plan = json.loads(Path(_args.plan).read_text())
    REPO = _plan["repo_dir"]
    STATE = Path(_plan["state_path"])
    GOTEST = _plan["test_cmd"]
    MODULES = [tuple(m) for m in _plan["modules"]]
else:
    REPO = "/tmp/cfgcheck"
    STATE = Path("/tmp/fleet-build-state.json")
    GO = "go1.22.5"
    GOTEST = (f"sudo apt-get update -qq && sudo apt-get install -y -qq curl ca-certificates && "
              f"curl -sSL https://go.dev/dl/{GO}.linux-arm64.tar.gz | sudo tar -C /usr/local -xz && "
              f"export PATH=$PATH:/usr/local/go/bin && go mod tidy && go test ./...")
    MODULES = [
        ("validate-json", "pkg/validate/json.go + pkg/validate/json_test.go",
         "Implement ValidateJSON using encoding/json."),
        ("validate-yaml", "pkg/validate/yaml.go + pkg/validate/yaml_test.go",
         "Implement ValidateYAML using gopkg.in/yaml.v3 (unmarshal into a generic value)."),
        ("validate-toml", "pkg/validate/toml.go + pkg/validate/toml_test.go",
         "Implement ValidateTOML using github.com/pelletier/go-toml/v2."),
        ("validate-registry", "pkg/validate/registry.go + pkg/validate/registry_test.go",
         "Implement ValidatorFor mapping .json/.yaml/.yml/.toml to the validators in THIS package."),
        ("finder", "pkg/finder/finder.go + pkg/finder/finder_test.go",
         "Implement Find: walk root with filepath.WalkDir, return sorted paths whose extension is in exts."),
        ("report", "pkg/report/report.go + pkg/report/report_test.go",
         "Implement the Result struct and Summary."),
        ("cli", "cmd/cfgcheck/main.go",
         "Implement package main wiring finder+validate+report per the contract."),
    ]

CONTRACT = (Path(REPO) / "CONTRACT.md").read_text()
CFG = dataclasses.replace(ra.RuntimeConfig.from_pincer_toml(), ultrawork=False)

def sh(cmd, timeout=1800):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.stdout, p.stderr, p.returncode

def alert(m):
    try:
        if _THREAD is not None:
            _THREAD.post(m)
        else:
            send_alert(m)
    except Exception as e:
        print("alert fail", e)

state = {"phase": "start", "modules": {}, "rounds": []}
def save(): STATE.write_text(json.dumps(state, indent=2, default=str))

def build_module(mod):
    name, files, task = mod
    brief = (f"You are building one part of a Go project. SHARED CONTRACT all parts must match:\n\n"
             f"{CONTRACT}\n\nYOUR TASK: create exactly these file(s): {files}\n{task}\n\n"
             "Match the contract signatures EXACTLY. Use only stdlib + the deps already in go.mod. "
             "Do NOT modify go.mod/go.sum, do NOT run `go mod tidy`, do NOT create any other files. "
             "Write idiomatic Go with real table-driven tests covering valid AND invalid inputs.")
    res = ra.dispatch(brief, workdir=REPO, config=CFG)
    return {"name": name, "files": files, "status": res.status,
            "runtime": res.runtime, "fallback": res.fallback_used}

def vm_test():
    out, err, rc = sh(["python3", str(THIS / "sandbox_gate.py"),
                       "--workdir", REPO, "--test", GOTEST, "--json"], 1800)
    try:
        j = json.loads(out)
        tail = (j.get("stdout_tail", "") + "\n" + j.get("stderr_tail", ""))
        keep = [l for l in tail.splitlines() if any(m in l for m in
                ("FAIL", "ok ", "Error", "error", "cannot", "undefined", "expected",
                 ".go:", "no test files", "PASS")) and "debconf" not in l and "crabbox" not in l]
        return j.get("verdict", "error"), "\n".join(keep[-40:])[:2500]
    except Exception:
        return "error", (err or out)[-800:]

def fix_round(failures, rnd):
    brief = (f"You are fixing a Go project that fails its tests. SHARED CONTRACT:\n\n{CONTRACT}\n\n"
             f"The full repo is in your working directory. `go test ./...` FAILS with:\n\n{failures}\n\n"
             "Fix the compile/test errors across whatever files are responsible so the WHOLE suite passes. "
             "Keep the contract signatures intact. Do NOT modify go.mod/go.sum or run go mod tidy.")
    res = ra.dispatch(brief, workdir=REPO, config=CFG)
    return res.runtime

_PROJECT = Path(REPO).name
try:
    _THREAD = AlertThread(f"🏗️ {_PROJECT}") if AlertThread else None
    alert(f"🏗️ FLEET BUILD START — building `{_PROJECT}` with {len(MODULES)} "
          "parallel codex builders against an Opus contract. We own the test env (no git/CI quirks).")
    state["phase"] = "build"; save()

    # Phase 1: parallel codex builders
    with cf.ThreadPoolExecutor(max_workers=7) as ex:
        for r in ex.map(build_module, MODULES):
            state["modules"][r["name"]] = r
            save()
            alert(f"  ✓ built {r['name']} ({r['runtime']}, status={r['status']})")
    codex_n = sum(1 for m in state["modules"].values() if m["runtime"] == "codex")
    files_n = len([p for p in Path(REPO).rglob("*.go")])
    alert(f"⌨️ Build done — {len(MODULES)} modules, {codex_n} on codex, {files_n} .go files written. Integrating + testing in VM.")

    # Phase 2: integrate + test, with a bounded fix-loop
    state["phase"] = "verify"; save()
    verdict, detail = vm_test()
    state["rounds"].append({"round": 0, "verdict": verdict}); save()
    rnd = 0
    while verdict != "pass" and rnd < 3:
        rnd += 1
        alert(f"🔧 Tests {verdict} (round {rnd}) — fixing:\n{detail[:600]}")
        fix_round(detail, rnd)
        verdict, detail = vm_test()
        state["rounds"].append({"round": rnd, "verdict": verdict}); save()

    state["phase"] = "done"; state["final_verdict"] = verdict; save()
    if verdict == "pass":
        alert(f"🎉 FLEET BUILD PASS — `cfgcheck` built from scratch by {codex_n} parallel codex agents, "
              f"GREEN `go test ./...` after {rnd} fix round(s). Multi-agent greenfield construction proven.")
    else:
        alert(f"🏁 FLEET BUILD ended {verdict} after {rnd} fix rounds. Last failures:\n{detail[:700]}")
except Exception as e:
    import traceback; traceback.print_exc()
    state["phase"] = "exception"; state["error"] = str(e); save()
    alert(f"💥 Fleet build crashed: {e}")
