"""Durable Curator work summaries stay bounded and content-free."""

import json

from gateway.codex_outbox import CodexLifecycleOutbox


def _summary(fingerprint: str, path: str = "src/change.py") -> str:
    return json.dumps({
        "schema_version": 1,
        "repository_fingerprint": fingerprint,
        "changed_files": [{"path": path, "change": "update"}],
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


def test_outbox_drops_wrong_version_type_and_malformed_labels(tmp_path):
    outbox = CodexLifecycleOutbox(tmp_path / "outbox.sqlite3", profile_id="profile-a")
    try:
        assert outbox.bind_goal("project-a", "session-a", "goal-a")
        cases = []
        boolean_version = json.loads(_summary("a" * 64))
        boolean_version["schema_version"] = True
        cases.append(boolean_version)
        malformed_path_change = json.loads(_summary("a" * 64))
        malformed_path_change["changed_files"][0]["change"] = ["update"]
        cases.append(malformed_path_change)
        malformed_check_name = json.loads(_summary("a" * 64))
        malformed_check_name["checks"][0]["name"] = ["scripts/check"]
        cases.append(malformed_check_name)

        for index, summary in enumerate(cases):
            event = outbox.enqueue("codex:turn:end", {
                "attempt_id": f"attempt-{index}",
                "session_id": "session-a",
                "turn_id": f"turn-{index}",
                "outcome": "completed",
                "work_summary_json": json.dumps(summary),
            })
            assert event is not None
            assert "work_summary_json" not in event["context"]
    finally:
        outbox.close()


def test_outbox_rejects_private_paths_even_with_valid_summary_schema(tmp_path):
    outbox = CodexLifecycleOutbox(tmp_path / "outbox.sqlite3", profile_id="profile-a")
    try:
        assert outbox.bind_goal("project-a", "session-a", "goal-a")
        forged = json.loads(_summary("a" * 64))
        forged["changed_files"] = [{"path": ".aws/credentials", "change": "update"}]
        event = outbox.enqueue("codex:turn:end", {
            "attempt_id": "attempt-a",
            "session_id": "session-a",
            "turn_id": "turn-a",
            "outcome": "completed",
            "work_summary_json": json.dumps(forged),
        })

        assert event is not None
        assert "work_summary_json" not in event["context"]
        assert ".aws" not in json.dumps(event)
        assert "credentials" not in json.dumps(event)
    finally:
        outbox.close()


def test_outbox_rejects_credential_values_embedded_in_changed_paths(tmp_path):
    paths = (
        "docs/sk-proj-" + "a" * 28 + ".md",
        "scripts/github_pat_" + "b" * 24 + ".txt",
        "docs/password=custom-value.txt",
        "docs/passphrase.txt",
        "config/aws_secret_access_key",
        "config/access_key_id",
        "config/session_key.txt",
        "browser/cookies.json",
        "config/MY_AWS_SECRET_ACCESS_KEY",
        "config/OAUTH_CLIENT_SECRET",
        "browser/MY_BEARER_TOKEN",
        "notes/dns_txt=challenge-value.txt",
        "notes/manual DNS code is Q7mN4pL8xW2cR6vB.txt",
        "notes/dns-challenge-Q7mN4pL8xW2cR6vB.txt",
        "notes/manual_dns_txt_Q7mN4pL8xW2cR6vB.txt",
    )
    for index, path in enumerate(paths):
        outbox = CodexLifecycleOutbox(
            tmp_path / f"outbox-{index}.sqlite3", profile_id="profile-a"
        )
        try:
            assert outbox.bind_goal("project-a", "session-a", "goal-a")
            event = outbox.enqueue("codex:turn:end", {
                "attempt_id": f"attempt-{index}",
                "session_id": "session-a",
                "turn_id": f"turn-{index}",
                "outcome": "completed",
                "work_summary_json": _summary("a" * 64, path),
            })

            assert event is not None
            assert "work_summary_json" not in event["context"]
            assert path not in json.dumps(event)
            persisted = outbox._connection.execute(
                "SELECT context_json FROM codex_events WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()[0]
            assert path not in persisted
        finally:
            outbox.close()
