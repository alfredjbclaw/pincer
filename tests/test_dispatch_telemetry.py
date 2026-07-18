import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import dispatch_telemetry as dt
import runtime_adapter as ra


def test_codex_usage_parser_keeps_final_numeric_usage_only():
    stdout = "\n".join(
        [
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 100, "cached_input_tokens": 40,
                "output_tokens": 10, "reasoning_output_tokens": 3,
                "total_tokens": 110,
            }}),
            "prompt text that is not JSON",
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {
                    "input_tokens": 120, "cached_input_tokens": 80,
                    "output_tokens": 12, "reasoning_output_tokens": 4,
                    "total_tokens": 132,
                }
            }}}),
        ]
    )

    assert dt.parse_codex_usage(stdout) == {
        "input_tokens": 120,
        "cached_input_tokens": 80,
        "output_tokens": 12,
        "reasoning_output_tokens": 4,
        "total_tokens": 132,
    }


def test_attempt_and_outcome_receipts_are_private_and_summarizable(tmp_path, monkeypatch):
    ledger = tmp_path / "state" / "dispatch.jsonl"
    monkeypatch.setenv("PINCER_DISPATCH_TELEMETRY", str(ledger))
    dt.start_dispatch(attempt=2, resumed=True, workdir=tmp_path / "private-project")
    payload_text = "do not persist this private prompt fixture material"
    stdout = json.dumps({
        "type": "turn.completed",
        "usage": {"input_tokens": 200, "cached_input_tokens": 150, "output_tokens": 20},
        "prompt": payload_text,
    })

    assert dt.record_attempt(
        runtime="codex",
        model="gpt-5.6-sol",
        effort="high",
        status="done",
        exit_code=0,
        duration_seconds=12.5,
        failure_class="none",
        stdout=stdout,
    )
    assert dt.record_outcome(
        runtime="codex",
        status="done",
        exit_code=0,
        duration_seconds=12.8,
        fallback_used=False,
        fallback_reason=None,
        empty_diff=False,
    )
    dt.clear_dispatch()

    raw = ledger.read_text()
    assert payload_text not in raw
    assert "private-project" not in raw
    assert ledger.stat().st_mode & 0o777 == 0o600
    assert ledger.parent.stat().st_mode & 0o777 == 0o700
    summary = dt.summarize(dt.read_records(ledger))
    assert summary["attempts"] == 1
    assert summary["outcomes"] == 1
    assert summary["input_tokens"] == 200
    assert summary["cached_input_tokens"] == 150
    assert summary["cache_read_ratio"] == 0.75
    assert summary["resumed_outcomes"] == 1
    assert summary["models"] == {"gpt-5.6-sol": 1}
    assert summary["runtimes"] == {"codex": 1}
    assert summary["efforts"] == {"high": 1}
    assert summary["retry_attempts"] == 0
    assert summary["retried_dispatches"] == 0


def test_summary_reports_privacy_safe_models_and_transport_retries():
    dispatch_id = "a" * 32
    secret_like = "gh" + "p_" + ("a" * 36)
    summary = dt.summarize([
        {"kind": "attempt", "dispatch_id": dispatch_id, "runtime": "codex",
         "model": "gpt-5.6-sol", "effort": "high", "failure_class": "rate_limit_429"},
        {"kind": "attempt", "dispatch_id": dispatch_id, "runtime": secret_like,
         "model": secret_like, "effort": secret_like, "failure_class": "none"},
    ])
    encoded = json.dumps(summary)

    assert summary["retry_attempts"] == 1
    assert summary["retried_dispatches"] == 1
    assert summary["models"] == {"gpt-5.6-sol": 1, "unknown": 1}
    assert summary["runtimes"] == {"codex": 1, "unknown": 1}
    assert summary["efforts"] == {"high": 1, "unknown": 1}
    assert secret_like not in encoded

    prefixed_secret = "gpt-5-" + ("a" * 64)
    assert dt._model(prefixed_secret) == "unknown"


def test_pytest_never_writes_default_live_ledger(monkeypatch):
    monkeypatch.delenv("PINCER_DISPATCH_TELEMETRY", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "fixture")
    dt.start_dispatch(attempt=1, resumed=False, workdir=Path.cwd())
    assert not dt.record_attempt(
        runtime="codex",
        model="fixture",
        effort="low",
        status="error",
        exit_code=1,
        duration_seconds=0,
        failure_class="test",
    )
    dt.clear_dispatch()


def test_runtime_dispatch_writes_attempt_and_outcome_receipts(tmp_path, monkeypatch):
    ledger = tmp_path / "dispatch.jsonl"
    monkeypatch.setenv("PINCER_DISPATCH_TELEMETRY", str(ledger))
    monkeypatch.setattr(ra.shutil, "which", lambda _name: "/fixture/codex")
    monkeypatch.setattr(ra.global_gate, "slot", lambda **_kwargs: nullcontext())
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "fixture-thread"}),
        json.dumps({"item": {"type": "agent_message", "text": (
            "STATUS: done\nFILES: changed.txt\nVALIDATION: fixture\nNEXT: none"
        )}}),
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 300, "cached_input_tokens": 200,
            "output_tokens": 30, "reasoning_output_tokens": 10,
            "total_tokens": 330,
        }}),
    ])
    monkeypatch.setattr(
        ra.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=stdout, stderr="", returncode=0),
    )

    result = ra.dispatch(
        "private prompt",
        workdir=tmp_path,
        config=ra.RuntimeConfig(fallback_enabled=False),
    )

    records = dt.read_records(ledger)
    assert result.status == "done"
    assert [record["kind"] for record in records] == ["attempt", "outcome"]
    assert records[0]["input_tokens"] == 300
    assert records[0]["cached_input_tokens"] == 200
    assert records[1]["status"] == "done"
    assert all("workdir_hash" not in record for record in records)
    assert "private prompt" not in ledger.read_text()


def test_runtime_dispatch_records_bounded_outcome_when_internal_step_raises(tmp_path, monkeypatch):
    ledger = tmp_path / "dispatch.jsonl"
    monkeypatch.setenv("PINCER_DISPATCH_TELEMETRY", str(ledger))
    private_error = "private exception details must not be stored"

    def explode(**_kwargs):
        raise RuntimeError(private_error)

    monkeypatch.setattr(ra.global_gate, "slot", explode)

    with pytest.raises(RuntimeError, match="private exception"):
        ra.dispatch(
            "private prompt",
            workdir=tmp_path,
            config=ra.RuntimeConfig(fallback_enabled=False),
        )

    records = dt.read_records(ledger)
    assert len(records) == 1
    assert records[0]["kind"] == "outcome"
    assert records[0]["status"] == "error"
    assert records[0]["fallback_reason"] == "exception:RuntimeError"
    assert private_error not in ledger.read_text()
    assert "private prompt" not in ledger.read_text()


def test_runtime_dispatch_does_not_record_success_before_gate_cleanup(tmp_path, monkeypatch):
    ledger = tmp_path / "dispatch.jsonl"
    monkeypatch.setenv("PINCER_DISPATCH_TELEMETRY", str(ledger))
    private_error = "private gate cleanup failure"

    class CleanupFails:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            raise RuntimeError(private_error)

    monkeypatch.setattr(ra.global_gate, "slot", lambda **_kwargs: CleanupFails())
    monkeypatch.setattr(
        ra,
        "_execute",
        lambda *_args, **_kwargs: (
            "STATUS: done\nFILES: changed.txt\nVALIDATION: fixture\nNEXT: none",
            "",
            0,
            "fixture-thread",
        ),
    )

    with pytest.raises(RuntimeError, match="gate cleanup"):
        ra.dispatch(
            "private prompt",
            workdir=tmp_path,
            config=ra.RuntimeConfig(fallback_enabled=False),
        )

    records = dt.read_records(ledger)
    assert len(records) == 1
    assert records[0]["kind"] == "outcome"
    assert records[0]["status"] == "error"
    assert records[0]["fallback_reason"] == "exception:RuntimeError"
    assert private_error not in ledger.read_text()


def test_override_keeps_existing_parent_permissions(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    ledger = parent / "dispatch.jsonl"

    assert dt._append({"kind": "fixture"}, path=ledger)

    assert parent.stat().st_mode & 0o777 == 0o755
    assert ledger.stat().st_mode & 0o777 == 0o600


def test_append_fsyncs_file_and_parent_directory(tmp_path, monkeypatch):
    ledger = tmp_path / "dispatch.jsonl"
    real_fsync = dt.os.fsync
    fsynced_modes = []

    def observe_fsync(fd):
        fsynced_modes.append(dt.os.fstat(fd).st_mode)
        return real_fsync(fd)

    monkeypatch.setattr(dt.os, "fsync", observe_fsync)

    assert dt._append({"kind": "fixture"}, path=ledger)
    stat_module = __import__("stat")
    assert any(stat_module.S_ISREG(mode) for mode in fsynced_modes)
    assert any(stat_module.S_ISDIR(mode) for mode in fsynced_modes)


def test_failed_receipt_writes_are_logged_without_breaking_dispatch(monkeypatch):
    monkeypatch.setattr(ra.dispatch_telemetry, "enabled", lambda: True)
    monkeypatch.setattr(ra.dispatch_telemetry, "record_attempt", lambda **_kwargs: False)
    monkeypatch.setattr(ra.dispatch_telemetry, "record_outcome", lambda **_kwargs: False)
    warnings = []
    monkeypatch.setattr(ra.LOG, "warning", lambda message: warnings.append(message))

    assert not ra._record_attempt_receipt(
        runtime="codex", model="gpt-5.6-sol", effort="high", status="error",
        exit_code=1, duration_seconds=1, failure_class="nonzero_exit",
    )
    assert not ra._record_outcome_receipt(
        runtime="codex", status="error", exit_code=1, duration_seconds=1,
        fallback_used=False, fallback_reason=None, empty_diff=False,
    )

    assert any("attempt telemetry write failed" in message for message in warnings)
    assert any("outcome telemetry write failed" in message for message in warnings)


def test_append_completes_short_os_writes(tmp_path, monkeypatch):
    ledger = tmp_path / "dispatch.jsonl"
    real_write = dt.os.write

    def short_write(fd, payload):
        return real_write(fd, payload[: max(1, len(payload) // 2)])

    monkeypatch.setattr(dt.os, "write", short_write)
    assert dt._append({"kind": "fixture", "status": "done"}, path=ledger)
    assert json.loads(ledger.read_text()) == {"kind": "fixture", "status": "done"}


def test_append_rolls_back_partial_record_on_write_failure(tmp_path, monkeypatch):
    ledger = tmp_path / "dispatch.jsonl"
    ledger.write_text('{"kind":"existing"}\n')
    original = ledger.read_bytes()
    real_write = dt.os.write
    calls = 0

    def fail_after_partial(fd, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, payload[: max(1, len(payload) // 2)])
        raise OSError("fixture write failure")

    monkeypatch.setattr(dt.os, "write", fail_after_partial)
    assert not dt._append({"kind": "fixture", "status": "done"}, path=ledger)
    assert ledger.read_bytes() == original


def test_read_limit_is_bounded_and_unreadable_paths_are_reported(tmp_path):
    ledger = tmp_path / "dispatch.jsonl"
    ledger.write_text("".join(json.dumps({"kind": "outcome", "status": str(i)}) + "\n"
                              for i in range(5)))
    assert [row["status"] for row in dt.read_records(ledger, limit=2)] == ["3", "4"]

    errors = []
    assert dt.read_records(tmp_path, errors=errors) == []
    assert errors == ["IsADirectoryError"]


def test_read_records_scans_only_a_bounded_byte_tail(tmp_path, monkeypatch):
    ledger = tmp_path / "dispatch.jsonl"
    ledger.write_text("".join(
        json.dumps({"kind": "outcome", "status": str(number), "padding": "x" * 40}) + "\n"
        for number in range(20)
    ))
    monkeypatch.setattr(dt, "MAX_READ_BYTES", 180)
    stats = {}

    records = dt.read_records(ledger, stats=stats)

    assert records
    assert records[-1]["status"] == "19"
    assert all(record["status"] != "0" for record in records)
    assert stats["bytes_scanned"] <= 180
    assert stats["bytes_total"] == ledger.stat().st_size
    assert stats["window_truncated"] is True


def test_summary_ignores_invalid_persisted_token_counters():
    summary = dt.summarize([
        {"kind": "attempt", "usage_known": True, "input_tokens": "not-a-number",
         "cached_input_tokens": 25, "output_tokens": -1},
    ])

    assert summary["input_tokens"] == 0
    assert summary["cached_input_tokens"] == 25
    assert summary["output_tokens"] == 0
    assert summary["invalid_usage_records"] == 1


def test_summary_sanitizes_persisted_status_and_failure_labels():
    private = "gh" + "p_" + ("a" * 36)
    summary = dt.summarize([
        {"kind": "attempt", "failure_class": private},
        {"kind": "outcome", "status": private},
    ])

    encoded = json.dumps(summary)
    assert private not in encoded
    assert summary["outcome_statuses"] == {"unknown": 1}
    assert summary["failure_classes"] == {"unknown": 1}


def test_recorded_failure_class_is_allowlisted_not_secret_shaped(tmp_path, monkeypatch):
    ledger = tmp_path / "dispatch.jsonl"
    monkeypatch.setenv("PINCER_DISPATCH_TELEMETRY", str(ledger))
    secret_like = "gh" + "p_" + ("a" * 36)
    dt.start_dispatch(attempt=1, resumed=False, workdir=tmp_path)

    assert dt.record_attempt(
        runtime="codex",
        model="gpt-5.6-sol",
        effort="high",
        status="error",
        exit_code=1,
        duration_seconds=1,
        failure_class=secret_like,
    )
    dt.clear_dispatch()

    raw = ledger.read_text()
    assert secret_like not in raw
    assert dt.read_records(ledger)[0]["failure_class"] == "unknown"


def test_recorded_status_is_an_enum_not_model_text(tmp_path, monkeypatch):
    ledger = tmp_path / "dispatch.jsonl"
    monkeypatch.setenv("PINCER_DISPATCH_TELEMETRY", str(ledger))
    secret_like = "gh" + "p_" + ("a" * 36)
    dt.start_dispatch(attempt=1, resumed=False, workdir=tmp_path)

    assert dt.record_outcome(
        runtime="codex",
        status=secret_like,
        exit_code=1,
        duration_seconds=1,
        fallback_used=False,
        fallback_reason=None,
        empty_diff=False,
    )
    dt.clear_dispatch()

    raw = ledger.read_text()
    assert secret_like not in raw
    assert dt.read_records(ledger)[0]["status"] == "unknown"


def test_corrupt_jsonl_fails_closed_and_reports_parse_class(tmp_path):
    invalid_json = tmp_path / "invalid.jsonl"
    invalid_json.write_text('{"kind":"outcome"}\nnot-json\n')
    errors = []
    assert dt.read_records(invalid_json, errors=errors) == []
    assert errors == ["JSONDecodeError"]

    invalid_utf8 = tmp_path / "invalid-utf8.jsonl"
    invalid_utf8.write_bytes(b'{"kind":"outcome"}\n\xff\n')
    errors = []
    assert dt.read_records(invalid_utf8, errors=errors) == []
    assert errors == ["UnicodeDecodeError"]


def test_cli_reports_corrupt_ledger_and_exits_nonzero(tmp_path, capsys):
    ledger = tmp_path / "invalid.jsonl"
    ledger.write_text("not-json\n")

    assert dt.main(["--path", str(ledger)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["read_errors"] == ["JSONDecodeError"]
    assert output["attempts"] == 0
