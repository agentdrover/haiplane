"""OpenClaw Hub — FastAPI application with REST API and web dashboard."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import uvicorn
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from hub import config, services
from hub import db as db_module
from hub import repository as repo
from hub.db import get_db
from hub.integrations.registry import plugins
from hub.workflow_reference import lifecycle_map_lines
from hub.models import (
    ACLocatorResolution,
    CallSiteEntry,
    CallSiteSection,
    ACTestResultView,
    CIRunReportResult,
    CIRunReportState,
    CIRunReportSubmit,
    BulkChildTasksCreate,
    BulkRefine,
    BulkRefineResult,
    AcceptanceCriterion,
    ActivityItem,
    DashboardData,
    HealthView,
    IdentityDiagnosticsView,
    ReadinessReport,
    ReadinessTreeReport,
    BatchApprove,
    BatchApproveResult,
    FindingScope,
    ReviewBrief,
    TaskAnswer,
    TaskReviewVerdict,
    TaskSubmitReview,
    TaskApprove,
    TaskArchive,
    TaskClaim,
    TaskContextView,
    TaskCreate,
    TaskProjectRef,
    MachineReviewSubmit,
    MachineReviewView,
    ProjectCreate,
    ProjectPatch,
    ProjectView,
    SkillCreate,
    SkillView,
    TaskDecide,
    TaskForceComplete,
    TaskQuestion,
    TaskRefine,
    TaskPairStart,
    TaskReject,
    TaskRelease,
    TaskReorder,
    TaskRisk,
    TaskSource,
    TaskStart,
    TaskTreeNode,
    TaskUnarchive,
    TaskUpdateCreate,
    TaskUpdateView,
    TaskView,
    WhoamiView,
)
from hub.actionable_errors import agent_create_forbidden_detail
from hub.auth import (
    AuthMiddleware,
    current_identity,
    require_agent_caller,
    require_human_or_admin,
    require_permission,
)
from hub.host_security import HostAllowlistMiddleware
from hub.mcp_http_compat import McpStreamableAcceptCompatMiddleware
from hub.mcp_server import mcp as mcp_server
from hub.services.refinement import (
    DuplicateAcceptanceCriterionError,
    ProjectBindError,
    LimitExceededError,
    TaskNotFoundError,
    get_write_lock,
)
from hub.services.diagnostics import (
    build_health,
    build_identity_diagnostics,
    build_whoami,
)
from hub.services.ac_tests import current_ac_test_results, run_ac_tests
from hub.services.ci_report import accept_ci_run_report, ci_report_state
from hub.services.validation_run import run_validation_commands
from hub.services.task_idempotency import resolve_client_request_id
from hub.services import call_sites
from hub.services.test_existence import collect_test_nodeids, resolve_ac_locators
from hub.services.tree_output import (
    TreeOutputOptions,
    apply_tree_limits,
    truncate_text,
)
from hub.poller import start_poller
from hub.web import router as web_router

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
log = logging.getLogger("hub")

HERE = Path(__file__).parent


def _register_plugins() -> None:
    """Register concrete integration plugins based on available binaries/config."""
    from pathlib import Path

    if config.DISPATCH_BIN and Path(config.DISPATCH_BIN).exists():
        from hub.integrations.dispatch import DispatchIntegration

        plugins.dispatch = DispatchIntegration()

    # #378: git ops depend on the git binary, not on the default-project
    # workspace. clone_repo (provisioning) must work on a fresh server;
    # operations that DO need the default workspace degrade readably
    # inside git_ops instead of silently disabling the whole plugin.
    import shutil

    if shutil.which("git"):
        from hub.integrations.git_ops import GitOpsIntegration

        plugins.git_ops = GitOpsIntegration()

    if config.GH_BIN:
        from hub.integrations.github import GitHubIntegration

        plugins.github = GitHubIntegration()

    if config.N4L_BIN:
        from hub.integrations.notes import NotesIntegration

        plugins.notes = NotesIntegration()

    if config.VAST_ENABLED and config.VAST_JOB_BIN:
        from hub.integrations.vast import VastIntegration

        plugins.vast = VastIntegration()

    if config.TRANSCRIPTS_DIR:
        from hub.integrations.transcripts import TranscriptsIntegration

        plugins.transcripts = TranscriptsIntegration()


# MCP streamable-HTTP ASGI app. We instantiate it once at import time so it
# can be mounted before lifespan runs; its session manager is started inside
# our own lifespan via Starlette's lifespan_context.
_mcp_streamable_app = mcp_server.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate_network_auth()
    _register_plugins()
    app.state.db = await get_db()
    log.info("Hub database ready at %s", config.HUB_DB_PATH)
    poll_task = start_poller(app)

    # Drive the MCP session manager lifespan inside ours so /mcp/* requests
    # have a live transport. Keeps everything in a single uvicorn process.
    mcp_lifespan = _mcp_streamable_app.router.lifespan_context(_mcp_streamable_app)
    try:
        async with mcp_lifespan:
            if config.HUB_TOKENS:
                log.info(
                    "Hub auth ENABLED (%d token(s) configured)",
                    len(config.HUB_TOKENS),
                )
            else:
                log.info(
                    "Hub auth DISABLED (open mode — set OPENCLAW_HUB_TOKENS to enable)"
                )

            # Workspace git health-check (#455): opt-in network probe so a
            # broken deploy key on the default workspace is loud, not silent.
            # Off by default to keep startup (and tests) free of network I/O.
            if os.environ.get("OPENCLAW_WORKSPACE_HEALTHCHECK") == "1":
                from hub.services.diagnostics import check_default_workspace_origin

                try:
                    await check_default_workspace_origin()
                except Exception:
                    log.warning("workspace origin health-check failed", exc_info=True)

            # Bootstrap token guard
            if config.HUB_BOOTSTRAP_TOKEN:
                from hub.db import has_active_admin

                if await has_active_admin(app.state.db):
                    log.warning(
                        "SECURITY: OPENCLAW_HUB_BOOTSTRAP_ADMIN_TOKEN is still set "
                        "but an admin already exists. Remove it from the environment "
                        "to prevent unauthorized bootstrap attempts."
                    )
            yield
    finally:
        poll_task.cancel()
        await app.state.db.close()


app = FastAPI(title="OpenClaw Hub", version="0.2.0", lifespan=lifespan)
app.add_middleware(AuthMiddleware)
# After Auth: runs first on the request — fixes MCP clients that omit Accept.
app.add_middleware(McpStreamableAcceptCompatMiddleware)
# Added last so it runs first. Empty allowlist preserves local/dev behavior;
# production deployments can set OPENCLAW_HUB_ALLOWED_HOSTS.
app.add_middleware(HostAllowlistMiddleware, allowed_hosts=config.HUB_ALLOWED_HOSTS)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
app.include_router(web_router)


def _db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


def _reject_agent_authored_source(request: Request, source: TaskSource) -> None:
    """Agents propose, humans create (#360).

    Initial status is derived from ``source``, and ``source`` used to be taken
    from the request body — so an agent could label its own request "human" and
    land a task in ``open``, or in ``running`` with run_immediately, past the
    draft gate. The gate lives here rather than in the MCP tool because a token
    reaches this endpoint directly; closing only the tool would be decoration.

    ``source=agent`` stays open to agents: that is the path hub_propose_task
    itself takes.
    """
    if source != TaskSource.agent and current_identity(request).is_agent:
        raise HTTPException(403, detail=agent_create_forbidden_detail())


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    """Liveness probe — always public, used by VPN / load balancer health checks."""
    return "ok"


@app.get("/health", response_model=HealthView)
async def health() -> HealthView:
    """Public service health snapshot without secrets or subprocess checks."""
    return build_health()


@app.get("/api/whoami", response_model=WhoamiView)
async def api_whoami(request: Request) -> WhoamiView:
    """Return the authenticated caller identity and permission summary."""
    return build_whoami(current_identity(request))


@app.get("/api/diagnostics/identity", response_model=IdentityDiagnosticsView)
async def api_diagnostics_identity(request: Request) -> IdentityDiagnosticsView:
    """Caller identity plus honest instance and workspace state (#452).

    ``connected_via`` reflects the address the client actually reached (the
    request Host), so ``config_mismatch`` catches a server whose
    OPENCLAW_HUB_URL disagrees with reality — the trap that had an operator
    editing prod while believing it was local.
    """
    connected_via = str(request.base_url).rstrip("/")
    return await build_identity_diagnostics(
        current_identity(request), connected_via=connected_via
    )


# ---------------------------------------------------------------------------
# REST API — Tasks
# ---------------------------------------------------------------------------


@app.post("/api/tasks", response_model=TaskView)
async def api_create_task(body: TaskCreate, request: Request, response: Response):
    _reject_agent_authored_source(request, body.source)
    idem_key = resolve_client_request_id(
        request.headers.get("X-Client-Request-Id"),
        body.client_request_id,
    )
    outcome = await services.create_task(
        _db(request),
        body,
        client_request_id=idem_key,
    )
    if idem_key:
        response.status_code = (
            status.HTTP_201_CREATED if outcome.is_new else status.HTTP_200_OK
        )
    return outcome.task


@app.post("/api/tasks/{parent_id}/subtasks", response_model=list[TaskView])
async def api_create_subtasks_bulk(
    parent_id: int,
    body: BulkChildTasksCreate,
    request: Request,
):
    """Atomically create multiple child tasks under ``parent_id``."""
    _reject_agent_authored_source(request, body.source)
    return await services.create_subtasks_bulk(_db(request), parent_id, body)


@app.post("/api/projects", response_model=ProjectView)
async def api_create_project(
    body: ProjectCreate,
    request: Request,
    identity=Depends(current_identity),
    background_tasks: BackgroundTasks = None,
):
    """Create a project (#338/#345).

    Humans create active projects. Agents PROPOSE: their projects start
    as ``pending`` and stay out of git routing until a human activates
    them (PATCH status=active). OPENCLAW_ALLOW_AGENT_PROJECTS=direct is
    the solo-mode opt-out.
    """
    db = _db(request)
    import json as _json

    if identity.is_agent and config.ALLOW_AGENT_PROJECTS != "direct":
        status_value = "pending"
    else:
        status_value = "active"
    # Write lock serializes check-then-insert: two concurrent creates with the
    # same slug would otherwise both pass the SELECT and the second INSERT
    # would surface as IntegrityError → 500 instead of the promised 409.
    async with get_write_lock(db):
        if await repo.get_project_by_slug(db, body.slug) is not None:
            raise HTTPException(409, f"project slug {body.slug!r} already exists")
        pid = await repo.create_project(
            db,
            slug=body.slug,
            name=body.name,
            repo_name=body.repo,
            workspace_path=body.workspace_path,
            default_branch=body.default_branch,
            default_branch_policy=_json.dumps(body.default_branch_policy),
            status=status_value,
        )
        await db.commit()
    await db_module.log_activity(
        db,
        "project_created",
        f"Project {body.slug} (#{pid}) created as {status_value} "
        f"by {identity.username}",
    )
    # Auto-provision (#347): an active project with repo+workspace starts
    # cloning right away — after the response, so a slow clone never blocks
    # creation and a git failure lands in provision_status, not in a 500.
    if (
        background_tasks is not None
        and status_value == "active"
        and body.repo.strip()
        and body.workspace_path.strip()
    ):
        background_tasks.add_task(
            services.provision_project, db, pid, actor=identity.username
        )
    row = await repo.get_project(db, pid)
    return ProjectView(**dict(row))


@app.get("/api/projects", response_model=list[ProjectView])
async def api_list_projects(
    request: Request,
    include_archived: bool = Query(default=False),
):
    rows = await repo.list_projects(_db(request), include_archived=include_archived)
    return [ProjectView(**dict(r)) for r in rows]


@app.patch("/api/projects/{project_id}", response_model=ProjectView)
async def api_patch_project(
    project_id: int,
    body: ProjectPatch,
    request: Request,
    _identity=Depends(require_human_or_admin),
):
    db = _db(request)
    before = await repo.get_project(db, project_id)
    if before is None:
        raise HTTPException(404, "project not found")
    fields = body.model_dump(exclude_unset=True)
    if (
        "default_branch_policy" in fields
        and fields["default_branch_policy"] is not None
    ):
        import json as _json

        fields["default_branch_policy"] = _json.dumps(fields["default_branch_policy"])
    if "gate_policy" in fields and fields["gate_policy"] is not None:
        import json as _json

        # #743: the hub never weakens oversight over itself — the default
        # project (the hub's own repo) refuses any 'auto' at any gate, from
        # any token. The rule lives here rather than in the model because it
        # needs to know WHICH project is being patched.
        if before["slug"] == "default" and any(
            v == "auto" for v in fields["gate_policy"].values()
        ):
            raise HTTPException(
                422,
                {
                    "error": "default_project_gate_locked",
                    "hint": (
                        "проект default (сам хаб) не принимает автопилот ни на "
                        "одном гейте; политика default всегда human"
                    ),
                },
            )
        fields["gate_policy"] = _json.dumps(fields["gate_policy"])
    if "archived" in fields and fields["archived"] is not None:
        fields["archived"] = int(fields["archived"])
    if fields:
        await repo.update_project(db, project_id, **fields)
        if fields.get("status") == "active" and before["status"] != "active":
            # Events feed (#349): a pending proposal became a real project.
            await repo.insert_event(
                db,
                kind="project_activated",
                project_id=project_id,
                actor="human",
                payload={"slug": before["slug"]},
            )
        await db.commit()
    row = await repo.get_project(db, project_id)
    return ProjectView(**dict(row))


@app.get("/api/events")
async def api_list_events(
    request: Request,
    since: int = Query(default=0, ge=0, description="Cursor: last seen event id"),
    wait: int = Query(
        default=0,
        ge=0,
        description="Long-poll seconds (capped at 60): block until events or timeout",
    ),
    kinds: str = Query(default="", description="Comma-separated kind filter"),
    limit: int = Query(default=100, ge=1, le=200),
):
    """Cursor-addressable events feed (#349).

    Returns transition events with id > ``since`` oldest-first plus
    ``next_cursor`` (last returned id; unchanged ``since`` when empty, so
    repeat calls are idempotent). With ``wait`` > 0 the request long-polls:
    periodic re-reads on asyncio.sleep — the shared write lock is never
    held between reads, so writers are not starved.
    """
    import json as _json
    import time as _time

    db = _db(request)
    kind_list = [k.strip() for k in kinds.split(",") if k.strip()] or None
    deadline = _time.monotonic() + min(wait, 60)
    while True:
        rows = await repo.list_events(db, since=since, kinds=kind_list, limit=limit)
        if rows or _time.monotonic() >= deadline:
            break
        await asyncio.sleep(1.0)

    events = []
    for r in rows:
        item = dict(r)
        try:
            item["payload"] = _json.loads(item.get("payload") or "{}")
        except (TypeError, ValueError):
            item["payload"] = {}
        events.append(item)
    return {
        "events": events,
        "next_cursor": events[-1]["id"] if events else since,
    }


@app.get("/api/skills", response_model=list[SkillView])
async def api_list_skills(request: Request):
    """Skills library (#380): latest version per name."""
    rows = await repo.list_skills(_db(request))
    return [SkillView(**dict(r)) for r in rows]


@app.get("/api/skills/{name}", response_model=SkillView)
async def api_get_skill(name: str, request: Request):
    """Active version of a skill — what agents should execute."""
    row = await repo.get_active_skill(_db(request), name)
    if row is None:
        raise HTTPException(404, f"no active skill named {name!r}")
    return SkillView(**dict(row))


@app.post("/api/skills", response_model=SkillView)
async def api_create_skill(
    body: SkillCreate,
    request: Request,
    identity=Depends(current_identity),
):
    """New skill version (#380). Draft-gate mirrors projects (#345):
    humans publish active versions, agents PROPOSE drafts."""
    import json as _json

    db = _db(request)
    status_value = "draft" if identity.is_agent else "active"
    skill_id, version = await repo.create_skill_version(
        db,
        name=body.name,
        kind=body.kind,
        content=body.content,
        tags=_json.dumps(body.tags, ensure_ascii=False),
        project_id=body.project_id,
        status=status_value,
        created_by=identity.username,
    )
    if status_value == "active":
        await repo.insert_event(
            db,
            kind="skill_activated",
            actor=identity.username,
            payload={"name": body.name, "version": version},
        )
    await db.commit()
    await db_module.log_activity(
        db,
        "skill_version_created",
        f"Skill {body.name} v{version} created as {status_value} "
        f"by {identity.username}",
    )
    row = await repo.get_skill_version(db, body.name, version)
    return SkillView(**dict(row))


@app.patch("/api/skills/{name}/versions/{version}/activate", response_model=SkillView)
async def api_activate_skill(
    name: str,
    version: int,
    request: Request,
    _identity=Depends(require_human_or_admin),
):
    """Activate a proposed skill version (human gate, #380)."""
    db = _db(request)
    row = await repo.get_skill_version(db, name, version)
    if row is None:
        raise HTTPException(404, "skill version not found")
    if row["status"] != "active":
        await repo.activate_skill_version(db, name, version)
        await repo.insert_event(
            db,
            kind="skill_activated",
            actor=_identity.username,
            payload={"name": name, "version": version},
        )
        await db.commit()
        await db_module.log_activity(
            db,
            "skill_activated",
            f"Skill {name} v{version} activated by {_identity.username}",
        )
    row = await repo.get_skill_version(db, name, version)
    return SkillView(**dict(row))


@app.post("/api/projects/{project_id}/provision")
async def api_provision_project(
    project_id: int,
    request: Request,
    _identity=Depends(require_human_or_admin),
):
    """Clone/verify the project workspace on demand (#347). Human gate:
    provisioning touches the server filesystem and git credentials."""
    db = _db(request)
    if await repo.get_project(db, project_id) is None:
        raise HTTPException(404, "project not found")
    result = await services.provision_project(db, project_id, actor=_identity.username)
    row = await repo.get_project(db, project_id)
    return {**result, "project": ProjectView(**dict(row))}


@app.get("/api/metrics/practices")
async def api_practice_metrics(
    request: Request,
    since_days: int = Query(default=90, ge=1, le=3650),
):
    """Practice metrics (#384): review economics, harness versions,
    recurring finding categories, cycle times."""
    return await services.practice_metrics(_db(request), since_days=since_days)


@app.post("/api/telemetry/deprecated-tool")
async def api_deprecated_tool_call(request: Request, body: dict[str, Any]):
    """Stage-1 deprecation telemetry (#325, ADR-0002): count alias calls."""
    tool = str(body.get("tool", ""))[:100]
    replacement = str(body.get("replacement", ""))[:100]
    agent = str(body.get("agent", ""))[:100]
    await db_module.log_activity(
        _db(request),
        "deprecated_tool_call",
        f"{tool} called{f' by {agent}' if agent else ''}; use {replacement}",
    )
    return {"ok": True}


@app.get("/api/integrations/notes")
async def api_notes_availability():
    """Diagnose the notesforllm link (#251): available | no_binary | no_space | error."""
    return await plugins.notes.availability()


@app.get("/api/tasks", response_model=None)
async def api_list_tasks(
    request: Request,
    status: str | None = None,
    task_type: str | None = Query(default=None, alias="type"),
    priority: str | None = None,
    parent_id: int | None = None,
    human_owner: str | None = None,
    human_reviewer: str | None = None,
    claimed_by: str | None = None,
    mine: str | None = Query(default=None, description="Filter owner OR claim holder"),
    limit: int = Query(default=50, le=200),
    include_archived: bool = Query(default=False, alias="include_archived"),
    after_id: int | None = Query(
        default=None,
        ge=0,
        description="Cursor (#254): 0 starts a paged walk, then pass next_cursor",
    ),
    mode: str = Query(default="full", pattern="^(full|summary)$"),
    project: str | None = Query(
        default=None,
        description="Project slug filter (#336): subtree of the project's epics",
    ),
):
    """List tasks. Plain list without after_id/mode=summary (backward
    compatible); paged/summary calls return {tasks, next_cursor} (#254).
    ``project`` narrows to tasks whose root epic belongs to the project (#336)."""
    return await services.list_tasks(
        _db(request),
        status=status,
        task_type=task_type,
        priority=priority,
        parent_id=parent_id,
        human_owner=human_owner,
        human_reviewer=human_reviewer,
        claimed_by=claimed_by,
        mine=mine,
        limit=limit,
        include_archived=include_archived,
        after_id=after_id,
        mode=mode,
        project=project,
    )


@app.get("/api/tasks/{task_id}", response_model=TaskView)
async def api_get_task(task_id: int, request: Request):
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    updates = await repo.get_task_updates(db, task_id)
    task_view = services.row_to_task(row, updates=updates)
    return await services.enrich_task_view(db, task_view)


@app.post("/api/tasks/{task_id}/archive", response_model=TaskView)
async def api_archive_task(
    task_id: int,
    request: Request,
    body: TaskArchive | None = None,
    _identity=Depends(require_permission("tasks.archive")),
):
    cascade = body.cascade if body else True
    try:
        return await services.archive_task(_db(request), task_id, cascade=cascade)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc


@app.post("/api/tasks/{task_id}/withdraw", response_model=TaskView)
async def api_withdraw_own_draft(
    task_id: int,
    request: Request,
    identity=Depends(require_agent_caller),
):
    """Agent-only: archive own agent draft without children (narrow withdraw)."""
    try:
        return await services.withdraw_own_draft(
            _db(request),
            task_id,
            caller=identity.username,
            caller_principal_id=identity.principal_id,
        )
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc


@app.post("/api/tasks/{task_id}/unarchive", response_model=TaskView)
async def api_unarchive_task(
    task_id: int,
    request: Request,
    body: TaskUnarchive | None = None,
    _identity=Depends(require_permission("tasks.archive")),
):
    cascade = body.cascade if body else True
    try:
        return await services.unarchive_task(_db(request), task_id, cascade=cascade)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc


@app.delete(
    "/api/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def api_delete_task(
    task_id: int,
    request: Request,
    _identity=Depends(require_permission("tasks.delete")),
):
    try:
        await services.delete_task_tree(_db(request), task_id)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Hierarchy endpoints ---


@app.get("/api/tasks/{task_id}/tree", response_model=TaskTreeNode)
async def api_task_tree(
    task_id: int,
    request: Request,
    response: Response,
    depth: int | None = Query(default=None, ge=0),
    max_nodes: int | None = Query(default=None, ge=1),
    mode: str = Query(default="full", pattern="^(full|summary)$"),
):
    """Get recursive tree of a task and all descendants.

    Without limit parameters the full tree is returned (backward compatible).
    Use ``mode=summary`` or explicit ``depth`` / ``max_nodes`` to cap output size.
    """
    db = _db(request)
    tree = await db_module.build_tree(db, task_id)
    if not tree:
        raise HTTPException(404, "task not found")
    options = TreeOutputOptions(depth=depth, max_nodes=max_nodes, mode=mode)  # type: ignore[arg-type]
    limited, truncated = apply_tree_limits(tree, options)
    if truncated:
        response.headers["X-Hub-Truncated"] = "true"
    return limited


@app.get("/api/tasks/{task_id}/context", response_model=TaskContextView)
async def api_task_context(
    task_id: int,
    request: Request,
    response: Response,
    max_chars: int | None = Query(default=None, ge=1),
    mode: str = Query(default="full", pattern="^(full|summary)$"),
):
    """Full developer contract for a task (#41).

    Returns a single envelope covering:
    - legacy navigation: breadcrumb, siblings, children, progress
    - the current task as a fully-hydrated TaskView (structured fields + ACs)
    - a compact readiness summary (score, dor_passed, blocking recommendations)
    - parent_goal: the nearest epic/feature in the hierarchy, so the work is
      grounded in a larger business goal even for deeply-nested subtasks
    - context_text: an LLM-friendly markdown digest of the same data
    """
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)

    breadcrumb = await db_module.get_breadcrumb(db, task_id)
    children = await db_module.get_children(db, task_id)
    progress = await db_module.get_progress(db, task_id) if children else None

    siblings: list[dict[str, Any]] = []
    if task.get("parent_id"):
        sib_rows = await repo.get_siblings(db, task["parent_id"], task_id)
        siblings = [dict(r) for r in sib_rows]

    # --- Hydrate the current task with ACs (structured fields already
    # come out of row_to_task after #39).
    task_view = services.row_to_task(row)
    ac_rows = await repo.list_acceptance_criteria(db, task_id)
    task_view.acceptance_criteria = [services.row_to_ac(r) for r in ac_rows]
    project_row = await repo.resolve_project_for_task(db, task_id)
    if project_row is not None:
        task_view.project = TaskProjectRef(
            id=project_row["id"], slug=project_row["slug"]
        )

    # --- Readiness summary. Reuse the same calculator as /readiness so
    # /context and /readiness can never drift.
    readiness_full = await services.get_readiness(db, task_id, explain=False)
    # Required-only list comes straight from ReadinessReport (review I1);
    # do NOT recompute it from dor_checks — the latter contains every
    # check, including those optional for the current work_type.
    missing_required = readiness_full.missing_required
    blocking_recommendations = [
        r for r in readiness_full.recommendations if r.severity == "blocking"
    ][:5]
    readiness_summary = {
        "score": readiness_full.score,
        "dor_passed": readiness_full.dor_passed,
        "missing_required": missing_required,
        "blocking_recommendations": [r.model_dump() for r in blocking_recommendations],
    }

    # --- Parent goal: nearest ancestor of type epic/feature. The
    # breadcrumb already includes the current task as its last element;
    # iterate in reverse, skip self, and stop at the first match.
    parent_goal: dict[str, Any] | None = None
    for node in reversed(breadcrumb[:-1]):  # exclude current task
        if node.get("task_type") in ("epic", "feature"):
            goal_row = await repo.get_task(db, node["id"])
            if goal_row is not None:
                goal_task = dict(goal_row)
                parent_goal = {
                    "id": goal_task["id"],
                    "task_type": goal_task.get("task_type", "task"),
                    "title": goal_task.get("title", ""),
                    "problem_statement": goal_task.get("problem_statement") or "",
                    "business_value": goal_task.get("business_value") or "",
                }
            break

    # --- Human/LLM digest. Structure matters: keep stable section headers
    # so downstream prompts can grep/extract predictably.
    breadcrumb_str = " > ".join(
        f"{c['task_type'].capitalize()}: {c['title']} (#{c['id']})" for c in breadcrumb
    )
    lines = ["## Current Work Context", f"Path: {breadcrumb_str}"]
    lines.append(
        f"Type: {task.get('task_type', 'task')} | Status: {task['status']} "
        f"| Priority: {task.get('priority', 'medium')}"
    )
    if progress:
        lines.append(
            f"Progress: {progress['completed']}/{progress['total']} "
            f"completed ({progress['percent']}%)"
        )
    if siblings:
        sib_strs = [f"{s['title']} (#{s['id']}/{s['status']})" for s in siblings[:5]]
        lines.append(f"Siblings: {', '.join(sib_strs)}")
    if children:
        child_strs = [f"{c['title']} (#{c['id']}/{c['status']})" for c in children[:8]]
        lines.append(f"Children: {', '.join(child_strs)}")
    if parent_goal:
        lines.append(
            f"Parent goal: {parent_goal['task_type'].capitalize()} "
            f"#{parent_goal['id']} — {parent_goal['title']}"
        )
        if parent_goal["problem_statement"]:
            lines.append(f"  Problem: {parent_goal['problem_statement']}")
        if parent_goal["business_value"]:
            lines.append(f"  Value: {parent_goal['business_value']}")
    if task_view.user_story:
        lines.append(f"User story: {task_view.user_story}")
    if task_view.problem_statement:
        lines.append(f"Problem: {task_view.problem_statement}")
    if task_view.business_value:
        lines.append(f"Value: {task_view.business_value}")
    if task_view.outcome_metric:
        # Discovery (#331): the number that should move, and when we check.
        outcome = f"Outcome: {task_view.outcome_metric}"
        if task_view.outcome_indicator:
            outcome += f" (leading: {task_view.outcome_indicator})"
        if task_view.outcome_deadline:
            outcome += f" by {task_view.outcome_deadline}"
        lines.append(outcome)
    if task_view.outcome_revisit_condition:
        lines.append(f"Revisit if: {task_view.outcome_revisit_condition}")
    if task_view.redesign_decision:
        decision = f"Redesign decision: {task_view.redesign_decision.value}"
        if task_view.redesign_rationale:
            decision += f" — {task_view.redesign_rationale}"
        lines.append(decision)
    if task_view.agent_fit:
        lines.append(f"Agent fit: {task_view.agent_fit.value}")
    if task_view.scope_in:
        lines.append(f"In-scope: {', '.join(task_view.scope_in)}")
    if task_view.scope_out:
        lines.append(f"Out-of-scope: {', '.join(task_view.scope_out)}")
    if task_view.validation_commands:
        lines.append("Validation: " + " && ".join(task_view.validation_commands))
    if task_view.acceptance_criteria:
        ac_ids = ", ".join(ac.id for ac in task_view.acceptance_criteria)
        lines.append(
            f"Acceptance criteria ({len(task_view.acceptance_criteria)}): {ac_ids}"
        )
    if task_view.risks:
        # Use .value so the digest reads as 'security:high', not
        # 'RiskKind.security:RiskSeverity.high' (review I4).
        risk_brief = ", ".join(
            f"{r.kind.value}:{r.severity.value}" for r in task_view.risks[:5]
        )
        lines.append(f"Risks ({len(task_view.risks)}): {risk_brief}")
    if task_view.latest_review:
        lr = task_view.latest_review
        freshness = "current" if lr.is_current else "stale — work resubmitted"
        solo = " [SELF-APPROVED: solo mode]" if lr.self_approved else ""
        lines.append(
            f"Latest review: {lr.verdict.value.upper()} "
            f"for submission #{lr.submission_generation} ({freshness}){solo}"
        )
        for finding in lr.findings[:10]:
            scope_mark = ""
            if finding.scope == FindingScope.out_of_scope:
                scope_mark = (
                    f" [out-of-scope → #{finding.linked_task_id}]"
                    if finding.linked_task_id
                    else " [out-of-scope]"
                )
            lines.append(
                f"  {finding.id}. [{finding.severity.value}]{scope_mark} "
                f"{finding.message}"
            )
    lines.append(
        f"Readiness: score={readiness_summary['score']} "
        f"dor_passed={'yes' if readiness_summary['dor_passed'] else 'no'}"
    )
    if missing_required:
        lines.append(f"  Missing required: {', '.join(missing_required)}")
    if mode == "full":
        lines.append("")
        lines.extend(lifecycle_map_lines())

    effective_max_chars = max_chars
    if mode == "summary" and effective_max_chars is None:
        effective_max_chars = 4000

    context_text, char_truncated = truncate_text("\n".join(lines), effective_max_chars)
    if char_truncated:
        response.headers["X-Hub-Truncated"] = "true"

    return {
        "task_id": task_id,
        "breadcrumb": breadcrumb,
        "siblings": siblings,
        "children": children,
        "progress": progress,
        "context_text": context_text,
        "task": task_view,
        "readiness": readiness_summary,
        "parent_goal": parent_goal,
    }


@app.post("/api/tasks/{task_id}/submit-review", response_model=TaskView)
async def api_submit_for_review(
    task_id: int,
    request: Request,
    body: TaskSubmitReview | None = None,
):
    """Submit the current work of a pair task for client-driven review (#307).

    Canonical REST operation behind hub_submit_for_review and the
    ``oc-hub submit-review`` CLI: running pair task → status=review with a
    bumped submission generation.
    """
    return await services.submit_for_review(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/machine-review", response_model=MachineReviewView)
async def api_submit_machine_review(
    task_id: int,
    body: MachineReviewSubmit,
    request: Request,
    identity=Depends(current_identity),
):
    """Accept a structured multi-agent review report (#381).

    Bound to the task's CURRENT submission generation — resubmitting work
    makes the report stale, exactly like human verdicts (#305). Informs
    the human verdict; never replaces it.
    """
    import json as _json

    db = _db(request)
    row = await repo.get_task(db, task_id)
    if row is None:
        raise HTTPException(404, "task not found")
    task = dict(row)
    generation = task.get("submission_generation") or 0
    if generation == 0:
        raise HTTPException(
            400,
            "no submission to review: submit_for_review must run at least once",
        )
    # raw_count is self-reported and was stored unchecked, so reports arrived
    # claiming fewer raw findings than the findings they themselves listed —
    # on production one had raw_count=0 alongside two confirmed findings
    # (#519). Normalised upward rather than rejected: the recorded risk asks
    # not to break existing clients, and a report with a miscounted header is
    # still worth keeping — its findings are real.
    adjudicated = len(body.findings_confirmed) + len(body.findings_rejected)
    raw_count = body.raw_count
    if raw_count < adjudicated:
        log.warning(
            "machine review for task #%s: raw_count=%s is below the %s findings "
            "it lists; normalised upward",
            task_id,
            raw_count,
            adjudicated,
        )
        raw_count = adjudicated
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill=body.harness_skill,
        harness_version=body.harness_version,
        agent_count=body.agent_count,
        tokens_spent=body.tokens_spent,
        duration_ms=body.duration_ms,
        orchestrator=body.orchestrator,
        model=body.model,
        raw_count=raw_count,
        findings_confirmed=_json.dumps(
            [f.model_dump(exclude_none=True) for f in body.findings_confirmed],
            ensure_ascii=False,
        ),
        findings_rejected=_json.dumps(
            [f.model_dump(exclude_none=True) for f in body.findings_rejected],
            ensure_ascii=False,
        ),
        submitted_by=(body.agent or identity.username)[:100],
        incomplete=body.incomplete,
        unresolved=_json.dumps(
            [f.model_dump(exclude_none=True) for f in body.unresolved],
            ensure_ascii=False,
        ),
        lost_dimensions=_json.dumps(body.lost_dimensions, ensure_ascii=False),
    )
    await repo.insert_event(
        db,
        kind="machine_review_completed",
        task_id=task_id,
        actor=(body.agent or identity.username)[:100],
        payload={
            "confirmed": len(body.findings_confirmed),
            "rejected": len(body.findings_rejected),
            "raw": raw_count,
            "generation": generation,
        },
    )
    # #750: a report that surfaced NO candidates, ran ONE agent and counted
    # NO tokens is the shape of a harness that never actually ran — 60 such
    # reports landed in 36 minutes on 2026-08-19 (cursor_cloud), silently
    # gutting the filtration metrics and, later, the auto-verdict (#745
    # already refuses raw_count=0). Warned once per generation, never
    # refused: the report itself is still worth keeping as evidence.
    if raw_count == 0:
        prior_zero = await db.execute_fetchall(
            "SELECT COUNT(*) AS n FROM machine_reviews "
            "WHERE task_id=? AND submission_generation=? AND raw_count=0",
            (task_id, generation),
        )
        if int(prior_zero[0]["n"]) == 1:
            single_agent = (body.agent_count or 0) <= 1
            no_tokens = body.tokens_spent is None
            detail = (
                "похоже, харнесс не запускался (agent_count≤1, токены не посчитаны)"
                if single_agent and no_tokens
                else "проверьте, что фазы измерений и адъюдикации исполнялись"
            )
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "alert",
                (
                    "Machine-review с raw_count=0: ноль кандидатов — это "
                    f"отсутствие данных, а не отсутствие находок; {detail}. "
                    "Отчёт принят, но автовердикт по нему невозможен, а "
                    "«чисто» не подтверждено (#750)."
                ),
            )
    await db.commit()
    await db_module.log_activity(
        db,
        "machine_review_completed",
        f"Task #{task_id}: machine review — {raw_count} raw → "
        f"{len(body.findings_confirmed)} confirmed, "
        f"{len(body.findings_rejected)} rejected",
    )
    saved = await repo.get_latest_machine_review(db, task_id)
    view = MachineReviewView(**dict(saved))
    view.is_current = view.submission_generation == generation

    # Auto-verdict (#745): a clean report in a project whose policy allows
    # it gets its APPROVED right here. Best-effort by contract — the report
    # intake must never fail because the autopilot stumbled.
    try:
        from hub.services.auto_verdict import maybe_auto_verdict

        await maybe_auto_verdict(db, task_id)
    except Exception:  # noqa: BLE001 - degradation is the contract
        log.exception("auto-verdict failed for task #%s", task_id)

    return view


@app.post("/api/tasks/{task_id}/review-verdict", response_model=TaskView)
async def api_review_verdict(
    task_id: int,
    request: Request,
    body: TaskReviewVerdict,
    identity=Depends(current_identity),
):
    """Record a review verdict for the current submission (#307).

    Separation of duties (#318): the agent principal that implemented the
    task (assigned_agent or claimed_by) may not review it. The check uses
    the AUTHENTICATED identity — the ``agent`` field in the body is
    display-only. Human/admin tokens always pass;
    ``OPENCLAW_REVIEW_SELF_APPROVE=allow`` is the solo-mode opt-out.

    Canonical REST operation behind hub_submit_review and the
    ``oc-hub review-verdict`` CLI. Client-driven review returns the task to
    ``running``; this endpoint never completes a task.

    Finding scope (#435): each finding carries ``scope``
    (in_scope|out_of_scope, default in_scope) and an optional
    ``linked_task_id`` referencing the follow-up task for out-of-scope
    findings. ``changes_requested`` with findings requires at least one
    in_scope finding (422 otherwise); out-of-scope findings without a
    linked task produce a non-blocking warning in the review update.

    Auto-drafts (#436): ``create_tasks_for_out_of_scope=true`` creates a
    DRAFT follow-up task for each unlinked out-of-scope finding and stamps
    its id into the stored finding; idempotent on resubmit.
    """
    db = _db(request)
    row = await repo.get_task(db, task_id)
    self_approved = False
    if row is not None:
        self_approved = services.ensure_reviewer_independence(
            dict(row),
            is_agent=identity.is_agent,
            principal_id=identity.principal_id,
            username=identity.username,
        )
    return await services.record_review_verdict(
        db,
        task_id,
        body,
        self_approved=self_approved,
        principal_id=identity.principal_id,
    )


@app.post("/api/tasks/{task_id}/run-ac-tests")
async def api_run_ac_tests(task_id: int, request: Request):
    """Run the tests bound to a task's verifiable_by=test AC and record them (#507).

    Best-effort: an unavailable workspace records ``not_found`` rather than a
    false ``fail``. Results are stamped with the current submission_generation.
    """
    db = _db(request)
    if not await repo.get_task(db, task_id):
        raise HTTPException(404, "task not found")
    return {"results": await run_ac_tests(db, task_id)}


@app.post("/api/tasks/{task_id}/run-validation")
async def api_run_validation(task_id: int, request: Request):
    """Run a task's declared validation_commands and record the result (#509)."""
    db = _db(request)
    if not await repo.get_task(db, task_id):
        raise HTTPException(404, "task not found")
    return await run_validation_commands(db, task_id)


@app.post("/api/tasks/{task_id}/ci-run-report", response_model=CIRunReportResult)
async def api_ci_run_report(
    task_id: int,
    body: CIRunReportSubmit,
    request: Request,
    identity=Depends(require_permission("tasks.ci_report")),
):
    """Accept the run evidence CI produced for one commit (#546).

    Execution lives in CI; the hub checks and keeps the result. The report counts
    only for the commit the hub pinned at submission (#572) — a report for any
    other commit is stored as evidence for that commit and explicitly not
    applied, so it can never open a gate for code nobody ran. A stale generation
    stated by the reporter is refused outright.

    The permission is deliberately narrow (``tasks.ci_report``, held only by the
    ci_runner role): this token lives in a CI secret and must not be able to move
    a task or write a verdict.
    """
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if row is None:
        raise HTTPException(404, "task not found")
    current_generation = dict(row).get("submission_generation") or 0
    if (
        body.submission_generation is not None
        and body.submission_generation != current_generation
    ):
        raise HTTPException(
            409,
            f"stale report: sent for submission #{body.submission_generation}, "
            f"current is #{current_generation}",
        )
    try:
        result = await accept_ci_run_report(
            db,
            task_id,
            head_sha=body.head_sha,
            ac_results=body.ac_results,
            validation_status=body.validation_status,
            validation_log=body.validation_log,
            reason=body.reason,
            reported_by=body.reported_by or identity.username,
        )
    except LookupError:
        raise HTTPException(404, "task not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return CIRunReportResult(**result)


async def _build_call_sites_section(db, task_id: int, task_view) -> CallSiteSection:
    """The call-site enumeration for a task's branch (#601).

    Best effort by contract: every failure answers ``unknown`` with a reason
    rather than an empty section. An empty section would say "no other call
    sites exist", which is exactly the false reassurance this was written to
    remove.
    """
    branch = (task_view.branch or "").strip()
    if not branch:
        return CallSiteSection(status=call_sites.UNKNOWN, reason="task has no branch")

    try:
        ctx = await services.project_git_context(db, task_id)
        workspace = ctx.get("repo")
        base = ctx.get("base_branch") or config.PAIR_BASE_BRANCH
        if not workspace:
            return CallSiteSection(
                status=call_sites.UNKNOWN, reason="project has no workspace"
            )
        diff = await plugins.git_ops.branch_diff(workspace, base, branch)
        if diff is None:
            return CallSiteSection(
                status=call_sites.UNKNOWN,
                reason=f"could not read the diff of {branch} against {base}",
            )
        report = await asyncio.to_thread(call_sites.analyse, workspace, diff)
    except Exception as exc:  # noqa: BLE001 - advisory section, never fatal
        log.warning("call-site section failed for task #%s: %s", task_id, exc)
        return CallSiteSection(status=call_sites.UNKNOWN, reason=f"failed: {exc}")

    return CallSiteSection(
        status=report.status,
        reason=report.reason,
        summary=report.summary(),
        note=report.note,
        unparsed=report.unparsed,
        entries=[
            CallSiteEntry(
                symbol=s.symbol,
                defined_in=s.defined_in,
                state=s.state,
                statement=s.statement(),
                total_sites=len(s.sites),
                untouched=[
                    f"{site.file}:{site.line} ({site.caller})"
                    for site in s.sites
                    if not site.touched
                ],
            )
            for s in report.symbols
        ],
    )


@app.get("/api/tasks/{task_id}/review-brief", response_model=ReviewBrief)
async def api_review_brief(
    task_id: int,
    request: Request,
    identity=Depends(current_identity),
):
    """Everything a reviewer agent needs in one response (#308).

    Bundles acceptance criteria, scope, validation commands, review
    checklist, branch/PR metadata with an advisory diff command, and the
    latest submission context — so review never depends on scraping task
    prose. Works without a GitHub PR: ``pr_number`` is optional metadata.

    Fail-fast self-review check (#433): when the caller is the agent that
    implemented the task, the brief carries a ``self_review_warning`` so
    the reviewer stops BEFORE spending review effort — hub_submit_review
    (the source of truth) would reject the verdict anyway. Not a hard-fail:
    the implementer may still read the brief for self-checking.
    """
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    self_review_warning = services.self_review_brief_warning(
        dict(row),
        is_agent=identity.is_agent,
        principal_id=identity.principal_id,
        username=identity.username,
    )
    task_view = services.row_to_task(row)
    project_row = await repo.resolve_project_for_task(db, task_id)
    if project_row is not None:
        task_view.project = TaskProjectRef(
            id=project_row["id"], slug=project_row["slug"]
        )
    ac_rows = await repo.list_acceptance_criteria(db, task_id)

    # Latest submission context: the most recent done report, falling back
    # to the most recent status update when the task has not reported yet.
    latest_submission_summary = ""
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    for kind in ("done", "status"):
        for u in reversed(updates):
            if u.get("kind") == kind:
                latest_submission_summary = u.get("content", "")
                break
        if latest_submission_summary:
            break

    diff_command = ""
    if task_view.branch:
        diff_command = f"git diff develop...{task_view.branch}"

    machine_review = None
    mr_row = await repo.get_latest_machine_review(db, task_id)
    if mr_row is not None:
        machine_review = MachineReviewView(**dict(mr_row))
        machine_review.is_current = machine_review.submission_generation == (
            task_view.submission_generation or 0
        )

    # Advisory branch-stacking check (#438): the reviewer should know when
    # the diff includes another task's unmerged work. Best-effort — no repo
    # access means no warning, never an error.
    stacking_warning = ""
    if task_view.branch:
        stacking = await services.detect_branch_stacking(db, task_id, task_view.branch)
        if stacking:
            stacking_warning = stacking["message"]

    # #506: resolve each verifiable_by=test AC's locator to a real test via
    # pytest collect-only (best-effort). Only pays the collection cost when the
    # brief actually has test-AC to check.
    ac_models = [services.row_to_ac(r) for r in ac_rows]
    locator_resolution: list[ACLocatorResolution] = []
    if any(a.verifiable_by.value == "test" for a in ac_models):
        ctx = await services.project_git_context(db, task_id)
        workspace = ctx.get("repo")
        # #506: the workspace is shared across a project's tasks and the pair
        # flow switches its branch. Collecting while HEAD sits on another task's
        # branch would report THIS task's tests as missing. Only trust the
        # collection when HEAD matches the task's branch; otherwise leave it
        # unavailable so the status is `unknown`, never a false `missing`.
        collected = None
        if task_view.branch:
            head = await plugins.git_ops.current_branch(repo=workspace)
            if head == task_view.branch:
                # collect_test_nodeids itself returns None without a workspace,
                # so an unresolvable repo still degrades to `unknown`.
                collected = await collect_test_nodeids(workspace)
        locator_resolution = [
            ACLocatorResolution(**r) for r in resolve_ac_locators(ac_models, collected)
        ]

    # #572: does the branch still stand where the submission pinned it? Three
    # states, never collapsed — the reviewer must see "could not look" as
    # itself, not as "nothing moved". Costs one fetch, and only when there is
    # a pinned submission to compare against.
    submission_sha = (task_view.submission_sha or "").strip()
    current_tip = ""
    sha_check = "unknown"
    sha_check_reason = "branch tip was not pinned at submission"
    if submission_sha and task_view.branch:
        current_tip, tip_reason = await services.resolve_branch_tip(
            db, task_id, task_view.branch
        )
        if not current_tip:
            sha_check_reason = tip_reason
        elif current_tip == submission_sha:
            sha_check = "match"
            sha_check_reason = ""
        else:
            sha_check = "diverged"
            sha_check_reason = (
                f"submitted at {submission_sha[:12]}, branch now at "
                f"{current_tip[:12]} — the diff under review is not the code "
                "in the branch"
            )

    # #601: where else is each changed symbol called, and does this diff touch
    # those places. Same shape as #506 above and for the same reason: the
    # analysis needs the checkout, so it runs against the project workspace and
    # answers `unknown` with a reason when that is not available. Silence here
    # would read as "no other call sites", which is the very mistake the
    # section exists to catch.
    call_sites_section = await _build_call_sites_section(db, task_id, task_view)

    # #507: recorded pass/fail of each test-AC for the current generation.
    ac_result_rows = await repo.list_ac_test_results(db, task_id)
    ac_test_results = [
        ACTestResultView(**r)
        for r in current_ac_test_results(
            ac_result_rows, task_view.submission_generation or 0
        )
    ]

    # #546: is there run evidence for the COMMIT under review? Two states only,
    # and the unknown one always carries its cause — a reviewer must be able to
    # tell "nobody ran it" from "it ran and failed".
    ci_state, ci_reason = await ci_report_state(
        db,
        {
            "id": task_id,
            "submission_sha": task_view.submission_sha,
        },
    )
    ci_run_report = CIRunReportState(
        state=ci_state,
        reason=ci_reason,
        head_sha=task_view.submission_sha or "",
    )

    # #615: the statement the reviewer is judging may predate the work that
    # invalidated it. Same computation as pair-start, one source.
    from hub.services.statement_freshness import statement_freshness as _freshness

    freshness = await _freshness(db, dict(await repo.get_task(db, task_id)))

    return ReviewBrief(
        task_id=task_view.id,
        title=task_view.title,
        status=task_view.status,
        description=task_view.description,
        project=task_view.project,
        acceptance_criteria=ac_models,
        locator_resolution=locator_resolution,
        ac_test_results=ac_test_results,
        ci_run_report=ci_run_report,
        statement_freshness=freshness,
        scope_in=task_view.scope_in,
        scope_out=task_view.scope_out,
        out_of_scope_for_review=task_view.out_of_scope_for_review,
        review_checklist=task_view.review_checklist,
        validation_commands=task_view.validation_commands,
        constraints=task_view.constraints,
        technical_hints=task_view.technical_hints,
        outcome_metric=task_view.outcome_metric,
        outcome_indicator=task_view.outcome_indicator,
        outcome_deadline=task_view.outcome_deadline,
        outcome_revisit_condition=task_view.outcome_revisit_condition,
        redesign_decision=task_view.redesign_decision,
        redesign_rationale=task_view.redesign_rationale,
        agent_fit=task_view.agent_fit,
        branch=task_view.branch,
        pr_number=task_view.pr_number,
        diff_command=diff_command,
        submission_sha=submission_sha,
        current_branch_tip=current_tip,
        sha_check=sha_check,
        sha_check_reason=sha_check_reason,
        call_sites=call_sites_section,
        review_cycle=task_view.review_cycle,
        submission_generation=task_view.submission_generation,
        latest_submission_summary=latest_submission_summary,
        latest_review=task_view.latest_review,
        machine_review=machine_review,
        self_review_warning=self_review_warning,
        stacking_warning=stacking_warning,
    )


@app.patch("/api/tasks/{task_id}/reorder", response_model=TaskView)
async def api_reorder_task(task_id: int, body: TaskReorder, request: Request):
    return await services.reorder_task(_db(request), task_id, body)


# --- Approve / Reject / Start ---


@app.post("/api/tasks/batch-approve", response_model=BatchApproveResult)
async def api_batch_approve(
    body: BatchApprove,
    request: Request,
    _identity=Depends(require_human_or_admin),
):
    """Approve many drafts in one human operation with per-task guards (#252).

    Human-only like single approve; ``force`` is intentionally unsupported —
    overrides remain individual, audited actions.
    """
    return await services.batch_approve_tasks(_db(request), body)


@app.post("/api/tasks/{task_id}/approve", response_model=TaskView)
async def api_approve_task(
    task_id: int,
    request: Request,
    body: TaskApprove | None = None,
    _identity=Depends(require_human_or_admin),
):
    return await services.approve_task(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/reject", response_model=TaskView)
async def api_reject_task(
    task_id: int,
    request: Request,
    body: TaskReject | None = None,
    _identity=Depends(require_human_or_admin),
):
    return await services.reject_task(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/start", response_model=TaskView)
async def api_start_task(
    task_id: int,
    request: Request,
    body: TaskStart | None = None,
    _identity=Depends(require_human_or_admin),
):
    return await services.start_task(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/pair-start", response_model=TaskView)
async def api_pair_start_task(
    task_id: int,
    request: Request,
    body: TaskPairStart | None = None,
    identity=Depends(current_identity),
):
    """Start pair mode: running without headless dispatch (human or agent)."""
    return await services.pair_start_task(
        _db(request),
        task_id,
        body,
        caller=identity.username,
        implementer_principal_id=(identity.principal_id if identity.is_agent else None),
    )


@app.post("/api/tasks/{task_id}/claim", response_model=TaskView)
async def api_claim_task(
    task_id: int,
    body: TaskClaim,
    request: Request,
    identity=Depends(current_identity),
):
    """Claim an open task for one Cursor agent/session."""
    if not body.agent.strip():
        body = TaskClaim(agent=identity.username, session_id=body.session_id)
    return await services.claim_task(
        _db(request),
        task_id,
        body,
        implementer_principal_id=(identity.principal_id if identity.is_agent else None),
    )


@app.post("/api/tasks/{task_id}/release", response_model=TaskView)
async def api_release_task(
    task_id: int,
    body: TaskRelease,
    request: Request,
    identity=Depends(current_identity),
):
    """Release a claimed task back to open."""
    if not body.agent.strip():
        body = TaskRelease(agent=identity.username, session_id=body.session_id)
    return await services.release_task(_db(request), task_id, body)


# --- Q&A: Question / Answer ---


@app.post("/api/tasks/{task_id}/question", response_model=TaskView)
async def api_task_question(task_id: int, body: TaskQuestion, request: Request):
    return await services.ask_question(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/answer", response_model=TaskView)
async def api_task_answer(
    task_id: int,
    body: TaskAnswer,
    request: Request,
    _identity=Depends(require_human_or_admin),
):
    return await services.answer_question(_db(request), task_id, body)


# --- Decide (after arbiter) ---


@app.post("/api/tasks/{task_id}/decide", response_model=TaskView)
async def api_decide_task(
    task_id: int,
    body: TaskDecide,
    request: Request,
    _identity=Depends(require_human_or_admin),
):
    return await services.decide_task(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/force-complete", response_model=TaskView)
async def api_force_complete_task(
    task_id: int,
    request: Request,
    body: TaskForceComplete | None = None,
    _identity=Depends(require_human_or_admin),
):
    """Force-complete a non-terminal task/subtask (human-only audited override).

    Allowed when no active dispatch job backs job_id or review_job_id; 409 if
    active. Missing/terminal jobs are audited. Comment required for active
    lifecycle states except pending_report/claimed.
    """
    return await services.force_complete_task(_db(request), task_id, body)


# --- Task Updates ---


@app.post("/api/tasks/{task_id}/updates", response_model=TaskUpdateView)
async def api_add_task_update(task_id: int, body: TaskUpdateCreate, request: Request):
    # Authorship is taken from the authenticated identity, not from the body
    # (#559) — the same rule the review verdict already follows.
    return await services.add_update(
        _db(request),
        task_id,
        body,
        principal_id=current_identity(request).principal_id,
    )


@app.get("/api/tasks/{task_id}/updates", response_model=list[TaskUpdateView])
async def api_list_task_updates(task_id: int, request: Request):
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    updates = await repo.get_task_updates(db, task_id)
    return [TaskUpdateView(**dict(u)) for u in updates]


@app.post("/api/tasks/{task_id}/refresh", response_model=TaskView)
async def api_refresh_task(task_id: int, request: Request):
    return await services.refresh_task(_db(request), task_id)


# ---------------------------------------------------------------------------
# REST API — Structured form, refinement, readiness (Epic #32)
# ---------------------------------------------------------------------------


def _not_found_to_http(exc: TaskNotFoundError) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


def _duplicate_to_http(
    exc: DuplicateAcceptanceCriterionError, code: int
) -> HTTPException:
    return HTTPException(code, str(exc))


@app.post("/api/tasks/{task_id}/refine", response_model=TaskView)
async def api_refine_task(task_id: int, body: TaskRefine, request: Request):
    """PATCH-style update of structured fields and (optionally) ACs.

    - Omitted fields are left untouched.
    - Passing ``acceptance_criteria=[]`` deliberately clears the list.
    - On duplicate ac_id within the payload returns 422.
    """
    db = _db(request)
    try:
        await services.refine_task(db, task_id, body)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    except DuplicateAcceptanceCriterionError as exc:
        raise _duplicate_to_http(exc, 422) from exc
    except ProjectBindError as exc:
        raise HTTPException(422, str(exc)) from exc

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    task_view = services.row_to_task(row, updates=updates)
    return await services.enrich_task_view(db, task_view)


@app.post("/api/tasks/refine-bulk", response_model=BulkRefineResult)
async def api_refine_tasks_bulk(body: BulkRefine, request: Request):
    """Apply a TaskRefine PATCH to many tasks in one atomic request.

    Either all items land or none do. Returns a per-task audit
    (fields set, AC/risks counts, readiness).
    """
    db = _db(request)
    try:
        return await services.refine_tasks_bulk(db, body)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    except DuplicateAcceptanceCriterionError as exc:
        raise _duplicate_to_http(exc, 422) from exc


@app.post(
    "/api/tasks/{task_id}/risks",
    response_model=TaskView,
    status_code=status.HTTP_201_CREATED,
)
async def api_add_risk(task_id: int, body: TaskRisk, request: Request):
    """Atomically append one risk without replacing the existing risk list."""
    db = _db(request)
    try:
        await services.add_risk(db, task_id, body)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    except LimitExceededError as exc:
        raise HTTPException(422, str(exc)) from exc

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    task_view = services.row_to_task(row, updates=updates)
    return await services.enrich_task_view(db, task_view)


@app.get(
    "/api/tasks/{task_id}/acceptance_criteria",
    response_model=list[AcceptanceCriterion],
)
async def api_list_acceptance_criteria(task_id: int, request: Request):
    try:
        return await services.list_acceptance_criteria(_db(request), task_id)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc


@app.post(
    "/api/tasks/{task_id}/acceptance_criteria",
    response_model=AcceptanceCriterion,
    status_code=status.HTTP_201_CREATED,
)
async def api_add_acceptance_criterion(
    task_id: int, body: AcceptanceCriterion, request: Request, response: Response
):
    """Append one AC. Idempotent by ``id``: duplicate ac_id returns existing row."""
    try:
        ac, created = await services.add_acceptance_criterion(
            _db(request), task_id, body
        )
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    except LimitExceededError as exc:
        raise HTTPException(422, str(exc)) from exc
    except DuplicateAcceptanceCriterionError as exc:
        raise _duplicate_to_http(exc, status.HTTP_409_CONFLICT) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return ac


@app.put(
    "/api/tasks/{task_id}/acceptance_criteria",
    response_model=list[AcceptanceCriterion],
)
async def api_replace_acceptance_criteria(
    task_id: int, body: list[AcceptanceCriterion], request: Request
):
    """Atomic replace of the AC list. Returns 422 on duplicate ids in payload."""
    from hub.models import MAX_ACCEPTANCE_CRITERIA

    if len(body) > MAX_ACCEPTANCE_CRITERIA:
        raise HTTPException(
            422,
            f"too many acceptance criteria: {len(body)} exceeds limit of {MAX_ACCEPTANCE_CRITERIA}",
        )
    try:
        return await services.replace_acceptance_criteria(_db(request), task_id, body)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    except DuplicateAcceptanceCriterionError as exc:
        raise _duplicate_to_http(exc, 422) from exc


@app.put(
    "/api/tasks/{task_id}/acceptance_criteria/{ac_id}",
    response_model=AcceptanceCriterion,
)
async def api_upsert_acceptance_criterion(
    task_id: int,
    ac_id: str,
    body: AcceptanceCriterion,
    request: Request,
    response: Response,
):
    """Idempotent upsert of one AC by ``ac_id``.

    Re-sending the same payload is a no-op; a changed payload overwrites the
    row instead of returning 409. The body ``id`` must match the path ``ac_id``.
    Returns 201 when a new criterion was created, 200 when one was updated.
    """
    if body.id != ac_id:
        raise HTTPException(422, f"ac id mismatch: path {ac_id!r} != body {body.id!r}")
    try:
        ac, created = await services.upsert_acceptance_criterion(
            _db(request), task_id, body
        )
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    except LimitExceededError as exc:
        raise HTTPException(422, str(exc)) from exc
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return ac


@app.delete(
    "/api/tasks/{task_id}/acceptance_criteria/{ac_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def api_delete_acceptance_criterion(task_id: int, ac_id: str, request: Request):
    try:
        removed = await services.delete_acceptance_criterion(
            _db(request), task_id, ac_id
        )
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    if not removed:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"acceptance criterion {ac_id!r} not found for task {task_id}",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/tasks/{task_id}/readiness", response_model=ReadinessReport)
async def api_task_readiness(
    task_id: int,
    request: Request,
    explain: bool = Query(default=False),
):
    try:
        return await services.get_readiness(_db(request), task_id, explain=explain)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc


@app.get(
    "/api/tasks/{task_id}/readiness-tree",
    response_model=ReadinessTreeReport,
)
async def api_task_readiness_tree(
    task_id: int,
    request: Request,
    include_root: bool = Query(default=False),
):
    """DoR rollup for a subtree: which descendants are not ready and why.

    Set ``include_root=true`` to include the queried task itself.
    """
    try:
        return await services.readiness_tree(
            _db(request), task_id, include_root=include_root
        )
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc


# ---------------------------------------------------------------------------
# Full log endpoints
# ---------------------------------------------------------------------------


@app.get("/api/tasks/{task_id}/log")
async def api_task_log(task_id: int, request: Request, job: str = Query("main")):
    """Return full dispatch log. ?job=main (default) or ?job=review."""
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    job_id = task.get("review_job_id") if job == "review" else task.get("job_id")
    if not job_id:
        raise HTTPException(404, f"no {job} job_id for this task")
    content = plugins.dispatch.job_log_full(job_id)
    if not content:
        raise HTTPException(404, f"log file not found for job {job_id}")
    return PlainTextResponse(content)


# ---------------------------------------------------------------------------
# Deprecated — Proposal endpoints (backward compatibility)
# ---------------------------------------------------------------------------


@app.post("/api/proposals", response_model=TaskView)
async def api_create_proposal_compat(request: Request):
    """Deprecated: creates a draft task instead of a proposal."""
    raw = await request.json()
    body = TaskCreate(
        title=raw.get("title", ""),
        description=raw.get("description", ""),
        source=TaskSource.agent,
        agent=raw.get("agent", ""),
        rationale=raw.get("rationale", ""),
    )
    return (await services.create_task(_db(request), body)).task


@app.get("/api/proposals", response_model=list[TaskView])
async def api_list_proposals_compat(
    request: Request,
    status: str | None = None,
    limit: int = Query(default=50, le=200),
):
    """Deprecated: lists draft/rejected tasks instead of proposals."""
    db = _db(request)
    status_map = {"pending": "draft", "approved": "open", "rejected": "rejected"}
    mapped = status_map.get(status, status) if status else None
    rows = await repo.list_agent_tasks(db, mapped, limit=limit)
    return [services.row_to_task(r) for r in rows]


@app.post("/api/proposals/{proposal_id}/action", response_model=TaskView)
async def api_proposal_action_compat(
    proposal_id: int,
    request: Request,
    _identity=Depends(require_human_or_admin),
):
    """Deprecated: approve/reject via the old proposal action format.

    Human-gated like its canonical twins (#359). Without the gate this route
    handed an agent the whole approval flow: it reaches approve_task directly
    and builds TaskApprove(run=True), so a single call approved AND dispatched.
    DoR was no obstacle — an agent may refine its own draft past it.
    """
    raw = await request.json()
    action = raw.get("action", "")
    comment = raw.get("comment", "")
    if action == "approved":
        body = TaskApprove(comment=comment, run=True)
        return await services.approve_task(_db(request), proposal_id, body)
    elif action == "rejected":
        body = TaskReject(comment=comment)
        return await services.reject_task(_db(request), proposal_id, body)
    raise HTTPException(400, f"unknown action: {action}")


# ---------------------------------------------------------------------------
# REST API — Dashboard data
# ---------------------------------------------------------------------------


@app.get("/api/dashboard", response_model=DashboardData)
async def api_dashboard(request: Request):
    return await services.get_dashboard_data(_db(request))


@app.get("/api/activity", response_model=list[ActivityItem])
async def api_activity(request: Request, limit: int = Query(default=30, le=100)):
    return await services.list_activity(_db(request), limit=limit)


@app.get("/api/dispatch/jobs", response_model=list[dict[str, Any]])
async def api_dispatch_jobs(limit: int = Query(default=30, le=100)):
    return plugins.dispatch.list_jobs(limit)


@app.get("/api/transcripts", response_model=list[dict[str, Any]])
async def api_transcripts(limit: int = Query(default=10, le=30)):
    return plugins.transcripts.list_recent_transcripts(limit)


# ---------------------------------------------------------------------------
# Vast.ai instance management
# ---------------------------------------------------------------------------


if config.VAST_ENABLED:

    @app.post("/api/vast/up")
    async def api_vast_up(_identity=Depends(require_human_or_admin)):
        return await plugins.vast.vast_up()

    @app.get("/api/vast/status")
    async def api_vast_status():
        return await plugins.vast.vast_status()

    @app.post("/api/vast/down")
    async def api_vast_down(_identity=Depends(require_human_or_admin)):
        return await plugins.vast.vast_down()


# ---------------------------------------------------------------------------
# REST API — Admin section (Stage 4)
# ---------------------------------------------------------------------------


@app.get("/api/admin/summary")
async def api_admin_summary(
    request: Request, _identity=Depends(require_permission("admin.read"))
):
    from hub.services import admin as admin_svc

    return await admin_svc.admin_summary(_db(request))


@app.get("/api/admin/principals")
async def api_admin_list_principals(
    request: Request,
    kind: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, le=500),
    _identity=Depends(require_permission("admin.read")),
):
    from hub.services import admin as admin_svc
    from hub.models import PrincipalView

    rows = await admin_svc.list_principals(
        _db(request), kind=kind, status=status, limit=limit
    )
    return [PrincipalView(**r) for r in rows]


@app.post("/api/admin/principals")
async def api_admin_create_principal(
    request: Request,
    _identity=Depends(require_permission("admin.users.write")),
):
    from hub.models import PrincipalCreate, PrincipalView
    from hub.services import admin as admin_svc

    identity = _identity
    body = PrincipalCreate(**(await request.json()))
    p = await admin_svc.create_principal(
        _db(request),
        kind=body.kind.value,
        username=body.username,
        display_name=body.display_name,
        email=body.email,
        notes=body.notes,
        password=body.password,
        role_slug=body.role or None,
        created_by=identity.principal_id,
    )
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="create_principal",
        target_type="principal",
        target_id=str(p["id"]),
        summary=f"Created {body.kind.value} principal {body.username!r}",
    )
    return PrincipalView(**p)


@app.get("/api/admin/principals/{principal_id}")
async def api_admin_get_principal(
    principal_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.read")),
):
    from hub.models import PrincipalView
    from hub.services import admin as admin_svc

    p = await admin_svc.get_principal(_db(request), principal_id)
    if not p:
        raise HTTPException(404, "principal not found")
    return PrincipalView(**p)


@app.patch("/api/admin/principals/{principal_id}")
async def api_admin_update_principal(
    principal_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.users.write")),
):
    from hub.models import PrincipalUpdate, PrincipalView
    from hub.services import admin as admin_svc

    identity = _identity
    body = PrincipalUpdate(**(await request.json()))
    p = await admin_svc.update_principal(
        _db(request),
        principal_id,
        display_name=body.display_name,
        email=body.email,
        notes=body.notes,
    )
    if not p:
        raise HTTPException(404, "principal not found")
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="update_principal",
        target_type="principal",
        target_id=str(principal_id),
        summary=f"Updated principal #{principal_id}",
    )
    return PrincipalView(**p)


@app.post("/api/admin/principals/{principal_id}/disable")
async def api_admin_disable_principal(
    principal_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.users.write")),
):
    from hub.models import PrincipalView
    from hub.services import admin as admin_svc
    from hub.services.admin import LastAdminError

    identity = _identity
    try:
        p = await admin_svc.disable_principal(_db(request), principal_id)
    except LastAdminError as e:
        raise HTTPException(409, str(e)) from e
    if not p:
        raise HTTPException(404, "principal not found")
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="disable_principal",
        target_type="principal",
        target_id=str(principal_id),
        summary=f"Disabled principal #{principal_id}",
    )
    return PrincipalView(**p)


@app.post("/api/admin/principals/{principal_id}/enable")
async def api_admin_enable_principal(
    principal_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.users.write")),
):
    from hub.models import PrincipalView
    from hub.services import admin as admin_svc

    identity = _identity
    p = await admin_svc.enable_principal(_db(request), principal_id)
    if not p:
        raise HTTPException(404, "principal not found")
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="enable_principal",
        target_type="principal",
        target_id=str(principal_id),
        summary=f"Enabled principal #{principal_id}",
    )
    return PrincipalView(**p)


@app.get("/api/admin/roles")
async def api_admin_list_roles(
    request: Request,
    _identity=Depends(require_permission("admin.read")),
):
    from hub.models import RoleView
    from hub.services import admin as admin_svc

    rows = await admin_svc.list_roles(_db(request))
    return [RoleView(**r) for r in rows]


@app.put("/api/admin/principals/{principal_id}/roles")
async def api_admin_set_roles(
    principal_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.roles.write")),
):
    from hub.models import RolesUpdatePayload
    from hub.services import admin as admin_svc
    from hub.services.admin import LastAdminError

    identity = _identity
    body = RolesUpdatePayload(**(await request.json()))
    try:
        slugs = await admin_svc.set_principal_roles(
            _db(request), principal_id, body.roles, granted_by=identity.principal_id
        )
    except LastAdminError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="set_roles",
        target_type="principal",
        target_id=str(principal_id),
        summary=f"Set roles for principal #{principal_id}: {', '.join(slugs)}",
    )
    return {"principal_id": principal_id, "roles": slugs}


@app.get("/api/admin/api-keys")
async def api_admin_list_keys(
    request: Request,
    principal_id: int | None = None,
    limit: int = Query(default=100, le=500),
    _identity=Depends(require_permission("admin.read")),
):
    from hub.models import ApiKeyView
    from hub.services import admin as admin_svc

    rows = await admin_svc.list_api_keys(
        _db(request), principal_id=principal_id, limit=limit
    )
    return [ApiKeyView(**r) for r in rows]


@app.post("/api/admin/principals/{principal_id}/api-keys")
async def api_admin_create_key(
    principal_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.credentials.write")),
):
    from hub.models import ApiKeyCreate, ApiKeyCreated
    from hub.services import admin as admin_svc

    identity = _identity
    body = ApiKeyCreate(**(await request.json()))
    key_data = await admin_svc.create_api_key(
        _db(request),
        principal_id,
        name=body.name,
        expires_days=body.expires_days,
        created_by=identity.principal_id,
    )
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="create_api_key",
        target_type="api_key",
        target_id=str(key_data["id"]),
        summary=f"Created API key {body.name!r} for principal #{principal_id}",
    )
    return ApiKeyCreated(**key_data)


@app.post("/api/admin/api-keys/{key_id}/revoke")
async def api_admin_revoke_key(
    key_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.credentials.write")),
):
    from hub.services import admin as admin_svc

    identity = _identity
    revoked = await admin_svc.revoke_api_key(_db(request), key_id)
    if not revoked:
        raise HTTPException(404, "key not found or already revoked")
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="revoke_api_key",
        target_type="api_key",
        target_id=str(key_id),
        summary=f"Revoked API key #{key_id}",
    )
    return {"revoked": True, "key_id": key_id}


@app.post("/api/admin/principals/{principal_id}/password")
async def api_admin_set_password(
    principal_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.credentials.write")),
):
    from hub.models import PasswordSetPayload
    from hub.services import admin as admin_svc

    identity = _identity
    body = PasswordSetPayload(**(await request.json()))
    await admin_svc.set_password(_db(request), principal_id, body.password)
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="set_password",
        target_type="principal",
        target_id=str(principal_id),
        summary=f"Password set for principal #{principal_id}",
    )
    return {"ok": True}


@app.get("/api/admin/audit")
async def api_admin_audit(
    request: Request,
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
    _identity=Depends(require_permission("admin.audit.read")),
):
    from hub.models import AuditEntry
    from hub.services import admin as admin_svc

    rows = await admin_svc.list_audit(_db(request), limit=limit, offset=offset)
    return [AuditEntry(**r) for r in rows]


@app.post("/api/admin/bootstrap")
async def api_admin_bootstrap(request: Request):
    """Bootstrap the first admin. Requires the bootstrap token or open mode."""
    from hub.models import AdminBootstrap, PrincipalView
    from hub.services import admin as admin_svc

    if not _is_open_mode() and not _check_bootstrap_token(request):
        raise HTTPException(
            403, "bootstrap requires OPENCLAW_HUB_BOOTSTRAP_ADMIN_TOKEN or open mode"
        )
    body = AdminBootstrap(**(await request.json()))
    try:
        p = await admin_svc.bootstrap_admin(
            _db(request),
            username=body.username,
            password=body.password,
            display_name=body.display_name,
            email=body.email,
        )
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return PrincipalView(**p)


def _is_open_mode() -> bool:
    if config.HUB_AUTH_DISABLED:
        return True
    return not config.HUB_TOKENS


def _check_bootstrap_token(request: Request) -> bool:
    if not config.HUB_BOOTSTRAP_TOKEN:
        return False
    bearer = request.headers.get("Authorization", "")
    if bearer.startswith("Bearer "):
        token = bearer[7:].strip()
        return token == config.HUB_BOOTSTRAP_TOKEN
    return False


# MCP transport for remote agents (Cursor, etc.). FastMCP's streamable HTTP app
# already owns the /mcp route, so mount it at root and keep this mount last so
# Hub REST/Web routes above continue to take precedence.
app.mount("/", _mcp_streamable_app)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main():
    uvicorn.run("hub.app:app", host=config.HUB_HOST, port=config.HUB_PORT, reload=False)


if __name__ == "__main__":
    main()
