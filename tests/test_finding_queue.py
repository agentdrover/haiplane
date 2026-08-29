"""Находки переживают статус задачи, а очередь — единственный путь к ним (#1038).

Разбор находки никогда не был дорогим: форма с радиокнопками живёт в карточке
задачи с #876. Дорогим было ДОЙТИ до неё. Отчёт пишется, пока задача в review;
к моменту, когда её находки стоит судить, задача уже completed, а все разделы
инбокса собираются из ``list_tasks_by_status`` — то есть находки исчезают ровно
тогда, когда задача завершается. За семь дней так накопилось 47 подтверждённых
находок без единого ответа.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.repository import (
    count_unjudged_findings,
    list_finding_dispositions,
    list_unjudged_findings,
)
from hub.services import finding_evidence as fe
from hub.services.finding_disposition import record_finding_dispositions
from hub.models import FindingDisposition, FindingDispositionItem
from hub.services.finding_identity import finding_uids
from hub.services.orchestration import practice_metrics
from tests.test_finding_evidence import (
    _FILE,
    _FINDING_LINES,
    _git as _repo_git,
    _init_repo,
    _report_on,
    _sha,
    _task_on_clone,
    _write_numbered,
)


def _finding(title: str, **over) -> dict:
    base = {
        "title": title,
        "severity": "high",
        "category": "correctness",
        "locator": "file",
        "file": "hub/db.py",
        "detail": "",
    }
    base.update(over)
    return base


async def _task(db: aiosqlite.Connection, task_id: int, status: str, generation: int):
    await db.execute(
        "INSERT INTO tasks (id, title, status, submission_generation) "
        "VALUES (?, ?, ?, ?)",
        (task_id, f"задача {task_id}", status, generation),
    )


async def _report(
    db: aiosqlite.Connection,
    review_id: int,
    task_id: int,
    generation: int,
    findings: list[dict],
):
    await db.execute(
        "INSERT INTO machine_reviews (id, task_id, submission_generation, "
        "findings_confirmed) VALUES (?, ?, ?, ?)",
        (review_id, task_id, generation, json.dumps(findings, ensure_ascii=False)),
    )


def _titles(rows) -> list[str]:
    return [json.loads(str(r["finding"]))["title"] for r in rows]


async def test_a_completed_task_is_still_in_the_queue(db: aiosqlite.Connection):
    """AC-1: именно этот случай сейчас теряется.

    Задача завершена, значит не попадает ни в один раздел инбокса — все они
    строятся по статусу. Находки при этом никуда не делись и ответа не имеют.
    """
    await _task(db, 7, "completed", 1)
    await _report(db, 10, 7, 1, [_finding("живая-A"), _finding("живая-B")])
    await db.commit()

    rows = await list_unjudged_findings(db)
    assert _titles(rows) == ["живая-A", "живая-B"]
    assert rows[0]["task_status"] == "completed", (
        "очередь не должна зависеть от статуса задачи — иначе чинится не тот дефект"
    )


async def test_a_stale_report_is_not_queued(db: aiosqlite.Connection):
    """AC-3: находки пересданной генерации описывают код, которого уже нет.

    Просить человека судить их — просить о дефекте в файле, каким тот был.
    """
    await _task(db, 8, "completed", 2)
    await _report(db, 20, 8, 1, [_finding("устаревшая-1"), _finding("устаревшая-2")])
    await _report(db, 21, 8, 2, [_finding("живая")])
    await db.commit()

    assert _titles(await list_unjudged_findings(db)) == ["живая"]
    assert (await count_unjudged_findings(db))["findings"] == 1


async def test_judging_one_finding_leaves_its_siblings_queued(
    db: aiosqlite.Connection,
):
    """AC-4: разметка идёт поштучно, а не отчётами целиком."""
    await _task(db, 9, "completed", 1)
    await _report(db, 30, 9, 1, [_finding("A"), _finding("B"), _finding("C")])
    await db.execute(
        "INSERT INTO finding_dispositions (review_id, task_id, submission_generation, "
        "finding_index, finding_uid, disposition, decided_by) "
        "VALUES (30, 9, 1, 1, 'uid-b', 'fixed', 'denis')"
    )
    await db.commit()

    assert _titles(await list_unjudged_findings(db)) == ["A", "C"]


async def test_queue_length_equals_the_metric(db: aiosqlite.Connection):
    """AC-2: одно множество — один ответ (#518).

    Считается ОДНИМ тестом на одних данных. Два независимых теста подтвердили
    бы, что каждое число самосогласовано, и промолчали бы о том, что они
    расходятся между собой — а именно это и происходило: метрика вычитала одну
    сумму из другой и потому считала находки устаревших отчётов.
    """
    await _task(db, 11, "completed", 2)
    await _report(db, 40, 11, 1, [_finding("устаревшая")])
    await _report(db, 41, 11, 2, [_finding("живая-1"), _finding("живая-2")])
    await _task(db, 12, "review", 1)
    await _report(db, 42, 12, 1, [_finding("живая-3")])
    await db.commit()

    queue = await list_unjudged_findings(db)
    metrics = await practice_metrics(db, since_days=90)
    assert (
        len(queue) == metrics["machine_reviews"]["dispositions"]["confirmed_unjudged"]
    )
    assert len(queue) == 3, "устаревший отчёт не считается ни там, ни там"


async def test_the_queue_records_through_the_same_path(client: AsyncClient):
    """AC-6: запись из очереди идёт тем же путём, что из карточки.

    Второй способ судить — второе место, где правило «решает человек» может
    разойтись с первым.
    """
    resp = await client.post("/api/tasks", json={"title": "Queue task"})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "model": "grok-4.6",
            "raw_count": 1,
            "findings_confirmed": [
                {
                    "locator": "file",
                    "file": "hub/db.py",
                    "title": "утечка",
                    "severity": "high",
                }
            ],
            "findings_rejected": [],
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "reviewer",
        },
    )

    page = await client.get("/findings")
    assert page.status_code == 200
    assert "утечка" in page.text

    # Форма обязана НАЗЫВАТЬ отчёт. Без этой проверки поле можно убрать из
    # шаблона, и тест останется зелёным: при единственном отчёте сервис
    # свалится на «новейший» и попадёт в него случайно.
    assert 'name="review_id"' in page.text
    review_id = int(page.text.split('name="review_id" value="')[1].split('"')[0])

    posted = await client.post(
        f"/tasks/{task_id}/web-finding-dispositions",
        data={
            "disposition-0": "fixed",
            "return_to": "queue",
            "review_id": str(review_id),
        },
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert posted.headers["location"] == "/findings", (
        "разметка из очереди возвращает в очередь, иначе цена навигации остаётся"
    )
    assert (await client.get("/findings")).text.count("утечка") == 0


async def test_an_out_of_range_review_id_is_refused_not_crashed(
    client: AsyncClient, db: aiosqlite.Connection
):
    """Отказ говорит, что запрос неверен, а не что сломался хаб.

    id шире SQLite INTEGER доходит до bind и поднимает OverflowError — не
    ValueError и не LookupError, — то есть 500 вместо 400.
    """
    await _task(db, 51, "completed", 1)
    await _report(db, 90, 51, 1, [_finding("A")])
    await db.commit()

    resp = await client.post(
        "/tasks/51/web-finding-dispositions",
        data={"disposition-0": "fixed", "review_id": str(2**63)},
        follow_redirects=False,
    )
    assert resp.status_code == 400, resp.text


async def test_the_queue_keeps_the_project_it_was_opened_with(
    client: AsyncClient, db: aiosqlite.Connection
):
    """Счёт в инбоксе сужен по проекту — страница обязана отвечать на тот же вопрос.

    Иначе человек жмёт «1» и попадает в список, где двадцать одна чужая
    находка: то же расхождение двух чисел об одном (#518), которое эта задача
    и чинит в метрике.
    """
    await db.execute("INSERT INTO projects (id, slug, name) VALUES (1, 'alpha', 'A')")
    await db.execute("INSERT INTO projects (id, slug, name) VALUES (2, 'beta', 'B')")
    await _task(db, 61, "completed", 1)
    await _task(db, 62, "completed", 1)
    await db.execute("UPDATE tasks SET project_id=1 WHERE id=61")
    await db.execute("UPDATE tasks SET project_id=2 WHERE id=62")
    await _report(db, 100, 61, 1, [_finding("альфа-1")])
    await _report(db, 101, 62, 1, [_finding("бета-1"), _finding("бета-2")])
    await db.commit()

    # Ссылка, по которой человек попадает в очередь, несёт тот же проект, что
    # и счёт рядом с ней. Иначе клик по «1» открывает двадцать одну находку.
    board = (await client.get("/?project=alpha")).text
    assert "/findings?project=alpha" in board

    scoped = (await client.get("/findings?project=alpha")).text
    assert "альфа-1" in scoped
    assert "бета-1" not in scoped, "клик по счёту проекта не расширяет выборку"
    # И возврат после сохранения остаётся в том же проекте.
    assert 'name="return_project" value="alpha"' in scoped

    posted = await client.post(
        "/tasks/61/web-finding-dispositions",
        data={
            "disposition-0": "fixed",
            "return_to": "queue",
            "return_project": "alpha",
            "review_id": "100",
        },
        follow_redirects=False,
    )
    assert posted.headers["location"] == "/findings?project=alpha"


async def test_the_top_bar_counts_waiting_findings(
    client: AsyncClient, db: aiosqlite.Connection
):
    """Верхняя плашка не должна писать «Inbox 0» при непустой секции ниже.

    Находки живут не в статусном списке, а в своём счёте, и inbox_total
    складывался только из статусных. Получалось, что раздел есть, а числа,
    на которое смотрят, нет — ровно та невидимость, которую задача убирает.
    """
    await _task(db, 71, "completed", 1)
    await _report(db, 110, 71, 1, [_finding("ждёт")])
    await db.commit()

    page = (await client.get("/")).text
    assert "Находки ждут суждения" in page, "секция есть"
    # А число, на которое смотрят с плашки, её видит: класс is-zero ставится
    # ровно при inbox_total == 0, поэтому его отсутствие и есть проверка.
    assert "topbar-stat--inbox is-zero" not in page, (
        "верхняя плашка показывала бы Inbox 0 при непустой секции ниже"
    )
    assert '<span class="topbar-stat-value">1</span>' in page


async def test_finding_text_is_escaped_on_the_queue(client: AsyncClient):
    """Текст находки пишет агент-ревьюер, то есть это НЕДОВЕРЕННЫЙ вход.

    Заголовок, деталь и путь к файлу приходят из отчёта чужой модели и попадают
    на страницу владельца. Шаблон обязан показывать их как данные, а не как
    разметку — то же правило, по которому пакет доказательств стюарда цитирует
    входы, а не исполняет их.
    """
    resp = await client.post("/api/tasks", json={"title": "Escaping task"})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "model": "grok-4.6",
            "raw_count": 1,
            "findings_confirmed": [
                {
                    "locator": "none",
                    "title": "<script>alert(1)</script>",
                    "severity": "high",
                    "detail": "<img src=x onerror=alert(2)>",
                }
            ],
            "findings_rejected": [],
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "reviewer",
        },
    )

    page = (await client.get("/findings")).text
    # Сверяются ТОЧНЫЕ полезные нагрузки, а не общие подстроки. Широкая проверка
    # вроде «нет "</script>"» ловит собственный тег htmx из базового шаблона, а
    # «нет "onerror="» — саму экранированную строку, где это слово безвредно:
    # тег обезвреживает угловая скобка, и проверять надо именно её.
    assert "<script>alert(1)</script>" not in page
    assert "<img src=x onerror=alert(2)>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page, "показан, но как текст"
    assert "&lt;img src=x onerror=alert(2)&gt;" in page


async def test_judging_names_the_report_it_read(db: aiosqlite.Connection):
    """Лестница #879 оставляет ДВА текущих отчёта, и судят конкретный.

    Карточка задачи рисует только новейший, поэтому «последний отчёт» был
    однозначным ответом везде — до очереди, которая показывает и ранний. Запись
    без указания отчёта легла бы на новейший: находка, которую никто не читал,
    оказалась бы размечена, а прочитанная осталась бы в очереди.
    """
    await _task(db, 21, "completed", 1)
    await _report(db, 50, 21, 1, [_finding("дешёвый-A")])
    await _report(db, 51, 21, 1, [_finding("глубокий-B")])
    await db.commit()

    await record_finding_dispositions(
        db,
        21,
        [
            FindingDispositionItem(
                finding_index=0, disposition=FindingDisposition("fixed")
            )
        ],
        decided_by="denis",
        review_id=50,
    )
    await db.commit()

    assert [
        dict(r)["finding_index"] for r in await list_finding_dispositions(db, 50)
    ] == [0]
    assert await list_finding_dispositions(db, 51) == [], (
        "ответ лёг на прочитанный отчёт, а не на новейший"
    )
    assert _titles(await list_unjudged_findings(db)) == ["глубокий-B"]


async def test_a_superseded_report_cannot_be_judged(db: aiosqlite.Connection):
    """Названный устаревший отчёт — не почти-угаданный, а другой вопрос."""
    await _task(db, 22, "completed", 2)
    await _report(db, 60, 22, 1, [_finding("устаревшая")])
    await _report(db, 61, 22, 2, [_finding("живая")])
    await db.commit()

    try:
        await record_finding_dispositions(
            db,
            22,
            [
                FindingDispositionItem(
                    finding_index=0, disposition=FindingDisposition("fixed")
                )
            ],
            decided_by="denis",
            review_id=60,
        )
    except ValueError as exc:
        assert "submission" in str(exc)
    else:  # pragma: no cover - защита от молчаливого приёма
        raise AssertionError("разметка устаревшего отчёта принята")


async def test_the_queue_is_narrowed_by_project(db: aiosqlite.Connection):
    """Доска, отфильтрованная по проекту, не показывает чужие находки (#627)."""
    await db.execute("INSERT INTO projects (id, slug, name) VALUES (1, 'alpha', 'A')")
    await db.execute("INSERT INTO projects (id, slug, name) VALUES (2, 'beta', 'B')")
    await _task(db, 31, "completed", 1)
    await _task(db, 32, "completed", 1)
    await db.execute("UPDATE tasks SET project_id=1 WHERE id=31")
    await db.execute("UPDATE tasks SET project_id=2 WHERE id=32")
    await _report(db, 70, 31, 1, [_finding("альфа-1")])
    await _report(db, 71, 32, 1, [_finding("бета-1"), _finding("бета-2")])
    await db.commit()

    assert _titles(await list_unjudged_findings(db, project_id=1)) == ["альфа-1"]
    assert (await count_unjudged_findings(db, project_id=1))["findings"] == 1
    assert (await count_unjudged_findings(db))["findings"] == 3


async def test_the_backlog_is_not_windowed(db: aiosqlite.Connection):
    """Ждущая находка не истекает, и число на метриках равно длине очереди.

    Precision — поток: как обернулись отчёты периода. «Сколько ждёт» — запас, и
    находка, оставшаяся без ответа в апреле, без ответа и сегодня. Окно спрятало
    бы ровно самые старые и развело бы счётчик со страницей, на которую он ведёт.
    """
    await _task(db, 41, "completed", 1)
    await _report(db, 80, 41, 1, [_finding("древняя")])
    await db.execute(
        "UPDATE machine_reviews SET created_at = datetime('now', '-100 days') "
        "WHERE id = 80"
    )
    await db.commit()

    metrics = await practice_metrics(db, since_days=90)
    assert len(await list_unjudged_findings(db)) == 1
    assert metrics["machine_reviews"]["dispositions"]["confirmed_unjudged"] == 1, (
        "находка старше окна всё ещё ждёт ответа и обязана считаться"
    )


def _li_for(page: str, title: str) -> str:
    for chunk in page.split("<li>"):
        if title in chunk:
            return chunk.split("</li>", 1)[0]
    raise AssertionError(f"finding {title!r} is not on the page")


_FOOTER = {
    **_FINDING_LINES,
    "title": "footer leak",
    "start_line": 18,
    "end_line": 19,
    "line": 18,
}


async def _current_generation(
    db: aiosqlite.Connection, task_id: int, generation: int = 1
) -> None:
    # _task_on_clone leaves submission_generation at 0; the queue only shows
    # findings whose report generation matches the task's current one.
    await repo.update_task(db, task_id, submission_generation=generation)


async def test_queue_shows_each_finding_its_own_touch_fact(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: Path
):
    """#1042 AC-1: the queue names a touch of the place, not a fix.

    The card already shows this. The person who judges in bulk never opened
    the card, so the same fact has to stand next to each finding here.
    """
    clone = _init_repo(tmp_path / "queue-touch")
    baseline = _sha(clone)
    task_id = await _task_on_clone(db, clone, title="queue mixed facts")
    await _report_on(db, task_id, generation=1, sha=baseline)
    await _current_generation(db, task_id)
    await db.execute(
        "UPDATE machine_reviews SET findings_confirmed=? WHERE task_id=?",
        (json.dumps([_FINDING_LINES, _FOOTER]), task_id),
    )
    _write_numbered(clone / _FILE, tweak=5)
    _repo_git(clone, "add", "-A")
    _repo_git(clone, "commit", "-m", "touch named lines")
    await db.commit()

    page = (await client.get("/findings")).text
    assert page.count("guard drops the flag") == 1
    touched = _li_for(page, "guard drops the flag")
    untouched = _li_for(page, "footer leak")
    assert "Место тронуто после отчёта" in touched
    assert "touch named lines" in touched
    assert "Место не тронуто после отчёта" in untouched
    assert "исправление дефекта" not in page.lower()
    assert "исправлено" in page


async def test_queue_git_runs_once_per_report_not_per_finding(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: Path
):
    """#1042 AC-2: the git COUNTER, not a story about how git ought to work.

    Three reports, several findings each. A call per finding would make the
    page unusable at the live queue size (47 findings). One log per report
    is the budget this page is allowed to spend.
    """
    clone = _init_repo(tmp_path / "queue-budget")
    baseline = _sha(clone)
    counts = (2, 3, 4)
    for n, count in enumerate(counts, start=1):
        findings = [
            {**_FINDING_LINES, "title": f"rep{n}-f{i}", "start_line": 5, "line": 5}
            for i in range(count)
        ]
        task_id = await _task_on_clone(db, clone, title=f"queue-budget-{n}")
        await repo.record_submission(
            db, task_id=task_id, generation=1, sha=baseline, base_branch="main"
        )
        await repo.insert_machine_review(
            db,
            task_id=task_id,
            submission_generation=1,
            findings_confirmed=json.dumps(findings),
            incomplete=False,
        )
        await _current_generation(db, task_id)
    await db.commit()

    real_git = fe._git
    calls: list[tuple] = []

    async def wrapped(*args, **kwargs):
        calls.append(args)
        return await real_git(*args, **kwargs)

    with patch.object(fe, "_git", wrapped):
        page = await client.get("/findings")
    assert page.status_code == 200
    logs = [c for c in calls if c and c[0] == "log"]
    assert len(logs) == len(counts), (
        f"git log must run once per report, not once per finding "
        f"(reports={len(counts)}, findings={sum(counts)}, logs={len(logs)})"
    )


async def test_queue_evidence_follows_uid_not_position(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: Path
):
    """#1042 AC-3: reversing the report must not swap the facts (#1007).

    Positional attach is what the card still does. A second site that keyed
    by index would reintroduce the addressing #1007 retired.
    """
    clone = _init_repo(tmp_path / "queue-uid")
    baseline = _sha(clone)
    task_id = await _task_on_clone(db, clone, title="queue uid stitch")
    ordered = [_FINDING_LINES, _FOOTER]
    await _report_on(db, task_id, generation=1, sha=baseline)
    await _current_generation(db, task_id)
    await db.execute(
        "UPDATE machine_reviews SET findings_confirmed=? WHERE task_id=?",
        (json.dumps(ordered), task_id),
    )
    _write_numbered(clone / _FILE, tweak=5)
    _repo_git(clone, "add", "-A")
    _repo_git(clone, "commit", "-m", "touch named lines")
    await db.commit()

    uids_fwd = finding_uids(ordered)
    uids_rev = finding_uids(list(reversed(ordered)))
    assert uids_fwd[0] == uids_rev[1]
    assert uids_fwd[1] == uids_rev[0]

    await db.execute(
        "UPDATE machine_reviews SET findings_confirmed=? WHERE task_id=?",
        (json.dumps(list(reversed(ordered))), task_id),
    )
    await db.commit()

    page = (await client.get("/findings")).text
    touched = _li_for(page, "guard drops the flag")
    untouched = _li_for(page, "footer leak")
    assert "Место тронуто после отчёта" in touched
    assert "Место не тронуто после отчёта" in untouched
    assert "Место тронуто после отчёта" not in untouched
    assert "Место не тронуто после отчёта" not in touched


async def test_queue_unknown_is_never_untouched(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: Path
):
    """#1042 AC-4: no clone / missing sha stay «ответа нет», never untouched (#762)."""
    clone = _init_repo(tmp_path / "queue-unknown")
    baseline = _sha(clone)

    no_clone_id = await _task_on_clone(db, clone, title="queue no clone")
    await _report_on(db, no_clone_id, generation=1, sha=baseline)
    await _current_generation(db, no_clone_id)
    empty = await repo.create_project(
        db, slug="queue-empty-ws", name="empty", workspace_path="", status="active"
    )
    await repo.update_task(db, no_clone_id, project_id=empty)

    no_sha_id = await _task_on_clone(db, clone, title="queue no sha")
    await repo.insert_machine_review(
        db,
        task_id=no_sha_id,
        submission_generation=1,
        findings_confirmed=json.dumps([{**_FINDING_LINES, "title": "no-sha-finding"}]),
        incomplete=False,
    )
    await _current_generation(db, no_sha_id)
    await db.commit()

    page = await client.get("/findings")
    assert page.status_code == 200
    body = page.text
    assert "guard drops the flag" in body
    assert "no-sha-finding" in body
    assert "Ответа нет:" in body
    assert "Место не тронуто после отчёта" not in body


async def test_queue_evidence_never_prechecks_a_radio(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: Path
):
    """#1042 AC-5: even a touched place is not a disposition (#876)."""
    clone = _init_repo(tmp_path / "queue-no-decide")
    baseline = _sha(clone)
    task_id = await _task_on_clone(db, clone, title="queue fact is not a verdict")
    await _report_on(db, task_id, generation=1, sha=baseline)
    await _current_generation(db, task_id)
    _write_numbered(clone / _FILE, tweak=5)
    _repo_git(clone, "add", "-A")
    _repo_git(clone, "commit", "-m", "touch the lines")
    await db.commit()

    page = (await client.get("/findings")).text
    assert "Место тронуто после отчёта" in page
    review = await repo.get_latest_machine_review(db, task_id)
    stored = await repo.list_finding_dispositions(db, int(review["id"]))
    assert stored == []
    assert 'value="fixed"' in page
    after_fixed = page.split('value="fixed"')[1][:80]
    assert "checked" not in after_fixed
    after_fp = page.split('value="false_positive"')[1][:80]
    assert "checked" not in after_fp
    after_wont = page.split('value="wont_fix"')[1][:80]
    assert "checked" not in after_wont
