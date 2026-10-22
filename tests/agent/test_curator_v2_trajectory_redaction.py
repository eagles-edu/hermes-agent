"""Curator V2 tool arguments must not enter saved trajectory samples."""

import json

from agent.trajectory import _trajectory_tool_call_turn, _trajectory_tool_responses


def test_curator_v2_tool_arguments_are_redacted_in_trajectory():
    canary = "private-trajectory-argument-4f2b"
    message = {
        "tool_calls": [
            {
                "function": {
                    "name": "mcp__curator_v2__fact_record",
                    "arguments": json.dumps({"bullet": canary}),
                }
            }
        ]
    }

    trajectory_turn = _trajectory_tool_call_turn(message)

    assert canary not in trajectory_turn
    assert '"name": "mcp__curator_v2__fact_record"' in trajectory_turn
    assert '"arguments": {"_redacted": true}' in trajectory_turn


def test_non_curator_tool_arguments_remain_in_trajectory():
    message = {
        "tool_calls": [
            {
                "function": {
                    "name": "web_search",
                    "arguments": json.dumps({"query": "public query"}),
                }
            }
        ]
    }

    trajectory_turn = _trajectory_tool_call_turn(message)

    assert '"arguments": {"query": "public query"}' in trajectory_turn


def test_curator_v2_tool_results_are_redacted_in_trajectory():
    canary = "private-trajectory-result-b91d"
    assistant_message = {
        "tool_calls": [
            {"function": {"name": "mcp__curator_v2__goal_report"}}
        ]
    }
    tool_messages = [
        {"role": "tool", "tool_call_id": "call-1", "content": canary}
    ]

    responses, next_index = _trajectory_tool_responses(
        assistant_message, tool_messages, 0
    )

    assert canary not in "\n".join(responses)
    assert '"content": {"_redacted": true}' in responses[0]
    assert '"name": "mcp__curator_v2__goal_report"' in responses[0]
    assert next_index == 1


def test_non_curator_tool_results_remain_in_trajectory():
    assistant_message = {
        "tool_calls": [{"function": {"name": "web_search"}}]
    }
    tool_messages = [
        {"role": "tool", "tool_call_id": "call-2", "content": '{"title":"Public"}'}
    ]

    responses, _ = _trajectory_tool_responses(assistant_message, tool_messages, 0)

    assert '"content": {"title": "Public"}' in responses[0]
