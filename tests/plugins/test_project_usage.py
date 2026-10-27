"""Behavioral contracts for the profile-scoped read-only project usage tool."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import hermes_yaml as yaml


def _configure(home, project_id="gptmd"):
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "plugins": {
                "enabled": ["project-usage"],
                "entries": {"project-usage": {"settings": {"project_id": project_id}}},
            }
        }),
        encoding="utf-8",
    )


def _tool():
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    manager = PluginManager()
    manager.discover_and_load()
    entry = registry.get_entry("project_usage", scope=manager.scope_key)
    assert entry is not None
    return entry


def _ledger_snapshot(db_path):
    # SQLite may update its transient WAL shared-memory index while opening a
    # mode=ro reader; the database and WAL files themselves must remain stable.
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in db_path.parent.glob(f"{db_path.name}*")
        if path.is_file() and not path.name.endswith("-shm")
    }


def _record_turn(outbox, project_id, session_id, attempt_id, *, usage):
    assert outbox.bind_goal(project_id, session_id, "goal-private")
    return outbox.enqueue("codex:turn:end", {
        "attempt_id": attempt_id,
        "session_id": session_id,
        "turn_id": f"turn-{attempt_id}",
        "outcome": "completed",
        "provider": "openai-codex",
        "model": "gpt-5.6",
        **({"usage": usage} if usage is not None else {}),
    })


def test_reads_only_configured_project_aggregates_without_private_ids(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _configure(home)

    from gateway.codex_outbox import CodexLifecycleOutbox

    outbox = CodexLifecycleOutbox(profile_id="gptmd-coding")
    _record_turn(outbox, "gptmd", "session-private", "attempt-reported", usage={
        "input_tokens": 100,
        "output_tokens": 25,
        "total_tokens": 125,
        "cache_read_tokens": 10,
        "cache_write_tokens": 4,
        "reasoning_tokens": 8,
    })
    _record_turn(outbox, "gptmd", "session-unknown", "attempt-unknown", usage=None)
    _record_turn(outbox, "another-project", "session-other", "attempt-other", usage={
        "input_tokens": 999,
        "output_tokens": 999,
        "total_tokens": 1998,
    })

    db_path = home / "curatormd" / "codex-outbox.sqlite3"
    before = _ledger_snapshot(db_path)
    entry = _tool()
    report = json.loads(entry.handler({}))
    after = _ledger_snapshot(db_path)

    assert report["status"] == "available"
    assert report["coverage"].startswith("bound Codex app-server")
    assert report["sources"] == ["codex_app_server"]
    assert report["measurement_scope"] == "all_retained_project_usage_rows"
    interval = report["measurement_interval"]
    assert interval["first_occurred_at"] <= interval["last_occurred_at"]
    measured_at = datetime.fromisoformat(report["measured_at"])
    assert measured_at.tzinfo is not None
    assert measured_at.utcoffset() == timedelta(0)
    assert isinstance(report["latest_event_age_seconds"], int)
    assert report["latest_event_age_seconds"] >= 0
    assert report["project_id"] == "gptmd"
    assert report["turns"] == 2
    assert report["reported_turns"] == 1
    assert report["unknown_usage_turns"] == 1
    assert report["coverage_complete"] is False
    assert report["coverage_exclusions"] == [
        "auxiliary_calls_outside_active_turn_context",
        "auxiliary_attempts_without_response", "unbound_sessions",
    ]
    assert report["tokens_complete"] is False
    assert report["tokens"] == {
        "input": 100,
        "output": 25,
        "total": 125,
        "cache_read": 10,
        "cache_write": 4,
        "reasoning": 8,
    }
    assert report["by_provider_model"] == [{
        "provider": "openai-codex",
        "model": "gpt-5.6",
        "turns": 2,
        "reported_turns": 1,
        "unknown_usage_turns": 1,
        "tokens": {
            "input": 100,
            "output": 25,
            "total": 125,
            "cache_read": 10,
            "cache_write": 4,
            "reasoning": 8,
        },
    }]
    serialized = json.dumps(report)
    for private_value in ("goal-private", "session-private", "attempt-reported", "another-project"):
        assert private_value not in serialized
    assert before == after
    outbox._connection.close()


def test_includes_provider_api_turns_without_overcounting_multiple_models(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _configure(home, "vhost")

    from gateway.codex_outbox import CodexLifecycleOutbox

    outbox = CodexLifecycleOutbox(profile_id="vhost-coding")
    assert outbox.bind_goal("vhost", "session-private", "goal-private")
    groups = [
        {
            "provider": "anthropic", "model": "claude-sonnet", "usage_status": "reported",
            "input_tokens": 30, "output_tokens": 7, "total_tokens": 41,
            "cache_read_tokens": 4, "cache_write_tokens": 0, "reasoning_tokens": 2,
        },
        {
            "provider": "openai", "model": "gpt-5.6", "usage_status": "reported",
            "input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 1,
        },
    ]
    assert outbox.record_agent_usage("session-private", "turn-private", groups) == 2
    assert outbox.record_agent_usage("session-private", "turn-private", groups) == 0
    assert outbox.record_agent_usage("session-private", "turn-unknown", [{
        "provider": "google", "model": "gemini", "usage_status": "unknown",
        "input_tokens": None, "output_tokens": None, "total_tokens": None,
        "cache_read_tokens": None, "cache_write_tokens": None, "reasoning_tokens": None,
    }]) == 1
    db_path = home / "curatormd" / "codex-outbox.sqlite3"
    before = _ledger_snapshot(db_path)

    report = json.loads(_tool().handler({}))

    assert _ledger_snapshot(db_path) == before
    assert report["status"] == "available"
    assert report["sources"] == ["hermes_agent_api"]
    assert report["turns"] == 2
    assert report["reported_turns"] == 1
    assert report["unknown_usage_turns"] == 1
    assert report["tokens_complete"] is False
    assert report["tokens"] == {
        "input": 40, "output": 10, "total": 54, "cache_read": 4,
        "cache_write": 0, "reasoning": 3,
    }
    assert {row["provider"] for row in report["by_provider_model"]} == {
        "anthropic", "google", "openai",
    }
    serialized = json.dumps(report)
    for private_value in ("session-private", "goal-private", "turn-private"):
        assert private_value not in serialized
    outbox.close()


def test_missing_ledger_is_unavailable_and_not_a_zero_usage_claim(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _configure(home)
    entry = _tool()

    report = json.loads(entry.handler({}))

    assert report == {"status": "unavailable", "reason": "usage_ledger_not_found"}
    assert "turns" not in report
    assert not (home / "curatormd").exists()


def test_empty_ledger_reports_zero_turns_without_inventing_token_totals(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _configure(home)

    from gateway.codex_outbox import CodexLifecycleOutbox

    outbox = CodexLifecycleOutbox(profile_id="gptmd-coding")
    entry = _tool()
    report = json.loads(entry.handler({}))

    assert report["status"] == "available"
    assert report["turns"] == 0
    assert report["measurement_interval"] == {
        "first_occurred_at": None,
        "last_occurred_at": None,
    }
    assert datetime.fromisoformat(report["measured_at"]).tzinfo == UTC
    assert report["latest_event_age_seconds"] is None
    assert report["reported_turns"] == 0
    assert report["unknown_usage_turns"] == 0
    assert set(report["tokens"].values()) == {None}
    assert report["coverage_complete"] is False
    assert report["coverage_exclusions"] == [
        "auxiliary_calls_outside_active_turn_context",
        "auxiliary_attempts_without_response", "unbound_sessions",
    ]
    assert report["tokens_complete"] is True
    assert report["by_provider_model"] == []
    outbox._connection.close()


def test_future_event_timestamp_keeps_latest_event_age_unknown(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _configure(home)

    from gateway.codex_outbox import CodexLifecycleOutbox

    outbox = CodexLifecycleOutbox(profile_id="gptmd-coding")
    _record_turn(outbox, "gptmd", "session-private", "attempt-future", usage={
        "input_tokens": 100,
        "output_tokens": 25,
        "total_tokens": 125,
    })
    outbox._connection.execute(
        "UPDATE project_usage_events SET occurred_at = ? WHERE project_id = ?",
        ("2999-01-01T00:00:00+00:00", "gptmd"),
    )
    outbox._connection.commit()

    report = json.loads(_tool().handler({}))

    assert report["measurement_interval"]["last_occurred_at"] == "2999-01-01T00:00:00+00:00"
    assert report["latest_event_age_seconds"] is None
    outbox.close()


def test_unconfigured_or_invalid_project_id_cannot_select_a_project(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _configure(home, "../other")
    (home / "curatormd").mkdir()
    db_path = home / "curatormd" / "codex-outbox.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE project_usage_events (project_id TEXT, usage_status TEXT)"
        )
        connection.execute(
            "INSERT INTO project_usage_events VALUES ('other', 'reported')"
        )
    entry = _tool()

    report = json.loads(entry.handler({}))

    assert report == {"status": "not_configured", "reason": "configure_project_id"}
