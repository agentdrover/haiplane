---
name: hub-task-prep
description: Use when turning a request into a structured Haiplane Hub task pack with work type selection, scope, acceptance criteria, risks, readiness expectations, and validation commands.
---

# Hub Task Prep

## Use This Skill For

- drafting or refining Hub tasks
- choosing a work type template
- preparing acceptance criteria and risks
- improving readiness before approval

## Workflow

1. Pick the closest template in `hub/cli_templates/work_types/`.
2. Check the required readiness fields in `hub/services/dor.py`.
3. Use `hub/models.py` as the schema source of truth.
4. Write scope in and scope out so implementation boundaries are unambiguous.
5. Make every acceptance criterion observable and verifiable.

## Output Requirements

- clear `work_type`
- explicit `scope_in`
- acceptance criteria with a validation path
- realistic validation commands
- risks with mitigation when uncertainty exists

## Reference Files

- `hub/models.py`
- `hub/services/dor.py`
- `hub/services/recommendations.py`
- `hub/cli_templates/work_types/`
