"""Read-only, profile-local project usage summary for Hermes Core."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_MODEL_ROWS = 50

PROJECT_USAGE = {
    "name": "project_usage",
    "description": (
        "Read aggregate token usage for the one project ID configured in this Hermes profile. "
        "The local ledger covers Codex app-server turns and Hermes provider API turns with "
        "explicit project/goal bindings; auxiliary calls outside the active-turn callback, "
        "attempts without a returned response, and unbound sessions are excluded. "
        "The response marks this coverage partial even if every captured turn has reported tokens. "
        "Token totals may already include cache buckets; do not add totals to input/cache values. "
        "It does not expose session or goal identifiers."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def _project_id(ctx: Any) -> str | None:
    value = ctx.get_config("project_id", default=None)
    return value if isinstance(value, str) and _PROJECT_ID.fullmatch(value) else None


def _ledger_path() -> Path:
    """Resolve only the current profile's fixed ledger location."""
    home = get_hermes_home().resolve(strict=True)
    state_dir = home / "curatormd"
    if state_dir.is_symlink():
        raise OSError("unsafe_ledger_directory")
    path = state_dir / "codex-outbox.sqlite3"
    if path.is_symlink():
        raise OSError("unsafe_ledger_file")
    resolved = path.resolve(strict=True)
    if resolved.parent != state_dir.resolve(strict=True) or not resolved.is_file():
        raise OSError("unsafe_ledger_path")
    return resolved


def _sum_or_none(value: Any) -> int | None:
    return int(value) if value is not None else None


def _read_usage(project_id: str) -> dict[str, Any]:
    try:
        path = _ledger_path()
    except FileNotFoundError:
        return {"status": "unavailable", "reason": "usage_ledger_not_found"}
    except OSError:
        return {"status": "unavailable", "reason": "unsafe_or_unreadable_ledger_path"}

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)
        connection.execute("PRAGMA query_only = ON")
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(project_usage_events)")
        }
        required = {
            "project_id", "provider", "model", "usage_status", "input_tokens",
            "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens",
            "reasoning_tokens", "turn_id", "usage_source",
        }
        if not required.issubset(columns):
            return {"status": "unavailable", "reason": "usage_ledger_schema_unavailable"}

        totals = connection.execute(
            """WITH turn_state AS (
                   SELECT turn_id, MAX(usage_status = 'unknown') AS unknown_usage
                   FROM project_usage_events WHERE project_id = ? GROUP BY turn_id
               ), token_totals AS (
                   SELECT SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
                          SUM(total_tokens) AS total_tokens,
                          SUM(cache_read_tokens) AS cache_read_tokens,
                          SUM(cache_write_tokens) AS cache_write_tokens,
                          SUM(reasoning_tokens) AS reasoning_tokens
                   FROM project_usage_events WHERE project_id = ?
               )
               SELECT (SELECT COUNT(*) FROM turn_state),
                      (SELECT SUM(unknown_usage = 0) FROM turn_state),
                      (SELECT SUM(unknown_usage = 1) FROM turn_state),
                      token_totals.input_tokens, token_totals.output_tokens,
                      token_totals.total_tokens, token_totals.cache_read_tokens,
                      token_totals.cache_write_tokens, token_totals.reasoning_tokens
               FROM token_totals""",
            (project_id, project_id),
        ).fetchone()
        model_rows = connection.execute(
            """SELECT provider, model, usage_status, COUNT(DISTINCT turn_id), SUM(input_tokens),
                      SUM(output_tokens), SUM(total_tokens), SUM(cache_read_tokens),
                      SUM(cache_write_tokens), SUM(reasoning_tokens)
               FROM project_usage_events WHERE project_id = ?
               GROUP BY provider, model, usage_status
               ORDER BY provider, model, usage_status LIMIT ?""",
            (project_id, _MAX_MODEL_ROWS + 1),
        ).fetchall()
        sources = [
            row[0] for row in connection.execute(
                "SELECT DISTINCT usage_source FROM project_usage_events "
                "WHERE project_id = ? AND usage_source IN ('codex_app_server', 'hermes_agent_api') "
                "ORDER BY usage_source",
                (project_id,),
            ).fetchall()
        ]
        interval = connection.execute(
            "SELECT MIN(occurred_at), MAX(occurred_at) "
            "FROM project_usage_events WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        models: dict[tuple[str, str], dict[str, Any]] = {}
        for row in model_rows[:_MAX_MODEL_ROWS]:
            provider, model, usage_status, turns, *sums = row
            key = (provider or "unknown", model or "unknown")
            entry = models.setdefault(key, {
                "provider": key[0],
                "model": key[1],
                "turns": 0,
                "reported_turns": 0,
                "unknown_usage_turns": 0,
                "tokens": {
                    "input": None,
                    "output": None,
                    "total": None,
                    "cache_read": None,
                    "cache_write": None,
                    "reasoning": None,
                },
            })
            entry["turns"] += int(turns)
            if usage_status == "reported":
                entry["reported_turns"] += int(turns)
                for token_name, value in zip(entry["tokens"], sums, strict=True):
                    if value is not None:
                        entry["tokens"][token_name] = (
                            (entry["tokens"][token_name] or 0) + int(value)
                        )
            else:
                entry["unknown_usage_turns"] += int(turns)

        return {
            "status": "available",
            "project_id": project_id,
            "coverage": (
                "bound Codex app-server and Hermes provider API turns, including auxiliary "
                "responses with usage in the active turn; unbound sessions, auxiliary calls "
                "outside that context, and attempts without a response are excluded"
            ),
            "sources": sources,
            "turns": int(totals[0] or 0),
            "reported_turns": int(totals[1] or 0),
            "unknown_usage_turns": int(totals[2] or 0),
            "measurement_scope": "all_retained_project_usage_rows",
            "measurement_interval": {
                "first_occurred_at": interval[0],
                "last_occurred_at": interval[1],
            },
            # ``tokens_complete`` describes captured rows, not the project-wide
            # coverage that remains partial while known call classes are excluded.
            "coverage_complete": False,
            "coverage_exclusions": [
                "auxiliary_calls_outside_active_turn_context",
                "auxiliary_attempts_without_response",
                "unbound_sessions",
            ],
            "tokens": {
                "input": _sum_or_none(totals[3]),
                "output": _sum_or_none(totals[4]),
                "total": _sum_or_none(totals[5]),
                "cache_read": _sum_or_none(totals[6]),
                "cache_write": _sum_or_none(totals[7]),
                "reasoning": _sum_or_none(totals[8]),
            },
            "tokens_complete": int(totals[2] or 0) == 0,
            "by_provider_model": list(models.values()),
            "provider_model_groups_truncated": len(model_rows) > _MAX_MODEL_ROWS,
        }
    except sqlite3.Error as exc:
        return {"status": "unavailable", "reason": f"ledger_read_failed:{type(exc).__name__}"}
    finally:
        if connection is not None:
            connection.close()


def register(ctx: Any) -> None:
    """Register the bounded read tool; data remains scoped to profile config."""
    def usage_handler(_args: dict[str, Any] | None = None, **_: Any) -> str:
        project_id = _project_id(ctx)
        if project_id is None:
            return json.dumps({"status": "not_configured", "reason": "configure_project_id"})
        return json.dumps(_read_usage(project_id), separators=(",", ":"))

    ctx.register_tool(
        name="project_usage",
        toolset="project_usage",
        schema=PROJECT_USAGE,
        handler=usage_handler,
        description="Read local aggregate usage for this Hermes profile's configured project",
    )
