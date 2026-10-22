"""Curator V2 dispatcher exceptions must not escape into diagnostics or hooks."""

import json
import logging

from model_tools import handle_function_call


def test_curator_v2_dispatch_exception_is_redacted(caplog, monkeypatch):
    canary = "private-central-dispatch-error-9c31"
    hook_calls = []

    monkeypatch.setattr(
        "model_tools._execute_tool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(canary)),
    )
    monkeypatch.setattr("model_tools._pre_dispatch_guards", lambda _n, args, *_a: (args, None))
    monkeypatch.setattr(
        "model_tools._emit_post_tool_call_hook",
        lambda **kwargs: hook_calls.append(kwargs),
    )

    with caplog.at_level(logging.ERROR, logger="model_tools"):
        result = json.loads(
            handle_function_call(
                "mcp__curator_v2__goal_report",
                {},
                skip_tool_request_middleware=True,
            )
        )

    assert canary not in caplog.text
    assert "[redacted Curator V2 tool failure]" in caplog.text
    assert canary not in json.dumps(result)
    assert "Curator V2 tool failed." in json.dumps(result)
    assert hook_calls[0]["error_message"] == "Curator V2 tool failed."


def test_curator_v2_request_middleware_exception_log_is_redacted(caplog, monkeypatch):
    import model_tools

    canary = "private-request-middleware-error-6a80"
    monkeypatch.setattr(
        "hermes_cli.middleware.apply_tool_request_middleware",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(canary)),
    )

    with caplog.at_level(logging.DEBUG, logger="model_tools"):
        model_tools._apply_request_middleware(
            "mcp__curator_v2__goal_report", {}, model_tools._CallIds(), []
        )

    assert canary not in caplog.text
    assert "[redacted Curator V2 tool failure]" in caplog.text


def test_curator_v2_post_tool_hook_exception_log_is_redacted(caplog, monkeypatch):
    import model_tools

    canary = "private-post-hook-error-94c1"
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(canary)),
    )

    with caplog.at_level(logging.DEBUG, logger="model_tools"):
        model_tools._emit_post_tool_call_hook(
            function_name="mcp__curator_v2__goal_report",
            function_args={},
            result="{}",
        )

    assert canary not in caplog.text
    assert "[redacted Curator V2 tool failure]" in caplog.text
