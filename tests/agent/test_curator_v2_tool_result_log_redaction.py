"""Curator V2 tool output must not enter Hermes diagnostic logs."""

import logging
from types import SimpleNamespace

from agent.tool_argument_log_safety import (
    tool_failure_log_value,
    tool_result_log_value,
)


def test_curator_v2_result_is_redacted_before_logging():
    canary = "private-tool-result-log-73ac"

    logged_value = tool_result_log_value(
        "mcp__curator_v2__goal_report", canary
    )

    assert canary not in str(logged_value)
    assert logged_value == "[redacted Curator V2 tool result]"


def test_non_curator_result_remains_available_for_existing_logs():
    result = "public diagnostic result"

    assert tool_result_log_value("web_search", result) == result


def test_curator_v2_failure_detail_is_redacted_before_logging():
    canary = "private-tool-failure-log-a5c1"

    logged_value = tool_failure_log_value(
        "mcp__curator_v2__goal_report", RuntimeError(canary)
    )

    assert canary not in str(logged_value)
    assert logged_value == "[redacted Curator V2 tool failure]"


def test_non_curator_failure_remains_available_for_existing_logs():
    failure = RuntimeError("public transport detail")

    assert tool_failure_log_value("mcp__web_search__search", failure) is failure


def test_curator_v2_executor_failures_redact_exception_and_result_logs(caplog, monkeypatch):
    from agent import tool_executor

    canary = "private-executor-failure-4b2c"
    tool_name = "mcp__curator_v2__goal_report"
    ref = tool_executor._ToolCallRef(tool_name, {}, "task", "call", [])

    def fail_before_dispatch(*_args, **_kwargs):
        raise RuntimeError(canary)

    monkeypatch.setattr(
        tool_executor, "_run_agent_tool_execution_middleware", fail_before_dispatch
    )
    monkeypatch.setattr(tool_executor, "_emit_terminal_post_tool_call", lambda *_a, **_k: None)
    batch = tool_executor._ConcurrentBatch(SimpleNamespace(), [], "task", [], None)

    with caplog.at_level(logging.INFO, logger="agent.tool_executor"):
        batch._dispatch_worker(
            0, ref, None, tool_executor._WorkerStartOnce(batch.gate, 0, tool_name)
        )

    assert canary not in caplog.text
    assert "[redacted Curator V2 tool failure]" in caplog.text
    assert "[redacted Curator V2 tool result]" in caplog.text


def test_curator_v2_sequential_executor_failure_redacts_exception(caplog, monkeypatch):
    from agent import tool_executor

    canary = "private-sequential-failure-26b1"
    tool_name = "mcp__curator_v2__goal_report"
    ref = tool_executor._ToolCallRef(tool_name, {}, "task", "call", [])
    dispatch = tool_executor._SequentialDispatch(
        execute=lambda _args: None,
        error_result=lambda exc: str(exc),
        error_log="handle_function_call raised for %s: %s",
        finish_spinner=False,
    )

    def fail_before_dispatch(*_args, **_kwargs):
        raise RuntimeError(canary)

    monkeypatch.setattr(
        tool_executor, "_run_sequential_tool_execution_middleware", fail_before_dispatch
    )

    with caplog.at_level(logging.ERROR, logger="agent.tool_executor"):
        tool_executor._run_sequential_call(
            SimpleNamespace(),
            dispatch,
            ref,
            scope_block=None,
            messages=[],
            remaining_calls=[],
            display_index=1,
            tool_start_time=0.0,
        )

    assert canary not in caplog.text
    assert "[redacted Curator V2 tool failure]" in caplog.text
