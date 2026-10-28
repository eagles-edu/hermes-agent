"""Durable Curator work summaries stay bounded and content-free."""

import json

from gateway.codex_outbox import CodexLifecycleOutbox


def _summary(fingerprint: str) -> str:
    return json.dumps({
        "schema_version": 1,
        "repository_fingerprint": fingerprint,
        "changed_files": [{"path": "src/change.py", "change": "update"}],
        "omitted_files": 0,
        "checks": [{"name": "scripts/check", "exit_code": 0}],
        "omitted_checks": 0,
    })


def test_outbox_persists_only_valid_work_summary_fields(tmp_path):
    outbox = CodexLifecycleOutbox(tmp_path / "outbox.sqlite3", profile_id="profile-a")
    try:
        assert outbox.bind_goal("project-a", "session-a", "goal-a")
        summary = _summary("a" * 64)
        event = outbox.enqueue("codex:turn:end", {
            "attempt_id": "attempt-a",
            "session_id": "session-a",
            "turn_id": "turn-a",
            "outcome": "completed",
            "work_summary_json": summary,
        })

        assert event is not None
        assert event["context"]["work_summary_json"] == summary
        persisted = outbox._connection.execute(
            "SELECT context_json FROM codex_events WHERE event_id = ?",
            (event["event_id"],),
        ).fetchone()[0]
        assert "private command" not in persisted
        assert "src/change.py" in persisted
    finally:
        outbox.close()


def test_outbox_drops_malformed_or_oversized_work_summary_but_keeps_lifecycle(tmp_path):
    outbox = CodexLifecycleOutbox(tmp_path / "outbox.sqlite3", profile_id="profile-a")
    try:
        assert outbox.bind_goal("project-a", "session-a", "goal-a")
        invalid = json.dumps({"prompt": "private prompt text"})
        event = outbox.enqueue("codex:turn:end", {
            "attempt_id": "attempt-a",
            "session_id": "session-a",
            "turn_id": "turn-a",
            "outcome": "completed",
            "work_summary_json": invalid,
        })
        assert event is not None
        assert "work_summary_json" not in event["context"]
        assert "private prompt" not in json.dumps(event)
    finally:
        outbox.close()
