import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import pincer_diagnostics as diagnostics


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_report_surfaces_synthetic_log_missing_artifacts_and_disabled_driver(tmp_path):
    state = tmp_path / "pincer"
    write_json(state / "loops" / "demo.json", {
        "name": "demo", "enabled": True, "schedule": "6h", "last_run": "2026-07-01T00:00:00",
    })
    log = state / "logs" / "loop-driver.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "Loop driver tick start dry_run=False due=2 loops=['a', 'b']\n"
        "Loop a result: {'result': 'done'}\nLoop b result: {'result': 'done'}\n"
    )
    write_json(state / "inflight.json", {})
    write_json(state / "global-gate.json", {})

    result = diagnostics.collect(
        state,
        now=log.stat().st_mtime + 10,
        driver_probe=lambda: {"available": True, "loaded": False},
    )

    codes = [warning["code"] for warning in result["warnings"]]
    assert result["driver"]["synthetic_only"] is True
    assert "driver_log_synthetic_only" in codes
    assert "enabled_loops_driver_unloaded" in codes
    assert codes.count("missing_artifact") == 4
    assert result["inflight_entries"] == 0


def test_configured_short_loop_names_are_not_inferred_to_be_synthetic(tmp_path):
    state = tmp_path / "pincer"
    for name in ("a", "b"):
        write_json(state / "loops" / f"{name}.json", {
            "name": name, "enabled": False, "schedule": "6h",
        })
    log = state / "logs" / "loop-driver.log"
    log.parent.mkdir(parents=True)
    log.write_text("Loop a result: {'result': 'done'}\nLoop b result: {'result': 'done'}\n")
    write_json(state / "inflight.json", {})
    write_json(state / "global-gate.json", {})

    result = diagnostics.collect(
        state,
        driver_probe=lambda: {"available": True, "loaded": False},
    )

    assert result["driver"]["synthetic_only"] is False
    assert "driver_log_synthetic_only" not in {
        warning["code"] for warning in result["warnings"]
    }


def test_driver_log_tail_never_reads_past_its_fixed_byte_window(tmp_path, monkeypatch):
    log = tmp_path / "loop-driver.log"
    log.write_bytes(b"x" * 10_000)
    monkeypatch.setattr(diagnostics, "MAX_LOG_BYTES", 128)

    result = diagnostics._driver_log(log)

    assert result["bytes_scanned"] == 0
    assert result["window_truncated"] is True
    assert result["ticks"] == 0
    assert result["loop_results"] == 0


def test_report_classifies_ledgers_and_dispatch_usage_without_raw_text(tmp_path):
    state = tmp_path / "pincer"
    write_jsonl(state / "run-ledger.jsonl", [
        {"name": "loop", "result": "done", "merged": [1], "prd": [], "infra_failures": []},
        {"name": "loop", "result": "done", "merged": [], "prd": [], "infra_failures": [2]},
    ])
    write_jsonl(state / "work-history.jsonl", [
        {"repo": "owner/repo", "issue": 2, "outcome": "failed", "reason": "private text"},
        {"repo": "owner/repo", "issue": 2, "outcome": "escalated", "reason": "private text"},
    ])
    write_jsonl(state / "mem-samples.jsonl", [
        {"event": "vm_end", "available_gb": 30},
        {"event": "vm_end", "available_gb": 26},
        {"event": "vm_end", "available_gb": 22},
    ])
    write_jsonl(state / "dispatch-telemetry.jsonl", [
        {"kind": "attempt", "usage_known": True, "input_tokens": 100,
         "cached_input_tokens": 75, "output_tokens": 10, "reasoning_output_tokens": 5,
         "failure_class": "none"},
        {"kind": "outcome", "status": "done", "resumed": True,
         "fallback_used": False, "empty_diff": False},
    ])
    write_json(state / "inflight.json", {"one": {"private": "ignored"}})
    write_json(state / "global-gate.json", {})

    result = diagnostics.collect(
        state,
        driver_probe=lambda: {"available": True, "loaded": True},
    )
    encoded = json.dumps(result)

    assert result["run_ledger"]["classes"] == {"infra": 1, "shipped": 1}
    assert result["work_history"]["active_escalations"] == 1
    assert result["memory"]["leak_signal"] is True
    assert result["dispatch"]["cache_read_ratio"] == 0.75
    assert result["dispatch"]["resumed_outcomes"] == 1
    assert result["inflight_entries"] == 1
    assert "private text" not in encoded
    assert "ignored" not in encoded


def test_internal_grouping_keeps_distinct_unsafe_identifiers_separate(tmp_path):
    state = tmp_path / "pincer"
    private_loop_a = "private loop alpha"
    private_loop_b = "private loop beta"
    private_repo_a = "private repo alpha"
    private_repo_b = "private repo beta"
    write_jsonl(state / "run-ledger.jsonl", [
        {"name": private_loop_a, "result": "done", "merged": [],
         "prd": [], "infra_failures": [1]},
        {"name": private_loop_b, "result": "done", "merged": [2],
         "prd": [], "infra_failures": []},
    ])
    write_jsonl(state / "work-history.jsonl", [
        {"repo": private_repo_a, "issue": 1, "outcome": "escalated"},
        {"repo": private_repo_b, "issue": 1, "outcome": "cleared"},
    ])
    write_json(state / "inflight.json", {})
    write_json(state / "global-gate.json", {})

    result = diagnostics.collect(
        state,
        driver_probe=lambda: {"available": True, "loaded": False},
    )
    encoded = json.dumps(result)

    assert result["run_ledger"]["infra_streak_groups"] == 1
    assert result["run_ledger"]["max_infra_streak"] == 1
    assert result["work_history"]["active_escalations"] == 1
    assert private_loop_a not in encoded
    assert private_loop_b not in encoded
    assert private_repo_a not in encoded
    assert private_repo_b not in encoded


def test_work_history_groups_numeric_and_string_issue_ids_together(tmp_path):
    state = tmp_path / "pincer"
    write_jsonl(state / "work-history.jsonl", [
        {"repo": "owner/repo", "issue": 1, "outcome": "escalated"},
        {"repo": "owner/repo", "issue": "1", "outcome": "cleared"},
    ])
    write_json(state / "inflight.json", {})
    write_json(state / "global-gate.json", {})

    result = diagnostics.collect(
        state,
        driver_probe=lambda: {"available": True, "loaded": False},
    )

    assert result["work_history"]["active_escalations"] == 0


def test_report_never_emits_arbitrary_persisted_labels(tmp_path):
    state = tmp_path / "pincer"
    secret_like = "gh" + "p_" + ("a" * 36)
    write_json(state / "loops" / "private.json", {
        "name": secret_like,
        "enabled": False,
        "schedule": secret_like,
        "last_run": secret_like,
    })
    log = state / "logs" / "loop-driver.log"
    log.parent.mkdir(parents=True)
    log.write_text(f"Loop {secret_like} result: done\n")
    write_jsonl(state / "work-history.jsonl", [
        {"repo": "owner/private-repo", "issue": 1, "outcome": secret_like},
    ])
    write_json(state / "private-state.json", {
        "phase": secret_like,
        "final_verdict": secret_like,
        "committed": [],
    })
    write_json(state / "inflight.json", {})
    write_json(state / "global-gate.json", {})

    result = diagnostics.collect(
        state,
        driver_probe=lambda: {"available": True, "loaded": False},
    )
    encoded = json.dumps(result)

    assert secret_like not in encoded
    assert "owner/private-repo" not in encoded
    assert result["work_history"]["outcomes"] == {"unknown": 1}
    assert result["fleet_states"]["phases"] == {"unknown": 1}
    assert result["fleet_states"]["verdicts"] == {"unknown": 1}
    assert diagnostics._error_class(secret_like) == "unknown"


def test_report_flags_unreadable_dispatch_artifact_and_bounds_jsonl_tail(tmp_path):
    state = tmp_path / "pincer"
    (state / "dispatch-telemetry.jsonl").mkdir(parents=True)
    write_json(state / "inflight.json", {})
    write_json(state / "global-gate.json", {})

    result = diagnostics.collect(
        state,
        driver_probe=lambda: {"available": True, "loaded": False},
    )

    warning = next(row for row in result["warnings"] if row["code"] == "unreadable_artifact")
    assert warning["artifact"] == "dispatch_telemetry"
    assert warning["error_class"] == "IsADirectoryError"

    ledger = tmp_path / "bounded.jsonl"
    write_jsonl(ledger, [{"sequence": number} for number in range(5)])
    assert [row["sequence"] for row in diagnostics._read_jsonl(ledger, limit=2)] == [3, 4]


def test_jsonl_reader_participates_in_writer_lock(tmp_path, monkeypatch):
    ledger = tmp_path / "locked.jsonl"
    write_jsonl(ledger, [{"sequence": 1}])
    operations = []

    def observe(_fd, operation):
        operations.append(operation)

    monkeypatch.setattr(diagnostics.fcntl, "flock", observe)
    assert diagnostics._read_jsonl(ledger) == [{"sequence": 1}]
    assert operations == [diagnostics.fcntl.LOCK_SH, diagnostics.fcntl.LOCK_UN]


def test_report_flags_every_unreadable_core_artifact(tmp_path):
    state = tmp_path / "pincer"
    for name in ("run-ledger.jsonl", "work-history.jsonl", "mem-samples.jsonl"):
        (state / name).mkdir(parents=True, exist_ok=True)
    write_json(state / "inflight.json", {})
    write_json(state / "global-gate.json", {})

    result = diagnostics.collect(
        state,
        driver_probe=lambda: {"available": True, "loaded": False},
    )

    unreadable = {
        warning["artifact"]: warning["error_class"]
        for warning in result["warnings"]
        if warning["code"] == "unreadable_artifact"
    }
    assert unreadable == {
        "run_ledger": "IsADirectoryError",
        "work_history": "IsADirectoryError",
        "memory_samples": "IsADirectoryError",
    }


def test_ledger_summaries_report_bounded_tail_window(tmp_path, monkeypatch):
    state = tmp_path / "pincer"
    write_jsonl(state / "run-ledger.jsonl", [
        {"name": "loop", "result": "done", "merged": [number], "infra_failures": []}
        for number in range(5)
    ])
    write_json(state / "inflight.json", {})
    write_json(state / "global-gate.json", {})
    monkeypatch.setattr(diagnostics, "MAX_JSONL_ROWS", 2)

    result = diagnostics.collect(
        state,
        driver_probe=lambda: {"available": True, "loaded": False},
    )

    assert result["run_ledger"]["records"] == 5
    assert result["run_ledger"]["summarized_records"] == 2
    assert result["run_ledger"]["window_truncated"] is True


def test_jsonl_reader_scans_only_a_bounded_byte_tail(tmp_path, monkeypatch):
    ledger = tmp_path / "bounded-bytes.jsonl"
    write_jsonl(ledger, [
        {"sequence": number, "padding": "x" * 40}
        for number in range(20)
    ])
    monkeypatch.setattr(diagnostics, "MAX_JSONL_BYTES", 180)
    stats = {}

    rows = diagnostics._read_jsonl(ledger, errors=[], stats=stats)

    assert rows
    assert rows[-1]["sequence"] == 19
    assert all(row["sequence"] > 0 for row in rows)
    assert stats["bytes_scanned"] <= 180
    assert stats["bytes_total"] == ledger.stat().st_size
    assert stats["window_truncated"] is True


def test_corrupt_jsonl_is_explicit_and_not_partially_summarized(tmp_path):
    state = tmp_path / "pincer"
    ledger = state / "run-ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"name":"loop","result":"done"}\nnot-json\n')
    write_json(state / "inflight.json", {})
    write_json(state / "global-gate.json", {})

    result = diagnostics.collect(
        state,
        driver_probe=lambda: {"available": True, "loaded": False},
    )

    warning = next(
        row for row in result["warnings"]
        if row["code"] == "unreadable_artifact" and row["artifact"] == "run_ledger"
    )
    assert warning["error_class"] == "JSONDecodeError"
    assert result["run_ledger"]["summarized_records"] == 0
    assert result["run_ledger"]["classes"] == {}


def test_corrupt_loop_and_fleet_files_are_reported_not_treated_as_empty(tmp_path):
    state = tmp_path / "pincer"
    (state / "loops").mkdir(parents=True)
    (state / "loops" / "bad.json").write_text("not-json")
    (state / "bad-state.json").write_bytes(b"\xff")
    write_json(state / "inflight.json", {})
    write_json(state / "global-gate.json", {})

    result = diagnostics.collect(
        state,
        driver_probe=lambda: {"available": True, "loaded": False},
    )

    unreadable = {
        row["artifact"]: row["error_class"]
        for row in result["warnings"]
        if row["code"] == "unreadable_artifact"
    }
    assert unreadable["loop_configs"] == "JSONDecodeError"
    assert unreadable["fleet_states"] == "UnicodeDecodeError"
    assert result["loops"]["files_seen"] == 1
    assert result["loops"]["invalid_files"] == 1
    assert result["fleet_states"]["files_seen"] == 1
    assert result["fleet_states"]["invalid_files"] == 1


def test_loop_and_fleet_file_scans_are_bounded(tmp_path, monkeypatch):
    state = tmp_path / "pincer"
    for number in range(5):
        write_json(state / "loops" / f"loop-{number}.json", {
            "name": f"loop-{number}", "enabled": False,
        })
        write_json(state / f"fleet-{number}-state.json", {
            "phase": "done", "final_verdict": "pass",
        })
    write_json(state / "inflight.json", {})
    write_json(state / "global-gate.json", {})
    monkeypatch.setattr(diagnostics, "MAX_STATE_FILES", 2)

    result = diagnostics.collect(
        state,
        driver_probe=lambda: {"available": True, "loaded": False},
    )

    assert result["loops"]["files_seen"] == 2
    assert result["loops"]["window_truncated"] is True
    assert result["fleet_states"]["files_seen"] == 2
    assert result["fleet_states"]["window_truncated"] is True


def test_output_is_private_at_creation_and_refuses_symlink(tmp_path):
    state = tmp_path / "pincer"
    output = tmp_path / "report.json"

    assert diagnostics.main(["--state-dir", str(state), "--output", str(output)]) == 0
    assert output.stat().st_mode & 0o777 == 0o600

    victim = tmp_path / "victim.txt"
    victim.write_text("do not overwrite")
    output.unlink()
    output.symlink_to(victim)
    try:
        diagnostics.main(["--state-dir", str(state), "--output", str(output)])
    except OSError:
        pass
    else:
        raise AssertionError("symlink output must be refused")
    assert victim.read_text() == "do not overwrite"
    assert output.is_symlink()
