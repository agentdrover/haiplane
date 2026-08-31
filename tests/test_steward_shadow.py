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
