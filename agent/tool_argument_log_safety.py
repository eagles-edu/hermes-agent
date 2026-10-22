"""Keep Curator V2 tool payloads out of Hermes diagnostic logs."""

from __future__ import annotations

from typing import Any

_CURATOR_V2_PREFIX = "mcp__curator_v2__"
_REDACTED_CURATOR_V2_ARGUMENTS = "[redacted Curator V2 tool arguments]"


def tool_arguments_log_excerpt(tool_name: str, raw_args: Any, max_chars: int) -> str:
    """Return a bounded log excerpt, redacting Curator V2 payloads entirely."""
    if tool_name.casefold().startswith(_CURATOR_V2_PREFIX):
        return _REDACTED_CURATOR_V2_ARGUMENTS
    if isinstance(raw_args, str):
        return raw_args[:max_chars]
    return repr(raw_args)[:max_chars]
