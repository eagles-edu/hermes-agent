"""Curator V2 tool arguments must not enter Hermes diagnostic logs."""

import logging
from types import SimpleNamespace

from agent.message_sanitization import _repair_tool_call_arguments
from agent.tool_dispatch_helpers import _batch_admission
from agent.tool_argument_log_safety import tool_arguments_log_excerpt


def test_curator_v2_tool_argument_excerpt_is_redacted() -> None:
    canary = "private-curator-fact-8f5c"

    excerpt = tool_arguments_log_excerpt(
        "mcp__curator_v2__goal_record", f'{{"fact":"{canary}"}}', 200,
    )

    assert canary not in excerpt
    assert excerpt == "[redacted Curator V2 tool arguments]"


def test_non_curator_tool_argument_excerpt_remains_bounded() -> None:
    raw_args = '{"command":"status","extra":"ignored"}'

    assert tool_arguments_log_excerpt("terminal", raw_args, 12) == raw_args[:12]


def test_repair_warning_redacts_curator_v2_arguments(caplog) -> None:
    canary = "private-curator-fact-8f5c"
    malformed = f'{{"fact":"{canary}",}}'

    repaired = _repair_tool_call_arguments(malformed, "MCP__CURATOR_V2__goal_record")

    assert canary not in caplog.text
    assert "[redacted Curator V2 tool arguments]" in caplog.text
    assert repaired == f'{{"fact":"{canary}"}}'


def test_batch_admission_debug_log_redacts_invalid_curator_arguments(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    canary = "private-curator-fact-8f5c"
    tool_call = SimpleNamespace(
        function=SimpleNamespace(
            name="mcp__curator_v2__goal_record",
            arguments=f'{{"fact":"{canary}"',
        ),
    )

    assert _batch_admission(tool_call, execution_cwd=None) is None

    assert canary not in caplog.text
    assert "[redacted Curator V2 tool arguments]" in caplog.text
