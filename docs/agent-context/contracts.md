# Contracts

## Canonical Sources

| Contract Type | Source |
|---|---|
| Domain enums and request/response models | `hub/models.py` |
| REST routes | `hub/app.py` |
| Web routes | `hub/web.py` |
| CLI surface | `hub/cli.py` |
| MCP tool surface | `hub/mcp_server.py` |
| Integration interfaces | `hub/integrations/protocols.py` |
| DB schema and migrations | `hub/db.py`, `hub/migrations/` |

## Surface Rules

- Prefer changing models first, then the service or repository behavior, then the entry surfaces.
- Do not add a new business rule only in CLI or only in MCP.
- MCP tools should call the same API semantics the web and CLI rely on.
- Plugin interface changes are contract changes; update protocol, registry assumptions, concrete plugin, and noop implementation together.

## Common Contract Bundles

### Adding a task field

- `hub/models.py`
- `hub/db.py`
- `hub/repository.py`
- API endpoint handling in `hub/app.py`
- refine/readiness/recommendation services if relevant
- CLI flags or file import paths in `hub/cli.py`
- MCP tool arguments if the field must be agent-visible

### Adding a task kind that automation must not touch

- the single predicate in `hub/services/result_kind.py` (`automation_not_applicable`), called by every door listed in `invariants.md` "State task (#1647)"; a new door of automation (review order, steward order, auto-approve, executor order) calls it, it does not compare `result_kind` itself
- DoR profile in `hub/services/dor.py`, snapshot fields in `hub/services/dor_snapshot.py`
- submit/verdict branches in `hub/services/lifecycle.py` route to `hub/services/state_task.py`; the commit pipelines (`SUBMIT_STEPS`, `VERDICT_STEPS`) are not edited
- REST/CLI/MCP: `result_kind`/`rollback` on create and refine, `evidence` on submit-review, `expected_generation` on review-verdict (web: hidden form field); the MCP catalog budget moves only down

### Showing a state task (`result_kind=state`) on the gate and in dependencies (#1648)

- ONE reader of "what the human accepts": `hub/services/state_review.py` (`current_submission`, `state_review_view`). Review queue, inbox, verdict route, review brief (REST + MCP text) and the task card read it; none of them counts evidence completeness or compares `result_kind` itself. The kind comes from `result_kind.automation_not_applicable`.
- Readiness for a verdict is the COMPLETE evidence kit of the CURRENT generation against the submission's snapshot (`submissions.state_snapshot`), not a machine report. `review_queue` row: `readiness=ready`, `result_kind=state`, `sha_check` and `report_status` = `not_applicable`, `evidence_count`/`evidence_complete`. Inbox: action `verdict`, `offer` true only with a complete kit. `verdict_route`: final `human`, code `state_task_no_automation`, the reason text of the automation doors.
- Brief (`ReviewBrief`): `result_kind`, `state_review` (`StateReviewView`: `generation`, `expected_generation`, `rollback`, snapshot `acceptance_criteria`, `evidence` of the current generation with `author`/`observed_at`, `evidence_complete`, `missing_ac`, `not_applicable`). Code checks (`sha_check`, `ci_run_report`, `ac_tests`, `base_merge`, `call_sites`, `path_notices`, machine review) are `not_applicable` with a reason, never `unknown`/`match`/`pass`. The state brief reads DB rows only: no git, no network, no repository walk (`build_state_brief`). A new block of the brief that touches code must be skipped there and named in `NOT_APPLICABLE_CHECKS`.
- Card: label "результат: состояние", rollback, evidence table, hidden `expected_generation`; a stale web form gets the refusal in place (plain post: redirect with the reason; htmx: 409), nothing is written.
- Dependencies: ONE reader, `delivery_state.blocker_delivery` (every enrichment — REST, context, start warning, queue, project path, steward evidence — goes through it). A state blocker is ready exactly when `result_kind.state_accepted` (completed + an APPROVED verdict on the current generation whose approval window is not closed — the same reader, `orchestration.review_approved_for_current_submission`, that gives `latest_review.is_current`; a revoked approval (`/decide rework`) never counts); delivery path `state_accepted`/`state_pending`. No `pipeline_merges`, no `releases`, no git. A parent (feature/epic) with state and commit children is delivered when it is completed and every non-rejected child is ready by ITS OWN rule: state child by acceptance, commit child by delivery. Returning a task to work changes its status and takes the readiness away without any write. A state task completed WITHOUT a valid approval (force-complete, decide accept) is not ready; a human `return-to-work` is allowed from `completed` for state tasks only (sets `unfrozen_generation`, clears `completed_at`), then a new submission (next generation) and APPROVED make it ready.
- A new surface that reads a task in review must branch on the predicate or call the readers above.

### Verdict of a commit task: generation and revision (#1706)

- `TaskReviewVerdict.expected_generation` (read on the commit path too) and `supersedes_revision`; `TaskView.review_verdict_revision` and `ReviewBrief.review_verdict_revision` (not in the steward packet). Column `tasks.review_verdict_revision` (+ `review_verdict_digest`), migration in `hub/db.py`.
- CLI `review-verdict`: `--expected-generation` is required (no substitution, the refusal prints the brief command), `--supersedes-revision` for a human replacement. MCP `hub_submit_review` forwards `expected_generation` and warns without it; it has no `supersedes_revision` (replacement is human). Web form: hidden `expected_generation`; `supersedes_revision` goes only with the "replace" checkbox, which exists only while the task is still in `review` (a headless review) — the first client-driven verdict moves the task to `running` and the form disappears, so the card says outright that replacing a verdict is done through the CLI (`--supersedes-revision`) or REST; the refusal is shown in place. Review orders (`DispatchIntegration.build_review_message`, `agents/code-reviewer.md`) carry `--expected-generation`.
- Errors: 409 `verdict_already_stands`, `verdict_revision_stale`, `verdict_generation_moved`, `verdict_window_closed`, `verdict_not_written`; 403 `verdict_replacement_is_human_only`.

### Verdict evidence (#1697)

- Route `GET /api/tasks/{id}/verdict-evidence?generation=` (default: the current submission generation), in `hub/verdict_evidence_api.py` (kept out of `hub/app.py` for its file-size budget); logic and the one text formatter in `hub/services/verdict_evidence.py`. The web card parses the judgement through `verdict_evidence.parse_grounds`.
- Permission `verdict.evidence.read`, checked by `require_permission` (a watcher passes the "any GET" allowlist, so the permission is the only gate). Held by system roles `admin`, `operator`, `agent` and the env defaults `_HUMAN_DEFAULT_PERMS`/`_AGENT_DEFAULT_PERMS`; NOT by developer, viewer, security_admin, reviewer_agent, ci_runner, watcher, ci_reader, ci_auditor, steward, chat-pair sessions. Existing databases get it at start (`seed_system_roles` adds missing permissions to system roles; no data migration).
- Parts `steward`, `machine_review`, `ci` are independently `null`; for a state task `machine_review` and `ci` are `"not_applicable"`. CI time is `checked_at` of the row of the CURRENT attempt (`ci_report_attempts`, found by the projection's `attempt_key`), never the projection's `reported_at`; no attempt row: `attempt_pending=true`.
- MCP `hub_get_review_brief` (projection and `section=full` text only) and CLI `oc-hub review-brief` (stderr) / `oc-hub verdict-evidence <id>` print the same "Для вердикта" block. `ReviewBrief` (JSON) and the steward packet do NOT carry the judgement.

### Adding or changing a status

- `hub/models.py`
- lifecycle and orchestration logic
- poller transitions
- status rendering in UI
- tests covering transitions and progress

### Adding a new integration capability

- `hub/integrations/protocols.py`
- noop implementation
- concrete adapter
- registry wiring
- calling service or poller path

### Pair-start and the discipline skill already in the order (#1683)

- `hub_skill` in the `working_rules` of a pair-start answer carries
  `delivered_in_order=true` (with `version`, `chars`, empty `content`) only
  when the skill version recorded in the CALLER's own cloud order
  (`executor_runs.ordered_skill_version`) equals the active version. MCP and CLI
  print the line "текст навыка v<N> уже в заказе этого прогона".
- The link is the caller's token only: `executor_runs.id` →
  `chat_pair_codes.executor_run_id` → `chat_pair_sessions.executor_run_id`
  (inherited at redeem), checked against task and generation. Any gap (manual
  code, NULL version, other task or generation, revoked session) means the full
  text. A changed version sends the full text plus `ordered_version` and
  `version_changed=true`.
- Not affected: `GET /context`, `hub_my_context(mode=full)`, local pair-start
  (human, agent key), the order prompt (always the full skill).
- The fact of issue (`executor_run_id`, `skill_version`, `delivered_in_order`,
  `omitted_chars`) is in the detail of the `task_pair_started` activity entry.

### Executor host orders: the hub issues the code to a slot (#1712)

Human-only (a person, CSRF for cookies):

- `POST /api/admin/executor-hosts {name, public_key_pem, expires_days=90}` →
  host + service principal `executor-host-<name>` (role `slot_runner`) + API key
  shown ONCE (`api_key`). `public_key_pem`: RSA ≥ 3072, PEM SPKI.
  `POST /api/admin/executor-hosts/{name}/disable`, `GET /api/admin/executor-hosts`.
  CLI: `oc-hub executor-host add|disable|list`.
- `POST /api/projects/{slug}/executor-launch {target: "cloud"|"slot:<host>",
  task_id?}` (no body = cloud, queue head, as before). `slot:<host>` needs the
  project in `executor_launch=manual`, an `open` task of that project (`task_id`
  or the queue head), a free enabled host; answer carries `order_id`, `host`,
  never the code. Card button «Запустить на <host>» →
  `POST /tasks/{id}/web-executor-slot`. CLI: `oc-hub executor-launch`.

Host key (`slot_runner`), exactly three routes of its OWN host:

- `GET /api/executor-hosts/{name}/orders?wait=<=60` → `{orders: [{id, task_id,
  generation, executor_run_id, state, code_ciphertext, order_text, expires_at}]}`:
  every unclosed unredeemed order, repeated until acked or closed (stable `id`,
  dedupe on it); the first delivery sets `delivered_at`.
- `POST …/orders/{id}/ack {result: redeemed|redeem_failed|redeem_lost}` — idempotent;
  `redeemed` is informational (the hub sees the spend itself) and keeps the
  booking; the other two close the order, burn the code, abort the run. A repeat
  with a DIFFERENT result is 409 (the first word of the host stands). Foreign or
  unknown order: 403. At most 3 concurrent long-polls per host (429).
- `POST …/orders/{id}/report {outcome: submitted|rescued|failed, sha?, branch?,
  note?}` — one feed entry per order, repeat is a no-op, a different outcome is 409,
  `note` ≤ 500 chars without codes or tokens (422), never changes the task status;
  works on a closed order. `failed`/`rescued` also CLOSE the order (the run ended
  without a submission: unspent code burnt, run aborted, slot free); `submitted`
  does not (the landed submission closes it). `branch` is a WHITELIST: the task's own `tasks.branch` or `task-<order task id>/rescue-<digits>`, anything else is 422; `sha` is `^[0-9a-f]{7,40}$`; `note` has the secret and long-token checks (no heuristics on branch/sha).

Host contract: decrypt `code_ciphertext` (RSA-OAEP, SHA-256 in OAEP and MGF1, no
label, base64url without padding — reference `scripts/executor_host_decrypt.py`),
put the code into `order_text` instead of `{{PAIR_CODE}}`, redeem it at the
unchanged `POST /api/auth/chat-pair/redeem` (session `ahcp_`, `channel=slot`,
`slot=<host>`, bound to the task and generation), ack, then claim/pair-start with
`git_mode=remote`. The host key never enters the executor VM.

## Contract-Smell Checklist

- Does the same concept have different names across API, CLI, MCP, and DB?
- Did a new enum value get added without a migration, rendering path, or tests?
- Did a “small” contract change bypass `hub/models.py` and get hardcoded in a route?
- Did an adapter change leak implementation assumptions into core services?

### Decision records (#1698)

- Route `GET /api/decisions?project=&limit=` (limit 1..50, default 10), in `hub/decisions_api.py`; reader `hub/services/decisions.list_decisions`. Response `{"decisions": [...], "source": "hub"}`; keys `id, task_id, task_title, project, content, agent, principal_id, author_kind, created_at`, order `created_at DESC, id DESC`.
- These are task-feed rows with `kind='decision'` WITH ORIGIN, not "owner decisions": `kind` is a free string, so an agent may write one too. Archived tasks and the project (task subtree, descendants inherit it) are filtered in the same query, BEFORE `LIMIT`. Other journals (dispatcher events, steward judgements, review verdicts, approve/reject) are not sources.
- Access is the same as `GET /api/dashboard` (no permission of its own; the middleware allowlists decide: watcher reads, steward/chat-pair/ci_reader/ci_auditor are refused). Consumers: MCP `hub_list_decisions`, the dashboard (`recent_decisions`, 8) and `hub_project_status`.
- The CLI link to n4l is gone (`hub/integrations/notes.py`, `N4L_BIN`, `N4L_SPACE`, `NotesPlugin`). `record_decision` is still accepted on REST/CLI/MCP and ignored; `GET /api/integrations/notes` answers 410.
