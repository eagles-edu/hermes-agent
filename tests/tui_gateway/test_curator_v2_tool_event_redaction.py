"""Curator V2 tool payloads are not retained in TUI event replay."""

import json

from tui_gateway import server


def test_curator_v2_tool_lifecycle_events_redact_args_and_results(monkeypatch):
    sid = "curator-redaction-event-test"
    canary = "CURATOR_TUI_CANARY_63B2"
    events = []
    monkeypatch.setitem(
        server._sessions,
        sid,
        {"edit_snapshots": {}, "tool_started_at": {}, "tool_progress_mode": "verbose"},
    )
    monkeypatch.setattr(server, "_connector_lifecycle_is_stale", lambda *_args: False)
    monkeypatch.setattr(server, "_connector_tool_lifecycle", lambda *_args: False)
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda _sid: True)
    monkeypatch.setattr(server, "_tool_lifecycle_required_for_ui", lambda _name: False)
    monkeypatch.setattr(server, "_tool_result_needs_user", lambda _result: False)
    monkeypatch.setattr(server, "_tool_ctx", lambda *_args: canary)
    monkeypatch.setattr(server, "_tool_labels", lambda *_args: {"label": canary})
    monkeypatch.setattr(server, "_tool_args_text", lambda _args: canary)
    monkeypatch.setattr(server, "_tool_summary", lambda *_args: canary)
    monkeypatch.setattr(server, "_tool_result_text", lambda _result: canary)
    monkeypatch.setattr(server, "_prepare_tool_result_metadata", lambda *_args: {"inline_diff": canary})
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, event_sid, payload: events.append((event, event_sid, payload)),
    )

    name = "mcp__curator_v2__goal_report"
    args = {"goal_id": canary}
    result = json.dumps({"facts": [canary]})
    server._on_tool_start(sid, "call-1", name, args)
    server._on_tool_complete(sid, "call-1", name, args, result)

    assert [event[0] for event in events] == ["tool.start", "tool.complete"]
    serialized = json.dumps([event[2] for event in events], ensure_ascii=False)
    assert canary not in serialized
    assert events[0][2]["args"] == {"_redacted": "[redacted Curator V2 tool arguments]"}
    assert events[0][2]["context"] == "[redacted Curator V2 tool call]"
    assert "labels" not in events[0][2]
    assert events[1][2]["result"] == "[redacted Curator V2 tool result]"
    assert events[1][2]["result_text"] == "[redacted Curator V2 tool result]"
    assert "summary" not in events[1][2]
    assert "inline_diff" not in events[1][2]


def test_curator_v2_restart_preview_redacts_args_and_result(monkeypatch):
    canary = "CURATOR_RESTART_CANARY_149A"
    progress = []
    callbacks = server._preview_restart_callbacks("parent", "task")
    monkeypatch.setattr(server, "_tool_ctx", lambda *_args: canary)
    monkeypatch.setattr(server, "_tool_summary", lambda *_args: canary)
    monkeypatch.setattr(server, "_preview_tool_result_preview", lambda *_args: canary)
    monkeypatch.setattr(server, "_fmt_tool_duration", lambda _seconds: "1s")
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, sid, payload: progress.append(payload["text"]),
    )

    name = "mcp__curator_v2__goal_report"
    callbacks["tool_start_callback"]("call-1", name, {"goal_id": canary})
    callbacks["tool_complete_callback"]("call-1", name, {"goal_id": canary}, canary)

    assert progress
    assert canary not in "\n".join(progress)
    assert "[redacted Curator V2 tool arguments]" in progress[0]
    assert "[redacted Curator V2 tool result]" in progress[1]
