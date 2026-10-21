"""Event hook system: fires handlers at gateway lifecycle points.

Hooks live in ~/.hermes/hooks/<name>/ with HOOK.yaml (name, description, events) and
handler.py (``def handle(event_type, context)``, sync or async); errors never block
the pipeline. Events include gateway:startup, session:start/end/reset, agent:start,
agent:step (each tool-loop turn), agent:end, Codex app-server ``codex:turn:start`` /
``codex:turn:end``, and command:* (wildcard). ``agent:*`` context includes platform,
user_id, chat_id, thread_id ("" outside a thread), chat_type ("dm"|"group"|"forum"|""),
session_id, and message (500 chars); agent:end adds response, model, and provider. Codex
turn events contain only bounded session/attempt/model/provider/cwd metadata; the end event
also carries the Codex turn id and runtime outcome. They never contain prompt/response text,
tool arguments/results, or exception text, and a runtime outcome is not goal-success evidence.
Forum follow-ups pass ``message_thread_id=int(thread_id)``.
"""

import asyncio
import inspect
import importlib.util
import json
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import hermes_yaml as yaml

from hermes_cli.config import get_hermes_home
from hermes_constants import hermes_home_key, profile_name_for_home

from gateway.codex_outbox import CodexLifecycleOutbox


HOOKS_DIR = get_hermes_home() / "hooks"
_HOOKS_DIR_AT_IMPORT = HOOKS_DIR


class HookContext:
    """Profile-scoped capabilities available to opt-in gateway hook handlers.

    MCP access is granted per hook through ``hooks.entries.<name>.mcp_allowlist``
    and remains default-deny. Trusted profile/session metadata is added only by
    the existing MCP handler trust gate for opted-in local stdio servers.
    """

    def __init__(self, hook_name: str):
        self.hook_name = hook_name

    def call_mcp(
        self,
        server: str,
        tool: str,
        arguments: Optional[dict[str, Any]] = None,
        timeout: float = 30,
        *,
        session_id: Optional[str] = None,
        hermes_durable_event: bool = False,
    ) -> dict[str, Any]:
        """Call an allowlisted MCP server through Hermes' managed connection."""
        from hermes_cli.config import load_config

        config = load_config() or {}
        hooks = config.get("hooks", {}) if isinstance(config, dict) else {}
        entries = hooks.get("entries", {}) if isinstance(hooks, dict) else {}
        entry = entries.get(self.hook_name, {}) if isinstance(entries, dict) else {}
        allowlist = entry.get("mcp_allowlist") if isinstance(entry, dict) else None
        allowed_servers = (
            [item for item in allowlist if isinstance(item, str)]
            if isinstance(allowlist, list)
            else []
        )
        if server not in allowed_servers:
            raise PermissionError(
                f"Hook {self.hook_name!r} is not allowed to call MCP server {server!r}. "
                f"Add it to hooks.entries.{self.hook_name}.mcp_allowlist in config.yaml."
            )
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = 30.0
        timeout = max(1.0, min(timeout, 600.0))
        if session_id is not None and (
            not isinstance(session_id, str)
            or not session_id.strip()
            or len(session_id) > 256
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in session_id)
        ):
            raise ValueError(
                "session_id must be a non-empty, single-line value of at most 256 characters"
            )
        if type(hermes_durable_event) is not bool:
            raise TypeError("hermes_durable_event must be a bool")
        if hermes_durable_event and session_id is None:
            raise ValueError("session_id is required for a durable Hermes event")

        from tools.mcp_tool_handlers import _make_tool_handler

        request_context: dict[str, Any] = {}
        if session_id is not None:
            request_context["session_id"] = session_id
        if hermes_durable_event:
            request_context["hermes_durable_event"] = True
        raw = _make_tool_handler(server, tool, timeout)(
            dict(arguments or {}), **request_context
        )
        if not isinstance(raw, str):
            raw = "" if raw is None else str(raw)
        truncated = len(raw) > 65536
        if truncated:
            raw = raw[:65536] + "… [truncated]"
        try:
            parsed: Any = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and "error" in parsed:
            envelope: dict[str, Any] = {"ok": False, "error": parsed["error"]}
        elif isinstance(parsed, dict) and "result" in parsed:
            envelope = {"ok": True, "result": parsed["result"]}
            if "structuredContent" in parsed:
                envelope["structuredContent"] = parsed["structuredContent"]
            binding = parsed.get("hermes_goal_binding")
            if binding in {"bound", "not_recorded"}:
                envelope["hermes_goal_binding"] = binding
        else:
            envelope = {"ok": True, "result": parsed if parsed is not None else raw}
        return {**envelope, "truncated": True} if truncated else envelope


def _resolve_hooks_dir() -> Path:
    """Active profile's hooks dir at call time: the patched ``HOOKS_DIR`` when a test changed it,
    else ``get_hermes_home()/hooks``. The import-time constant is the LAUNCH profile's; under
    ``gateway.multiplex_profiles`` every served profile has its own ``hooks/``, and a registry
    loaded from the launch home would run the default profile's handlers (arbitrary Python) on
    every other profile's messages, responses and user ids."""
    configured = Path(HOOKS_DIR)
    return configured if configured != _HOOKS_DIR_AT_IMPORT else get_hermes_home() / "hooks"


def _skip(name: str, reason: str) -> None:
    print(f"[hooks] Skipping {name}: {reason}", flush=True)


def _load_hook_dir(hook_dir: Path) -> Optional[tuple]:
    """``(name, events, durable_events, handle_fn, description)`` for a valid hook dir."""
    manifest_path, handler_path = hook_dir / "HOOK.yaml", hook_dir / "handler.py"
    if not manifest_path.exists() or not handler_path.exists():
        return None
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8-sig"))
    if not manifest or not isinstance(manifest, dict):
        return _skip(hook_dir.name, "invalid HOOK.yaml")
    hook_name = manifest.get("name", hook_dir.name)
    events = manifest.get("events", [])
    if not events:
        return _skip(hook_name, "no events declared")
    durable_events = manifest.get("durable_events", [])
    if not isinstance(durable_events, list) or any(
        event not in {"codex:turn:start", "codex:turn:end"} or event not in events
        for event in durable_events
    ):
        return _skip(hook_name, "durable_events must opt in to declared Codex lifecycle events only")
    # Register in sys.modules BEFORE exec_module so Pydantic/dataclass forward references
    # (``from __future__ import annotations``) resolve; otherwise a handler declaring a
    # BaseModel fails at first dispatch with "TypeAdapter ... is not fully defined".
    module_name = f"hermes_hook_{hook_name}"
    spec = importlib.util.spec_from_file_location(module_name, handler_path)
    if spec is None or spec.loader is None:
        return _skip(hook_name, "could not load handler.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    handle_fn = getattr(module, "handle", None)
    if handle_fn is None:
        return _skip(hook_name, "no 'handle' function found")
    return hook_name, events, durable_events, handle_fn, manifest.get("description", "")


class HookRegistry:
    """Discovers, loads, and fires event hooks."""

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = {}  # event_type -> handlers
        self._durable_handlers: dict[str, list[Callable]] = {}
        self._hook_contexts: dict[int, HookContext] = {}
        self._loaded_hooks: list[dict] = []  # metadata for listing
        self._outbox: CodexLifecycleOutbox | None = None
        self._durable_drain_lock = asyncio.Lock()

    @property
    def loaded_hooks(self) -> list[dict]:
        return list(self._loaded_hooks)

    def _register_builtin_hooks(self) -> None:
        """Extension point for always-on built-in hooks; currently none shipped."""

    def discover_and_load(self) -> None:
        """Register built-in hooks, then load every valid hook dir under the active profile's ``hooks/``."""
        self._register_builtin_hooks()
        hooks_dir = _resolve_hooks_dir()
        if not hooks_dir.exists():
            return
        for hook_dir in sorted(hooks_dir.iterdir()):
            if not hook_dir.is_dir():
                continue
            try:
                loaded = _load_hook_dir(hook_dir)
            except Exception as e:
                print(f"[hooks] Error loading hook {hook_dir.name}: {e}", flush=True)
                continue
            if loaded is None:
                continue
            hook_name, events, durable_events, handle_fn, description = loaded
            self._hook_contexts[id(handle_fn)] = HookContext(hook_name)
            for event in events:
                self._handlers.setdefault(event, []).append(handle_fn)
            for event in durable_events:
                self._durable_handlers.setdefault(event, []).append(handle_fn)
            self._loaded_hooks.append(
                {
                    "name": hook_name,
                    "description": description,
                    "events": events,
                    "durable_events": durable_events,
                    "path": str(hook_dir),
                }
            )
            print(f"[hooks] Loaded hook '{hook_name}' for events: {events}", flush=True)
        if self._durable_handlers:
            home = hooks_dir.parent
            self._outbox = CodexLifecycleOutbox(
                home / "curatormd" / "codex-outbox.sqlite3",
                profile_id=profile_name_for_home(home) or home.name,
            )

    def _resolve_handlers(self, event_type: str) -> list[Callable]:
        """Exact-match handlers first, then ``<base>:*`` wildcards.  A bare base type
        ("agent") does NOT fire for "agent:start" — only exact matches and explicit wildcards."""
        handlers = list(self._handlers.get(event_type, []))
        if ":" in event_type:
            handlers.extend(self._handlers.get(f"{event_type.split(':')[0]}:*", []))
        return handlers

    def _invoke_handler(self, handler: Callable, event_type: str, context: dict[str, Any]):
        """Pass the public hook capability only to handlers that declare ``ctx``."""
        try:
            parameters = inspect.signature(handler).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "ctx" in parameters:
            hook_context = self._hook_contexts.get(id(handler))
            if hook_context is None:
                return handler(event_type, context)
            return handler(
                event_type,
                context,
                ctx=hook_context,
            )
        return handler(event_type, context)

    async def emit(self, event_type: str, context: Optional[dict[str, Any]] = None) -> None:
        """Fire all handlers for an event, discarding return values."""
        await self.emit_collect(event_type, context)

    def queue_durable_event(
        self, event_type: str, context: Optional[dict[str, Any]] = None
    ) -> dict[str, Any] | None:
        """Persist an opted-in Codex lifecycle event before scheduling delivery."""
        if self._outbox is None or event_type not in self._durable_handlers:
            return None
        return self._outbox.enqueue(event_type, context or {})

    async def emit_durable(self, event: dict[str, Any]) -> bool:
        """Deliver a persisted event and acknowledge only explicit consumer success."""
        if self._outbox is None:
            return False
        event_type = event.get("event_type")
        handlers = self._durable_handlers.get(event_type, [])
        if not handlers:
            return False
        context = dict(event.get("context") or {})
        context.update(
            event_id=event["event_id"],
            source_id=event["source_id"],
            source_sequence=event["source_sequence"],
            occurred_at=event["occurred_at"],
            project_id=event["project_id"],
            goal_id=event["goal_id"],
        )
        acknowledged = True
        matched_consumer = False
        for handler in handlers:
            try:
                result = self._invoke_handler(handler, event_type, context)
                result = await result if asyncio.iscoroutine(result) else result
                if result is None:
                    continue
                matched_consumer = True
                if result is not True:
                    acknowledged = False
            except Exception as exc:  # health: allow BLE001 -- isolate trusted hook failures without logging payload details.
                print(
                    f"[hooks] Durable handler failed for '{event_type}': {type(exc).__name__}",
                    flush=True,
                )
                matched_consumer = True
                acknowledged = False
        acknowledged = acknowledged and matched_consumer
        self._outbox.mark_attempt(event["event_id"], acknowledged=acknowledged)
        return acknowledged

    async def drain_durable(self) -> None:
        """Retry pending deliveries in source order after startup or a new event."""
        if self._outbox is None:
            return
        async with self._durable_drain_lock:
            while True:
                events = self._outbox.pending()
                if not events:
                    return
                for event in events:
                    if not await self.emit_durable(event):
                        return
                if len(events) < 100:
                    return

    async def emit_collect(self, event_type: str, context: Optional[dict[str, Any]] = None) -> list[Any]:
        """Fire handlers and return their non-None return values in order (decision-style
        hooks, e.g. ``command:<name>`` policies).  A failing handler is logged, not fatal."""
        if context is None:
            context = {}
        elif event_type in {"codex:turn:start", "codex:turn:end"} and "usage" in context:
            # Project token accounting stays in the profile-local Hermes ledger; all hooks,
            # including Curator consumers, receive only the established lifecycle metadata.
            context = {key: value for key, value in context.items() if key != "usage"}
        results: list[Any] = []
        handlers = self._resolve_handlers(event_type)
        if event_type in self._durable_handlers:
            durable_ids = {id(fn) for fn in self._durable_handlers[event_type]}
            handlers = [fn for fn in handlers if id(fn) not in durable_ids]
        for fn in handlers:
            try:
                result = self._invoke_handler(fn, event_type, context)
                result = await result if asyncio.iscoroutine(result) else result  # sync or async handlers
                if result is not None:
                    results.append(result)
            except Exception as e:
                print(f"[hooks] Error in handler for '{event_type}': {e}", flush=True)
        # The process-level startup event is emitted only for the launch profile.
        # In multiplex mode, a secondary profile may not exist until its first
        # turn; retry its profile-local outbox at the start of that turn too.
        if event_type in {"gateway:startup", "agent:start"}:
            await self.drain_durable()
        return results


class ProfileHookRegistries:
    """``HookRegistry`` per served profile home, picked at emit time from the active HERMES_HOME.

    The gateway holds ONE of these. Every hook emit already runs inside the routed profile's
    ``_profile_runtime_scope`` (message handlers, /new, turn wiring), so resolving the registry by
    ``get_hermes_home()`` there gives each profile its own ``hooks/`` and keeps the default
    profile's handlers from seeing other profiles' messages. Each home's registry is loaded on its
    first emit, i.e. inside that profile's scope (handler imports see its HERMES_HOME); multiplexing off
    means a single entry for the launch home, i.e. exactly the old behaviour.
    """

    def __init__(self):
        self._by_home: dict[str, HookRegistry] = {}
        self._lock = threading.Lock()

    def _active(self) -> HookRegistry:
        key = hermes_home_key(get_hermes_home())
        registry = self._by_home.get(key)
        if registry is None:
            with self._lock:
                registry = self._by_home.get(key)
                if registry is None:
                    registry = HookRegistry()
                    registry.discover_and_load()
                    self._by_home[key] = registry
        return registry

    def for_current_profile(self) -> HookRegistry:
        """Resolve and pin the active profile's registry while its runtime scope is bound.

        Agent callbacks may run in executor threads. Capturing this registry when a turn
        is wired prevents a callback from depending on a later context-variable lookup.
        """
        return self._active()

    @property
    def loaded_hooks(self) -> list[dict]:
        return self._active().loaded_hooks

    def discover_and_load(self) -> None:
        """Load the active home's hooks now (startup, or a secondary profile's scoped startup)."""
        self._active()

    async def emit(self, event_type: str, context: Optional[dict[str, Any]] = None) -> None:
        await self._active().emit(event_type, context)

    def queue_durable_event(
        self, event_type: str, context: Optional[dict[str, Any]] = None
    ) -> dict[str, Any] | None:
        return self._active().queue_durable_event(event_type, context)

    async def emit_durable(self, event: dict[str, Any]) -> bool:
        return await self._active().emit_durable(event)

    async def emit_collect(self, event_type: str, context: Optional[dict[str, Any]] = None) -> list[Any]:
        return await self._active().emit_collect(event_type, context)
