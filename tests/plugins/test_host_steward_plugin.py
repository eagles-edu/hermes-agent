"""Behavioral contracts for the root-account Host Steward plugin."""

from __future__ import annotations

import json
from types import SimpleNamespace
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
        "managed_action_id": "empty_configured_cache",
        "service_units": [],
        "service_impact": "Cached data may need to be downloaded again.",
        "recovery_path": "Re-run the owning project's dependency install.",
    }
    return {**result, **overrides}


def _approve_cleanup_call(arguments: dict, monkeypatch, *, tool_call_id: str, choice: str = "once") -> dict:
    """Pass the exact request through Hermes' one-time approval observer path."""
    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
    import tools.approval as approval_module

    def approve(tool_name, reason, *, rule_key, **_kwargs):
        from tools.approval_context import _fire_approval_hook

        _fire_approval_hook(
            "post_approval_response",
            pattern_key=f"plugin_rule:{rule_key}",
            choice=choice,
            surface="test-human-gate",
        )
        return {"approved": True}

    monkeypatch.setattr(approval_module, "request_tool_approval", approve)
    block, modified = _dispatch_pre_tool_call_hooks(
        "host_cleanup", arguments, tool_call_id=tool_call_id
    )
    assert block is None
    return modified


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
    cleanup_entry = registry.get_entry("host_cleanup", scope=manager.scope_key)
    assert inventory_entry is not None
    assert proposal_entry is not None
    assert cleanup_entry is not None
    assert set(cleanup_entry.schema["parameters"]["properties"]) == {"proposal_id"}
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


def test_inventory_labels_filesystem_growth_outside_configured_targets(
    steward_env, tmp_path, monkeypatch
):
    target_dir = tmp_path / "cache"
    target_dir.mkdir()
    (target_dir / "artifact.bin").write_bytes(b"unchanged")
    _enable(steward_env, [_target(target_dir)])
    manager, registry = _load_tools()
    inventory = registry.get_entry("host_inventory", scope=manager.scope_key).handler
    disk_samples = iter([
        SimpleNamespace(total=100_000, used=40_000, free=60_000),
        SimpleNamespace(total=100_000, used=70_000, free=30_000),
    ])
    monkeypatch.setitem(inventory.__globals__, "_disk_usage", lambda _path: next(disk_samples))

    baseline = json.loads(inventory({}))
    current = json.loads(inventory({}))

    assert baseline["inventory"]["growth_attribution"]["status"] == "baseline"
    attribution = current["inventory"]["growth_attribution"]
    assert attribution["scope"] == "configured_targets_only"
    assert attribution["status"] == "measured"
    assert attribution["filesystem_used_growth_bytes"] == 30_000
    assert attribution["configured_owner_growth_bytes"] == []
    assert attribution["unattributed_growth_estimate_bytes"] == 30_000
    assert attribution["configured_target_coverage_complete"] is True


def test_inventory_splits_measured_owner_growth_from_unattributed_remainder(
    steward_env, tmp_path, monkeypatch
):
    target_dir = tmp_path / "cache"
    target_dir.mkdir()
    cached_file = target_dir / "artifact.bin"
    cached_file.write_bytes(b"a" * 512)
    _enable(steward_env, [_target(target_dir)])
    manager, registry = _load_tools()
    inventory = registry.get_entry("host_inventory", scope=manager.scope_key).handler
    disk_samples = iter([
        SimpleNamespace(total=200_000, used=50_000, free=150_000),
        SimpleNamespace(total=200_000, used=150_000, free=50_000),
    ])
    monkeypatch.setitem(inventory.__globals__, "_disk_usage", lambda _path: next(disk_samples))

    baseline = json.loads(inventory({}))["inventory"]
    cached_file.write_bytes(b"b" * 16_384)
    current = json.loads(inventory({}))["inventory"]
    target_growth = (
        current["targets"][0]["allocated_bytes"]
        - baseline["targets"][0]["allocated_bytes"]
    )

    attribution = current["growth_attribution"]
    assert attribution["status"] == "measured"
    assert attribution["configured_owner_growth_bytes"] == [
        {"owner": "test-cache-owner", "growth_bytes": target_growth}
    ]
    assert attribution["filesystem_used_growth_bytes"] == 100_000
    assert attribution["unattributed_growth_estimate_bytes"] == 100_000 - target_growth
    assert attribution["configured_target_coverage_complete"] is True


def test_inventory_with_incomplete_target_scan_does_not_attribute_owner_growth(
    steward_env, tmp_path, monkeypatch
):
    target_dir = tmp_path / "cache"
    target_dir.mkdir()
    cached_file = target_dir / "artifact.bin"
    cached_file.write_bytes(b"a" * 512)
    (target_dir / "second.bin").write_bytes(b"another file")
    _enable(steward_env, [_target(target_dir)], max_entries=1)
    manager, registry = _load_tools()
    inventory = registry.get_entry("host_inventory", scope=manager.scope_key).handler
    disk_samples = iter([
        SimpleNamespace(total=200_000, used=50_000, free=150_000),
        SimpleNamespace(total=200_000, used=70_000, free=130_000),
    ])
    monkeypatch.setitem(inventory.__globals__, "_disk_usage", lambda _path: next(disk_samples))

    inventory({})
    cached_file.write_bytes(b"b" * 16_384)
    current = json.loads(inventory({}))["inventory"]

    attribution = current["growth_attribution"]
    assert attribution["status"] == "partial"
    assert attribution["configured_owner_growth_bytes"] == []
    assert attribution["unattributed_growth_estimate_bytes"] == 20_000
    assert attribution["configured_target_coverage_complete"] is False


def test_cleanup_requires_exact_one_time_approval_and_writes_receipt(
    steward_env, tmp_path, monkeypatch
):
    target_dir = tmp_path / "cache"
    nested = target_dir / "nested"
    nested.mkdir(parents=True)
    cached_file = nested / "artifact.bin"
    cached_file.write_bytes(b"x" * 4096)
    _enable(steward_env, [_target(target_dir, service_units=["hermes-agent.service"])])
    manager, registry = _load_tools()
    inventory = registry.get_entry("host_inventory", scope=manager.scope_key).handler
    proposals = registry.get_entry("host_proposals", scope=manager.scope_key).handler
    cleanup = registry.get_entry("host_cleanup", scope=manager.scope_key).handler

    inventory({})  # baseline
    cached_file.write_bytes(b"y" * 8192)
    inventory({})
    proposal = json.loads(proposals({}))["proposals"][0]
    arguments = {"proposal_id": proposal["proposal_id"]}

    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
    import tools.approval as approval_module

    # A globally bypassed/remembered approval has no one-time observer receipt.
    monkeypatch.setattr(approval_module, "request_tool_approval", lambda *a, **k: {"approved": True})
    block, modified = _dispatch_pre_tool_call_hooks(
        "host_cleanup", arguments, tool_call_id="call-bypassed"
    )
    assert block is None
    blocked = json.loads(cleanup(modified))
    assert blocked["status"] == "blocked"
    assert cached_file.read_bytes() == b"y" * 8192

    def approve_once(tool_name, reason, *, rule_key, **_kwargs):
        from tools.approval_context import _fire_approval_hook
        _fire_approval_hook(
            "post_approval_response",
            pattern_key=f"plugin_rule:{rule_key}",
            choice="once",
            surface="test-human-gate",
        )
        return {"approved": True}

    monkeypatch.setattr(approval_module, "request_tool_approval", approve_once)
    plugin_globals = cleanup.__globals__
    monkeypatch.setitem(
        plugin_globals,
        "_service_health",
        lambda units: {"status": "healthy", "services": [{"unit": unit, "active": True} for unit in units]},
    )
    disk_samples = iter([
        SimpleNamespace(total=10_000, used=9_000, free=1_000),
        SimpleNamespace(total=10_000, used=8_000, free=2_000),
    ])
    monkeypatch.setitem(plugin_globals, "_disk_usage", lambda _path: next(disk_samples))
    block, modified = _dispatch_pre_tool_call_hooks(
        "host_cleanup", arguments, tool_call_id="call-once"
    )
    assert block is None
    result = json.loads(cleanup(modified))
    assert "cleanup_receipt" in result, result
    receipt = result["cleanup_receipt"]
    assert receipt["status"] == "completed", receipt
    assert "free_bytes_after" in receipt
    assert receipt["decision"] == "approved_once"
    assert receipt["files_removed"] == 1
    assert receipt["directories_removed"] == 1
    assert not nested.exists()
    assert target_dir.is_dir()
    receipts = json.loads(
        registry.get_entry("host_cleanup_receipts", scope=manager.scope_key).handler({})
    )
    assert receipts["retained_count"] == 1
    assert receipts["receipts"][0]["decision"] == "approved_once"
    replayed = json.loads(cleanup(modified))
    assert replayed["status"] == "blocked"
    assert not target_dir.joinpath("nested").exists()


def test_rejected_or_broad_approval_never_cleans(steward_env, tmp_path, monkeypatch):
    target_dir = tmp_path / "cache"
    target_dir.mkdir()
    cached_file = target_dir / "artifact.bin"
    cached_file.write_bytes(b"x" * 4096)
    _enable(steward_env, [_target(target_dir)])
    manager, registry = _load_tools()
    inventory = registry.get_entry("host_inventory", scope=manager.scope_key).handler
    proposals = registry.get_entry("host_proposals", scope=manager.scope_key).handler
    cleanup = registry.get_entry("host_cleanup", scope=manager.scope_key).handler

    inventory({})
    cached_file.write_bytes(b"y" * 8192)
    inventory({})
    proposal = json.loads(proposals({}))["proposals"][0]
    arguments = {"proposal_id": proposal["proposal_id"]}
    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
    import tools.approval as approval_module
    def approve_always(tool_name, reason, *, rule_key, **_kwargs):
        from tools.approval_context import _fire_approval_hook
        _fire_approval_hook("post_approval_response", pattern_key=f"plugin_rule:{rule_key}",
                            choice="always", surface="test-human-gate")
        return {"approved": True}
    monkeypatch.setattr(approval_module, "request_tool_approval", approve_always)
    block, modified = _dispatch_pre_tool_call_hooks(
        "host_cleanup", arguments, tool_call_id="call-broad"
    )
    assert block is None

    result = json.loads(cleanup(modified))
    assert result["status"] == "blocked"
    assert cached_file.read_bytes() == b"y" * 8192


def test_cleanup_refuses_target_changed_after_proposal(steward_env, tmp_path, monkeypatch):
    target_dir = tmp_path / "cache"
    target_dir.mkdir()
    cached_file = target_dir / "artifact.bin"
    cached_file.write_bytes(b"x" * 4096)
    _enable(steward_env, [_target(target_dir)])
    manager, registry = _load_tools()
    inventory = registry.get_entry("host_inventory", scope=manager.scope_key).handler
    proposals = registry.get_entry("host_proposals", scope=manager.scope_key).handler
    cleanup = registry.get_entry("host_cleanup", scope=manager.scope_key).handler

    inventory({})
    cached_file.write_bytes(b"y" * 8192)
    inventory({})
    proposal = json.loads(proposals({}))["proposals"][0]
    arguments = {"proposal_id": proposal["proposal_id"]}
    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
    import tools.approval as approval_module
    def approve_once(tool_name, reason, *, rule_key, **_kwargs):
        from tools.approval_context import _fire_approval_hook
        _fire_approval_hook("post_approval_response", pattern_key=f"plugin_rule:{rule_key}",
                            choice="once", surface="test-human-gate")
        return {"approved": True}
    monkeypatch.setattr(approval_module, "request_tool_approval", approve_once)
    block, modified = _dispatch_pre_tool_call_hooks(
        "host_cleanup", arguments, tool_call_id="call-stale"
    )
    assert block is None
    cached_file.write_bytes(b"changed after review")

    result = json.loads(cleanup(modified))
    assert result["status"] == "blocked"
    assert cached_file.read_bytes() == b"changed after review"


def test_cleanup_refuses_symlink_added_after_approval(steward_env, tmp_path, monkeypatch):
    target_dir = tmp_path / "cache"
    target_dir.mkdir()
    cached_file = target_dir / "artifact.bin"
    cached_file.write_bytes(b"a" * 512)
    outside_file = tmp_path / "outside.bin"
    outside_file.write_bytes(b"must remain untouched")
    _enable(steward_env, [_target(target_dir)])
    manager, registry = _load_tools()
    inventory = registry.get_entry("host_inventory", scope=manager.scope_key).handler
    proposals = registry.get_entry("host_proposals", scope=manager.scope_key).handler
    cleanup = registry.get_entry("host_cleanup", scope=manager.scope_key).handler

    inventory({})
    cached_file.write_bytes(b"b" * 8192)
    inventory({})
    proposal = json.loads(proposals({}))["proposals"][0]
    modified = _approve_cleanup_call(
        {"proposal_id": proposal["proposal_id"]},
        monkeypatch,
        tool_call_id="call-symlink-stale",
    )
    cached_file.unlink()
    cached_file.symlink_to(outside_file)

    result = json.loads(cleanup(modified))
    assert result["status"] == "blocked"
    assert outside_file.read_bytes() == b"must remain untouched"
    assert cached_file.is_symlink()
    receipts = json.loads(
        registry.get_entry("host_cleanup_receipts", scope=manager.scope_key).handler({})
    )
    assert receipts["receipts"][-1]["execution_status"] == "blocked_stale_inventory"


def test_cleanup_does_not_run_when_configured_service_is_unhealthy(
    steward_env, tmp_path, monkeypatch
):
    target_dir = tmp_path / "cache"
    target_dir.mkdir()
    cached_file = target_dir / "artifact.bin"
    cached_file.write_bytes(b"a" * 512)
    _enable(steward_env, [_target(target_dir, service_units=["hermes-agent.service"])])
    manager, registry = _load_tools()
    inventory = registry.get_entry("host_inventory", scope=manager.scope_key).handler
    proposals = registry.get_entry("host_proposals", scope=manager.scope_key).handler
    cleanup_entry = registry.get_entry("host_cleanup", scope=manager.scope_key)
    assert cleanup_entry is not None

    inventory({})
    cached_file.write_bytes(b"b" * 8192)
    inventory({})
    proposal = json.loads(proposals({}))["proposals"][0]
    modified = _approve_cleanup_call(
        {"proposal_id": proposal["proposal_id"]},
        monkeypatch,
        tool_call_id="call-unhealthy-service",
    )
    monkeypatch.setitem(
        cleanup_entry.handler.__globals__,
        "_service_health",
        lambda units: {
            "status": "unhealthy",
            "services": [{"unit": unit, "active": False} for unit in units],
        },
    )

    result = json.loads(cleanup_entry.handler(modified))
    assert result["status"] == "blocked"
    assert cached_file.read_bytes() == b"b" * 8192
    receipts = json.loads(
        registry.get_entry("host_cleanup_receipts", scope=manager.scope_key).handler({})
    )
    assert receipts["receipts"][-1]["execution_status"] == "blocked_service_unhealthy"


def test_cleanup_without_service_probe_is_not_reported_as_verified(
    steward_env, tmp_path, monkeypatch
):
    target_dir = tmp_path / "cache"
    target_dir.mkdir()
    cached_file = target_dir / "artifact.bin"
    cached_file.write_bytes(b"a" * 512)
    _enable(steward_env, [_target(target_dir)])
    manager, registry = _load_tools()
    inventory = registry.get_entry("host_inventory", scope=manager.scope_key).handler
    proposals = registry.get_entry("host_proposals", scope=manager.scope_key).handler
    cleanup_entry = registry.get_entry("host_cleanup", scope=manager.scope_key)
    assert cleanup_entry is not None

    inventory({})
    cached_file.write_bytes(b"b" * 8192)
    inventory({})
    proposal = json.loads(proposals({}))["proposals"][0]
    modified = _approve_cleanup_call(
        {"proposal_id": proposal["proposal_id"]},
        monkeypatch,
        tool_call_id="call-no-service-probe",
    )
    disk_samples = iter([
        SimpleNamespace(total=10_000, used=9_000, free=1_000),
        SimpleNamespace(total=10_000, used=8_000, free=2_000),
    ])
    monkeypatch.setitem(
        cleanup_entry.handler.__globals__, "_disk_usage", lambda _path: next(disk_samples)
    )

    result = json.loads(cleanup_entry.handler(modified))
    receipt = result["cleanup_receipt"]
    assert receipt["status"] == "verification_incomplete"
    assert receipt["service_health_after"]["status"] == "not_configured"
    assert not cached_file.exists()


def test_partial_cleanup_failure_leaves_a_bounded_recovery_receipt(
    steward_env, tmp_path, monkeypatch
):
    target_dir = tmp_path / "cache"
    target_dir.mkdir()
    cached_file = target_dir / "artifact.bin"
    cached_file.write_bytes(b"a" * 512)
    _enable(steward_env, [_target(target_dir)])
    manager, registry = _load_tools()
    inventory = registry.get_entry("host_inventory", scope=manager.scope_key).handler
    proposals = registry.get_entry("host_proposals", scope=manager.scope_key).handler
    cleanup_entry = registry.get_entry("host_cleanup", scope=manager.scope_key)
    assert cleanup_entry is not None

    inventory({})
    cached_file.write_bytes(b"b" * 8192)
    inventory({})
    proposal = json.loads(proposals({}))["proposals"][0]
    modified = _approve_cleanup_call(
        {"proposal_id": proposal["proposal_id"]},
        monkeypatch,
        tool_call_id="call-partial-cleanup",
    )

    def delete_then_fail(root: Path, _manifest: str, _max_entries: int) -> dict:
        (root / "artifact.bin").unlink()
        raise OSError("private filesystem detail")

    monkeypatch.setitem(
        cleanup_entry.handler.__globals__, "_empty_configured_cache", delete_then_fail
    )
    result = json.loads(cleanup_entry.handler(modified))
    receipt = result["cleanup_receipt"]
    assert receipt["status"] == "partial_or_failed"
    assert receipt["error_type"] == "OSError"
    assert "private filesystem detail" not in json.dumps(result)
    assert receipt["recovery_path"] == "Re-run the owning project's dependency install."
    assert not cached_file.exists()
    saved = json.loads(
        registry.get_entry("host_cleanup_receipts", scope=manager.scope_key).handler({})
    )
    assert saved["receipts"][-1]["status"] == "partial_or_failed"


def test_interrupted_cleanup_reconcile_never_retries_deletion(
    steward_env, tmp_path, monkeypatch
):
    target_dir = tmp_path / "cache"
    target_dir.mkdir()
    cached_file = target_dir / "artifact.bin"
    cached_file.write_bytes(b"a" * 512)
    _enable(steward_env, [_target(target_dir)])
    manager, registry = _load_tools()
    inventory = registry.get_entry("host_inventory", scope=manager.scope_key).handler
    proposals = registry.get_entry("host_proposals", scope=manager.scope_key).handler
    cleanup_entry = registry.get_entry("host_cleanup", scope=manager.scope_key)
    receipts_entry = registry.get_entry("host_cleanup_receipts", scope=manager.scope_key)
    reconcile_entry = registry.get_entry("host_cleanup_reconcile", scope=manager.scope_key)
    assert cleanup_entry is not None
    assert receipts_entry is not None
    assert reconcile_entry is not None

    inventory({})
    cached_file.write_bytes(b"b" * 8192)
    inventory({})
    proposal = json.loads(proposals({}))["proposals"][0]
    modified = _approve_cleanup_call(
        {"proposal_id": proposal["proposal_id"]},
        monkeypatch,
        tool_call_id="call-crash-cleanup",
    )
    attempts = 0

    def delete_then_crash(root: Path, _manifest: str, _max_entries: int) -> dict:
        nonlocal attempts
        attempts += 1
        (root / "artifact.bin").unlink()
        raise SystemExit("simulated process interruption after deletion")

    monkeypatch.setitem(
        cleanup_entry.handler.__globals__, "_empty_configured_cache", delete_then_crash
    )
    with pytest.raises(SystemExit):
        cleanup_entry.handler(modified)
    running = json.loads(receipts_entry.handler({}))["receipts"][-1]
    assert running["status"] == "running"
    assert not cached_file.exists()

    assert json.loads(reconcile_entry.handler({
        "receipt_id": running["receipt_id"],
        "confirm_process_stopped": False,
    }))["status"] == "blocked"
    reconciled = json.loads(reconcile_entry.handler({
        "receipt_id": running["receipt_id"],
        "confirm_process_stopped": True,
    }))
    assert reconciled["status"] == "reconciled"
    assert reconciled["cleanup_receipt"]["status"] == "interrupted_requires_review"
    assert "fresh inventory" in reconciled["cleanup_receipt"]["recovery_path"]
    assert attempts == 1
    replay = json.loads(reconcile_entry.handler({
        "receipt_id": running["receipt_id"],
        "confirm_process_stopped": True,
    }))
    assert replay["status"] == "not_running"
    assert attempts == 1


def test_service_health_uses_fixed_systemctl_probe(steward_env, tmp_path, monkeypatch):
    target_dir = tmp_path / "cache"
    target_dir.mkdir()
    _enable(steward_env, [_target(target_dir)])
    manager, registry = _load_tools()
    globals_map = registry.get_entry("host_inventory", scope=manager.scope_key).handler.__globals__
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(globals_map["subprocess"], "run", run)
    result = globals_map["_service_health"](["hermes-agent.service"])
    assert result == {
        "status": "healthy",
        "services": [{"unit": "hermes-agent.service", "active": True}],
    }
    assert calls == [(
        ["systemctl", "is-active", "--quiet", "hermes-agent.service"],
        {"check": False, "capture_output": True, "timeout": 5},
    )]
    assert globals_map["_service_health"]([])["status"] == "not_configured"


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
    assert registry.get_entry("host_cleanup", scope=manager.scope_key) is None
    assert registry.get_entry("host_cleanup_receipts", scope=manager.scope_key) is None
    assert registry.get_entry("host_cleanup_reconcile", scope=manager.scope_key) is None
