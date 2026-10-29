"""Behavior contracts for bounded Curator V2 Codex work summaries."""

import json
from pathlib import Path
from types import SimpleNamespace

from agent.codex_runtime import make_codex_app_server_event_bridge

from agent.codex_work_summary import CodexWorkSummary


def test_summary_keeps_only_project_relative_paths_and_safe_check_results(tmp_path: Path):
    changed = tmp_path / "src" / "changed.py"
    private_file = tmp_path / ".env"
    outside = tmp_path.parent / "outside.py"
    summary = CodexWorkSummary(str(tmp_path))

    summary.observe({
        "type": "fileChange",
        "status": "completed",
        "changes": [
            {"path": str(changed), "kind": {"type": "update"}},
            {"path": str(private_file), "kind": {"type": "update"}},
            {"path": str(outside), "kind": {"type": "add"}},
        ],
    })
    summary.observe({
        "type": "commandExecution",
        "command": "uv run --locked scripts/check",
        "exitCode": 0,
        "aggregatedOutput": "private output canary",
    })
    summary.observe({
        "type": "commandExecution",
        "command": "rm -rf private-target",
        "exitCode": 0,
        "aggregatedOutput": "private output canary",
    })

    encoded = summary.to_json()
    assert encoded is not None
    value = json.loads(encoded)
    assert value["changed_files"] == [{"path": "src/changed.py", "change": "update"}]
    assert value["omitted_files"] == 2
    assert value["checks"] == [{"name": "scripts/check", "exit_code": 0}]
    assert "private-argument" not in encoded
    assert "private output canary" not in encoded
    assert "private-target" not in encoded
    assert str(tmp_path) not in encoded


def test_summary_records_nonzero_exit_code_without_command_or_output(tmp_path: Path):
    summary = CodexWorkSummary(str(tmp_path))
    summary.observe({
        "type": "commandExecution",
        "command": "python3 -m pytest tests/example.py",
        "exitCode": 4,
        "aggregatedOutput": "secret output value",
    })

    value = json.loads(summary.to_json() or "{}")
    assert value["checks"] == [{"name": "pytest", "exit_code": 4}]
    assert "password" not in json.dumps(value)
    assert "secret" not in json.dumps(value)


def test_summary_omits_unrecognized_or_unbounded_items(tmp_path: Path):
    summary = CodexWorkSummary(str(tmp_path))
    summary.observe({
        "type": "commandExecution",
        "command": "curl https://example.invalid/private",
        "exitCode": 0,
    })
    summary.observe({
        "type": "commandExecution",
        "command": "git diff --check; rm private-target",
        "exitCode": 0,
    })
    summary.observe({
        "type": "commandExecution",
        "command": "python3 -c 'print(1)' scripts/check",
        "exitCode": 0,
    })
    summary.observe({
        "type": "commandExecution",
        "command": "pytest --version",
        "exitCode": 0,
    })

    assert summary.to_json() is None


def test_summary_excludes_common_credential_paths_and_bounds_raw_path(tmp_path: Path):
    summary = CodexWorkSummary(str(tmp_path))
    paths = (
        ".aws/credentials",
        ".ssh/id_ed25519",
        ".npmrc",
        "certificates/client.pem",
        "keys/signing.p12",
        ".env.production",
    )
    summary.observe({
        "type": "fileChange",
        "status": "completed",
        "changes": [
            {"path": path, "kind": "update"} for path in paths
        ] + [{"path": "x" * 5000, "kind": "update"}],
    })

    value = json.loads(summary.to_json() or "{}")
    assert value["changed_files"] == []
    assert value["omitted_files"] == len(paths) + 1
    encoded = json.dumps(value)
    assert ".aws" not in encoded
    assert ".ssh" not in encoded
    assert "npmrc" not in encoded
    assert "client.pem" not in encoded
    assert "signing.p12" not in encoded


def test_app_server_bridge_collects_only_completed_work_items(tmp_path: Path):
    summary = CodexWorkSummary(str(tmp_path))
    agent = SimpleNamespace(
        _curator_codex_work_summary=summary,
        _touch_activity=None,
    )
    bridge = make_codex_app_server_event_bridge(agent)
    bridge({"method": "item/started", "params": {"item": {
        "type": "fileChange",
        "id": "change-1",
        "changes": [{"path": "unapplied.py", "kind": {"type": "add"}}],
    }}})
    bridge({"method": "item/completed", "params": {"item": {
        "type": "fileChange",
        "id": "change-2",
        "status": "completed",
        "changes": [{"path": "applied.py", "kind": {"type": "add"}}],
    }}})

    value = json.loads(summary.to_json() or "{}")
    assert value["changed_files"] == [{"path": "applied.py", "change": "add"}]
