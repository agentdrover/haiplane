"""Спот-чек стюарда: oversample применённых approve, эскалации представлены (#1144).

Продолжение #739/#1143. Детерминированная ~10% выборка для спот-чека
строилась только из auto_approvals/auto_verdicts автопилота и совсем не
видела суждений стюарда — ни его эскалаций (для стюарда эскалация есть его
ОТКАЗ судить, и это тоже надо проверять), ни его применённых approve (самая
дорогая ошибка: решение уже изменило исход, а человек его не видел).

Три теста здесь бьют по трём кускам ограничения: применённый approve обязан
попадать чаще среднего (AC-1), тем же хешем — то есть без потери
воспроизводимости (AC-2), и результат спот-чека по такой задаче обязан дойти
до gate=audit в hub_practice_metrics (AC-3) так же, как он доходит для
решений автопилота.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.services.digest import deterministic_sample, generate_due_digests
from hub.services.orchestration import practice_metrics

# Стиль и вспомогательные функции — из tests/test_autopilot_digest.py (#739,
# #1143): дайджест читает то, что пишет настоящий записывающий путь
# (record_steward_judgement), а не самодельную строку в таблице событий.
from tests.test_autopilot_digest import (
    _autopilot_project,
    _node,
    _steward_judged_task,
    _steward_project,
    _tomorrow,
)


# День, к которому прижимаются события AC-1 (#1144 ревью, отчёт 202).
# Выборка — sha256(task_id:дата) % 10, и утверждения «эскалации есть» и
# «approve чаще» от даты ЗАВИСЯТ: перебор по календарю давал ~3.4% дней, в
# которые тест краснел без единой правки кода. Дата фиксируется, как в AC-2:
# зелёный прогон на случайном дне ничего не доказывал.
_FROZEN_DAY = "2026-09-01"
_FROZEN_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


async def _freeze_day(db: aiosqlite.Connection) -> None:
    """Все события этого теста — в один известный день.

    Дайджест читает события по created_at, а записываются они «сейчас».
    Чтобы дата выборки была константой, события переносятся в _FROZEN_DAY;
    хеш и порог при этом настоящие, подделан только календарь.
    """
    await db.execute("UPDATE events SET created_at = ?", (f"{_FROZEN_DAY} 12:00:00",))
    await db.commit()


async def _policy_verdict_task(
    db: aiosqlite.Connection, feature_id: int, title: str, verdict: str
) -> int:
    """Задача с вердиктом АВТОПИЛОТА — словарь у него свой.

    Автопилот пишет "approved" (с "d"), стюард — "approve". Две системы,
    два словаря, одно поле в разных событиях. Помощник существует ровно
    затем, чтобы это различие проверялось исполнением, а не жило в
    комментарии рядом с константой.
    """
    task_id = await _node(db, title=title, task_type="task", parent_id=feature_id)
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor="policy",
        payload={"verdict": verdict, "submission_generation": 1},
    )
    await db.commit()
    return task_id


async def _policy_escalated_task(
    db: aiosqlite.Connection, feature_id: int, title: str
) -> int:
    task_id = await _node(db, title=title, task_type="task", parent_id=feature_id)
    await repo.insert_event(
        db,
        kind="verdict_escalated",
        task_id=task_id,
        actor="policy",
        payload={"reason": "security-находка"},
    )
    await db.commit()
    return task_id


async def test_policy_approved_verdicts_are_oversampled_too(db: aiosqlite.Connection):
    """Применённый approve автопилота оверсэмплится наравне со стюардовым.

    Тест держит РАЗЛИЧИЕ СЛОВАРЕЙ, а не только общее правило. Константа
    "approved" описана комментарием как то, чью подмену «ничего не сломало
    бы явно, а просто тихо перестало бы ловить половину применённых
    approve» — ровно поэтому она обязана проверяться, иначе это обещание,
    а не гарантия: подмена написания на стюардово проходила все тесты.
    """
    _pid, feature = await _autopilot_project(db, "spot-check-policy")
    approved_ids = [
        await _policy_verdict_task(db, feature, f"approved {i}", "approved")
        for i in range(40)
    ]
    returned_ids = [
        await _policy_verdict_task(db, feature, f"changes {i}", "changes_requested")
        for i in range(40)
    ]

    await _freeze_day(db)
    assert await generate_due_digests(db, now=_FROZEN_NOW) == 1
    payload = json.loads((await repo.list_digests(db))[0]["payload"])
    assert payload["date"] == _FROZEN_DAY
    sample = set(payload["audit_sample"])

    picked_approved = sample & set(approved_ids)
    picked_returned = sample & set(returned_ids)

    assert picked_returned, "возвращённые вердикты тоже обязаны быть в пуле"
    assert len(picked_approved) > len(picked_returned), (
        "применённый approve автопилота обязан попадать чаще среднего: "
        f"{len(picked_approved)} против {len(picked_returned)}"
    )
    assert len(picked_approved) < len(approved_ids), "это выборка, а не «взять всё»"


async def test_policy_escalations_enter_the_pool(db: aiosqlite.Connection):
    """День из одних эскалаций автопилота всё равно даёт непустую выборку.

    Дыра «эскалации не попадают в выборку ВООБЩЕ» существовала для обеих
    систем, и закрыта она тоже для обеих. Проверяется днём, где эскалации —
    единственное, что произошло: иначе выборку наполнили бы соседние
    категории, и удаление эскалаций из пула осталось бы незаметным.
    """
    _pid, feature = await _autopilot_project(db, "spot-check-escalations")
    escalated_ids = [
        await _policy_escalated_task(db, feature, f"эскалация {i}") for i in range(20)
    ]

    assert await generate_due_digests(db, now=_tomorrow()) == 1
    payload = json.loads((await repo.list_digests(db))[0]["payload"])
    sample = set(payload["audit_sample"])

    assert sample, "выборка не может быть пустой в день, когда решения были"
    assert sample <= set(escalated_ids)


async def test_sample_oversamples_applied_approvals(db: aiosqlite.Connection):
    # AC-1 (#1144): день с решениями approve И эскалациями стюарда.
    _pid, feature = await _steward_project(db, "spot-check-mix")

    # 40 применённых approve — самая дорогая ошибка, если её никто не
    # проверит: решение уже изменило исход.
    approved_ids = [
        await _steward_judged_task(db, feature, f"approve {i}", verdict="approve")
        for i in range(40)
    ]
    # 40 эскалаций стюарда — его отказ судить. До этой правки они не
    # попадали в выборку вообще: "эскалация и так уйдёт человеку" было
    # верно для автопилота и неверно для стюарда — эскалация тоже нуждается
    # в проверке (осмысленно ли отказался, а не спрятался за "не уверен").
    escalated_ids = [
        await _steward_judged_task(db, feature, f"escalate {i}", verdict="escalate")
        for i in range(40)
    ]

    await _freeze_day(db)
    assert await generate_due_digests(db, now=_FROZEN_NOW) == 1
    payload = json.loads((await repo.list_digests(db))[0]["payload"])
    assert payload["date"] == _FROZEN_DAY
    sample = set(payload["audit_sample"])

    picked_approved = sample & set(approved_ids)
    picked_escalated = sample & set(escalated_ids)

    # Эскалации ПРЕДСТАВЛЕНЫ — дыра "эскалации не попадают в выборку вообще"
    # закрыта.
    assert picked_escalated, "эскалации стюарда обязаны попадать в выборку"

    # Применённый approve — чаще среднего: тот же хеш, порог втрое шире.
    assert len(picked_approved) > len(picked_escalated), (
        f"approve должен оверсэмплиться сильнее эскалаций: "
        f"{len(picked_approved)} approve против {len(picked_escalated)} escalate"
    )

    # НЕ вырождается в «всё подряд» — ни целиком, ни по approve-категории
    # отдельно. Порог для oversample обязан оставаться СТРОГО меньше "взять
    # всё": если бы порог стал равен модулю (100%), это уже не усиленный
    # надзор, а отмена делегирования approve.
    assert 0 < len(picked_approved) < len(approved_ids), (
        "oversample обязан быть выборкой, а не «взять все approve»"
    )
    assert len(sample) < len(approved_ids) + len(escalated_ids)


def test_oversample_stays_deterministic():
    # AC-2 (#1144): тот же набор решений и та же дата — тот же список
    # поимённо, при повторном расчёте. Oversample меняет ВЕРОЯТНОСТЬ
    # попадания (порог по модулю), а не воспроизводимость: это по-прежнему
    # один и тот же хеш sha256(task_id:дата), просто с другой границей
    # сравнения для другой категории — не второй проход и не random.
    task_ids = list(range(1, 201))
    oversample_ids = {tid for tid in task_ids if tid % 2 == 0}
    digest_date = "2026-09-01"

    first = deterministic_sample(task_ids, digest_date, oversample_ids=oversample_ids)
    second = deterministic_sample(task_ids, digest_date, oversample_ids=oversample_ids)
    assert first == second, "тот же вход обязан давать тот же список поимённо"

    # Инвариант «минимум один» и «не всё подряд» сохраняются и при oversample.
    assert first
    assert len(first) < len(task_ids)

    # Другая дата — законно другой список (это не про повтор, а про то, что
    # oversample не подменяет собой сам механизм хеширования по дате).
    other_day = deterministic_sample(
        task_ids, "2026-09-02", oversample_ids=oversample_ids
    )
    assert other_day == deterministic_sample(
        task_ids, "2026-09-02", oversample_ids=oversample_ids
    )


async def test_spot_check_lands_in_audit_gate(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-3 (#1144): человек закрывает спот-чек по задаче, попавшей в выборку
    # ИМЕННО из-за суждения стюарда (не автопилота) — круг record_audit_result
    # → events(kind=audit_result, actor=human) → _human_gate_metrics должен
    # замыкаться для стюарда так же, как он уже замыкается для автопилота.
    _pid, feature = await _steward_project(db, "spot-check-audit")
    task_id = await _steward_judged_task(
        db, feature, "применённый approve стюарда", verdict="approve"
    )

    assert await generate_due_digests(db, now=_tomorrow()) == 1
    digest = dict((await repo.list_digests(db))[0])
    sample = json.loads(digest["payload"])["audit_sample"]
    # Единственная задача дня — правило "минимум один" гарантирует, что она
    # в выборке, независимо от хеша: удобная опора для проверки именно
    # замыкания круга, а не самого oversample (это уже AC-1).
    assert sample == [task_id]

    resp = await client.post(
        f"/api/digests/{digest['id']}/audit",
        json={"task_id": task_id, "result": "problem", "comment": "стюард ошибся"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["audit_results"] == {str(task_id): "problem"}

    gates = (await practice_metrics(db))["human_gates"]
    audit_rows = [g for g in gates if g["gate"] == "audit"]
    assert audit_rows, (
        "решение стюарда, закрытое человеком, обязано попасть в gate=audit"
    )
    assert audit_rows[0]["overrides"] == 1, (
        "human_gates должен видеть 'problem' по суждению стюарда как override, "
        "а не терять его"
    )
