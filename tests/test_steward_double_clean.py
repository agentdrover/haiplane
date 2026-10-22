"""Стюард выше потолка класса при «двойной чистоте» (#1986).

Потолок остаётся правилом. Исключение узкое и проверяемое: два привязанных
чистых отчёта разных принципалов (один - из списка проекта), CI без разрыва и
согласие советника. Каждое условие здесь портится ПООДИНОЧКЕ - иначе тест не
отличил бы сработавшее условие от несобранной сцены.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from hub import repository as repo
from hub.models import ReviewVerdict, TaskReviewVerdict, validated_gate_policy
from hub.services import steward_apply
from hub.services.lifecycle import record_review_verdict
from hub.services.steward_apply import apply_refusals, ceiling_gate, ceiling_scope
from hub.services.steward_double_clean import frozen_pair
from hub.services.steward_applied import (
    APPLIED,
    ESCALATED_TO_HUMAN,
    apply_self_approval,
)
from tests.steward_double_clean_support import (
    CEILING,
    CLEAN_CEILING,
    FINDING,
    IMPLEMENTER,
    UNRESOLVED,
    LISTED,
    OTHER,
    REVIEWERS,
    SHA,
    add_report,
    build_case,
    ceiling_detail,
    feed,
    judge_and_advise,
    policy_of,
    verdict_events,
    verdict_of,
)

OLD_REFUSAL = (
    "class_above_policy_ceiling",
    "steward_approve_max_class=r1: класс задачи R3, по диффу R3 — выше потолка R1",
)


# ---------------------------------------------------------------------------
# AC-1 - все условия: approve применён, аудит полон, строка в карточке
# ---------------------------------------------------------------------------


async def test_double_clean_approves_above_the_ceiling(db, monkeypatch):
    case = await build_case(db, monkeypatch, "dc-approve")

    outcome, detail = await apply_self_approval(db, case.task_id, 1)

    assert outcome == APPLIED, detail
    assert await verdict_of(db, case.task_id) == "approved"
    events = await verdict_events(db, case.task_id)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert events[0]["actor"] == "steward"
    assert payload["source"] == "steward_applied"
    audit = payload["double_clean"]
    assert audit["mode"] == "double_clean"
    assert audit["generation"] == 1 and audit["sha"] == SHA
    assert audit["main"] == {"review_id": case.main, "principal_id": OTHER}
    assert audit["extra"] == {"review_id": case.extra, "principal_id": LISTED}
    assert audit["ci"]["attempt_key"] == "ci:1:1"
    assert audit["class"] == "R3"
    assert audit["policy"] == {
        CEILING: "r1",
        CLEAN_CEILING: "r3",
        REVIEWERS: [LISTED],
    }
    line = await feed(db, case.task_id)
    assert "одобрено стюардом по двойной чистоте" in line.lower()
    assert f"#{case.main}" in line and f"#{case.extra}" in line


async def test_a_class_within_the_ordinary_ceiling_carries_no_double_clean(
    db, monkeypatch
):
    """Класс в пределах обычного потолка: исключение не нужно и не пишется."""
    case = await build_case(db, monkeypatch, "dc-within", risk="R1")

    outcome, detail = await apply_self_approval(db, case.task_id, 1)

    assert outcome == APPLIED, detail
    payload = (await verdict_events(db, case.task_id))[0]["payload"]
    assert "double_clean" not in payload
    assert "двойной чистоте" not in (await feed(db, case.task_id)).lower()


# ---------------------------------------------------------------------------
# AC-2 - любое невыполненное условие оставляет потолок, с перечнем
# ---------------------------------------------------------------------------

# (метка, признак в перечне, что портим)
_BREAKS = [
    ("no_pin", "pin", {"extra": {"pinned": False}}),
    ("main_no_pin", "pin", {"main": {"pinned": False}}),
    ("sha_claimed_not_verified", "pin", {"extra": {"pinned": "claimed"}}),
    ("pinned_to_another_sha", "pin", {"main": {"sha": "c" * 40}}),
    ("incomplete", "incomplete", {"main": {"incomplete": True}}),
    ("incomplete_unclaimed", "incomplete", {"main": {"incomplete": None}}),
    ("confirmed", "confirmed", {"extra": {"confirmed": (FINDING,)}}),
    ("unresolved", "unresolved", {"main": {"unresolved": (UNRESOLVED,)}}),
    ("lost_dimensions", "lost_dimensions", {"extra": {"lost": ("security",)}}),
    ("self_reviewed", "self_reviewed", {"main": {"self_reviewed": True}}),
    ("main_principal_unknown", "identity", {"main": {"principal": None}}),
    ("extra_principal_unknown", "identity", {"extra": {"principal": None}}),
    ("extra_not_listed", "reviewers", {"policy": policy_of(reviewers=(999,))}),
    ("empty_list", None, {"policy": policy_of(reviewers=())}),
    (
        "executor_is_extra",
        "implementer",
        {
            "policy": policy_of(reviewers=(IMPLEMENTER,)),
            "extra": {"principal": IMPLEMENTER},
        },
    ),
    ("executor_is_main", "implementer", {"main": {"principal": IMPLEMENTER}}),
    ("one_principal_twice", "distinct", {"main": {"principal": LISTED}}),
    ("implementer_unknown", "identity", {"implementer": None}),
    ("ci_gap", "ci", {"ci": "fail"}),
    ("class_above_double_clean", "class", {"risk": "R4"}),
    ("advisor_object", "advisor", {"concur": "object"}),
    ("empty_extra_report", "no_data", {"extra": {"evidence": False}}),
    ("empty_main_report", "no_data", {"main": {"evidence": False}}),
    ("carried_report", "carried", {"main": {"carried_from": 1}}),
]


@pytest.mark.parametrize("label", [b[0] for b in _BREAKS])
async def test_any_missing_condition_keeps_the_ceiling(db, monkeypatch, label):
    _, token, kwargs = next(b for b in _BREAKS if b[0] == label)
    case = await build_case(db, monkeypatch, f"dc-break-{label}", **kwargs)

    refusals = await apply_refusals(db, case.task_id, 1)
    detail = ceiling_detail(refusals)

    assert detail, f"{label}: потолок обязан остаться, получено {refusals}"
    if token is not None:
        assert f"{token}:" in detail, f"{label}: перечень не назвал {token!r}: {detail}"
    outcome, _ = await apply_self_approval(db, case.task_id, 1)
    assert outcome == ESCALATED_TO_HUMAN, label
    assert await verdict_of(db, case.task_id) == ""


async def test_an_objecting_advisor_keeps_the_ceiling(db, monkeypatch):
    case = await build_case(db, monkeypatch, "dc-object", concur="object")

    refusals = await apply_refusals(db, case.task_id, 1)

    assert "advisor:" in ceiling_detail(refusals)
    outcome, _ = await apply_self_approval(db, case.task_id, 1)
    assert outcome == ESCALATED_TO_HUMAN
    assert await verdict_of(db, case.task_id) == ""


async def test_the_exception_needs_the_main_report_beside_the_listed_one(
    db, monkeypatch
):
    """Один чистый отчёт - даже от принципала из списка - пары не делает."""
    case = await build_case(db, monkeypatch, "dc-single", pair=False, order=False)
    only = await add_report(db, case.task_id, LISTED)
    await judge_and_advise(db, case.task_id)

    assert only and await frozen_pair(db, case.task_id, 1) is None
    detail = ceiling_detail(await apply_refusals(db, case.task_id, 1))
    assert detail and "pair:" in detail and "distinct:" in detail
    assert (await apply_self_approval(db, case.task_id, 1))[0] == ESCALATED_TO_HUMAN


async def test_every_unmet_condition_is_listed_not_only_the_first(db, monkeypatch):
    case = await build_case(
        db,
        monkeypatch,
        "dc-all-listed",
        main={"incomplete": True},
        extra={"confirmed": (FINDING,)},
        ci="fail",
    )

    detail = ceiling_detail(await apply_refusals(db, case.task_id, 1))

    for token in ("incomplete", "confirmed", "ci"):
        assert f"{token}:" in detail, (token, detail)


# ---------------------------------------------------------------------------
# AC-5 - ключи, края и соседние гейты: прежнее поведение байт в байт
# ---------------------------------------------------------------------------


def test_the_policy_keys_are_accepted_only_in_their_shape():
    ok = validated_gate_policy(
        {CEILING: "r1", CLEAN_CEILING: "r3", REVIEWERS: [LISTED, OTHER]}
    )
    assert ok[CLEAN_CEILING] == "r3" and ok[REVIEWERS] == [LISTED, OTHER]
    for bad in (
        {CLEAN_CEILING: "R3"},
        {CLEAN_CEILING: "high"},
        {CLEAN_CEILING: 3},
        {REVIEWERS: "21"},
        {REVIEWERS: [True]},
        {REVIEWERS: [0]},
        {REVIEWERS: [-1]},
        {REVIEWERS: [1.5]},
        {REVIEWERS: ["a"]},
    ):
        with pytest.raises(ValueError):
            validated_gate_policy(bad)


@pytest.mark.parametrize(
    "label",
    [
        "keys_absent",
        "only_class",
        "only_reviewers",
        "clean_below_ordinary",
        "clean_equals_ordinary",
        "no_ordinary_ceiling",
        "bad_class_value",
        "bad_reviewers_value",
        "unreadable_ordinary",
        "settings_edges",
        "autopilot_key",
        "agent_approve",
        "transaction_alone",
        "human_verdict",
    ],
)
async def test_off_and_other_gates_are_unchanged(db, monkeypatch, label):
    if label == "settings_edges":
        return _settings_edges()
    other = _OTHER_GATES.get(label)
    if other is not None:
        return await other(db, monkeypatch)
    policies = {
        "keys_absent": policy_of(clean=None, reviewers=None),
        "only_class": policy_of(reviewers=None),
        "only_reviewers": policy_of(clean=None),
        "clean_below_ordinary": policy_of(ordinary="r2", clean="r1"),
        "clean_equals_ordinary": policy_of(ordinary="r3", clean="r3"),
        "no_ordinary_ceiling": policy_of(ordinary=None),
        "bad_class_value": {**policy_of(), CLEAN_CEILING: "R3"},
        "bad_reviewers_value": {**policy_of(), REVIEWERS: "21"},
        "unreadable_ordinary": {**policy_of(), CEILING: "R1"},
    }
    policy = policies[label]
    case = await build_case(db, monkeypatch, f"dc-off-{label}", policy=policy)

    refusals = await apply_refusals(db, case.task_id, 1)

    if label in ("clean_equals_ordinary", "no_ordinary_ceiling"):
        assert ceiling_detail(refusals) == "", label
        return
    if label == "clean_below_ordinary":
        # R3 выше и обычного r2: ключ ниже обычного потолка ничего не даёт.
        assert OLD_REFUSAL[0] in {c for c, _ in refusals}
        assert "двойн" not in ceiling_detail(refusals)
        return
    if label == "unreadable_ordinary":
        assert ceiling_detail(refusals) == (
            "steward_approve_max_class=unreadable: значение потолка нечитаемо, "
            "approve не применяется"
        )
        return
    assert ceiling_detail(refusals) == OLD_REFUSAL[1], label
    assert (await apply_self_approval(db, case.task_id, 1))[0] == ESCALATED_TO_HUMAN


async def _autopilot_case(db, monkeypatch):
    """auto_approve_max_class не меняется: пара и ключи стюарда ему не помогают."""
    case = await build_case(
        db,
        monkeypatch,
        "dc-autopilot",
        policy={**policy_of(), "verdict": "auto", "auto_approve_max_class": "r1"},
    )
    await repo.update_task(db, case.task_id, submission_generation=1)
    await db.commit()

    async with ceiling_scope():
        await steward_apply.remember_risk_fact(
            db, case.task_id, 1, _risk_fact("R3"), "auto_approve_max_class"
        )
        refusal = await ceiling_gate(db, case.task_id, "auto_approve_max_class")

    assert refusal is not None and refusal[0] == "class_above_policy_ceiling"
    assert "двойн" not in refusal[1]


def _risk_fact(cls: str):
    from hub.services.steward_evidence import present

    return present("risk_class", "класс", stored=cls, recomputed=cls, raised=False)


async def _agent_case(db, monkeypatch):
    """#1872: approve пишет только внутренний steward_applied, не агентский вызов."""
    case = await build_case(db, monkeypatch, "dc-agent")
    project = await repo.get_project(db, case.project_id)
    assert project is not None
    await db.execute(
        "UPDATE projects SET slug='default' WHERE id=?", (case.project_id,)
    )
    await db.commit()

    with pytest.raises(HTTPException) as err:
        await record_review_verdict(
            db,
            case.task_id,
            TaskReviewVerdict(agent="steward", verdict=ReviewVerdict.approved),
            expected_generation=1,
            agent_caller=True,
        )

    assert err.value.status_code in (403, 409)
    assert await verdict_of(db, case.task_id) == ""


async def _transaction_alone_case(db, monkeypatch):
    """Транзакционный этап без раннего решения исключения не выдаёт."""
    case = await build_case(db, monkeypatch, "dc-no-early")

    with pytest.raises(HTTPException) as err:
        async with ceiling_scope():
            # Факт риска ранней проверки есть (иначе отказ случился бы раньше),
            # а решения «двойной чистоты» ранний этап не принимал.
            await steward_apply.remember_risk_fact(
                db, case.task_id, 1, _risk_fact("R3")
            )
            await record_review_verdict(
                db,
                case.task_id,
                TaskReviewVerdict(agent="steward", verdict=ReviewVerdict.approved),
                expected_generation=1,
                applied_by_steward=True,
            )

    assert "class_above_policy_ceiling" in str(err.value.detail)
    assert "early:" in str(err.value.detail), err.value.detail
    assert await verdict_of(db, case.task_id) == ""


async def _human_case(db, monkeypatch):
    case = await build_case(db, monkeypatch, "dc-human")

    await record_review_verdict(
        db,
        case.task_id,
        TaskReviewVerdict(agent="denis", verdict=ReviewVerdict.approved),
        expected_generation=1,
        human_caller=True,
    )

    assert await verdict_of(db, case.task_id) == "approved"
    payload = (await verdict_events(db, case.task_id))[0]["payload"]
    assert "double_clean" not in payload


def _settings_edges() -> None:
    """Таблица краёв ключей: исключение включается ровно одним сочетанием."""
    from hub.services.steward_double_clean import settings_of

    good = policy_of()
    assert settings_of(good) is not None
    for broken in (
        {**good, CEILING: "R1"},  # обычный потолок нечитаем
        {k: v for k, v in good.items() if k != CEILING},  # обычного потолка нет
        {**good, CLEAN_CEILING: "R3"},
        {**good, CLEAN_CEILING: None},
        {**good, REVIEWERS: []},
        {**good, REVIEWERS: "21"},
        {**good, REVIEWERS: [True]},
        {**good, CLEAN_CEILING: "r1"},  # равен обычному
        {**good, CLEAN_CEILING: "r0"},  # ниже обычного
    ):
        assert settings_of(broken) is None, broken


_OTHER_GATES = {
    "autopilot_key": _autopilot_case,
    "agent_approve": _agent_case,
    "transaction_alone": _transaction_alone_case,
    "human_verdict": _human_case,
}


# ---------------------------------------------------------------------------
# AC-6 - ошибочное двойное одобрение ловят прежние детекторы и breaker
# ---------------------------------------------------------------------------


async def _false_approvals(db, task_id: int) -> list[dict]:
    rows = await db.execute_fetchall(
        "SELECT * FROM steward_false_approvals WHERE task_id=? ORDER BY id", (task_id,)
    )
    return [dict(r) for r in rows]


async def test_detectors_catch_a_bad_double_clean_approval(db, monkeypatch):
    from hub.services.steward_breaker import steward_delivery
    from hub.services.steward_exit import concur_pairs

    case = await build_case(db, monkeypatch, "dc-detectors")
    outcome, detail = await apply_self_approval(db, case.task_id, 1)
    assert outcome == APPLIED, detail
    assert [p.task_id for p in await concur_pairs(db)] == [case.task_id]

    # Доставка этого одобрения доказана как стюардова (breaker).
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha, "
        "merged_at) VALUES (?, 1, ?, 'm' || hex(randomblob(8)), datetime('now', '+1 minute'))",
        (case.project_id, case.task_id),
    )
    await db.commit()
    merge = dict(
        (await db.execute_fetchall("SELECT merge_sha FROM pipeline_merges"))[0]
    )
    assert await steward_delivery(db, case.project_id, merge["merge_sha"]) == (
        case.task_id,
        1,
    )

    # Человеческий возврат после одобрения.
    await record_review_verdict(
        db,
        case.task_id,
        TaskReviewVerdict(
            agent="denis",
            verdict=ReviewVerdict.changes_requested,
            comments="возвращаю",
            supersedes_revision=1,
        ),
        expected_generation=1,
        human_caller=True,
    )
    rows = await _false_approvals(db, case.task_id)
    assert {r["source"] for r in rows} == {"human_changes_requested"}

    # Переоткрытие и прод-дефект - тоже случаи, как у обычного одобрения.
    await db.execute("UPDATE tasks SET status='completed' WHERE id=?", (case.task_id,))
    await db.execute("UPDATE tasks SET status='running' WHERE id=?", (case.task_id,))
    defect = await repo.create_task(
        db,
        title="дефект",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="x",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.execute(
        "UPDATE tasks SET found_in='prod', caused_by_task_id=? WHERE id=?",
        (case.task_id, defect),
    )
    await db.commit()
    sources = {r["source"] for r in await _false_approvals(db, case.task_id)}
    assert sources == {"human_changes_requested", "reopened", "prod_defect"}


async def test_the_digest_counts_double_clean_approvals(db, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from hub.services.digest import generate_due_digests

    case = await build_case(db, monkeypatch, "dc-digest")
    outcome, detail = await apply_self_approval(db, case.task_id, 1)
    assert outcome == APPLIED, detail

    assert (
        await generate_due_digests(db, now=datetime.now(UTC) + timedelta(days=1)) == 1
    )

    payload = json.loads(dict((await repo.list_digests(db))[0])["payload"])
    assert payload["steward_double_clean_approvals"] == 1
    assert payload["steward_self_approvals"][0]["double_clean"] is True


async def test_the_digest_of_a_plain_approval_is_unchanged(db, monkeypatch):
    """Одобрение в пределах обычного потолка: ни ключа, ни счётчика двойной чистоты."""
    from datetime import UTC, datetime, timedelta

    from hub.services.digest import generate_due_digests

    case = await build_case(db, monkeypatch, "dc-digest-plain", risk="R1")
    outcome, detail = await apply_self_approval(db, case.task_id, 1)
    assert outcome == APPLIED, detail

    await generate_due_digests(db, now=datetime.now(UTC) + timedelta(days=1))

    payload = json.loads(dict((await repo.list_digests(db))[0])["payload"])
    assert "steward_double_clean_approvals" not in payload
    assert "double_clean" not in payload["steward_self_approvals"][0]
    events = await db.execute_fetchall(
        "SELECT payload FROM events WHERE kind='digest_created'"
    )
    assert "steward_double_clean_approvals" not in json.loads(events[0]["payload"])
