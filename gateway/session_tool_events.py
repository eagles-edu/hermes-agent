"""Bound and redact tool input/output projections sent to session viewers."""
import json

from agent.tool_argument_log_safety import is_curator_v2_tool

MAX_TOOL_EVENT_CHARS = 64 * 1024
_REDACTED_CURATOR_V2_ARGUMENTS = "[redacted Curator V2 tool arguments]"
_REDACTED_CURATOR_V2_RESULT = "[redacted Curator V2 tool result]"


def bounded_args(args):
    encoded = json.dumps(args, ensure_ascii=True, default=str)
    if len(encoded) <= MAX_TOOL_EVENT_CHARS:
        return args
    # Retain ordinary scalar paths/commands for editor locations even when a content field is large.
    result = {}
    remaining = MAX_TOOL_EVENT_CHARS // 2
    for key, value in args.items():
        if remaining <= 0 or len(result) >= 64:
            break
        name = str(key)[:256]
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True, default=str)
        text = text[:min(4096, remaining)]
        result[name] = text
        remaining -= len(json.dumps([name, text], ensure_ascii=True))
    return result


def viewer_args(tool_name, args):
    """Return a bounded viewer projection without exposing Curator V2 payloads."""
    if is_curator_v2_tool(tool_name):
        return {"_redacted": _REDACTED_CURATOR_V2_ARGUMENTS}
    return bounded_args(args)


def bounded_result(result, is_error):
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=True, default=str)
    if len(text) <= MAX_TOOL_EVENT_CHARS:
        return text
    # ACP reads errors from the result as well as the event flag; keep valid JSON after truncation.
    return json.dumps({'error' if is_error else 'output': text[:MAX_TOOL_EVENT_CHARS // 2], 'truncated': True})


def viewer_result(tool_name, result, is_error):
    """Return a bounded viewer result, redacting Curator V2 facts and evidence."""
    if is_curator_v2_tool(tool_name):
        return _REDACTED_CURATOR_V2_RESULT
    return bounded_result(result, is_error)
