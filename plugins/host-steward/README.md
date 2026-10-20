# Host Steward (Hermes Core)

Host Steward is an opt-in Hermes Core account component for disk growth. It is
separate from CuratorMD and from the existing `disk-cleanup` plugin. It never
writes project knowledge.

The initial component exposes two model tools:

- `host_inventory` measures only explicitly configured directories and root
  filesystem free space. It reads metadata only, does not follow symlinks, and
  stops at a bounded entry count and depth. A compact before/after snapshot is
  stored in Hermes plugin state. Its `growth_attribution` field compares the
  root filesystem used-byte delta with allocated-byte deltas from complete,
  unchanged, non-overlapping configured targets. It groups measured target
  growth by configured owner and labels the remainder as an unattributed
  growth estimate. The first observation is a baseline; incomplete or changed
  target coverage is marked partial and contributes no owner attribution;
  overlapping target paths also produce partial coverage. The comparison
  covers configured target paths only and does not identify which unconfigured
  path caused a filesystem delta. For a stable, complete target it also reports
  the configured service-unit names associated with that target's positive
  allocated-byte growth. This records operator-supplied target metadata; it does
  not establish that a named service caused the writes. Targets outside the
  configured inventory remain unattributed, and a changed service binding makes
  the comparison partial.
- `host_proposals` lists targets above their configured threshold with the
  configured owner, registered managed-action id, reclaim estimate, impact,
  recovery path, and inventory-manifest hash. It marks each proposal
  `review_required` and `executable: false`.
- `host_cleanup` runs only the registered `empty_configured_cache` action on
  the exact configured directory, after the Hermes approval gate returns a
  fresh `once` decision for that proposal and inventory hash. A plugin-owned
  one-use receipt check independently rejects yolo/off-mode bypasses, remembered
  session/permanent approvals, expired or replayed grants, changed inventory,
  symlinks, special files, and unsupported action IDs. It uses bounded,
  no-follow directory-descriptor operations; no shell or model-selected path is
  accepted. The configured directory itself is retained while its contents are
  removed.
- `host_cleanup_receipts` exposes the latest 50 receipts from a bounded
  200-record profile-local history. Receipts distinguish approved, rejected,
  running, completed, verification-incomplete/failed, and partial-failure
  outcomes. Free space is checked before and after. Configured systemd services
  are checked with a fixed `systemctl is-active --quiet <validated-unit>` call;
  no configured units leaves verification incomplete, not green.
- `host_cleanup_reconcile` handles a receipt left `running` after an operator
  confirms the original Hermes process has stopped. It changes that receipt to
  `interrupted_requires_review` and never resumes or repeats deletion. The
  operator must take fresh inventory and verify target contents, free space, and
  service health; another cleanup requires a new proposal and fresh approval.

The proposal generator cannot approve or execute its own proposal. The approval
API does not expose an authenticated human subject to this plugin; receipts say
so explicitly. Production account identity, host-owner attribution, live
installation, and deployment recovery drills remain open gates.

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
            managed_action_id: empty_configured_cache
            service_impact: Cached packages may need to be fetched again.
            recovery_path: Re-run the project's uv sync or uv tool install.
```

Treat the expected reclaim figure as an operator estimate, not a promise.
`managed_action_id` must currently be `empty_configured_cache`; unknown action
names are rejected. Choose service units from a verified host inventory and
describe impact/recovery accurately. With no service units configured, cleanup
may run after approval but its receipt remains `verification_incomplete`, not
green. Never configure a project tree, secrets
directory, or broad system root as a cleanup target. Keep the project-local
`curatormd-v2` plugin and all project knowledge out of this account-level host
component. A successful local test does not enable cleanup on a real host.
