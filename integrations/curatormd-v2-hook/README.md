# Curator V2 Hermes hook

This profile hook forwards only the Hermes outbox's bound Codex turn-start and
turn-end events to the `curator_v2` MCP server. It sends identifiers, the
runtime result, and the already-filtered work summary. It does not receive or
forward prompts, replies, command output, or tool arguments.

Install `HOOK.yaml` and `handler.py` into the active profile's
`hooks/curator-v2/` directory. Add this allowlist to that profile's
`config.yaml` so the hook can call only the configured Curator server:

```yaml
hooks:
  entries:
    curator-v2:
      mcp_allowlist:
        - curator_v2
```

The Curator MCP server must be configured for the same project and repository.
The hook acknowledges an outbox event only when Curator returns a processed or
exact-duplicate receipt for the same project and event. Missing, failed,
truncated, or mismatched responses remain available for retry.
