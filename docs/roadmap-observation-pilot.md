# Roadmap observation pilot #2406

The local Hub operator activates approval event 26249 for #2406 revision 2,
project `home-infra`, steward principal 42 and roadmap principal 31. The
command is available after the server release and migration. Run it on the
Hub server as the database owner, with the service configuration and its
configured database path.

```sh
uv run python -m hub.services.roadmap_pilot_activation --db /path/to/hub.db --apply
```

The operation records the owner approval and the local operator separately.
The scoped grant expires 24 hours after activation. Repeating the command
returns the existing grant. Expiry or revocation ends the mandate and a repeat
cannot renew it. Revocation uses the existing administrator
`DELETE /api/tasks/2406/roadmap-grant`. Keep #2406 active on the exact approved
revision through the live check. A status of review, completed, or archival
ends current authority. The #2404 grant and role flow continue independently.

## Host protocol

Use the existing k8 executor host key with prefix
`/api/executor-hosts/k8/roadmap-pilot/2406`.

- `GET /observations` returns `activation_id` (grant ID or null) and
  `observation_permitted`. Sample after obtaining a permitted activation ID.
- `POST /observations` has required `activation_id`, lowercase UUIDv4
  `observation_id`, and `codex_ready`, `openai_ready`, `repository_read`.
  The three check values are true for success, false for an observed failure,
  or null when unavailable. The Hub assigns UTC receipt times. It accepts at
  most two samples, at least 60 seconds apart, within ten minutes of activation.
  A retry must use the same UUID and identical check values.
- `GET /effect` returns `effect_id` or null. A previously claimed effect stays
  visible while the grant is current so the host can retry reporting.
- `POST /effects/{effect_id}/claim` expects `{}`. Only the first successful
  claim returns `execute:true`. If the first response is lost or the host was
  interrupted, treat the physical read as uncertain and record `unknown`.
- `POST /effects/{effect_id}/report` accepts `outcome` of `read`, `failed` or
  `unknown`. Supply the exact lowercase 40-character Git commit in `version`
  for `read` and null otherwise. An identical report is a no-op.

The host keeps the read scoped to the approved target. The Hub records the
reported commit and the consuming host identity. Credentials, probe output and
repository contents stay off the wire.

## Steward and roadmap protocol

The steward fetches `GET /api/tasks/2406/roadmap-grant` using its existing
dedicated key. A sealed batch exposes `observation_batch_id`, including an
incomplete batch after the cutoff. Send it to the existing task-scoped request
endpoint with `grant_id`, `plan_id`, `request_id` and `question`. Use a durable
`request_id`. The batch permits one order.

The existing started roadmap context includes the batch and the bounded
recommendations. The roadmap uses its own principal and the order-bound source
header. It supplies the selected recommendation, risk `unknown` and
`basis: {"observation_batch_id": <integer>}` through the existing host role
report. The host and steward can each see only their authorized surfaces.
A consultation must start within 15 minutes of the second observation or the
measurement cutoff, whichever sealed the batch. Once the deadline passes, a
repeated request records a technical block if order issuance failed. The grant
then hides the batch offer and exposes `consultation_block_reason` and
`consultation_blocked_at`. The request response has `order_id: null`,
`status: "blocked"` and `reason: "consultation_failed"`. A technical block
has no decision ID or read permit. Started work uses the normal role-order
runtime limit.

When the grant lists `pending_decision_order_id`, POST the existing decision
endpoint with `grant_id`. The Hub verifies all batches and recommendations.
Two full successful samples plus a `verify_version_read` recommendation permit
one read claim. `hold_pilot` records confirmed failure. `insufficient_facts`
records missing evidence. `consultation_failed` and `consultation_invalid`
identify unavailable or unusable advice. These states provide no read permit.
The owner receives an audit record attributable to the acting identities.

The machine protocol uses REST. Host and steward keys intentionally have no MCP
tool access. The existing `hp-hub roadmap request` command supports
`--observation-batch-id` for the scoped steward API.
