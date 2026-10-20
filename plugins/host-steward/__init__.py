"""Root-account Host Steward: bounded inventory and approval-gated managed cleanup.

The plugin observes only explicitly configured paths, stores compact snapshots and
receipts in its owning Hermes Core profile, and executes only registered actions
after a fresh one-time Hermes approval.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import threading
import secrets
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PLUGIN_ID = "host-steward"
_STATE_KEY = "latest_inventory_v1"
_APPROVAL_STATE_KEY = "cleanup_approvals_v1"
_RECEIPTS_STATE_KEY = "cleanup_receipts_v1"
_TARGET_ID = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_ACTION_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SERVICE_UNIT = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}\.service$")
_MAX_TARGETS = 20
_MAX_ENTRIES = 20_000
_MAX_DEPTH = 12
_MAX_RECEIPTS = 200
_STATE_LOCK = threading.RLock()
_APPROVAL_RULE_PREFIX = "host-steward-cleanup:"
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
    # no-tmp: ok — explicit denial entry prevents choosing the host temporary directory as a target.
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
        "A proposal is not approval; only the separate exact-target cleanup tool can consume it."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

HOST_CLEANUP = {
    "name": "host_cleanup",
    "description": (
        "Execute one exact, configured Host Steward cache-cleanup proposal. Hermes requires a "
        "fresh human approval for this proposal; the handler rejects missing, stale, reused, or "
        "non-human approvals. It never accepts a path or shell command."
    ),
    "parameters": {
        "type": "object",
        "properties": {"proposal_id": {"type": "string", "minLength": 1, "maxLength": 100}},
        "required": ["proposal_id"],
        "additionalProperties": False,
    },
}

HOST_CLEANUP_RECEIPTS = {
    "name": "host_cleanup_receipts",
    "description": "Read the bounded Hermes Core Host Steward cleanup decision and verification receipts.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

HOST_CLEANUP_RECONCILE = {
    "name": "host_cleanup_reconcile",
    "description": (
        "Reconcile a Host Steward receipt left running after a confirmed Hermes process stop. "
        "This records an interrupted outcome only; it never resumes or repeats cleanup. "
        "Use only after the operator confirms the original process is no longer active."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "receipt_id": {"type": "string", "minLength": 1, "maxLength": 100},
            "confirm_process_stopped": {"type": "boolean"},
        },
        "required": ["receipt_id", "confirm_process_stopped"],
        "additionalProperties": False,
    },
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
        service_units = item.get("service_units", [])
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
        if action_id != "empty_configured_cache":
            issues.append(
                f"{prefix}.managed_action_id is not a registered safe action"
            )
            continue
        if (
            not isinstance(service_units, list)
            or len(service_units) > 20
            or any(not isinstance(unit, str) or not _SERVICE_UNIT.fullmatch(unit)
                   for unit in service_units)
            or len(set(service_units)) != len(service_units)
        ):
            issues.append(f"{prefix}.service_units must be a list of unique systemd service names")
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
            "service_units": service_units,
            "threshold_bytes": threshold,
            "expected_reclaim_bytes": expected,
            "service_impact": item["service_impact"].strip()[:500],
            "recovery_path": item["recovery_path"].strip()[:500],
        })
    return valid, issues


def _scan_directory(root: Path, max_entries: int, *, include_manifest: bool = False) -> dict[str, Any]:
    """Measure file metadata only; don't follow symlinks or read contents."""
    logical_bytes = 0
    allocated_bytes = 0
    files = 0
    directories = 0
    errors = 0
    examined = 0
    truncated = False
    depth_limited = False
    unsafe_entries = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    manifest: list[dict[str, Any]] = []

    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in sorted(entries, key=lambda row: row.name):
                    examined += 1
                    if examined > max_entries:
                        truncated = True
                        stack.clear()
                        break
                    try:
                        entry_kind, item_stat = _entry_kind(entry)
                    except OSError:
                        errors += 1
                        continue
                    if entry_kind == "unsafe":
                        unsafe_entries += 1
                        continue
                    if item_stat is None:
                        errors += 1
                        continue
                    if entry_kind == "dir":
                        directories += 1
                        manifest.append(_manifest_row(root, Path(entry.path), item_stat, "dir"))
                        if depth < _MAX_DEPTH:
                            stack.append((Path(entry.path), depth + 1))
                        else:
                            depth_limited = True
                    else:
                        files += 1
                        logical_bytes += item_stat.st_size
                        allocated_bytes += (
                            getattr(item_stat, "st_blocks", 0) * 512 or item_stat.st_size
                        )
                        manifest.append(_manifest_row(root, Path(entry.path), item_stat, "file"))
        except OSError:
            errors += 1

    result = {
        "logical_bytes": logical_bytes,
        "allocated_bytes": allocated_bytes,
        "files": files,
        "directories": directories,
        "entries_examined": min(examined, max_entries),
        "entry_limit_reached": truncated,
        "depth_limit_reached": depth_limited,
        "metadata_errors": errors,
        "unsafe_entries": unsafe_entries,
        "manifest_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    if include_manifest:
        result["_manifest_entries"] = manifest
    return result


def _entry_kind(entry: os.DirEntry) -> tuple[str, os.stat_result | None]:
    """Classify one entry with no symlink following and return its metadata."""
    if entry.is_symlink():
        return "unsafe", None
    if entry.is_dir(follow_symlinks=False):
        return "dir", entry.stat(follow_symlinks=False)
    if entry.is_file(follow_symlinks=False):
        return "file", entry.stat(follow_symlinks=False)
    return "unsafe", None


def _manifest_row(root: Path, path: Path, metadata: os.stat_result, kind: str) -> dict[str, Any]:
    return _manifest_metadata(path.relative_to(root).as_posix(), metadata, kind)


def _manifest_metadata(relative_path: str, metadata: os.stat_result, kind: str) -> dict[str, Any]:
    return {
        "path": relative_path,
        "kind": kind,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": metadata.st_mode,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
    }


def _make_inventory(
    targets: list[dict[str, Any]],
    max_entries: int,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observed_at = datetime.now(UTC).isoformat()
    target_results: list[dict[str, Any]] = []
    for target in targets:
        target_results.append({
            "id": target["id"],
            "path": str(target["path"]),
            "owner": target["owner"],
            **_scan_directory(target["path"], max_entries),
        })
    usage = _disk_usage("/")
    inventory = {
        "observed_at": observed_at,
        "filesystem": {
            "path": "/",
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        },
        "targets": target_results,
    }
    inventory["growth_attribution"] = _growth_attribution(inventory, previous)
    return inventory


def _complete_scan(row: dict[str, Any]) -> bool:
    """Return whether one target's metadata scan can support attribution."""
    return (
        row.get("entry_limit_reached") is False
        and row.get("depth_limit_reached") is False
        and row.get("metadata_errors") == 0
        and row.get("unsafe_entries") == 0
    )


def _valid_size(value: Any) -> bool:
    """Accept a non-negative byte count, excluding booleans."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _target_owner_growth(
    current_rows: list[Any], previous_rows: list[Any]
) -> tuple[dict[str, int], bool]:
    """Compare only complete, unchanged target bindings and aggregate by owner."""
    current_by_id = {
        row.get("id"): row for row in current_rows if isinstance(row, dict)
    }
    previous_by_id = {
        row.get("id"): row for row in previous_rows if isinstance(row, dict)
    }
    complete = (
        len(current_by_id) == len(current_rows)
        and len(previous_by_id) == len(previous_rows)
        and current_by_id.keys() == previous_by_id.keys()
    )
    owners: dict[str, int] = {}
    paths: list[Path] = []
    for target_id, row in current_by_id.items():
        prior = previous_by_id.get(target_id)
        if not isinstance(prior, dict):
            complete = False
            continue
        path = row.get("path")
        owner = row.get("owner")
        if not isinstance(path, str) or not isinstance(owner, str):
            complete = False
            continue
        target_path = Path(path)
        if any(target_path == seen or target_path in seen.parents or seen in target_path.parents for seen in paths):
            complete = False
        paths.append(target_path)
        if path != prior.get("path") or owner != prior.get("owner"):
            complete = False
            continue
        current_bytes = row.get("allocated_bytes")
        previous_bytes = prior.get("allocated_bytes")
        if (
            not _complete_scan(row)
            or not _complete_scan(prior)
            or not _valid_size(current_bytes)
            or not _valid_size(previous_bytes)
        ):
            complete = False
            continue
        growth = max(0, current_bytes - previous_bytes)
        if growth:
            owners[owner] = owners.get(owner, 0) + growth
    if not complete:
        return {}, False
    return owners, True


def _growth_attribution(
    current: dict[str, Any], previous: dict[str, Any] | None
) -> dict[str, Any]:
    """Compare filesystem usage with complete, configured-owner target deltas."""
    base = {
        "scope": "configured_targets_only",
        "configured_owner_growth_bytes": [],
        "configured_target_coverage_complete": False,
    }
    if previous is None:
        return {
            **base,
            "status": "baseline",
            "filesystem_used_growth_bytes": None,
            "unattributed_growth_estimate_bytes": None,
        }

    current_filesystem = current.get("filesystem")
    previous_filesystem = previous.get("filesystem")
    if not isinstance(current_filesystem, dict) or not isinstance(previous_filesystem, dict):
        return {
            **base,
            "status": "incomplete",
            "filesystem_used_growth_bytes": None,
            "unattributed_growth_estimate_bytes": None,
        }
    current_used = current_filesystem.get("used_bytes")
    previous_used = previous_filesystem.get("used_bytes")
    if not _valid_size(current_used) or not _valid_size(previous_used):
        return {
            **base,
            "status": "incomplete",
            "filesystem_used_growth_bytes": None,
            "unattributed_growth_estimate_bytes": None,
        }

    current_rows = current.get("targets")
    previous_rows = previous.get("targets")
    if not isinstance(current_rows, list) or not isinstance(previous_rows, list):
        current_rows, previous_rows = [], []
        coverage_complete = False
        owner_growth: dict[str, int] = {}
    else:
        owner_growth, coverage_complete = _target_owner_growth(current_rows, previous_rows)

    filesystem_growth = max(0, current_used - previous_used)
    attributed_growth = min(filesystem_growth, sum(owner_growth.values()))
    return {
        "scope": "configured_targets_only",
        "status": "measured" if coverage_complete else "partial",
        "filesystem_used_growth_bytes": filesystem_growth,
        "configured_owner_growth_bytes": [
            {"owner": owner, "growth_bytes": growth}
            for owner, growth in sorted(owner_growth.items())
        ],
        "unattributed_growth_estimate_bytes": filesystem_growth - attributed_growth,
        "configured_target_coverage_complete": coverage_complete,
    }


def _disk_usage(path: str | Path):
    return shutil.disk_usage(path)


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
            or row["unsafe_entries"]
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
            "manifest_sha256": row["manifest_sha256"],
            "service_units": target["service_units"],
            "service_impact": target["service_impact"],
            "recovery_path": target["recovery_path"],
            "requires_human_approval": True,
            "executable": False,
            "status": "review_required",
        })
    return results


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _append_receipt(ctx: Any, receipt: dict[str, Any]) -> None:
    with _STATE_LOCK:
        receipts = ctx.state.get(_RECEIPTS_STATE_KEY, default=[])
        if not isinstance(receipts, list):
            receipts = []
        ctx.state.set(_RECEIPTS_STATE_KEY, [*receipts[-(_MAX_RECEIPTS - 1):], receipt])


def _save_receipt(ctx: Any, receipt: dict[str, Any]) -> None:
    with _STATE_LOCK:
        receipts = ctx.state.get(_RECEIPTS_STATE_KEY, default=[])
        if not isinstance(receipts, list):
            receipts = []
        replaced = False
        for index, existing in enumerate(receipts):
            if isinstance(existing, dict) and existing.get("receipt_id") == receipt.get("receipt_id"):
                receipts[index] = receipt
                replaced = True
                break
        if not replaced:
            receipts = [*receipts[-(_MAX_RECEIPTS - 1):], receipt]
        ctx.state.set(_RECEIPTS_STATE_KEY, receipts)


def _service_health(service_units: list[str]) -> dict[str, Any]:
    if not service_units:
        return {"status": "not_configured", "services": []}
    results = []
    for unit in service_units:
        try:
            completed = subprocess.run(
                ["systemctl", "is-active", "--quiet", unit],
                check=False,
                capture_output=True,
                timeout=5,
            )
            active = completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            active = False
        results.append({"unit": unit, "active": active})
    return {"status": "healthy" if all(row["active"] for row in results) else "unhealthy",
            "services": results}


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_mode, left.st_size, left.st_mtime_ns, left.st_ctime_ns) == (
        right.st_dev, right.st_ino, right.st_mode, right.st_size, right.st_mtime_ns, right.st_ctime_ns
    )


def _same_directory_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
        right.st_dev, right.st_ino, stat.S_IFMT(right.st_mode)
    )


def _empty_configured_cache(root: Path, expected_manifest: str, max_entries: int) -> dict[str, Any]:
    """Remove only the unchanged contents of one configured directory, using dir-fd operations."""
    resolved = root.resolve(strict=True)
    if resolved != root or not root.is_dir() or root.is_symlink():
        raise ValueError("configured target changed or became a symlink")
    before = _scan_directory(root, max_entries, include_manifest=True)
    if (before["entry_limit_reached"] or before["depth_limit_reached"]
            or before["metadata_errors"] or before["unsafe_entries"]
            or before["manifest_sha256"] != expected_manifest):
        raise ValueError("configured target changed since the proposal; take a fresh inventory")

    expected_by_path = {row["path"]: row for row in before["_manifest_entries"]}
    examined = 0
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)

    def validate_contents(
        directory_fd: int, *, delete: bool, prefix: str, seen: set[str], depth: int
    ) -> tuple[int, int]:
        nonlocal examined
        if depth > _MAX_DEPTH:
            raise ValueError("target exceeded the cleanup depth limit")
        files_removed = 0
        directories_removed = 0
        with os.scandir(directory_fd) as iterator:
            names = []
            for entry in iterator:
                examined += 1
                if examined > max_entries:
                    raise ValueError("target exceeded the cleanup entry limit")
                names.append(entry.name)
        names.sort()
        for name in names:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(current.st_mode):
                raise ValueError("target changed to contain a symlink; cleanup stopped")
            relative = f"{prefix}/{name}" if prefix else name
            seen.add(relative)
            expected = expected_by_path.get(relative)
            if stat.S_ISDIR(current.st_mode):
                if expected != _manifest_metadata(relative, current, "dir"):
                    raise ValueError("target changed during cleanup; cleanup stopped")
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if not _same_directory_identity(current, opened):
                        raise ValueError("directory changed during cleanup; cleanup stopped")
                    child_files, child_directories = validate_contents(
                        child_fd, delete=delete, prefix=relative, seen=seen, depth=depth + 1
                    )
                    files_removed += child_files
                    directories_removed += child_directories
                finally:
                    os.close(child_fd)
                if delete:
                    latest = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if not _same_directory_identity(current, latest):
                        raise ValueError("directory changed during cleanup; cleanup stopped")
                    os.rmdir(name, dir_fd=directory_fd)
                    directories_removed += 1
            elif stat.S_ISREG(current.st_mode):
                if expected != _manifest_metadata(relative, current, "file"):
                    raise ValueError("target changed during cleanup; cleanup stopped")
                latest = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not _same_file_identity(current, latest):
                    raise ValueError("file changed during cleanup; cleanup stopped")
                if delete:
                    os.unlink(name, dir_fd=directory_fd)
                    files_removed += 1
            else:
                raise ValueError("target contains a special file; cleanup stopped")
        return files_removed, directories_removed

    # Resolve every path component from the filesystem root with O_NOFOLLOW; checking only the
    # final directory would still allow a swapped parent symlink to redirect cleanup.
    root_fd = os.open("/", flags)
    try:
        for component in root.parts[1:]:
            child_fd = os.open(component, flags, dir_fd=root_fd)
            os.close(root_fd)
            root_fd = child_fd
        # Preflight the entire tree before the first unlink. Repeat checks during deletion so new
        # files are rejected rather than silently swept into an already-approved cleanup.
        seen: set[str] = set()
        validate_contents(root_fd, delete=False, prefix="", seen=seen, depth=0)
        if seen != set(expected_by_path):
            raise ValueError("target changed during cleanup; cleanup stopped")
        delete_seen: set[str] = set()
        removed_files, removed_directories = validate_contents(
            root_fd, delete=True, prefix="", seen=delete_seen, depth=0
        )
        if delete_seen != set(expected_by_path):
            raise ValueError("target changed during cleanup; cleanup stopped")
    finally:
        os.close(root_fd)
    return {"files_removed": removed_files, "directories_removed": removed_directories}


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
        prior_state = ctx.state.get(_STATE_KEY, default={})
        previous = prior_state.get("latest") if isinstance(prior_state, dict) else None
        inventory = _make_inventory(
            targets,
            max_entries,
            previous if isinstance(previous, dict) else None,
        )
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

    def get_proposal(proposal_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        targets, _issues = _target_specs(ctx.get_config("targets", default=[]))
        snapshot_state = ctx.state.get(_STATE_KEY, default={})
        if not isinstance(snapshot_state, dict):
            return None, None
        latest, previous = snapshot_state.get("latest"), snapshot_state.get("previous")
        if not isinstance(latest, dict):
            return None, None
        proposals = _proposals(targets, latest, previous if isinstance(previous, dict) else None)
        proposal = next((row for row in proposals if row["proposal_id"] == proposal_id), None)
        target = next((row for row in targets if proposal and row["id"] == proposal["target_id"]), None)
        return proposal, target

    def prepare_cleanup_call(
        tool_name: str = "", args: dict[str, Any] | None = None,
        tool_call_id: str = "", **_: Any,
    ) -> dict[str, Any] | None:
        if tool_name != "host_cleanup":
            return None
        proposal_id = args.get("proposal_id") if isinstance(args, dict) else None
        proposal, _target = get_proposal(proposal_id) if isinstance(proposal_id, str) else (None, None)
        if proposal is None or not tool_call_id:
            return {"action": "block", "message": "Host cleanup is blocked: proposal is missing, stale, or unbound."}
        token = secrets.token_urlsafe(24)
        pending = ctx.state.get(_APPROVAL_STATE_KEY, default={})
        if not isinstance(pending, dict):
            pending = {}
        now = datetime.now(UTC)
        fresh_pending = {}
        for key, value in pending.items():
            try:
                age = (now - datetime.fromisoformat(value["created_at"])).total_seconds()
            except (KeyError, TypeError, ValueError):
                continue
            if isinstance(value, dict) and 0 <= age <= 120:
                fresh_pending[key] = value
        pending = fresh_pending
        pending[tool_call_id] = {
            "token": token,
            "proposal_id": proposal["proposal_id"],
            "manifest_sha256": proposal["manifest_sha256"],
            "created_at": now.isoformat(),
        }
        ctx.state.set(_APPROVAL_STATE_KEY, pending)
        return {"action": "modify", "args": {"_steward_approval_token": token}}

    def require_cleanup_approval(
        tool_name: str = "", args: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any] | None:
        if tool_name != "host_cleanup":
            return None
        proposal_id = args.get("proposal_id") if isinstance(args, dict) else None
        proposal, _target = get_proposal(proposal_id) if isinstance(proposal_id, str) else (None, None)
        if proposal is None:
            return {"action": "block", "message": "Host cleanup is blocked: proposal is missing or stale."}
        message = (
            f"Approve once to empty the contents of {proposal['exact_target']} "
            f"({proposal['observed_bytes']} observed bytes; expected reclaim up to "
            f"{proposal['expected_reclaimed_bytes']} bytes). Impact: {proposal['service_impact']} "
            f"Recovery: {proposal['recovery_path']}"
        )
        return {
            "action": "approve",
            "message": message,
            "rule_key": f"{_APPROVAL_RULE_PREFIX}{proposal['proposal_id']}:{proposal['manifest_sha256']}",
        }

    def approval_observer(
        pattern_key: str = "", choice: str = "", tool_call_id: str = "", surface: str = "unknown",
        **_: Any,
    ) -> None:
        if not pattern_key.startswith(f"plugin_rule:{_APPROVAL_RULE_PREFIX}") or not tool_call_id:
            return
        pending = ctx.state.get(_APPROVAL_STATE_KEY, default={})
        if not isinstance(pending, dict):
            return
        request = pending.get(tool_call_id)
        if not isinstance(request, dict):
            return
        token = request.get("token")
        if not isinstance(token, str):
            return
        approvals = ctx.state.get(_APPROVAL_STATE_KEY, default={})
        if not isinstance(approvals, dict):
            approvals = {}
        approvals.pop(tool_call_id, None)
        approvals[token] = {
            **request,
            "choice": choice,
            "approved_once": choice == "once",
            "surface": surface,
            "decided_at": datetime.now(UTC).isoformat(),
        }
        ctx.state.set(_APPROVAL_STATE_KEY, approvals)
        _append_receipt(ctx, {
            "receipt_id": token,
            "proposal_id": request["proposal_id"],
            "decision": "approved_once" if choice == "once" else "not_approved_once",
            "approval_choice": choice[:40] if isinstance(choice, str) else "unknown",
            "approval_surface": surface[:40] if isinstance(surface, str) else "unknown",
            "approver_identity": "not-exposed-by-hermes-approval-api",
            "recorded_at": datetime.now(UTC).isoformat(),
        })

    def cleanup_handler(args: dict[str, Any] | None = None, **_: Any) -> str:
        args = args or {}
        proposal_id = args.get("proposal_id")
        token = args.get("_steward_approval_token")
        if not isinstance(proposal_id, str) or not isinstance(token, str):
            return _json_result({"status": "blocked", "reason": "fresh human approval is required"})
        with _STATE_LOCK:
            approvals = ctx.state.get(_APPROVAL_STATE_KEY, default={})
            if not isinstance(approvals, dict):
                approvals = {}
            grant = approvals.get(token)
            if (not isinstance(grant, dict) or grant.get("proposal_id") != proposal_id
                    or grant.get("approved_once") is not True
                    or grant.get("manifest_sha256") is None):
                return _json_result({"status": "blocked", "reason": "no matching one-time human approval receipt"})
            # Consume while holding the lock so duplicate/concurrent calls cannot both execute.
            approvals.pop(token, None)
            ctx.state.set(_APPROVAL_STATE_KEY, approvals)
        try:
            age_seconds = (datetime.now(UTC) - datetime.fromisoformat(
                grant["decided_at"]
            )).total_seconds()
        except (KeyError, TypeError, ValueError):
            age_seconds = _MAX_ENTRIES + 1
        if age_seconds < 0 or age_seconds > 120:
            _save_receipt(ctx, {
                "receipt_id": token,
                "proposal_id": proposal_id,
                "decision": "approved_once",
                "execution_status": "blocked_expired",
                "reason": "approval expired; request a fresh approval",
                "recorded_at": datetime.now(UTC).isoformat(),
            })
            return _json_result({"status": "blocked", "reason": "approval expired; request a fresh approval"})
        proposal, target = get_proposal(proposal_id)
        if (proposal is None or target is None
                or proposal["manifest_sha256"] != grant["manifest_sha256"]):
            _save_receipt(ctx, {
                "receipt_id": token,
                "proposal_id": proposal_id,
                "decision": "approved_once",
                "execution_status": "blocked_stale_proposal",
                "reason": "proposal changed; take a fresh inventory",
                "recorded_at": datetime.now(UTC).isoformat(),
            })
            return _json_result({"status": "blocked", "reason": "proposal changed; take a fresh inventory"})
        current_scan = _scan_directory(target["path"], min(_MAX_ENTRIES, max(1, int(ctx.get_config(
            "max_entries_per_target", default=20_000)))))
        if (current_scan["entry_limit_reached"] or current_scan["depth_limit_reached"]
                or current_scan["metadata_errors"] or current_scan["unsafe_entries"]
                or current_scan["manifest_sha256"] != proposal["manifest_sha256"]):
            approvals.pop(token, None)
            ctx.state.set(_APPROVAL_STATE_KEY, approvals)
            _save_receipt(ctx, {
                "receipt_id": token,
                "proposal_id": proposal_id,
                "decision": "approved_once",
                "exact_target": str(target["path"]),
                "execution_status": "blocked_stale_inventory",
                "approved_manifest_sha256": proposal["manifest_sha256"],
                "current_manifest_sha256": current_scan["manifest_sha256"],
                "reason": "target changed after review; take a fresh inventory",
                "recorded_at": datetime.now(UTC).isoformat(),
            })
            return _json_result({"status": "blocked", "reason": "target changed after review; take a fresh inventory"})
        receipt_id = token
        before_usage = _disk_usage(target["path"])
        service_before = _service_health(target["service_units"])
        if service_before["status"] == "unhealthy":
            _save_receipt(ctx, {
                "receipt_id": token,
                "proposal_id": proposal_id,
                "decision": "approved_once",
                "exact_target": str(target["path"]),
                "execution_status": "blocked_service_unhealthy",
                "service_health_before": service_before,
                "reason": "a configured service is not healthy; cleanup was not run",
                "recorded_at": datetime.now(UTC).isoformat(),
            })
            return _json_result({"status": "blocked", "reason": "a configured service is not healthy; cleanup was not run"})
        receipt: dict[str, Any] = {
            "receipt_id": receipt_id,
            "proposal_id": proposal_id,
            "target_id": target["id"],
            "exact_target": str(target["path"]),
            "managed_action_id": target["managed_action_id"],
            "manifest_sha256": proposal["manifest_sha256"],
            "decision": "approved_once",
            "approval_choice": grant.get("choice"),
            "approval_surface": grant.get("surface", "unknown"),
            "approver_identity": "not-exposed-by-hermes-approval-api",
            "expected_reclaimed_bytes": proposal["expected_reclaimed_bytes"],
            "free_bytes_before": before_usage.free,
            "service_health_before": service_before,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
        }
        _save_receipt(ctx, receipt)
        try:
            removed = _empty_configured_cache(target["path"], proposal["manifest_sha256"],
                                               min(_MAX_ENTRIES, max(1, int(ctx.get_config(
                                                   "max_entries_per_target", default=20_000)))))
            after_usage = _disk_usage(target["path"])
            service_after = _service_health(target["service_units"])
            verified = after_usage.free >= before_usage.free and service_after["status"] == "healthy"
            receipt.update({
                **removed,
                "free_bytes_after": after_usage.free,
                "measured_free_byte_delta": after_usage.free - before_usage.free,
                "service_health_after": service_after,
                "status": "completed" if verified else (
                    "verification_incomplete" if service_after["status"] == "not_configured"
                    else "verification_failed"
                ),
                "finished_at": datetime.now(UTC).isoformat(),
            })
        except Exception as exc:  # health: allow BLE001 -- preserve a bounded receipt for every executor failure.
            try:
                free_after = _disk_usage(target["path"]).free
            except OSError:
                free_after = None
            receipt.update({
                "status": "partial_or_failed",
                "free_bytes_after": free_after,
                "error_type": type(exc).__name__,
                "recovery_path": target["recovery_path"],
                "finished_at": datetime.now(UTC).isoformat(),
            })
        _save_receipt(ctx, receipt)
        return _json_result({"cleanup_receipt": receipt})

    # Serialize receipt reconciliation with cleanup execution. If the operation is
    # still running in this process, reconciliation waits and then sees its final
    # receipt rather than misclassifying it as interrupted.
    cleanup_handler_unlocked = cleanup_handler

    def cleanup_handler(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
        with _STATE_LOCK:
            return cleanup_handler_unlocked(args, **kwargs)

    def reconcile_handler(args: dict[str, Any] | None = None, **_: Any) -> str:
        args = args or {}
        receipt_id = args.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id:
            return _json_result({"status": "blocked", "reason": "receipt_id is required"})
        if args.get("confirm_process_stopped") is not True:
            return _json_result({
                "status": "blocked",
                "reason": "confirm the original Hermes process has stopped before reconciliation",
            })
        with _STATE_LOCK:
            receipts = ctx.state.get(_RECEIPTS_STATE_KEY, default=[])
            if not isinstance(receipts, list):
                receipts = []
            index = next((i for i, item in enumerate(receipts)
                          if isinstance(item, dict) and item.get("receipt_id") == receipt_id), None)
            if index is None:
                return _json_result({"status": "not_found", "receipt_id": receipt_id})
            receipt = dict(receipts[index])
            if receipt.get("status") != "running":
                return _json_result({
                    "status": "not_running",
                    "receipt_id": receipt_id,
                    "current_status": receipt.get("status"),
                })
            receipt.update({
                "status": "interrupted_requires_review",
                "reconciled_at": datetime.now(UTC).isoformat(),
                "reconciliation": "operator_confirmed_process_stopped",
                "recovery_path": (
                    "Take a fresh inventory and verify target contents, free space, and configured "
                    "service health. Any further cleanup requires a new proposal and fresh approval."
                ),
            })
            receipts[index] = receipt
            ctx.state.set(_RECEIPTS_STATE_KEY, receipts[-_MAX_RECEIPTS:])
            return _json_result({"status": "reconciled", "cleanup_receipt": receipt})

    def receipts_handler(args: dict[str, Any] | None = None, **_: Any) -> str:
        receipts = ctx.state.get(_RECEIPTS_STATE_KEY, default=[])
        if not isinstance(receipts, list):
            receipts = []
        return _json_result({"receipts": receipts[-50:], "retained_count": len(receipts)})

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
    ctx.register_tool(
        name="host_cleanup",
        toolset="host_steward",
        schema=HOST_CLEANUP,
        handler=cleanup_handler,
        description="Human-approved managed cleanup of one configured cache target",
    )
    ctx.register_tool(
        name="host_cleanup_receipts",
        toolset="host_steward",
        schema=HOST_CLEANUP_RECEIPTS,
        handler=receipts_handler,
        description="Read bounded Host Steward cleanup receipts",
    )
    ctx.register_tool(
        name="host_cleanup_reconcile",
        toolset="host_steward",
        schema=HOST_CLEANUP_RECONCILE,
        handler=reconcile_handler,
        description=(
            "Mark a receipt interrupted after operator-confirmed process stop; never retries cleanup"
        ),
    )
    ctx.register_hook("pre_tool_call", prepare_cleanup_call)
    ctx.register_hook("pre_tool_call", require_cleanup_approval)
    ctx.register_hook("post_approval_response", approval_observer)
