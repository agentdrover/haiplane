"""Autopilot daily digest and sampling audit (#739).

One digest per project per UTC day of autopilot activity; empty days stay
silent; the sample is deterministic; the spot-check flows back into the
human_gates metric as the ``audit`` gate.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import aiosqlite
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.services.digest import deterministic_sample, generate_due_digests
from hub.services.orchestration import practice_metrics


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


async def _autopilot_project(db: aiosqlite.Connection, slug: str) -> tuple[int, int]:
    """(project_id, feature_id) — a project with dor=auto and a hierarchy."""
    pid = await repo.create_project(db, slug=slug, name=slug.title())
    await repo.update_project(db, pid, gate_policy=json.dumps({"dor": "auto"}))
    epic = await _node(db, title="epic", task_type="epic", parent_id=None)
    await repo.update_task(db, epic, project_id=pid)
    feature = await _node(db, title="feature", task_type="feature", parent_id=epic)
    await db.commit()
    return pid, feature


async def _policy_approved_task(
    db: aiosqlite.Connection, feature_id: int, title: str
) -> int:
    task_id = await _node(db, title=title, task_type="task", parent_id=feature_id)
    await repo.insert_event(
        db,
        kind="task_approved",
        task_id=task_id,
        actor="policy",
        payload={"auto": True, "risk_class": "R0"},
    )
    await db.commit()
    return task_id


def _tomorrow() -> datetime:
    return datetime.now(UTC) + timedelta(days=1)


async def test_digest_content_and_empty_day(db: aiosqlite.Connection):
    # AC-1 (#739): a day with autopilot activity produces one digest with
    # the approvals, escalations and a sample; a quiet project and a quiet
    # day produce nothing.
    _pid, feature = await _autopilot_project(db, "spike-dg")
    t1 = await _policy_approved_task(db, feature, "auto one")
    t2 = await _policy_approved_task(db, feature, "auto two")
    await repo.insert_event(
        db,
        kind="verdict_escalated",
        task_id=t1,
        actor="policy",
        payload={"reason": "security-находка"},
    )
    await db.commit()
    await _autopilot_project(db, "spike-quiet")  # policy on, no activity

    created = await generate_due_digests(db, now=_tomorrow())
    assert created == 1, "only the active project gets a digest"

    rows = await repo.list_digests(db)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert {a["task_id"] for a in payload["auto_approvals"]} == {t1, t2}
    assert payload["escalations"][0]["payload"]["reason"] == "security-находка"
    assert payload["audit_sample"], "a non-empty day carries a sample"
    assert set(payload["audit_sample"]) <= {t1, t2}

    # Same day again → idempotent; the NEXT (empty) day → nothing new.
    assert await generate_due_digests(db, now=_tomorrow()) == 0
    assert await generate_due_digests(db, now=_tomorrow() + timedelta(days=1)) == 0, (
        "a day without autopilot transitions must not create a digest"
    )


async def test_digest_event_published(client: AsyncClient, db: aiosqlite.Connection):
    # AC-2 (#739): the feed gets digest_created (hub_wait_events reads the
    # same feed), and the /digests page renders the digest.
    _pid, feature = await _autopilot_project(db, "spike-ev")
    await _policy_approved_task(db, feature, "auto ev")
    await generate_due_digests(db, now=_tomorrow())

    events = await repo.list_events(db, since=0, kinds=["digest_created"], limit=10)
    assert events, "the digest must announce itself in the events feed"
    payload = json.loads(dict(events[-1])["payload"])
    assert payload["auto_approvals"] == 1
    assert payload["audit_sample"]

    page = await client.get("/digests")
    assert page.status_code == 200
    assert "spike-ev" in page.text
    assert "ждёт проверки" in page.text


async def test_audit_result_feeds_metrics(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#739): the human-only audit endpoint stores the outcome in the
    # task feed and surfaces it in human_gates as the audit gate; agents 403.
    _pid, feature = await _autopilot_project(db, "spike-audit")
    task_id = await _policy_approved_task(db, feature, "auto audited")
    await generate_due_digests(db, now=_tomorrow())
    digest = dict((await repo.list_digests(db))[0])
    sample = json.loads(digest["payload"])["audit_sample"]
    assert sample == [task_id]

    resp = await client.post(
        f"/api/digests/{digest['id']}/audit",
        json={"task_id": task_id, "result": "problem", "comment": "не то поведение"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["audit_results"] == {str(task_id): "problem"}

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    audit_notes = [u for u in updates if "Выборочный аудит" in u["content"]]
    assert audit_notes and "проблема" in audit_notes[0]["content"]

    gates = (await practice_metrics(db))["human_gates"]
    audit_rows = [g for g in gates if g["gate"] == "audit"]
    assert audit_rows and audit_rows[0]["overrides"] == 1

    # Outside the sample → refused: auditing an unsampled task would
    # fabricate coverage.
    outsider = await _policy_approved_task(db, feature, "not sampled")
    resp = await client.post(
        f"/api/digests/{digest['id']}/audit",
        json={"task_id": outsider, "result": "ok"},
    )
    assert resp.status_code == 404

    # Agent token → 403: the audit is the owner's counterpart.
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {"agent-token": TokenIdentity("bot", "agent")},
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    resp = await client.post(
        f"/api/digests/{digest['id']}/audit",
        json={"task_id": task_id, "result": "ok"},
        headers={"Authorization": "Bearer agent-token"},
    )
    assert resp.status_code == 403


def test_sample_deterministic():
    # AC-4 (#739): stable composition, ~10%, minimum one on a non-empty day.
    ids = list(range(1, 101))
    first = deterministic_sample(ids, "2026-08-20")
    second = deterministic_sample(ids, "2026-08-20")
    assert first == second
    assert 1 <= len(first) <= 25
    assert deterministic_sample([7], "2026-08-20") == [7]
    assert deterministic_sample([], "2026-08-20") == []
    other_day = deterministic_sample(ids, "2026-08-21")
    assert other_day == deterministic_sample(ids, "2026-08-21")


async def test_digest_lists_waiting_human_queue(db: aiosqlite.Connection):
    """#1020 AC-4: the digest names who is waiting on a person, and for how long.

    It rides along a digest created for other reasons, exactly as the category
    debt does (#878) — and that is a limit worth stating rather than hiding: a
    digest exists only for a delegating project on a day with autopilot
    activity, so this line is a summary, not the alarm. The alarm is the
    events feed the poller writes on every rung.
    """
    _pid, feature = await _autopilot_project(db, "spike-queue")
    approved = await _policy_approved_task(db, feature, "auto one")
    waiting = await _node(db, title="ждёт решения", task_type="task", parent_id=feature)
    await repo.update_task(db, waiting, status="needs_decision")
    await repo.record_human_queue_reminder(
        db,
        task_id=waiting,
        instance="needs_decision",
        entered_at="2026-08-01 00:00:00",
        rung="168h",
        age_minutes=10100,
    )
    await db.execute(
        "UPDATE human_queue_reminders SET created_at = datetime('now') WHERE task_id=?",
        (waiting,),
    )
    await db.commit()

    assert await generate_due_digests(db, now=_tomorrow()) == 1
    payload = json.loads((await repo.list_digests(db))[0]["payload"])

    queue = payload["human_queue"]
    assert [q["task_id"] for q in queue] == [waiting]
    assert queue[0]["rung"] == "168h"
    assert queue[0]["age_minutes"] == 10100
    assert queue[0]["title"] == "ждёт решения"
    assert approved not in [q["task_id"] for q in queue]


# ---------------------------------------------------------------------------
# #1143 — стюард виден в дайджесте (F7 #1001)
# ---------------------------------------------------------------------------


async def _steward_project(db: aiosqlite.Connection, slug: str) -> tuple[int, int]:
    """Проект, делегировавший ТОЛЬКО стюарду: ни одного значения auto."""
    pid = await repo.create_project(db, slug=slug, name=slug.title())
    await repo.update_project(db, pid, gate_policy=json.dumps({"verdict": "steward"}))
    epic = await _node(db, title="epic", task_type="epic", parent_id=None)
    await repo.update_task(db, epic, project_id=pid)
    feature = await _node(db, title="feature", task_type="feature", parent_id=epic)
    await db.commit()
    return pid, feature


async def _steward_judged_task(
    db: aiosqlite.Connection,
    feature_id: int,
    title: str,
    *,
    verdict: str = "approve",
    grounds: list[dict] | None = None,
) -> int:
    """Суждение приходит контрактом #1022 — тем же путём, что у живого прогона.

    Не самодельной строкой в таблице: дайджест читает то, что пишет реальный
    записывающий, и подделка записи проверяла бы согласие теста с собой.
    """
    from hub.config import TokenIdentity
    from hub.models import StewardJudgementSubmit
    from hub.services.steward_judgement import record_steward_judgement

    task_id = await _node(db, title=title, task_type="task", parent_id=feature_id)
    await repo.update_task(db, task_id, status="review", submission_generation=1)
    await db.commit()
    await record_steward_judgement(
        db,
        task_id,
        StewardJudgementSubmit(
            generation=1,
            kind="verdict",
            verdict=verdict,
            confidence="high",
            escalate_reason="precondition_failed" if verdict == "escalate" else None,
            grounds=grounds or [],
            model="gpt-5.3-codex",
        ),
        TokenIdentity("steward-bot", "steward", principal_id=42),
    )
    return task_id


async def test_digest_covers_steward_actions(
    db: aiosqlite.Connection, client: AsyncClient
):
    """#1143 AC-1: раздел стюарда есть, и в нём ОСНОВАНИЯ, а не только вердикт.

    Вердикт сам по себе непроверяем: «одобрено» говорит, что произошло, и
    ничего — о том, должно ли было. Поэтому проверяются оба состояния: с
    основаниями и без них, и второе обязано читаться как отсутствие, а не
    как пустой список, который шаблон считает истинным (#762).
    """
    _pid, feature = await _autopilot_project(db, "dg-steward")
    grounded = await _steward_judged_task(
        db,
        feature,
        "судил с основаниями",
        verdict="changes_requested",
        grounds=[{"source": "ci_pinned_sha", "detail": "CI на закреплённом sha упал"}],
    )
    bare = await _steward_judged_task(db, feature, "судил молча", verdict="approve")

    assert await generate_due_digests(db, now=_tomorrow()) == 1
    payload = json.loads((await repo.list_digests(db))[0]["payload"])

    section = {j["task_id"]: j for j in payload["steward_judgements"]}
    assert set(section) == {grounded, bare}, "оба суждения обязаны быть в разделе"

    assert section[grounded]["verdict"] == "changes_requested"
    assert section[grounded]["grounds_state"] == "present"
    assert section[grounded]["grounds"][0]["source"] == "ci_pinned_sha"

    assert section[bare]["grounds"] == []
    assert section[bare]["grounds_state"] == "absent", (
        "«оснований нет» — это состояние, а не пустое значение"
    )

    events = await repo.list_events(db, since=0, kinds=["digest_created"], limit=10)
    announced = json.loads(dict(events[-1])["payload"])
    assert announced["steward_judgements"] == 2

    page = await client.get("/digests")
    assert page.status_code == 200
    assert "Суждения стюарда" in page.text
    assert "Решение остаётся человеческим" in page.text
    assert "CI на закреплённом sha упал" in page.text
    assert "оснований не приложено" in page.text


async def test_steward_only_project_still_gets_a_digest(db: aiosqlite.Connection):
    """#1143 AC-2: делегирование стюарду — тоже делегирование.

    Признак делегирования искал строку "auto" среди значений gate_policy, и
    проект, отдавший стюарду вердикт и больше ничего, признавался не
    делегирующим ничего. Дайджеста для него не было вовсе — а отсутствие
    дайджеста выглядит ровно как спокойный день.
    """
    _pid, feature = await _steward_project(db, "dg-steward-only")
    judged = await _steward_judged_task(db, feature, "единственная работа дня")

    assert await generate_due_digests(db, now=_tomorrow()) == 1
    payload = json.loads((await repo.list_digests(db))[0]["payload"])
    assert [j["task_id"] for j in payload["steward_judgements"]] == [judged]
    assert payload["auto_approvals"] == [], "автопилот тут ничего не решал"


async def test_no_steward_activity_no_digest(db: aiosqlite.Connection):
    """#1143 AC-3: пустой день молчит — правило #739 не ослаблено.

    Проверяется в обе стороны: делегирующий проект без единого действия
    дайджеста не получает, и следующий (пустой) день после дня с работой —
    тоже. Отчёт, который выходит каждый день, читать перестают.
    """
    _pid, feature = await _steward_project(db, "dg-steward-quiet")
    assert await generate_due_digests(db, now=_tomorrow()) == 0

    await _steward_judged_task(db, feature, "один день работы")
    assert await generate_due_digests(db, now=_tomorrow()) == 1
    assert await generate_due_digests(db, now=_tomorrow() + timedelta(days=1)) == 0


def _steward_counter_cell(page_text: str) -> str:
    """Именно та ячейка счётчика, а не любая цифра на странице.

    У дайджеста четыре счётчика, и пустые списки соседей рисуют свои нули
    честно. Проверять «нет нуля на странице» значило бы проверять их, а не
    стюарда — первая версия этого теста так и падала.
    """
    found = re.search(r"суждений стюарда</dt>\s*<dd>(.*?)</dd>", page_text, re.S)
    assert found is not None, "счётчик суждений стюарда обязан быть на странице"
    return re.sub(r"<[^>]+>", "", found.group(1)).strip()


async def test_a_digest_written_before_the_section_still_renders(
    db: aiosqlite.Connection, client: AsyncClient
):
    """Дайджесты, лежащие в проде, ключа steward_judgements не имеют.

    Не AC, а условие выкладки: страница читается людьми каждый день, и
    новая секция не имеет права уронить старые записи. Проверяется тем же
    способом, каким это случилось бы — настоящим GET, а не рассуждением о
    поведении шаблонизатора.
    """
    pid = await repo.create_project(db, slug="dg-legacy", name="Legacy")
    await repo.create_digest(
        db,
        project_id=pid,
        digest_date="2026-08-01",
        payload=json.dumps(
            {
                "date": "2026-08-01",
                "project": "dg-legacy",
                "auto_approvals": [],
                "auto_verdicts": [],
                "escalations": [],
                "deliveries": [],
                "audit_sample": [],
                "audit_results": {},
            }
        ),
    )
    await db.commit()

    page = await client.get("/digests")
    assert page.status_code == 200
    assert "dg-legacy" in page.text
    assert "Суждения стюарда" not in page.text, (
        "у записи без суждений раздела быть не должно: пустая секция "
        "читается как «стюард смотрел и ничего не нашёл»"
    )
    cell = _steward_counter_cell(page.text)
    assert "не измерялось" in cell, (
        "день до появления раздела не измерен, и счётчик обязан это сказать"
    )
    assert "0" not in cell, (
        "ноль здесь был бы утверждением о дне, в который никто не смотрел (#762, #750)"
    )


async def test_a_quiet_steward_is_a_measured_zero(
    db: aiosqlite.Connection, client: AsyncClient
):
    """Ноль и «не измерялось» обязаны отличаться в обе стороны (#1143 ревью).

    Зеркало предыдущего теста, и без него правка неотличима от «печатать
    "не измерялось" всегда»: у делегирующего проекта, где стюард за день
    не судил ничего, ноль — настоящий результат дня, а не пробел.
    """
    _pid, feature = await _autopilot_project(db, "dg-quiet-steward")
    await _policy_approved_task(db, feature, "автопилот работал, стюард нет")

    assert await generate_due_digests(db, now=_tomorrow()) == 1
    payload = json.loads((await repo.list_digests(db))[0]["payload"])
    assert payload["steward_judgements"] == [], "день измерен, суждений нет"

    page = await client.get("/digests")
    assert page.status_code == 200
    cell = _steward_counter_cell(page.text)
    assert cell == "0", (
        f"этот день измерен: ноль тут результат, а не пробел, получено {cell!r}"
    )


# ---------------------------------------------------------------------------
# #1171 — очередь находок в дайджесте: попутчик под тестом, и precision без лжи
# ---------------------------------------------------------------------------


async def _confirmed_report(
    db: aiosqlite.Connection, review_id: int, task_id: int, findings: int
) -> None:
    """Отчёт с подтверждёнными находками на ТЕКУЩЕЙ сдаче задачи."""
    await repo.update_task(db, task_id, submission_generation=1)
    await db.execute(
        "INSERT INTO machine_reviews (id, task_id, submission_generation, "
        "profile, model, findings_confirmed) VALUES (?, ?, 1, 'deep', 'grok', ?)",
        (
            review_id,
            task_id,
            json.dumps(
                [
                    {"title": f"находка {n}", "category": "correctness"}
                    for n in range(findings)
                ],
                ensure_ascii=False,
            ),
        ),
    )
    await db.commit()


async def test_digest_carries_the_findings_queue(
    db: aiosqlite.Connection, client: AsyncClient
):
    """#1171: сток находок доезжает до дайджеста — в payload и на страницу.

    Попутчик, который никто не держит, тихо отваливается: убрать ключ из
    payload, сломать порог или выкинуть блок из шаблона — и весь набор
    дайджеста остаётся зелёным, потому что смотрит на соседние секции. Сторож
    очереди #1171 задумывался «по образцу #751, в дайджест (#739)», и без
    этого теста его сводка ничем не закреплена.
    """
    _pid, feature = await _autopilot_project(db, "spike-findings")
    approved = await _policy_approved_task(db, feature, "auto one")
    await _confirmed_report(db, 900, approved, findings=3)

    with patch.object(config, "UNJUDGED_FINDINGS_ALERT_THRESHOLD", 2):
        assert await generate_due_digests(db, now=_tomorrow()) == 1
        payload = json.loads((await repo.list_digests(db))[0]["payload"])

    section = payload["findings_queue"]
    assert section["findings"] == 3, "число ждущих находок обязано доехать"
    assert section["reports"] == 1
    assert section["threshold"] == 2
    assert section["over_threshold"] is True, "три находки при пороге два — выше порога"

    page = (await client.get("/digests")).text
    assert "Находки без диспозиции" in page, "блок обязан быть на странице дайджестов"
    assert 'href="/findings"' in page, "число без пути к очереди не даёт её открыть"
    assert "3</a> подтверждённых" in page


async def test_a_queue_under_the_threshold_is_not_an_alarm(db: aiosqlite.Connection):
    """Порог живёт в секции: ниже него сводка едет, но тревогой не притворяется."""
    _pid, feature = await _autopilot_project(db, "spike-under")
    approved = await _policy_approved_task(db, feature, "auto one")
    await _confirmed_report(db, 901, approved, findings=1)

    with patch.object(config, "UNJUDGED_FINDINGS_ALERT_THRESHOLD", 40):
        assert await generate_due_digests(db, now=_tomorrow()) == 1
        section = json.loads((await repo.list_digests(db))[0]["payload"])[
            "findings_queue"
        ]
    assert section["findings"] == 1
    assert section["over_threshold"] is False


async def test_the_digest_stops_claiming_precision_is_uncomputable(
    db: aiosqlite.Connection, client: AsyncClient
):
    """#1171: «precision не считается вовсе» — только пока не разобрано НИЧЕГО.

    precision это ``real/judged`` по РАЗОБРАННЫМ, и первая же диспозиция
    делает его числом; непустая очередь рядом его не обнуляет. После
    частичного разбора — типичного состояния этой самой задачи — безусловная
    строка отправляла читателя мимо числа, которое hub_practice_metrics уже
    отдаёт: полуправда #516/#549. Ставка едет с размером выборки (#1153).
    """
    _pid, feature = await _autopilot_project(db, "spike-precision")
    approved = await _policy_approved_task(db, feature, "auto one")
    await _confirmed_report(db, 902, approved, findings=2)

    assert await generate_due_digests(db, now=_tomorrow()) == 1
    section = json.loads((await repo.list_digests(db))[0]["payload"])["findings_queue"]
    assert section["judged"] == 0
    assert section["precision"] is None
    page = (await client.get("/digests")).text
    assert "Ни одной диспозиции нет" in page
    assert "precision не считается вовсе" in page, "нулевой разбор назван честно"

    # Одна находка разобрана, вторая ждёт — очередь непуста, precision есть.
    await db.execute(
        "INSERT INTO finding_dispositions (review_id, task_id, "
        "submission_generation, finding_index, finding_uid, disposition, "
        "decided_by) VALUES (902, ?, 1, 0, 'uid-a', 'fixed', 'denis')",
        (approved,),
    )
    await db.execute("DELETE FROM autopilot_digests")
    await db.commit()

    assert await generate_due_digests(db, now=_tomorrow()) == 1
    section = json.loads((await repo.list_digests(db))[0]["payload"])["findings_queue"]
    assert section["judged"] == 1, "разобранное обязано доехать до дайджеста"
    assert section["precision"] == 1.0
    assert section["findings"] == 1, "вторая находка всё ещё ждёт — очередь непуста"

    page = (await client.get("/digests")).text
    assert "precision не считается вовсе" not in page, (
        "очередь непуста, но precision посчитан — безусловная строка была ложью"
    )
    flat = re.sub(r"\s+", " ", page)
    assert "Разобрано 1 — precision 1.0 по ним" in flat, (
        "ставка без размера выборки рядом не печатается (#1153)"
    )
    assert "окно 90 дней" in flat, (
        "precision без имени окна читается как «за всё время»"
    )
    assert section["precision_window_days"] == 90, (
        "окно precision совпадает с окном страницы метрик"
    )


async def test_the_digest_does_not_call_a_judged_stock_zero(
    db: aiosqlite.Connection, client: AsyncClient
):
    """#1171: разбор СТОКА не читается как «диспозиций нет вовсе».

    ``practice_metrics`` берёт диспозиции по дате ОТЧЁТА, поэтому суждение,
    вынесенное сегодня о находке из отчёта трёхмесячной давности, в оконный
    срез не попадает. Очередь #1171 — запас за год; наутро после того, как её
    разобрали, оконное ``judged`` снова ноль, и безусловная ветка шаблона
    утверждала, что числа нет вовсе, когда хаб его уже знает. Тот же класс
    #516/#549, что закрыт для оконного случая, и тот же вход, что у CLI в
    ``test_judging_the_old_stock_is_not_read_as_zero``.
    """
    _pid, feature = await _autopilot_project(db, "spike-old-stock")
    approved = await _policy_approved_task(db, feature, "auto one")
    await _confirmed_report(db, 903, approved, findings=2)
    await db.execute(
        "UPDATE machine_reviews SET created_at=datetime('now','-100 days') WHERE id=903"
    )
    await db.execute(
        "INSERT INTO finding_dispositions (review_id, task_id, "
        "submission_generation, finding_index, finding_uid, disposition, "
        "decided_by) VALUES (903, ?, 1, 0, 'uid-old', 'fixed', 'denis')",
        (approved,),
    )
    await db.commit()

    assert await generate_due_digests(db, now=_tomorrow()) == 1
    section = json.loads((await repo.list_digests(db))[0]["payload"])["findings_queue"]
    assert section["judged"] == 0, "оконный срез не трогаем: по нему считает /metrics"
    assert section["judged_all_time"] == 1, (
        "разобранная находка старого стока обязана быть видна хоть одним числом"
    )
    assert section["precision_all_time"] == 1.0

    flat = re.sub(r"\s+", " ", (await client.get("/digests")).text)
    assert "precision не считается вовсе" not in flat, (
        "диспозиция записана, а дайджест утверждает, что числа нет вовсе"
    )
    assert "за всё время разобрано 1 — precision 1.0 по ним" in flat, (
        "ставка едет с размером выборки рядом (#1153)"
    )
    assert "За окно 90 дней диспозиций нет" in flat, (
        "оконный ноль остаётся названным: он не отменяется разбором стока"
    )


async def test_a_wholly_unjudged_queue_still_says_precision_is_uncomputable(
    db: aiosqlite.Connection, client: AsyncClient
):
    """Честность в обе стороны: когда не разобрано НИЧЕГО, так и сказано.

    Без этого теста починку предыдущего можно было бы сделать, просто убрав
    строку «precision не считается вовсе»: она правдива ровно тогда, когда ни
    за окно, ни за всё время не разобрано ни одной находки.
    """
    _pid, feature = await _autopilot_project(db, "spike-nothing")
    approved = await _policy_approved_task(db, feature, "auto one")
    await _confirmed_report(db, 904, approved, findings=2)
    await db.execute(
        "UPDATE machine_reviews SET created_at=datetime('now','-100 days') WHERE id=904"
    )
    await db.commit()

    assert await generate_due_digests(db, now=_tomorrow()) == 1
    section = json.loads((await repo.list_digests(db))[0]["payload"])["findings_queue"]
    assert section["judged_all_time"] == 0
    assert section["precision_all_time"] is None
    flat = re.sub(r"\s+", " ", (await client.get("/digests")).text)
    assert "ни за окно 90 дней, ни за всё время" in flat
    assert "precision не считается вовсе" in flat, "нулевой разбор назван честно"
