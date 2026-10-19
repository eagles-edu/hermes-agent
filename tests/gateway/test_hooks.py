"""Tests for gateway/hooks.py — event hook system."""

from unittest.mock import patch

import pytest

from gateway.hooks import HookRegistry


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
            await second.drain_durable()
        assert second._outbox.pending() == []
        assert [call[1]["event_id"] for call in first_calls] == [queued["event_id"]]
        calls = second._durable_handlers["codex:turn:start"][0].__globals__["calls"]
        assert [call[1]["event_id"] for call in calls] == [queued["event_id"], queued_second["event_id"]]
        assert [call[1]["source_sequence"] for call in calls] == [1, 2]
        second._outbox.close()


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


