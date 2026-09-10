from __future__ import annotations

import json
import subprocess
from pathlib import Path

import aiosqlite
from httpx import AsyncClient

from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins

from hub import repository as repo
from hub.models import (
    ACVerifiableBy,
    AcceptanceCriterion,
    DoRCheckItem,
    RiskKind,
    RiskSeverity,
    TaskCreate,
    TaskRefine,
    TaskRisk,
    TaskSize,
    WipTag,
    WorkType,
)
from hub.services.dor import DoREvaluation, evaluate_from_data
from hub.services.readiness import (
    parse_risks_from_row,
    DEFAULT_CONFIG,
    ReadinessConfig,
    ScoreComponent,
    calculate_readiness,
    calculate_score_from_data,
)


# --- helpers ---


def _ac(idx: int = 1) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=f"AC-{idx}",
        given="g",
        when="w",
        then="t",
        verifiable_by=ACVerifiableBy.test,
    )


def _all_passed_dor(work_type: WorkType = WorkType.feature) -> DoREvaluation:
    return evaluate_from_data(
        work_type=work_type.value,
        user_story="us",
        problem_statement="ps",
        business_value="bv",
        scope_in_count=1,
        validation_count=1,
        size="S",
        wip_tag="feature_work",
        ac_count=1,
        affected_areas_count=1,
    )


def _empty_dor(work_type: WorkType = WorkType.feature) -> DoREvaluation:
    return evaluate_from_data(
        work_type=work_type.value,
        user_story=None,
        problem_statement=None,
        business_value=None,
        scope_in_count=0,
        validation_count=0,
        size=None,
        wip_tag=None,
        ac_count=0,
    )


# --- pure scoring ---


def test_perfect_feature_no_risks_scores_100():
    score, components = calculate_score_from_data(dor=_all_passed_dor(), risks=[])
    assert score == 100
    assert components == []


def test_empty_feature_drops_score_by_required_count_times_penalty():
    dor = _empty_dor()
    score, components = calculate_score_from_data(dor=dor, risks=[])
    expected_required_failed = len(dor.required)  # all 8 fail
    assert score == 100 - expected_required_failed * DEFAULT_CONFIG.penalty_required
    assert all(c.delta == -DEFAULT_CONFIG.penalty_required for c in components)
    assert len(components) == expected_required_failed


def test_optional_check_failures_use_optional_penalty():
    """For docs: only scope+size required. user_story etc. failing is optional."""
    dor = evaluate_from_data(
        work_type=WorkType.docs.value,
        user_story=None,
        problem_statement=None,
        business_value=None,
        scope_in_count=1,
        validation_count=0,
        size="S",
        wip_tag=None,
        ac_count=0,
    )
    score, components = calculate_score_from_data(dor=dor, risks=[])
    # all required pass for docs, so dor.passed is True
    assert dor.passed is True
    optional_failed = sum(
        1 for c in components if c.delta == -DEFAULT_CONFIG.penalty_optional
    )
    assert optional_failed > 0
    # no required-failed in this scenario
    assert all(c.delta == -DEFAULT_CONFIG.penalty_optional for c in components)
    assert score == 100 - optional_failed * DEFAULT_CONFIG.penalty_optional


def test_risks_subtract_by_severity():
    dor = _all_passed_dor()
    risks = [
        TaskRisk(
            kind=RiskKind.security,
            severity=RiskSeverity.high,
            description="d",
            mitigation="m",
        ),
        TaskRisk(
            kind=RiskKind.unknown_unknowns,
            severity=RiskSeverity.medium,
            description="d",
            mitigation="m",
        ),
        TaskRisk(
            kind=RiskKind.large_scope,
            severity=RiskSeverity.low,
            description="d",
            mitigation="m",
        ),
    ]
    score, components = calculate_score_from_data(dor=dor, risks=risks)
    # These three carry a mitigation, so they are priced from the mitigated
    # table (#610). The intent of this test is unchanged — penalties still
    # scale with severity — but which table applies is now part of the rule.
    expected = 100 - (
        DEFAULT_CONFIG.mitigated_risk_penalties[RiskSeverity.high]
        + DEFAULT_CONFIG.mitigated_risk_penalties[RiskSeverity.medium]
        + DEFAULT_CONFIG.mitigated_risk_penalties[RiskSeverity.low]
    )
    assert score == expected
    assert len(components) == 3
    assert all(c.field == "risks" for c in components)


def test_score_clamped_to_zero_minimum():
    dor = _empty_dor()
    big_risks = [
        TaskRisk(
            kind=RiskKind.security,
            severity=RiskSeverity.high,
            description="d",
            mitigation="m",
        )
    ] * 100
    score, _ = calculate_score_from_data(dor=dor, risks=big_risks)
    assert score == 0


def test_score_clamped_to_base_maximum():
    """Negative penalties (in custom config) shouldn't push above base."""
    config = ReadinessConfig(penalty_required=-5, penalty_optional=0)
    dor = _empty_dor()
    score, _ = calculate_score_from_data(dor=dor, risks=[], config=config)
    assert score == 100


def test_score_clamped_to_100_even_when_config_base_exceeds_100():
    """ReadinessReport.score has le=100 — see review fix #2.4.

    Without the upper clamp at 100 a misconfigured ``base=200`` would
    produce score=200 and crash Pydantic validation downstream.
    """
    config = ReadinessConfig(base=200, penalty_required=0, penalty_optional=0)
    dor = _all_passed_dor()
    score, _ = calculate_score_from_data(dor=dor, risks=[], config=config)
    assert score == 100


def test_components_describe_each_failure():
    dor = _empty_dor()
    _, components = calculate_score_from_data(
        dor=dor,
        risks=[
            TaskRisk(
                kind=RiskKind.external_dependency,
                severity=RiskSeverity.medium,
                description="d",
                mitigation="m",
            )
        ],
    )
    failed_keys = {c.field for c in components if c.field != "risks"}
    assert failed_keys == set(dor.required)
    assert any("DoR required" in c.reason for c in components)
    assert any("external_dependency" in c.reason for c in components)


def test_score_component_to_dict_shape():
    sc = ScoreComponent(field="x", delta=-3, reason="r")
    assert sc.to_dict() == {"field": "x", "delta": -3, "reason": "r"}


def test_custom_config_overrides_defaults():
    dor = _empty_dor()
    cfg = ReadinessConfig(
        base=50,
        penalty_required=1,
        penalty_optional=0,
        risk_penalties={
            RiskSeverity.low: 0,
            RiskSeverity.medium: 0,
            RiskSeverity.high: 0,
        },
    )
    score, _ = calculate_score_from_data(dor=dor, risks=[], config=cfg)
    assert score == 50 - len(dor.required)


def test_unknown_risk_severity_does_not_subtract():
    """Defensive: a missing severity in the penalty dict is treated as 0."""
    dor = _all_passed_dor()
    cfg = ReadinessConfig(
        risk_penalties={RiskSeverity.high: 7},
        mitigated_risk_penalties={RiskSeverity.high: 3},
    )
    risks = [
        TaskRisk(
            kind=RiskKind.other,
            severity=RiskSeverity.low,
            description="d",
            mitigation="m",
        )
    ]
    score, components = calculate_score_from_data(dor=dor, risks=risks, config=cfg)
    assert score == 100
    assert components == []


# --- async integration with repository ---


async def _make_task_with_full_dor(db: aiosqlite.Connection) -> int:
    payload = TaskCreate(
        title="t",
        user_story="us",
        problem_statement="ps",
        business_value="bv",
        scope_in=["a"],
        validation_commands=["pytest"],
        size=TaskSize.S,
        wip_tag=WipTag.feature_work,
        affected_areas=["hub/services/dor.py"],
    )
    task_id = await repo.create_task_full(db, payload, status="draft")
    await repo.add_acceptance_criterion(db, task_id, _ac(1))
    await db.commit()
    return task_id


async def test_calculate_readiness_perfect_task(db: aiosqlite.Connection):
    task_id = await _make_task_with_full_dor(db)
    report = await calculate_readiness(db, task_id)
    assert report.score == 100
    assert report.dor_passed is True
    assert report.recommendations == []
    assert report.explain is None


async def test_calculate_readiness_with_explain_returns_components(
    db: aiosqlite.Connection,
):
    payload = TaskCreate(title="t")
    task_id = await repo.create_task_full(db, payload, status="draft")
    await db.commit()

    report = await calculate_readiness(db, task_id, explain=True)
    assert report.dor_passed is False
    assert report.explain is not None
    assert all({"field", "delta", "reason"} <= e.keys() for e in report.explain)
    assert sum(e["delta"] for e in report.explain) == report.score - 100


async def test_calculate_readiness_includes_persisted_risks(db: aiosqlite.Connection):
    task_id = await _make_task_with_full_dor(db)
    await repo.update_task_structured(
        db,
        task_id,
        TaskRefine(
            risks=[
                TaskRisk(
                    kind=RiskKind.breaking_change,
                    severity=RiskSeverity.high,
                    description="api change",
                    mitigation="versioned route",
                )
            ]
        ),
    )
    await db.commit()
    report = await calculate_readiness(db, task_id)
    assert report.dor_passed is True
    assert len(report.risks) == 1
    assert report.risks[0].kind == RiskKind.breaking_change
    assert (
        report.score == 100 - DEFAULT_CONFIG.mitigated_risk_penalties[RiskSeverity.high]
    ), "the stored risk carries a mitigation, so the softer rate applies (#610)"


async def test_calculate_readiness_drops_malformed_risks(db: aiosqlite.Connection):
    task_id = await _make_task_with_full_dor(db)
    # Bypass Pydantic and write raw garbage into risks column.
    await repo.update_task(
        db,
        task_id,
        risks='[{"kind": "security", "severity": "high", "description": "d", "mitigation": "m"}, '
        '{"kind": "not-a-real-kind"}, "string", 42]',
    )
    await db.commit()

    report = await calculate_readiness(db, task_id)
    # Only the first risk validates cleanly.
    assert len(report.risks) == 1
    assert report.risks[0].kind == RiskKind.security


async def test_calculate_readiness_dor_checks_echoed_in_report(
    db: aiosqlite.Connection,
):
    task_id = await _make_task_with_full_dor(db)
    report = await calculate_readiness(db, task_id)
    keys = [c.key for c in report.dor_checks]
    assert "has_user_story" in keys
    assert all(isinstance(c, DoRCheckItem) for c in report.dor_checks)


# ---- disclosure must not be the expensive option (#610) ----
#
# Risks were priced by severity alone, so naming one cost the same whether or
# not you had a plan for it. On #546 a rewritten statement declared a fifth
# risk — high severity, WITH a mitigation — and the score fell from 73 to 58.
# The text got better and the number got worse, which makes silence the cheap
# move in a field that exists to make risk visible. The module's own comment
# had claimed all along that the penalty was for *unmitigated* risks; the code
# never looked at mitigation, and the model would not even store a risk
# without one, so the honest state "seen, no remedy yet" was inexpressible.


def _risk(severity: RiskSeverity, mitigation: str) -> TaskRisk:
    return TaskRisk(
        kind=RiskKind.other,
        severity=severity,
        description="the same risk either way",
        mitigation=mitigation,
    )


def test_a_mitigated_risk_costs_less_than_an_unmitigated_one():
    # AC-1 (#610): the only difference between these two tasks is that one
    # author wrote down what they intend to do about it.
    dor = _all_passed_dor()
    open_score, _ = calculate_score_from_data(
        dor=dor, risks=[_risk(RiskSeverity.high, "")]
    )
    handled_score, _ = calculate_score_from_data(
        dor=dor, risks=[_risk(RiskSeverity.high, "step after the main run")]
    )
    assert handled_score > open_score, (
        "declaring a risk AND handling it must not cost as much as leaving it open"
    )


def test_a_mitigated_risk_still_costs_something():
    # AC-2 (#610): a plan is not a cure. Residual risk stays visible in the
    # number, otherwise the field becomes a free way to look ready.
    dor = _all_passed_dor()
    clean, _ = calculate_score_from_data(dor=dor, risks=[])
    handled, components = calculate_score_from_data(
        dor=dor, risks=[_risk(RiskSeverity.high, "mitigated properly")]
    )
    assert handled < clean, "a mitigated risk is still a risk"
    assert components and components[0].delta < 0


def test_explain_names_the_rate_applied_to_each_risk():
    # AC-3 (#610): two rates mean the number stops being self-evident, so the
    # breakdown has to say which one applied — the same reason #612 refuses to
    # collapse "not checked" into "checked".
    dor = _all_passed_dor()
    _score, components = calculate_score_from_data(
        dor=dor,
        risks=[
            _risk(RiskSeverity.high, ""),
            _risk(RiskSeverity.low, "handled"),
        ],
    )
    reasons = " | ".join(c.reason for c in components)
    assert "unmitigated" in reasons
    assert "mitigated" in reasons.replace("unmitigated", "")


def test_a_risk_without_mitigation_is_kept_and_scored():
    # AC-4 (#610): the worst outcome of the old model was silent deletion —
    # parse_risks_from_row dropped anything that failed validation, and a risk
    # with no remedy failed it. The least-handled risks were the ones that
    # disappeared from the score entirely.
    parsed = parse_risks_from_row(
        json.dumps(
            [
                {
                    "kind": "other",
                    "severity": "high",
                    "description": "seen, no remedy yet",
                    "mitigation": "",
                }
            ]
        )
    )
    assert len(parsed) == 1, "an honest 'no plan yet' must survive validation"

    dor = _all_passed_dor()
    score, components = calculate_score_from_data(dor=dor, risks=parsed)
    assert score == 100 - DEFAULT_CONFIG.risk_penalties[RiskSeverity.high]
    assert "unmitigated" in components[0].reason


def test_risk_penalties_do_not_touch_the_dor_gate():
    # AC-5 (#610): score and gate are independent signals, and this change must
    # not quietly turn the score into a second gate.
    dor = _all_passed_dor()
    score, _ = calculate_score_from_data(
        dor=dor,
        risks=[_risk(RiskSeverity.high, ""), _risk(RiskSeverity.high, "")],
    )
    assert score < 100
    assert dor.passed is True, "declared risks never decide the DoR gate"


# --- #1232: the subject of a statement must be in the base branch ---


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(root),
        },
    ).stdout.strip()


SUBJECT_PATH = "hub/services/thing.py"
OTHER_BRANCH = "task-9001/delivers-the-subject"

# What the base branch holds in the case the executors met on 09.09: the name
# is nowhere near the module.
BASE_WITHOUT_SUBJECT = '''"""A module that knows nothing about the subject yet."""


def existing_helper() -> int:
    return 1
'''

# The same module in the shape a GREP would be fooled by: the name appears in
# the docstring, in a comment, in a string literal and in a dead call — four
# textual hits, zero definitions.
BASE_MENTIONING_SUBJECT_IN_PROSE = '''"""A module that only talks about stranded_base."""

# stranded_base will live here one day, once #1204 lands.
QUERY = "select stranded_base from tasks"


def existing_helper() -> int:
    # A dead reference: nobody ever defined this here.
    return len("stranded_base")
'''

# What the unfinished branch of the other task actually delivers.
BRANCH_WITH_SUBJECT = '''"""The module as the other task's branch leaves it."""


def existing_helper() -> int:
    return 1


def stranded_base() -> str:
    return "here"


def base_can_deliver_itself() -> bool:
    return True
'''


def _clone_with(tmp_path: Path, base_source: str, branch_source: str) -> str:
    """A clone whose base branch and one task branch differ in ONE module."""
    root = tmp_path / "clone"
    (root / "hub" / "services").mkdir(parents=True)
    _git(root, "init", "-b", "develop")
    (root / SUBJECT_PATH).write_text(base_source)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    _git(root, "checkout", "-q", "-b", OTHER_BRANCH)
    (root / SUBJECT_PATH).write_text(branch_source)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "the other task's work")
    _git(root, "checkout", "-q", "develop")
    return str(root)


def _use_real_git_reads(monkeypatch) -> None:
    """Read files from a real repository; leave every other git op mocked."""
    real = GitOpsIntegration()
    monkeypatch.setattr(plugins.git_ops, "file_at_ref", real.file_at_ref, raising=False)
    monkeypatch.setattr(
        plugins.git_ops, "files_at_ref", real.files_at_ref, raising=False
    )


async def _point_project_at(db: aiosqlite.Connection, workspace: str) -> int:
    """A project whose declared clone is the fixture repository."""
    return await repo.create_project(
        db,
        slug="subject",
        name="Subject",
        workspace_path=workspace,
        default_branch="develop",
    )


async def _other_task_holding_the_subject(
    client: AsyncClient, db: aiosqlite.Connection, status: str = "review"
) -> int:
    """A task whose branch carries the subject and which has not delivered."""
    other_id = (
        await client.post("/api/tasks", json={"title": "Гейт доставки"})
    ).json()["id"]
    await db.execute(
        "UPDATE tasks SET status=?, branch=?, submission_sha='c0ffee' WHERE id=?",
        (status, OTHER_BRANCH, other_id),
    )
    await db.commit()
    return other_id


async def _task_named(
    client: AsyncClient,
    db: aiosqlite.Connection,
    project_id: int,
    hints: str,
    areas: list[str] | None = None,
) -> int:
    task_id = (
        await client.post("/api/tasks", json={"title": "Задача с предметом"})
    ).json()["id"]
    await repo.update_task(db, task_id, project_id=project_id)
    await db.commit()
    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "technical_hints": hints,
            "affected_areas": areas if areas is not None else [SUBJECT_PATH],
        },
    )
    await client.post(f"/api/tasks/{task_id}/approve", json={"force": True})
    await client.post(f"/api/tasks/{task_id}/claim", json={"agent": "dev"})
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: implement"},
    )
    return task_id


async def _pair_start(client: AsyncClient, task_id: int):
    return await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )


async def test_a_task_whose_subject_is_still_in_someone_elses_branch_does_not_open(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path: Path
):
    """AC-1 (#1232): the #1209 case — the symbols live only on another branch.

    The refusal has to name things, not decline politely: the NAMES that are
    missing and the NUMBER of the task whose delivery has to be waited for.
    Every executor on 09.09 had to find both by hand.
    """
    workspace = _clone_with(tmp_path, BASE_WITHOUT_SUBJECT, BRANCH_WITH_SUBJECT)
    _use_real_git_reads(monkeypatch)
    project_id = await _point_project_at(db, workspace)
    other_id = await _other_task_holding_the_subject(client, db)
    task_id = await _task_named(
        client,
        db,
        project_id,
        "Опереться на stranded_base и base_can_deliver_itself из гейта доставки.",
    )

    resp = await _pair_start(client, task_id)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "subject_not_in_base_branch"
    assert set(detail["missing"]) == {"stranded_base", "base_can_deliver_itself"}
    assert detail["found_in_task_id"] == other_id
    assert detail["found_in_branch"] == OTHER_BRANCH
    # The task did NOT open.
    assert (await client.get(f"/api/tasks/{task_id}")).json()["status"] == "claimed"
    # And the card says so out loud, with the same names and number.
    feed = " ".join(
        u["content"] for u in (await client.get(f"/api/tasks/{task_id}/updates")).json()
    )
    assert "stranded_base" in feed
    assert "base_can_deliver_itself" in feed
    assert f"#{other_id}" in feed
    assert OTHER_BRANCH in feed


async def test_new_work_is_not_mistaken_for_a_missing_subject(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path: Path
):
    """AC-2 (#1232): absent from the base branch is the NORMAL case.

    #1172 passed exactly this way on the same day and turned out to be
    perfectly implementable. The refusal rests on TWO facts, and this test is
    what dies when the second one is dropped: simplify the rule to "the symbol
    is not in the base branch" and a task that merely creates something new
    stops opening — which would be worse than no check at all.
    """
    workspace = _clone_with(tmp_path, BASE_WITHOUT_SUBJECT, BRANCH_WITH_SUBJECT)
    _use_real_git_reads(monkeypatch)
    project_id = await _point_project_at(db, workspace)
    # The branch of the other task exists and is unfinished — it simply does
    # not carry THIS subject.
    await _other_task_holding_the_subject(client, db)
    task_id = await _task_named(
        client, db, project_id, "Завести completely_new_symbol и never_written_before."
    )

    resp = await _pair_start(client, task_id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"

    from hub.services.readiness import SUBJECT_NEW_WORK, subject_presence

    row = await repo.get_task(db, task_id)
    presence = await subject_presence(db, dict(row))
    assert presence.verdict == SUBJECT_NEW_WORK
    # Named as missing — the verdict is not "found", it is "missing and nobody
    # else is carrying it".
    assert set(presence.missing) == {"completely_new_symbol", "never_written_before"}


async def test_a_name_in_a_comment_is_not_a_subject(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path: Path
):
    """AC-3 (#1232): the check is runtime-shaped, not textual.

    The base branch here mentions ``stranded_base`` four times — docstring,
    comment, string literal, dead reference — and defines it zero times. A
    grep would call the subject present and open an empty task; asking what
    the module BINDS gives the answer ``hasattr`` would give after an import.
    """
    from hub.services.readiness import defined_names

    # The direct statement of the property, without any hub around it.
    bound = defined_names(BASE_MENTIONING_SUBJECT_IN_PROSE)
    assert bound is not None
    assert "stranded_base" not in bound
    assert "existing_helper" in bound
    assert "stranded_base" in BASE_MENTIONING_SUBJECT_IN_PROSE  # a grep WOULD find it

    # And the same property where it costs something: the task still refuses.
    workspace = _clone_with(
        tmp_path, BASE_MENTIONING_SUBJECT_IN_PROSE, BRANCH_WITH_SUBJECT
    )
    _use_real_git_reads(monkeypatch)
    project_id = await _point_project_at(db, workspace)
    other_id = await _other_task_holding_the_subject(client, db)
    task_id = await _task_named(client, db, project_id, "Починить stranded_base.")

    resp = await _pair_start(client, task_id)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["missing"] == ["stranded_base"]
    assert detail["found_in_task_id"] == other_id


async def test_a_branch_with_no_submission_is_not_something_to_wait_for(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path: Path
):
    """An unfinished branch is one that has work ON it (#1232).

    A task that owns a branch name and never submitted has nothing to deliver,
    so treating it as the thing to wait for would refuse openings on the
    strength of an empty branch.
    """
    workspace = _clone_with(tmp_path, BASE_WITHOUT_SUBJECT, BRANCH_WITH_SUBJECT)
    _use_real_git_reads(monkeypatch)
    project_id = await _point_project_at(db, workspace)
    other_id = await _other_task_holding_the_subject(client, db)
    await db.execute("UPDATE tasks SET submission_sha='' WHERE id=?", (other_id,))
    await db.commit()
    task_id = await _task_named(client, db, project_id, "Опереться на stranded_base.")

    resp = await _pair_start(client, task_id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"


async def test_a_statement_with_no_names_opens_as_before(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path: Path
):
    """No names in the hints means the check has nothing to say (#1232).

    The stated mitigation of the one risk this task carries: hints are free
    text, and a silent refusal on an empty list of names would cost more than
    the empty task it is meant to prevent.
    """
    workspace = _clone_with(tmp_path, BASE_WITHOUT_SUBJECT, BRANCH_WITH_SUBJECT)
    _use_real_git_reads(monkeypatch)
    project_id = await _point_project_at(db, workspace)
    await _other_task_holding_the_subject(client, db)
    task_id = await _task_named(
        client, db, project_id, "Сделать хорошо, а плохо не делать.", areas=[]
    )

    resp = await _pair_start(client, task_id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"


def test_subject_names_ignores_prose_and_paths():
    """Only identifier-shaped tokens, and paths donate no symbols (#1232)."""
    from hub.services.readiness import subject_names

    names = subject_names(
        "Правка в hub/services/delivery_state.py: символы stranded_base и "
        "base_can_deliver_itself, класс ReadinessConfig. Ветка task-1204/foo."
    )
    assert names == [
        "stranded_base",
        "base_can_deliver_itself",
        "ReadinessConfig",
    ]


async def test_the_dispatch_start_refuses_the_same_way(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path: Path
):
    """Both doors into work, not one (#1232).

    A rule applied at one of two call sites is a rule with a hole: pair-start
    and the headless dispatch start are the two ways a task reaches
    ``running``, and an empty task opened through the second one costs exactly
    what it costs through the first.
    """
    workspace = _clone_with(tmp_path, BASE_WITHOUT_SUBJECT, BRANCH_WITH_SUBJECT)
    _use_real_git_reads(monkeypatch)
    project_id = await _point_project_at(db, workspace)
    other_id = await _other_task_holding_the_subject(client, db)
    task_id = (
        await client.post("/api/tasks", json={"title": "Через диспетчер"})
    ).json()["id"]
    await repo.update_task(db, task_id, project_id=project_id)
    await db.commit()
    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "technical_hints": "Опереться на stranded_base.",
            "affected_areas": [SUBJECT_PATH],
        },
    )
    await client.post(f"/api/tasks/{task_id}/approve", json={"force": True})
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: implement"},
    )

    resp = await client.post(f"/api/tasks/{task_id}/start", json={})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["found_in_task_id"] == other_id
    assert (await client.get(f"/api/tasks/{task_id}")).json()["status"] == "open"
