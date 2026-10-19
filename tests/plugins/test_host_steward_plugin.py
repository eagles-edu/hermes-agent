"""Behavioral contracts for the root-account Host Steward plugin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import hermes_yaml as yaml


@pytest.fixture
def steward_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _enable(home: Path, targets: list[dict], *, max_entries: int = 20_000) -> None:
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "plugins": {
                "enabled": ["host-steward"],
                "entries": {
                    "host-steward": {
                        "settings": {
                            "targets": targets,
                            "max_entries_per_target": max_entries,
                        }
                    }
                },
            },
        }),
        encoding="utf-8",
    )


def _load_tools():
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    manager = PluginManager()
    manager.discover_and_load()
    return manager, registry


def _target(path: Path, **overrides) -> dict:
    result = {
        "id": "cache-a",
        "path": str(path.resolve()),
        "owner": "test-cache-owner",
        "threshold_bytes": 1,
        "expected_reclaim_bytes": 128,
        "managed_action_id": "test_cache_prune",
        "service_impact": "Cached data may need to be downloaded again.",
        "recovery_path": "Re-run the owning project's dependency install.",
    }
    return {**result, **overrides}


def test_inventory_and_proposal_are_observational_and_never_executable(
    steward_env, tmp_path, monkeypatch
):
    target_dir = tmp_path / "cache"
    target_dir.mkdir()
    cached_file = target_dir / "artifact.bin"
    cached_file.write_bytes(b"a" * 512)
    _enable(steward_env, [_target(target_dir)])
    manager, registry = _load_tools()

    inventory_entry = registry.get_entry("host_inventory", scope=manager.scope_key)
    proposal_entry = registry.get_entry("host_proposals", scope=manager.scope_key)
    assert inventory_entry is not None
    assert proposal_entry is not None
    inventory = json.loads(inventory_entry.handler({}))
    assert inventory["inventory"]["targets"][0]["logical_bytes"] == 512
    assert inventory["proposals"] == []

    cached_file.write_bytes(b"b" * 16_384)
    second_inventory = json.loads(inventory_entry.handler({}))
    assert second_inventory["inventory"]["targets"][0]["logical_bytes"] == 16_384
    proposals = json.loads(proposal_entry.handler({}))
    proposal = proposals["proposals"][0]
    assert proposal["exact_target"] == str(target_dir.resolve())
    assert proposal["growth_since_previous_observation_bytes"] > 0
    assert proposal["expected_reclaimed_bytes"] <= proposal["observed_bytes"]
    assert proposal["requires_human_approval"] is True
    assert proposal["executable"] is False
    assert proposal["status"] == "review_required"
    assert cached_file.read_bytes() == b"b" * 16_384
    assert registry.get_entry("host_approve", scope=manager.scope_key) is None
    assert registry.get_entry("host_execute", scope=manager.scope_key) is None


def test_inventory_does_not_follow_nested_symlinks(steward_env, tmp_path):
    target_dir = tmp_path / "cache"
    outside = tmp_path / "outside"
    target_dir.mkdir()
    outside.mkdir()
    (target_dir / "inside.bin").write_bytes(b"inside")
    (outside / "secret-sized.bin").write_bytes(b"x" * 8192)
    (target_dir / "linked").symlink_to(outside, target_is_directory=True)
    _enable(steward_env, [_target(target_dir)])
    manager, registry = _load_tools()

    result = json.loads(
        registry.get_entry("host_inventory", scope=manager.scope_key).handler({})
    )
    row = result["inventory"]["targets"][0]
    assert row["logical_bytes"] == len(b"inside")
    assert row["files"] == 1


def test_entry_limit_marks_inventory_incomplete_and_suppresses_proposals(
    steward_env, tmp_path
):
    target_dir = tmp_path / "cache"
    target_dir.mkdir()
    for index in range(4):
        (target_dir / f"{index}.bin").write_bytes(b"x" * 10)
    _enable(steward_env, [_target(target_dir)], max_entries=2)
    manager, registry = _load_tools()

    result = json.loads(
        registry.get_entry("host_inventory", scope=manager.scope_key).handler({})
    )
    row = result["inventory"]["targets"][0]
    assert row["entry_limit_reached"] is True
    assert result["proposals"] == []


def test_depth_limit_marks_inventory_incomplete_and_suppresses_proposals(
    steward_env, tmp_path
):
    target_dir = tmp_path / "cache"
    deep_dir = target_dir
    for index in range(14):
        deep_dir = deep_dir / f"level-{index}"
    deep_dir.mkdir(parents=True)
    (deep_dir / "artifact.bin").write_bytes(b"x" * 100)
    _enable(steward_env, [_target(target_dir)])
    manager, registry = _load_tools()

    result = json.loads(
        registry.get_entry("host_inventory", scope=manager.scope_key).handler({})
    )
    row = result["inventory"]["targets"][0]
    assert row["depth_limit_reached"] is True
    assert result["proposals"] == []


def test_broad_path_is_rejected_without_scanning_it(steward_env):
    _enable(steward_env, [_target(Path("/home"))])
    manager, registry = _load_tools()

    result = json.loads(
        registry.get_entry("host_inventory", scope=manager.scope_key).handler({})
    )
    assert result["inventory"]["targets"] == []
    assert any("too broad" in item for item in result["configuration_issues"])


def test_project_profile_does_not_register_host_steward_tools(tmp_path, monkeypatch):
    project_home = tmp_path / ".hermes" / "profiles" / "gptmd-coding"
    project_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(project_home))
    _enable(project_home, [])
    manager, registry = _load_tools()

    assert registry.get_entry("host_inventory", scope=manager.scope_key) is None
    assert registry.get_entry("host_proposals", scope=manager.scope_key) is None
