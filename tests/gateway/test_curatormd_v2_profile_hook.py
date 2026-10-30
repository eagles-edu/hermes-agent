"""Behavior tests for the profile-local Curator V2 durable hook."""

import importlib.util
from pathlib import Path
import sys


_HANDLER_PATH = (
    Path(__file__).resolve().parents[2]
    / "integrations"
    / "curatormd-v2-hook"
    / "handler.py"
)
_SPEC = importlib.util.spec_from_file_location("curatormd_v2_profile_hook", _HANDLER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
handle = _MODULE.handle


class _HookContext:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def call_mcp(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


def _event_context(**overrides):
    value = {
        "project_id": "hermes-core-v2-alpha",
        "goal_id": "goal-alpha",
        "event_id": "event-alpha",
        "source_id": "source-alpha",
        "source_sequence": 1,
        "occurred_at": "2026-10-30T00:00:00+00:00",
        "attempt_id": "attempt-alpha",
        "session_id": "session-alpha",
        "turn_id": "turn-alpha",
        "outcome": "completed",
        "work_summary_json": (
            '{"schema_version":1,"repository_fingerprint":"'
            + "a" * 64
            + '"}'
        ),
        "prompt": "must not be forwarded",
        "command_output": "must not be forwarded",
    }
    value.update(overrides)
    return value


def test_profile_hook_sends_only_bounded_fields_and_requires_matching_receipt():
    context = _HookContext({
        "ok": True,
        "truncated": False,
        "result": {
            "project_id": "hermes-core-v2-alpha",
            "event_id": "event-alpha",
            "status": "processed",
        },
    })

    acknowledged = handle("codex:turn:end", _event_context(), ctx=context)

    assert acknowledged is True
    args, kwargs = context.calls[0]
    assert args[0:2] == ("curator_v2", "hermes_codex_event")
    event = args[2]
    assert set(event) == {
        "event_type", "goal_id", "event_id", "source_id",
        "source_sequence", "occurred_at", "context",
    }
    assert event["event_id"] == "event-alpha"
    assert event["context"]["work_summary_json"].startswith('{"schema_version"')
    assert "prompt" not in event and "command_output" not in event
    assert kwargs == {
        "timeout": 30,
        "session_id": "session-alpha",
        "hermes_durable_event": True,
    }

    start_context = _HookContext({
        "ok": True,
        "truncated": False,
        "result": {
            "project_id": "hermes-core-v2-alpha",
            "event_id": "event-alpha",
            "status": "processed",
        },
    })
    assert handle("codex:turn:start", _event_context(), ctx=start_context) is True
    start_event = start_context.calls[0][0][2]
    assert set(start_event["context"]) == {"attempt_id", "session_id"}


def test_profile_hook_does_not_acknowledge_a_different_or_failed_receipt():
    context = _HookContext({
        "ok": True,
        "truncated": False,
        "result": {
            "project_id": "hermes-core-v2-alpha",
            "event_id": "different-event",
            "status": "processed",
        },
    })
    assert handle("codex:turn:start", _event_context(), ctx=context) is False

    context.response = {"ok": False, "error": "read denied"}
    assert handle("codex:turn:start", _event_context(), ctx=context) is False
    context.response = {
        "ok": True,
        "result": {
            "project_id": "hermes-core-v2-alpha",
            "event_id": "event-alpha",
            "status": [],
        },
    }
    assert handle("codex:turn:start", _event_context(), ctx=context) is False


def test_profile_hook_rejects_malformed_events_without_calling_curator():
    context = _HookContext({"ok": True, "result": {}})
    assert handle("codex:turn:start", _event_context(source_sequence=True), ctx=context) is False
    assert handle("codex:turn:end", _event_context(work_summary_json="raw text"), ctx=context) is False
    assert handle("codex:turn:end", _event_context(outcome=[]), ctx=context) is False
    assert handle("agent:end", _event_context(), ctx=context) is None
    assert context.calls == []
