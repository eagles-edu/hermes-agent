# Project Usage

`project_usage` provides a read-only aggregate view of the current Hermes
profile's local Codex app-server usage ledger. It is deliberately separate from
Host Steward: it measures project token usage and does not inventory, propose,
or clean host files.

Opt in per Hermes profile by enabling the plugin and setting one exact project
ID:

```yaml
plugins:
  enabled:
    - project-usage
  entries:
    project-usage:
      settings:
        project_id: gptmd
```

The model tool accepts no project selector, so a call cannot broaden that
scope. If the local ledger or its expected schema is absent, the tool reports
unavailable instead of creating a database or claiming zero usage.

Coverage is limited to Codex app-server turns recorded with an explicit
project/goal binding. Other providers, unbound turns, budgets, retention, and
alerts are not represented. Results contain aggregate totals only, not goal,
session, or event identifiers. The SQLite ledger is opened read-only.
