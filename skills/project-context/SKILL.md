---
name: project-context
description: Use when onboarding to the standalone Haiplane Hub project, analyzing impact before changes, or needing a fast map of architecture, invariants, contract boundaries, and test routing without rereading the whole codebase.
---

# Project Context

## Use This Skill For

- first contact with this repository
- change-impact analysis before editing
- architecture questions
- deciding which files and tests matter for a request

## Read Order

1. Read `../../docs/agent-context/system-map.md`.
2. Read `../../docs/agent-context/change-map.md`.
3. Read `../../docs/agent-context/invariants.md` if the task touches lifecycle, schema, DoR, MCP, CLI, or integrations.
4. Read `../../docs/agent-context/testing-playbook.md` before selecting validation.
5. Read `../../docs/agent-context/contracts.md` for cross-surface or protocol changes.

## Navigation Rules

- Start from the requested behavior, not from a full repo scan.
- Open only the files listed in `change-map.md` for the relevant area.
- Treat `hub/models.py` as the schema and enum source of truth.
- Treat API, CLI, and MCP as aligned surfaces over the same domain behavior.

## When To Expand Beyond The Docs

- The task changes a field, enum, or status not covered by the docs yet.
- The change-map points to a service, and that service clearly fans out into other layers.
- A plugin protocol change affects multiple adapters.
