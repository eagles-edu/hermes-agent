"""Root-account Host Steward: bounded, read-only inventory and cleanup proposals.

This component deliberately has no approval or cleanup executor. It observes only
explicitly configured paths, stores compact snapshots in the owning Hermes profile,
and emits non-executable proposals for a separate human approval/execution phase.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PLUGIN_ID = "host-steward"
_STATE_KEY = "latest_inventory_v1"
_TARGET_ID = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_ACTION_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MAX_TARGETS = 20
_MAX_ENTRIES = 20_000
_MAX_DEPTH = 12
_ROOT_PATHS = frozenset({
    "/",
    "/boot",
    "/dev",
    "/etc",
    "/home",
    "/opt",
    "/proc",
    "/root",
    "/run",
    "/sys",
    "/tmp",
    "/usr",
    "/var",
})

HOST_INVENTORY = {
    "name": "host_inventory",
    "description": (
        "Read-only, bounded inventory of paths explicitly configured for the Hermes Core "
        "account. Reports logical and allocated bytes, entry limits, and growth since the "
        "last observation. Does not inspect file contents, delete files, or execute commands."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

HOST_PROPOSALS = {
    "name": "host_proposals",
    "description": (
        "List review-only cleanup proposals derived from the latest Host Steward inventory. "
        "Proposals are not approvals and cannot be executed by this plugin."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def _root_account_home(home: Path) -> bool:
    """Do not expose host-wide inventory from a project/profile-specific Hermes home."""
    return not (home.parent.name == "profiles" and home.parent.parent.name == ".hermes")


def _target_specs(raw_targets: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate explicit targets; never accept a path supplied by a model tool call."""
    if not isinstance(raw_targets, list):
        return [], ["targets must be a list"]
    if len(raw_targets) > _MAX_TARGETS:
        return [], [f"targets exceeds the {_MAX_TARGETS}-target limit"]

    valid: list[dict[str, Any]] = []
    issues: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_targets):
        prefix = f"targets[{index}]"
        if not isinstance(item, dict):
            issues.append(f"{prefix} must be an object")
            continue
        target_id = item.get("id")
        path_text = item.get("path")
        action_id = item.get("managed_action_id")
        threshold = item.get("threshold_bytes")
        expected = item.get("expected_reclaim_bytes")
        fields = ("owner", "service_impact", "recovery_path")
        if (
            not isinstance(target_id, str)
            or not _TARGET_ID.fullmatch(target_id)
            or target_id in seen
        ):
            issues.append(f"{prefix}.id must be a unique lowercase identifier")
            continue
        seen.add(target_id)
        if not isinstance(path_text, str) or not Path(path_text).is_absolute():
            issues.append(f"{prefix}.path must be an absolute path")
            continue
        raw_path = Path(os.path.abspath(path_text))
        if str(raw_path) in _ROOT_PATHS:
            issues.append(f"{prefix}.path is too broad to inventory")
            continue
        try:
            # Do not let a configured parent or leaf symlink silently redirect a scan.
            if any(part.is_symlink() for part in (raw_path, *raw_path.parents)):
                issues.append(f"{prefix}.path must not contain symlinks")
                continue
            target_path = raw_path.resolve(strict=True)
        except (OSError, RuntimeError):
            issues.append(f"{prefix}.path does not resolve to an existing target")
            continue
        if str(target_path) in _ROOT_PATHS or target_path == Path.home().resolve():
            issues.append(f"{prefix}.path is too broad to inventory")
            continue
        if not target_path.is_dir():
            issues.append(f"{prefix}.path must be an existing directory")
            continue
        if not isinstance(action_id, str) or not _ACTION_ID.fullmatch(action_id):
            issues.append(
                f"{prefix}.managed_action_id must be a symbolic action identifier"
            )
            continue
        if (
            not isinstance(threshold, int)
            or isinstance(threshold, bool)
            or threshold < 1
        ):
            issues.append(f"{prefix}.threshold_bytes must be a positive integer")
            continue
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            issues.append(
                f"{prefix}.expected_reclaim_bytes must be a non-negative integer"
            )
            continue
        if any(
            not isinstance(item.get(key), str) or not item[key].strip()
            for key in fields
        ):
            issues.append(f"{prefix} requires owner, service_impact, and recovery_path")
            continue
        valid.append({
            "id": target_id,
            "path": target_path,
            "owner": item["owner"].strip()[:120],
            "managed_action_id": action_id,
            "threshold_bytes": threshold,
            "expected_reclaim_bytes": expected,
            "service_impact": item["service_impact"].strip()[:500],
            "recovery_path": item["recovery_path"].strip()[:500],
        })
    return valid, issues


def _scan_directory(root: Path, max_entries: int) -> dict[str, Any]:
    """Measure file metadata only; don't follow symlinks or read contents."""
    logical_bytes = 0
    allocated_bytes = 0
    files = 0
    directories = 0
    errors = 0
    examined = 0
    truncated = False
    depth_limited = False
    stack: list[tuple[Path, int]] = [(root, 0)]

    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    examined += 1
                    if examined > max_entries:
                        truncated = True
                        stack.clear()
                        break
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            directories += 1
                            if depth < _MAX_DEPTH:
                                stack.append((Path(entry.path), depth + 1))
                            else:
                                depth_limited = True
                        elif entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            files += 1
                            logical_bytes += stat.st_size
                            allocated_bytes += (
                                getattr(stat, "st_blocks", 0) * 512 or stat.st_size
                            )
                    except OSError:
                        errors += 1
        except OSError:
            errors += 1

    return {
        "logical_bytes": logical_bytes,
        "allocated_bytes": allocated_bytes,
        "files": files,
        "directories": directories,
        "entries_examined": min(examined, max_entries),
        "entry_limit_reached": truncated,
        "depth_limit_reached": depth_limited,
        "metadata_errors": errors,
    }


def _make_inventory(targets: list[dict[str, Any]], max_entries: int) -> dict[str, Any]:
    observed_at = datetime.now(timezone.utc).isoformat()
    target_results: list[dict[str, Any]] = []
    for target in targets:
        target_results.append({
            "id": target["id"],
            "path": str(target["path"]),
            "owner": target["owner"],
            **_scan_directory(target["path"], max_entries),
        })
    usage = shutil.disk_usage("/")
    return {
        "observed_at": observed_at,
        "filesystem": {
            "path": "/",
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        },
        "targets": target_results,
    }


def _proposals(
    targets: list[dict[str, Any]],
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    previous_by_id = {
        row.get("id"): row
        for row in (previous or {}).get("targets", [])
        if isinstance(row, dict)
    }
    current_by_id = {row["id"]: row for row in current.get("targets", [])}
    results: list[dict[str, Any]] = []
    for target in targets:
        row = current_by_id.get(target["id"])
        if (
            not row
            or row["entry_limit_reached"]
            or row["depth_limit_reached"]
            or row["metadata_errors"]
        ):
            continue
        if row["allocated_bytes"] < target["threshold_bytes"]:
            continue
        prior = previous_by_id.get(target["id"], {})
        if not isinstance(prior, dict) or "allocated_bytes" not in prior:
            continue  # Establish a baseline before attributing growth.
        prior_bytes = prior.get("allocated_bytes")
        if (
            not isinstance(prior_bytes, int)
            or isinstance(prior_bytes, bool)
            or prior_bytes < 0
        ):
            continue
        growth = max(0, row["allocated_bytes"] - prior_bytes)
        if growth == 0:
            continue
        results.append({
            "proposal_id": f"host-{target['id']}-{current['observed_at'].replace(':', '').replace('-', '')}",
            "target_id": target["id"],
            "exact_target": str(target["path"]),
            "owner": target["owner"],
            "observed_bytes": row["allocated_bytes"],
            "growth_since_previous_observation_bytes": growth,
            "expected_reclaimed_bytes": min(
                target["expected_reclaim_bytes"], row["allocated_bytes"]
            ),
            "managed_action_id": target["managed_action_id"],
            "service_impact": target["service_impact"],
            "recovery_path": target["recovery_path"],
            "requires_human_approval": True,
            "executable": False,
            "status": "review_required",
        })
    return results


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def register(ctx: Any) -> None:
    """Expose host inventory only in the non-project Hermes Core account home."""
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home()).resolve()
    if not _root_account_home(home):
        return

    def inventory_handler(args: dict[str, Any] | None = None, **_: Any) -> str:
        targets, issues = _target_specs(ctx.get_config("targets", default=[]))
        try:
            max_entries = int(ctx.get_config("max_entries_per_target", default=20_000))
        except (TypeError, ValueError):
            max_entries = 20_000
        max_entries = min(_MAX_ENTRIES, max(1, max_entries))
        inventory = _make_inventory(targets, max_entries)
        prior_state = ctx.state.get(_STATE_KEY, default={})
        previous = prior_state.get("latest") if isinstance(prior_state, dict) else None
        ctx.state.set(_STATE_KEY, {"previous": previous, "latest": inventory})
        proposals = _proposals(
            targets, inventory, previous if isinstance(previous, dict) else None
        )
        return _json_result({
            "inventory": inventory,
            "proposals": proposals,
            "configuration_issues": issues,
        })

    def proposals_handler(args: dict[str, Any] | None = None, **_: Any) -> str:
        targets, issues = _target_specs(ctx.get_config("targets", default=[]))
        snapshot_state = ctx.state.get(_STATE_KEY, default={})
        if not isinstance(snapshot_state, dict) or not isinstance(
            snapshot_state.get("latest"), dict
        ):
            return _json_result({
                "status": "no_inventory",
                "proposals": [],
                "configuration_issues": issues,
            })
        latest = snapshot_state["latest"]
        previous = snapshot_state.get("previous")
        proposals = _proposals(
            targets, latest, previous if isinstance(previous, dict) else None
        )
        return _json_result({
            "status": "review_required",
            "proposals": proposals,
            "configuration_issues": issues,
            "note": "Listing a proposal is not approval or execution.",
        })

    ctx.register_tool(
        name="host_inventory",
        toolset="host_steward",
        schema=HOST_INVENTORY,
        handler=inventory_handler,
        description="Read-only Hermes Core host inventory",
    )
    ctx.register_tool(
        name="host_proposals",
        toolset="host_steward",
        schema=HOST_PROPOSALS,
        handler=proposals_handler,
        description="Review-only host cleanup proposals",
    )
