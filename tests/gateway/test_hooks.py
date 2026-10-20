"""Tests for gateway/hooks.py — event hook system."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.hooks import HookContext, HookRegistry


def _create_hook(hooks_dir, hook_name, events, handler_code):
    """Helper to create a hook directory with HOOK.yaml and handler.py."""
    hook_dir = hooks_dir / hook_name
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text(
        f"name: {hook_name}\n"
        f"description: Test hook\n"
        f"events: {events}\n"
    )
    (hook_dir / "handler.py").write_text(handler_code)
    return hook_dir




def _patch_no_builtins(reg):
    """Suppress built-in hook registration so tests only exercise user-hook discovery."""
    return patch.object(reg, "_register_builtin_hooks")


class TestDiscoverAndLoad:
    def test_loads_valid_hook(self, tmp_path):
        _create_hook(tmp_path, "my-hook", '["agent:start"]',
                      "def handle(event_type, context):\n    pass\n")

        reg = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path), _patch_no_builtins(reg):
            reg.discover_and_load()

        assert len(reg.loaded_hooks) == 1
        assert reg.loaded_hooks[0]["name"] == "my-hook"
        assert "agent:start" in reg.loaded_hooks[0]["events"]


    def test_skips_no_events(self, tmp_path):
        hook_dir = tmp_path / "empty-hook"
        hook_dir.mkdir()
        (hook_dir / "HOOK.yaml").write_text("name: empty\nevents: []\n")
        (hook_dir / "handler.py").write_text("def handle(e, c): pass\n")

        reg = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path), _patch_no_builtins(reg):
            reg.discover_and_load()

        assert len(reg.loaded_hooks) == 0


class TestEmit:

    @pytest.mark.asyncio
    async def test_hook_receives_public_context_only_when_declared(self, tmp_path):
        _create_hook(
            tmp_path,
            "ctx-hook",
            '["codex:turn:start"]',
            "seen = []\n"
            "def handle(event_type, context, *, ctx):\n"
            "    seen.append((event_type, ctx))\n"
            "    return True\n",
        )

        reg = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path), _patch_no_builtins(reg):
            reg.discover_and_load()
        await reg.emit("codex:turn:start", {"session_id": "session-1"})

        seen = reg._handlers["codex:turn:start"][0].__globals__["seen"]
        assert len(seen) == 1
        assert isinstance(seen[0][1], HookContext)
        assert seen[0][1].hook_name == "ctx-hook"

    def test_hook_context_call_mcp_uses_scoped_allowlist_and_host_request_context(self):
        from tools import mcp_tool_handlers

        call = {}
        with patch(
            "hermes_cli.config.load_config",
            return_value={
                "hooks": {"entries": {"curator": {"mcp_allowlist": ["curator-mcp"]}}}
            },
        ), patch.object(
            mcp_tool_handlers,
            "_make_tool_handler",
            side_effect=lambda server, tool, timeout: lambda args, **kwargs: (
                call.update(
                    server=server,
                    tool=tool,
                    timeout=timeout,
                    arguments=args,
                    request_context=kwargs,
                )
                or '{"result": "receipt"}'
            ),
        ):
            result = HookContext("curator").call_mcp(
                "curator-mcp",
                "hermes_codex_event",
                {"event_id": "event-1"},
                session_id="session-1",
                hermes_durable_event=True,
            )

        assert result == {"ok": True, "result": "receipt"}
        assert call == {
            "server": "curator-mcp",
            "tool": "hermes_codex_event",
            "timeout": 30.0,
            "arguments": {"event_id": "event-1"},
            "request_context": {
                "session_id": "session-1",
                "hermes_durable_event": True,
            },
        }

    def test_hook_context_mcp_is_default_deny(self):
        with patch("hermes_cli.config.load_config", return_value={}):
            with pytest.raises(PermissionError, match="hooks.entries.curator.mcp_allowlist"):
                HookContext("curator").call_mcp("curator-mcp", "hermes_codex_event")

    @pytest.mark.asyncio
    async def test_emit_calls_async_handler(self, tmp_path):
        results = []

        hook_dir = tmp_path / "async-hook"
        hook_dir.mkdir()
        (hook_dir / "HOOK.yaml").write_text(
            "name: async-hook\nevents: ['agent:end']\n"
        )
        (hook_dir / "handler.py").write_text(
            "import asyncio\n"
            "results = []\n"
            "async def handle(event_type, context):\n"
            "    results.append(event_type)\n"
        )

        reg = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path):
            reg.discover_and_load()

        handler_fn = reg._handlers["agent:end"][0]
        handler_fn.__globals__["results"] = results

        await reg.emit("agent:end", {})
        assert "agent:end" in results

    @pytest.mark.asyncio
    async def test_wildcard_matching(self, tmp_path):
        results = []

        _create_hook(tmp_path, "wildcard-hook", '["command:*"]',
                      "results = []\n"
                      "def handle(event_type, context):\n"
                      "    results.append(event_type)\n")

        reg = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path):
            reg.discover_and_load()

        handler_fn = reg._handlers["command:*"][0]
        handler_fn.__globals__["results"] = results

        await reg.emit("command:reset", {})
        assert "command:reset" in results

    @pytest.mark.asyncio
    async def test_codex_durable_delivery_survives_registry_restart_and_ack(self, tmp_path):
        hook_dir = tmp_path / "curator"
        hook_dir.mkdir()
        (hook_dir / "HOOK.yaml").write_text(
            "name: curator\n"
            "events: ['codex:turn:start']\n"
            "durable_events: ['codex:turn:start']\n"
        )
        (hook_dir / "handler.py").write_text(
            "calls = []\n"
            "def handle(event_type, context):\n"
            "    calls.append((event_type, dict(context)))\n"
            "    return globals().get('ack', False)\n"
        )

        first = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path):
            first.discover_and_load()
        handler = first._durable_handlers["codex:turn:start"][0]
        first_calls = handler.__globals__["calls"]
        assert first._outbox.bind_goal("project-1", "session-1", "goal-1")
        context = {
            "attempt_id": "attempt-1",
            "session_id": "session-1",
            "model": "must-not-be-retained",
            "provider": "must-not-be-retained",
            "cwd": "/private/repository",
        }
        queued = first.queue_durable_event("codex:turn:start", context)
        assert queued is not None
        assert queued["source_sequence"] == 1
        assert "model" not in queued["context"]
        assert "provider" not in queued["context"]
        assert "cwd" not in queued["context"]
        queued_second = first.queue_durable_event(
            "codex:turn:start", {"attempt_id": "attempt-2", "session_id": "session-1"}
        )
        assert queued_second is not None and queued_second["source_sequence"] == 2
        assert first.queue_durable_event(
            "codex:turn:start", {"attempt_id": "unbound", "session_id": "other-session"}
        ) is None

        # First attempt is not explicitly acknowledged, so it remains for replay.
        await first.emit_durable(queued)
        assert first._outbox.pending() == [queued, queued_second]
        first._outbox.close()

        (hook_dir / "handler.py").write_text(
            "ack = True\n"
            "calls = []\n"
            "def handle(event_type, context):\n"
            "    calls.append((event_type, dict(context)))\n"
            "    return ack\n"
        )
        second = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path):
            second.discover_and_load()
            await second.emit("agent:start", {})
        assert second._outbox.pending() == []
        assert [call[1]["event_id"] for call in first_calls] == [queued["event_id"]]
        calls = second._durable_handlers["codex:turn:start"][0].__globals__["calls"]
        assert [call[1]["event_id"] for call in calls] == [queued["event_id"], queued_second["event_id"]]
        assert [call[1]["source_sequence"] for call in calls] == [1, 2]
        second._outbox.close()

    @pytest.mark.asyncio
    async def test_codex_durable_delivery_uses_managed_mcp_hook_context(self, tmp_path):
        """A persisted Codex event reaches Hermes' managed MCP session before ack."""
        from hermes_constants import get_hermes_home, profile_name_for_home
        from tools import mcp_tool

        mcp_result = SimpleNamespace(
            content=[],
            isError=False,
            structuredContent={"status": "processed"},
            _meta=None,
        )
        session = MagicMock()
        session.call_tool = AsyncMock(return_value=mcp_result)
        server = SimpleNamespace(session=session, _rpc_lock=None)

        def run_on_mcp_loop(coro_or_factory, timeout=30):
            coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever)
            thread.start()

            async def run_call():
                if server._rpc_lock is None:
                    server._rpc_lock = asyncio.Lock()
                return await coro

            try:
                return asyncio.run_coroutine_threadsafe(run_call(), loop).result(timeout)
            finally:
                loop.call_soon_threadsafe(loop.stop)
                thread.join()
                loop.close()

        hook_dir = tmp_path / "curator"
        hook_dir.mkdir()
        (hook_dir / "HOOK.yaml").write_text(
            "name: curator\n"
            "events: ['codex:turn:start']\n"
            "durable_events: ['codex:turn:start']\n"
        )
        (hook_dir / "handler.py").write_text(
            "observed = []\n"
            "def handle(event_type, context, *, ctx):\n"
            "    response = ctx.call_mcp(\n"
            "        'curator-mcp', 'hermes_codex_event',\n"
            "        {'event_type': event_type, 'project_id': context['project_id'],\n"
            "         'goal_id': context['goal_id'], 'event_id': context['event_id'],\n"
            "         'source_id': context['source_id'],\n"
            "         'source_sequence': context['source_sequence'],\n"
            "         'occurred_at': context['occurred_at'],\n"
            "         'context': {'attempt_id': context['attempt_id'],\n"
            "                     'session_id': context['session_id']}},\n"
            "        session_id=context['session_id'], hermes_durable_event=True)\n"
            "    observed.append(response)\n"
            "    receipt = response.get('result', {})\n"
            "    return response.get('ok') is True and receipt.get('status') in {\n"
            "        'processed', 'duplicate'}\n"
        )

        registry = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path), _patch_no_builtins(registry):
            registry.discover_and_load()
        with patch(
            "hermes_cli.config.load_config",
            return_value={
                "hooks": {
                    "entries": {"curator": {"mcp_allowlist": ["curator-mcp"]}}
                }
            },
        ), patch(
            "hermes_cli.mcp_config._get_mcp_servers",
            return_value={
                "curator-mcp": {
                    "command": "curator-mcp",
                    "hermes_context": True,
                }
            },
        ), patch.dict(mcp_tool._servers, {"curator-mcp": server}), patch(
            "tools.mcp_tool_loop._run_on_mcp_loop", side_effect=run_on_mcp_loop
        ), patch.dict(mcp_tool._server_error_counts, {}, clear=True):
            assert registry._outbox is not None
            assert registry._outbox.bind_goal("project-1", "session-1", "goal-1")
            event = registry.queue_durable_event(
                "codex:turn:start",
                {
                    "attempt_id": "attempt-1",
                    "session_id": "session-1",
                    "model": "must-not-be-retained",
                    "cwd": "/private/repository",
                },
            )
            assert event is not None
            assert "model" not in event["context"]
            assert "cwd" not in event["context"]
            delivered = await registry.emit_durable(event)
            observed = registry._durable_handlers["codex:turn:start"][0].__globals__["observed"]
            assert observed and observed[0].get("ok") is True, observed
            assert delivered
            assert registry._outbox.pending() == []

        session.call_tool.assert_awaited_once()
        tool, call_kwargs = session.call_tool.await_args.args[0], session.call_tool.await_args.kwargs
        assert tool == "hermes_codex_event"
        arguments = call_kwargs["arguments"]
        assert arguments["project_id"] == "project-1"
        assert arguments["goal_id"] == "goal-1"
        assert arguments["event_id"] == event["event_id"]
        assert arguments["source_sequence"] == event["source_sequence"] == 1
        assert arguments["context"] == {
            "attempt_id": "attempt-1",
            "session_id": "session-1",
        }
        assert call_kwargs["meta"] == {
            "hermes/profile_id": profile_name_for_home(get_hermes_home()),
            "hermes/session_id": "session-1",
            "hermes/durable_event": True,
        }
        assert registry._outbox is not None
        registry._outbox.close()

    @pytest.mark.asyncio
    async def test_codex_usage_is_project_scoped_local_and_content_free(self, tmp_path):
        hook_dir = tmp_path / "curator"
        hook_dir.mkdir()
        (hook_dir / "HOOK.yaml").write_text(
            "name: curator\n"
            "events: ['codex:turn:end']\n"
            "durable_events: ['codex:turn:end']\n"
        )
        (hook_dir / "handler.py").write_text("def handle(event_type, context): return True\n")

        registry = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path):
            registry.discover_and_load()
        assert registry._outbox.bind_goal("project-1", "session-1", "goal-1")
        observed_hooks = []
        registry._handlers["codex:turn:end"].append(
            lambda _event, value: observed_hooks.append(dict(value))
        )
        context = {
            "attempt_id": "attempt-usage-1",
            "session_id": "session-1",
            "turn_id": "turn-usage-1",
            "outcome": "completed",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "usage": {
                "input_tokens": 80,
                "output_tokens": 50,
                "total_tokens": 130,
                "cache_read_tokens": 10,
            },
        }
        queued = registry.queue_durable_event("codex:turn:end", context)
        assert queued is not None
        assert "usage" not in queued["context"]
        assert "provider" not in queued["context"]
        assert "model" not in queued["context"]
        await registry.emit("codex:turn:end", context)
        assert len(observed_hooks) == 1
        assert "usage" not in observed_hooks[0]
        assert registry.queue_durable_event("codex:turn:end", context) == queued
        assert registry._outbox.project_usage_totals("project-1") == {
            "project_id": "project-1",
            "source": "codex_app_server",
            "turns": 1,
            "reported_turns": 1,
            "unknown_turns": 0,
            "input_tokens": 80,
            "output_tokens": 50,
            "total_tokens": 130,
            "cache_read_tokens": 10,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        }
        assert registry._outbox.project_usage_by_model("project-1") == [{
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "usage_status": "reported",
            "turns": 1,
            "input_tokens": 80,
            "output_tokens": 50,
            "total_tokens": 130,
        }]

        unknown = registry.queue_durable_event(
            "codex:turn:end",
            {**context, "attempt_id": "attempt-usage-2", "usage": {"total_tokens": True}},
        )
        assert unknown is not None
        totals = registry._outbox.project_usage_totals("project-1")
        assert (totals["turns"], totals["reported_turns"], totals["unknown_turns"]) == (2, 1, 1)
        assert [row["usage_status"] for row in registry._outbox.project_usage_by_model("project-1")] == [
            "reported", "unknown"
        ]
        assert registry._outbox.project_usage_totals("project-2")["turns"] == 0
        outbox_path = registry._outbox.path
        registry._outbox.close()

        from gateway.codex_outbox import CodexLifecycleOutbox

        reopened = CodexLifecycleOutbox(outbox_path, profile_id="hermes-test")
        recovered = reopened.project_usage_totals("project-1")
        assert (recovered["turns"], recovered["reported_turns"], recovered["unknown_turns"]) == (
            2, 1, 1
        )
        assert reopened.enqueue("codex:turn:end", context) == queued
        replayed = reopened.project_usage_totals("project-1")
        assert (replayed["turns"], replayed["reported_turns"], replayed["unknown_turns"]) == (
            2, 1, 1
        )
        reopened.close()


class TestEmitCollect:
    """Tests for emit_collect() — returns handler return values for decision-style hooks."""

    @pytest.mark.asyncio
    async def test_collects_sync_return_values(self):
        reg = HookRegistry()
        reg._handlers["command:status"] = [
            lambda _e, _c: {"decision": "allow"},
            lambda _e, _c: {"decision": "deny", "message": "nope"},
        ]

        results = await reg.emit_collect("command:status", {})

        assert results == [
            {"decision": "allow"},
            {"decision": "deny", "message": "nope"},
        ]


    @pytest.mark.asyncio
    async def test_drops_none_return_values(self):
        reg = HookRegistry()
        reg._handlers["command:x"] = [
            lambda _e, _c: None,  # fire-and-forget, returns nothing
            lambda _e, _c: {"decision": "deny"},
            lambda _e, _c: None,
        ]

        results = await reg.emit_collect("command:x", {})

        assert results == [{"decision": "deny"}]


