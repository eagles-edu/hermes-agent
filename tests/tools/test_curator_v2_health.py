"""Focused coverage for the local Curator V2 readiness bridge."""

from __future__ import annotations

import os
import sqlite3
from unittest.mock import patch

from tools.curator_v2_health import curator_v2_readiness


def test_curator_readiness_reports_observed_local_state(tmp_path):
    project_id = "pilot-project"
    home = tmp_path / "profile"
    outbox = home / "curatormd" / "codex-outbox.sqlite3"
    outbox.parent.mkdir(parents=True)
    with sqlite3.connect(outbox) as connection:
        connection.execute(
            "CREATE TABLE codex_events "
            "(project_id TEXT, acknowledged INTEGER NOT NULL)"
        )
    database = tmp_path / "curator.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE project_grants "
            "(grant_id TEXT, project_id TEXT, subject_id TEXT, action TEXT)"
        )
        connection.execute(
            "CREATE TABLE project_grant_revocations (grant_id TEXT)"
        )
        connection.execute(
            "INSERT INTO project_grants VALUES (?, ?, ?, 'review')",
            ("g1", project_id, f"local-uid:{os.geteuid()}"),
        )
    hooks = home / "hooks" / "curator-v2"
    hooks.mkdir(parents=True)
    (hooks / "HOOK.yaml").write_text(
        "events:\n  - codex:turn:start\n  - codex:turn:end\n"
        "durable_events:\n  - codex:turn:start\n  - codex:turn:end\n",
        encoding="utf-8",
    )
    result = curator_v2_readiness(
        {
            "args": ["--database", str(database)],
            "hermes_context": True,
            "hermes_project_id": project_id,
        },
        profile_id="profile-a",
        session_id="session-a",
        home=home,
    )

    assert result["event_router"]["ready"] is True
    assert result["human_review"]["ready"] is True
    assert result["artifact_producer_inventory"]["ready"] is True
    assert result["host_identity"]["ready"] is True
    assert result["purge_scheduler"]["ready"] is False
    assert all(value["observed_at"] for value in result.values())


def test_curator_readiness_keeps_missing_probe_sources_unknown(tmp_path):
    result = curator_v2_readiness(
        {"hermes_context": True, "hermes_project_id": "pilot-project"},
        profile_id="profile-a",
        session_id="session-a",
        home=tmp_path,
    )

    assert result["event_router"]["ready"] is None
    assert result["human_review"]["ready"] is None
    assert result["artifact_producer_inventory"]["ready"] is None
    assert result["purge_scheduler"]["ready"] is False


def test_trusted_curator_mcp_metadata_carries_host_probes(tmp_path):
    from tools.mcp_tool_handlers import _trusted_hermes_request_meta

    with patch(
        "hermes_cli.mcp_config._get_mcp_servers",
        return_value={
            "curator_v2": {
                "command": "curatormd-v2-mcp",
                "hermes_context": True,
                "hermes_project_id": "pilot-project",
            }
        },
    ), patch("hermes_constants.get_hermes_home", return_value=tmp_path), patch(
        "hermes_constants.profile_name_for_home", return_value="pilot-profile"
    ):
        metadata = _trusted_hermes_request_meta(
            "curator_v2", {"session_id": "session-1"}
        )

    assert metadata is not None
    assert metadata["hermes/profile_id"] == "pilot-profile"
    assert set(metadata["hermes/readiness"]) == {
        "event_router",
        "purge_scheduler",
        "host_identity",
        "human_review",
        "artifact_producer_inventory",
    }
