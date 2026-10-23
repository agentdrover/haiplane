"""Советник не заказывается там, где approve стюарда не применится (#2024).

Платный прогон советника бесполезен, когда одобрение стюарда исключено
причиной, не зависящей от ответа советника: целостность CI текущей сдачи не
чиста или класс выше обычного потолка при недостижимой двойной чистоте. Пропуск
- не согласие: правило применения (обязательный concur) не менялось, а гейты
integrity и потолка остаются у привратника применения.

Пропуск узкий. Теневой режим, недостаточная выборка пар для выхода в act,
достижимая двойная чистота и непрочитанные данные заказывают советника как
раньше: иначе набор пар для выхода в act остановился бы.
"""

from __future__ import annotations

import json

import aiosqlite

from hub import config
from hub import repository as repo
from hub import repository_ci_integrity as store
from hub.db import fetchall
from hub.services import ci_integrity, steward_apply, steward_cap
from hub.services import steward_shadow as sh
from hub.services.steward_act_readiness import _advisor_reason
from hub.services.steward_exit import contour_counts
from hub.services.steward_advisor_skip import mark_of, record_skip
from hub.services.steward_advisor import (
    STATE_NOT_ORDERED,
    advisor_refusal,
    advisor_state,
    apply_advisor_outcomes,
    order_due_advisors,
)
from tests.steward_double_clean_support import (
    SHA,
    build_case,
    judge_and_advise,
    policy_of,
    set_policy,
    verdict_events,
    verdict_of,
)

_SIGNS = json.dumps([{"path": ".github/workflows/ci.yml", "line": 3, "rule": "wf"}])
_SKIPPED = "steward_advisor_skipped"


async def _mode(monkeypatch, effective: str) -> None:
    """Режим выдан тем же читателем, что и в бою: ``mode_report``."""
    real = sh.mode_report

    async def _report(_db):
        return {**await real(_db), "effective": effective}

    monkeypatch.setattr(sh, "mode_report", _report)


async def _scene(
    db, monkeypatch, slug: str, *, risk: str = "R1", policy=None, mode="act"
):
    """Сдача с approve судьи и БЕЗ заказа советника; режим ``mode`` выдан.

    ``mode=None`` - выдача настоящая: act запрошен настройкой, пар в выборке нет.
    """
    case = await build_case(
        db, monkeypatch, slug, risk=risk, policy=policy, pair=True, order=False
    )
    await judge_and_advise(db, case.task_id, concur=None)
    if mode is None:
        monkeypatch.setattr(config, "STEWARD_MODE", "act")
    else:
        await _mode(monkeypatch, mode)
    return case


async def _signs(db: aiosqlite.Connection, task_id: int) -> None:
    await store.save_ci_integrity_scan(db, task_id, SHA, "signs", _SIGNS, "")
    await db.commit()


async def _clean(db: aiosqlite.Connection, task_id: int) -> None:
    await store.save_ci_integrity_scan(db, task_id, SHA, "clean", "[]", "")
    await db.commit()


async def _advisor_rows(db: aiosqlite.Connection, task_id: int) -> list:
    return await fetchall(
        db,
        "SELECT * FROM steward_runs WHERE task_id=? AND kind='advisor'",
        (task_id,),
    )


async def _skips(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    rows = await fetchall(
        db, "SELECT * FROM events WHERE kind=? AND task_id=?", (_SKIPPED, task_id)
    )
    return [dict(r) for r in rows]


async def _skip_lines(db: aiosqlite.Connection, task_id: int) -> list[str]:
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if "советник не заказан" in dict(u)["content"].lower()
    ]


async def _no_approve(db: aiosqlite.Connection, task_id: int) -> None:
    assert await verdict_of(db, task_id) == "", "approve без согласия записан"
    assert await verdict_events(db, task_id) == []


async def test_no_advisor_where_approve_cannot_apply(
    db: aiosqlite.Connection, db_dsn, monkeypatch
):
    """AC-1: integrity и потолок без double-clean, режим act - заказа нет.

    Повторные тики и рестарт (новое соединение) не рождают ни заказа, ни второй
    записи причины; пропуск не согласие - approve не записан.
    """
    integrity = await _scene(db, monkeypatch, "skip-integrity", risk="R1")
    await _signs(db, integrity.task_id)
    ceiling = await _scene(
        db, monkeypatch, "skip-ceiling", risk="R3", policy=policy_of(clean=None)
    )

    nopair = await build_case(
        db,
        monkeypatch,
        "skip-nopair",
        risk="R3",
        pair=False,
        order=False,
    )
    await judge_and_advise(db, nopair.task_id, concur=None)
    await _mode(monkeypatch, "act")  # двойная чистота допускается, пары нет
    overclass = await _scene(db, monkeypatch, "skip-overclass", risk="R5")
    # чужая цитата ключа в комментарии пропуском не считается
    await repo.add_task_update(
        db,
        integrity.task_id,
        "denis",
        "status",
        mark_of(1, "ci_integrity"),
        author_kind="principal",
    )
    await db.commit()
    assert not (await advisor_state(db, integrity.task_id, 1)).skipped
    modes: list[int] = []
    real_mode = sh.mode_report

    async def _counted(_db):
        modes.append(1)
        return await real_mode(_db)

    monkeypatch.setattr(sh, "mode_report", _counted)
    for _ in range(3):
        assert await order_due_advisors(db) == 0
        assert await apply_advisor_outcomes(db) == 0
    assert len(modes) == 3, "режим читается один раз за проход, не на кандидата"
    other = await aiosqlite.connect(db_dsn, uri=True)
    other.row_factory = aiosqlite.Row
    try:
        assert await order_due_advisors(other) == 0, "после рестарта - то же"
    finally:
        await other.close()

    counts = await contour_counts(db)
    assert counts.advisor_skipped == 4 and counts.advisor_pending == 0

    for case, marker in (
        (integrity, "CI"),
        (ceiling, "потолк"),
        (nopair, "потолк"),
        (overclass, "потолк"),
    ):
        task_id = case.task_id
        assert await _advisor_rows(db, task_id) == [], "в steward_runs нет advisor"
        events = await _skips(db, task_id)
        assert len(events) == 1, "одна запись причины на пакет"
        assert json.loads(events[0]["payload"])["generation"] == 1
        lines = await _skip_lines(db, task_id)
        assert len(lines) == 1 and marker in lines[0], lines
        await _no_approve(db, task_id)
        state = await advisor_state(db, task_id, 1)
        assert state.state == STATE_NOT_ORDERED and marker in state.reason
        refusal = await advisor_refusal(db, task_id, 1)
        assert refusal is not None, "пропуск не согласие"
        # привратник применения сам отказывает по той же независимой причине
        refusals = await steward_apply.apply_refusals(db, task_id)
        assert any(marker in detail for _, detail in refusals), refusals
        blocked = _advisor_reason(state)
        assert blocked is not None and blocked[0] == "blocked", "известный запрет"

    # тот же код с другим текстом - не новая причина
    assert not await record_skip(db, integrity.task_id, 1, "ci_integrity", "иначе")

    # маршрут вердикта называет человека там, где стюард не применит approve
    await _route_names_the_human(db, nopair)
    await _route_names_the_human(db, integrity)

    # события чистятся по сроку: дедуп переживает это через строку ленты
    await db.execute("DELETE FROM events WHERE kind=?", (_SKIPPED,))
    await db.commit()
    assert await order_due_advisors(db) == 0
    for case in (integrity, ceiling, nopair, overclass):
        assert len(await _skip_lines(db, case.task_id)) == 1
        assert await _skips(db, case.task_id) == [], "событие не воскресло"
        state = await advisor_state(db, case.task_id, 1)
        assert state.skipped, "читатели видят пропуск и после чистки событий"
    assert (await contour_counts(db)).advisor_skipped == 4
    # причина изменилась (потолок поднят), ключ тот же - второй записи нет
    await set_policy(
        db,
        ceiling.project_id,
        {**policy_of(clean=None), "steward_approve_max_class": "r2"},
    )
    assert await order_due_advisors(db) == 0
    assert not db.in_transaction, "повторный пропуск не оставил открытую транзакцию"
    assert len(await _skip_lines(db, ceiling.task_id)) == 1

    # цитата чужого ключа внутри причины (справка ревьюера) не гасит другой пропуск
    quoted = f"справка: {mark_of(7, 'class_above_policy_ceiling')}"
    assert await record_skip(db, integrity.task_id, 7, "ci_integrity", quoted)
    assert await record_skip(
        db, integrity.task_id, 7, "class_above_policy_ceiling", "потолок"
    )
    await db.commit()


async def test_shadow_and_double_clean_still_order_the_advisor(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-2: тень, малая выборка, достижимая двойная чистота, непрочитанное - заказ."""
    for label in (
        "shadow",
        "sample",
        "double_clean",
        "unknown_class",
        "unscanned",
        "unreadable",
        "listened_only",
        "read_failure",
        "mode_failure",
    ):
        await _ordered_as_before(db, monkeypatch, label)


async def _ordered_as_before(db, monkeypatch, label: str) -> None:
    risk = "R3" if label == "double_clean" else "R1"
    patch = None  # сбой чтения действует только на один тик заказа
    mode = {"shadow": "shadow", "sample": None}.get(label, "act")
    case = await _scene(db, monkeypatch, f"order-{label}", risk=risk, mode=mode)
    if label in ("shadow", "sample"):
        await _signs(db, case.task_id)
        if label == "sample":
            assert (await sh.mode_report(db))["effective"] == "shadow"
    elif label == "unscanned":
        await db.execute(
            "DELETE FROM ci_integrity_scans WHERE task_id=?", (case.task_id,)
        )
        await db.commit()
    elif label == "unreadable":
        await store.save_ci_integrity_scan(
            db, case.task_id, SHA, "unreadable", "[]", "дифф не получен"
        )
        await db.commit()
    elif label == "read_failure":  # не смог прочитать - не причина пропускать
        await _signs(db, case.task_id)

        async def _broken(*_a, **_kw):
            raise RuntimeError("база недоступна")

        patch = (ci_integrity, "standing_of", _broken)
    elif label == "mode_failure":  # режим не прочитан - не act
        await _signs(db, case.task_id)

        async def _unread(_db):
            raise RuntimeError("режим не прочитан")

        patch = (sh, "mode_report", _unread)
    elif label == "listened_only":  # вердикт не отдан, стюарда лишь слушают
        await _signs(db, case.task_id)
        await set_policy(
            db, case.project_id, {"verdict": "human", "steward_shadow": True}
        )
    elif label == "unknown_class":
        await db.execute("UPDATE tasks SET risk_class=NULL WHERE id=?", (case.task_id,))
        await db.commit()
        await set_policy(db, case.project_id, policy_of(clean=None))

    with monkeypatch.context() as scoped:
        if patch is not None:
            scoped.setattr(*patch)
        assert await order_due_advisors(db) == 1, label
    assert len(await _advisor_rows(db, case.task_id)) == 1
    assert await _skips(db, case.task_id) == []


async def test_skip_lifts_and_no_forbidden_approve(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-3: временная причина снята - один заказ; после заказа approve не пройдёт."""
    temp = await _scene(db, monkeypatch, "lift", risk="R1")
    await _signs(db, temp.task_id)
    assert await order_due_advisors(db) == 0
    assert len(await _skips(db, temp.task_id)) == 1

    await _clean(db, temp.task_id)  # новый CI снял причину
    assert await order_due_advisors(db) == 1
    assert await order_due_advisors(db) == 0, "заказ восстановлен ровно один раз"
    assert len(await _advisor_rows(db, temp.task_id)) == 1
    assert len(await _skips(db, temp.task_id)) == 1, "запись причины не растёт"

    for change in ("control", "integrity", "class", "policy", "generation", "cap"):
        await _change_after_the_order(db, monkeypatch, change)


async def _change_after_the_order(db, monkeypatch, change: str) -> None:
    """После заказа и concur меняются integrity, класс, политика, cap, поколение."""
    case = await build_case(db, monkeypatch, f"after-{change}", risk="R1")
    await _mode(monkeypatch, "act")
    task_id = case.task_id
    if change == "integrity":
        await _signs(db, task_id)
    elif change == "class":
        await repo.update_task(db, task_id, risk_class="R3")
        await set_policy(db, case.project_id, policy_of(clean=None))
    elif change == "policy":
        await set_policy(db, case.project_id, {"verdict": "human"})
    elif change == "cap":
        monkeypatch.setattr(config, "STEWARD_ADVISOR_DAILY_CAP", "0")
        await db.execute(  # заказ старше отказа: иначе он «заказан после отказа»
            "UPDATE steward_runs SET created_at=datetime('now','-5 seconds') "
            "WHERE task_id=? AND kind='advisor'",
            (task_id,),
        )
        await steward_cap.record_refusal(
            db, task_id, case.project_id, 1, "advisor", "x"
        )
    elif change == "generation":
        await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()

    applied = await apply_advisor_outcomes(db)
    if change == "control":
        assert applied == 1 and await verdict_of(db, task_id) == "approved"
        return
    await _no_approve(db, task_id)


async def _route_names_the_human(db: aiosqlite.Connection, case) -> None:
    """Пропуск без ответа советника не оставляет сдачу «ждать стюарда» (#2024)."""
    from types import SimpleNamespace

    from hub.services import verdict_route

    task = dict(await repo.get_task(db, case.task_id))
    project = await repo.resolve_project_for_task(db, case.task_id)
    stance = SimpleNamespace(code="unclean", reason="автопилот молчит")
    route = await verdict_route._steward_route(db, task, project, stance)
    assert (route.decider, route.final) == ("human", "human"), route
    assert route.code == verdict_route.CODE_ADVISOR_SKIPPED
