# Host Steward (Hermes Core)

Host Steward is an opt-in, root-account-only observer for disk growth. It is
separate from CuratorMD and from the existing `disk-cleanup` plugin.

The initial component exposes two model tools:

- `host_inventory` measures only explicitly configured directories and root
  filesystem free space. It reads metadata only, does not follow symlinks, and
  stops at a bounded entry count and depth. A compact before/after snapshot is
  stored in Hermes plugin state.
- `host_proposals` lists targets above their configured threshold with the
  configured owner, symbolic managed-action id, reclaim estimate, impact, and
  recovery path. It marks each proposal `review_required` and `executable: false`.

There is deliberately no approval API, shell-command dispatch, or deletion
executor in this increment. A model can inventory and propose, but cannot
approve or clean up its own proposal. The plan's distinct human approval,
managed action, post-action verification, and receipt gates remain open.

The plugin declines to register in named Hermes profiles under
`~/.hermes/profiles/`; it belongs only to the Hermes Core account home. Keep it
disabled until the account owner configures explicit targets in
`plugins.entries.host-steward.settings.targets`:

```yaml
plugins:
  enabled:
    - host-steward
  entries:
    host-steward:
      settings:
        max_entries_per_target: 20000
        targets:
          - id: uv-cache
            path: /home/you/.cache/uv
            owner: uv
            threshold_bytes: 1073741824
            expected_reclaim_bytes: 536870912
            managed_action_id: uv_cache_prune
            service_impact: Cached packages may need to be fetched again.
            recovery_path: Re-run the project's uv sync or uv tool install.
```

Treat the expected reclaim figure as an operator estimate, not a promise. The
symbolic action id is descriptive only; this version never resolves or runs it.
Never configure a project tree, secrets directory, or broad system root as a
cleanup target. Keep the project-local `curatormd-v2` plugin and all project
knowledge out of this account-level host component.
