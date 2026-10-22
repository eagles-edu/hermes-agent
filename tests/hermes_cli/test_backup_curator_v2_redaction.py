"""Hermes state.db backups must not restore Curator V2 tool payloads."""

from argparse import Namespace
import json
import sqlite3
from pathlib import Path
import zipfile

from hermes_state import SessionDB
from tests.hermes_cli.test_backup import _make_hermes_tree


def test_backup_preserves_redacted_curator_v2_transcript_projection(tmp_path, monkeypatch):
    from agent.session_persistence import _db_flush_row
    from hermes_cli.backup import run_backup

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    _make_hermes_tree(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    session_db = SessionDB(db_path=hermes_home / "state.db")
    try:
        session_db.create_session("privacy-check", "cli", model="m")
    finally:
        session_db.close()

    argument_canary = "CURATOR_V2_BACKUP_ARGUMENT_CANARY"
    result_canary = "CURATOR_V2_BACKUP_RESULT_CANARY"
    assistant = _db_flush_row(None, {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call-curator",
            "type": "function",
            "function": {
                "name": "mcp__curator_v2__fact_record",
                "arguments": json.dumps({"bullet": argument_canary}),
            },
        }],
    }, False)
    tool = _db_flush_row(None, {
        "role": "tool",
        "name": "mcp__curator_v2__goal_report",
        "tool_call_id": "call-curator",
        "content": json.dumps({"fact": result_canary}),
    }, False)

    # Store the redacted projection exactly as the writer passes it to SessionDB.
    # The ordinary append path loads the live conversation loop to classify
    # display identity; this backup test needs only the durable row contract.
    rows = [
        (
            assistant["role"],
            assistant["content"],
            json.dumps(assistant["tool_calls"]),
            None,
            f"{1:032x}",
        ),
        (
            tool["role"],
            tool["content"],
            None,
            tool["tool_name"],
            f"{2:032x}",
        ),
    ]
    with sqlite3.connect(hermes_home / "state.db") as connection:
        connection.executemany(
            """INSERT INTO messages
               (session_id, role, content, tool_calls, tool_name, timestamp, message_uid)
               VALUES ('privacy-check', ?, ?, ?, ?, 1.0, ?)""",
            rows,
        )
        connection.execute(
            "UPDATE sessions SET message_count = 2, tool_call_count = 1 WHERE id = 'privacy-check'"
        )

    archive = tmp_path / "curator-v2-backup.zip"
    run_backup(Namespace(output=str(archive)))
    with zipfile.ZipFile(archive) as snapshot:
        state_db_bytes = snapshot.read("state.db")

    assert argument_canary.encode() not in state_db_bytes
    assert result_canary.encode() not in state_db_bytes
    backed_up_db = tmp_path / "backed-up-state.db"
    backed_up_db.write_bytes(state_db_bytes)
    with sqlite3.connect(backed_up_db) as connection:
        tool_calls_json, result_content = connection.execute(
            """SELECT tool_calls, content FROM messages
               WHERE session_id = 'privacy-check' AND role = 'tool'"""
        ).fetchone()
        assistant_tool_calls = connection.execute(
            """SELECT tool_calls FROM messages
               WHERE session_id = 'privacy-check' AND role = 'assistant'"""
        ).fetchone()[0]
    assert tool_calls_json is None
    assert json.loads(assistant_tool_calls)[0]["function"]["arguments"] == '{"_redacted":true}'
    assert result_content == "[redacted Curator V2 tool result]"
