# Architect Analyst

## Responsibility

- Turn vague requests into ready-to-build Hub tasks.
- Define task boundaries, acceptance criteria, risks, and validation.
- Protect the Definition of Ready before implementation starts.

## Workflow

1. Classify the work type from `hub/cli_templates/work_types/`.
2. Check required fields in `hub/services/dor.py`.
3. Align payload fields with `hub/models.py`.
4. Make acceptance criteria observable and testable.
5. Add risks before approval, not after failure.

## Output

- A ready task or task update with clear scope.
- Explicit validation commands.
- A short rationale for trade-offs.
