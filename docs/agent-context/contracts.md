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
- CLI `review-verdict` (#1696): by default one line (`<verdict>, вердикт на поколение N (текущий|устарел|текущесть неизвестна), статус S, дальше: <next_action>`) plus the hub's `lifecycle_hint` lines; N is `review_verdict_generation` of the recorded verdict, currency is `latest_review.is_current` of a follow-up `GET /api/tasks/{id}`, next_action is `build_mutation_envelope` (same as MCP). `--json` prints the REST body as before; REST is unchanged. MCP `hub_submit_review` appends `Lifecycle: <lifecycle_hint>` to `message`. Subparser lives in `hub/cli_review_verdict.py`.
- `TaskReviewVerdict.pair_miss` / `pair_miss_note` (#2139): a human marks a `changes_requested` return as NOT a miss of the steward pair (wording, not a defect the pair should have caught). `pair_miss` accepts only JSON `false` (or absent/`null` = a pair miss, as before); the published schema is `enum [false, null]` and any other value (`true`, `"false"`, `0`, `"no"`, a list) -> 422 `pair_miss_invalid`. `pair_miss_note` is mandatory and non-blank with `pair_miss=false` (422 `pair_miss_invalid` otherwise, also for a note without the mark or the mark on `approved`). Only a human/admin credential may send either field; an agent or any non-human caller -> 403 `pair_miss_human_only`, nothing written. The value is stored in the payload of the `review_verdict_recorded` event (`pair_miss: false`, `pair_miss_note`), nowhere else; trigger `trg_false_approve_human_return` (recreated by `m202610290900_2139_pair_miss_trigger`) and `steward_exit._human_returns` skip the case ONLY on JSON false. The mark is part of the repeat digest. CLI `review-verdict --not-pair-miss NOTE`. MCP `hub_submit_review` has no such field (`tools/list` unchanged). Summary counter: `contour.human_returns_not_pair_miss_14d` (non-blocking; not in `false_approve` or `false_approve_events_14d`), line `human_returns_not_pair_miss_14d` in the effective-policy text.
- Errors: 409 `verdict_already_stands`, `verdict_revision_stale`, `verdict_generation_moved`, `verdict_window_closed`, `verdict_not_written`; 403 `verdict_replacement_is_human_only`; 403 `pair_miss_human_only`, 422 `pair_miss_invalid` (#2139).

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
  `POST /api/admin/executor-hosts/{name}/slots {slots: 1..4}` (#2107, human).
  CLI: `oc-hub executor-host add|disable|list|set-slots`.
- `POST /api/projects/{slug}/executor-launch {target: "cloud"|"slot:<host>",
  task_id?}` (no body = cloud, queue head, as before). `slot:<host>` needs the
  project in `executor_launch=manual`, an `open` task of that project (`task_id`
  or the queue head), an enabled host with a free place (fewer unclosed orders than `slots`); answer carries `order_id`, `host`,
  never the code. Card button «Запустить на <host>» →
  `POST /tasks/{id}/web-executor-slot`. CLI: `oc-hub executor-launch`.

Host key (`slot_runner`), exactly six order routes of its OWN host (plus the two critic routes below, #1953):

- `GET /api/executor-hosts/{name}/orders?wait=<=60` → `{orders: [{id, task_id,
  generation, executor_run_id, state, code_ciphertext, order_text, expires_at,
  run_seconds, project}]}` (#1960: the run deadline snapshotted at issue; #2122: `project` = slug of the project the order was issued under — the one `_chosen_task` matched to the task's owner — fixed in `executor_orders.project_slug` in the issuing INSERT and never recomputed, so a later rebinding of the task does not change it; `null` for orders issued before the migration, the host then checks the project after redeem as before; also present in `waiting_memory` orders; visible only to the addressee host):
  every unclosed unredeemed order, repeated until acked or closed (stable `id`,
  dedupe on it); the first delivery sets `delivered_at`.
- `POST …/orders/{id}/redeem {code}` (#1825) → the `chat-pair/redeem` answer plus
  `order_id`, `token_version`, `retry_until`. Only the order's own host, only the
  code of THAT order (any mismatch of host, order or code: one 403
  `order_unavailable`). The first call spends the code (same `_spend_code`, same
  session, `first_redeemed_at` and `retry_until` = first redeem + 600 s,
  `HAIPLANE_HOST_REDEEM_RETRY_SECONDS`, never extended, capped by the order's `run_seconds`).
  A repeat inside the window while the token was NEVER presented replaces the token
  hash of the SAME session (rotation, `token_version`+1, original session expiry,
  old token dead) — this is the safe retry after a lost answer (network, 502 during
  a deploy). 409 `already_redeemed_in_use`: the token was presented once (any
  request, GET/MCP and business errors included; `first_used_at` is set atomically
  at admission in `chat_pair.resolve_session`) — never rotated; 409
  `redeem_retry_expired` after `retry_until` (also after purge/restart; the order
  row keeps the window); 409 `order_closed`/`session_gone`/`host_disabled`; 429
  `redeem_retry_limited` (≤ 5 rotations per order, 60 attempts/5 min per host; it
  does not close the order). Repeats write no feed entries. Host side: store the
  order BEFORE the call; on timeout/5xx retry with backoff inside `retry_until`;
  ignore an answer with a smaller `token_version`; start the VM once; `redeem_lost`
  is the final refusal to recover.
- `GET …/orders/{id}` (#1831) → the outcome of the host's OWN order: `state`,
  `close_reason`, `task_id`, `task_status`, `submission_generation`,
  `submission_sha`, `branch`, `pr_number`, `report_outcome` — no code, ciphertext,
  tokens or order text. After a submission the run's sessions are revoked (#1783),
  so the host checks the submission with THIS read, not with the agent session.
  If the task was ordered again later (any host) the order is `superseded: true` and carries no task fields; likewise when the task's `submission_generation` moved past the order's `generation` (a resubmission by another run: cloud, manual pair). `submitted` is true only when the generations are equal; `submission_*` and `pr_number` are given only then. A submission is the order's own only when the order's run held the task (`tasks.executor_run_id` = the order's run; `settle` closes the order with `submitted` only then, and the order stays attributed after the holder is released); a resubmission of the SAME generation by cloud or a manual pair is foreign (`superseded`). A `submitted` order whose task went back to open/claimed/running is `superseded` too. A branch that carries a hub code or token is returned empty. Order, "newer order exists" and task are ONE query (one snapshot); a branch that looks like a secret is returned empty. Foreign, unknown and out-of-range order id: one 404; ≤ 60 reads/min per host (429).
- `POST …/orders/{id}/ack {result: redeemed|redeem_failed|redeem_lost}` — idempotent;
  `redeemed` is informational (the hub sees the spend itself) and keeps the
  booking; the other two close the order, burn the code, REVOKE the session while its token was never presented, abort the run; once the token was presented they are 409 `already_redeemed_in_use` and change nothing (#1825). A repeat
  with a DIFFERENT result is 409 (the first word of the host stands). Foreign or
  unknown order: 403. At most 3 concurrent long-polls per host (429).
- `POST …/orders/{id}/phase {phase: vm_starting|agent_running|critic|submitting}` (#1817)
  — the host tells which step the run is on; the card plaque shows the last accepted
  one. Only the host's own order that is FACTUALLY `redeemed` (not merely delivered),
  unclosed and without a report; phases go forward only, skipping is fine; the same
  phase again is 200 `duplicate`, backwards / before redeem / after report or close
  is 409, a foreign order is 403, an unknown phase 422, over 30/min per host 429.
  Phases are never written to the task feed. Old hosts that send none keep working.
- `POST …/orders/{id}/phase {phase: waiting_memory, need_mib, free_mib}` (#1972) — the ONE phase
  BEFORE redeem: the host has the order but no memory to start the VM. `need_mib` is an integer
  1..1048576, `free_mib` an integer 0..1048576 (0 is valid); a string, boolean, float, null or a missing
  field is 422 and nothing changes. Allowed from `pending`, `delivered` and `waiting_memory` (a repeat is a
  heartbeat). The host sends `waiting_memory` BEFORE redeem and repeats it at least every 5 minutes while it
  waits; the way out is the ordinary redeem. Each accepted heartbeat extends the order AND its code in one
  transaction to `min(now + 10 min, waiting_since + 6 h)` (server clock only); `waiting_since` is written once.
  409 when the order is already `redeemed`/`closed`, when the order or its code is past its term, missing or
  spent (a late heartbeat never revives), and at the 6 h limit (`memory_wait_limit`). At the limit `settle`
  closes the order with `close_reason=memory_wait_timeout` (one feed entry, one task alert, one event
  `executor_memory_wait_timeout`) and the host learns it by reading its own order (`GET …/orders/{id}`:
  `state=closed`, `close_reason`); vanishing from the orders list is not a confirmation. The `phase` column
  is not written, the allowlist is unchanged. A waiting order is still unclosed: it holds one of the host's
  `slots` places (#2107), the task stays busy, and `read_order_outcome` returns `state=waiting_memory`.
- `POST …/orders/{id}/report {outcome: submitted|rescued|failed, sha?, branch?,
  note?, reason_code?, reason?}` — NO feed entry (#1817: a host report only stores outcome, sha, branch, note and reason for the card plaque and `GET …/orders/{id}`; the ONE final feed entry of an order is written by the order's close, in the transaction that sets `state=closed`, from the actual outcome at that moment — the task submitted by this order's run → submission, otherwise the close reason with the host `reason_code`/`reason` as detail; a return of the task to open or a claim release of THIS order's run writes none of its own, any other run writes its normal entry), repeat is a no-op, a different outcome is 409, `note` ≤ 500 chars without codes or tokens (422), never changes the task status; works on a closed order. `reason_code` (#1817, #1941): channel_check_failed, setup_failed, redeem_failed, vm_failed, busy, over_budget, agent_failed, critic_failed, critic_cycles_exhausted, critic_time_exhausted, critic_unavailable, critic_delivery_failed, no_new_head, integrity_violation, timeout, hub_unavailable, stale_token, other (absent on failed/rescued from an old client = `other`; unknown value 422); `reason` ≤ 200 chars passes the same secret and order-code checks as `note`; the reason of the first report is immutable (a repeat with another `reason_code` is 409, an identical repeat is a no-op). `failed`/`rescued` also CLOSE the order (the run ended
  without a submission: unspent code burnt, run aborted, slot free); `submitted`
  does not (the landed submission closes it). `branch` is a WHITELIST: the task's own `tasks.branch` or `task-<order task id>/rescue-<digits>`, anything else is 422; `sha` is `^[0-9a-f]{7,40}$`; `note` has the secret and long-token checks (no heuristics on branch/sha).


Class of a failed or rescued run by `reason_code` (#1941; one table in `hub/services/executor_triage.py` `CODE_CLASS`, a code not in the table — including an unknown STORED code — is class 0 `unclassified`, never an error; an unknown INPUT code stays 422):

| reason_code | class | class_name |
|---|---|---|
| `critic_unavailable` | 1 | infra |
| `critic_delivery_failed` | 1 | infra |
| `channel_check_failed` | 1 | infra |
| `setup_failed` | 1 | infra |
| `redeem_failed` | 1 | infra |
| `vm_failed` | 1 | infra |
| `hub_unavailable` | 1 | infra |
| `stale_token` | 1 | infra |
| `busy` | 1 | infra |
| `over_budget` | 1 | infra |
| `timeout` | 1 | infra |
| `no_new_head` | 2 | no_new_head |
| `critic_cycles_exhausted` | 3 | critic_exhausted |
| `critic_time_exhausted` | 3 | critic_exhausted |
| `integrity_violation` | 4 | integrity |
| `critic_failed` | 0 | unclassified |
| `agent_failed` | 0 | unclassified |
| `other` | 0 | unclassified |

`last_executor_order` (#1941, task card: REST `GET /api/tasks/{id}`, MCP `hub_task_status` compact and full, `oc-hub status`, card line «После неудачного прогона»): `null` when the task has no host order; otherwise `{order_id, state, outcome, reason_code, class, class_name, after_failed_run}` plus, ONLY while `state=waiting_memory` (#1972), `waiting_memory: {host, since, last_seen_at, need_mib, free_mib, line}` where `line` is the sentence the hub builds for every surface (REST, MCP «Waiting for memory: …», `oc-hub status`, card row, launch plaque; times in the owner's timezone) (no order code, hash or ciphertext; `class`/`class_name` are null unless outcome is failed/rescued). `after_failed_run` is true only when the LAST host order is closed with outcome rescued/failed AND, in the same single SELECT snapshot (`executor_triage._SNAPSHOT_SQL`; never `executor_launch._live_run`, which writes), the task is `open`, not archived, has no holder (`claimed_by`, `claim_session_id`, `implementer_principal_id`), no live implementer session, no running or booked executor run (cloud booking included) and `submission_generation` below the order's generation (a canonical submission beats a later report failed/rescued). A newer order is the last order itself, so it is never "after a failed run". A read failure is `{"unavailable": <reason>}` with NO `after_failed_run` key (unknown is neither false nor true). The state is a read, not a decision: nothing is launched (invariants: a code for a slot only by a human's executor-launch); "reviewed by someone" is #1942. The second shadow entry «task back from review» (`rework_on_host`, #1947) reads the same snapshot (`close_reason`, `closed_at`, `latest_review` columns) and is not part of this projection.

Owner's triage of a failed k8 run (#1943): `POST /api/executor-triage/{recommendation_id}/verdict` body `{verdict: "accepted"|"rejected"}` (other value 422) → `{recommendation_id, task_id, owner_verdict}`; human only (`require_human_or_admin`; agent, steward and host keys 403); second call 409 (the first value stays); unknown id 404. Web twin `POST /triage/{id}/web-verdict` (form field `verdict`, htmx fragment). No MCP tool (human-only gate), no CLI. Inbox data `get_inbox_data`: `triage_items` (`recommendation_id, order_id, task_id, title, class, class_name, action, recommendation, reason, created_at`), `triage_unavailable` (reason or empty), `triage_note`. Digest payload `triage`: `{state: measured|unavailable, created, retry, owner, resolved, matched, unmeasured, late[, reason]}`; line «разобрано N, рекомендован повтор K, к владельцу M; совпало с вашим решением X из Y».

Critic orders for a cloud submission (#1953; hub side, the k8 side is #1952). Policy `gate_policy.critic_host` = the name of a registered executor host (absent or empty = off; PATCH validates). Table `critic_orders` (own table: `executor_orders` needs a task and an implementer code and its per-host `slots` booking would break). The ONLY way to create an order is a feed entry whose WHOLE text (after strip) is the one line `нужен критик: sha <40 hex>` (a line inside a longer entry, a quote or a code block is not a command) written through `POST /api/tasks/{id}/updates` (kind not `done`) by the LIVE implementer session of the CLOUD holder: session -> `executor_run_id` == `tasks.executor_run_id` and `executor_runs.channel='cloud'` of that task. Check order: session and holder, policy, line, quota ("open" everywhere means `state<>'closed' AND expires_at>now`; 1 open order on another sha; `HAIPLANE_CRITIC_ORDERS_PER_HOUR`, default 6), dedupe (open, or already passed / has findings on the same sha), and only then the single network observation of the branch tip (must equal the sha). Every refusal is a hub `alert` with a code (`not_cloud_holder`, `policy_off`, `request_line`, `quota_open`, `quota_hourly`, `duplicate`, `already_passed`, `findings_unchanged`, `head_moved`, `head_unobserved`, `proof_unreadable`, `race`; the order insert re-reads policy, holder, live session, quota and history under the write lock and first closes the task's overdue rows) and `executor_orders` / `executor_runs` are never written. After `failed`/`expired` a new order for the SAME sha is allowed. Context (`repo`, `branch`, `base`) is fixed at creation; TTL `HAIPLANE_CRITIC_ORDER_TTL_MIN` (default 45 min), expiry only by the hub (sweep `critic_orders` + checks at read, report and gate).
Host routes (key `slot_runner` of THAT host, `_host_caller`; +2 in `SLOT_RUNNER_ALLOWLIST`): `GET /api/executor-hosts/{name}/critic-orders?wait=<=60` -> `{host, orders: [{id, task_id, sha, repo, branch, base, state, expires_at}]}` (a delivered order is repeated until closed; at most 3 concurrent long-polls per host, the counter is released on cancel) and `POST …/critic-orders/{id}/report {outcome: passed|findings|failed, findings_p1, findings_p2, summary<=4000, host_ref}`: `passed` only with P1=P2=0 (422), `findings` needs P1 or P2 (422), bad `host_ref` (422); an identical repeat (compared by a digest of the SENT report, not of the truncated summary) is a 200 no-op, a changed one 409; a report after `expires_at` is 409 `critic_order_expired` and the order is closed `expired` (ONE conditional UPDATE decides the race); an order of another host is 404/403. One feed record per closing, atomic with it; the summary is untrusted and passes `untrusted_quote.sanitize_untrusted_quote` (control, bidi, zero-width, TAG, code spans, HTML and Markdown; shared vectors `tests/fixtures/untrusted_quote_vectors.json`).
Submission gate: a task whose holder is a cloud run, in a project with `critic_host`, is accepted by `submit-review` AND by `done` (pair, including `auto_review=false`; the poller entry holds the task in `needs_decision` with an alert) only with a `passed` order on the sha of the SAME single observation that is pinned as `submission_sha`; otherwise HTTP 422 `critic_not_passed` (`cause`: `no_order`, `order_open`, `findings`, `failed`, `expired`, `head_unobserved`, `sha_changed`, `proof_unreadable`) before the generation bump and before any write. `passed(A)` with tip B needs `passed(B)`. k8, local, no-holder tasks and a disabled policy: as before. Card: `critic_order` (`order_id, sha, state, outcome, expires_at, findings_p1, findings_p2, closed_reason, next_step`, or `{"unavailable": …}`) on REST `GET /api/tasks/{id}`, MCP `hub_task_status`, `oc-hub status` and the task page.

Host contract: decrypt `code_ciphertext` (RSA-OAEP, SHA-256 in OAEP and MGF1, no
label, base64url without padding — reference `scripts/executor_host_decrypt.py`),
put the code into `order_text` instead of `{{PAIR_CODE}}`, redeem it at
`POST /api/executor-hosts/{name}/orders/{id}/redeem` (#1825; the public
`POST /api/auth/chat-pair/redeem` still takes a host's code until the switch
`HAIPLANE_HOST_ORDER_PUBLIC_REDEEM_CLOSED=1`, default off — then the public route answers a host order code with the one indistinguishable 401; cloud and manual codes are never affected) (session `ahcp_`, `channel=slot`,
`slot=<host>`, bound to the task and generation), ack, then claim/pair-start with
`git_mode=remote`. The host key never enters the executor VM.

### Dry-run submission: `submit-review?dry_run=1` (#1975)

- REST `POST /api/tasks/{id}/submit-review?dry_run=1` (same route, same auth allowlist and same permissions as a real submission) runs the SAME steps in the SAME order — `SUBMIT_STEPS_BEFORE_SAME_SHA_CHECK`, then `SUBMIT_STEPS_AFTER_SAME_SHA_CHECK` without `delivery_pr` — and refuses with the same code and `detail` as a real submission. The pydantic body check happens first, as always. Success is 200 `SubmitDryRunView` (`hub/services/submit_dry_run.py`, not `hub/models.py`): `dry_run`, `would_submit`, `same_sha_noop`, `passed_steps` (executed steps in pipeline order; a step in mode off — by the pipeline or by the project policy of `submission_contract`/`bug_red_test`/`ci_before_submit` — is not listed), `outcome_hint` (`OutcomeHintView`), `notices` (warn gates and the `path_notices` compute). The route's `response_model` is `TaskView | SubmitDryRunView`; OpenAPI names both. CLI: `oc-hub submit-review --dry-run` sends `dry_run=1`, prints the JSON body (rc 0) or the refusal (rc != 0). MCP is out of scope (catalog headroom; the launcher uses REST), so `surface_parity` warns about `hub/mcp_server.py`/`hub/models.py` on purpose.
- What it writes: nothing in the hub tables — no generation, `submission_sha`, status, feed row, event, `finding_outcomes`, `path_notice_results`, CI tables, no executor order/run/slot change, no defect draft; the pair session is not closed (#1783 closes it only on a real submission); no push or PR (`delivery_pr` is not run), no review order. There is NO `BEGIN`/`ROLLBACK` around the real pipeline: the CI refresh runs only outside a transaction and `BEGIN IMMEDIATE` under the network would hold the shared lock. Instead the writes were separated from the computation: `ci_trust.preview_report` asks GitHub like `refresh` and returns the projection the refresh would leave (`ci_trust.plan_projection`/`projected_row`, the same plan `_project` saves); `bug_red_test` and `ci_before_submit` read it through `SubmitContext.ci_computed`; `expected_sha_gate.head_refusal` computes the 409, `refuse_unless_head_is_expected` writes the alert only outside a dry run. A real submission saves exactly as before.
- Same SHA from `review` (#1265): the answered-uid filter (`finding_outcome.items_still_to_plan`), `plan_outcomes`, and with `accept_areas` `resolve_diff` + `surfaces` repeat `_same_sha_noop_response` without its writes; area and risk are computed, not saved; `path_notices` shows the stored result of the generation. State tasks (`result_kind=state`): 422 `dry_run_unsupported_for_state`, no writes.
- Allowed side effects (named, not hidden): `first_used_at` of a new `ahcp_` session token (authentication), `git fetch` and the refs cache, the in-process `_observed_tips`, GitHub reads. Sources of `notices`: `surface_note`, `outcome_note`, `rule_lines`, `unchecked_lines`, `contract_alert`, `ci_before_alert`, `risk_alert`, an `alert`-kind red-test record, and `path_notices.compute` (never `record`).
- Guarantee boundary: a successful dry run does NOT promise that the real submission passes. The real one re-reads the state and the CI standing under the lock (`recheck_in_transaction`, the critic acceptance `critic_orders.accept_in_transaction`), and the world may move between the two calls. The read-only `critic_passed` step (#1953) is part of the dry run like any other step of the pipeline.

### CI contract of a project (#1848)

- `gate_policy.ci_contract = {"workflow_path": ".github/workflows/<file>.yml|yaml", "required_checks": [<subset of lint, format, types, tests>]}`; sorted on write, extra fields refused, project `default` and `policy-schedule` refused, human PATCH only (`models.validated_gate_policy` -> `ci_contract.validated_ci_contract`; reader `project_policy.ci_contract_from`; shown by `effective-policy` as `ci_contract` with `fingerprint`). Storage: `ci_report_attempts.contract_fp` and `ci_run_reports.contract_fp` (migration `add_ci_*_contract_fp`, DEFAULT = the hub contract's fingerprint; `''` = no fingerprint).
- Signatures: `ci_trust.trust_of(report, current_fp)`, `is_verified(report, current_fp)`, `can_testify_pass(report, current_fp)`, `green_gap(report, contract)`, `ci_green_proof.proof_gap(report, ac_rows, sha, contract)`, `check_attempt(row, gh_args, contract)`; forge `ci_runs_for_sha(head_sha, ..., workflow_path=".github/workflows/ci.yml")`. The REST/CLI/MCP surfaces carry no new field: the fingerprint is seen in `effective-policy` only.
- Sender: `scripts/ci_trusted_report.py` reports the workflow named by `HAIPLANE_CI_WORKFLOW_PATH` (set by the default-branch sender workflow through the `workflow-path` input of `.github/actions/hub-ci-send`), else `ci.yml`. Templates: `hub/workflow_templates/satellite-ci-producer.yml` (branch code, read key only) and `satellite-ci-report.yml` (`workflow_run`, environment `hub-report`); rendered by `hub.services.satellite_ci`. Setup, rollback and the acceptance evidence: `docs/satellite-ci-report.md`.

### Executor autonomy in practice metrics (#1980)

`GET /api/metrics/practices` (also `oc-hub practice-metrics`, `hub_practice_metrics` `metrics`) carries `executor_autonomy`, JSON only (no text summary, no new argument). Code: `hub/services/executor_autonomy.py`; the section is in `MODEL_INDEPENDENT` (window and project apply, the reviewer-model filter does not), not in `UNFILTERED`.

```json
"executor_autonomy": {
  "target": 0.9,
  "definition": "autonomous = report_outcome 'submitted' AND close_reason 'submitted' ...",
  "hosts": {"k8": {"weeks": [
    {"week": "2026-W43", "start": "2026-10-19", "partial": false,
     "orders_closed": 5, "autonomous": 2, "rate": 0.4,
     "by_outcome": {"submitted": 3, "rescued": 1, "failed": 1},
     "by_reason_code": {"agent_failed": 2, "unknown": 1}}
  ]}}
}
```

- Sample: `executor_orders` with `state='closed'` and `closed_at` not NULL, windowed and project-scoped by `scope.where("o.closed_at", task_column="o.task_id")` (project comes from the epic). One host = one key; hosts are never mixed. The unit is an order, not a task.
- `autonomous` = `report_outcome='submitted'` AND `close_reason='submitted'`. It is a PROXY for "without a human", not proof. A late report (`reported_at` after `closed_at`) recounts the week of closing, so a past week can change.
- Weeks are ISO (Monday), UTC; the key carries the ISO year (`2026-W53`, `2027-W01`). Every week of the window is present, zero weeks with `orders_closed=0` and `rate=null`; the edge weeks cut by the window are `partial=true`. A window longer than 104 weeks starts at the first closed order. `rate` is rounded to 3 digits.
- `by_outcome` covers all orders (empty `report_outcome` = `none`); `by_reason_code` only the non-autonomous ones (empty = `unknown`), no truncation, order: count descending, then code.

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
