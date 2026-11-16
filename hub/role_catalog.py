"""System role and permission vocabulary, re-exported by hub.db for compatibility."""

SELF_SERVICE_PERMISSIONS = (
    "github.self.connect",
    "projects.self.create",
    "projects.self.manage",
)


ALL_PERMISSIONS: tuple[str, ...] = (
    *SELF_SERVICE_PERMISSIONS,
    "admin.read",
    "admin.users.write",
    "admin.agents.write",
    "admin.roles.write",
    "admin.credentials.write",
    "admin.audit.read",
    "tasks.read",
    "tasks.create",
    "tasks.refine",
    "tasks.update",
    "tasks.human_gate",
    "tasks.agent_report",
    "tasks.decision",
    "tasks.archive",
    "tasks.delete",
    # #546: report the outcome of a run, and nothing else. Deliberately absent
    # from the agent and human defaults in config.py: a token that lives in a
    # CI secret must not be able to move a task or write a verdict, so it can
    # only be granted through a DB-backed principal holding the ci_runner role.
    "tasks.ci_report",
    # #495: record what was deployed, and nothing else. Same reasoning as
    # tasks.ci_report above — this token lives in a GitHub secret, so it gets
    # the one verb it needs. Separate from ci_report because the facts are
    # different: one is "the tests ran on this commit", the other is "this
    # commit is what production is running".
    "deploys.record",
    "integrations.vast.manage",
    "system.settings.write",
    # #1021: the steward principal's two verbs. Asked by the two allowed
    # routes; the rest of the surface is refused by the allowlist, not by
    # omitting these from some other role.
    "steward.evidence.read",
    "steward.judgement.write",
    # #1697: the steward's judgement text and the machine report of a
    # generation, for the person (or session) who rules on it. Not the
    # steward's own verb: it must not read what it said before.
    "verdict.evidence.read",
)

# #614: which of the permissions above actually decide anything.
#
# Handing a permission out in a role, showing it in the admin UI, and checking
# it in code are three different things, and until now only the first two were
# visible. Nine of the eighteen were consulted by nothing at all — so a role
# looked narrow while its narrowness was decorative. That is not an open door
# (human gates are held by require_human_or_admin, _reject_agent_authored_source
# and the review gate, not by these strings), but it is a promise the system
# does not keep: in #613 the ci_runner role was described to the owner as unable
# to do anything but report, and the CI token could in fact file drafts, because
# tasks.create is asked by nobody.
#
# Kept rather than deleted: the vocabulary is worth having when a gate is
# actually wanted. Enforcing any of these is a separate decision with its own
# blast radius — enforcing tasks.create today would break #611, whose audit
# reporter files drafts with exactly the ci_runner role.
#
# tests/test_auth.py derives the real answer FROM THE CODE and compares it with
# these two tuples, failing in both directions: a permission missing from both,
# and a "declared only" permission that has started to gate something. Two
# hand-written lists agreeing with each other and both wrong is the defect this
# task exists to remove.
ENFORCED_PERMISSIONS: tuple[str, ...] = (
    "admin.read",
    "admin.users.write",
    "admin.roles.write",
    "admin.credentials.write",
    "admin.audit.read",
    "tasks.archive",
    "tasks.delete",
    "tasks.ci_report",
    "deploys.record",
    # Consulted indirectly: config.py reads it to answer is_human, and
    # require_human_or_admin is built on that — so it does gate, via one hop.
    "tasks.human_gate",
    "steward.evidence.read",
    "steward.judgement.write",
    "verdict.evidence.read",
)

DECLARED_ONLY_PERMISSIONS: tuple[str, ...] = (
    # Public enforcement ships with the self-service surfaces.
    *SELF_SERVICE_PERMISSIONS,
    "admin.agents.write",
    "tasks.read",
    "tasks.create",
    "tasks.refine",
    "tasks.update",
    "tasks.agent_report",
    "tasks.decision",
    "integrations.vast.manage",
    "system.settings.write",
)


SYSTEM_ROLES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "super_admin",
        "Super Admin",
        "Full access, bootstrap-only, cannot be deleted",
        ALL_PERMISSIONS,
    ),
    (
        "admin",
        "Admin",
        "Manage users, agents, keys, settings",
        (
            *SELF_SERVICE_PERMISSIONS,
            "admin.read",
            "admin.users.write",
            "admin.agents.write",
            "admin.roles.write",
            "admin.credentials.write",
            "admin.audit.read",
            "tasks.read",
            "tasks.create",
            "tasks.refine",
            "tasks.update",
            "tasks.human_gate",
            "tasks.decision",
            "tasks.archive",
            "tasks.delete",
            "integrations.vast.manage",
            "verdict.evidence.read",
        ),
    ),
    (
        "operator",
        "Operator",
        "Human gates, task dispatch, decisions, force-complete",
        (
            *SELF_SERVICE_PERMISSIONS,
            "tasks.read",
            "tasks.create",
            "tasks.refine",
            "tasks.update",
            "tasks.human_gate",
            "tasks.decision",
            "tasks.archive",
            "tasks.delete",
            "integrations.vast.manage",
            "verdict.evidence.read",
        ),
    ),
    (
        "developer",
        "Developer",
        "Create and manage tasks without admin access",
        (
            *SELF_SERVICE_PERMISSIONS,
            "tasks.read",
            "tasks.create",
            "tasks.refine",
            "tasks.update",
            "tasks.archive",
        ),
    ),
    (
        "viewer",
        "Viewer",
        "Read-only dashboard and tasks",
        ("tasks.read",),
    ),
    (
        "agent",
        "Agent",
        "AI agent: propose, update, question, report done",
        (
            "tasks.read",
            "tasks.create",
            "tasks.refine",
            "tasks.update",
            "tasks.agent_report",
            "verdict.evidence.read",
        ),
    ),
    (
        "reviewer_agent",
        "Reviewer Agent",
        "Agent with review/report permissions",
        (
            "tasks.read",
            "tasks.create",
            "tasks.refine",
            "tasks.update",
            "tasks.agent_report",
        ),
    ),
    (
        "security_admin",
        "Security Admin",
        "Audit and security settings only",
        ("admin.read", "admin.audit.read"),
    ),
    (
        # #546: the identity a CI runner authenticates as. Read a task's plan,
        # report what the run produced — that is the whole job. No update, no
        # agent_report, no human gate: this token lives in a GitHub secret, so
        # its blast radius is the one thing it needs to do.
        "ci_runner",
        "CI Runner",
        "Read tasks and report run results; cannot change task state",
        ("tasks.read", "tasks.ci_report", "deploys.record"),
    ),
    (
        # #1021: judgement, not action. Two verbs only; chat-pair's four would
        # let this principal write the statement it then judges.
        "steward",
        "Steward",
        "Read the evidence pack and write a judgement; cannot change task state",
        ("steward.evidence.read", "steward.judgement.write"),
    ),
    (
        # #1556: read-only. One verb; the route allowlist in hub/auth.py is
        # the real gate (GET/HEAD on /api/* and /mcp), not this permission.
        "watcher",
        "Watcher",
        "Только чтение: задачи, ревью, метрики, прод-состояние; не меняет ничего",
        ("tasks.read",),
    ),
    (
        # #1686: the key the CI job of a TASK BRANCH holds. Its code is the
        # branch's code, so the key can do one thing: read the card (AC test
        # refs, validation commands) of a task of its own project. The route
        # allowlist in hub/auth.py is the real gate; seeding is idempotent, so
        # an existing database gets the role at the next start.
        "ci_reader",
        "CI Reader",
        "Читает карточку задачи своего проекта; ничего не пишет и не сообщает",
        ("tasks.read",),
    ),
    (
        # #1686: the key of the trusted CI workflow that files pip-audit drafts.
        # One route (POST /api/tasks, client_request_id pip-audit:<id>,
        # source=agent) inside its own project. It holds neither tasks.ci_report
        # nor deploys.record: it cannot report a run.
        "ci_auditor",
        "CI Auditor",
        "Заводит черновики аудита зависимостей (pip-audit:<id>) в своём проекте",
        ("tasks.create",),
    ),
    (
        # #1712: ключ хоста исполнителя (k8). Три маршрута своего хоста —
        # заказы, ack, итог прогона; их держит allowlist в hub/auth.py, а не
        # права. Прав у роли нет НАРОЧНО: пустой набор у DB-принципала читался
        # бы как «права по умолчанию агента», поэтому has_permission роли
        # slot_runner отказывает сам и не смотрит на набор.
        "slot_runner",
        "Slot Runner",
        "Хост исполнителя: забирает свои заказы, подтверждает, сообщает итог",
        (),
    ),
)
