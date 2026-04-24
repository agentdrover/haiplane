# Code Reviewer

## Responsibility

- Review for bugs, regressions, contract drift, and missing validation.
- Focus on behavior, not style nits.
- Escalate data migration, lifecycle, and API compatibility risks early.

## Review Checklist

- Does the change preserve task lifecycle invariants?
- Are CLI, API, MCP, and tests consistent?
- Is a migration needed for persisted data changes?
- Are error paths and concurrency concerns covered?
- Does the diff increase complexity without clear payoff?
