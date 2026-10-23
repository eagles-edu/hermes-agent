"""Read-only local readiness probes for the opted-in Curator V2 MCP server."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import os
from pathlib import Path
import shlex
import sqlite3
from typing import Any
from urllib.parse import quote

import yaml


_PROBED_DEPENDENCIES = (
    "event_router",
    "purge_scheduler",
    "host_identity",
    "human_review",
    "artifact_producer_inventory",
)


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
                (project_id, f"local-uid:{os.geteuid()}"),
            ).fetchone()
            return row is not None
    except (OSError, sqlite3.Error):
        return None


def _producer_inventory_ready(home: Path) -> bool | None:
    """Check the installed profile hook manifest is bounded and internally valid."""

    path = home / "hooks" / "curator-v2" / "HOOK.yaml"
    try:
        if not path.is_file() or path.stat().st_size > 16_384:
            return None
        with path.open("r", encoding="utf-8") as stream:
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


def curator_v2_readiness(
    config: dict[str, Any],
    *,
    profile_id: str,
    session_id: str,
    home: Path,
) -> dict[str, dict[str, Any]]:
    """Return five bounded observations; no payloads, paths, or identities escape."""

    observed_at = datetime.now(UTC).isoformat(timespec="seconds")
    project_id = config.get("hermes_project_id")
    project_valid = isinstance(project_id, str) and 1 <= len(project_id) <= 128
    database = _database_arg(config)
    result = {
        "event_router": _observation(
            _outbox_ready(home, project_id) if project_valid else None, observed_at
        ),
        # No scheduler heartbeat/configuration contract exists in this local alpha.
        # False means the required service is known absent, not that its state is unknown.
        "purge_scheduler": _observation(False, observed_at),
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
