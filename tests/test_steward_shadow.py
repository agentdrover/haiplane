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
from hub.integrations import cursor_cloud
from hub.db import fetchall
from hub.services import steward_shadow as sh
from hub.services.steward_dispatch import (
    RUN_NEVER_STARTED,
    RUN_OPEN,
    RUN_TIMEOUT,
    close_finished_runs,
    order_run,
)
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

# Провал, который НЕ означает недоступность модели: связь. Судью он не
# меняет (#1182), поэтому в тестах, проверяющих отказ старта, стоит именно он.
_TRANSPORT = cursor_cloud.Refusal(detail="соединение оборвалось")

# Канал доставки идентичности появится своей задачей (#1084 для ревьюера —
# отдельная работа); здесь он подменяется, чтобы тесты проверяли СТАРТ, а не
# отсутствие канала. Отсутствие канала проверяется своим тестом ниже.
_DELIVERY = "код доступа: ABC-123"


@pytest.fixture
def with_identity(monkeypatch):
    """Канал доставки идентичности стал настоящим в #1120.

    Здесь он подменяется, потому что эти тесты про СТАРТ: минтить живой код
    им незачем. Его ОТСУТСТВИЕ проверяется своим тестом ниже, а сам канал —
    в tests/test_steward_identity.py.
    """

    async def _delivery(_db, _task_id, _generation, _base_url):
        return _DELIVERY

    monkeypatch.setattr(sh, "identity_delivery", _delivery)


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
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
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
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
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
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
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
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
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
        "hub.integrations.cursor_cloud.create_agent_attempt",
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
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(None, _TRANSPORT)),
    ):
        assert await start_due_runs(db) == 0

    run = (await _runs(db, task_id))[0]
    assert run["status"] == RUN_OPEN, "заказ обязан остаться открытым"
    assert run["agent_id"] == "", "замок снят — следующий тик попробует снова"
    refusals = await _events(db, "steward_run_refused")
    assert refusals and json.loads(refusals[-1]["payload"])["retryable"] is True

    # И следующий тик действительно стартует, когда провайдер ожил.
    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
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
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
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
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
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
    from hub.services.steward_dispatch import RUN_JUDGED, close_run

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


_CAPACITY = cursor_cloud.Refusal(
    status=400, code="usage_limit_exceeded", detail="Usage-based pricing required"
)


async def test_a_capacity_refusal_moves_to_the_next_model(
    db: aiosqlite.Connection, with_identity, monkeypatch
):
    """AC-1: «эта модель недоступна» переводит прогон на замену, а не тратит слот.

    Наблюдённый случай 06.09.2026: провайдер отвечал usage_limit_exceeded,
    хаб повторял ТУ ЖЕ модель каждые 32 секунды семнадцать минут, и слот
    истёк. Замена существовала и проходила гейт — её просто некому было
    выбрать.
    """
    monkeypatch.setattr(config, "STEWARD_MODEL_FALLBACKS", ("composer-2.5",))
    project_id = await _project(db, "shadow-fallback")
    task_id = await _task(db, project_id)
    await order_run(db, task_id, 1)

    attempts: list[str] = []

    async def _attempt(**kwargs):
        attempts.append(kwargs["model_id"])
        if kwargs["model_id"] == "gpt-5.3-codex":
            return None, _CAPACITY
        return _CREATED, None

    with patch("hub.integrations.cursor_cloud.create_agent_attempt", new=_attempt):
        assert await start_due_runs(db) == 1

    assert attempts == ["gpt-5.3-codex", "composer-2.5"], (
        "основной пробуется первым, замена — только после отказа по недоступности"
    )
    run = (await _runs(db, task_id))[0]
    assert run["model"] == "composer-2.5", (
        "строка прогона называет того, кто СУДИЛ, — по ней считает надзор F7"
    )


async def test_the_list_is_walked_past_a_second_refusal(
    db: aiosqlite.Connection, with_identity, monkeypatch
):
    """Перечень замен — перечень, а не одна запасная (находка ревью №249).

    Раскол адъюдикации: цикл выглядит прямолинейно, но ни один тест не
    заставлял ПЕРВУЮ замену тоже отказать, и мутация «break после первой
    замены» оставалась зелёной. Перечень, из которого проверена одна
    позиция, — одна запасная модель с видом списка; лимит у провайдера
    приходит сразу ко всем моделям одного тарифа, так что второй отказ
    подряд — обычный случай, а не экзотический.
    """
    monkeypatch.setattr(
        config, "STEWARD_MODEL_FALLBACKS", ("composer-2.5", "gemini-3.1-pro")
    )
    project_id = await _project(db, "shadow-fallback-second")
    task_id = await _task(db, project_id)
    await order_run(db, task_id, 1)

    attempts: list[str] = []

    async def _attempt(**kwargs):
        attempts.append(kwargs["model_id"])
        # Предел заходов: перебор без памяти о пробованных не падал бы, а
        # ВИС — в CI это повешенный прогон вместо названного дефекта.
        assert len(attempts) <= 4, f"перебор не кончается: {attempts}"
        if kwargs["model_id"] == "gemini-3.1-pro":
            return _CREATED, None
        return None, _CAPACITY

    with patch("hub.integrations.cursor_cloud.create_agent_attempt", new=_attempt):
        assert await start_due_runs(db) == 1

    assert attempts == ["gpt-5.3-codex", "composer-2.5", "gemini-3.1-pro"], (
        "перебор идёт по порядку предпочтения и не останавливается на первой "
        "замене: каждая пробуется ровно раз"
    )
    run = (await _runs(db, task_id))[0]
    assert run["model"] == "gemini-3.1-pro"


async def test_an_exhausted_list_stops_instead_of_looping(
    db: aiosqlite.Connection, with_identity, monkeypatch
):
    """Оборотная сторона: перебор конечен, и каждая модель пробуется однажды.

    Без этого «идти дальше по списку» превращается в круг по нему же —
    деньги тратятся на повтор того, что уже отказало.
    """
    monkeypatch.setattr(
        config, "STEWARD_MODEL_FALLBACKS", ("composer-2.5", "gemini-3.1-pro")
    )
    project_id = await _project(db, "shadow-fallback-exhausted")
    task_id = await _task(db, project_id)
    await order_run(db, task_id, 1)

    attempts: list[str] = []

    async def _attempt(**kwargs):
        attempts.append(kwargs["model_id"])
        # Предел заходов: перебор без памяти о пробованных не падал бы, а
        # ВИС — в CI это повешенный прогон вместо названного дефекта.
        assert len(attempts) <= 4, f"перебор не кончается: {attempts}"
        return None, _CAPACITY

    with patch("hub.integrations.cursor_cloud.create_agent_attempt", new=_attempt):
        assert await start_due_runs(db) == 0

    assert attempts == ["gpt-5.3-codex", "composer-2.5", "gemini-3.1-pro"]
    run = (await _runs(db, task_id))[0]
    # Заказ остаётся ОТКРЫТЫМ: лимит провайдера — состояние временное, а
    # UNIQUE(task_id, generation, kind) сжёг бы единственное суждение
    # поколения окончательно.
    assert run["status"] == RUN_OPEN
    assert run["agent_id"] == ""


async def test_a_same_family_substitute_is_skipped(
    db: aiosqlite.Connection, with_identity, monkeypatch
):
    """AC-2: экономия не покупается снятием гейта разнообразия.

    Ревьюер на этой сдаче — grok, и замена grok прошла бы «успешно», дав
    судью, наследующего слепые зоны того, кого он судит. Проверяется
    ПОРЯДОК вызовов: однофамилец не должен быть даже опробован, иначе
    правило работает постфактум и уже потратило деньги.
    """
    monkeypatch.setattr(config, "STEWARD_MODEL_FALLBACKS", ("grok-4.6", "composer-2.5"))
    project_id = await _project(db, "shadow-samefamily")
    task_id = await _task(db, project_id, reviewer="grok-4.6")
    await order_run(db, task_id, 1)

    attempts: list[str] = []

    async def _attempt(**kwargs):
        attempts.append(kwargs["model_id"])
        if kwargs["model_id"] == "gpt-5.3-codex":
            return None, _CAPACITY
        return _CREATED, None

    with patch("hub.integrations.cursor_cloud.create_agent_attempt", new=_attempt):
        assert await start_due_runs(db) == 1

    assert "grok-4.6" not in attempts, (
        "однофамилец ревьюера не пробуется вовсе, а не отвергается после вызова"
    )
    assert attempts == ["gpt-5.3-codex", "composer-2.5"]


async def test_a_transport_error_keeps_the_judge(
    db: aiosqlite.Connection, with_identity, monkeypatch
):
    """AC-3: оборвавшаяся связь судью НЕ меняет.

    Судья, зависящий от качества сети, невоспроизводим: завтра тот же обрыв
    даст другого судью на той же сдаче, и сравнивать суждения будет не с чем.
    Заказ остаётся открытым — следующий тик пробует ТУ ЖЕ модель.
    """
    monkeypatch.setattr(config, "STEWARD_MODEL_FALLBACKS", ("composer-2.5",))
    project_id = await _project(db, "shadow-transport")
    task_id = await _task(db, project_id)
    await order_run(db, task_id, 1)

    attempts: list[str] = []

    async def _attempt(**kwargs):
        attempts.append(kwargs["model_id"])
        return None, _TRANSPORT

    with patch("hub.integrations.cursor_cloud.create_agent_attempt", new=_attempt):
        assert await start_due_runs(db) == 0

    assert attempts == ["gpt-5.3-codex"], "замена на сетевую ошибку не берётся"
    run = (await _runs(db, task_id))[0]
    assert run["status"] == "open", "заказ остаётся открытым для следующего тика"
    assert run["model"] == "gpt-5.3-codex", "и модель в записи не подменена"


async def test_the_substitution_is_recorded(
    db: aiosqlite.Connection, with_identity, monkeypatch
):
    """AC-4: подмена названа — и в записи прогона, и в карточке, и в событии.

    Судья, которого никто не выбирал и который нигде не назван, делает
    статистику надзора ложной вернее, чем отсутствие суждения: в таблице
    2x2 суждение окажется приписано модели, не работавшей ни минуты.
    """
    monkeypatch.setattr(config, "STEWARD_MODEL_FALLBACKS", ("composer-2.5",))
    project_id = await _project(db, "shadow-recorded")
    task_id = await _task(db, project_id)
    await order_run(db, task_id, 1)

    async def _attempt(**kwargs):
        if kwargs["model_id"] == "gpt-5.3-codex":
            return None, _CAPACITY
        return _CREATED, None

    with patch("hub.integrations.cursor_cloud.create_agent_attempt", new=_attempt):
        assert await start_due_runs(db) == 1

    started = await _events(db, "steward_run_started")
    payload = json.loads(started[-1]["payload"])
    assert payload["model"] == "composer-2.5", "событие называет того, кто судит"
    assert payload["requested_model"] == "gpt-5.3-codex", (
        "и того, кого просили: без этого подмену не отличить от выбора"
    )

    updates = await fetchall(
        db, "SELECT content FROM task_updates WHERE task_id=?", (task_id,)
    )
    said = [dict(u)["content"] for u in updates]
    assert any("Судья заменён" in c and "composer-2.5" in c for c in said), (
        f"человек, читающий карточку, должен знать, кто судил и почему: {said}"
    )


async def test_no_eligible_substitute_is_an_honest_refusal(
    db: aiosqlite.Connection, with_identity, monkeypatch
):
    """Годной замены нет — отказ, а не однофамилец ради состоявшегося прогона.

    Оборотная сторона AC-2: правило, умеющее только подменять, рано или
    поздно подменит на того, кого гейт не пускает. Здесь перечень состоит
    ровно из однофамильцев сторон, и прогон обязан не состояться.
    """
    monkeypatch.setattr(config, "STEWARD_MODEL_FALLBACKS", ("grok-4.6",))
    project_id = await _project(db, "shadow-nosub")
    task_id = await _task(db, project_id, reviewer="grok-4.6")
    await order_run(db, task_id, 1)

    attempts: list[str] = []

    async def _attempt(**kwargs):
        attempts.append(kwargs["model_id"])
        return None, _CAPACITY

    with patch("hub.integrations.cursor_cloud.create_agent_attempt", new=_attempt):
        assert await start_due_runs(db) == 0

    assert attempts == ["gpt-5.3-codex"], (
        "однофамилец не пробуется даже как последний шанс"
    )
    assert (await _runs(db, task_id))[0]["status"] == "open"


async def test_a_5xx_naming_a_limit_is_still_transport(
    db: aiosqlite.Connection, with_identity, monkeypatch
):
    """Транспортный провал остаётся транспортным, даже если назвал код лимита.

    Мутация, выжившая при первом заходе: страж is_transport был написан и
    ничем не проверен — в соседнем тесте обрыв связи отсекается раньше, по
    пустому коду ошибки, и до стража дело не доходит. Случай, ради которого
    он существует, здесь: провайдер отдаёт 503 и В ТЕЛЕ называет
    usage_limit_exceeded. Судить по коду, не посмотрев на статус, значило бы
    менять судью на временном сбое — и завтра тот же сбой дал бы другого
    судью на той же сдаче.
    """
    monkeypatch.setattr(config, "STEWARD_MODEL_FALLBACKS", ("composer-2.5",))
    project_id = await _project(db, "shadow-5xx")
    task_id = await _task(db, project_id)
    await order_run(db, task_id, 1)

    flaky = cursor_cloud.Refusal(
        status=503, code="usage_limit_exceeded", detail="upstream unavailable"
    )
    attempts: list[str] = []

    async def _attempt(**kwargs):
        attempts.append(kwargs["model_id"])
        return None, flaky

    with patch("hub.integrations.cursor_cloud.create_agent_attempt", new=_attempt):
        assert await start_due_runs(db) == 0

    assert attempts == ["gpt-5.3-codex"], (
        "код лимита на пятисотке — сбой провайдера, а не недоступность модели"
    )
    assert (await _runs(db, task_id))[0]["model"] == "gpt-5.3-codex"


async def _no_dispatch(db: aiosqlite.Connection, task_id: int) -> None:
    """Убрать запись о заказе ревьюера — состояние окна гонки (#1185)."""
    await db.execute("DELETE FROM review_dispatches WHERE task_id=?", (task_id,))
    await db.commit()


async def test_a_reviewer_not_yet_dispatched_is_waited_for(
    db: aiosqlite.Connection, with_identity
):
    """#1185 AC-1: пока ревьюера не позвали, заказ ждёт, а не закрывается.

    Путь сдачи коммитит статус review и только потом зовёт ревьюера по HTTP.
    Тик, попавший в это окно, видел пустое имя модели и закрывал слот
    навсегда: наблюдено на #1175 поколения 3.
    """
    project_id = await _project(db, "shadow-race")
    task_id = await _task(db, project_id)
    await _no_dispatch(db, task_id)
    await order_run(db, task_id, 1)

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ) as started:
        assert await start_due_runs(db) == 0

    assert started.await_count == 0
    run = (await _runs(db, task_id))[0]
    # Слот ЖИВ: закрыть его — значит потерять суждение из-за порядка тиков.
    assert run["status"] == RUN_OPEN
    assert run["agent_id"] == ""

    # И когда ревьюер появляется, тот же заказ стартует без вмешательства.
    await db.execute(
        "INSERT INTO review_dispatches "
        "(task_id, submission_generation, agent_id, model, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, 1, "rev-agent", "grok-4.6", "done"),
    )
    await db.commit()
    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ) as started:
        assert await start_due_runs(db) == 1
    assert started.await_count == 1


async def test_a_dispatch_landing_mid_decision_does_not_burn_the_slot(
    db: aiosqlite.Connection, with_identity
):
    """Находка ревью №254: строка диспетча ложится МЕЖДУ чтениями.

    Сдача идёт по тому же asyncio-циклу, что и поллер, поэтому каждый
    ``await`` внутри решения отдаёт управление. Первая правка #1185 читала
    модель ревьюера снимком, потом спрашивала про строку диспетча — и если
    строка появлялась в этом промежутке, ожидание отменялось, а гейт судил
    по СТАРОЙ пустой строке и закрывал слот навсегда.

    Мои AC-тесты этого не ловили: они не перемежают INSERT с этими await.
    Здесь вставка происходит ровно внутри решения.
    """
    project_id = await _project(db, "shadow-race-interleaved")
    task_id = await _task(db, project_id)
    await _no_dispatch(db, task_id)
    await order_run(db, task_id, 1)

    original = repo.resolve_project_for_task
    landed = False

    async def _resolve_and_land(conn, tid):
        nonlocal landed
        project = await original(conn, tid)
        if not landed:
            landed = True
            await conn.execute(
                "INSERT INTO review_dispatches "
                "(task_id, submission_generation, agent_id, model, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (tid, 1, "rev-agent", "grok-4.6", "running"),
            )
            await conn.commit()
        return project

    with (
        patch("hub.repository.resolve_project_for_task", new=_resolve_and_land),
        patch(
            "hub.integrations.cursor_cloud.create_review_agent",
            new=AsyncMock(return_value=_CREATED),
        ),
    ):
        await start_due_runs(db)

    assert landed, "подставка не сработала — тест не проверил то, ради чего написан"
    run = (await _runs(db, task_id))[0]
    # Слот НЕ сожжён: ревьюер существует, и решение по устаревшему снимку
    # закрыло бы поколение навсегда — UNIQUE(task_id, generation, kind).
    assert run["status"] != RUN_REFUSED, run["closed_reason"]

    # И следующий тик доводит дело до конца, уже видя настоящую модель.
    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ) as started:
        await start_due_runs(db)
    assert started.await_count == 1
    assert (await _runs(db, task_id))[0]["agent_id"] == "agent-1"


async def test_an_unrecognised_reviewer_still_closes_the_slot(
    db: aiosqlite.Connection, with_identity
):
    """#1185 AC-2: ожидание не ослабляет гейт монокультуры.

    Ждут ОТСУТСТВИЯ заказа. Заказ, который есть, а модель в нём не
    опознаётся, — это ответ «не знаю, кто судил», и он по-прежнему
    окончательный отказ: отсрочка касается момента вопроса, а не ответа.
    """
    project_id = await _project(db, "shadow-race-garbage")
    task_id = await _task(db, project_id, reviewer="my-model-42")
    await order_run(db, task_id, 1)

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ) as started:
        assert await start_due_runs(db) == 0

    assert started.await_count == 0
    run = (await _runs(db, task_id))[0]
    assert run["status"] == RUN_REFUSED
    assert REFUSED_UNDECLARED_MODEL in run["closed_reason"]

    # Имя может прийти и из отчёта, когда строки диспетча нет вовсе: ревьюер
    # уже отработал, ждать его «появления» бессмысленно, а имя непонятно.
    named_id = await _task(db, project_id)
    await _no_dispatch(db, named_id)
    await order_run(db, named_id, 1)
    await db.execute(
        "INSERT INTO machine_reviews "
        "(task_id, submission_generation, model, submitted_by) "
        "VALUES (?, ?, ?, ?)",
        (named_id, 1, "my-model-42", "rev"),
    )
    await db.commit()
    assert await start_due_runs(db) == 0
    named = (await _runs(db, named_id))[0]
    assert named["status"] == RUN_REFUSED
    assert REFUSED_UNDECLARED_MODEL in named["closed_reason"]

    # И семейное совпадение тоже закрывает, а не ждёт.
    other_id = await _task(db, project_id, reviewer="gpt-5.2")
    await order_run(db, other_id, 1)
    assert await start_due_runs(db) == 0
    assert (await _runs(db, other_id))[0]["closed_reason"].startswith(
        REFUSED_SAME_FAMILY_REVIEWER
    )


async def test_waiting_for_a_reviewer_still_ends(
    db: aiosqlite.Connection, with_identity
):
    """#1185 AC-3: ожидание ограничено дедлайном слота, вечных нет.

    Ревьюер может не появиться никогда — провайдер отказал, диспетч выключен
    после заказа. Открытый слот тогда закрывает та же уборка дедлайнов, что
    и всегда: у ожидания нет собственного таймера, и заводить второй было бы
    вторым источником правды о том же сроке.
    """
    project_id = await _project(db, "shadow-race-forever")
    task_id = await _task(db, project_id)
    await _no_dispatch(db, task_id)
    await order_run(db, task_id, 1)

    # Срок ставится ДО тика ожидания: поставь его после — тест починил бы
    # собственную проверку и не заметил ожидания, которое двигает дедлайн.
    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now','-1 minute') "
        "WHERE task_id=?",
        (task_id,),
    )
    await db.commit()

    assert await start_due_runs(db) == 0
    assert (await _runs(db, task_id))[0]["status"] == RUN_OPEN

    assert await close_finished_runs(db) == 1
    run = (await _runs(db, task_id))[0]
    # Заказ ждал ревьюера и не начинался — с #1181 это отдельный исход, а не
    # таймаут судьи: обвинять того, кто не работал, статистика не должна.
    assert run["status"] == RUN_NEVER_STARTED


async def test_waiting_needs_a_project_that_asks_for_review(
    db: aiosqlite.Connection, with_identity
):
    """#1185 AC-1, вторая половина: ждут не всегда, а только когда есть кого.

    На проекте, где кросс-модельного ревью нет вовсе, ревьюер не появится
    ни через минуту, ни через час. Ожидание там — не осторожность, а слот,
    открытый до дедлайна ради заведомо пустого места.
    """
    project_id = await _project(db, "shadow-race-no-review")
    await db.execute(
        "UPDATE projects SET gate_policy=? WHERE id=?",
        (json.dumps({"verdict": "human", "review": "off"}), project_id),
    )
    await db.commit()
    task_id = await _task(db, project_id)
    await _no_dispatch(db, task_id)
    await order_run(db, task_id, 1)

    assert await start_due_runs(db) == 0
    run = (await _runs(db, task_id))[0]
    assert run["status"] == RUN_REFUSED
    assert REFUSED_UNDECLARED_MODEL in run["closed_reason"]


async def test_the_window_belongs_to_whoever_holds_the_claim(
    db: aiosqlite.Connection, with_identity, monkeypatch
):
    """#1181: рабочее окно достаётся только захватившему слот.

    Между меткой захвата и подтверждением лежит вызов провайдера — секунды,
    в которые строку может занять кто-то другой. Запись, ставящая окно
    ОТДЕЛЬНО от agent_id, отдала бы его тому, кто слот не брал, и надзор
    считал бы судью, которого никто не выбирал. Условие agent_id=claim в
    той же записи — это и есть принадлежность.
    """
    # Окна РАЗВЕДЕНЫ намеренно: при равных умолчаниях пересчёт даёт ровно то
    # же значение, что стояло при заказе, и проверка «дедлайн не сдвинулся»
    # слепа — мутация с отдельной записью проходила бы незамеченной.
    monkeypatch.setattr(config, "STEWARD_RUN_DEADLINE_MIN", 90)
    project_id = await _project(db, "shadow-stolen-claim")
    task_id = await _task(db, project_id)
    order = await order_run(db, task_id, 1)
    assert order is not None
    before = dict(
        (await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (order["id"],)))[0]
    )["deadline_at"]

    async def _steal_then_answer(**_kwargs):
        # Пока провайдер «думает», слот уводят.
        await db.execute(
            "UPDATE steward_runs SET agent_id='bc-thief' WHERE id=?", (order["id"],)
        )
        await db.commit()
        return _CREATED, None

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt", new=_steal_then_answer
    ):
        await start_due_runs(db)

    row = dict(
        (await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (order["id"],)))[0]
    )
    assert row["agent_id"] == "bc-thief", "чужой захват не перезаписывается"
    assert row["deadline_at"] == before, (
        "окно ушло тому, кто слот не брал — записи разошлись"
    )


async def test_the_working_deadline_starts_when_work_does(
    db: aiosqlite.Connection, with_identity, monkeypatch
):
    """#1181 AC-1: судья получает полное окно с минуты, когда начал.

    Проверяется НАСТОЯЩИМ путём старта, а не записью, сделанной руками в
    тесте: первая версия этого теста ставила захват сама и потому не
    замечала, есть пересчёт в рабочем коде или нет.

    Заказ здесь уже просрочен по своему ожиданию — и всё равно стартует,
    потому что выборка смотрит на статус и пустой agent_id, а не на срок.
    Если окно отмеряется от работы, уборка его больше не закроет.
    """
    monkeypatch.setattr(config, "STEWARD_RUN_DEADLINE_MIN", 30)
    project_id = await _project(db, "shadow-window")
    task_id = await _task(db, project_id)
    order = await order_run(db, task_id, 1)
    assert order is not None
    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now','-1 minute') WHERE id=?",
        (order["id"],),
    )
    await db.commit()

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ):
        assert await start_due_runs(db) == 1

    assert await close_finished_runs(db) == 0, (
        "по старому правилу окно уже истекло бы: оно текло, пока заказ ждал"
    )
    run = (await _runs(db, task_id))[0]
    assert run["status"] == RUN_OPEN
    assert run["agent_id"] == "agent-1"


# ---------------------------------------------------------------------------
# Реплей истории сдач (#1167)
# ---------------------------------------------------------------------------

_SHA = "c" * 40


class _HistoricalGitOps:
    """Клон, в котором ветки уже нет, а коммит есть.

    Ровно то состояние, из-за которого наивная сборка выродилась бы в
    эскалации: ``head_sha`` и ``fetch_base`` по ИМЕНИ ветки не отвечают,
    ``commit_exists`` по sha — отвечает, и ``branch_diff_paths`` принимает
    sha там, где живой сборщик передавал бы имя.
    """

    def __init__(self, paths: list[str], *, commit_here: bool = True) -> None:
        self.paths = paths
        self.commit_here = commit_here
        self.asked = []

    async def commit_exists(self, repo, sha):
        return self.commit_here

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        self.asked.append(branch)
        # Ветки нет: спросили по имени — ответа нет. Спросили по коммиту — есть.
        if branch != _SHA:
            return None
        return list(self.paths)

    async def head_sha(self, repo, base):
        return ""

    async def fetch_base(self, repo, base):
        return (False, "ветка удалена после мержа")

    async def branch_ci_runs(self, branch, repo=None, gh_repo=None, forge=""):
        return None


async def _historical_project(db: aiosqlite.Connection, slug: str) -> int:
    project_id = await repo.create_project(
        db, slug=slug, name=slug, workspace_path="/tmp/ws", status="active"
    )
    await db.execute(
        "UPDATE projects SET default_branch='develop' WHERE id=?", (project_id,)
    )
    await db.commit()
    return project_id


async def _historical_submission(
    db: aiosqlite.Connection,
    project_id: int,
    *,
    human_verdict: str,
    areas: list[str] | None = None,
    raw_count: int = 4,
    confirmed: str = "[]",
    tokens: int = 1000,
) -> int:
    """Завершённая сдача: закреплённый sha, отчёт, зелёный CI, вердикт."""
    areas = areas if areas is not None else ["hub/services/steward_shadow.py"]
    task_id = await repo.create_task(
        db,
        title="историческая сдача",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="done",
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
        submission_sha=_SHA,
        submission_model="claude-opus-5",
        risk_class="R2",
        affected_areas=json.dumps(areas),
        # Ветка удалена после мержа — имя осталось, ref не резолвится.
        branch=f"task-{task_id}/gone",
    )
    review_id = await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        model="grok-4.6",
        raw_count=raw_count,
        findings_confirmed=confirmed,
        tokens_spent=tokens,
        profile="deep",
    )
    await db.execute(
        "UPDATE machine_reviews SET provider_tokens=? WHERE id=?",
        (tokens, review_id),
    )
    await repo.upsert_ci_run_report(
        db,
        task_id=task_id,
        head_sha=_SHA,
        ac_results="{}",
        validation_status="pass",
        validation_log="",
        reason="",
        reported_by="ci",
    )
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor="Denis",
        payload={"submission_generation": 1, "verdict": human_verdict},
    )
    # Вердикт должен лежать ПОЗЖЕ всего остального: рубеж пакета — его метка.
    await db.execute(
        "UPDATE events SET created_at=datetime('now', '+1 hour') "
        "WHERE task_id=? AND kind='review_verdict_recorded'",
        (task_id,),
    )
    await db.commit()
    return task_id


async def test_offline_replay_makes_no_provider_calls(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167 AC-1: отчёт с таблицей 2x2 и НИ ОДНОГО обращения к провайдеру.

    Проверяется по коду, а не по описанию: клиент провайдера подменён на
    счётчик, и он обязан остаться нулевым.
    """
    from hub.integrations.registry import plugins

    project_id = await _historical_project(db, "replay-free")
    monkeypatch.setattr(
        plugins, "git_ops", _HistoricalGitOps(["hub/services/steward_shadow.py"])
    )
    await _historical_submission(db, project_id, human_verdict="approved")
    await _historical_submission(
        db, project_id, human_verdict="changes_requested", confirmed='[{"title": "x"}]'
    )

    calls = AsyncMock(return_value=({}, None))
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", calls)
    usage = AsyncMock(return_value={})
    monkeypatch.setattr(cursor_cloud, "get_usage", usage)

    entries = await sh.collect_corpus(db, days=60)
    cases, excluded = await sh.build_cases(db, entries)
    report = sh.replay(cases, window_days=60, excluded=excluded)

    assert calls.await_count == 0
    assert usage.await_count == 0
    assert report.provider_calls == 0
    # Таблица 2x2 непуста и различает клетки, а не суммирует их.
    assert report.table.judged == 2
    assert report.table.both_approve == 1
    assert report.table.both_changes == 1
    assert report.table.false_approve == 0
    text = sh.render_report(report)
    assert "Таблица 2x2" in text
    assert "Размер выборки" in text
    assert "обращений к провайдеру за этот прогон: 0" in text


async def test_historical_packet_rejects_post_submission_events(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167 AC-2: строка моложе рубежа роняет сборку, а не въезжает в пакет.

    Утечка тут не абстрактная: пакет, собранный из сегодняшнего состояния,
    несёт человеческий вердикт, и реплей над ним мерил бы списывание.
    """
    from hub.integrations.registry import plugins
    from hub.services.steward_evidence import PacketLeak, build_historical_packet

    project_id = await _historical_project(db, "replay-leak")
    monkeypatch.setattr(
        plugins, "git_ops", _HistoricalGitOps(["hub/services/steward_shadow.py"])
    )
    task_id = await _historical_submission(db, project_id, human_verdict="approved")

    rows = await fetchall(
        db,
        "SELECT created_at FROM events WHERE task_id=? AND "
        "kind='review_verdict_recorded'",
        (task_id,),
    )
    cutoff = dict(rows[0])["created_at"]

    # До рубежа собирается.
    packet = await build_historical_packet(db, task_id, 1, cutoff)
    assert packet.fact("machine_review_report").is_present
    # И не несёт человеческого вердикта ни одним полем.
    assert packet.brief is None

    # Отчёт, дописанный ПОСЛЕ вердикта, — это будущее. Сборка обязана упасть.
    await db.execute(
        "UPDATE machine_reviews SET created_at=datetime('now', '+2 hours') "
        "WHERE task_id=?",
        (task_id,),
    )
    await db.commit()
    with pytest.raises(PacketLeak) as leak:
        await build_historical_packet(db, task_id, 1, cutoff)
    assert leak.value.source == "machine_review_report"


async def test_historical_packet_rebuilt_from_sha_not_branch(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167 AC-3: ветки нет, а факты о коде present — потому что спросили sha.

    Наивная сборка спросила бы имя ветки, получила бы absent на обоих фактах,
    и вся историческая выборка выродилась бы в эскалации-артефакты.
    """
    from hub.integrations.registry import plugins
    from hub.services.steward_evidence import CorpusExclusion, build_historical_packet

    project_id = await _historical_project(db, "replay-sha")
    git = _HistoricalGitOps(["hub/services/steward_shadow.py"])
    monkeypatch.setattr(plugins, "git_ops", git)
    task_id = await _historical_submission(db, project_id, human_verdict="approved")
    rows = await fetchall(
        db,
        "SELECT created_at FROM events WHERE task_id=? AND "
        "kind='review_verdict_recorded'",
        (task_id,),
    )
    cutoff = dict(rows[0])["created_at"]

    packet = await build_historical_packet(db, task_id, 1, cutoff)
    tip = packet.fact("branch_tip")
    surface = packet.fact("diff_vs_areas")
    assert tip.is_present and surface.is_present
    assert tip.value["tip"] == _SHA
    # Восстановление названо восстановлением: «вершина не двигалась» здесь
    # не наблюдение, и пакет об этом говорит.
    assert tip.value["reconstructed"] is True
    assert tip.value["observed"] is False
    # Дифф спрашивали по коммиту, а не по имени ветки.
    assert git.asked == [_SHA]

    # Коммита нет — сдача ВЫБЫВАЕТ с причиной, а не входит с пустыми фактами.
    monkeypatch.setattr(
        plugins,
        "git_ops",
        _HistoricalGitOps(["x"], commit_here=False),
    )
    with pytest.raises(CorpusExclusion) as gone:
        await build_historical_packet(db, task_id, 1, cutoff)
    assert gone.value.reason == "sha_unresolved"

    # И третий выход: задача пересдана, поле submission_sha перезаписано, и
    # закреплённый коммит уже про ДРУГОЙ код. Судить по нему вердикт о
    # первой генерации — та же утечка будущего, только через поле задачи.
    monkeypatch.setattr(plugins, "git_ops", git)
    await repo.update_task(
        db, task_id, submission_generation=2, submission_sha="d" * 40
    )
    with pytest.raises(CorpusExclusion) as stale:
        await build_historical_packet(db, task_id, 1, cutoff)
    assert stale.value.reason == "sha_other_generation"


async def test_historical_rows_do_not_clear_act_refusals(db: aiosqlite.Connection):
    """#1167 AC-4: сколько ни залей истории — sample_too_small остаётся.

    Защитный отказ не снимается данными, под которые он не проектировался
    (#585). Механизм — отдельный kind, а не фильтр: строку, которой
    ``shadow_table`` не видит по своему же запросу, нельзя зачесть забыв
    дописать условие.
    """
    project_id = await _historical_project(db, "replay-refusal")
    before = await sh.act_refusals(db)
    assert {code for code, _ in before} == {
        sh.REASON_SAMPLE_TOO_SMALL,
        sh.REASON_NO_SAMPLE,
    }

    for _ in range(40):
        task_id = await _historical_submission(
            db, project_id, human_verdict="changes_requested"
        )
        await sh.record_historical_judgement(db, task_id, 1, "changes_requested")

    after = await sh.act_refusals(db)
    assert {code for code, _ in after} == {
        sh.REASON_SAMPLE_TOO_SMALL,
        sh.REASON_NO_SAMPLE,
    }, "исторические строки сняли отказ — ровно то, что запрещено #585"
    # При этом они СЧИТАЮТСЯ и видны отдельно.
    historical = await sh.historical_table(db)
    assert historical.both_changes == 40
    assert (await sh.shadow_table(db)).judged == 0


async def test_replay_report_omits_share_on_small_sample(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167 AC-5: на недоборе доля не печатается вовсе, а называется причина."""
    from hub.integrations.registry import plugins

    project_id = await _historical_project(db, "replay-small")
    monkeypatch.setattr(
        plugins, "git_ops", _HistoricalGitOps(["hub/services/steward_shadow.py"])
    )
    await _historical_submission(db, project_id, human_verdict="approved")

    entries = await sh.collect_corpus(db, days=60)
    cases, excluded = await sh.build_cases(db, entries)
    small = sh.render_report(sh.replay(cases, excluded=excluded))
    assert "НЕ ПЕЧАТАЕТСЯ" in small
    assert f"меньше порога {sh.REPLAY_MIN_SAMPLE}" in small
    assert "Доля эскалаций: 0%" not in small

    empty = sh.render_report(sh.replay([], excluded=[]))
    # Пустая выборка и малая выборка — РАЗНЫЕ ответы, а не один.
    assert "не измерена" in empty
    assert "НЕ ПЕЧАТАЕТСЯ" not in empty


async def test_replay_is_deterministic(db: aiosqlite.Connection, monkeypatch):
    """#1167 AC-6: один корпус и одна политика — побайтово один отчёт."""
    from hub.integrations.registry import plugins

    project_id = await _historical_project(db, "replay-determinism")
    monkeypatch.setattr(
        plugins, "git_ops", _HistoricalGitOps(["hub/services/steward_shadow.py"])
    )
    for verdict in ("approved", "changes_requested", "approved"):
        await _historical_submission(db, project_id, human_verdict=verdict)

    first_entries = await sh.collect_corpus(db, days=60)
    first_cases, first_excluded = await sh.build_cases(db, first_entries)
    first = sh.render_report(sh.replay(first_cases, excluded=first_excluded))

    second_entries = await sh.collect_corpus(db, days=60)
    second_cases, second_excluded = await sh.build_cases(db, second_entries)
    second = sh.render_report(sh.replay(second_cases, excluded=second_excluded))

    assert first == second

    # И бэкфилл без потолка не стартует, назвав цену вместо умолчания.
    refused = sh.plan_backfill(first_cases, 0)
    assert refused.refused
    assert refused.runs == 0
    planned = sh.plan_backfill(first_cases, 2)
    assert planned.runs == 2
    assert planned.tokens_estimate == 2 * sh.BACKFILL_TOKENS_PER_RUN
