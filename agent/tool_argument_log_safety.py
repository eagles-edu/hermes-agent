"""Keep Curator V2 tool payloads out of Hermes diagnostic logs."""

from __future__ import annotations

from typing import Any

_CURATOR_V2_PREFIX = "mcp__curator_v2__"
_REDACTED_CURATOR_V2_ARGUMENTS = "[redacted Curator V2 tool arguments]"
_REDACTED_CURATOR_V2_RESULT = "[redacted Curator V2 tool result]"
_REDACTED_CURATOR_V2_FAILURE = "[redacted Curator V2 tool failure]"


def is_curator_v2_tool(tool_name: str) -> bool:
    """Return whether a tool name belongs to the Curator V2 MCP namespace."""

    return tool_name.casefold().startswith(_CURATOR_V2_PREFIX)


def tool_arguments_log_excerpt(tool_name: str, raw_args: Any, max_chars: int) -> str:
    """Return a bounded log excerpt, redacting Curator V2 payloads entirely."""
    if is_curator_v2_tool(tool_name):
        return _REDACTED_CURATOR_V2_ARGUMENTS
    if isinstance(raw_args, str):
        return raw_args[:max_chars]
    return repr(raw_args)[:max_chars]


def tool_result_log_value(tool_name: str, result: Any) -> Any:
    """Keep Curator V2 tool output out of diagnostics while preserving other results."""

    if is_curator_v2_tool(tool_name):
        return _REDACTED_CURATOR_V2_RESULT
    return result


def tool_failure_log_value(tool_name: str, failure: Any) -> Any:
    """Keep Curator V2 failure details out of diagnostics."""

    if is_curator_v2_tool(tool_name):
        return _REDACTED_CURATOR_V2_FAILURE
    return failure
