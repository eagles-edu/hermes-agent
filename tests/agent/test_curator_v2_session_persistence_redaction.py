"""Curator V2 payloads stay out of durable Hermes transcript rows."""

from __future__ import annotations

import json

from agent.session_persistence import _db_flush_row


def test_curator_v2_tool_arguments_are_redacted_only_in_durable_projection():
    secret = "CURATOR_V2_TRANSCRIPT_ARGUMENT_CANARY"
    original_arguments = json.dumps({"bullet": secret})
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-curator",
                "type": "function",
                "function": {
                    "name": "mcp__curator_v2__fact_record",
                    "arguments": original_arguments,
                },
            }
        ],
    }

    durable = _db_flush_row(None, message, False)

    assert durable["tool_calls"][0]["id"] == "call-curator"
    assert durable["tool_calls"][0]["function"]["arguments"] == '{"_redacted":true}'
    assert secret not in json.dumps(durable)
    assert message["tool_calls"][0]["function"]["arguments"] == original_arguments


def test_curator_v2_tool_results_are_redacted_but_other_tools_are_preserved():
    secret = "CURATOR_V2_TRANSCRIPT_RESULT_CANARY"
    result_message = {
        "role": "tool",
        "name": "mcp__curator_v2__goal_status",
        "tool_call_id": "call-curator",
        "content": json.dumps({"fact": secret}),
        "api_content": secret,
    }
    other_message = {
        "role": "tool",
        "name": "mcp__ordinary__lookup",
        "tool_call_id": "call-other",
        "content": "ordinary result stays available",
    }

    durable_curator = _db_flush_row(None, result_message, False)
    durable_other = _db_flush_row(None, other_message, False)

    assert durable_curator["content"] == "[redacted Curator V2 tool result]"
    assert durable_curator["api_content"] is None
    assert secret not in json.dumps(durable_curator)
    assert result_message["content"] == json.dumps({"fact": secret})
    assert durable_other["content"] == "ordinary result stays available"
