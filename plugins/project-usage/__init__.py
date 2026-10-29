"""Read-only, profile-local project usage summary for Hermes Core."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+ -]{0,127}$")
_MAX_MODEL_ROWS = 50

PROJECT_USAGE = {
    "name": "project_usage",
    "description": (
        "Read aggregate token usage for the one project ID configured in this Hermes profile. "
        "The local ledger covers Codex app-server turns and Hermes provider API turns with "
        "explicit project/goal bindings; durable Codex starts without a matching end are "
        "counted as unknown. Auxiliary calls outside the active-turn callback, auxiliary "
        "attempts without a returned response, attempts without a durable start, and unbound "
        "sessions are excluded. "
        "The response marks this coverage partial even if every captured turn has reported tokens. "
        "Token totals may already include cache buckets; do not add totals to input/cache values. "
        "The report includes the read time and age of its latest retained event without a "
        "freshness threshold. It does not expose session or goal identifiers."
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


def _unclosed_codex_attempts(
    connection: sqlite3.Connection, project_id: str,
) -> tuple[int, str | None, str | None, list[tuple[str, str, int, str | None, str | None]]]:
    """Count durable bound Codex starts that have no matching end as unknown usage."""
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "codex_events" not in tables:
        return 0, None, None, []

    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(codex_events)")
    }
    required = {"project_id", "source_id", "event_type", "occurred_at", "context_json"}
    if not required.issubset(columns):
        return 0, None, None, []

    attempts = """
        WITH starts AS (
            SELECT event_id, source_id, project_id, occurred_at,
                   CASE WHEN json_valid(context_json)
                        THEN json_extract(context_json, '$.attempt_id') END AS attempt_id,
                   CASE WHEN json_valid(context_json)
                        THEN json_extract(context_json, '$.provider') END AS provider,
                   CASE WHEN json_valid(context_json)
                        THEN json_extract(context_json, '$.model') END AS model
            FROM codex_events
            WHERE project_id = ? AND event_type = 'codex:turn:start'
        ), unmatched AS (
            SELECT s.event_id, s.occurred_at, s.provider, s.model
            FROM starts AS s
            WHERE typeof(s.attempt_id) = 'text'
              AND NOT EXISTS (
                  SELECT 1 FROM codex_events AS e
                  WHERE e.project_id = s.project_id
                    AND e.source_id = s.source_id
                    AND e.event_type = 'codex:turn:end'
                    AND CASE WHEN json_valid(e.context_json)
                             THEN json_extract(e.context_json, '$.attempt_id') END = s.attempt_id
              )
        )
    """
    aggregate = connection.execute(
        attempts + "SELECT COUNT(*), MIN(occurred_at), MAX(occurred_at) FROM unmatched",
        (project_id,),
    ).fetchone()
    groups = connection.execute(
        attempts + """
            SELECT COALESCE(provider, 'unknown'), COALESCE(model, 'unknown'),
                   COUNT(*), MIN(occurred_at), MAX(occurred_at)
            FROM unmatched
            GROUP BY COALESCE(provider, 'unknown'), COALESCE(model, 'unknown')
            ORDER BY COUNT(*) DESC, 1, 2 LIMIT ?
        """,
        (project_id, _MAX_MODEL_ROWS + 1),
    ).fetchall()
    return int(aggregate[0] or 0), aggregate[1], aggregate[2], groups


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

        unclosed_turns, unclosed_first, unclosed_last, unclosed_models = (
            _unclosed_codex_attempts(connection, project_id)
        )
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
        if unclosed_turns and "codex_app_server" not in sources:
            sources.append("codex_app_server")
            sources.sort()
        interval = connection.execute(
            "SELECT MIN(occurred_at), MAX(occurred_at) "
            "FROM project_usage_events WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        first_time_candidates = [
            value for value in (interval[0], unclosed_first) if isinstance(value, str)
        ]
        last_time_candidates = [
            value for value in (interval[1], unclosed_last) if isinstance(value, str)
        ]
        first_occurred_at = min(first_time_candidates, default=None)
        last_occurred_at = max(last_time_candidates, default=None)
        measured_at = datetime.now(UTC)
        latest_event_age_seconds: int | None = None
        if last_occurred_at is not None:
            try:
                latest_event = datetime.fromisoformat(last_occurred_at)
                if latest_event.tzinfo is not None:
                    age = measured_at - latest_event.astimezone(UTC)
                    if age >= timedelta(0):
                        latest_event_age_seconds = int(age.total_seconds())
            except ValueError:
                # Keep the age unknown when the stored observation time is invalid.
                pass
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

        for provider, model, turns, _first_time, _last_time in unclosed_models:
            provider = (
                provider if isinstance(provider, str) and _LABEL_RE.fullmatch(provider)
                else "unknown"
            )
            model = model if isinstance(model, str) and _LABEL_RE.fullmatch(model) else "unknown"
            key = (provider, model)
            entry = models.setdefault(key, {
                "provider": provider,
                "model": model,
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
            entry["unknown_usage_turns"] += int(turns)

        ordered_models = sorted(models.values(), key=lambda item: (item["provider"], item["model"]))
        model_groups_truncated = (
            len(model_rows) > _MAX_MODEL_ROWS
            or len(unclosed_models) > _MAX_MODEL_ROWS
            or len(ordered_models) > _MAX_MODEL_ROWS
        )

        return {
            "status": "available",
            "project_id": project_id,
            "coverage": (
                "bound Codex app-server and Hermes provider API turns, including auxiliary "
                "responses with usage in the active turn; durable Codex starts without a "
                "matching end count as unknown; unbound sessions, auxiliary calls outside "
                "that context, auxiliary attempts without a response, and attempts without "
                "a durable start are excluded"
            ),
            "sources": sources,
            "turns": int(totals[0] or 0) + unclosed_turns,
            "reported_turns": int(totals[1] or 0),
            "unknown_usage_turns": int(totals[2] or 0) + unclosed_turns,
            "measurement_scope": (
                "all_retained_project_usage_rows_and_durable_unclosed_codex_starts"
            ),
            "measurement_interval": {
                "first_occurred_at": first_occurred_at,
                "last_occurred_at": last_occurred_at,
            },
            "measured_at": measured_at.isoformat(timespec="microseconds"),
            "latest_event_age_seconds": latest_event_age_seconds,
            # ``tokens_complete`` describes captured rows, not the project-wide
            # coverage that remains partial while known call classes are excluded.
            "coverage_complete": False,
            "coverage_exclusions": [
                "auxiliary_calls_outside_active_turn_context",
                "auxiliary_attempts_without_response",
                "codex_attempts_without_durable_start_event",
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
            "tokens_complete": int(totals[2] or 0) == 0 and unclosed_turns == 0,
            "by_provider_model": ordered_models[:_MAX_MODEL_ROWS],
            "provider_model_groups_truncated": model_groups_truncated,
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
