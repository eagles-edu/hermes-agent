# Project Usage

`project_usage` provides a read-only aggregate view of the current Hermes
profile's local project usage ledger. It can report Codex app-server turns and
main Hermes provider API responses when the turn has an explicit project/goal
binding. It is deliberately separate from Host Steward: it measures project
token usage and does not inventory, propose, or clean host files.

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

Coverage includes auxiliary provider responses whose usage can be normalized
inside an active agent turn. Auxiliary calls outside that callback context,
attempts without a returned response, and unbound sessions are excluded.
Missing or unassignable token usage in a returned response is reported as
unknown, not zero. The report sets `coverage_complete` to `false` and lists
these known exclusions. Separately,
`tokens_complete` indicates whether any turn in the captured ledger has unknown
usage; it does not claim project-wide completeness. The report lists the sources
it observed and aggregate totals by provider/model. It does not enforce budgets
or define retention/alert policy. Token bucket meanings follow each source's
normalization: `input` is uncached input where Hermes has that distinction,
while `cache_read` and `cache_write` are separate buckets. `total` is the
source-reported or normalized total and may already include cache buckets; do
not add it to the sub-buckets. Results contain no goal, session, turn, or event
identifiers, and the SQLite ledger is opened read-only.
