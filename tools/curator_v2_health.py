"""Read-only local readiness probes for the opted-in Curator V2 MCP server."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import hermes_yaml as yaml


_PROBED_DEPENDENCIES = (
    "event_router",
    "purge_scheduler",
    "database_storage_adapter",
    "alert_delivery",
    "host_identity",
    "human_review",
    "artifact_producer_inventory",
)
_PURGE_SCHEDULER_FRESHNESS_US = 64_800_000_000
_SYSTEMD_MONOTONIC_PART = re.compile(r"(\d+(?:\.\d+)?)(min|ms|us|d|h|s)")
_SYSTEMD_MONOTONIC_FACTORS = {
    "d": 86_400_000_000,
    "h": 3_600_000_000,
    "min": 60_000_000,
    "s": 1_000_000,
    "ms": 1_000,
    "us": 1,
}


def _observation(ready: bool | None, observed_at: str) -> dict[str, Any]:
    return {"ready": ready, "observed_at": observed_at}


def _database_arg(config: dict[str, Any]) -> Path | None:
    """Read the explicitly configured absolute database path from argv."""

    args = config.get("args")
    if isinstance(args, str):
        try:
            args = shlex.split(args)
        except ValueError:
            return None
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        return None
    try:
        value = args[args.index("--database") + 1]
    except (ValueError, IndexError):
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else None


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=1)


def _outbox_ready(home: Path, project_id: str) -> bool | None:
    path = home / "curatormd" / "codex-outbox.sqlite3"
    if not path.is_file():
        return None
    try:
        with closing(_readonly_connection(path)) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(codex_events)")
            }
            if integrity != ("ok",) or not {"project_id", "acknowledged"} <= columns:
                return False
            pending = connection.execute(
                "SELECT count(*) FROM codex_events "
                "WHERE project_id = ? AND acknowledged = 0",
                (project_id,),
            ).fetchone()
            return pending is not None and pending[0] == 0
    except (OSError, sqlite3.Error):
        return None


def _review_grant_ready(database: Path | None, project_id: str) -> bool | None:
    if database is None or not database.is_file():
        return None
    effective_uid = getattr(os, "geteuid", None)
    if not callable(effective_uid):
        return None
    try:
        with closing(_readonly_connection(database)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {"project_grants", "project_grant_revocations"} <= tables:
                return None
            row = connection.execute(
                "SELECT 1 FROM project_grants g "
                "LEFT JOIN project_grant_revocations r USING (grant_id) "
                "WHERE g.project_id = ? AND g.subject_id = ? "
                "AND g.action = 'review' AND r.grant_id IS NULL LIMIT 1",
                (project_id, f"local-uid:{effective_uid()}"),
            ).fetchone()
            return row is not None
    except (OSError, sqlite3.Error):
        return None


def _storage_ready(database: Path | None) -> bool | None:
    """Probe only that the configured SQLite database can be read consistently."""

    if database is None or not database.is_file():
        return None
    try:
        with closing(_readonly_connection(database)) as connection:
            return connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    except (OSError, sqlite3.Error):
        return None


def _producer_inventory_ready(home: Path) -> bool | None:
    """Check the installed profile hook manifest is bounded and internally valid."""

    path = home / "hooks" / "curator-v2" / "HOOK.yaml"
    try:
        if not path.is_file() or path.stat().st_size > 16_384:
            return None
        with path.open("r", encoding="utf-8-sig") as stream:
            manifest = yaml.safe_load(stream)
        if not isinstance(manifest, dict):
            return False
        events = manifest.get("events")
        durable = manifest.get("durable_events", [])
        return (
            isinstance(events, list)
            and bool(events)
            and all(isinstance(event, str) and 1 <= len(event) <= 64 for event in events)
            and isinstance(durable, list)
            and all(event in events for event in durable)
        )
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        return None


def _systemd_properties(unit: str) -> dict[str, str] | None:
    """Read a small allowlisted property set from the current user's manager."""

    executable = shutil.which("systemctl")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [
                executable,
                "--user",
                "show",
                "--no-pager",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=UnitFileState",
                "--property=LastTriggerUSecMonotonic",
                "--property=Result",
                "--property=ExecMainStartTimestampMonotonic",
                "--property=StandardOutput",
                "--property=StandardError",
                unit,
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or len(result.stdout) > 4096 or len(result.stderr) > 4096:
        return None
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in properties:
            return None
        properties[key] = value
    return properties


def _monotonic_usec(value: str) -> int | None:
    """Parse an integer or systemctl-formatted monotonic microsecond value."""

    if value.isdecimal():
        return int(value)
    position = 0
    total = Decimal(0)
    found = False
    for match in _SYSTEMD_MONOTONIC_PART.finditer(value):
        if value[position : match.start()].strip():
            return None
        amount, unit = match.groups()
        total += Decimal(amount) * _SYSTEMD_MONOTONIC_FACTORS[unit]
        position = match.end()
        found = True
    if not found or value[position:].strip() or total != total.to_integral_value():
        return None
    return int(total)


def _purge_scheduler_ready(project_id: str) -> bool | None:
    """Require an enabled timer and a recent successful run for this project."""

    if not isinstance(project_id, str) or not project_id.isascii() or not (
        1 <= len(project_id) <= 128
    ) or any(not (character.isalnum() or character in "._-") for character in project_id):
        return None
    timer = _systemd_properties(f"curatormd-v2-maintenance@{project_id}.timer")
    if timer is None:
        return None
    if timer.get("LoadState") == "not-found":
        return False
    if timer.get("LoadState") != "loaded":
        return None
    if timer.get("ActiveState") != "active" or timer.get("UnitFileState") != "enabled":
        return False
    last_trigger = timer.get("LastTriggerUSecMonotonic", "")
    trigger_us = _monotonic_usec(last_trigger)
    if trigger_us is None or trigger_us <= 0:
        return False

    service = _systemd_properties(f"curatormd-v2-maintenance@{project_id}.service")
    if service is None:
        return None
    if service.get("LoadState") == "not-found":
        return False
    if service.get("LoadState") != "loaded":
        return None
    if service.get("Result") != "success" or service.get("ActiveState") != "inactive":
        return False
    service_start = service.get("ExecMainStartTimestampMonotonic", "")
    if not service_start.isdecimal() or int(service_start) <= 0:
        return False

    now = time.monotonic_ns() // 1000
    service_start_us = _monotonic_usec(service_start)
    if service_start_us is None or service_start_us <= 0:
        return False
    return (
        service_start_us >= trigger_us
        and now >= trigger_us
        and now - trigger_us <= _PURGE_SCHEDULER_FRESHNESS_US
        and now - service_start_us <= _PURGE_SCHEDULER_FRESHNESS_US
    )


def _journal_alert_delivery_ready(project_id: str) -> bool | None:
    """Check that this project's maintenance output reaches a readable journal."""

    if not isinstance(project_id, str) or not project_id.isascii() or not (
        1 <= len(project_id) <= 128
    ) or any(not (character.isalnum() or character in "._-") for character in project_id):
        return None
    service = _systemd_properties(f"curatormd-v2-maintenance@{project_id}.service")
    if service is None:
        return None
    if service.get("LoadState") == "not-found":
        return False
    if service.get("LoadState") != "loaded":
        return None
    if service.get("StandardOutput") != "journal" or service.get("StandardError") != "journal":
        return False

    executable = shutil.which("journalctl")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "--user", "--disk-usage", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if len(result.stdout) > 4096 or len(result.stderr) > 4096:
        return None
    return True if result.returncode == 0 else None


def curator_v2_readiness(
    config: dict[str, Any],
    *,
    profile_id: str,
    session_id: str,
    home: Path,
) -> dict[str, dict[str, Any]]:
    """Return seven bounded observations; no payloads, paths, or identities escape."""

    observed_at = datetime.now(UTC).isoformat(timespec="seconds")
    project_id = config.get("hermes_project_id")
    project_valid = isinstance(project_id, str) and 1 <= len(project_id) <= 128
    database = _database_arg(config)
    result = {
        "event_router": _observation(
            _outbox_ready(home, project_id) if project_valid else None, observed_at
        ),
        "purge_scheduler": _observation(
            _purge_scheduler_ready(project_id) if project_valid else None,
            observed_at,
        ),
        "database_storage_adapter": _observation(
            _storage_ready(database), observed_at
        ),
        # Local alpha failures are delivered through the user journal only.
        "alert_delivery": _observation(
            _journal_alert_delivery_ready(project_id) if project_valid else None,
            observed_at,
        ),
        "host_identity": _observation(
            bool(profile_id and session_id and config.get("hermes_context") is True),
            observed_at,
        ),
        "human_review": _observation(
            _review_grant_ready(database, project_id) if project_valid else None,
            observed_at,
        ),
        "artifact_producer_inventory": _observation(
            _producer_inventory_ready(home), observed_at
        ),
    }
    return {name: result[name] for name in _PROBED_DEPENDENCIES}
