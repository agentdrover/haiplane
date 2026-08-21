"""Hub-dispatched cross-model reviews (#757).

The hub calls the reviewer, not the implementer; failures alert once and
change nothing; a run that finished without a report fails loudly; a
report whose tokens disagree with the provider's usage is flagged.
"""

from __future__ import annotations

import json

import aiosqlite
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub import services
from hub.integrations import cursor_cloud
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import TaskRefine, TaskSubmitReview
from hub.services.project_policy import review_dispatch_enabled
from hub.services.review_dispatch import (
    pick_review_model,
    pick_review_profile,
    sweep_review_dispatches,
)

_TIP = "c" * 40


_HARMLESS_DIFF = "+++ b/docs/notes.md\n+одна строка текста\n"


class _PinnedGitOps(NoopGitOps):
    def __init__(self, tip: str, paths: list[str], diff: str | None = None) -> None:
        self._tip = tip
        self._paths = paths
        # #820: the profile is decided against the diff, so the double must
        # serve one. None means "could not be read", which buys deep.
        self._diff = _HARMLESS_DIFF if diff is None else diff

    async def branch_diff(self, repo, base, branch):
        return self._diff

    async def fetch_base(self, repo: str, base: str):
        return True, ""

    async def head_sha(self, repo: str, base: str) -> str:
        return self._tip

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        return self._paths


async def _node(
    db: aiosqlite.Connection, *, title: str, task_type: str, parent_id: int | None
) -> int:
    return await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type=task_type,
        parent_id=parent_id,
        priority="medium",
    )


class _DispatchRecorder:
    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _wire(monkeypatch, recorder: _DispatchRecorder) -> None:
    monkeypatch.setattr(config, "CURSOR_API_KEY", "test-key")
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", "reviewer-token")
    monkeypatch.setattr(cursor_cloud, "create_review_agent", recorder)


async def _submitted(
    client: AsyncClient,
    db: aiosqlite.Connection,
    slug: str,
    *,
    verdict_auto: bool = True,
    policy: dict | None = None,
    areas: list[str] | None = None,
    risks: list[dict] | None = None,
    clear_risk_class: bool = False,
    diff: str | None = None,
) -> int:
    areas = ["docs/notes.md"] if areas is None else areas
    pid = await repo.create_project(
        db,
        slug=slug,
        name=slug.title(),
        repo_name="mrPDA/spike-repo",
        workspace_path="/tmp/ws",
    )
    if policy is not None:
        await repo.update_project(db, pid, gate_policy=json.dumps(policy))
    elif verdict_auto:
        await repo.update_project(db, pid, gate_policy=json.dumps({"verdict": "auto"}))
    epic = await _node(db, title="epic", task_type="epic", parent_id=None)
    await repo.update_task(db, epic, project_id=pid)
    feature = await _node(db, title="feature", task_type="feature", parent_id=epic)
    task_id = await _node(db, title="probe", task_type="task", parent_id=feature)
    await repo.add_task_update(db, task_id, "dev", "status", "Plan: work")
    await repo.update_task_structured(
        db, task_id, TaskRefine(affected_areas=areas, risks=risks)
    )
    if clear_risk_class:
        # A task whose class was never computed: the state #582 calls
        # "not computed", which must never be read as low risk. NULL is that
        # state in the column; the empty string is not a valid class.
        await db.execute("UPDATE tasks SET risk_class = NULL WHERE id = ?", (task_id,))
    await db.commit()

    plugins.git_ops = _PinnedGitOps(_TIP, areas, diff)
    started = await services.pair_start_task(db, task_id, caller="dev-agent")
    assert started.status.value == "running"
    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )
    assert view.status.value == "review"
    return task_id


def test_pick_review_model_prefers_another_family(monkeypatch):
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL", "")
    assert pick_review_model("claude-fable-5") == "grok-4.6"
    assert pick_review_model("grok-4.5") == "gpt-5.3-codex"
    assert pick_review_model("") == "grok-4.6"
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL", "gemini-3.1-pro")
    assert pick_review_model("claude-fable-5") == "gemini-3.1-pro"


async def test_clean_submit_dispatches_cloud_reviewer(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#757): a submission in a verdict=auto project dispatches the
    # reviewer with the task branch and the hub MCP; without the policy —
    # no dispatch at all.
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-dispatch")

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["repo_url"] == "https://github.com/mrPDA/spike-repo"
    assert call["starting_ref"].startswith(f"task-{task_id}/")
    assert call["model_id"] == "grok-4.6", "claude implementer → grok reviewer"
    assert call["reviewer_token"] == "reviewer-token"
    assert call["hub_mcp_url"].endswith("/mcp")
    assert "не коммить" in call["prompt_text"]

    rows = await repo.list_active_review_dispatches(db)
    assert len(rows) == 1 and dict(rows[0])["agent_id"] == "bc-1"
    events = [
        dict(r)
        for r in await repo.list_events(
            db, since=0, kinds=["review_dispatched"], limit=10
        )
    ]
    assert events and events[0]["actor"] == "policy"
    assert json.loads(events[0]["payload"])["model"] == "grok-4.6"

    await _submitted(client, db, "spike-nopolicy", verdict_auto=False)
    assert len(recorder.calls) == 1, "no policy — no dispatch"


async def test_finished_run_without_report_alerts_once(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#757): terminal run, no report, grace expired → one loud alert
    # and status=failed; the second sweep stays quiet.
    recorder = _DispatchRecorder({"agent": {"id": "bc-2"}, "run": {"id": "run-2"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-silent")
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _finished(agent_id, run_id):
        return {"id": run_id, "status": "FINISHED"}

    monkeypatch.setattr(cursor_cloud, "get_run", _finished)

    await sweep_review_dispatches(db)
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    alerts = [u for u in updates if "отчёт НЕ сдан" in u["content"]]
    assert len(alerts) == 1
    assert not await repo.list_active_review_dispatches(db)

    await sweep_review_dispatches(db)
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert len([u for u in updates if "отчёт НЕ сдан" in u["content"]]) == 1


async def test_usage_mismatch_is_flagged(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#757): the report's tokens are cross-checked against the
    # provider's usage — a big gap is flagged to the audit, the dispatch
    # settles as done either way.
    recorder = _DispatchRecorder({"agent": {"id": "bc-3"}, "run": {"id": "run-3"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-usage")

    review = {
        "harness_skill": "multi-agent-review",
        "harness_version": 8,
        "raw_count": 3,
        "findings_confirmed": [],
        "findings_rejected": [
            {"title": "x", "category": "correctness", "reason": "no"}
        ],
        "incomplete": False,
        "unresolved": [],
        "lost_dimensions": [],
        "agent": "cursor-cloud-reviewer",
        "model": "grok-4.6",
        "tokens_spent": 1000,
    }
    resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=review)
    assert resp.status_code == 200, resp.text

    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 100_000}}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)

    await sweep_review_dispatches(db)
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    flags = [u for u in updates if "расходится с данными провайдера" in u["content"]]
    assert len(flags) == 1
    assert not await repo.list_active_review_dispatches(db)


async def test_dispatch_failure_degrades_visibly(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#757): the API refused (beta broke / no key) — one alert, the
    # submission itself is untouched.
    recorder = _DispatchRecorder(None)
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-apifail")

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] == "review", "the submit must not suffer"
    alerts = [
        u["content"] for u in body["updates"] or [] if "НЕ вызвано" in u["content"]
    ]
    assert len(alerts) == 1
    assert not await repo.list_active_review_dispatches(db)


# --- Review profiles (#807) --------------------------------------------------
#
# The profile answers "how much was this run allowed to spend", and it is
# decided by the hub before the run starts. Every kind of ignorance —
# unknown class, unreadable class, a human explicitly asking — resolves
# toward deep: cheap is the default only where the facts say it is safe.


async def _dispatch_row(db: aiosqlite.Connection, task_id: int) -> dict:
    rows = await repo.list_active_review_dispatches(db)
    mine = [dict(r) for r in rows if r["task_id"] == task_id]
    assert mine, "no dispatch recorded for the task"
    return mine[-1]


async def test_low_risk_task_gets_lite_profile(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#807): ordinary low-class work is reviewed cheaply, and the
    # profile travels with the run instead of being inferred later.
    recorder = _DispatchRecorder({"agent": {"id": "bc-lite"}, "run": {"id": "r-lite"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_LITE_TOKEN_BUDGET", 40000)

    task_id = await _submitted(client, db, "spike-lite")

    prompt = recorder.calls[0]["prompt_text"]
    assert "ЛЁГКОЕ ревью" in prompt
    assert "40000" in prompt, "the ceiling must be stated to the reviewer"
    assert "multi-agent-review" not in prompt, "lite must not call the harness"
    assert (await _dispatch_row(db, task_id))["profile"] == "lite"


async def test_high_risk_task_gets_deep_profile(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#807): a migration-class change and a declared high risk each
    # buy the expensive harness on their own.
    recorder = _DispatchRecorder({"agent": {"id": "bc-deep"}, "run": {"id": "r-deep"}})
    _wire(monkeypatch, recorder)

    by_class = await _submitted(client, db, "spike-deep-class", areas=["hub/db.py"])
    assert (await _dispatch_row(db, by_class))["profile"] == "deep"
    assert "multi-agent-review" in recorder.calls[0]["prompt_text"]

    by_risk = await _submitted(
        client,
        db,
        "spike-deep-risk",
        # #827: a TECHNICAL high risk. A product one no longer buys the
        # harness — see test_product_high_risk_does_not_buy_deep.
        risks=[{"kind": "breaking_change", "severity": "high", "description": "d"}],
    )
    assert (await _dispatch_row(db, by_risk))["profile"] == "deep", (
        "a declared technical high risk is what the expensive harness is for"
    )


async def test_unclassified_task_gets_deep_profile(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#807): no class is not a low class. Otherwise never classifying
    # a task would be the cheapest way to skip the harness.
    recorder = _DispatchRecorder({"agent": {"id": "bc-unk"}, "run": {"id": "r-unk"}})
    _wire(monkeypatch, recorder)

    # No declared areas and an empty diff: the class stays uncomputed all the
    # way through the submit-time recalculation (#583/#762).
    task_id = await _submitted(
        client, db, "spike-unclassified", areas=[], clear_risk_class=True
    )
    row = dict(await repo.get_task(db, task_id))
    assert not row["risk_class"], "the fixture must leave the class uncomputed"

    assert (await _dispatch_row(db, task_id))["profile"] == "deep"
    # And the same for a class the enum cannot read at all.
    # #820: the rule now answers with its reasons, and judges against a diff.
    assert pick_review_profile({"risk_class": "R99"}, _HARMLESS_DIFF)[0] == "deep"
    assert pick_review_profile({"risk_class": "R0"}, _HARMLESS_DIFF)[0] == "lite"
    assert (
        pick_review_profile(
            {"risk_class": "R0", "machine_review_override": "require"}, _HARMLESS_DIFF
        )[0]
        == "deep"
    ), "a human who asked for machine review asked for the real thing"


async def test_budget_truncation_marks_run_incomplete(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#807): a lite run that spent its whole ceiling did not finish
    # looking. Left as the client sent it, the report would read as a clean
    # review of the whole diff — the substitution #549 exists to prevent.
    recorder = _DispatchRecorder({"agent": {"id": "bc-b"}, "run": {"id": "r-b"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_LITE_TOKEN_BUDGET", 1000)

    task_id = await _submitted(client, db, "spike-budget")

    body = {
        "harness_skill": "lite-diff-review",
        "agent_count": 1,
        "tokens_spent": 1000,
        "raw_count": 1,
        "findings_confirmed": [
            {"title": "off-by-one", "severity": "medium", "file": "a.py"}
        ],
        "findings_rejected": [],
        "incomplete": False,
        "unresolved": [],
        "lost_dimensions": [],
        "agent": "cursor-cloud-reviewer",
    }
    resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=body)
    assert resp.status_code == 200, resp.text

    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["profile"] == "lite", "the profile comes from the dispatch"
    assert saved["incomplete"] == 1, "an exhausted budget is not a complete run"

    data = (await client.get(f"/api/tasks/{task_id}")).json()
    alerts = [
        u["content"]
        for u in data["updates"] or []
        if u["kind"] == "alert" and "бюджет" in u["content"]
    ]
    assert len(alerts) == 1 and "неполным" in alerts[0]


async def test_report_without_dispatch_has_no_profile(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # "We do not know how this was reviewed" and "it was reviewed cheaply"
    # are different facts, and the cheap one must never be assumed.
    recorder = _DispatchRecorder({"agent": {"id": ""}, "run": {}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-no-dispatch", verdict_auto=False)

    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "multi-agent-review",
            "harness_version": 8,
            "agent_count": 4,
            "tokens_spent": 999999,
            "raw_count": 2,
            "findings_confirmed": [],
            "findings_rejected": [
                {"title": "noise", "category": "style", "reason": "not a defect"}
            ],
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "dev",
        },
    )
    assert resp.status_code == 200, resp.text
    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["profile"] == ""
    assert saved["incomplete"] == 0, "no dispatch — no budget rule to apply"


# --- The review key, separate from the verdict key (#805) --------------------
#
# "Call a reviewer" and "who signs the verdict" were one switch, which left
# the hub's own project choosing between no review and no human. They are
# two questions now, and only the first one spends tokens.


async def test_dispatch_runs_without_auto_verdict(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#805): review=dispatch calls the reviewer; the verdict stays
    # human — no auto-verdict is recorded for the submission.
    recorder = _DispatchRecorder({"agent": {"id": "bc-rev"}, "run": {"id": "r-rev"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(
        client, db, "spike-review-only", policy={"review": "dispatch"}
    )

    assert len(recorder.calls) == 1, "the reviewer must be called"
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "review", "the task waits for a human verdict"
    assert not row["review_verdict"], "policy must not sign the verdict here"


async def test_verdict_auto_still_dispatches(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#805): projects that already run on verdict=auto keep working
    # without anyone editing their stored policy — the autopilot reads the
    # report, so asking for it implies asking for the review.
    recorder = _DispatchRecorder({"agent": {"id": "bc-auto"}, "run": {"id": "r-auto"}})
    _wire(monkeypatch, recorder)

    await _submitted(client, db, "spike-legacy-auto", policy={"verdict": "auto"})

    assert len(recorder.calls) == 1


async def test_absent_review_policy_never_dispatches(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#805): today's behaviour for a project that asked for nothing.
    recorder = _DispatchRecorder({"agent": {"id": "bc-no"}, "run": {"id": "r-no"}})
    _wire(monkeypatch, recorder)

    await _submitted(client, db, "spike-silent", policy={"dor": "human"})

    assert recorder.calls == []


async def test_unknown_review_value_falls_back_to_off(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#805): a typo, or a value from a future version, must not spend
    # tokens. Unreadable policy never grants anything — including budget.
    recorder = _DispatchRecorder({"agent": {"id": "bc-typo"}, "run": {"id": "r-typo"}})
    _wire(monkeypatch, recorder)

    await _submitted(client, db, "spike-typo", policy={"review": "dispath"})

    assert recorder.calls == []
    assert review_dispatch_enabled({"review": "dispatch"}) is True
    assert review_dispatch_enabled({"review": "off"}) is False
    assert review_dispatch_enabled({}) is False
    assert review_dispatch_enabled({"verdict": "auto"}) is True


# --- Process surfaces buy the expensive profile (#820) -----------------------
#
# Measured, not assumed: the lite-vs-deep comparison of 21.08.2026 found the
# cheap profile caught 2 of 7 confirmed findings, and both misses were process
# defects — an orphaned collector and a collection run against the wrong
# branch. Neither is visible in the diff text; both live on surfaces that can
# be named.

# Real added lines from the branches where those defects were found. Written
# out rather than generated, so the regression is against what actually
# happened rather than against a shape invented to pass.
_DIFF_506 = (
    "+++ b/hub/services/test_existence.py\n"
    "+        proc = await asyncio.create_subprocess_exec(\n"
    '+            "uv",\n'
    "+            stdout=asyncio.subprocess.PIPE,\n"
    "+        )\n"
    "+        out, _ = await asyncio.wait_for("
    "proc.communicate(), timeout=_COLLECT_TIMEOUT)\n"
)
_DIFF_509 = (
    "+++ b/hub/services/validation_run.py\n"
    "+            out, dropped = await asyncio.wait_for("
    "_collect(proc), timeout=_RUN_TIMEOUT)\n"
    "+        except TimeoutError:\n"
    "+            proc.kill()\n"
)


async def test_subprocess_surface_forces_deep_with_reason(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#820): a low-class diff that starts a subprocess still buys deep,
    # and the feed says which surface bought it — "deep" alone cannot be
    # argued with later.
    recorder = _DispatchRecorder({"agent": {"id": "bc-ps"}, "run": {"id": "r-ps"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-subprocess", diff=_DIFF_506)

    assert (await _dispatch_row(db, task_id))["profile"] == "deep"
    data = (await client.get(f"/api/tasks/{task_id}")).json()
    notes = [
        u["content"] for u in data["updates"] or [] if "профиль deep" in u["content"]
    ]
    assert notes, "the dispatch note must name the profile"
    assert "процессная поверхность" in notes[0]
    assert "create_subprocess_exec" in notes[0]


async def test_workspace_surface_forces_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#820): touching the workspace or branch state is the other half
    # of the measured blind spot — #506's collection ran against whatever the
    # shared clone happened to be on.
    recorder = _DispatchRecorder({"agent": {"id": "bc-ws"}, "run": {"id": "r-ws"}})
    _wire(monkeypatch, recorder)

    diff = (
        "+++ b/hub/services/thing.py\n"
        "+    await plugins.git_ops.checkout(workspace_path, branch)\n"
    )
    task_id = await _submitted(client, db, "spike-workspace", diff=diff)

    assert (await _dispatch_row(db, task_id))["profile"] == "deep"


async def test_ordinary_diff_stays_lite(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#820): the saving #807 exists for must survive. Ordinary work,
    # and the very words in a COMMENT, keep the cheap profile — otherwise
    # 'deep' becomes the default by way of prose.
    recorder = _DispatchRecorder({"agent": {"id": "bc-ord"}, "run": {"id": "r-ord"}})
    _wire(monkeypatch, recorder)

    talky = (
        "+++ b/hub/services/thing.py\n"
        "+# раньше здесь был create_subprocess_exec и wait_for(, теперь нет\n"
        '+    """Документация упоминает worktree и checkout, но кода нет."""\n'
        "+    return sorted(items)  # никаких подпроцессов\n"
    )
    task_id = await _submitted(client, db, "spike-ordinary", diff=talky)

    assert (await _dispatch_row(db, task_id))["profile"] == "lite", (
        "markers inside comments and docstrings are talk, not code"
    )


async def test_unreadable_diff_forces_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#820): a diff nobody could read is not a harmless one. Same rule
    # the ladder uses for an uncomputed class (#582).
    recorder = _DispatchRecorder({"agent": {"id": "bc-nod"}, "run": {"id": "r-nod"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-nodiff", diff="")
    # An empty string is a readable, empty diff; None is the unreadable one.
    assert (await _dispatch_row(db, task_id))["profile"] == "lite"

    profile, reasons = pick_review_profile({"risk_class": "R0"}, None)
    assert profile == "deep"
    assert reasons == ["дифф сдачи прочитать не удалось"]


def test_known_process_defect_diffs_would_get_deep():
    # AC-5 (#820): the regression standard is the real thing — the two diffs
    # whose process defects the cheap profile actually missed. If the rule
    # stops catching these, it has stopped being worth its cost.
    for name, diff in (("#506", _DIFF_506), ("#509", _DIFF_509)):
        profile, reasons = pick_review_profile({"risk_class": "R2"}, diff)
        assert profile == "deep", f"{name} must buy the expensive profile"
        assert any("процессная поверхность" in r for r in reasons), name


# --- The KIND of risk decides, not the word "high" (#827) --------------------
#
# From the first live dispatch (#818): a task honestly declaring "the daily
# message turns into noise" bought a multi-agent harness that cannot judge
# whether a message is noise. Dogfooding answers that question; a code review
# does not. The honest statement should not be the expensive one.

_PRODUCT_RISK = [
    {
        "kind": "other",
        "severity": "high",
        "description": "ежедневное сообщение превращается в шум",
    }
]


async def test_product_high_risk_does_not_buy_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#827): the real #818 risk, verbatim. It stays on the class.
    recorder = _DispatchRecorder({"agent": {"id": "bc-pr"}, "run": {"id": "r-pr"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-product-risk", risks=_PRODUCT_RISK)

    assert (await _dispatch_row(db, task_id))["profile"] == "lite"


def test_technical_high_risk_buys_deep_with_named_kind():
    # AC-2 (#827): technical high still buys the harness, and the reason says
    # WHICH kind — "high" alone was never enough to argue with.
    for kind in ("breaking_change", "data_migration", "performance"):
        profile, reasons = pick_review_profile(
            {
                "risk_class": "R0",
                "risks": json.dumps([{"kind": kind, "severity": "high"}]),
            },
            _HARMLESS_DIFF,
        )
        assert profile == "deep", kind
        assert kind in reasons[0], f"the reason must name the kind: {reasons}"


def test_security_kind_still_buys_deep_at_any_severity():
    # AC-3 (#827): unchanged from #807. A security risk somebody rated 'low'
    # is still a security risk, and rating it is not the same as judging it.
    for severity in ("low", "medium", "high"):
        profile, reasons = pick_review_profile(
            {
                "risk_class": "R0",
                "risks": json.dumps([{"kind": "security", "severity": severity}]),
            },
            _HARMLESS_DIFF,
        )
        assert profile == "deep", severity
        assert "security" in reasons[0]


async def test_process_surface_wins_over_product_risk(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#827): the new rule must not punch a hole in #820. A product risk
    # says nothing about a subprocess left running after its timeout.
    recorder = _DispatchRecorder({"agent": {"id": "bc-pw"}, "run": {"id": "r-pw"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(
        client, db, "spike-product-and-process", risks=_PRODUCT_RISK, diff=_DIFF_509
    )

    assert (await _dispatch_row(db, task_id))["profile"] == "deep"


def test_unknown_risk_kind_at_high_stays_deep():
    # AC-5 (#827): not knowing what a risk is must never be the cheap answer
    # (#582) — and it closes the obvious way around the rule.
    for kind in ("", "какой-то-новый-вид"):
        profile, reasons = pick_review_profile(
            {
                "risk_class": "R0",
                "risks": json.dumps([{"kind": kind, "severity": "high"}]),
            },
            _HARMLESS_DIFF,
        )
        assert profile == "deep", repr(kind)
        assert "нераспознанным" in reasons[0]


async def test_provider_usage_is_stored_on_the_report(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#828): the sweep already fetches the bill to compare it. Keeping
    # it is the whole point — the economics were being computed from the
    # harness's own claim while the billed number was thrown away.
    recorder = _DispatchRecorder({"agent": {"id": "bc-bill"}, "run": {"id": "r-bill"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-billed")

    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "multi-agent-review",
            "harness_version": 8,
            "raw_count": 1,
            "findings_confirmed": [{"title": "real one", "severity": "medium"}],
            "findings_rejected": [],
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "cursor-cloud-reviewer",
            "tokens_spent": 175_000,
        },
    )
    assert resp.status_code == 200, resp.text

    async def _usage(agent_id, run_id=None):
        # The real #818 numbers.
        return {"totalUsage": {"totalTokens": 6_013_569}}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)
    await sweep_review_dispatches(db)

    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["provider_tokens"] == 6_013_569
    assert saved["tokens_spent"] == 175_000, "the self-report is not overwritten"
