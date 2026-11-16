# Private project foundation (#2350)

This is an internal contract for epic #2345. It does **not** enable private
projects through the existing REST, Web, CLI or MCP routes. Those routes still
have their legacy access behavior. Do not expose these writers until #2351,
#2352 and the execution isolation packages enforce the complete boundary.

## Storage and writers

`hub/repository_private_projects.py` owns the internal creation and ownership
transfer transactions. Its arguments are trusted server inputs after both Hub
authorization and live GitHub delegation verification. They must never be
mapped directly from a legacy project create/patch payload. External verification,
user credentials, installation state and import endpoints belong to later tasks.

`private_project_security` marks projects that have explicit ownership. It has
a non-null owner and a deferred key to that owner's membership. The partial
unique membership index prevents two owners; the reverse key prevents zero.
An active human's metadata-only account link is required by each writer.
The binding's composite foreign keys tie project, owner and account link together.
Credentials must not be stored in these tables. Account-link registration and
validation belong to the GitHub connection flow, not to a public CRUD endpoint.

Project identity is an opaque server-generated slug. Workspace allocation keys
are distinct per project (`private/<opaque slug>`); they are not an execution
sandbox or a validated absolute filesystem path. The future isolated workspace
allocator must resolve them under its configured root. No clone or directory is
created by this foundation. Repository identity uses GitHub's numeric ID; the
owner/repository pair is unique. An identical import retry returns the same
project; a changed intent raises `ValueError`. Different owners have independent
project rows and allocation keys, even for the same repository and display name.

`transfer_ownership` uses `BEGIN IMMEDIATE` and `expected_epoch` as a compare and
swap. A stale epoch raises `OwnershipConflict`. The transaction replaces the
owner membership and delegating account link together, advances `auth_epoch`
and revokes pending invitations. The old owner retains no membership. Existing
non-owner members remain members. A duplicate owner/repository pair or any
constraint failure rolls the whole transfer back. A nested failed writer rolls
back its savepoint without committing or erasing the caller's transaction.

Invitation storage permits member/viewer roles only, one pending invitation
per recipient/project and a unique 64-character token hash. Expiry, secure token
generation, recipient-bound acceptance and permission checks are implemented in
#2353; there is no invitation API in this change.

## Authorization decision

`hub/services/project_access.py::decide` consumes the authenticated
`TokenIdentity`. The permission is an intersection of its system permissions,
the active DB principal's membership and the allowed project-role verbs.
Global roles, including admin, do not grant membership. An owner with developer
system permissions cannot acquire human-gate privileges through ownership.
Viewer membership remains read-only even for a system operator.

For an agent/service, membership alone is insufficient: a trusted server-issued
`Delegation` must match principal, project, task, epoch, permission and a future
timezone-aware expiry. A delegation is an in-process server contract, **not** a
client payload or a bearer credential. Issuance/persistence and order delivery
belong to #2352. Agents cannot execute human gates or manage project ownership.
API-key project scopes and task-bound chat-pair scopes further narrow access.

Task ancestry is resolved from the database, with cycle/depth guards and no
default-project fallback. A supplied project must equal the server result.
Unknown, unassigned, legacy-unowned, archived or inactive projects deny access.
Deny results contain no foreign project identifier or reason; future handlers
must apply the specified 404/403 policy without leaking existence.

Decisions are snapshots, not reusable grants. Before a write or external side
effect, the caller must recheck authorization in the operation's transaction
and supply the observed epoch. Future membership, binding and delegation writers
must advance the epoch whenever authority changes. Read streams must recheck
each batch. Read-only authorization does not require a working GitHub binding;
GitHub operations additionally require live connection/delegation validation.
Break-glass access is not implemented; no global-role bypass is provided.

## Capabilities and compatibility

`hub/role_catalog.py` declares `github.self.connect`, `projects.self.create` and
`projects.self.manage`. System role seeds grant them to super_admin, admin,
operator and developer. They do not add `admin.credentials.write` to operator or
developer, and do not grant these capabilities to viewers or agents.
`self_service_capabilities` additionally requires an active DB human. Public
enforcement remains declared-only until the corresponding endpoints ship.
The established catalog names remain re-exported by `hub.db`; role seeding and
existing consumers retain their contracts.

## Upgrade and activation plan

1. Back up the database using the existing deployment procedure. Apply the new
   append-only migration on startup. No existing owner is guessed or assigned.
2. Existing projects remain legacy-unassigned: no sidecar/membership/binding
   backfill occurs. The strict decision denies them. The unchanged legacy
   routes remain usable while the new mode is unavailable.
3. After all access and execution boundaries are delivered, present legacy
   projects to the authorized migration operator in the migration UI (#2364).
   Explicit ownership confirmation and verified GitHub import/rebind are needed.
   Migrate each project in one transaction; do not turn global roles into members.
4. Enable the new flow only after the epic's cross-user and cross-agent acceptance
   suite passes. Creating sidecar rows directly on a live legacy deployment is
   not an activation procedure. Roll back the application with the new mode
   disabled; retain the additive metadata and all project data.

Tests live in `tests/test_private_project_schema.py`, including the three named
AC tests, upgrade/replay, independent database connections, nested rollback,
scope intersections and expired/foreign/stale agent delegations.
