"""Privacy regressions for MCP sampling argument diagnostics."""

from tools.mcp_tool_sampling import _parse_tool_call_arguments


def test_malformed_sampling_arguments_are_preserved_but_not_logged(caplog):
    canary = "private-sampling-argument-7e91"

    parsed = _parse_tool_call_arguments("curator_v2", f"{{malformed:{canary}")

    assert parsed == {"_raw": f"{{malformed:{canary}"}
    assert canary not in caplog.text
    assert "argument text omitted" in caplog.text
