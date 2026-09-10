"""Отчёт о готовности умеет отвечать про исходы находок (#1155).

Гейт #911 спрашивает автора, что стало с находками, из-за которых работу
вернули. На pair-сдаче ответ приезжал полем ``finding_outcomes``; у отчёта о
готовности такого поля не было вовсе, и #1122 объявил гейт на этом пути
неприменимым: вопрос задать можно, ответить нечем.

Здесь пиньтся то, что делает ответ возможным и не даёт ему стать чем-то
другим: поле принимают все три поверхности, старый отчёт без него не изменился,
запись идёт тем же кодом и в то поколение, которому отвечает, — и авторский
отчёт НЕ становится суждением о находке.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.services import finding_outcome, lifecycle


def _finding(title: str, **over: Any) -> dict[str, Any]:
    base = {
        "title": title,
        "severity": "high",
        "category": "correctness",
        "locator": "file",
        "file": "hub/db.py",
    }
    base.update(over)
    return base


@pytest.fixture
def quiet_git_ops(monkeypatch):
    """Хвост доставки на отчёте о готовности — заглушкой.

    Тест здесь про исходы находок, а не про git: настоящие адаптеры полезли бы
    в клон, и падение стало бы неотличимым от дефекта поля. Подменяются
    ИМЕНОВАННЫЕ методы настоящего адаптера, а не объект целиком: подмена
    целиком возвращает мок на любое имя, и такой мок однажды уже уехал в базу
    как значение колонки.
    """
    from hub.integrations.registry import plugins

    for name, value in (
        ("pair_prepare_branch", "task-x/pair"),
        ("pair_prepare_worktree", "task-x/pair"),
        ("checkout", True),
        ("dirty_paths", []),
        ("auto_commit", True),
        ("squash_branch", True),
        ("push_branch", True),
        ("create_pr", None),
        ("branch_tip", ""),
        ("changed_paths", []),
    ):
        if hasattr(plugins.git_ops, name):
            monkeypatch.setattr(
                plugins.git_ops, name, AsyncMock(return_value=value), raising=False
            )
    monkeypatch.setattr(
        plugins.dispatch, "submit_task", AsyncMock(return_value={}), raising=False
    )
    yield plugins.git_ops


async def _sent_back_with_a_finding(
    client: AsyncClient,
    db: aiosqlite.Connection,
    title: str,
    findings: tuple[str, ...] = ("утечка курсора",),
) -> tuple[int, int, str]:
    """Задача, которую вернули с ПОДТВЕРЖДЁННЫМИ находками первой сдачи.

    Возвращает ``(task_id, review_id, finding_uid)`` — uid ПЕРВОЙ находки.
    Остальные читаются вызывающим через ``open_findings``. Поколение сдачи,
    на которую ответят исходы, — 1.
    """
    resp = await client.post("/api/tasks", json={"title": title})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: работать"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        harness_skill="lite-diff-review",
        raw_count=len(findings),
        findings_confirmed=json.dumps(
            [_finding(t) for t in findings], ensure_ascii=False
        ),
        unresolved=json.dumps([], ensure_ascii=False),
        incomplete=False,
    )
    await db.commit()
    review_id = int(dict(await repo.get_latest_machine_review(db, task_id))["id"])
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )
    uid = (await finding_outcome.open_findings(db, task_id, 1))[0]["finding_uid"]
    return task_id, review_id, uid


async def test_a_done_report_can_answer_findings(
    db: aiosqlite.Connection, client: AsyncClient, quiet_git_ops
):
    """AC-1: исход приезжает отчётом о готовности и ложится в СВОЁ поколение.

    Поколение здесь несущее: отчёт о готовности сам бампает счётчик, и запись
    ответа в поколение, которое начинается этим отчётом, значила бы ответ не на
    тот отчёт. Проверяется числом, а не фактом записи.
    """
    task_id, review_id, uid = await _sent_back_with_a_finding(
        client, db, "Ответ отчётом"
    )

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={
            "agent": "dev",
            "kind": "done",
            "content": "починил, сдаю",
            "finding_outcomes": [{"finding_uid": uid, "outcome": "fixed"}],
        },
    )
    assert resp.status_code == 200, resp.text

    stored = [dict(r) for r in await repo.list_finding_outcomes(db, review_id)]
    assert [r["outcome"] for r in stored] == ["fixed"], (
        "исход отчёта о готовности не записан вовсе"
    )
    assert stored[0]["finding_uid"] == uid
    assert stored[0]["submission_generation"] == 1, (
        "исход обязан принадлежать поколению, на которое ОТВЕЧАЕТ, а не тому, "
        f"что начинается этим отчётом: записано {stored[0]['submission_generation']}"
    )
    row = dict(await repo.get_task(db, task_id))
    assert row["submission_generation"] == 2, (
        "отчёт о готовности всё так же бампает поколение — запись исходов идёт ДО"
    )


async def test_the_authors_account_on_done_is_not_a_judgement(
    db: aiosqlite.Connection, client: AsyncClient, quiet_git_ops
):
    """Отчёт автора об исходе НЕ попадает в число разобранных находок.

    ``finding_dispositions`` читает precision без фильтра по тому, кто решал:
    собственное «исправлено», записанное туда, считалось бы подтверждением
    находки человеком, и метрика начала бы мерить мнение автора о своей работе.
    На pair-пути это уже закреплено (#876); второй путь не должен открывать ту
    же дверь с другой стороны.
    """
    task_id, review_id, uid = await _sent_back_with_a_finding(
        client, db, "Отчёт не суждение"
    )

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={
            "agent": "dev",
            "kind": "done",
            "content": "готово",
            "finding_outcomes": [{"finding_uid": uid, "outcome": "fixed"}],
        },
    )
    assert resp.status_code == 200, resp.text
    # Сначала — что исход ВООБЩЕ записан: иначе «в диспозициях пусто» верно и
    # тогда, когда не записано ничего никуда, и тест зелен при снятом поле.
    assert [dict(r)["outcome"] for r in await repo.list_finding_outcomes(db, review_id)]

    assert await repo.list_finding_dispositions(db, review_id) == []
    rows = await repo.fetchall(db, "SELECT COUNT(*) AS n FROM finding_dispositions")
    assert int(dict(rows[0])["n"]) == 0, (
        "самоотчёт автора не является суждением о находке (#876)"
    )


async def test_a_done_report_without_outcomes_is_unchanged(
    db: aiosqlite.Connection, client: AsyncClient, quiet_git_ops
):
    """AC-2: старый вызов работает без правки — поле необязательное.

    Отчёт о готовности зовётся чаще любого другого инструмента: обязательное
    поле остановило бы всех агентов в день выкладки.
    """
    task_id, review_id, _uid = await _sent_back_with_a_finding(client, db, "Как раньше")

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "готово"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "done" and body["content"] == "готово"
    assert await repo.list_finding_outcomes(db, review_id) == [], (
        "молчание автора не должно превращаться в исход"
    )
    row = dict(await repo.get_task(db, task_id))
    assert row["submission_generation"] == 2, "отчёт прошёл ровно как прежде"


async def test_an_unanswered_finding_is_named_but_never_refused_on_done(
    db: aiosqlite.Connection, client: AsyncClient, monkeypatch, quiet_git_ops
):
    """Потолок warn: путь называет незакрытые находки и НЕ отказывает.

    Отказ здесь оставил бы задачу стоять без человека рядом — решение
    владельца по всем гейтам этого пути (#1122). При этом молчание гейта,
    который отработал, читается как «проверка прошла чисто», поэтому находка
    названа в ленте.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id, _review_id, _uid = await _sent_back_with_a_finding(
        client, db, "Warn, не отказ"
    )

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "готово"},
    )

    assert resp.status_code == 200, resp.text
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "утечка курсора" in feed, (
        "гейт отработал и промолчал — читатель решит, что находок нет"
    )


async def _pending_report_after_a_dispatch_run(
    db: aiosqlite.Connection, task_id: int
) -> None:
    """Как задача попадает в ``pending_report``: прогон кончился без отчёта.

    Поллер видит завершённый job и НЕ видит done-отчёта в ленте
    (``poller.py`` #1018) — ``transition_after_agent_done(has_done=False)``
    кладёт задачу в ``pending_report``. Отчёт присылается уже оттуда, и это
    ЕДИНСТВЕННЫЙ статус, из которого диспетчерская задача (с ``job_id``)
    вообще может его прислать: ``_validate_done_report`` пропускает
    ``running``/``claimed`` только БЕЗ ``job_id``.
    """
    await repo.update_task(db, task_id, status="pending_report", job_id="job-1")
    await db.commit()


async def test_the_pending_report_route_names_unanswered_findings(
    db: aiosqlite.Connection, client: AsyncClient, monkeypatch, quiet_git_ops
):
    """Маршрут pending_report обязан назвать находки, на которые не ответили.

    Воспроизведено до починки: задача в ``pending_report`` с открытой находкой
    поколения 1 уезжала в review на поколении 2, и в ленте не было ни слова.
    Молчание здесь дороже, чем на других маршрутах: гейт после бампа
    спрашивает уже о поколении 2, где отчётов ревью нет вовсе, — находка
    поколения 1 выпадает из цикла НАВСЕГДА, вопрос исчезает вместе с ответом.

    Конвейер headless-гейтов сюда не доезжает: обе ветки уходят из
    ``pending_report`` сами, не зовя ``transition_after_agent_done``.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id, _review_id, _uid = await _sent_back_with_a_finding(
        client, db, "Молчаливый маршрут"
    )
    await _pending_report_after_a_dispatch_run(db, task_id)

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "готово"},
    )

    assert resp.status_code == 200, resp.text
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "утечка курсора" in feed, (
        "маршрут pending_report промолчал о неотвеченной находке — а спросить "
        "о ней после бампа поколения больше негде"
    )
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "review", "потолок warn: назвать, но не отказать"
    assert row["submission_generation"] == 2
    # Назвать находку — не значит ответить на неё и тем более не значит
    # рассудить её. Заметка, которая по дороге завела бы исход или диспозицию,
    # закрыла бы вопрос от имени того, кто на него не отвечал.
    assert await repo.list_finding_outcomes(db, _review_id) == [], (
        "заметка сама записала исход — молчание автора превратилось в ответ"
    )
    assert await repo.list_finding_dispositions(db, _review_id) == [], (
        "заметка стала суждением о находке (#876)"
    )


async def test_the_pending_report_route_names_only_what_is_left_unanswered(
    db: aiosqlite.Connection, client: AsyncClient, monkeypatch, quiet_git_ops
):
    """Названо то, на что ответа НЕТ, — а не всё подряд.

    Заметка, называющая уже закрытую находку, была бы тем же шумом, что и
    молчание: читатель перестаёт её читать. Проверяется парой — один исход
    прислан и записан, второй нет.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id, review_id, first_uid = await _sent_back_with_a_finding(
        client, db, "Половина ответа", findings=("утечка курсора", "вторая находка")
    )
    await _pending_report_after_a_dispatch_run(db, task_id)

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={
            "agent": "dev",
            "kind": "done",
            "content": "часть починил",
            "finding_outcomes": [{"finding_uid": first_uid, "outcome": "fixed"}],
        },
    )

    assert resp.status_code == 200, resp.text
    assert [
        dict(r)["outcome"] for r in await repo.list_finding_outcomes(db, review_id)
    ] == ["fixed"], (
        "присланный исход не записан — тогда «названо только второе» ни о чём"
    )
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "вторая находка" in feed, "остаток без ответа не назван"
    assert "утечка курсора" not in feed, (
        "названа находка, на которую автор ответил — заметка стала шумом"
    )


async def test_the_pending_report_route_names_them_even_when_it_completes(
    db: aiosqlite.Connection, client: AsyncClient, monkeypatch, quiet_git_ops
):
    """Ветка «завершить без ревью» — та же обязанность, и она хуже.

    При ``auto_review=false`` отчёт из ``pending_report`` закрывает задачу
    сразу. Незакрытая находка уезжает не в следующую сдачу, а в completed:
    сказать о ней здесь — последний момент, когда это ещё кому-то видно.
    Поэтому заметка стоит ДО развилки, а не в ветке review.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id, _review_id, _uid = await _sent_back_with_a_finding(
        client, db, "Закрыть молча"
    )
    await repo.update_task(db, task_id, auto_review=0)
    await _pending_report_after_a_dispatch_run(db, task_id)

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "готово"},
    )

    assert resp.status_code == 200, resp.text
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "completed", (
        "тест обязан идти ИМЕННО веткой завершения, иначе он повторяет соседа"
    )
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "утечка курсора" in feed, (
        "задача закрыта, а незакрытая находка не названа ни разу"
    )


async def _a_subtask_sent_back_with_a_finding(
    client: AsyncClient,
    db: aiosqlite.Connection,
    findings: tuple[str, ...] = ("утечка курсора",),
) -> tuple[int, int, str]:
    """Сабтаск, вернувшийся с подтверждённой находкой, — без opt-out руками.

    Ревью назвало достижимость этого пути НЕсогласованной: путь в коде есть,
    но «а как туда попасть без искусственного ``auto_review=False``?».
    Отсюда и сабтаск, а не ``update_task(auto_review=0)``: ноль ставит сам
    продукт — ``create_subtasks_bulk`` в ``hub/services/lifecycle.py``
    принудительно гасит ``auto_review`` для ``task_type=subtask``. Запрета на
    ``pair-start``/``submit-review`` у сабтаска нет, и весь путь пройден
    настоящим транспортом (REST), а не вызовами функций.
    """
    parent = (await client.post("/api/tasks", json={"title": "Родитель"})).json()["id"]
    resp = await client.post(
        f"/api/tasks/{parent}/subtasks",
        json={
            "task_type": "subtask",
            "source": "human",
            "items": [{"title": "Сабтаск с находкой"}],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    task_id = int(resp.json()[0]["id"])
    assert dict(await repo.get_task(db, task_id))["auto_review"] == 0, (
        "продукт больше НЕ гасит auto_review у сабтаска — тогда этот тест "
        "проверяет выдуманную конфигурацию, а не достижимый путь"
    )

    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: работать"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        harness_skill="lite-diff-review",
        raw_count=len(findings),
        findings_confirmed=json.dumps(
            [_finding(t) for t in findings], ensure_ascii=False
        ),
        unresolved=json.dumps([], ensure_ascii=False),
        incomplete=False,
    )
    await db.commit()
    review_id = int(dict(await repo.get_latest_machine_review(db, task_id))["id"])
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )
    uid = (await finding_outcome.open_findings(db, task_id, 1))[0]["finding_uid"]
    return task_id, review_id, uid


async def test_the_pair_done_route_names_unanswered_findings_when_it_completes(
    db: aiosqlite.Connection, client: AsyncClient, monkeypatch, quiet_git_ops
):
    """Последний маршрут, до которого конвейер гейтов не доезжает (#1155).

    ``transition_after_agent_done`` при ``auto_review=false`` уходит в
    ``_complete_without_review`` ВЫШЕ вызова гейтов сдачи: шаг исходов на
    этой ветке не работал никогда. Воспроизведено зондом до починки — путь
    open → pair-start → submit-review → CHANGES_REQUESTED с подтверждённой
    находкой → done уносил задачу в ``completed``, и в ленте не было ни
    слова про находку.

    Хуже, чем на ``pending_report``: там задача остаётся жива и вопрос ещё
    можно задать, здесь он закрывается вместе с задачей. Потолок тот же —
    warn: назвать, но не отказать.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id, review_id, _uid = await _a_subtask_sent_back_with_a_finding(client, db)
    assert dict(await repo.get_task(db, task_id))["status"] == "running", (
        "путь обязан идти веткой pair-running, иначе тест повторяет соседа"
    )

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "готово"},
    )

    assert resp.status_code == 200, resp.text
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "completed", (
        "тест обязан идти ИМЕННО веткой завершения без ревью"
    )
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "утечка курсора" in feed, (
        "задача закрыта, а незакрытая находка не названа ни разу — спросить "
        "о ней после completed уже негде"
    )
    # Назвать находку — не значит ответить на неё и тем более не значит
    # рассудить её: авторский отчёт об исходе НЕ диспозиция.
    assert await repo.list_finding_outcomes(db, review_id) == [], (
        "заметка сама записала исход — молчание автора стало ответом"
    )
    assert await repo.list_finding_dispositions(db, review_id) == [], (
        "заметка стала суждением о находке (#876)"
    )


async def test_the_pair_done_route_names_only_what_is_left_unanswered(
    db: aiosqlite.Connection, client: AsyncClient, monkeypatch, quiet_git_ops
):
    """И на этом маршруте названо то, на что ответа НЕТ, — а не всё подряд.

    Исходы, приехавшие с отчётом, пишутся ДО развилки маршрутов, поэтому
    заметка обязана вычесть уже отвеченное. Заметка, называющая закрытую
    находку, была бы тем же шумом, что и молчание.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id, review_id, first_uid = await _a_subtask_sent_back_with_a_finding(
        client, db, findings=("утечка курсора", "вторая находка")
    )

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={
            "agent": "dev",
            "kind": "done",
            "content": "часть починил",
            "finding_outcomes": [{"finding_uid": first_uid, "outcome": "fixed"}],
        },
    )

    assert resp.status_code == 200, resp.text
    assert [
        dict(r)["outcome"] for r in await repo.list_finding_outcomes(db, review_id)
    ] == ["fixed"], (
        "присланный исход не записан — тогда «названо только второе» ни о чём"
    )
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "вторая находка" in feed, "остаток без ответа не назван"
    assert "утечка курсора" not in feed, (
        "названа находка, на которую автор ответил — заметка стала шумом"
    )


async def test_the_deprecated_alias_delivers_the_answer_through_its_published_door(
    quiet_git_ops,
):
    """Депрекированный вход не имеет права ТИХО потерять ответ автора (#1155).

    Ревью назвало это неразрешённой находкой. Предыдущая проверка объявила её
    ложной, потому что смотрела на ФУНКЦИЮ: вызов ``.fn`` с лишним аргументом
    падает TypeError, и это выглядело как запертая дверь. Опубликованный вход —
    не функция, а транспорт: FastMCP валидирует аргументы по СХЕМЕ инструмента
    и молча выбрасывает всё, чего в схеме нет. Воспроизведено зондом через
    ``mcp.call_tool`` до починки: тело запроса уезжало как
    ``{agent, kind, content}``, исходы исчезали, автор получал успех — и после
    бампа поколения спросить о находке было уже негде (тот же механизм, что на
    маршруте pending_report выше).

    Поэтому тест ходит ИМЕННО через ``mcp.call_tool``. Через ``.fn`` он был бы
    зелёным и с выброшенным полем, то есть проверял бы не ту дверь. ADR-0002
    держит алиас на этапе 1 — предупреждение и телеметрия, — а «депрекирован»
    не значит «может терять данные».
    """
    from hub import mcp_server

    tools = await mcp_server.mcp.list_tools()
    schema = next(t for t in tools if t.name == "hub_task_update").inputSchema
    assert "finding_outcomes" in schema["properties"], (
        "поля нет в ОПУБЛИКОВАННОЙ схеме — транспорт выбросит аргумент молча, "
        "и никакая проверка внутри функции этого не увидит"
    )

    outcomes = [{"finding_uid": "0" * 16, "outcome": "fixed"}]
    with (
        patch.object(mcp_server, "_api_post", new_callable=AsyncMock) as post,
        patch.object(mcp_server, "_api_get", new_callable=AsyncMock) as get,
    ):
        post.return_value = {"id": 1}
        get.return_value = {"status": "review"}
        await mcp_server.mcp.call_tool(
            "hub_task_update",
            {
                "task_id": 7,
                "content": "готово",
                "agent": "dev",
                "kind": "done",
                "finding_outcomes": outcomes,
            },
        )

    bodies = [
        c.args[1] for c in post.await_args_list if str(c.args[0]).endswith("/updates")
    ]
    assert bodies, "алиас не отправил отчёт вовсе"
    assert bodies[0].get("finding_outcomes") == outcomes, (
        "ответ автора не доехал до запроса — ровно та тихая потеря, которой "
        f"быть не должно: тело {bodies[0]}"
    )


async def test_the_done_path_gate_is_active_and_explains_nothing_away():
    """AC-5: причина неактивности снята вместе с включением шага.

    Старая причина ссылалась на эту задачу («поле заводится задачей #1155») и
    после её закрытия врала бы. Проверяется, что шаг ИМЕННО включён, а не что
    у неактивных шагов есть причина: второе верно и при выключенном шаге.
    """
    step = next(s for s in lifecycle.HEADLESS_STEPS if s.name == "finding_outcomes")
    assert step.active, "гейт исходов на пути отчёта о готовности обязан выполняться"
    assert not step.inactive_reason
    assert step.mode() in ("off", "warn"), (
        f"шаг может отказать на этом пути: режим {step.mode()}"
    )


async def test_the_warn_cap_reaches_the_outcomes_step(monkeypatch):
    """Потолок обязан ДОХОДИТЬ до шага, а не только стоять в списке.

    Шаг читал ``config.FINDING_OUTCOME`` сам — ровно тот дефект, который уже
    ловили у поверхностей: потолок объявлен, а require поднимает 422.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    state = lifecycle.SubmitContext(
        db=None,  # type: ignore[arg-type]
        task_id=1,
        task={"submission_generation": 0},
    )
    state.gate_mode = "warn"

    with patch.object(
        finding_outcome,
        "open_findings",
        AsyncMock(
            return_value=[{"finding_uid": "u", "title": "т", "severity": "high"}]
        ),
    ):
        await lifecycle._step_finding_outcomes(state)

    # Отказа не было — иначе сюда бы не дошли, — и заметка режима warn на месте.
    assert state.outcome_note, "warn обязан оставить заметку вместо отказа"
    assert "warn" in state.outcome_note


async def test_outcomes_are_refused_outside_a_done_report(client: AsyncClient):
    """Ответ, которому некуда лечь, отклоняется вслух, а не теряется молча."""
    resp = await client.post("/api/tasks", json={"title": "Не done"})
    task_id = resp.json()["id"]

    bad = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={
            "agent": "dev",
            "kind": "status",
            "content": "просто запись",
            "finding_outcomes": [{"finding_uid": "0" * 16, "outcome": "fixed"}],
        },
    )

    assert bad.status_code == 422, bad.text
    assert "kind='done'" in bad.text


async def test_every_surface_accepts_outcomes_on_done(
    db: aiosqlite.Connection, client: AsyncClient, quiet_git_ops
):
    """AC-3: REST, MCP и CLI принимают исходы при отчёте о готовности.

    Отставшая поверхность краснит ЭТОТ тест со своим именем: одно поле одного
    контракта, опубликованное три раза, — самая плотная семья подтверждённых
    находок ревью (#810, #819, #833), и предупреждение surface_parity про неё
    приходит после пропуска, а не вместо него.
    """
    import argparse

    from hub import cli, mcp_server

    lagging: list[str] = []

    # REST — единственная поверхность, которую можно проверить записью в базу:
    # остальные две ходят в неё же по HTTP.
    task_id, review_id, uid = await _sent_back_with_a_finding(client, db, "Три двери")
    rest = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={
            "agent": "dev",
            "kind": "done",
            "content": "готово",
            "finding_outcomes": [{"finding_uid": uid, "outcome": "fixed"}],
        },
    )
    if rest.status_code != 200 or not await repo.list_finding_outcomes(db, review_id):
        lagging.append(f"REST POST /api/tasks/{{id}}/updates: {rest.status_code}")

    # MCP — поле доезжает до тела запроса, а не только до сигнатуры.
    with (
        patch.object(mcp_server, "_api_post", new_callable=AsyncMock) as mcp_post,
        patch.object(mcp_server, "_api_get", new_callable=AsyncMock) as mcp_get,
    ):
        mcp_post.return_value = {"id": 1}
        mcp_get.return_value = {"status": "review"}
        await mcp_server.hub_report_done(
            7,
            "готово",
            agent="dev",
            finding_outcomes=[{"finding_uid": uid, "outcome": "fixed"}],
        )
    sent = mcp_post.await_args.args[1]
    if sent.get("finding_outcomes") != [{"finding_uid": uid, "outcome": "fixed"}]:
        lagging.append(f"MCP hub_report_done: тело без исходов — {sent}")

    # CLI — тот же ключ, что у submit-review.
    with patch.object(cli, "_api", MagicMock(return_value={})) as cli_api:
        rc = cli.cmd_update(
            argparse.Namespace(
                task_id=7,
                agent="dev",
                kind="done",
                message="готово",
                finding_outcomes=json.dumps([{"finding_uid": uid, "outcome": "fixed"}]),
            )
        )
    cli_body = cli_api.call_args.args[2] if cli_api.call_args else {}
    if rc != 0 or "finding_outcomes" not in cli_body:
        lagging.append(f"CLI update --finding-outcomes: rc={rc}, тело {cli_body}")

    assert not lagging, "поверхности отстали: " + "; ".join(lagging)
