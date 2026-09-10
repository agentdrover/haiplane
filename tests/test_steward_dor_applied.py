"""Возврат драфта автору: сообщение, а не переход (#1161).

Проверяется не «умеет ли применить», а три правила, каждое против своей
ошибки: драфт остаётся драфтом (иначе цикл F6 обрывается молча),
замечания уходят перечнем (иначе автору нечего править), и круг имеет
потолок по РЕВИЗИИ постановки, а не по числу заходов.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import repository as repo
from hub.config import TokenIdentity
from hub.db import fetchall
from hub.models import StewardJudgementSubmit
from hub.services.finding_identity import finding_uids
from hub.services.statement_generation import (
    baseline_if_absent,
    bump_if_the_statement_changed,
)
from hub.services.steward_dor_applied import (
    ESCALATED_TO_HUMAN,
    EVENT_CEILING,
    EVENT_RETURNED,
    RETURNED,
    apply_dor_judgement,
)
from hub.services.steward_judgement import record_steward_judgement

#: Две настоящие претензии к постановке: у каждой своё имя, и автор
#: отвечает на них по отдельности.
TWO_REMARKS = [
    {
        "category": "ac_not_verifiable",
        "file": "",
        "locator": "none",
        "title": "AC-2 проверяется словом «корректно»",
        "why": "«корректно» нельзя ни выполнить, ни опровергнуть",
    },
    {
        "category": "locator_missing",
        "file": "",
        "locator": "none",
        "title": "у AC-3 нет test_ref",
        "why": "проверка объявлена тестом, но тест не назван",
    },
]


async def _draft(db: aiosqlite.Connection, *, title: str = "драфт на чтение") -> int:
    """Драфт, прошедший DoR, — ровно то, что забирает диспетчер (#1160)."""
    task_id = await repo.create_task(
        db,
        title=title,
        description="исходная постановка",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="draft",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, dor_passed=1)
    await db.commit()
    # Тот же вызов, которым диспетчер снимает базис перед заказом прогона:
    # ревизия 0 становится честной ревизией нынешнего текста.
    return task_id


async def _judge(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    *,
    verdict: str = "changes_requested",
    findings: list[dict] | None = None,
) -> None:
    """Суждение приходит контрактом #1022 — тем же путём, что у живого прогона."""
    await record_steward_judgement(
        db,
        task_id,
        StewardJudgementSubmit(
            generation=generation,
            kind="dor",
            verdict=verdict,
            confidence="high",
            escalate_reason="precondition_failed" if verdict == "escalate" else None,
            findings=TWO_REMARKS if findings is None else findings,
            model="gpt-5.3-codex",
        ),
        TokenIdentity("steward-bot", "steward", principal_id=42),
    )


async def _alerts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]


async def _events(db: aiosqlite.Connection, task_id: int, kind: str) -> list[dict]:
    rows = await fetchall(
        db, "SELECT payload FROM events WHERE task_id=? AND kind=?", (task_id, kind)
    )
    return [json.loads(dict(r)["payload"]) for r in rows]


async def test_weak_ac_returns_to_author(db: aiosqlite.Connection):
    """AC-1: драфт не двинулся, а автору ушли ВСЕ замечания поимённо.

    Оба поля читаются запросом после применения, а не обещаются: сброшенный
    ``dor_passed`` выкидывает драфт из выборки диспетчера, и цикл F6
    оборвался бы молча (риск задачи). Замечания сверяются по тексту каждой
    находки — «замечаний: 2» автору отвечать не на что.
    """
    task_id = await _draft(db)
    generation = await baseline_if_absent(db, task_id)
    before = dict(await repo.get_task(db, task_id))
    await _judge(db, task_id, generation)

    outcome, detail = await apply_dor_judgement(db, task_id, generation)

    assert outcome == RETURNED, detail
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "draft", "новых статусов у возвращённого драфта нет"
    assert task["dor_passed"] == before["dor_passed"], (
        "dor_passed не сбрасывается: иначе драфт выпадет из выборки диспетчера"
    )
    assert task["statement_generation"] == before["statement_generation"], (
        "возврат — это сообщение, а не правка постановки"
    )

    alerts = await _alerts(db, task_id)
    assert len(alerts) == 1, "поверхность одна с watchdog (#751), второй не заводится"
    for remark in TWO_REMARKS:
        assert remark["title"] in alerts[0], (
            f"замечание {remark['title']!r} до автора не доехало"
        )
        assert remark["why"] in alerts[0], "без «почему» замечание нечем закрыть"
    for uid in finding_uids(TWO_REMARKS):
        assert uid in alerts[0], (
            "замечание адресуется тем же finding_uid (#1007), которым его "
            "зовёт судья: иначе ответ автора не с чем сопоставить"
        )
    assert await _events(db, task_id, EVENT_RETURNED) == [
        {"generation": generation, "remarks": 2}
    ]


async def test_a_real_edit_earns_another_read(db: aiosqlite.Connection):
    """AC-2: постановку поправили — это снова возврат, а не эскалация.

    Потолок стоит на НЕИЗМЕНИВШЕЙСЯ ревизии, и правка её сдвигает. Ревизия
    двигается тем же путём, что у настоящего refine
    (``bump_if_the_statement_changed``), а не присваиванием числа: иначе
    тест проверял бы арифметику теста, а не признак «автор правил».
    """
    task_id = await _draft(db)
    first = await baseline_if_absent(db, task_id)
    await _judge(db, task_id, first)
    assert (await apply_dor_judgement(db, task_id, first))[0] == RETURNED

    await repo.update_task(db, task_id, description="постановка, переписанная автором")
    second = await bump_if_the_statement_changed(db, task_id)
    await db.commit()
    assert second != first, "предусловие: правка объявила новую ревизию"
    await _judge(db, task_id, second)

    outcome, detail = await apply_dor_judgement(db, task_id, second)

    assert outcome == RETURNED, detail
    assert dict(await repo.get_task(db, task_id))["status"] == "draft"
    assert len(await _alerts(db, task_id)) == 2, "второе чтение — второе замечание"
    assert await _events(db, task_id, EVENT_CEILING) == [], (
        "правка постановки потолком не считается"
    )


async def test_the_loop_has_a_ceiling(db: aiosqlite.Connection):
    """AC-3: та же ревизия во второй раз — к человеку, а не по кругу.

    «Стюард снова просит правок» на неизменившейся ревизии — это ровно
    повторное применение того же суждения: слот kind='dor' уникален по
    (задача, ревизия), поэтому второго суждения о том же тексте просто
    некуда лечь (#1156). Проверяется, что второй возврат не отправлен, а
    задача по-прежнему драфт: потолок — это смена адресата, а не переход.
    """
    task_id = await _draft(db)
    generation = await baseline_if_absent(db, task_id)
    await _judge(db, task_id, generation)
    assert (await apply_dor_judgement(db, task_id, generation))[0] == RETURNED

    outcome, detail = await apply_dor_judgement(db, task_id, generation)

    assert outcome == ESCALATED_TO_HUMAN, detail
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "draft", "человек читает драфт, а не новый статус"
    assert task["dor_passed"] == 1
    assert await _events(db, task_id, EVENT_RETURNED) == [
        {"generation": generation, "remarks": 2}
    ], "третьего замечания тем же текстом автору не уходит"
    ceiling = await _events(db, task_id, EVENT_CEILING)
    assert ceiling == [{"generation": generation, "reason": "statement_unchanged"}]
    last = (await _alerts(db, task_id))[-1]
    assert "hub_approve_task" in last and "hub_reject_task" in last, (
        "человеку называются решения, которые у него есть"
    )
    for remark in TWO_REMARKS:
        assert remark["title"] in last, "человек читает те же замечания, что и автор"


async def test_the_ceiling_speaks_once(db: aiosqlite.Connection):
    """Третий заход на ту же ревизию не пишет человеку второй раз.

    Второй алерт тем же текстом выглядел бы новым событием, которого не
    было, — та же причина, по которой не дублируется эскалация по бюджету
    (#1149).
    """
    task_id = await _draft(db)
    generation = await baseline_if_absent(db, task_id)
    await _judge(db, task_id, generation)
    await apply_dor_judgement(db, task_id, generation)
    await apply_dor_judgement(db, task_id, generation)
    before = len(await _alerts(db, task_id))

    with pytest.raises(HTTPException) as refusal:
        await apply_dor_judgement(db, task_id, generation)

    assert refusal.value.status_code == 409
    assert len(await _alerts(db, task_id)) == before


async def test_a_return_without_remarks_is_refused(db: aiosqlite.Connection):
    """«Постановку надо доработать» — это не замечание, а отказ без содержания.

    Проверяются оба вида пустоты: находок нет вовсе и находка есть, но в
    ней нет ни заголовка, ни объяснения. Второй случай важнее: он проходит
    любую проверку «список непустой» и до автора доезжает строкой, на
    которую нечего ответить.
    """
    for findings in ([], [{"category": "unclear", "file": "", "locator": "none"}]):
        task_id = await _draft(db, title=f"пустой возврат {len(findings)}")
        generation = await baseline_if_absent(db, task_id)
        await _judge(db, task_id, generation, findings=findings)

        with pytest.raises(HTTPException) as refusal:
            await apply_dor_judgement(db, task_id, generation)

        assert refusal.value.status_code == 409
        assert await _alerts(db, task_id) == [], "драфт остался нетронутым"


async def test_a_finding_without_words_still_gets_a_line(db: aiosqlite.Connection):
    """Бессодержательная находка рядом с настоящей не исчезает молча.

    Выброшенное замечание автор не отличит от замечания, которого не было,
    а число в шапке разошлось бы со списком под ней.
    """
    task_id = await _draft(db)
    generation = await baseline_if_absent(db, task_id)
    await _judge(
        db,
        task_id,
        generation,
        findings=[
            TWO_REMARKS[0],
            {"category": "unclear", "file": "", "locator": "none"},
        ],
    )

    await apply_dor_judgement(db, task_id, generation)

    alert = (await _alerts(db, task_id))[0]
    assert "Замечания (2)" in alert, "число в шапке считает то же, что список"
    assert alert.count("\n- ") == 2, "строк ровно столько, сколько находок"
    assert "судья не оставил текста" in alert


async def test_an_old_revision_is_not_applied_to_the_live_text(
    db: aiosqlite.Connection,
):
    """Суждение о прошлой редакции не применяется к нынешней.

    И это же отделяет «ревизия не менялась» от «суждение опоздало»: без
    проверки поздний прогон уходил бы в потолок, и драфт попадал бы к
    человеку из-за расторопности автора, а не из-за круга.
    """
    task_id = await _draft(db)
    stale = await baseline_if_absent(db, task_id)
    await _judge(db, task_id, stale)
    await repo.update_task(db, task_id, description="автор переписал, пока судья читал")
    await bump_if_the_statement_changed(db, task_id)
    await db.commit()

    with pytest.raises(HTTPException) as refusal:
        await apply_dor_judgement(db, task_id, stale)

    assert refusal.value.status_code == 409
    assert await _alerts(db, task_id) == []


async def test_approve_belongs_to_the_gatekeeper(db: aiosqlite.Connection):
    """Одобрение драфта здесь не применяется — и отклоняется громко.

    «Применил одобрение» и «ничего не сделал» вызывающий обязан различать:
    молчаливый пропуск выглядел бы снятым гейтом DoR, которого никто не
    снимал.
    """
    task_id = await _draft(db)
    generation = await baseline_if_absent(db, task_id)
    await _judge(db, task_id, generation, verdict="approve", findings=[])

    with pytest.raises(HTTPException) as refusal:
        await apply_dor_judgement(db, task_id, generation)

    assert refusal.value.status_code == 409
    assert "привратник" in str(refusal.value.detail), (
        "отказ называет, ЧЬЁ это дело: «неизвестный вердикт» отправил бы "
        "читателя искать опечатку в словаре"
    )
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "draft", "гейт DoR этим модулем не снимается"


async def test_a_task_that_left_the_draft_is_not_returned(db: aiosqlite.Connection):
    """Человек успел одобрить драфт, пока прогон думал.

    Замечание по постановке задачи, которую уже взяли в работу, автору
    нечем ответить: правится постановка только у драфта.
    """
    task_id = await _draft(db)
    generation = await baseline_if_absent(db, task_id)
    await _judge(db, task_id, generation)
    await repo.update_task(db, task_id, status="open")
    await db.commit()

    with pytest.raises(HTTPException) as refusal:
        await apply_dor_judgement(db, task_id, generation)

    assert refusal.value.status_code == 409
    assert await _alerts(db, task_id) == []


# --- Находки машинного ревью #330 ------------------------------------------


async def test_the_return_is_decided_under_a_write_lock(
    db: aiosqlite.Connection, monkeypatch
):
    """Находка 8fcfcf7912b84697: проверил — записал, без лока на запись."""
    from hub.services import steward_dor_applied as mod

    task_id = await _draft(db)
    generation = await baseline_if_absent(db, task_id)
    await _judge(db, task_id, generation)
    await db.commit()
    assert not db.in_transaction, "предусловие: соединение чистое"

    at_read: list[bool] = []
    at_write: list[bool] = []
    real_seen = mod._seen
    real_event = repo.insert_event

    async def _watch_read(conn, tid, gen, kind):
        at_read.append(bool(conn.in_transaction))
        return await real_seen(conn, tid, gen, kind)

    async def _watch_write(conn, **kw):
        at_write.append(bool(conn.in_transaction))
        return await real_event(conn, **kw)

    monkeypatch.setattr(mod, "_seen", _watch_read, raising=True)
    monkeypatch.setattr(mod.repo, "insert_event", _watch_write, raising=True)

    await apply_dor_judgement(db, task_id, generation)

    assert at_read and all(at_read), "проверка «уже возвращали?» читается вне лока"
    assert at_write and all(at_write)


async def test_a_finding_explains_itself_from_detail(db: aiosqlite.Connection):
    """Находка e2db58901a8aaa20: объяснение читается только из why."""
    task_id = await _draft(db)
    generation = await baseline_if_absent(db, task_id)
    await _judge(
        db,
        task_id,
        generation,
        findings=[
            {
                "category": "ac_not_verifiable",
                "file": "",
                "locator": "none",
                "title": "AC-2 проверяется словом «корректно»",
                "detail": "«корректно» нельзя ни выполнить, ни опровергнуть",
            }
        ],
    )

    await apply_dor_judgement(db, task_id, generation)

    alert = (await _alerts(db, task_id))[0]
    assert "«корректно» нельзя ни выполнить, ни опровергнуть" in alert


async def test_a_finding_with_only_detail_is_not_empty(db: aiosqlite.Connection):
    """Та же находка с другой стороны: находка только с detail — не пустая."""
    task_id = await _draft(db, title="только detail")
    generation = await baseline_if_absent(db, task_id)
    await _judge(
        db,
        task_id,
        generation,
        findings=[
            {
                "category": "ac_not_verifiable",
                "file": "",
                "locator": "none",
                "detail": "AC-1 не проверяется ничем",
            }
        ],
    )

    outcome, _ = await apply_dor_judgement(db, task_id, generation)

    assert outcome == RETURNED
    assert "AC-1 не проверяется ничем" in (await _alerts(db, task_id))[0]


async def test_unreadable_findings_are_not_called_an_empty_list(
    db: aiosqlite.Connection,
):
    """Находка 552e3b7085c66dc9: битый JSON выдаётся за «замечаний не было»."""
    task_id = await _draft(db)
    generation = await baseline_if_absent(db, task_id)
    await _judge(db, task_id, generation)
    await db.execute(
        "UPDATE steward_judgements SET findings=? WHERE task_id=?",
        ("{не json", task_id),
    )
    await db.commit()

    with pytest.raises(HTTPException) as refusal:
        await apply_dor_judgement(db, task_id, generation)

    assert refusal.value.status_code == 409
    detail = str(refusal.value.detail)
    assert "без единого замечания" not in detail, (
        "нечитаемые находки — не «замечаний не было»"
    )
    assert "JSON" in detail or "прочитать" in detail
    assert await _alerts(db, task_id) == []


async def test_a_half_written_return_leaves_no_alert(
    db: aiosqlite.Connection, monkeypatch
):
    """Возврат ложится целиком или не ложится вовсе.

    Обратная сторона того же лока: алерт пишется раньше события, и без
    общей транзакции упавшая запись события оставила бы автору замечания,
    о которых хаб не помнит. Следующее применение сочло бы такой возврат
    первым — потолок сдвинулся бы на заход вперёд.
    """
    from hub.services import steward_dor_applied as mod

    task_id = await _draft(db)
    generation = await baseline_if_absent(db, task_id)
    await _judge(db, task_id, generation)
    await db.commit()

    async def _falls(conn, **kw):
        raise RuntimeError("запись события не прошла")

    monkeypatch.setattr(mod.repo, "insert_event", _falls, raising=True)

    with pytest.raises(RuntimeError):
        await apply_dor_judgement(db, task_id, generation)

    assert await _alerts(db, task_id) == [], (
        "замечания без события — возврат, которого хаб не помнит"
    )
