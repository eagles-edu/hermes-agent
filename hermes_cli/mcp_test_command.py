"""MCP test command phases, including the profile-bound Curator V2 probe."""

import json
import time
import uuid
from typing import Any


def cmd_mcp_test(args) -> int:
    """Test an MCP server and return a stable CLI status code."""
    from hermes_cli import mcp_config as api

    request = _prepare_request(args, api)
    if request is None:
        return 1
    if request.get("missing"):
        return 3
    started = time.monotonic()
    result: dict[str, Any] = {}
    try:
        tools = _connect(request, result, api)
    except Exception as exc:  # health: allow BLE001 -- MCP servers can raise transport-specific failures
        return _report_failure(request, exc, started, api)
    if request["json_output"]:
        return _report_json(request, result)
    return _report_success(request, result, tools, started, api)


def _emit_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))


def _prepare_request(args, api) -> dict[str, Any] | None:
    name = args.name
    json_output = bool(getattr(args, "json_output", False))
    curator_probe = bool(getattr(args, "probe_curator_status", False))
    if json_output and not curator_probe:
        _emit_json({"schema_version": 1, "connected": False, "code": "probe_required"})
        return None
    cfg = api._lookup_server(name, api._get_mcp_servers(), "Available")
    if cfg is None:
        if json_output:
            _emit_json({"schema_version": 1, "connected": False, "code": "server_not_configured"})
        return {"missing": True}
    if curator_probe and not _curator_probe_configured(name, cfg):
        if json_output:
            _emit_json({"schema_version": 1, "connected": False, "code": "probe_not_configured"})
        else:
            api._error("Curator status probe requires the opted-in local stdio curator_v2 server")
        return None
    request = {"name": name, "cfg": cfg, "json_output": json_output,
               "curator_probe": curator_probe}
    if not json_output:
        _show_transport(name, cfg, api)
        _show_auth(cfg, api)
    return request


def _curator_probe_configured(name: str, cfg: dict) -> bool:
    command = cfg.get("command")
    return (name == "curator_v2" and cfg.get("hermes_context") is True
            and isinstance(command, str) and bool(command.strip()) and not cfg.get("url"))


def _show_transport(name: str, cfg: dict, api) -> None:
    from hermes_cli.colors import Colors, color

    print()
    print(color(f"  Testing '{name}'...", Colors.CYAN))
    if "url" in cfg:
        api._info(f"Transport: HTTP → {cfg['url']}")
    else:
        api._info(f"Transport: stdio → {cfg.get('command', '?')}")


def _show_auth(cfg: dict, api) -> None:
    if cfg.get("auth", "") == "oauth":
        api._info("Auth: OAuth 2.0")
    elif _uses_hermes_context(cfg):
        api._info("Context: Hermes profile/session (host-injected; local stdio)")
    else:
        _show_headers(cfg.get("headers", {}), api)


def _uses_hermes_context(cfg: dict) -> bool:
    command = cfg.get("command")
    return (cfg.get("hermes_context") is True and isinstance(command, str)
            and bool(command.strip()) and not cfg.get("url"))


def _show_headers(headers: dict, api) -> None:
    if not headers:
        api._info("Auth: none")
        return
    for key, value in headers.items():
        if isinstance(value, str) and ("key" in key.lower() or "auth" in key.lower()):
            # Keep header identity with the value; partial env references do not prove safety.
            print(f"    {key}: {api.redact_mcp_header_display(key, value)}")


def _connect(request: dict, result: dict[str, Any], api) -> list:
    curator_probe = request["curator_probe"]
    return api._probe_single_server(
        request["name"],
        request["cfg"],
        curator_status_probe=curator_probe,
        probe_result=result if curator_probe else None,
        probe_session_id=f"curator-status-probe-{uuid.uuid4().hex}" if curator_probe else None,
    )


def _report_failure(request: dict, exc: BaseException, started: float, api) -> int:
    if request["json_output"]:
        _emit_json({"schema_version": 1, "connected": False,
                    "code": api.probe_failure_class(exc)})
        return 1
    elapsed = time.monotonic() - started
    api._error(f"Connection failed ({elapsed:.1f}s): {api._probe_failure_reason(exc)}")
    api._info(api._probe_failure_next_step(request["name"], exc))
    return 1


def _report_json(request: dict, result: dict[str, Any]) -> int:
    if result.get("is_error"):
        _emit_json({"schema_version": 1, "connected": True, "code": "curator_read_rejected"})
        return 1
    health = result.get("health_snapshot")
    project_id = request["cfg"].get("hermes_project_id")
    if (not isinstance(health, dict) or health.get("project_id") != project_id
            or health.get("status") not in {"healthy", "degraded", "unknown"}):
        _emit_json({"schema_version": 1, "connected": True, "code": "invalid_health_response"})
        return 1
    _emit_json({"schema_version": 1, "connected": True,
                "project_id": project_id, "health": health})
    return 0


def _report_success(request: dict, result: dict[str, Any], tools: list, started: float, api) -> int:
    api._success(f"Connected ({(time.monotonic() - started) * 1000:.0f}ms)")
    api._success(f"Tools discovered: {len(tools)}")
    if request["curator_probe"] and _report_curator_result(result, api):
        return 1
    if tools:
        print()
        api._print_tools(tools, 36, 55)
    print()
    return 0


def _report_curator_result(result: dict[str, Any], api) -> bool:
    if result.get("is_error"):
        api._error("Curator V2 rejected the Hermes-context status probe")
        api._info(str(result.get("text", ""))[:4096])
        return True
    api._success("Curator V2 ledger reads returned with Hermes context")
    api._info(str(result.get("text", ""))[:4096])
    api._info(f"Paginated goal-list total: {result['goal_list_total']}")
    statuses = ", ".join(f"{name}={count}" for name, count in sorted(result["goal_status_counts"].items()))
    api._info(f"Goal lifecycle counts: {statuses}")
    api._info("Project health snapshot (separate observation):")
    api._info(str(result["health_text"])[:4096])
    gates = ", ".join(result["health_gate_codes"]) or "none"
    api._info(f"Project health: {result['health_status']} (gates: {gates})")
    return False
