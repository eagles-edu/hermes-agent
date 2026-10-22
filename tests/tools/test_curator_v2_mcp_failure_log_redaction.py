"""Curator V2 MCP transport failures must not log exception payloads."""

import logging

from tools import mcp_tool
from tools import mcp_tool_handlers


def test_curator_v2_mcp_failure_log_redacts_exception(caplog, monkeypatch):
    canary = "private-mcp-failure-detail-2ad8"

    monkeypatch.setattr(mcp_tool_handlers, "_trust_gate_check", lambda *_: None)
    monkeypatch.setattr(mcp_tool_handlers, "_check_circuit_breaker", lambda *_: None)
    monkeypatch.setattr(
        mcp_tool_handlers, "_acquire_call_server", lambda *_: (object(), None)
    )
    monkeypatch.setattr(mcp_tool_handlers, "_tool_is_read_only", lambda *_: True)
    monkeypatch.setattr(mcp_tool, "_bump_server_error", lambda *_: None)

    def dispatch(*args, **kwargs):
        on_final_failure = args[6]
        on_final_failure(RuntimeError(canary))
        return "tool call failed"

    monkeypatch.setattr(mcp_tool_handlers, "_dispatch", dispatch)
    handler = mcp_tool_handlers._make_tool_handler(
        "curator_v2", "goal_report", 1.0
    )

    with caplog.at_level(logging.ERROR, logger="tools.mcp_tool"):
        handler({})

    assert canary not in caplog.text
    assert "[redacted Curator V2 tool failure]" in caplog.text
