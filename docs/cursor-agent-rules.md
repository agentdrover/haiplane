# Cursor Agent Rules

These rules bind Cursor agents to the Haiplane Hub lifecycle. The hub is the
source of truth for task state, questions, blockers, decisions, and completion
reports.

Branch and workspace invariants (one branch per executable task, no writes to
another task's branch without human decision, out-of-scope work only as a
draft proposal, unresolved branch/PR/CI conflicts move the task to
`needs_decision`) are defined in
[workspace-safety-policy.md](workspace-safety-policy.md).

## Required Workflow

1. Before work starts, call `hub_my_context(task_id)` and read the returned
   task context.
2. If the task is not ready, do not start code changes. Use `hub_refine_task`,
   `hub_add_acceptance_criterion`, `hub_replace_acceptance_criteria`, or
   `hub_add_risk` to improve the draft, then check `hub_get_readiness`.
3. Before dispatch or implementation, record a plan. Use
   `hub_start_task(..., plan="...")` or `hub_task_update(..., kind="status",
   content="Plan: ...")`.
4. Ask questions through `hub_ask_question`. Do not rely on chat-only questions
   for missing requirements.
5. Record blockers through `hub_task_update(..., kind="blocker")`. If the
   blocker requires a human answer, use `hub_ask_question`.
6. Pass the Universal Review Gate before completion: submit work with
   `hub_submit_for_review`, have a reviewer (another agent session or a
   human — never yourself) run `hub_get_review_brief` and
   `hub_submit_review`, fix `changes_requested` findings by number and
   resubmit. Only after an APPROVED verdict finish with `hub_report_done`
   (changed files, behavior change, validation commands with results).
   A done report without a current APPROVED review does not complete the
   task — the server routes it to `review`.
7. New work discovered outside the task scope must be a draft proposal through
   `hub_propose_task`. Do not silently expand the current task.
8. Do not close or force-complete a task when there is failed CI, an unresolved
   blocker, or requested review changes. Use `hub_decide_task` or a human gate.

## Human Gates

- Approval uses `hub_approve_task`; `force=true` is for explicit human
  overrides and is audited by the API.
- Weak or missing reports in `pending_report` are accepted only by a human via
  `hub_force_complete_task`.
- Human decisions after arbitration use `hub_decide_task`.

## Minimum MCP Tools

- `hub_project_status`
- `hub_list_tasks`
- `hub_task_status`
- `hub_my_context`
- `hub_refine_task`
- `hub_add_acceptance_criterion`
- `hub_replace_acceptance_criteria`
- `hub_add_risk`
- `hub_get_readiness`
- `hub_approve_task`
- `hub_reject_task`
- `hub_start_task`
- `hub_task_update`
- `hub_ask_question`
- `hub_submit_for_review`
- `hub_get_review_brief`
- `hub_submit_review`
- `hub_report_done`
- `hub_force_complete_task`
- `hub_decide_task`
