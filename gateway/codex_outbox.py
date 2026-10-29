"""Profile-local durable queue for opted-in Curator Codex lifecycle hooks.

Only a deliberately small allowlist of correlation fields is retained. The
queue stores the exact event envelope passed to durable hook handlers so a
restart or a failed handler can retry the same event ID and source sequence.
Reported token counts are stored separately in this profile's local usage ledger
and are never included in hook deliveries.
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
        {
            "attempt_id", "session_id", "turn_id", "outcome", "failure_kind",
            "work_summary_json",
        }
    ),
}
_ACCEPTED_FIELDS = {
    "codex:turn:start": frozenset({"attempt_id", "session_id", "model", "provider", "cwd"}),
    "codex:turn:end": frozenset(
        {
            "attempt_id", "session_id", "model", "provider", "cwd", "turn_id",
            "outcome", "failure_kind", "usage", "work_summary_json",
        }
    ),
}
_USAGE_FIELDS = frozenset({
    "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
    "cache_write_tokens", "reasoning_tokens",
})
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+ -]{0,127}$")
_OUTCOME_VALUES = frozenset({"completed", "interrupted", "failed"})
_FAILURE_KIND_VALUES = frozenset({"exception", "postprocess_exception", "codex_error"})
_PROFILE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,255}$")
_WORK_SUMMARY_PATH_RE = re.compile(r"^[A-Za-z0-9_./ @+-]{1,256}$")
_WORK_SUMMARY_CHECKS = frozenset(
    {"scripts/check", "scripts/run_tests.sh", "git diff --check", "pytest", "npm test"}
)
_WORK_SUMMARY_PRIVATE_NAMES = frozenset({
    ".env", ".npmrc", ".pypirc", ".netrc", "credentials", "secrets",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "authorized_keys",
    "known_hosts", "application_default_credentials.json",
})
_WORK_SUMMARY_PRIVATE_DIRECTORIES = frozenset(
    {".aws", ".azure", ".docker", ".gnupg", ".kube", ".ssh"}
)
_WORK_SUMMARY_PRIVATE_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".jks", ".keystore")
_WORK_SUMMARY_CREDENTIAL_VALUE_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b",
    r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b",
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b",
    r"\bAIza[0-9A-Za-z_-]{35}\b",
))


def _private_work_summary_path(path: str) -> bool:
    """Reject path labels that commonly identify credentials, keys, or certs."""

    parts = tuple(part.lower() for part in path.split("/"))
    basename = parts[-1] if parts else ""
    return (
        basename in _WORK_SUMMARY_PRIVATE_NAMES
        or basename.endswith(_WORK_SUMMARY_PRIVATE_SUFFIXES)
        or any(part in _WORK_SUMMARY_PRIVATE_DIRECTORIES for part in parts)
        or any(part.startswith((".env", "secret", "credential")) for part in parts)
        or any(pattern.search(path) for pattern in _WORK_SUMMARY_CREDENTIAL_VALUE_PATTERNS)
    )


def _valid_work_summary(value: object) -> bool:
    """Accept only the tiny, content-free summary produced by the Codex runtime."""

    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
        return False
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return False
    if not isinstance(decoded, dict) or set(decoded) != {
        "schema_version", "repository_fingerprint", "changed_files",
        "omitted_files", "checks", "omitted_checks",
    } or decoded.get("schema_version") != 1:
        return False
    fingerprint = decoded.get("repository_fingerprint")
    files, checks = decoded.get("changed_files"), decoded.get("checks")
    omitted_files, omitted_checks = decoded.get("omitted_files"), decoded.get("omitted_checks")
    if (
        not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
        or not isinstance(files, list) or len(files) > 16
        or not isinstance(checks, list) or len(checks) > 8
        or type(omitted_files) is not int or not 0 <= omitted_files <= 1_000_000
        or type(omitted_checks) is not int or not 0 <= omitted_checks <= 1_000_000
    ):
        return False
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "change"}:
            return False
        path, change = item.get("path"), item.get("change")
        if (
            not isinstance(path, str) or not _WORK_SUMMARY_PATH_RE.fullmatch(path)
            or path.startswith("/") or ".." in path.split("/")
            or _private_work_summary_path(path)
            or change not in {"add", "update", "delete", "rename"}
        ):
            return False
    for item in checks:
        if not isinstance(item, dict) or set(item) != {"name", "exit_code"}:
            return False
        if (
            item.get("name") not in _WORK_SUMMARY_CHECKS
            or type(item.get("exit_code")) is not int
            or not 0 <= item["exit_code"] <= 255
        ):
            return False
    return True


def _source_id(profile_id: str, project_id: str, source_epoch: int) -> str:
    """Keep existing queue IDs at epoch one and separate later enrollments."""
    identity = f"{profile_id}\0{project_id}"
    if source_epoch > 1:
        identity += f"\0{source_epoch}"
    return "hermes-" + hashlib.sha256(identity.encode()).hexdigest()


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
                source_epoch INTEGER NOT NULL DEFAULT 1 CHECK (source_epoch >= 1),
                bound_at TEXT NOT NULL,
                PRIMARY KEY(session_id)
            )"""
        )
        binding_columns = {
            row[1]
            for row in self._connection.execute(
                "PRAGMA table_info(active_goal_bindings)"
            ).fetchall()
        }
        if "source_epoch" not in binding_columns:
            self._connection.execute(
                "ALTER TABLE active_goal_bindings ADD COLUMN "
                "source_epoch INTEGER NOT NULL DEFAULT 1 CHECK (source_epoch >= 1)"
            )
        self._connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS active_goal_binding_session "
            "ON active_goal_bindings(session_id)"
        )
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS project_usage_events (
                event_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                goal_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                usage_status TEXT NOT NULL CHECK (usage_status IN ('reported', 'unknown')),
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                reasoning_tokens INTEGER
            )"""
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS project_usage_project_time "
            "ON project_usage_events(project_id, occurred_at)"
        )
        usage_columns = {
            row[1] for row in self._connection.execute(
                "PRAGMA table_info(project_usage_events)"
            ).fetchall()
        }
        if "turn_id" not in usage_columns:
            self._connection.execute("ALTER TABLE project_usage_events ADD COLUMN turn_id TEXT")
        if "usage_source" not in usage_columns:
            self._connection.execute(
                "ALTER TABLE project_usage_events ADD COLUMN usage_source TEXT NOT NULL "
                "DEFAULT 'codex_app_server'"
            )
        self._connection.execute(
            "UPDATE project_usage_events SET turn_id = event_id WHERE turn_id IS NULL OR turn_id = ''"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS project_usage_turn "
            "ON project_usage_events(project_id, turn_id)"
        )
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS goal_binding_history (
                binding_revision INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                goal_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_epoch INTEGER NOT NULL DEFAULT 1 CHECK (source_epoch >= 1),
                bound_at TEXT NOT NULL
            )"""
        )
        history_columns = {
            row[1]
            for row in self._connection.execute(
                "PRAGMA table_info(goal_binding_history)"
            ).fetchall()
        }
        if "source_epoch" not in history_columns:
            self._connection.execute(
                "ALTER TABLE goal_binding_history ADD COLUMN "
                "source_epoch INTEGER NOT NULL DEFAULT 1 CHECK (source_epoch >= 1)"
            )

    def bind_goal(
        self, project_id: str, session_id: str, goal_id: str, source_epoch: int = 1
    ) -> bool:
        """Bind one explicit active goal to this Hermes session, retaining binding history."""
        if (
            not isinstance(project_id, str)
            or not _PROFILE_ID_RE.fullmatch(project_id)
            or not isinstance(session_id, str)
            or not _SESSION_ID_RE.fullmatch(session_id)
            or not isinstance(goal_id, str)
            or not _PROFILE_ID_RE.fullmatch(goal_id)
            or type(source_epoch) is not int
            or source_epoch < 1
        ):
            return False
        profile_id = self.profile_id or Path(self.path).parent.parent.name
        if not _PROFILE_ID_RE.fullmatch(profile_id):
            return False
        source_id = _source_id(profile_id, project_id, source_epoch)
        bound_at = datetime.now(UTC).isoformat(timespec="microseconds")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._connection.execute(
                    "SELECT goal_id, source_epoch FROM active_goal_bindings "
                    "WHERE project_id = ? AND session_id = ?",
                    (project_id, session_id),
                ).fetchone()
                if current is not None and current[1] != source_epoch:
                    self._connection.rollback()
                    return False
                if current is not None and current[0] == goal_id:
                    self._connection.commit()
                    return True
                self._connection.execute(
                    "INSERT INTO goal_binding_history "
                    "(project_id, session_id, goal_id, source_id, source_epoch, bound_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (project_id, session_id, goal_id, source_id, source_epoch, bound_at),
                )
                self._connection.execute(
                    "INSERT INTO active_goal_bindings "
                    "(project_id, session_id, goal_id, source_id, source_epoch, bound_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                    "goal_id = excluded.goal_id, source_id = excluded.source_id, "
                    "source_epoch = excluded.source_epoch, bound_at = excluded.bound_at",
                    (project_id, session_id, goal_id, source_id, source_epoch, bound_at),
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
                    envelope = {
                        "event_id": event_id,
                        "source_id": source_id,
                        "source_sequence": int(existing[0]),
                        "occurred_at": existing[1],
                        "project_id": existing[3],
                        "goal_id": existing[4],
                        "event_type": event_type,
                        "context": json.loads(existing[2]),
                    }
                    if event_type == "codex:turn:end":
                        self._record_project_usage(
                            event_id, existing[3], existing[4], existing[1], context, source_id
                        )
                    self._connection.commit()
                    return envelope
                retained_context = {
                    key: context[key]
                    for key in sorted(allowed)
                    if key in context
                    and (
                        key != "work_summary_json"
                        or _valid_work_summary(context[key])
                    )
                    and (
                        key != "failure_kind"
                        or (
                            isinstance(context[key], str)
                            and context[key] in _FAILURE_KIND_VALUES
                        )
                    )
                }
                if any(
                    not isinstance(value, str)
                    or len(value) > (4096 if key == "work_summary_json" else 256)
                    or "\n" in value
                    or "\r" in value
                    for key, value in retained_context.items()
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
                if event_type == "codex:turn:end":
                    self._record_project_usage(event_id, project_id, goal_id, now, context, source_id)
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return envelope

    def _record_project_usage(
        self, event_id: str, project_id: str, goal_id: str, occurred_at: str,
        context: Mapping[str, Any], source_id: str,
    ) -> None:
        """Keep token accounting local to Hermes; never add it to the Curator event envelope."""
        # Pre-migration rows used the envelope event ID as their usage ID. A
        # replay after schema upgrade must not add a second stable-ID row.
        legacy_row = self._connection.execute(
            "SELECT 1 FROM project_usage_events WHERE event_id = ? "
            "AND usage_source = 'codex_app_server'",
            (event_id,),
        ).fetchone()
        if legacy_row is not None:
            return
        raw_usage = context.get("usage")
        usage: dict[str, int] = {}
        if isinstance(raw_usage, Mapping) and not (set(raw_usage) - _USAGE_FIELDS):
            for key, value in raw_usage.items():
                if type(value) is not int or value < 0 or value > 10**12:
                    usage.clear()
                    break
                usage[key] = value
        status = "reported" if usage else "unknown"
        provider = context.get("provider")
        model = context.get("model")
        provider = provider if isinstance(provider, str) and _LABEL_RE.fullmatch(provider) else None
        model = model if isinstance(model, str) and _LABEL_RE.fullmatch(model) else None
        raw_turn_id = context.get("turn_id")
        turn_id = raw_turn_id if isinstance(raw_turn_id, str) and raw_turn_id else event_id
        private_turn_id = hashlib.sha256(f"{source_id}\0{turn_id}".encode()).hexdigest()
        usage_event_id = "hermes-" + hashlib.sha256("\0".join((
            source_id, "codex-app-server", turn_id, provider or "", model or "",
        )).encode()).hexdigest()
        self._connection.execute(
            """INSERT OR IGNORE INTO project_usage_events(
                event_id, project_id, goal_id, occurred_at, provider, model, usage_status,
                input_tokens, output_tokens, total_tokens, cache_read_tokens,
                cache_write_tokens, reasoning_tokens, turn_id, usage_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (usage_event_id, project_id, goal_id, occurred_at, provider, model,
             status, *(usage.get(key) for key in (
                 "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
                 "cache_write_tokens", "reasoning_tokens",
             )), private_turn_id, "codex_app_server"),
        )

    def record_agent_usage(
        self, session_id: str, turn_id: str, groups: list[Mapping[str, Any]],
    ) -> int:
        """Persist bounded provider-API usage only for an explicitly bound profile session."""
        if (
            not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id)
            or not isinstance(turn_id, str) or not _SESSION_ID_RE.fullmatch(turn_id)
            or type(groups) is not list or not groups or len(groups) > 50
        ):
            return 0

        normalized: list[dict[str, Any]] = []
        seen_models: set[tuple[str | None, str | None]] = set()
        token_names = (
            "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
            "cache_write_tokens", "reasoning_tokens",
        )
        required = {"provider", "model", "usage_status", *token_names}
        for group in groups:
            if not isinstance(group, Mapping) or set(group) != required:
                return 0
            provider = group["provider"]
            model = group["model"]
            if provider is not None and (
                not isinstance(provider, str) or not _LABEL_RE.fullmatch(provider)
            ):
                return 0
            if model is not None and (
                not isinstance(model, str) or not _LABEL_RE.fullmatch(model)
            ):
                return 0
            model_key = (provider, model)
            if model_key in seen_models:
                return 0
            seen_models.add(model_key)
            status = group["usage_status"]
            if not isinstance(status, str) or status not in {"reported", "unknown"}:
                return 0
            tokens = [group[name] for name in token_names]
            if status == "reported":
                if any(type(value) is not int or value < 0 or value > 10**12 for value in tokens):
                    return 0
            elif any(value is not None for value in tokens):
                return 0
            normalized.append({
                "provider": provider,
                "model": model,
                "usage_status": status,
                **dict(zip(token_names, tokens, strict=True)),
            })

        occurred_at = datetime.now(UTC).isoformat(timespec="microseconds")
        inserted = 0
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                bindings = self._connection.execute(
                    "SELECT project_id, goal_id, source_id FROM active_goal_bindings "
                    "WHERE session_id = ?", (session_id,),
                ).fetchall()
                if len(bindings) != 1:
                    self._connection.rollback()
                    return 0
                project_id, goal_id, source_id = bindings[0]
                private_turn_id = hashlib.sha256(
                    f"{source_id}\0{turn_id}".encode()
                ).hexdigest()
                for group in normalized:
                    stable_key = "\0".join((
                        source_id, "agent-api", turn_id,
                        group["provider"] or "", group["model"] or "",
                    )).encode()
                    event_id = "hermes-" + hashlib.sha256(stable_key).hexdigest()
                    cursor = self._connection.execute(
                        """INSERT OR IGNORE INTO project_usage_events(
                            event_id, project_id, goal_id, occurred_at, provider, model,
                            usage_status, input_tokens, output_tokens, total_tokens,
                            cache_read_tokens, cache_write_tokens, reasoning_tokens,
                            turn_id, usage_source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            event_id, project_id, goal_id, occurred_at,
                            group["provider"], group["model"], group["usage_status"],
                            *(group[name] for name in token_names), private_turn_id, "hermes_agent_api",
                        ),
                    )
                    inserted += max(cursor.rowcount, 0)
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return inserted

    def project_usage_totals(self, project_id: str) -> dict[str, int | str]:
        """Summarize local Hermes usage without returning session, goal, or event identifiers."""
        with self._lock:
            row = self._connection.execute(
                """WITH turn_usage AS (
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
                   SELECT (SELECT COUNT(*) FROM turn_usage),
                          (SELECT SUM(unknown_usage = 0) FROM turn_usage),
                          (SELECT SUM(unknown_usage = 1) FROM turn_usage),
                          token_totals.input_tokens, token_totals.output_tokens,
                          token_totals.total_tokens, token_totals.cache_read_tokens,
                          token_totals.cache_write_tokens, token_totals.reasoning_tokens
                   FROM token_totals""",
                (project_id, project_id),
            ).fetchone()
        return {
            "project_id": project_id,
            "source": "hermes_profile",
            "turns": int(row[0] or 0),
            "reported_turns": int(row[1] or 0),
            "unknown_turns": int(row[2] or 0),
            "input_tokens": int(row[3] or 0),
            "output_tokens": int(row[4] or 0),
            "total_tokens": int(row[5] or 0),
            "cache_read_tokens": int(row[6] or 0),
            "cache_write_tokens": int(row[7] or 0),
            "reasoning_tokens": int(row[8] or 0),
        }

    def project_usage_by_model(self, project_id: str) -> list[dict[str, int | str]]:
        """Return aggregate token counts by provider/model, without session-level identifiers."""
        with self._lock:
            rows = self._connection.execute(
                """SELECT provider, model, usage_status, COUNT(DISTINCT turn_id),
                          SUM(input_tokens), SUM(output_tokens), SUM(total_tokens)
                   FROM project_usage_events WHERE project_id = ?
                   GROUP BY provider, model, usage_status ORDER BY provider, model, usage_status""",
                (project_id,),
            ).fetchall()
        return [
            {
                "provider": row[0] or "unknown",
                "model": row[1] or "unknown",
                "usage_status": row[2],
                "turns": int(row[3] or 0),
                "input_tokens": int(row[4] or 0),
                "output_tokens": int(row[5] or 0),
                "total_tokens": int(row[6] or 0),
            }
            for row in rows
        ]

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
