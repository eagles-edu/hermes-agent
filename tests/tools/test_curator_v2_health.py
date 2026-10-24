"""Focused coverage for the local Curator V2 readiness bridge."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from unittest.mock import patch

from tools.curator_v2_health import _purge_scheduler_ready, curator_v2_readiness


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
        "\ufeffevents:\n  - codex:turn:start\n  - codex:turn:end\n"
        "durable_events:\n  - codex:turn:start\n  - codex:turn:end\n",
        encoding="utf-8",
    )
    with patch("tools.curator_v2_health._purge_scheduler_ready", return_value=False), patch(
        "tools.curator_v2_health._journal_alert_delivery_ready", return_value=False
    ):
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
    assert result["database_storage_adapter"]["ready"] is True
    assert result["alert_delivery"]["ready"] is False
    assert result["artifact_producer_inventory"]["ready"] is True
    assert result["host_identity"]["ready"] is True
    assert result["purge_scheduler"]["ready"] is False
    assert all(value["observed_at"] for value in result.values())


def test_curator_readiness_keeps_missing_probe_sources_unknown(tmp_path):
    with patch("tools.curator_v2_health._purge_scheduler_ready", return_value=False), patch(
        "tools.curator_v2_health._journal_alert_delivery_ready", return_value=False
    ):
        result = curator_v2_readiness(
            {"hermes_context": True, "hermes_project_id": "pilot-project"},
            profile_id="profile-a",
            session_id="session-a",
            home=tmp_path,
        )

    assert result["event_router"]["ready"] is None
    assert result["human_review"]["ready"] is None
    assert result["database_storage_adapter"]["ready"] is None
    assert result["alert_delivery"]["ready"] is False
    assert result["artifact_producer_inventory"]["ready"] is None
    assert result["purge_scheduler"]["ready"] is False


def test_purge_scheduler_requires_fresh_enabled_timer_and_successful_run(monkeypatch):
    now_us = 100_000_000_000
    trigger_us = now_us - 17 * 60 * 60 * 1_000_000
    service_start_us = trigger_us + 1_000_000
    outputs = iter(
        (
            "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
            f"LastTriggerUSecMonotonic={trigger_us}\n",
            "LoadState=loaded\nActiveState=inactive\nResult=success\n"
            f"ExecMainStartTimestampMonotonic={service_start_us}\n",
        )
    )
    monkeypatch.setattr("tools.curator_v2_health.shutil.which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr("tools.curator_v2_health.time.monotonic_ns", lambda: now_us * 1000)
    monkeypatch.setattr(
        "tools.curator_v2_health.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, next(outputs), ""
        ),
    )

    assert _purge_scheduler_ready("vhost-manager-alpha") is True


def test_purge_scheduler_reports_missing_and_unavailable_states(monkeypatch):
    monkeypatch.setattr("tools.curator_v2_health.shutil.which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(
        "tools.curator_v2_health.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "LoadState=not-found\nActiveState=inactive\n", ""
        ),
    )
    assert _purge_scheduler_ready("vhost-manager-alpha") is False

    monkeypatch.setattr(
        "tools.curator_v2_health.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "manager unavailable"),
    )
    assert _purge_scheduler_ready("vhost-manager-alpha") is None


def test_purge_scheduler_rejects_stale_trigger_and_failed_service(monkeypatch):
    now_us = 100_000_000_000
    timer = (
        "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
    )
    monkeypatch.setattr("tools.curator_v2_health.shutil.which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr("tools.curator_v2_health.time.monotonic_ns", lambda: now_us * 1000)

    def run_with_service_result(result: str, trigger_us: int) -> bool | None:
        outputs = iter(
            (
                timer + f"LastTriggerUSecMonotonic={trigger_us}\n",
                "LoadState=loaded\nActiveState=inactive\n"
                f"Result={result}\nExecMainStartTimestampMonotonic={trigger_us + 1}\n",
            )
        )
        monkeypatch.setattr(
            "tools.curator_v2_health.subprocess.run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args[0], 0, next(outputs), ""
            ),
        )
        return _purge_scheduler_ready("vhost-manager-alpha")

    stale_us = 19 * 60 * 60 * 1_000_000
    assert run_with_service_result("success", now_us - stale_us) is False
    assert run_with_service_result("failed", now_us - 60_000_000) is False


def test_journal_alert_delivery_requires_journald_streams_and_readable_user_journal(
    monkeypatch,
):
    from tools.curator_v2_health import _journal_alert_delivery_ready

    monkeypatch.setattr(
        "tools.curator_v2_health._systemd_properties",
        lambda unit: {
            "LoadState": "loaded",
            "StandardOutput": "journal",
            "StandardError": "journal",
        },
    )
    monkeypatch.setattr(
        "tools.curator_v2_health.shutil.which", lambda _: "/usr/bin/journalctl"
    )
    monkeypatch.setattr(
        "tools.curator_v2_health.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "Archived and active journals take up 22.1M.\n", ""
        ),
    )

    assert _journal_alert_delivery_ready("vhost-manager-alpha") is True


def test_journal_alert_delivery_reports_bad_route_and_unknown_journal(monkeypatch):
    from tools.curator_v2_health import _journal_alert_delivery_ready

    service = {
        "LoadState": "loaded",
        "StandardOutput": "journal",
        "StandardError": "journal",
    }
    monkeypatch.setattr(
        "tools.curator_v2_health._systemd_properties", lambda unit: service
    )
    monkeypatch.setattr(
        "tools.curator_v2_health.shutil.which", lambda _: "/usr/bin/journalctl"
    )
    monkeypatch.setattr(
        "tools.curator_v2_health.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "unavailable"),
    )
    assert _journal_alert_delivery_ready("vhost-manager-alpha") is None

    service["StandardError"] = "null"
    assert _journal_alert_delivery_ready("vhost-manager-alpha") is False


def test_review_grant_probe_is_unknown_when_effective_uid_is_unavailable(
    tmp_path, monkeypatch
):
    import tools.curator_v2_health as health_probe

    database = tmp_path / "curator.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE project_grants "
            "(grant_id TEXT, project_id TEXT, subject_id TEXT, action TEXT)"
        )
        connection.execute(
            "CREATE TABLE project_grant_revocations (grant_id TEXT)"
        )
    monkeypatch.delattr(health_probe.os, "geteuid", raising=False)

    result = curator_v2_readiness(
        {"args": ["--database", str(database)], "hermes_project_id": "pilot"},
        profile_id="profile-a",
        session_id="session-a",
        home=tmp_path,
    )

    assert result["human_review"]["ready"] is None


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
        "database_storage_adapter",
        "alert_delivery",
        "host_identity",
        "human_review",
        "artifact_producer_inventory",
    }
