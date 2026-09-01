"""Исполнитель прогона стюарда: кто берёт заказ и когда не берёт (#1105).

Проверяется не «стартует ли агент», а два правила, ради которых старт
выделен в отдельную работу: разнородность семейств проверяется ДО обращения
к провайдеру, и один заказ порождает ровно один прогон.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.services import steward_shadow as sh
from hub.services.steward_dispatch import RUN_OPEN, order_run
from hub.services.steward_shadow import (
    EVENT_RUN_STARTED,
    REFUSED_SAME_FAMILY_IMPLEMENTER,
    REFUSED_SAME_FAMILY_REVIEWER,
    REFUSED_UNDECLARED_MODEL,
    RUN_REFUSED,
    family_refusal,
    start_due_runs,
    start_run,
)

_CREATED = {"agent": {"id": "agent-1"}, "run": {"id": "run-1"}}

# Канал доставки идентичности появится своей задачей (#1084 для ревьюера —
# отдельная работа); здесь он подменяется, чтобы тесты проверяли СТАРТ, а не
# отсутствие канала. Отсутствие канала проверяется своим тестом ниже.
_DELIVERY = "код доступа: ABC-123"


@pytest.fixture
def with_identity(monkeypatch):
    monkeypatch.setattr(
        sh, "identity_delivery", lambda _task_id, _generation: _DELIVERY
    )


@pytest.fixture(autouse=True)
def shadow_mode(monkeypatch):
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 20)
    monkeypatch.setattr(config, "STEWARD_RUN_DEADLINE_MIN", 30)
    monkeypatch.setattr(config, "STEWARD_MODEL", "gpt-5.3-codex")
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", "steward-token")
    # Ключ провайдера нужен только чтобы дойти до места, где решается вопрос
    # этой задачи: без него отказ был бы «не настроено», а не «семейства».
    monkeypatch.setattr(config, "CURSOR_API_KEY", "cursor-key")


async def _project(db: aiosqlite.Connection, slug: str) -> int:
    project_id = await repo.create_project(
        db, slug=slug, name=slug, workspace_path="", status="active"
    )
    await db.execute(
        "UPDATE projects SET gate_policy=?, repo=? WHERE id=?",
        (json.dumps({"verdict": "steward"}), "agentdrover/haiplane", project_id),
    )
    await db.commit()
    return project_id


async def _task(
    db: aiosqlite.Connection,
    project_id: int,
    *,
    implementer: str = "claude-opus-5",
    reviewer: str = "grok-4.6",
) -> int:
    task_id = await repo.create_task(
        db,
        title="сдача на суд",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="review",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(
        db,
        task_id,
        project_id=project_id,
        submission_generation=1,
        submission_sha="a" * 40,
        submission_model=implementer,
        branch=f"task-{task_id}/work",
    )
    if reviewer:
        await db.execute(
            "INSERT INTO review_dispatches "
            "(task_id, submission_generation, agent_id, model, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, 1, "rev-agent", reviewer, "done"),
        )
    await db.commit()
    return task_id


async def _runs(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE task_id=?", (task_id,))
    return [dict(r) for r in rows]


async def _events(db: aiosqlite.Connection, kind: str) -> list[dict]:
    rows = await fetchall(db, "SELECT * FROM events WHERE kind=?", (kind,))
    return [dict(r) for r in rows]


async def test_open_order_starts_one_run(db: aiosqlite.Connection, with_identity):
    """#1105 AC-1: открытый заказ превращается в прогон, и это видно в фиде.

    До этой задачи заказы доживали до дедлайна и закрывались run_timeout —
    очередь в пустоту.
    """
    project_id = await _project(db, "shadow-start")
    task_id = await _task(db, project_id)
    await order_run(db, task_id, 1)

    with patch(
        "hub.integrations.cursor_cloud.create_review_agent",
        new=AsyncMock(return_value=_CREATED),
    ) as started:
        assert await start_due_runs(db) == 1

    assert started.await_count == 1
    kwargs = started.await_args.kwargs
    assert kwargs["model_id"] == "gpt-5.3-codex"
    assert kwargs["reviewer_token"] == "steward-token"
    # Пакет — единственный вход: в промпте стоит дверь, а не обход в репозиторий.
    assert "steward-evidence" in kwargs["prompt_text"]

    run = (await _runs(db, task_id))[0]
    assert run["status"] == RUN_OPEN
    assert run["agent_id"] == "agent-1"
    events = await _events(db, EVENT_RUN_STARTED)
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["model"] == "gpt-5.3-codex"
    assert payload["implementer_model"] == "claude-opus-5"
    assert payload["reviewer_model"] == "grok-4.6"


async def test_three_family_rule_refuses_run(db: aiosqlite.Connection, monkeypatch):
    """#1105 AC-2: судья одного семейства с исполнителем или ревьюером не стартует.

    Проверка стоит ДО вызова провайдера: после вызова деньги потрачены, а
    отказ после старта — это отказ, за который уже заплатили.
    """
    monkeypatch.setattr(config, "STEWARD_MODEL", "claude-opus-5")
    project_id = await _project(db, "shadow-family")
    task_id = await _task(db, project_id, implementer="claude-opus-5")
    await order_run(db, task_id, 1)

    with patch(
        "hub.integrations.cursor_cloud.create_review_agent",
        new=AsyncMock(return_value=_CREATED),
    ) as started:
        assert await start_due_runs(db) == 0

    assert started.await_count == 0, "провайдер не должен быть вызван вовсе"
    run = (await _runs(db, task_id))[0]
    assert run["status"] == RUN_REFUSED
    assert REFUSED_SAME_FAMILY_IMPLEMENTER in run["closed_reason"]

    # И зеркальный случай: то же семейство, что у ревьюера.
    assert (
        family_refusal("grok-4.6", "claude-opus-5", "grok-4.6")[0]
        == REFUSED_SAME_FAMILY_REVIEWER
    )


async def test_missing_declaration_is_not_diversity(db: aiosqlite.Connection):
    """#1105 AC-3: отсутствующая или неопознанная декларация — отказ.

    Дыра #1008 в другом месте: незнакомая строка сравнивалась с известной
    моделью, давала False и читалась как «разные семейства». Здесь такого
    ответа нет вовсе — «не могу сказать» никогда не было основанием идти.
    """
    project_id = await _project(db, "shadow-undeclared")
    task_id = await _task(db, project_id, implementer="", reviewer="")
    await order_run(db, task_id, 1)

    with patch(
        "hub.integrations.cursor_cloud.create_review_agent",
        new=AsyncMock(return_value=_CREATED),
    ) as started:
        assert await start_due_runs(db) == 0

    assert started.await_count == 0
    run = (await _runs(db, task_id))[0]
    assert run["status"] == RUN_REFUSED
    assert REFUSED_UNDECLARED_MODEL in run["closed_reason"]
    # Выдуманная строка — тоже отсутствие данных, а не третье семейство.
    assert (
        family_refusal("gpt-5.3-codex", "my-model-42", "grok-4.6")[0]
        == REFUSED_UNDECLARED_MODEL
    )


async def test_run_starts_at_most_once_per_order(
    db: aiosqlite.Connection, with_identity
):
    """#1105 AC-4: повторный тик не плодит второй прогон.

    Поллер тикает каждые тридцать секунд, пока заказ стоит, так что «стартуй
    ещё раз» — обычный случай. Второй прогон это второе оплаченное суждение
    об одном и том же коммите.
    """
    project_id = await _project(db, "shadow-once")
    task_id = await _task(db, project_id)
    await order_run(db, task_id, 1)

    with patch(
        "hub.integrations.cursor_cloud.create_review_agent",
        new=AsyncMock(return_value=_CREATED),
    ) as started:
        assert await start_due_runs(db) == 1
        assert await start_due_runs(db) == 0
        assert await start_due_runs(db) == 0

    assert started.await_count == 1
    runs = await _runs(db, task_id)
    assert len(runs) == 1

    # И гонка, а не только повтор: два тика, прочитавшие заказ ДО записи
    # замка, приходят к старту с одинаковым снимком. Выигрывает один.
    stale = dict(runs[0])
    stale["agent_id"] = ""
    with patch(
        "hub.integrations.cursor_cloud.create_review_agent",
        new=AsyncMock(return_value={"agent": {"id": "agent-2"}, "run": {}}),
    ) as raced:
        assert await start_run(db, stale) is False

    # Находка ревью #172: раньше проигравший ОПЛАЧИВАЛ второго агента и
    # бросал его — с живым токеном и открытой дверью. Замок берётся до
    # обращения к провайдеру, поэтому проигравший до него не доходит.
    assert raced.await_count == 0, "проигравший не должен платить провайдеру"
    after = await _runs(db, task_id)
    assert len(after) == 1
    assert after[0]["agent_id"] == "agent-1", "первый старт не перезаписан"
    assert len(await _events(db, EVENT_RUN_STARTED)) == 1


# ---------------------------------------------------------------------------
# Находки ревью сдачи #1 (grok-4.6, отчёт 172)
# ---------------------------------------------------------------------------


async def test_a_transient_failure_leaves_the_order_open(
    db: aiosqlite.Connection, with_identity
):
    """Моргание провайдера не сжигает единственный шанс этой сдачи.

    UNIQUE(task_id, generation, kind) значит, что закрытый заказ уже никогда
    не будет размещён заново. Значит закрывать его на сетевой ошибке —
    решать судьбу ревью подбрасыванием монетки от беты Cursor.
    """
    project_id = await _project(db, "shadow-transient")
    task_id = await _task(db, project_id)
    await order_run(db, task_id, 1)

    with patch(
        "hub.integrations.cursor_cloud.create_review_agent",
        new=AsyncMock(return_value=None),
    ):
        assert await start_due_runs(db) == 0

    run = (await _runs(db, task_id))[0]
    assert run["status"] == RUN_OPEN, "заказ обязан остаться открытым"
    assert run["agent_id"] == "", "замок снят — следующий тик попробует снова"
    refusals = await _events(db, "steward_run_refused")
    assert refusals and json.loads(refusals[-1]["payload"])["retryable"] is True

    # И следующий тик действительно стартует, когда провайдер ожил.
    with patch(
        "hub.integrations.cursor_cloud.create_review_agent",
        new=AsyncMock(return_value=_CREATED),
    ):
        assert await start_due_runs(db) == 1
    assert (await _runs(db, task_id))[0]["agent_id"] == "agent-1"


async def test_missing_config_does_not_burn_the_slot(
    db: aiosqlite.Connection, monkeypatch
):
    """Не настроено сейчас — не значит «не будет настроено никогда».

    Ключ появляется на хосте drop-in'ом за минуту; заказ, сожжённый в эту
    минуту, не вернуть.
    """
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", "")
    project_id = await _project(db, "shadow-unconfigured")
    task_id = await _task(db, project_id)
    await order_run(db, task_id, 1)

    with patch(
        "hub.integrations.cursor_cloud.create_review_agent",
        new=AsyncMock(return_value=_CREATED),
    ) as started:
        assert await start_due_runs(db) == 0

    assert started.await_count == 0
    run = (await _runs(db, task_id))[0]
    assert run["status"] == RUN_OPEN
    assert run["agent_id"] == ""


async def test_no_identity_channel_means_no_paid_run(db: aiosqlite.Connection):
    """Без канала доставки идентичности прогон не запускается вовсе.

    Cursor отбрасывает mcpServers (#1084), поэтому агент, стартовавший без
    одноразового кода, не прочитает пакет и не сдаст суждение — он просто
    доживёт до дедлайна. Платить за немого агента незачем, и это отказ, а не
    оптимизм.
    """
    project_id = await _project(db, "shadow-no-identity")
    task_id = await _task(db, project_id)
    await order_run(db, task_id, 1)

    with patch(
        "hub.integrations.cursor_cloud.create_review_agent",
        new=AsyncMock(return_value=_CREATED),
    ) as started:
        assert await start_due_runs(db) == 0

    assert started.await_count == 0, "провайдер не вызывается без канала"
    run = (await _runs(db, task_id))[0]
    assert run["status"] == RUN_OPEN, "заказ ждёт канала, а не сгорает"
    refusals = await _events(db, "steward_run_refused")
    assert json.loads(refusals[-1]["payload"])["reason"] == "no_identity_channel"


async def test_reviewer_model_reads_this_generation(db: aiosqlite.Connection):
    """Декларация ревьюера берётся по генерации заказа, а не по последней.

    Пересдача создаёт более новый отчёт; заказ прошлой генерации остаётся
    открытым. Чтение latest-of-task возвращало пустую строку — то есть
    «модель не объявлена» — и навсегда закрывало заказ, у которого своя
    декларация лежала в той же таблице.
    """
    project_id = await _project(db, "shadow-generation")
    task_id = await _task(db, project_id, reviewer="")
    await repo.insert_machine_review(
        db, task_id=task_id, submission_generation=1, model="grok-4.6", incomplete=False
    )
    await repo.insert_machine_review(
        db, task_id=task_id, submission_generation=2, model="", incomplete=False
    )
    await db.commit()

    assert await sh.reviewer_model(db, task_id, 1) == "grok-4.6"


# ---------------------------------------------------------------------------
# #1106 — рекомендация видна, слот закрывается
# ---------------------------------------------------------------------------


async def _judge(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    verdict: str = "approve",
    generation: int = 1,
    confidence: str = "high",
) -> None:
    """Суждение приходит контрактом #1022 — тем же путём, что у живого прогона."""
    from hub.config import TokenIdentity
    from hub.models import StewardJudgementSubmit
    from hub.services.steward_judgement import record_steward_judgement

    await record_steward_judgement(
        db,
        task_id,
        StewardJudgementSubmit(
            generation=generation,
            kind="verdict",
            verdict=verdict,
            confidence=confidence,
            escalate_reason="precondition_failed" if verdict == "escalate" else None,
            model="gpt-5.3-codex",
        ),
        TokenIdentity("steward-bot", "steward", principal_id=42),
    )


async def test_judgement_closes_the_slot(db: aiosqlite.Connection):
    """#1106 AC-1: суждение закрывает заказ, ради которого его ждали.

    Иначе слот доживает до дедлайна и закрывается как run_timeout: работа
    сделана, а состояние говорит «ждём». Разница стоит дважды — суточный
    потолок считает занятый слот, и дверь пакета (#1075) остаётся открытой
    на заказе, который никто не исполняет.
    """
    from hub.services.steward_dispatch import RUN_JUDGED

    project_id = await _project(db, "shadow-judged")
    task_id = await _task(db, project_id)
    await order_run(db, task_id, 1)

    await _judge(db, task_id)

    run = (await _runs(db, task_id))[0]
    assert run["status"] == RUN_JUDGED
    assert run["closed_reason"], "закрытие обязано называть причину"
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "review", "суждение не двигает задачу — это F4"


async def test_card_shows_it_as_a_recommendation(db: aiosqlite.Connection, client):
    """#1106 AC-2: человек видит рекомендацию там, где принимает решение.

    И видит, что это РЕКОМЕНДАЦИЯ: блок, который читается как вердикт,
    превращает теневую фазу в тихое делегирование.
    """
    project_id = await _project(db, "shadow-card")
    task_id = await _task(db, project_id)
    await _judge(db, task_id, verdict="changes_requested")

    page = await client.get(f"/tasks/{task_id}")

    assert page.status_code == 200
    body = page.text
    assert "рекомендация стюарда" in body
    assert "changes_requested" in body
    assert "gpt-5.3-codex" in body
    assert "Решение остаётся человеческим" in body


async def test_shadow_never_transitions(db: aiosqlite.Connection):
    """#1106 AC-3: ни один вердикт в тени не двигает задачу и не пишет вердикт.

    Проверяется тестом, а не обещанием: approve — самый опасный случай, он и
    стоит первым.
    """
    project_id = await _project(db, "shadow-no-transition")
    for verdict in ("approve", "changes_requested", "escalate"):
        task_id = await _task(db, project_id)
        before = dict(await repo.get_task(db, task_id))

        await _judge(
            db,
            task_id,
            verdict=verdict,
            confidence="high" if verdict != "escalate" else "medium",
        )

        after = dict(await repo.get_task(db, task_id))
        assert after["status"] == before["status"] == "review", verdict
        assert after["review_verdict"] is None, verdict
        assert after["review_verdict_generation"] is None, verdict


# ---------------------------------------------------------------------------
# Находки ревью сдачи #1 (grok-4.6, отчёт 179)
# ---------------------------------------------------------------------------


async def test_the_deadline_never_overwrites_a_judgement(db: aiosqlite.Connection):
    """Дедлайн закрывает только НЕЗАВЕРШЁННОЕ (находка high).

    Поллер читает открытые слоты, потом обходит их по одному — и суждение
    успевает лечь в этот зазор. Запись по одному id позволяла дедлайну
    затереть уже вынесенный вердикт: ответ лежал в строке, а состояние
    говорило «прогон не ответил».
    """
    from hub.services.steward_dispatch import RUN_JUDGED, RUN_TIMEOUT, close_run

    project_id = await _project(db, "shadow-deadline-race")
    task_id = await _task(db, project_id)
    run = await order_run(db, task_id, 1)
    assert run is not None

    # Суждение пришло...
    await _judge(db, task_id)
    # ...а поллер держит снимок, снятый ДО него, и дошёл до дедлайна.
    stale = dict(run)
    applied = await close_run(
        db, stale, RUN_TIMEOUT, "прогон не вернул суждение до дедлайна слота"
    )

    assert applied is False, "закрытие закрытого слота не применяется"
    after = (await _runs(db, task_id))[0]
    assert after["status"] == RUN_JUDGED, "суждение обязано пережить дедлайн"
    assert "суждение записано" in after["closed_reason"]


async def test_a_stale_deadline_sweep_leaves_the_verdict_alone(
    db: aiosqlite.Connection,
):
    """То же самое через настоящий свип, а не через прямой вызов.

    Свип — это путь, которым дедлайн срабатывает в проде; проверять только
    close_run значило бы проверить деталь и не проверить дорогу.
    """
    from hub.services.steward_dispatch import RUN_JUDGED, close_finished_runs

    project_id = await _project(db, "shadow-sweep-race")
    task_id = await _task(db, project_id)
    run = await order_run(db, task_id, 1)
    await db.execute(
        "UPDATE steward_runs SET deadline_at = datetime('now', '-1 minute') WHERE id=?",
        (run["id"],),
    )
    await db.commit()
    await _judge(db, task_id)

    closed = await close_finished_runs(db)

    assert closed == 0, "закрывать было нечего: слот уже судим"
    assert (await _runs(db, task_id))[0]["status"] == RUN_JUDGED


async def test_empty_grounds_are_not_shown_as_a_list(db: aiosqlite.Connection, client):
    """Пустые основания читаются как отсутствие, а не как «[]» (находка low).

    Раскрывашка с пустым JSON-массивом внутри выглядит как содержимое,
    которого нет, — и человек на гейте видит «основания» там, где их не
    приложили.
    """
    project_id = await _project(db, "shadow-empty-grounds")
    task_id = await _task(db, project_id)
    await _judge(db, task_id)

    page = await client.get(f"/tasks/{task_id}")

    assert page.status_code == 200
    assert "оснований не приложено" in page.text
    assert "[]" not in page.text


# ---------------------------------------------------------------------------
# #1107 — таблица 2x2 и пороги
# ---------------------------------------------------------------------------


async def _pair(
    db: aiosqlite.Connection,
    project_id: int,
    *,
    steward: str,
    human: str | None,
    generation: int = 1,
) -> int:
    """Одна пара «что сказал бы стюард» / «что сделал человек»."""
    task_id = await _task(db, project_id)
    await repo.update_task(db, task_id, submission_generation=generation)
    await repo.insert_steward_judgement(
        db,
        task_id=task_id,
        generation=generation,
        kind="verdict",
        submitted_verdict=steward,
        verdict=steward,
        confidence="high",
        escalate_reason="precondition_failed" if steward == "escalate" else "",
        grounds="[]",
        findings="[]",
        closures="[]",
        model="gpt-5.3-codex",
        tokens_spent=None,
        duration_ms=None,
        submitted_by="steward-bot",
        principal_id=42,
    )
    if human is not None:
        await repo.insert_event(
            db,
            kind="review_verdict_recorded",
            task_id=task_id,
            actor="denis",
            payload={"verdict": human, "submission_generation": generation},
        )
    await db.commit()
    return task_id


async def test_two_by_two_counts_false_approve_apart(db: aiosqlite.Connection):
    """#1107 AC-1: четыре клетки считаются верно, false-approve — отдельно.

    Он не «одно из расхождений»: это единственная неприемлемая ошибка, и
    спрятанная внутри общего несогласия она перестаёт быть видимой.
    """
    from hub.services.steward_shadow import shadow_table

    project_id = await _project(db, "shadow-table")
    await _pair(db, project_id, steward="approve", human="approved")
    await _pair(db, project_id, steward="approve", human="changes_requested")
    await _pair(db, project_id, steward="changes_requested", human="approved")
    await _pair(db, project_id, steward="changes_requested", human="changes_requested")
    await _pair(db, project_id, steward="escalate", human="approved")
    await _pair(db, project_id, steward="approve", human=None)

    table = await shadow_table(db)

    assert table.both_approve == 1
    assert table.steward_approve_human_changes == 1
    assert table.steward_changes_human_approve == 1
    assert table.both_changes == 1
    assert table.escalated == 1
    # Суждение без человеческого вердикта — «данных нет», а не согласие.
    assert table.unpaired == 1
    assert table.false_approve == 1
    assert table.human_changes == 2


async def test_act_refused_until_thresholds_met(db: aiosqlite.Connection, monkeypatch):
    """#1107 AC-2: маленькая выборка и false-approve не пускают в act.

    Отказ называет недобранный критерий: «не готово» без имени нечем
    закрывать.
    """
    from hub.services.steward_shadow import (
        REASON_FALSE_APPROVE,
        REASON_SAMPLE_TOO_SMALL,
        act_refusals,
        effective_mode,
    )

    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    project_id = await _project(db, "shadow-thresholds")
    await _pair(db, project_id, steward="approve", human="changes_requested")

    codes = {code for code, _ in await act_refusals(db)}

    assert REASON_SAMPLE_TOO_SMALL in codes
    assert REASON_FALSE_APPROVE in codes
    assert await effective_mode(db) == "shadow", "act не выдаётся по просьбе"
    details = {code: detail for code, detail in await act_refusals(db)}
    assert "сто любых сдач" in details[REASON_SAMPLE_TOO_SMALL]


async def test_stamping_is_refused_too(db: aiosqlite.Connection, monkeypatch):
    """#1107 AC-3: нижняя граница коридора такая же жёсткая, как верхняя.

    Судья, который не эскалирует никогда, согласен со всем подряд — то есть
    штампует. По верхней границе его бы поймали, по нижней раньше нет.
    """
    from hub.services.steward_shadow import (
        REASON_OVER_ESCALATING,
        REASON_STAMPING,
        act_refusals,
    )

    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    project_id = await _project(db, "shadow-stamp")
    # Достаточная выборка, ноль false-approve, ноль эскалаций.
    for _ in range(10):
        await _pair(
            db, project_id, steward="changes_requested", human="changes_requested"
        )

    codes = {code for code, _ in await act_refusals(db)}
    assert REASON_STAMPING in codes, "штамповка обязана отказывать"

    # И зеркально: судья, эскалирующий почти всё, тоже не проходит.
    loud = await _project(db, "shadow-loud")
    for _ in range(30):
        await _pair(db, loud, steward="escalate", human="approved")
    codes_loud = {code for code, _ in await act_refusals(db)}
    assert REASON_OVER_ESCALATING in codes_loud


async def test_act_is_granted_when_the_numbers_allow(
    db: aiosqlite.Connection, monkeypatch
):
    """Пороги не только запрещают: выполненные — пропускают.

    Проверка, которая умеет только отказывать, неотличима от выключателя.
    """
    from hub.services.steward_shadow import act_refusals, effective_mode

    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    project_id = await _project(db, "shadow-ready")
    for _ in range(10):
        await _pair(
            db, project_id, steward="changes_requested", human="changes_requested"
        )
    for _ in range(2):
        await _pair(db, project_id, steward="escalate", human="approved")

    assert await act_refusals(db) == []
    assert await effective_mode(db) == "act"


# ---------------------------------------------------------------------------
# Находки ревью сдачи #1 (grok, отчёт 187)
# ---------------------------------------------------------------------------


def test_act_cannot_be_reached_without_the_measurement():
    """Синхронный читатель режима НИКОГДА не отдаёт act (находка high).

    Пороги, стоящие в стороне от выключателя, — это не пороги, а справка:
    поставил STEWARD_MODE=act, и контур пошёл бы работать, ни разу их не
    спросив. Теперь autonomy выдаёт только effective_mode, которому нужна
    база; забывший про него потребитель получает сегодняшнее поведение.
    """
    import hub.services.steward_dispatch as sd

    for asked in ("act", "ACT", " act "):
        sd.config.STEWARD_MODE = asked
        assert sd.configured_mode() == "act", "сырое значение читается как есть"
        assert sd.requested_mode() == "shadow", "но синхронно act не выдаётся"
        assert sd.steward_mode() == "shadow", "включая старое имя функции"
    sd.config.STEWARD_MODE = "off"


async def test_no_reader_compares_the_mode_to_act_directly(db: aiosqlite.Connection):
    """Ни один потребитель не сравнивает режим с act мимо стража.

    Перечислением, а не примером: это тот же приём, которым закрыт пин
    (#1120) — границу, которую нельзя пересчитать, нельзя и удержать.
    """
    from pathlib import Path

    hub_dir = Path(__file__).resolve().parents[1] / "hub"
    offenders = []
    for path in hub_dir.rglob("*.py"):
        if path.name == "steward_shadow.py":
            continue  # тут и живёт единственный законный читатель
        for number, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or '"""' in stripped:
                continue
            if '== "act"' not in stripped and "== 'act'" not in stripped:
                continue
            # Понижение — не чтение: строка, которая сравнивает с act, чтобы
            # ВЕРНУТЬ shadow, и есть тот самый колпак. Всё остальное —
            # действие по автономии, которую никто не измерял.
            if '"shadow"' in stripped:
                continue
            offenders.append(f"{path.name}:{number}")
    assert not offenders, f"act читается мимо effective_mode: {offenders}"


async def test_a_policy_verdict_is_not_a_human_one(db: aiosqlite.Connection):
    """Автовердикт политики не попадает в числитель таблицы (находка medium).

    auto_verdict (#745) пишет то же событие под actor='policy'. Засчитанный
    как человеческий, он превращает таблицу в измерение согласия с
    автоматикой — ровно то, что она должна проверять.
    """
    from hub.services.steward_shadow import shadow_table

    project_id = await _project(db, "shadow-policy")
    task_id = await _pair(db, project_id, steward="approve", human=None)
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor="policy",
        payload={"verdict": "changes_requested", "submission_generation": 1},
    )
    await db.commit()

    table = await shadow_table(db)

    assert table.false_approve == 0, "подпись политики не человеческая"
    assert table.unpaired == 1, "и это отсутствие пары, а не согласие"


async def test_an_empty_sample_has_no_escalation_share(db: aiosqlite.Connection):
    """Пустая выборка — «не измерено», а не ноль (находка medium).

    Ноль это результат («судья не эскалирует никогда»), и подставленный
    вместо отсутствия он читается как обвинение в штамповке там, где
    измерять было нечего.
    """
    from hub.services.steward_shadow import REASON_NO_SAMPLE, act_refusals, shadow_table

    table = await shadow_table(db)

    assert table.judged == 0
    assert table.escalation_share is None
    codes = {code for code, _ in await act_refusals(db)}
    assert REASON_NO_SAMPLE in codes


async def test_the_refusal_is_written_once_per_reason_set(
    db: aiosqlite.Connection, monkeypatch
):
    """Отказ пишется при СМЕНЕ причин, а не на каждый вызов (находка medium).

    Поллер тикает каждые тридцать секунд; строка на тик — это фид, в котором
    больше нечего прочитать.
    """
    from hub.services.steward_shadow import EVENT_ACT_REFUSED, effective_mode

    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    project_id = await _project(db, "shadow-quiet")
    await _pair(db, project_id, steward="approve", human="changes_requested")

    for _ in range(5):
        assert await effective_mode(db) == "shadow"

    events = await _events(db, EVENT_ACT_REFUSED)
    assert len(events) == 1, f"ожидалась одна запись, получено {len(events)}"

    # Меняется состав причин — появляется вторая запись.
    for _ in range(10):
        await _pair(
            db, project_id, steward="changes_requested", human="changes_requested"
        )
    assert await effective_mode(db) == "shadow"
    assert len(await _events(db, EVENT_ACT_REFUSED)) == 2


async def test_the_table_stands_beside_practice_metrics(db: aiosqlite.Connection):
    """Таблица видна там же, где остальные числа практики (находка medium).

    Метрика в собственном углу — метрика, которую не читают: решение об
    автономии принимают рядом с override-rate и исходами ревью.
    """
    from hub.services.orchestration import practice_metrics

    project_id = await _project(db, "shadow-metrics")
    await _pair(db, project_id, steward="approve", human="changes_requested")

    metrics = await practice_metrics(db, since_days=90)

    block = metrics["steward_shadow"]
    assert block["false_approve"] == 1
    assert block["act_ready"] is False
    assert any(item["reason"] == "false_approve" for item in block["act_refusals"])
