"""Forward bound Codex lifecycle events to the profile's Curator V2 server."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any


_EVENTS = frozenset({"codex:turn:start", "codex:turn:end"})
_START_CONTEXT_FIELDS = frozenset({"attempt_id", "session_id"})
_END_CONTEXT_FIELDS = _START_CONTEXT_FIELDS | frozenset(
    {"turn_id", "outcome", "failure_kind", "work_summary_json"}
)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def _text(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    )


def _payload(event_type: str, context: Mapping[str, Any]) -> dict[str, Any] | None:
    required = (
        "project_id",
        "goal_id",
        "event_id",
        "source_id",
        "occurred_at",
    )
    if any(not _text(context.get(key), 256) for key in required):
        return None
    if any(not _ID.fullmatch(context[key]) for key in required[:-1]):
        return None
    if type(context.get("source_sequence")) is not int or context["source_sequence"] < 1:
        return None

    allowed_fields = (
        _START_CONTEXT_FIELDS if event_type == "codex:turn:start" else _END_CONTEXT_FIELDS
    )
    event_context = {
        key: context[key]
        for key in allowed_fields
        if key in context
    }
    if not _text(event_context.get("attempt_id"), 256) or not _text(
        event_context.get("session_id"), 256
    ):
        return None
    if not _ID.fullmatch(event_context["attempt_id"]) or not _ID.fullmatch(
        event_context["session_id"]
    ):
        return None
    if event_type == "codex:turn:end":
        outcome = event_context.get("outcome")
        if (
            not _text(event_context.get("turn_id"), 256)
            or not _ID.fullmatch(event_context["turn_id"])
            or not isinstance(outcome, str)
            or outcome not in {"completed", "interrupted", "failed"}
        ):
            return None
        failure_kind = event_context.get("failure_kind")
        if failure_kind is not None and (
            not isinstance(failure_kind, str)
            or failure_kind not in {"exception", "postprocess_exception", "codex_error"}
        ):
            return None
    summary = event_context.get("work_summary_json")
    if summary is not None:
        if (
            event_type != "codex:turn:end"
            or not _text(summary, 4096)
            or len(summary.encode("utf-8")) > 4096
        ):
            return None
        try:
            decoded = json.loads(summary)
        except (TypeError, ValueError):
            return None
        if not isinstance(decoded, dict) or decoded.get("schema_version") != 1:
            return None

    return {
        "event_type": event_type,
        "goal_id": context["goal_id"],
        "event_id": context["event_id"],
        "source_id": context["source_id"],
        "source_sequence": context["source_sequence"],
        "occurred_at": context["occurred_at"],
        "context": event_context,
    }


def _matching_receipt(
    response: object, *, project_id: str, event_id: str
) -> bool:
    if not isinstance(response, Mapping) or response.get("ok") is not True:
        return False
    if response.get("truncated") is True:
        return False
    result = response.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            return False
    if not isinstance(result, Mapping):
        return False
    receipt = result.get("receipt", result)
    status = receipt.get("status") if isinstance(receipt, Mapping) else None
    return (
        isinstance(receipt, Mapping)
        and receipt.get("project_id") == project_id
        and receipt.get("event_id") == event_id
        and isinstance(status, str)
        and status in {"processed", "duplicate"}
    )


def handle(event_type: str, context: Mapping[str, Any], *, ctx: Any) -> bool | None:
    """Acknowledge only the exact durable receipt; failed or mismatched calls retry."""
    if not isinstance(event_type, str) or event_type not in _EVENTS:
        return None
    if not isinstance(context, Mapping):
        return False
    payload = _payload(event_type, context)
    if payload is None:
        return False
    try:
        response = ctx.call_mcp(
            "curator_v2",
            "hermes_codex_event",
            payload,
            timeout=30,
            session_id=payload["context"]["session_id"],
            hermes_durable_event=True,
        )
    except Exception:
        return False
    return _matching_receipt(
        response,
        project_id=context["project_id"],
        event_id=payload["event_id"],
    )
