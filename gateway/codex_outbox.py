"""Profile-local durable queue for opted-in Curator Codex lifecycle hooks.

Only a deliberately small allowlist of correlation fields is retained. The
queue stores the exact event envelope passed to durable hook handlers so a
restart or a failed handler can retry the same event ID and source sequence.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Mapping

from hermes_constants import get_hermes_home


_EVENT_FIELDS = {
    "codex:turn:start": frozenset({"attempt_id", "session_id"}),
    "codex:turn:end": frozenset(
        {"attempt_id", "session_id", "turn_id", "outcome", "failure_kind"}
    ),
}
_ACCEPTED_FIELDS = {
    "codex:turn:start": frozenset({"attempt_id", "session_id", "model", "provider", "cwd"}),
    "codex:turn:end": frozenset(
        {"attempt_id", "session_id", "model", "provider", "cwd", "turn_id", "outcome", "failure_kind"}
    ),
}
_OUTCOME_VALUES = frozenset({"completed", "interrupted", "failed"})
_PROFILE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,255}$")


class CodexLifecycleOutbox:
    """SQLite-backed, per-Hermes-profile event outbox."""

    def __init__(self, path: str | Path | None = None, *, profile_id: str | None = None) -> None:
        self.path = Path(path) if path is not None else get_hermes_home() / "curatormd" / "codex-outbox.sqlite3"
        self.profile_id = profile_id
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, timeout=5, isolation_level=None, check_same_thread=False
        )
        os.chmod(self.path, 0o600)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS codex_events (
                event_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                source_sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                context_json TEXT NOT NULL,
                project_id TEXT,
                goal_id TEXT,
                acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
                attempts INTEGER NOT NULL DEFAULT 0,
                UNIQUE(source_id, source_sequence)
            )"""
        )
        columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(codex_events)").fetchall()
        }
        for name in ("project_id", "goal_id"):
            if name not in columns:
                self._connection.execute(f"ALTER TABLE codex_events ADD COLUMN {name} TEXT")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS active_goal_bindings (
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                goal_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                bound_at TEXT NOT NULL,
                PRIMARY KEY(session_id)
            )"""
        )
        self._connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS active_goal_binding_session "
            "ON active_goal_bindings(session_id)"
        )
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS goal_binding_history (
                binding_revision INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                goal_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                bound_at TEXT NOT NULL
            )"""
        )

    def bind_goal(self, project_id: str, session_id: str, goal_id: str) -> bool:
        """Bind one explicit active goal to this Hermes session, retaining binding history."""
        if (
            not isinstance(project_id, str)
            or not _PROFILE_ID_RE.fullmatch(project_id)
            or not isinstance(session_id, str)
            or not _SESSION_ID_RE.fullmatch(session_id)
            or not isinstance(goal_id, str)
            or not _PROFILE_ID_RE.fullmatch(goal_id)
        ):
            return False
        profile_id = self.profile_id or Path(self.path).parent.parent.name
        if not _PROFILE_ID_RE.fullmatch(profile_id):
            return False
        source_id = "hermes-" + hashlib.sha256(
            f"{profile_id}\0{project_id}".encode()
        ).hexdigest()
        bound_at = datetime.now(UTC).isoformat(timespec="microseconds")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._connection.execute(
                    "SELECT goal_id FROM active_goal_bindings WHERE project_id = ? AND session_id = ?",
                    (project_id, session_id),
                ).fetchone()
                if current is not None and current[0] == goal_id:
                    self._connection.commit()
                    return True
                self._connection.execute(
                    "INSERT INTO goal_binding_history(project_id, session_id, goal_id, source_id, bound_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (project_id, session_id, goal_id, source_id, bound_at),
                )
                self._connection.execute(
                    "INSERT INTO active_goal_bindings(project_id, session_id, goal_id, source_id, bound_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                    "goal_id = excluded.goal_id, source_id = excluded.source_id, bound_at = excluded.bound_at",
                    (project_id, session_id, goal_id, source_id, bound_at),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return True

    def enqueue(self, event_type: str, context: Mapping[str, Any]) -> dict[str, Any] | None:
        """Atomically persist a bounded event before any durable handler runs."""
        allowed = _EVENT_FIELDS.get(event_type)
        accepted = _ACCEPTED_FIELDS.get(event_type)
        if allowed is None or accepted is None or set(context) - accepted:
            return None
        if any(
            not isinstance(context.get(key), str)
            or not context[key]
            or len(context[key]) > 256
            or "\n" in context[key]
            or "\r" in context[key]
            for key in ("attempt_id", "session_id")
        ):
            return None
        if event_type == "codex:turn:end":
            if not isinstance(context.get("turn_id"), str) or not context["turn_id"]:
                return None
            if len(context["turn_id"]) > 256 or "\n" in context["turn_id"] or "\r" in context["turn_id"]:
                return None
            if not isinstance(context.get("outcome"), str) or context["outcome"] not in _OUTCOME_VALUES:
                return None

        now = datetime.now(UTC).isoformat(timespec="microseconds")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                binding_rows = self._connection.execute(
                    "SELECT project_id, goal_id, source_id FROM active_goal_bindings WHERE session_id = ?",
                    (context["session_id"],),
                ).fetchall()
                if len(binding_rows) != 1:
                    self._connection.rollback()
                    return None
                project_id, goal_id, source_id = binding_rows[0]
                row = self._connection.execute(
                    "SELECT COALESCE(MAX(source_sequence), 0) + 1 FROM codex_events "
                    "WHERE source_id = ? AND project_id = ?",
                    (source_id, project_id),
                ).fetchone()
                sequence = int(row[0])
                stable_key = f"{source_id}\0{event_type}\0{context['attempt_id']}".encode()
                event_id = f"hermes-{hashlib.sha256(stable_key).hexdigest()}"
                # Repeated callback delivery for the same lifecycle event is a no-op;
                # return its original exact envelope instead of advancing sequence.
                existing = self._connection.execute(
                    "SELECT source_sequence, occurred_at, context_json, project_id, goal_id "
                    "FROM codex_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if existing is not None:
                    envelope = json.loads(existing[2])
                    envelope.update(
                        event_id=event_id,
                        source_id=source_id,
                        source_sequence=int(existing[0]),
                        occurred_at=existing[1],
                        project_id=existing[3],
                        goal_id=existing[4],
                    )
                    self._connection.commit()
                    return envelope
                retained_context = {key: context[key] for key in sorted(allowed) if key in context}
                if any(
                    not isinstance(value, str)
                    or len(value) > 256
                    or "\n" in value
                    or "\r" in value
                    for value in retained_context.values()
                ):
                    self._connection.rollback()
                    return None
                serialized = json.dumps(retained_context, sort_keys=True, separators=(",", ":"))
                envelope = {
                    "event_id": event_id,
                    "source_id": source_id,
                    "source_sequence": sequence,
                    "occurred_at": now,
                    "project_id": project_id,
                    "goal_id": goal_id,
                    "event_type": event_type,
                    "context": retained_context,
                }
                self._connection.execute(
                    "INSERT INTO codex_events(event_id, source_id, source_sequence, event_type, occurred_at, "
                    "context_json, project_id, goal_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (event_id, source_id, sequence, event_type, now, serialized, project_id, goal_id),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return envelope

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_id, source_id, source_sequence, event_type, occurred_at, context_json, project_id, goal_id "
                "FROM codex_events WHERE acknowledged = 0 AND project_id IS NOT NULL AND goal_id IS NOT NULL "
                "ORDER BY source_id, project_id, source_sequence LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "event_id": row[0],
                "source_id": row[1],
                "source_sequence": row[2],
                "event_type": row[3],
                "occurred_at": row[4],
                "context": json.loads(row[5]),
                "project_id": row[6],
                "goal_id": row[7],
            }
            for row in rows
        ]

    def mark_attempt(self, event_id: str, *, acknowledged: bool) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE codex_events SET attempts = attempts + 1, acknowledged = ? WHERE event_id = ?",
                (int(acknowledged), event_id),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
