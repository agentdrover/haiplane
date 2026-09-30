"""Повтор отложенной находки и его границы (#1448).

Находка, осознанно отложенная до НЕдоставленной задачи, не должна заново
запирать каждую следующую сдачу у автопилота и стюарда. Правило узкое, и
главное здесь — его ГРАНИЦЫ: каждая проверяется своим прогоном, а не одним
удачным примером, потому что проверка, умеющая только пропускать, неотличима
от выключателя.
"""

from __future__ import annotations

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.services import gate_grounds as grounds
from hub.services.delivery_state import DELIVERED, UNKNOWN
from hub.services.finding_identity import finding_uids
from hub.services.steward_apply import REFUSED_UNCLOSED, apply_refusals
from tests.test_steward_apply import _FINDING_B, _codes, _green
from tests.test_steward_shadow import _project, _task

_DEFERRED = {
    "title": "оркестратор не вызывается из CLI",
    "severity": "low",
    "category": "correctness",
    "locator": "none",
    "file": "",
}
_SECURITY = {
    "title": "токен пишется в лог",
    "severity": "low",
    "category": "security",
    "locator": "none",
    "file": "",
}


def _uid(finding: dict) -> str:
    return finding_uids([finding])[0]


async def _defer(
    db: aiosqlite.Connection,
    task_id: int,
    finding: dict,
    *,
    linked: int | None,
    outcome: str = "deferred",
) -> None:
    """Прошлая сдача: находка названа исходом и, возможно, привязана к задаче."""
    review_id = await repo.insert_machine_review(
        db, task_id=task_id, submission_generation=0, incomplete=False
    )
    await repo.upsert_finding_outcome(
        db,
        review_id=review_id,
        task_id=task_id,
        submission_generation=0,
        finding_uid=_uid(finding),
        finding_index=0,
        finding_title=finding["title"],
        outcome=outcome,
        note="делается отдельной задачей",
        linked_task_id=linked,
        reported_by="pda_claude",
    )
    await db.commit()


async def _follow_up(db: aiosqlite.Connection, *, state: str | None = None) -> int:
    """Задача, куда отложили. ``state`` — что о её доставке записал свип."""
    linked = await repo.create_task(
        db,
        title="сюда отложено",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="open",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    if state is not None:
        await repo.record_delivery_discrepancy(
            db,
            task_id=linked,
            state=state,
            reason="ответ свипа",
            delivery_path="outside_gate" if state == DELIVERED else "unknown",
        )
    await db.commit()
    return linked


async def _bare_task(db: aiosqlite.Connection) -> int:
    return await _follow_up(db)


# Случаи AC-2: каждое условие названо в задаче и проверяется само по себе.
# (имя, находка отчёта, кроме неё в отчёте, состояние доставки связанной
# задачи, ссылается ли исход на задачу)
_BREAKERS = [
    ("security", _SECURITY, [], None, True),
    ("delivered", _DEFERRED, [], DELIVERED, True),
    ("unknown", _DEFERRED, [], UNKNOWN, True),
    ("unlinked", _DEFERRED, [], None, False),
    ("another_confirmed", _DEFERRED, [_FINDING_B], None, True),
]


async def test_deferral_does_not_excuse_security_delivered_unknown_or_unlinked(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """AC-2: пять границ правила, и стюард видит тот же перечень, что автопилот.

    Идёт парой к эталону: без него «блокер есть» в каждом случае доказывало
    бы только, что правило не работает нигде.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "deferral-bounds")

    # --- Эталон: то же самое без нарушения границ — повтор не блокирует ----
    task_id = await _bare_task(db)
    linked = await _follow_up(db)
    await _defer(db, task_id, _DEFERRED, linked=linked)
    repeats = await grounds.deferred_repeats(db, task_id, [_DEFERRED])
    assert set(repeats) == {_uid(_DEFERRED)}, repeats
    assert repeats[_uid(_DEFERRED)]["linked_task_id"] == linked
    assert grounds.unattended_blockers([_DEFERRED], [], False, repeats) == ()

    steward_task = await _task(db, project_id)
    # Отсрочка — ДО отчёта: «последний отчёт» задачи должен остаться текущим.
    await _defer(db, steward_task, _DEFERRED, linked=await _follow_up(db))
    await _green(db, steward_task, confirmed=[_DEFERRED])
    assert REFUSED_UNCLOSED not in _codes(await apply_refusals(db, steward_task)), (
        "стюард обязан считать тот же повтор не блокером, что и автопилот"
    )

    # --- Границы: каждая возвращает блокер обоим ---------------------------
    for name, finding, others, state, linked_flag in _BREAKERS:
        confirmed = [finding, *others]
        task_id = await _bare_task(db)
        target = await _follow_up(db, state=state)
        await _defer(db, task_id, finding, linked=target if linked_flag else None)

        repeats = await grounds.deferred_repeats(db, task_id, confirmed)
        blockers = grounds.unattended_blockers(confirmed, [], False, repeats)
        assert "confirmed" in blockers, f"{name}: автопилот обязан отказать"

        steward_task = await _task(db, project_id)
        await _defer(
            db,
            steward_task,
            finding,
            linked=await _follow_up(db, state=state) if linked_flag else None,
        )
        await _green(db, steward_task, confirmed=confirmed)
        assert REFUSED_UNCLOSED in _codes(await apply_refusals(db, steward_task)), (
            f"{name}: стюард обязан отказать там же, где автопилот"
        )


async def test_only_the_deferred_outcome_with_the_same_uid_excuses(
    db: aiosqlite.Connection,
) -> None:
    """Исход другой, находка другая, задачи нет — повтором не считается."""
    other = {**_DEFERRED, "title": "совсем другая находка"}
    for outcome in ("wont_fix", "fixed", "false_positive"):
        task_id = await _bare_task(db)
        linked = await _follow_up(db)
        await _defer(db, task_id, _DEFERRED, linked=linked, outcome=outcome)
        assert await grounds.deferred_repeats(db, task_id, [_DEFERRED]) == {}, outcome

    task_id = await _bare_task(db)
    await _defer(db, task_id, _DEFERRED, linked=await _follow_up(db))
    assert await grounds.deferred_repeats(db, task_id, [other]) == {}, (
        "повтор узнаётся по finding_uid, а не по теме"
    )

    task_id = await _bare_task(db)
    await _defer(db, task_id, _DEFERRED, linked=999_999)
    assert await grounds.deferred_repeats(db, task_id, [_DEFERRED]) == {}, (
        "ссылка на несуществующую задачу — не отсрочка"
    )


async def test_unresolved_and_incomplete_are_not_excused_by_a_deferral(
    db: aiosqlite.Connection,
) -> None:
    """Правило только про confirmed: остальные разделы блокируют как раньше."""
    task_id = await _bare_task(db)
    await _defer(db, task_id, _DEFERRED, linked=await _follow_up(db))
    repeats = await grounds.deferred_repeats(db, task_id, [_DEFERRED])
    assert grounds.unattended_blockers(
        [_DEFERRED], [{"title": "неразрешённая"}], True, repeats
    ) == ("unresolved", "incomplete")


@pytest.mark.parametrize("repeats", [None, {}])
def test_without_repeats_the_old_rule_stands(repeats) -> None:
    assert grounds.unattended_blockers([_DEFERRED], [], False, repeats) == (
        "confirmed",
    )


async def test_the_repeat_is_shown_in_the_report_not_hidden(
    db: aiosqlite.Connection,
) -> None:
    """Правило «не блокирует» не равно «спрятано»: карточка и бриф называют повтор.

    Идёт через настоящий сборщик отчёта, а не через предикат: uid считается
    по модели отчёта, и расхождение с uid по сырому JSON увело бы метку мимо
    находки — тихо, потому что гейты при этом считали бы верно.
    """
    from hub.services.review_evidence import report_view

    project_id = await _project(db, "deferral-shown")
    task_id = await _task(db, project_id)
    linked = await _follow_up(db)
    await _defer(db, task_id, _DEFERRED, linked=linked)
    await _green(db, task_id, confirmed=[_DEFERRED, _FINDING_B])

    row = await repo.get_task(db, task_id)
    report = await report_view(
        db, dict(row), await repo.get_latest_machine_review(db, task_id)
    )

    shown = report.machine_review.deferred_repeats
    assert [(r.finding_uid, r.linked_task_id) for r in shown] == [
        (_uid(_DEFERRED), linked)
    ], "метка только у повтора, не у соседней находки"
    by_uid = {f.finding_uid: f for f in report.machine_review.findings_confirmed}
    assert _uid(_DEFERRED) in by_uid, "uid по модели отчёта совпал с uid по сырому JSON"
