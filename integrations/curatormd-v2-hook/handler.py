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
_SUMMARY_PATH = re.compile(r"^[A-Za-z0-9_./ @+-]{1,256}$")
_SUMMARY_CHECKS = frozenset(
    {"scripts/check", "scripts/run_tests.sh", "git diff --check", "pytest", "npm test"}
)
_SUMMARY_CHANGES = frozenset({"add", "update", "delete", "rename"})
_SUMMARY_PRIVATE_NAMES = frozenset({
    ".env", ".npmrc", ".pypirc", ".netrc", "credentials", "secrets",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "authorized_keys",
    "known_hosts", "application_default_credentials.json",
})
_SUMMARY_PRIVATE_DIRECTORIES = frozenset(
    {".aws", ".azure", ".docker", ".gnupg", ".kube", ".ssh"}
)
_SUMMARY_PRIVATE_COMPONENT = re.compile(
    r"(?:^|[._-])(?:\.env|\.secret|secret|credential|passphrase|password|passwd|pwd|token|"
    r"api[_-]?(?:key|secret|token)|(?:access|refresh)[_-]?(?:key|token)|"
    r"(?:auth|secret|session)[_-]?(?:key|token)|bearer[_-]?(?:token|key)|"
    r"oauth[_-]?client[_-]?secret|"
    r"aws[_-]?(?:secret[_-]?access[_-]?key|access[_-]?key(?:[_-]?id)?)|"
    r"client[_-]?secret|private[_-]?key|authorization|bearer|cookie)s?(?:$|[._-])",
    re.IGNORECASE,
)
_SUMMARY_PRIVATE_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b",
    r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b",
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b",
    r"\bAIza[0-9A-Za-z_-]{35}\b",
    r"\b(?:TXT(?:\s+(?:record|value))?|DNS\s+(?:challenge|TXT)"
    r"(?:\s+(?:value|code|token))?)\s*[:=]\s*[\"']?[A-Za-z0-9_-]{32,}\b",
    r"\b(?:manual\s+)?DNS\s+(?:TXT\s+)?(?:code|challenge|value)"
    r"\s*(?::|=|\bis\b)\s*[\"']?[A-Za-z0-9_-]{12,}\b",
    r"\b(?:manual[-_ ]dns|dns)[-_ ](?:txt|challenge|code|token|value)"
    r"[-_]+[A-Za-z0-9_-]{12,}\b",
    r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|secret|password|passwd|pwd|dns[_-]?txt)"
    r"\s*[=:]\s*[^/\\\s]{4,}",
))


def _text(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    )


def _private_summary_path(path: str) -> bool:
    parts = tuple(part.lower() for part in path.split("/"))
    basename = parts[-1] if parts else ""
    return (
        basename in _SUMMARY_PRIVATE_NAMES
        or basename.endswith((".key", ".pem", ".p12", ".pfx", ".jks", ".keystore"))
        or any(part in _SUMMARY_PRIVATE_DIRECTORIES for part in parts)
        or any(_SUMMARY_PRIVATE_COMPONENT.search(part) for part in parts)
        or any(pattern.search(path) for pattern in _SUMMARY_PRIVATE_PATTERNS)
    )


def _valid_work_summary(value: object) -> bool:
    """Accept only the bounded summary schema, rejecting raw fields and secrets."""
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        return False
    try:
        if len(value.encode("utf-8")) > 4096:
            return False
        summary = json.loads(value)
    except (UnicodeEncodeError, TypeError, ValueError):
        return False
    if not isinstance(summary, dict) or set(summary) != {
        "schema_version", "repository_fingerprint", "changed_files",
        "omitted_files", "checks", "omitted_checks",
    }:
        return False
    if (
        type(summary.get("schema_version")) is not int
        or summary["schema_version"] != 1
        or not isinstance(summary.get("repository_fingerprint"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", summary["repository_fingerprint"])
    ):
        return False
    changed_files, checks = summary.get("changed_files"), summary.get("checks")
    if (
        not isinstance(changed_files, list) or len(changed_files) > 16
        or not isinstance(checks, list) or len(checks) > 8
        or type(summary.get("omitted_files")) is not int
        or not 0 <= summary["omitted_files"] <= 1_000_000
        or type(summary.get("omitted_checks")) is not int
        or not 0 <= summary["omitted_checks"] <= 1_000_000
    ):
        return False
    for item in changed_files:
        if not isinstance(item, dict) or set(item) != {"path", "change"}:
            return False
        path, change = item.get("path"), item.get("change")
        if (
            not isinstance(path, str) or not _SUMMARY_PATH.fullmatch(path)
            or path.startswith("/") or ".." in path.split("/")
            or _private_summary_path(path)
            or not isinstance(change, str) or change not in _SUMMARY_CHANGES
        ):
            return False
    for item in checks:
        if not isinstance(item, dict) or set(item) != {"name", "exit_code"}:
            return False
        if (
            not isinstance(item.get("name"), str)
            or item["name"] not in _SUMMARY_CHECKS
            or type(item.get("exit_code")) is not int
            or not 0 <= item["exit_code"] <= 255
        ):
            return False
    return True


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
    if summary is not None and (
        event_type != "codex:turn:end" or not _valid_work_summary(summary)
    ):
        # The summary is optional. Drop malformed or sensitive metadata but
        # still deliver the bounded lifecycle event so the outbox can advance.
        event_context.pop("work_summary_json", None)

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
    except Exception:  # health: allow BLE001 -- retry failed delivery without logging payload details.
        return False
    return _matching_receipt(
        response,
        project_id=context["project_id"],
        event_id=payload["event_id"],
    )
