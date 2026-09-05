"""Находка ревью заканчивается исходом, а не тишиной (#911).

Измерено до этого гейта: 47 подтверждённых находок за семь дней и ноль
суждений. Находка, на которую никто не ответил, не становится неверной — она
становится невидимой, и названный ею дефект уезжает в прод. Заодно исчезают
оба числа, по которым видно, окупается ли машинное ревью: без знаменателя не
считаются ни precision, ни цена исправленной находки.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.models import FindingOutcomeItem
from hub.services import finding_outcome


def _finding(title: str, **over) -> dict:
    base = {
        "title": title,
        "severity": "high",
        "category": "correctness",
        "locator": "file",
        "file": "hub/db.py",
    }
    base.update(over)
    return base


async def _submitted_task(client: AsyncClient, title: str) -> int:
    resp = await client.post("/api/tasks", json={"title": title})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    return task_id


def _unresolved(title: str, why: str = "голоса разошлись") -> dict:
    """Неразрешённая находка ровно той формы, что приходит с прода.

    Ни файла, ни строки, ни категории: MachineUnresolvedFinding объявлена с
    extra="forbid" и несёт только title и why. Проверено на живом отчёте #169
    задачи #1084 — форма закрыта схемой, а не совпадением примера.
    """
    return {"title": title, "why": why}


async def _report(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    findings: list[dict],
    unresolved: list[dict] | None = None,
) -> int:
    unresolved = unresolved or []
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill="lite-diff-review",
        raw_count=len(findings) + len(unresolved),
        findings_confirmed=json.dumps(findings, ensure_ascii=False),
        unresolved=json.dumps(unresolved, ensure_ascii=False),
        incomplete=False,
    )
    await db.commit()
    row = await repo.get_latest_machine_review(db, task_id)
    return int(dict(row)["id"])


async def test_submit_requires_disposition_for_confirmed(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1: сдача с незакрытыми находками отклонена, и находки НАЗВАНЫ.

    Отказ, говорящий «у вас девять незакрытых находок», отправляет автора
    искать, какие именно девять. Гейт их уже знает.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Findings owe an answer")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await _report(db, task_id, 1, [_finding("утечка курсора"), _finding("гонка")])
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )

    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 422, resp.text
    assert "утечка курсора" in resp.text and "гонка" in resp.text


async def test_no_confirmed_findings_no_requirement(client: AsyncClient, monkeypatch):
    """AC-3: где ответа не должны — гейт молчит.

    На первой сдаче отчётов нет вовсе. Правило, срабатывающее там, где отвечать
    не на что, учит обходить себя.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Nothing to answer")

    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 200, resp.text


async def test_wont_fix_spawns_defect_draft(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2: то, что не чинят, остаётся работой, а не исчезает вместе с решением."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Wont fix leaves a trace")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    review_id = await _report(db, task_id, 1, [_finding("тяжёлый запрос")])
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

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [
                {
                    "finding_uid": uid,
                    "outcome": "wont_fix",
                    "note": "цена оптимизации выше цены задержки",
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text

    drafts = await repo.list_tasks_by_status(db, "draft", limit=20)
    spawned = [dict(r) for r in drafts if dict(r).get("caused_by_task_id") == task_id]
    assert len(spawned) == 1, "решение не чинить оставляет дефект-драфт"
    assert spawned[0]["found_in"] == "review"
    assert spawned[0]["work_type"] == "bug"
    assert "цена оптимизации выше цены задержки" in spawned[0]["description"]

    stored = [dict(r) for r in await repo.list_finding_outcomes(db, review_id)]
    assert [r["outcome"] for r in stored] == ["wont_fix"]


async def test_the_authors_account_is_not_a_human_judgement(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Исход автора НЕ попадает в finding_dispositions и не трогает precision.

    _disposition_metrics читает finding_dispositions без фильтра по тому, кто
    решал. Запись туда самоотчёта автора — самый дешёвый способ уничтожить
    метрику: каждое собственное «исправлено» считалось бы подтверждением
    человеком того, что находка настоящая, и precision начала бы мерить мнение
    автора о своей же работе.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Account is not judgement")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    review_id = await _report(db, task_id, 1, [_finding("лишний индекс")])
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

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={"finding_outcomes": [{"finding_uid": uid, "outcome": "fixed"}]},
    )
    # Сначала убедиться, что исход ВООБЩЕ записан: без этого «в диспозициях
    # пусто» верно и тогда, когда не записано ничего никуда, и тест зелен при
    # снятом гейте.
    assert resp.status_code == 200, resp.text
    stored = [dict(r) for r in await repo.list_finding_outcomes(db, review_id)]
    assert [r["outcome"] for r in stored] == ["fixed"]

    assert await repo.list_finding_dispositions(db, review_id) == [], (
        "самоотчёт автора не является суждением человека (#876)"
    )
    metrics = await repo.fetchall(db, "SELECT COUNT(*) AS n FROM finding_dispositions")
    assert int(dict(metrics[0])["n"]) == 0


async def test_an_outcome_about_another_generation_is_refused(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Ответ про находку, которой эта сдача не несёт, — не частичный успех."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Wrong address")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await _report(db, task_id, 1, [_finding("настоящая")])
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={"finding_outcomes": [{"finding_uid": "0" * 16, "outcome": "fixed"}]},
    )
    assert resp.status_code == 422
    assert "not an open confirmed finding" in resp.text


def test_an_unfixed_finding_owes_a_reason():
    """«Исправлено» видно в диффе; остальное видно только в том, что сказано."""
    with pytest.raises(ValueError):
        FindingOutcomeItem(finding_uid="abc", outcome="false_positive")
    assert FindingOutcomeItem(
        finding_uid="abc", outcome="false_positive", note="в коде этого нет"
    ).note


async def test_a_rejected_submission_leaves_no_outcome_behind(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Отказ не должен оставлять следов: иначе исправленная сдача ломается.

    Запись идёт на общий коннект процесса, и отказ его не откатывает. Если
    исходы писались до остальных гейтов, отклонённая попытка закрывала часть
    находок — и ПОЛНЫЙ повторный ответ автора падал с «находка уже не открыта»,
    то есть правильный ответ отвергался из-за собственной отброшенной попытки.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Refusal leaves nothing")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    review_id = await _report(db, task_id, 1, [_finding("A"), _finding("B")])
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )
    open_items = await finding_outcome.open_findings(db, task_id, 1)
    uid_a = next(i["finding_uid"] for i in open_items if i["title"] == "A")
    uid_b = next(i["finding_uid"] for i in open_items if i["title"] == "B")

    # Неполный ответ — отказ.
    refused = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [
                {"finding_uid": uid_a, "outcome": "wont_fix", "note": "живём"}
            ]
        },
    )
    assert refused.status_code == 422
    assert await repo.list_finding_outcomes(db, review_id) == [], (
        "отклонённая сдача ничего не записала"
    )
    drafts = await repo.list_tasks_by_status(db, "draft", limit=20)
    assert not [r for r in drafts if dict(r).get("caused_by_task_id") == task_id], (
        "и дефект-драфт за отклонённую сдачу не завела"
    )

    # Тот же ответ, дополненный до полного, обязан пройти.
    ok = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [
                {"finding_uid": uid_a, "outcome": "wont_fix", "note": "живём"},
                {"finding_uid": uid_b, "outcome": "fixed"},
            ]
        },
    )
    assert ok.status_code == 200, ok.text
    assert len(await repo.list_finding_outcomes(db, review_id)) == 2


async def test_one_answer_closes_the_finding_in_both_ladder_reports(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Лестница #879 описывает один дефект дважды — ответ на него один.

    finding_uid выводится из содержания, поэтому одна и та же находка в
    lite- и deep-отчёте несёт один uid. Ключ по одному uid схлопывал две
    строки в одну: ответ ложился на последний отчёт, а находка первого
    оставалась неотвеченной навсегда — ровно то исчезновение, которое мерили.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Ladder answers once")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    same = _finding("одна и та же утечка")
    lite = await _report(db, task_id, 1, [same])
    deep = await _report(db, task_id, 1, [same])
    assert lite != deep
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )
    open_items = await finding_outcome.open_findings(db, task_id, 1)
    assert len(open_items) == 2, "оба отчёта несут её и оба ждут ответа"
    uid = open_items[0]["finding_uid"]
    assert open_items[1]["finding_uid"] == uid

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={"finding_outcomes": [{"finding_uid": uid, "outcome": "fixed"}]},
    )
    assert resp.status_code == 200, resp.text
    assert len(await repo.list_finding_outcomes(db, lite)) == 1
    assert len(await repo.list_finding_outcomes(db, deep)) == 1


async def test_a_named_task_replaces_the_draft(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Названная работа не дублируется: одна находка — одно место учёта."""
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Named task wins")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await _report(db, task_id, 1, [_finding("уже разложено")])
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

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [
                {
                    "finding_uid": uid,
                    "outcome": "deferred",
                    "note": "вынесено отдельно",
                    "linked_task_id": task_id,
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    drafts = await repo.list_tasks_by_status(db, "draft", limit=20)
    assert not [r for r in drafts if dict(r).get("caused_by_task_id") == task_id], (
        "работа уже названа автором — второе место учёта не заводится"
    )


def test_open_findings_doc_names_both_sections():
    """Docstring is the contract callers read before the body.

    The function used to return confirmed findings only, and its first line
    still said so after #1085 started walking ``unresolved`` too. A caller
    that trusted the docstring would skip the section that carried every
    useful finding of the measured runs.
    """
    doc = finding_outcome.open_findings.__doc__ or ""
    first = doc.strip().splitlines()[0]
    assert "confirmed" in first.lower()
    assert "unresolved" in first.lower(), (
        "первая строка называет оба раздела: иначе она описывает функцию "
        "до #1085, а не ту, что исполняется"
    )


# --- Находки, о которых адъюдикаторы не договорились (#1085) ----------------
#
# Замер по пяти отчётам подряд (#163-#167, задачи #1083 и #1084, 30-31.08.2026):
# 13 сырых кандидатов, 0 в findings_confirmed за все пять прогонов, 6 в
# unresolved — и все шесть оказались настоящими дефектами. Гейт вёл себя верно
# (auto_verdict отказывает при непустом unresolved), слепа была БУХГАЛТЕРИЯ:
# open_findings читал ровно findings_confirmed, поэтому у неразрешённой находки
# не было ни uid, ни исхода, ни предупреждения на пересдаче. Весь полезный
# выход платных прогонов за две задачи не оставил в учёте ни одного следа.


#: uid подтверждённой находки отчёта #169 задачи #1084, снятый с ПРОДА.
#: Прибит гвоздём намеренно: материал, из которого он выводится, — контракт с
#: уже вынесенными диспозициями. Сдвинется материал — каждое человеческое
#: суждение перестанет находить свою находку, и упасть об этом должен тест, а
#: не прод.
LIVE_CONFIRMED_UID = "bf3c65f410eef64a"  # pragma: allowlist secret


async def _sent_back(client: AsyncClient, task_id: int) -> None:
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
    )


async def test_unresolved_finding_accepts_an_outcome(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1: у неразрешённой находки есть uid, и исход по нему принимается.

    До этой правки автор задачи #1083 получал на этом месте 422: uid'ы
    раздавались по findings_confirmed, а раздел, который нёс всю пользу
    прогона, их не имел вовсе.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Unresolved owes an answer too")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    review_id = await _report(
        db,
        task_id,
        1,
        [],
        [_unresolved("хвост промпта зовёт MCP, когда путь — HTTP")],
    )
    await _sent_back(client, task_id)

    open_items = await finding_outcome.open_findings(db, task_id, 1)
    assert len(open_items) == 1, (
        "неразрешённая находка ждёт ответа так же, как confirmed"
    )
    item = open_items[0]
    assert item["finding_kind"] == "unresolved"
    uid = item["finding_uid"]
    assert uid, "у записи есть устойчивый uid"

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [
                {
                    "finding_uid": uid,
                    "outcome": "real_fixed",
                    "note": "разобрал: порядок в промпте действительно жёг TTL",
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text

    stored = [dict(r) for r in await repo.list_finding_outcomes(db, review_id)]
    assert [r["outcome"] for r in stored] == ["real_fixed"]
    assert [r["finding_kind"] for r in stored] == ["unresolved"], (
        "читатель видит, что адъюдикаторы не сошлись, а решил автор"
    )
    assert [r["finding_uid"] for r in stored] == [uid]

    # По этому же uid находку можно найти повторно: он выводится из содержания,
    # а не выдаётся при записи.
    again = await _report(
        db,
        task_id,
        2,
        [],
        [
            _unresolved(
                "хвост промпта зовёт MCP, когда путь — HTTP", why="другими словами"
            )
        ],
    )
    assert again != review_id
    assert [
        i["finding_uid"] for i in await finding_outcome.open_findings(db, task_id, 2)
    ] == [uid], (
        "переформулированное объяснение — та же находка: why в идентичность не входит"
    )


async def test_undisposed_unresolved_is_named_on_resubmission(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2: неназванный исход неразрешённой находки НАЗЫВАЮТ поштучно.

    В режиме warn сдача проходит — меняется видимость, а не жёсткость гейта, —
    но предупреждение обязано назвать находку, а не её количество.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Warn names the unresolved")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await _report(
        db,
        task_id,
        1,
        [],
        [_unresolved("замыкание на ключе кэша")],
    )
    await _sent_back(client, task_id)

    # Строгий режим идёт первым: он ничего не меняет в состоянии задачи, и та
    # же сдача остаётся для второй половины проверки. Наоборот не выйдет —
    # принятая сдача бампает поколение, а долг остаётся за предыдущим.
    refused = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert refused.status_code == 422
    assert "замыкание на ключе кэша" in refused.text, (
        "отказ называет находку, а не её количество"
    )

    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 200, "режим warn не блокирует сдачу"

    updates = (await client.get(f"/api/tasks/{task_id}/updates")).json()
    notes = "\n".join(u["content"] for u in updates)
    assert "замыкание на ключе кэша" in notes, (
        "предупреждение тоже называет находку поимённо"
    )
    assert "подтверждённых находок предыдущей сдачи" not in notes, (
        "предупреждение не называет неразрешённую находку подтверждённой: "
        "адъюдикаторы о ней не договорились"
    )


async def test_reports_written_before_the_change_still_read(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3: у старого отчёта исходов нет — и это отсутствие, а не ошибка.

    Золотое значение uid взято с ПРОДА: находка отчёта #169 задачи #1084
    несёт finding_uid bf3c65f410eef64a. Если материал confirmed сдвинется
    хоть на бит, каждая уже вынесенная человеком диспозиция перестанет
    находить свою находку — поэтому число здесь прибито гвоздём.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "warn")
    task_id = await _submitted_task(client, "Old reports keep reading")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    live = {
        "title": (
            "Восстановление из текста штампует отчёт текущим поколением "
            "и обходит пин #1084"
        ),
        "severity": "high",
        "category": "correctness",
        "file": "hub/services/review_dispatch.py",
        "line": 1433,
        "locator": "lines",
        "start_line": 1433,
        "end_line": 1433,
    }
    review_id = await _report(
        db, task_id, 1, [live], [_unresolved("никто не рассудил")]
    )
    await _sent_back(client, task_id)

    open_items = await finding_outcome.open_findings(db, task_id, 1)
    confirmed = [i for i in open_items if i["finding_kind"] == "confirmed"]
    assert [i["finding_uid"] for i in confirmed] == [LIVE_CONFIRMED_UID], (
        "uid подтверждённой находки не изменился: живой отчёт #169 с прода"
    )

    assert await repo.list_finding_outcomes(db, review_id) == [], (
        "у старого отчёта исходов нет — это отсутствие, а не ошибка чтения"
    )

    brief = await client.get(f"/api/tasks/{task_id}/review-brief")
    assert brief.status_code == 200, brief.text
    report = brief.json()["machine_review"]
    assert report["findings_confirmed"][0]["finding_uid"] == LIVE_CONFIRMED_UID
    assert report["unresolved"][0]["finding_uid"], (
        "uid проштампован на чтении и для неразрешённой находки"
    )


async def test_an_outcome_from_the_wrong_dictionary_is_refused(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Словарь confirmed отвечает про дефект, о котором ДОГОВОРИЛИСЬ.

    Назвать неразрешённую находку false_positive значит записать, что гейт её
    подтвердил, а автор объявил ложной. Гейт не подтверждал ничего — поэтому
    чужое слово отклоняется, а не переводится молча.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Dictionaries do not mix")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await _report(db, task_id, 1, [_finding("настоящая")], [_unresolved("спорная")])
    await _sent_back(client, task_id)

    open_items = await finding_outcome.open_findings(db, task_id, 1)
    conf_uid = next(
        i["finding_uid"] for i in open_items if i["finding_kind"] == "confirmed"
    )
    unres_uid = next(
        i["finding_uid"] for i in open_items if i["finding_kind"] == "unresolved"
    )

    wrong = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [
                {"finding_uid": conf_uid, "outcome": "fixed"},
                {
                    "finding_uid": unres_uid,
                    "outcome": "false_positive",
                    "note": "её тут нет",
                },
            ]
        },
    )
    assert wrong.status_code == 422, wrong.text
    assert "real_fixed" in wrong.text, "отказ называет словарь, которым отвечают"

    right = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [
                {"finding_uid": conf_uid, "outcome": "fixed"},
                {
                    "finding_uid": unres_uid,
                    "outcome": "not_a_defect",
                    "note": "разобрал построчно: описанного пути нет",
                },
            ]
        },
    )
    assert right.status_code == 200, right.text


async def test_an_unexamined_unresolved_finding_leaves_a_defect_draft(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """«Не разбирал» — ответ, и он оставляет работу, а не тишину.

    Отличие от «разобрал и отверг» — то самое, ради которого заведён свой
    словарь: после not_judged дефект может быть в коде, и это надо кому-то
    планировать.
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Not judged leaves work")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await _report(db, task_id, 1, [], [_unresolved("подозрительный кэш")])
    await _sent_back(client, task_id)
    uid = (await finding_outcome.open_findings(db, task_id, 1))[0]["finding_uid"]

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [
                {
                    "finding_uid": uid,
                    "outcome": "not_judged",
                    "note": "не хватило времени на разбор, оставляю следующему",
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text

    drafts = await repo.list_tasks_by_status(db, "draft", limit=20)
    spawned = [dict(r) for r in drafts if dict(r).get("caused_by_task_id") == task_id]
    assert len(spawned) == 1, "неразобранная находка остаётся работой"
    assert "не разбиралась" in spawned[0]["description"], (
        "драфт говорит, ЧТО с находкой произошло"
    )
    assert "адъюдикаторы" in spawned[0]["description"], (
        "и что суждение здесь авторское: гейт находку не подтверждал"
    )


async def test_an_unresolved_twin_of_a_confirmed_finding_is_a_separate_row(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Один заголовок в двух разделах — две находки, а не одна.

    Проверяется УЧЁТ, а не хеш: подтверждённая находка без места и
    неразрешённая с тем же заголовком остаются двумя строками долга, и ответ
    на первую не закрывает вторую, которой автор не касался. (Материалы этих
    двух id сегодня различаются ещё и формой — пять компонент против одной, —
    поэтому проба с удалением метки раздела тест не роняет; метка защищает от
    изменения формы, а не от сегодняшней коллизии.)
    """
    monkeypatch.setattr("hub.config.FINDING_OUTCOME", "require")
    task_id = await _submitted_task(client, "Twins across sections")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    same = "одна и та же формулировка"
    await _report(
        db,
        task_id,
        1,
        [{"title": same, "severity": "high", "category": "", "locator": "none"}],
        [_unresolved(same)],
    )
    await _sent_back(client, task_id)

    open_items = await finding_outcome.open_findings(db, task_id, 1)
    uids = {i["finding_kind"]: i["finding_uid"] for i in open_items}
    assert len(open_items) == 2 and len(set(uids.values())) == 2, (
        "разделы не схлопываются в один uid"
    )

    partial = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={
            "finding_outcomes": [{"finding_uid": uids["confirmed"], "outcome": "fixed"}]
        },
    )
    assert partial.status_code == 422, "вторая находка осталась без ответа"
