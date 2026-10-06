"""Исполнитель прогона стюарда: кто берёт заказ и когда не берёт (#1105).

Проверяется не «стартует ли агент», а два правила, ради которых старт
выделен в отдельную работу: разнородность семейств проверяется ДО обращения
к провайдеру, и один заказ порождает ровно один прогон.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

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
    EVENT_RUN_REFUSED,
    EVENT_RUN_STARTED,
    REFUSED_NOT_CONFIGURED,
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
def review_wait_elapsed(monkeypatch):
    """Страж ожидания отчёта (#1600) пропускает: потолок ожидания вышел.

    Эти тесты про ВНУТРЕННИЙ страж старта — ожидание ревьюера (#1185) и отказ
    по декларациям, — а не про окно «ревью положено, но не заказано». Без
    этого страж #1600 отвечал бы раньше них (ждать отчёта), и они проверяли
    бы не то, что заявлено. Поведение стража — в test_steward_dispatch.
    """
    monkeypatch.setattr(config, "STEWARD_REVIEW_WAIT_MAX", 0)


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


async def test_missing_declaration_is_not_diversity(
    db: aiosqlite.Connection, review_wait_elapsed
):
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
    grounds: list[dict] | None = None,
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
            grounds=[{"source": "ci_pinned_sha"}] if grounds is None else grounds,
            escalate_reason="precondition_failed" if verdict == "escalate" else None,
            model="gpt-5.3-codex",
        ),
        TokenIdentity("steward-bot", "steward", principal_id=42),
    )


# ---------------------------------------------------------------------------
# #1601 — советник-критик: общие помощники
# ---------------------------------------------------------------------------


def _advisor_identity():
    """Сессия советника: тот же принципал, другой ВИД сессии (#1601)."""
    from hub.config import TokenIdentity

    return TokenIdentity(
        "steward-bot", "steward", principal_id=42, chat_pair_kind="steward_advisor"
    )


async def _mark_started(
    db: aiosqlite.Connection,
    order: dict,
    *,
    model: str,
    packet: str = "pkt",
) -> None:
    """Прогон начат так же, как это делает старт: агент, модель, выданный пакет."""
    await db.execute(
        "UPDATE steward_runs SET agent_id=?, run_id=?, model=?, packet_hash=?, "
        "started_at=strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE id=?",
        (f"agent-{order['id']}", f"run-{order['id']}", model, packet, order["id"]),
    )
    await db.commit()


async def _judge_run(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    model: str = "gpt-5.3-codex",
    packet: str = "pkt",
    generation: int = 1,
) -> dict:
    """Заказ судьи, уже начатый на ФАКТИЧЕСКОЙ модели (после замены)."""
    order = await order_run(db, task_id, generation)
    assert order is not None
    await _mark_started(db, order, model=model, packet=packet)
    return order


async def _advisor_run(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    model: str = "claude-sonnet-5",
    packet: str = "pkt",
    generation: int = 1,
) -> dict:
    """Заказ советника, начатый: под ним советник вправе ответить."""
    from hub.services.steward_dispatch import KIND_ADVISOR

    order = await order_run(
        db,
        task_id,
        generation,
        KIND_ADVISOR,
        model=model,
        deadline_min=config.STEWARD_ADVISOR_WAIT_MAX,
    )
    assert order is not None
    await _mark_started(db, order, model=model, packet=packet)
    return order


async def _advise(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    verdict: str = "concur",
    generation: int = 1,
    confidence: str = "high",
    grounds: list[dict] | None = None,
    findings: list[dict] | None = None,
):
    """Ответ советника контрактом #1022 — тем же путём, что у живого прогона."""
    from hub.models import StewardJudgementSubmit
    from hub.services.steward_judgement import record_steward_judgement

    return await record_steward_judgement(
        db,
        task_id,
        StewardJudgementSubmit(
            generation=generation,
            kind="advisor",
            verdict=verdict,
            confidence=confidence,
            grounds=[{"source": "ci_pinned_sha"}] if grounds is None else grounds,
            findings=findings or [],
            model="claude-sonnet-5",
        ),
        _advisor_identity(),
    )


async def _pair_on_packet(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    verdict: str = "concur",
    generation: int = 1,
    judge_model: str = "gpt-5.3-codex",
    advisor_model: str = "claude-sonnet-5",
    packet: str = "pkt",
) -> None:
    """Судья одобрил, советник ответил — по настоящему контракту, под заказами.

    Хеш пакета ставит хаб на заказе при выдаче; здесь его ставит помощник
    (``_mark_started``), потому что выдача идёт HTTP-дверью, а не этим вызовом.
    """
    await _judge_run(db, task_id, model=judge_model, packet=packet)
    await _judge(db, task_id, generation=generation)
    await _advisor_run(db, task_id, model=advisor_model, packet=packet)
    await _advise(db, task_id, verdict=verdict, generation=generation)


def _same_packet(monkeypatch, value: str = "pkt") -> None:
    """Сегодняшний пакет считается равным ``value`` (как если бы он не менялся)."""
    from hub.services import steward_evidence

    async def _now(_db, _task_id, _generation):
        return value

    monkeypatch.setattr(steward_evidence, "current_packet_hash", _now)


async def _v2_row(
    db: aiosqlite.Connection,
    project_id: int,
    *,
    verdict: str,
    reason: str = "",
    advisor: str | None = None,
    generation: int = 1,
    contour: int = 2,
) -> int:
    """Одна строка выборки нового контура напрямую в таблицы (для счёта формул)."""
    task_id = await _task(db, project_id)
    await repo.update_task(db, task_id, submission_generation=generation)
    judge_id = await repo.insert_steward_judgement(
        db,
        task_id=task_id,
        generation=generation,
        kind="verdict",
        submitted_verdict=verdict,
        verdict=verdict,
        confidence="high",
        escalate_reason=reason,
        grounds="[]",
        findings="[]",
        closures="[]",
        model="gpt-5.3-codex",
        submitted_by="steward-bot",
        principal_id=42,
        packet_hash="pkt",
        contour=contour,
    )
    if advisor is not None:
        await repo.insert_steward_judgement(
            db,
            task_id=task_id,
            generation=generation,
            kind="advisor",
            submitted_verdict=advisor,
            verdict=advisor,
            confidence="high",
            grounds="[]",
            findings="[]",
            closures="[]",
            model="claude-sonnet-5",
            submitted_by="steward-bot",
            principal_id=42,
            judged_id=judge_id,
            packet_hash="pkt",
            contour=contour,
        )
    await db.commit()
    return task_id


async def _v2_sample(
    db: aiosqlite.Connection,
    project_id: int,
    *,
    concur: int = 0,
    objects: int = 0,
    changes: int = 0,
    substantive: int = 0,
    procedural: int = 0,
    procedural_reason: str = "no_current_report",
) -> list[int]:
    """Выборка по числам: возвращает номера задач, одобренных парой (concur)."""
    approved: list[int] = []
    for _ in range(concur):
        approved.append(
            await _v2_row(db, project_id, verdict="approve", advisor="concur")
        )
    for _ in range(objects):
        await _v2_row(db, project_id, verdict="approve", advisor="object")
    for _ in range(changes):
        await _v2_row(db, project_id, verdict="changes_requested")
    for _ in range(substantive):
        await _v2_row(db, project_id, verdict="escalate", reason="precondition_failed")
    for _ in range(procedural):
        await _v2_row(db, project_id, verdict="escalate", reason=procedural_reason)
    return approved


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
    # #1327: суждение без оснований хаб принимает только как эскалацию.
    await _judge(db, task_id, verdict="escalate", grounds=[])

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
    """#1107 AC-2 (v2, #1601): маленькая выборка и ошибочное одобрение не пускают в act.

    Отказ называет недобранный критерий: «не готово» без имени нечем
    закрывать. Ошибочное одобрение — человеческий возврат того, что пара
    одобрила; старое «человеческих возвратов 10» больше не критерий.
    """
    from hub.services.steward_shadow import (
        REASON_FALSE_APPROVE,
        REASON_SAMPLE_TOO_SMALL,
        act_refusals,
        effective_mode,
    )

    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    project_id = await _project(db, "shadow-thresholds")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor="denis",
        payload={"verdict": "changes_requested", "submission_generation": 1},
    )
    await db.commit()

    codes = {code for code, _ in await act_refusals(db)}

    assert REASON_SAMPLE_TOO_SMALL in codes
    assert REASON_FALSE_APPROVE in codes
    assert await effective_mode(db) == "shadow", "act не выдаётся по просьбе"
    details = {code: detail for code, detail in await act_refusals(db)}
    assert "пар судья+советник в выборке 1" in details[REASON_SAMPLE_TOO_SMALL]
    assert f"#{task_id}" in details[REASON_FALSE_APPROVE], "номер задачи назван"


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
    await _v2_sample(db, project_id, concur=18, objects=2, changes=10, substantive=5)

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
    await _v2_row(db, project_id, verdict="changes_requested")

    for _ in range(5):
        assert await effective_mode(db) == "shadow"

    events = await _events(db, EVENT_ACT_REFUSED)
    assert len(events) == 1, f"ожидалась одна запись, получено {len(events)}"

    # Меняется состав причин — появляется вторая запись.
    await _v2_sample(db, project_id, concur=20)
    assert await effective_mode(db) == "shadow"
    assert len(await _events(db, EVENT_ACT_REFUSED)) == 2


async def test_the_table_stands_beside_practice_metrics(db: aiosqlite.Connection):
    """Критерий и таблица видны там же, где остальные числа практики (находка medium).

    Метрика в собственном углу — метрика, которую не читают: решение об
    автономии принимают рядом с override-rate и исходами ревью. С #1601 наверху
    критерий выхода v2, а таблица «стюард против человека» — под своим ключом
    и подписана другой метрикой.
    """
    from hub.services.orchestration import practice_metrics

    project_id = await _project(db, "shadow-metrics")
    await _pair(db, project_id, steward="approve", human="changes_requested")
    approved = await _v2_sample(db, project_id, concur=1, objects=1, procedural=2)
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=approved[0],
        actor="denis",
        payload={"verdict": "changes_requested", "submission_generation": 1},
    )
    await db.commit()

    metrics = await practice_metrics(db, since_days=90)

    block = metrics["steward_shadow"]
    assert (block["pairs"], block["concur"], block["object"]) == (2, 1, 1)
    assert block["timeout"] == 0
    assert block["false_approve"] == 1
    assert [t["task_id"] for t in block["false_approve_tasks"]] == [approved[0]]
    assert block["procedural_escalations"] == 2, "процедурные — отдельной строкой"
    assert block["act_ready"] is False
    assert any(item["reason"] == "false_approve" for item in block["act_refusals"])
    human = block["human_table"]
    # Своя метрика и свои числа: одиночный approve с человеческим возвратом из
    # _pair и approve пары — обе клетки таблицы «стюард против человека».
    assert human["false_approve"] == 2


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
    db: aiosqlite.Connection, with_identity, review_wait_elapsed
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
    calls = 0

    async def _resolve_and_land(conn, tid):
        nonlocal landed, calls
        project = await original(conn, tid)
        calls += 1
        # Первое чтение проекта — страж ожидания отчёта (#1600), он стоит
        # впереди; строка ложится на второе — то самое, что решает судьбу
        # снимка модели внутри start_run.
        if calls == 2 and not landed:
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
            "hub.integrations.cursor_cloud.create_agent_attempt",
            new=AsyncMock(return_value=(_CREATED, None)),
        ) as premature,
    ):
        await start_due_runs(db)

    # Тик, на котором строка диспетча легла посреди решения, судью не
    # заказывает: он откладывается, а не стартует по старому снимку.
    assert premature.await_count == 0

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
    db: aiosqlite.Connection, with_identity, review_wait_elapsed
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


# ---------------------------------------------------------------------------
# #1290: ожидание ревьюера пишется один раз, а не на каждом проходе поллера
# ---------------------------------------------------------------------------


async def _tick_waiting(db: aiosqlite.Connection, times: int) -> None:
    """Прогнать поллер несколько раз, как он ходит в проде — раз в 30 секунд."""
    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ):
        for _ in range(times):
            assert await start_due_runs(db) == 0


async def test_a_waiting_refusal_is_recorded_once_not_every_tick(
    db: aiosqlite.Connection, with_identity, monkeypatch, review_wait_elapsed
):
    """#1290 AC-1: одно ожидание — одна запись, а не одна на проход.

    Измерено 22.09.2026 спайком #1269: 160 из 217 событий стюарда за вечер
    были повторами этого самого отказа, ровно по 51 на задачу за полчаса.
    Запись, которую никто не может прочитать, равна отсутствию записи.

    Молчания при этом быть не должно: первая запись обязана лечь, иначе
    ожидание становится невидимым. И смена причины обязана дать новую —
    иначе дедуп прячет уже не повтор, а новость.
    """
    project_id = await _project(db, "shadow-waiting-once")
    task_id = await _task(db, project_id)
    await _no_dispatch(db, task_id)
    await order_run(db, task_id, 1)

    await _tick_waiting(db, 5)

    assert (await _runs(db, task_id))[0]["status"] == RUN_OPEN, (
        "дедуп события не должен трогать сам слот"
    )
    events = await _events(db, EVENT_RUN_REFUSED)
    assert len(events) == 1, (
        f"пять проходов дали {len(events)} записей — это и есть шум задачи"
    )
    first = json.loads(events[0]["payload"])
    assert first["reason"] == REFUSED_UNDECLARED_MODEL
    assert first["retryable"] is True

    # Причина сменилась: ревьюер появился, но провайдера нечем звать.
    await db.execute(
        "INSERT INTO review_dispatches "
        "(task_id, submission_generation, agent_id, model, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, 1, "rev-agent", "grok-4.6", "done"),
    )
    await db.commit()
    monkeypatch.setattr(config, "CURSOR_API_KEY", "")

    await _tick_waiting(db, 3)

    events = await _events(db, EVENT_RUN_REFUSED)
    assert len(events) == 2, (
        "смена причины обязана дать новую запись — иначе дедуп вечный"
    )
    assert json.loads(events[1]["payload"])["reason"] == REFUSED_NOT_CONFIGURED


async def test_waiting_still_ends_when_the_reviewer_arrives(
    db: aiosqlite.Connection, with_identity
):
    """#1290 AC-2: дедуп записи — не отказ навсегда.

    Мутация «отказывать навсегда после первой записи» роняет этот тест:
    ожидание обязано кончиться стартом, как только ревьюер назван. «Пока
    неизвестно» не превращается в «неизвестно никогда» оттого, что про
    ожидание перестали писать в ленту.
    """
    project_id = await _project(db, "shadow-waiting-ends")
    task_id = await _task(db, project_id)
    await _no_dispatch(db, task_id)
    await order_run(db, task_id, 1)

    await _tick_waiting(db, 4)
    assert (await _runs(db, task_id))[0]["status"] == RUN_OPEN

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
    assert (await _runs(db, task_id))[0]["agent_id"] == "agent-1"


async def test_a_slot_that_waited_for_a_reviewer_says_so_when_it_closes(
    db: aiosqlite.Connection, with_identity, monkeypatch, review_wait_elapsed
):
    """#1290 AC-3: закрытие по дедлайну называет именно это ожидание.

    По журналу первого вечера нельзя было отличить «ждали ревьюера полчаса и
    не дождались» от любого другого заказа, который не начался: оба читались
    как never_started с общим текстом. Статус остаётся прежним — он отделяет
    «не начинался» от таймаута судьи, — а причина обязана назвать ожидание.
    """
    project_id = await _project(db, "shadow-waited-and-closed")
    task_id = await _task(db, project_id)
    await _no_dispatch(db, task_id)
    await order_run(db, task_id, 1)
    await _tick_waiting(db, 2)

    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now','-1 minute') "
        "WHERE task_id=?",
        (task_id,),
    )
    await db.commit()

    assert await close_finished_runs(db) == 1
    run = (await _runs(db, task_id))[0]
    assert run["status"] == RUN_NEVER_STARTED
    assert "ревьюер" in run["closed_reason"], (
        f"причина не называет ожидание ревьюера: {run['closed_reason']}"
    )

    # Контраст: заказ, который не начался по другой причине, называет её же,
    # а не ожидание ревьюера — иначе «называет причину» ничего не значит.
    other_id = await _task(db, project_id)
    await order_run(db, other_id, 1)
    monkeypatch.setattr(config, "CURSOR_API_KEY", "")
    await _tick_waiting(db, 1)
    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now','-1 minute') "
        "WHERE task_id=?",
        (other_id,),
    )
    await db.commit()

    assert await close_finished_runs(db) == 1
    other = (await _runs(db, other_id))[0]
    assert other["status"] == RUN_NEVER_STARTED
    assert "ревьюер" not in other["closed_reason"], other["closed_reason"]


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

    def __init__(
        self,
        paths: list[str],
        *,
        commit_here: bool = True,
        base: str = "develop",
        diff_empty: bool = False,
        diff_unreadable: bool = False,
        ancestor: bool | None = False,
        local_ancestor: bool | None = False,
        other_refs: tuple[str, ...] = (),
    ) -> None:
        self.paths = paths
        self.commit_here = commit_here
        #: База, которая в этом клоне вообще есть. Спросили другую — клон не
        #: отвечает, как не ответил бы живой git на неизвестное имя. Без
        #: этого стенд не отличал бы «спросили ту базу, против которой
        #: судили» от «спросили сегодняшнюю»: заглушка отдавала пути на
        #: ЛЮБУЮ базу, и подмена точки сравнения проходила зелёной.
        self.base = base
        #: Трёхточечный дифф вернул ПУСТОЙ список — топология, а не отказ.
        self.diff_empty = diff_empty
        #: Гит не ответил вовсе: ``None``, а не пустой список. Отдельный
        #: флаг, потому что отказ гита и пустой дифф — разные ответы, и
        #: заглушка обязана уметь дать каждый.
        self.diff_unreadable = diff_unreadable
        #: Лежит ли коммит в истории ``origin/<база>`` — той вершины, против
        #: которой ``branch_diff_paths`` и считает дифф. Три ответа, как у
        #: живого git: True, False и None («спросить не удалось»).
        self.ancestor = ancestor
        #: То же про ОТСТАВШИЙ локальный ref. По умолчанию False: в общем
        #: клоне хаба локальный ``develop`` годами позади ``origin/develop``
        #: (#824, #1046), и коммит, давно уехавший в origin, локальному
        #: имени не предок. Вопрос, заданный сюда вместо origin, отвечает
        #: «дифф не схлопнулся» на схлопнувшемся диффе.
        self.local_ancestor = local_ancestor
        #: Ref-ы, которые в этом клоне ЕСТЬ, но коммита в себе не несут.
        #: Нужны, чтобы отличить «спросили не тот ref, и его тут нет»
        #: (живой git отвечает ``None``, и сдача уходит к человеку) от
        #: «спросили не тот ref, а он есть»: там ответ ``False``, дыра не
        #: ставится, и схлопнувшийся дифф проходит за измеренную
        #: поверхность. Опасно именно второе, и запинить надо его.
        self.other_refs = other_refs
        self.asked = []
        self.bases = []
        self.ancestry_asked = []

    async def commit_exists(self, repo, sha):
        return self.commit_here

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        self.asked.append(branch)
        self.bases.append(base_branch)
        # Ветки нет: спросили по имени — ответа нет. Спросили по коммиту — есть.
        if branch != _SHA:
            return None
        if base_branch != self.base:
            return None
        if self.diff_unreadable:
            return None
        return [] if self.diff_empty else list(self.paths)

    async def is_ancestor(self, repo, ancestor, descendant):
        self.ancestry_asked.append(descendant)
        # Каждому ref — свой ответ. Заглушка, отвечающая одно и то же на
        # любое имя, не видит рассинхрона origin/local, а именно он и
        # превращал схлопнувшийся дифф в «ничего не менялось».
        if descendant == f"origin/{self.base}":
            return self.ancestor
        if descendant == self.base:
            return self.local_ancestor
        if descendant in self.other_refs:
            # Ref в клоне есть, коммита в себе не несёт: живой git отвечает
            # rc=1, то есть False. Именно этот ответ и опасен — он читается
            # как «дифф не схлопнулся».
            return False
        # Такого ref в клоне нет — живой git отвечает None (cat-file -e).
        return None

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

    entries, dropped = await sh.collect_corpus(db, days=60)
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

    # Третий выход: коммит генерации не записан НИГДЕ. Не «задача помнит
    # другую генерацию» — леджер сдач помнит каждую, и следующий тест это
    # проверяет; сюда попадают только сдачи без единой записи о коммите.
    monkeypatch.setattr(plugins, "git_ops", git)
    await repo.update_task(
        db, task_id, submission_generation=2, submission_sha="d" * 40
    )
    with pytest.raises(CorpusExclusion) as stale:
        await build_historical_packet(db, task_id, 1, cutoff)
    assert stale.value.reason == "sha_unrecorded"


async def test_resubmitted_task_keeps_its_own_generation_sha(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167: возврат человеком почти всегда влечёт пересдачу — и корпус
    обязан пережить её, иначе он теряет ровно ту половину, ради которой
    существует.

    tasks.submission_sha — одно поле, пересдача его перезаписывает. Леджер
    сдач (#880) помнит каждую генерацию, и коммит берётся оттуда: тем же
    путём восстанавливает sha finding_evidence. Тест ведёт сдачу через
    collect_corpus → build_cases, а не только через сборщик: подмена
    аргумента в проводке вернула бы утечку при зелёном юните.
    """
    from hub.integrations.registry import plugins

    project_id = await _historical_project(db, "replay-resubmit")
    git = _HistoricalGitOps(["hub/services/steward_shadow.py"])
    monkeypatch.setattr(plugins, "git_ops", git)
    task_id = await _historical_submission(db, project_id, human_verdict="approved")

    # Леджер помнит коммит первой генерации; задача уже пересдана и помнит
    # третий, а второй сдачи не было вовсе.
    await db.execute(
        "INSERT INTO submissions (task_id, generation, sha, base_branch) "
        "VALUES (?, 1, ?, 'develop')",
        (task_id, _SHA),
    )
    await repo.update_task(
        db, task_id, submission_generation=3, submission_sha="e" * 40
    )
    # И у пересдачи есть свой, более поздний отчёт. «Последний отчёт задачи»
    # описывал бы именно его — то есть другой код.
    later = await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=3,
        model="grok-4.6",
        raw_count=9,
        findings_confirmed='[{"title": "находка о другом коде"}]',
    )
    await db.commit()

    entries, dropped = await sh.collect_corpus(db, days=60)
    cases, excluded = await sh.build_cases(db, entries)

    assert excluded == [], f"сдача выброшена зря: {excluded}"
    assert len(cases) == 1
    # Механизм зафиксирован ЧИСЛАМИ, а не отсутствием исключения: дифф
    # спрошен по коммиту ПЕРВОЙ генерации, и отчёт взят её же, а не поздний.
    assert git.asked == [_SHA]
    report = cases[0].packet.fact("machine_review_report")
    assert report.is_present, "отчёт своей генерации потерян — корпус выродится"
    assert report.value["review_id"] != later
    assert report.value["generation"] == 1
    assert cases[0].packet.fact("branch_tip").value["tip"] == _SHA

    # А ВОТ КАРТОЧКА не восстановима, и пакет об этом говорит, вместо того
    # чтобы сверить дифф первой генерации с областями, дописанными на
    # третьей: расширенный набор ответил бы «в заявленном» мягче правды.
    surface = cases[0].packet.fact("diff_vs_areas")
    risk = cases[0].packet.fact("risk_class")
    assert surface.is_absent and surface.reason == "card_not_recorded"
    assert risk.is_absent and risk.reason == "card_not_recorded"
    # Декларация модели — из того же одного поля, и для прошлой генерации
    # она тоже чужая; корпус её не копирует.
    assert cases[0].entry.implementer_model == ""

    # Исход — эскалация, и она СЧИТАЕТСЯ ОТДЕЛЬНО: «политика вывела к
    # человеку» и «нам нечем было судить» — разные утверждения.
    replayed = sh.replay(cases, excluded=excluded)
    assert replayed.table.escalated == 1
    assert replayed.card_not_recorded == 1
    assert "карточка сдачи не сохранена" in sh.render_report(replayed)


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

    entries, dropped = await sh.collect_corpus(db, days=60)
    cases, excluded = await sh.build_cases(db, entries)
    small = sh.render_report(sh.replay(cases, excluded=excluded))
    assert "НЕ ПЕЧАТАЕТСЯ" in small
    assert f"меньше порога {sh.REPLAY_MIN_SAMPLE}" in small
    assert "Доля эскалаций: 0%" not in small
    # Правило одно на ВСЕ доли отчёта, а не на ту, о которой вспомнили.
    # Доля исключённых печаталась процентом на выборке из одной сдачи —
    # счётчик честен, процент рядом с ним читается как измерение.
    assert "%" not in small, small
    assert "Исключено из корпуса: 0" in small

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

    first_entries, dropped = await sh.collect_corpus(db, days=60)
    first_cases, first_excluded = await sh.build_cases(db, first_entries)
    first = sh.render_report(sh.replay(first_cases, excluded=first_excluded))

    second_entries, dropped = await sh.collect_corpus(db, days=60)
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


async def test_corpus_names_every_submission_it_drops(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167: знаменатель, из которого молча вычли, — не измерение (#516).

    events.task_id намеренно без внешнего ключа: событие переживает задачу.
    Прежняя редакция делала на таком вердикте continue, и сдача исчезала и
    из корпуса, и из списка исключённых — «доля от всех сдач окна»
    занижалась молча.
    """
    from hub.integrations.registry import plugins

    project_id = await _historical_project(db, "replay-dropped")
    monkeypatch.setattr(
        plugins, "git_ops", _HistoricalGitOps(["hub/services/steward_shadow.py"])
    )
    kept = await _historical_submission(db, project_id, human_verdict="approved")
    orphan = await _historical_submission(db, project_id, human_verdict="approved")
    await db.execute("DELETE FROM tasks WHERE id=?", (orphan,))
    await db.commit()

    entries, dropped = await sh.collect_corpus(db, days=60)

    assert [e.task_id for e in entries] == [kept]
    assert [(d.task_id, d.reason) for d in dropped] == [(orphan, "no_task")]

    # И доезжает до отчёта: склейка живёт в библиотеке, а не у вызывающего.
    _, excluded = await sh.build_cases(db, entries, dropped)
    assert [(e.task_id, e.reason) for e in excluded] == [(orphan, "no_task")]
    assert "no_task: 1" in sh.render_report(
        sh.replay(await _cases_only(db, entries), excluded=excluded)
    )


async def _cases_only(db: aiosqlite.Connection, entries):
    cases, _ = await sh.build_cases(db, entries)
    return cases


async def test_card_gap_counter_counts_outcomes_not_holes(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167: счётчик «эскалаций не по существу» не смеет обгонять эскалации.

    Лестница до карточки может и не дойти: отчёт с находками отвечает
    changes_requested на первой ступени, и такая сдача уезжает в клетку
    2x2, а не в эскалации. Счётчик, прибавленный по НАЛИЧИЮ дыры, обещал
    бы читателю вычесть из эскалаций больше, чем их есть.
    """
    from hub.integrations.registry import plugins

    project_id = await _historical_project(db, "replay-cardgap")
    git = _HistoricalGitOps(["hub/services/steward_shadow.py"])
    monkeypatch.setattr(plugins, "git_ops", git)
    task_id = await _historical_submission(
        db,
        project_id,
        human_verdict="changes_requested",
        confirmed='[{"title": "настоящая находка"}]',
    )
    await db.execute(
        "INSERT INTO submissions (task_id, generation, sha, base_branch) "
        "VALUES (?, 1, ?, 'develop')",
        (task_id, _SHA),
    )
    await repo.update_task(
        db, task_id, submission_generation=2, submission_sha="f" * 40
    )
    await db.commit()

    entries, dropped = await sh.collect_corpus(db, days=60)
    cases, excluded = await sh.build_cases(db, entries, dropped)
    report = sh.replay(cases, excluded=excluded)

    # Дыра в карточке есть...
    assert cases[0].packet.fact("diff_vs_areas").reason == "card_not_recorded"
    # ...но исход определила НАХОДКА, и это клетка таблицы, а не эскалация.
    assert report.table.both_changes == 1
    assert report.table.escalated == 0
    assert report.card_not_recorded == 0, "счётчик обогнал эскалации"
    # И дифф по коммиту прочитан, поэтому доля восстановленных не страдает
    # за то, к чему реконструкция отношения не имеет.
    assert report.reconstructed == 1.0


async def test_collapsed_three_dot_diff_is_a_named_hole(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167: пустой трёхточечный дифф — не «сдача ничего не меняла».

    ``branch_diff_paths`` считает ``base...sha`` от merge-base. Как только
    закреплённый коммит попал в историю сегодняшней базы (доставка
    настоящим мержем, а не squash), merge-base равен самому коммиту, и
    список пуст ВСЕГДА — что бы сдача ни меняла. Проверено на живой
    истории: у доставленной задачи #1185 вершина 1695a41 — предок develop,
    ``git diff --name-only origin/develop...1695a41`` даёт 0 файлов, а сам
    коммит трогает 2.

    Опаснее всего, что пустой список — не ``None``: без этой проверки пакет
    не исключается, поверхность читается как «в заявленном», класс не
    поднимается, а доля восстановленного засчитывает провал успехом.
    """
    from hub.integrations.registry import plugins
    from hub.services.steward_evidence import (
        build_historical_packet,
        diff_recovered,
        reconstructed_share,
    )

    project_id = await _historical_project(db, "replay-collapsed")
    git = _HistoricalGitOps(
        ["hub/services/steward_shadow.py"], diff_empty=True, ancestor=True
    )
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
    surface = packet.fact("diff_vs_areas")
    risk = packet.fact("risk_class")
    assert surface.is_absent, "пустой дифф выдан за измеренную поверхность"
    assert surface.reason == "historical_diff_collapsed"
    assert risk.is_absent and risk.reason == "historical_diff_collapsed"
    assert "истории базы" in surface.detail
    # Отказ ГИТА и схлопнувшаяся топология — РАЗНЫЕ коды: доля
    # восстановленного считает первое, и путать их значит отменять бэкфилл
    # по причине, к реконструкции по sha отношения не имеющей.
    assert surface.reason != "historical_diff_unreadable"
    assert diff_recovered(packet) is False
    assert reconstructed_share([packet]) == 0.0

    # И это доезжает до исхода: судить не по чему — значит к человеку.
    entries, dropped = await sh.collect_corpus(db, days=60)
    cases, excluded = await sh.build_cases(db, entries, dropped)
    report = sh.replay(cases, excluded=excluded)
    assert report.table.escalated == 1
    assert report.table.both_approve == 0, "approve на невосстановимой поверхности"
    assert report.card_not_recorded == 0, "дыра карточки тут ни при чём"
    assert report.reconstructed == 0.0


async def test_unanswered_ancestry_is_not_read_as_an_empty_diff(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167: «не смогли спросить» — не «сдача ничего не меняла».

    Пустой дифф значит одно из двух, и различает их только вопрос о предке.
    Когда git на него не ответил, пустота недоказуема как измерение — и
    падает туда же, куда любой другой отказ гита.
    """
    from hub.integrations.registry import plugins
    from hub.services.steward_evidence import build_historical_packet, diff_recovered

    project_id = await _historical_project(db, "replay-ancestry-unknown")
    monkeypatch.setattr(
        plugins,
        "git_ops",
        _HistoricalGitOps(["x"], diff_empty=True, ancestor=None),
    )
    task_id = await _historical_submission(db, project_id, human_verdict="approved")
    rows = await fetchall(
        db,
        "SELECT created_at FROM events WHERE task_id=? AND "
        "kind='review_verdict_recorded'",
        (task_id,),
    )
    packet = await build_historical_packet(db, task_id, 1, dict(rows[0])["created_at"])
    surface = packet.fact("diff_vs_areas")
    assert surface.is_absent and surface.reason == "historical_diff_unreadable"
    assert diff_recovered(packet) is False


async def test_ancestry_is_asked_against_the_ref_the_diff_used(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167: вопрос о предке задаётся ТОЙ вершине, против которой считан дифф.

    ``branch_diff_paths`` резолвит базу remote-first (#762) и фетчем двигает
    ``origin/<база>``; локальный ref не трогает никто, и в общем клоне хаба
    он отстаёт (#824, #1046). Воспроизведено настоящим git на клоне с
    отставшим локальным ``develop``:

        git diff --name-only origin/develop...<sha>        -> 0 файлов
        git merge-base --is-ancestor <sha> develop         -> rc=1
        git merge-base --is-ancestor <sha> origin/develop  -> rc=0

    Спросив голое имя, детектор коллапса получает «не предок», дыру не
    ставит, и ``_surface_fact`` на пустом списке отвечает present +
    ``within_declared=True``: одобрение по измерению, которого не было.
    Заглушка отвечает на два ref РАЗНОЕ — иначе тест зелен при любом из них.
    """
    from hub.integrations.registry import plugins
    from hub.services.steward_evidence import build_historical_packet, diff_recovered

    project_id = await _historical_project(db, "replay-ancestry-ref")
    git = _HistoricalGitOps(
        ["hub/services/steward_shadow.py"],
        diff_empty=True,
        ancestor=True,
        local_ancestor=False,
    )
    monkeypatch.setattr(plugins, "git_ops", git)
    task_id = await _historical_submission(db, project_id, human_verdict="approved")
    rows = await fetchall(
        db,
        "SELECT created_at FROM events WHERE task_id=? AND "
        "kind='review_verdict_recorded'",
        (task_id,),
    )

    packet = await build_historical_packet(db, task_id, 1, dict(rows[0])["created_at"])

    # Сначала ПОСЛЕДСТВИЕ, потом уже способ: тест обязан краснеть от того,
    # что схлопнувшийся дифф прошёл за измерение, а не от одного лишь имени
    # ref в журнале заглушки.
    surface = packet.fact("diff_vs_areas")
    assert surface.is_absent, "пустой дифф выдан за измеренную поверхность"
    assert surface.reason == "historical_diff_collapsed"
    assert packet.fact("risk_class").is_absent
    assert diff_recovered(packet) is False
    assert git.ancestry_asked == ["origin/develop"], (
        "предка спросили не у той вершины, против которой считан дифф: "
        f"{git.ancestry_asked}"
    )

    entries, dropped = await sh.collect_corpus(db, days=60)
    cases, excluded = await sh.build_cases(db, entries, dropped)
    report = sh.replay(cases, excluded=excluded)
    assert report.table.escalated == 1
    assert report.table.both_approve == 0, "approve на невосстановимой поверхности"


async def test_collapse_is_detected_on_a_base_that_is_not_develop(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167: детектор коллапса держится на БАЗЕ СДАЧИ, а не на слове develop.

    Дыра, найденная машинным ревью и воспроизведённая мутацией: зашей в
    ``_diff_base_ref`` строку ``"origin/develop"`` — и все три теста про
    пустой дифф остаются зелёными, потому что база у них у всех develop.
    То есть проверялся механизм ``origin/``, но не то, что спрошена ИМЕННО
    та база, против которой дифф посчитан.

    Здесь база другая и клон РЕАЛЬНЫЙ: в нём есть и ``origin/release-2026-08``
    (туда работу и влили), и ``origin/develop`` (сегодняшняя база проекта),
    причём коммита в develop нет. Три ответа git расходятся:

        merge-base --is-ancestor <sha> origin/release-2026-08  -> rc=0
        merge-base --is-ancestor <sha> origin/develop          -> rc=1

    Спросив develop, детектор получает честное «не предок», дыру не ставит,
    и ``_surface_fact`` на пустом списке отвечает present +
    ``within_declared=True``. Это не косметика имени дыры: сдача с
    невосстановимой поверхностью уходит в both_approve. Поэтому тест сперва
    проверяет ПОСЛЕДСТВИЕ и только потом журнал заглушки.
    """
    from hub.integrations.registry import plugins
    from hub.services.steward_evidence import build_historical_packet, diff_recovered

    project_id = await _historical_project(db, "replay-collapse-other-base")
    git = _HistoricalGitOps(
        ["hub/services/steward_shadow.py"],
        base="release-2026-08",
        diff_empty=True,
        ancestor=True,
        other_refs=("origin/develop", "develop"),
    )
    monkeypatch.setattr(plugins, "git_ops", git)
    task_id = await _historical_submission(db, project_id, human_verdict="approved")
    # Судили против релизной ветки; сегодняшняя база проекта — develop.
    await db.execute(
        "INSERT INTO submissions (task_id, generation, sha, base_branch) "
        "VALUES (?, 1, ?, 'release-2026-08')",
        (task_id, _SHA),
    )
    await db.commit()
    rows = await fetchall(
        db,
        "SELECT created_at FROM events WHERE task_id=? AND "
        "kind='review_verdict_recorded'",
        (task_id,),
    )

    packet = await build_historical_packet(db, task_id, 1, dict(rows[0])["created_at"])

    surface = packet.fact("diff_vs_areas")
    assert surface.is_absent, (
        "схлопнувшийся дифф выдан за измеренную поверхность: предка спросили "
        f"не у базы сдачи, а у {git.ancestry_asked}"
    )
    assert surface.reason == "historical_diff_collapsed"
    assert packet.fact("risk_class").is_absent
    assert diff_recovered(packet) is False
    assert git.ancestry_asked == ["origin/release-2026-08"], (
        "предка спросили не у той базы, против которой считан дифф: "
        f"{git.ancestry_asked}"
    )

    entries, dropped = await sh.collect_corpus(db, days=60)
    cases, excluded = await sh.build_cases(db, entries, dropped)
    report = sh.replay(cases, excluded=excluded)
    assert report.table.escalated == 1
    assert report.table.both_approve == 0, "approve на невосстановимой поверхности"
    assert report.reconstructed == 0.0


async def test_git_refusing_the_diff_is_not_a_card_gap(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167: ``diff_paths is None`` — отказ ГИТА, и он запинен здесь.

    Ветка ``if diff_paths is None`` в ``_card_facts`` до сих пор не была
    пройдена ни одним тестом задачи: у ``test_unanswered_ancestry`` дифф
    пустой (``[]``), а не непрочитанный, card-gap тесты получают непустой
    список, и заглушка отдавала ``None`` только на чужую базу — то есть на
    сдачу, которую корпус и так выбрасывает.

    Разница несущая: ``diff_recovered`` засчитывает ``card_not_recorded``
    восстановлением, потому что дифф там ПРОЧИТАН, а сверять его не с чем.
    Отказ гита восстановлением не является, и подмена кода тихо подняла бы
    долю восстановленного — то самое число, по которому задача решает,
    отменять ли бэкфилл («ниже 70%»).
    """
    from hub.integrations.registry import plugins
    from hub.services.steward_evidence import (
        build_historical_packet,
        diff_recovered,
        reconstructed_share,
    )

    project_id = await _historical_project(db, "replay-diff-unreadable")
    git = _HistoricalGitOps(["hub/services/steward_shadow.py"], diff_unreadable=True)
    monkeypatch.setattr(plugins, "git_ops", git)
    task_id = await _historical_submission(db, project_id, human_verdict="approved")
    rows = await fetchall(
        db,
        "SELECT created_at FROM events WHERE task_id=? AND "
        "kind='review_verdict_recorded'",
        (task_id,),
    )

    packet = await build_historical_packet(db, task_id, 1, dict(rows[0])["created_at"])

    # Сначала ПОСЛЕДСТВИЕ: отказ гита, зачтённый восстановлением, — это
    # завышенная доля, по которой задача решает, отменять ли бэкфилл.
    assert diff_recovered(packet) is False, "отказ гита зачтён восстановлением"
    assert reconstructed_share([packet]) == 0.0
    surface = packet.fact("diff_vs_areas")
    risk = packet.fact("risk_class")
    assert surface.is_absent and surface.reason == "historical_diff_unreadable"
    assert risk.is_absent and risk.reason == "historical_diff_unreadable"
    assert surface.reason != "card_not_recorded", "отказ гита выдан за дыру карточки"
    assert "не прочитать" in surface.detail
    # Пустоты не было — предка никто не спрашивал: ``None`` и ``[]`` идут
    # разными ветками, и путать их значит спрашивать про несуществующий дифф.
    assert git.ancestry_asked == []

    entries, dropped = await sh.collect_corpus(db, days=60)
    cases, excluded = await sh.build_cases(db, entries, dropped)
    report = sh.replay(cases, excluded=excluded)
    assert report.card_not_recorded == 0, "отказ гита посчитан дырой карточки"
    assert report.reconstructed == 0.0
    assert report.table.both_approve == 0, "approve без прочитанной поверхности"


async def test_historical_diff_is_asked_against_the_recorded_base(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167: точка сравнения — та, против которой судили, и это проверяется.

    Заглушка отвечает ТОЛЬКО на свою базу: спросили другую — ответа нет.
    Без этого условия стенд не отличал бы базу из леджера от сегодняшней
    базы проекта, и подмена точки сравнения проходила бы зелёной.
    """
    from hub.integrations.registry import plugins
    from hub.services.steward_evidence import BASE_FROM_LEDGER

    project_id = await _historical_project(db, "replay-recorded-base")
    git = _HistoricalGitOps(["hub/services/steward_shadow.py"], base="release-2026-08")
    monkeypatch.setattr(plugins, "git_ops", git)
    task_id = await _historical_submission(db, project_id, human_verdict="approved")
    # Сдачу судили против релизной ветки; сегодняшняя база проекта — develop.
    await db.execute(
        "INSERT INTO submissions (task_id, generation, sha, base_branch) "
        "VALUES (?, 1, ?, 'release-2026-08')",
        (task_id, _SHA),
    )
    await db.commit()

    entries, dropped = await sh.collect_corpus(db, days=60)
    cases, excluded = await sh.build_cases(db, entries, dropped)

    assert excluded == [], f"сдача выброшена зря: {excluded}"
    assert git.bases == ["release-2026-08"], "дифф спрошен не от той базы"
    packet = cases[0].packet
    assert packet.fact("diff_vs_areas").is_present
    assert packet.diff_base == "release-2026-08"
    assert packet.diff_base_source == BASE_FROM_LEDGER
    report = sh.replay(cases, excluded=excluded)
    assert report.base_not_recorded == 0
    assert "база сдачи не записана): 0" in sh.render_report(report)


async def test_substituted_base_is_counted_in_the_report(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167: подмена базы обязана доехать до ОТЧЁТА, а не осесть в детали.

    Леджер базу headless-сдачи не пишет, и тогда дифф считается от
    сегодняшней базы проекта. Это не отказ — дифф посчитан, — но сравнивали
    не с тем, с чем судил человек. Приписка внутри ``detail`` этого не
    закрывает: по ней нельзя сложить число, и читатель отчёта не узнает,
    скольких сдач это касается.
    """
    from hub.integrations.registry import plugins
    from hub.services.steward_evidence import BASE_NOT_RECORDED

    project_id = await _historical_project(db, "replay-substituted-base")
    monkeypatch.setattr(
        plugins, "git_ops", _HistoricalGitOps(["hub/services/steward_shadow.py"])
    )
    await _historical_submission(db, project_id, human_verdict="approved")

    entries, dropped = await sh.collect_corpus(db, days=60)
    cases, excluded = await sh.build_cases(db, entries, dropped)

    packet = cases[0].packet
    assert packet.diff_base == "develop"
    assert packet.diff_base_source == BASE_NOT_RECORDED
    # И названа в самом факте — тем именем, против которого дифф посчитан на
    # самом деле. «База не названа» было бы неправдой: пустое имя ниже
    # подменяет PAIR_BASE_BRANCH, и пакет обязан говорить эту базу.
    assert "сегодняшняя база проекта (develop)" in packet.fact("diff_vs_areas").detail
    report = sh.replay(cases, excluded=excluded)
    assert report.base_not_recorded == 1
    text = sh.render_report(report)
    assert "Сравнено с сегодняшней базой проекта (база сдачи не записана): 1" in text


async def test_replay_without_policy_uses_the_live_token_budget(
    db: aiosqlite.Connection, monkeypatch
):
    """#1167: стенд без аргументов повторяет ПРОД, а не мягче его.

    Ноль в token_budget означает «проверка выключена». Умолчание-ноль
    показывало бы approve там, где живой гейт эскалирует перерасход, — и
    отчёт, на который сошлются, врал бы в сторону автономии.
    """
    from hub.integrations.registry import plugins
    from hub.services.gate_grounds import GatePolicy

    monkeypatch.setattr(config, "REVIEW_TOKEN_BUDGET", 300000)
    assert GatePolicy().token_budget == 300000

    project_id = await _historical_project(db, "replay-budget")
    monkeypatch.setattr(
        plugins, "git_ops", _HistoricalGitOps(["hub/services/steward_shadow.py"])
    )
    await _historical_submission(
        db, project_id, human_verdict="approved", tokens=400000
    )
    entries, dropped = await sh.collect_corpus(db, days=60)
    cases, excluded = await sh.build_cases(db, entries, dropped)

    report = sh.replay(cases, excluded=excluded)
    assert dict(report.reasons).get("report_token_budget") == 1
    assert report.table.both_approve == 0


# Восстановление захвата, оставленного мёртвым процессом (#1195)
# ---------------------------------------------------------------------------
#
# Измерено на #1190: заказ размещён в 08:49:05 UTC, служба перезапущена в
# 08:49:07, одноразовый код обменян в 08:49:20 — агент у провайдера создан и
# оплачен. Строка осталась с agent_id='pending:6', и выборка исполнителя,
# отбирающая по agent_id='', не видела её никогда.


async def _claimed(db: aiosqlite.Connection, run_id: int) -> None:
    """Захват ровно в той форме, в какой его ставит start_run."""
    await db.execute(
        "UPDATE steward_runs SET agent_id=? WHERE id=?",
        (f"{sh.PENDING_PREFIX}{run_id}", run_id),
    )
    await db.commit()


async def test_a_claim_from_a_dead_process_is_recovered_at_startup(
    db: aiosqlite.Connection, with_identity
):
    """#1195 AC-1: метка снята, и ближайший тик стартует заказ как обычный.

    Проверяется не «снялась ли метка», а то, ради чего она снимается:
    поколение получает назад свой единственный слот. Поэтому после
    восстановления здесь идёт настоящий start_due_runs — без него тест
    подтвердил бы косметику.
    """
    project_id = await _project(db, "shadow-recover")
    task_id = await _task(db, project_id)
    order = await order_run(db, task_id, 1)
    assert order is not None
    await _claimed(db, order["id"])

    assert await sh.recover_dead_process_claims(db) == 1

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ):
        assert await start_due_runs(db) == 1
    run = (await _runs(db, task_id))[0]
    assert run["status"] == RUN_OPEN
    assert run["agent_id"] == "agent-1", "заказ вернулся в работу и стартовал"


async def test_the_recovery_names_the_orphan_risk(db: aiosqlite.Connection):
    """#1195 AC-2: событие называет риск, а не только факт снятия метки.

    Тихое восстановление прячет оплаченного агента ровно так же, как прятала
    метка. На #1190 агент был не гипотетическим: сверка выдачи провайдера
    нашла bc-953df24a, созданного в 08:49:06Z с живым допуском на два часа, о
    котором хаб не знает ничего.
    """
    project_id = await _project(db, "shadow-recover-event")
    task_id = await _task(db, project_id)
    order = await order_run(db, task_id, 1)
    assert order is not None
    await _claimed(db, order["id"])

    await sh.recover_dead_process_claims(db)

    events = await _events(db, sh.EVENT_CLAIM_RECOVERED)
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["run_id"] == order["id"], "заказ назван номером"
    assert payload["generation"] == 1
    assert payload["orphan_risk"] is True
    detail = payload["detail"]
    assert "оплачен" in detail, "риск оплаченного агента назван словами"
    assert "идентификатор" in detail, "и сказано, что его не сопоставить"


async def test_recovery_never_reopens_a_closed_order(db: aiosqlite.Connection):
    """#1195 AC-3: закрытый заказ не оживает — ни один вид закрытия.

    На ВСЕ виды сразу, а не на один: восстановление возвращает в работу
    недоделанное, и «недоделанное» здесь — это статус, а не форма метки.
    Проверка на одном виде закрытия пропустила бы остальные четыре, а
    оживший superseded позвал бы судью к вопросу, на который уже ответили.
    """
    from hub.services.steward_dispatch import RUN_JUDGED, RUN_SUPERSEDED

    project_id = await _project(db, "shadow-recover-closed")
    closed_statuses = [
        RUN_JUDGED,
        RUN_TIMEOUT,
        RUN_SUPERSEDED,
        RUN_REFUSED,
        RUN_NEVER_STARTED,
    ]
    orders = []
    for index, status in enumerate(closed_statuses):
        task_id = await _task(db, project_id)
        order = await order_run(db, task_id, 1)
        assert order is not None
        await _claimed(db, order["id"])
        await db.execute(
            "UPDATE steward_runs SET status=? WHERE id=?", (status, order["id"])
        )
        await db.commit()
        orders.append((order["id"], status))

    assert await sh.recover_dead_process_claims(db) == 0

    for run_id, status in orders:
        rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (run_id,))
        row = dict(rows[0])
        assert row["status"] == status, f"{status} не должен был ожить"
        assert row["agent_id"].startswith(sh.PENDING_PREFIX), (
            f"метка закрытого заказа ({status}) не трогается"
        )
    assert await _events(db, sh.EVENT_CLAIM_RECOVERED) == []


async def test_startup_actually_runs_the_recovery():
    """#1195: восстановление зовёт ПОДЪЁМ, а не только тест.

    Отдельным тестом и через НАСТОЯЩИЙ lifespan, потому что три AC выше
    проходят и на функции, которую никто не вызывает: они зовут её сами.
    Признак задачи — старт процесса, и непроверенный крючок сделал бы всю
    работу мёртвым кодом.

    Заодно проверяется порядок: восстановление до поллера. Метка, снятая
    после первого тика, стоила бы поколению ещё одного круга ожидания.
    """
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from hub.db import _SCHEMA, _migrate

    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    order: list[str] = []

    async def _spy(_conn):
        order.append("recovery")
        return 0

    def _poller(_app):
        order.append("poller")
        # MagicMock, а не AsyncMock: lifespan на выходе зовёт poll_task.cancel(),
        # и вызванный AsyncMock оставил бы неожиданную корутину.
        return MagicMock()

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_SCHEMA)
    await _migrate(conn)

    from hub.app import app, lifespan

    with (
        patch("hub.app.get_db", AsyncMock(return_value=conn)),
        patch("hub.app.start_poller", _poller),
        patch(
            "hub.app._mcp_streamable_app",
            SimpleNamespace(router=SimpleNamespace(lifespan_context=_noop_lifespan)),
        ),
        patch.object(sh, "recover_dead_process_claims", _spy),
    ):
        async with lifespan(app):
            pass

    assert order == ["recovery", "poller"], (
        "восстановление зовётся при подъёме и ДО поллера"
    )


async def test_a_failed_recovery_does_not_hold_the_write_lock(
    db: aiosqlite.Connection,
):
    """#1195 (ревью #304): сбой на полпути не оставляет открытую транзакцию.

    Соединение подъёма живёт весь процесс, а isolation_level="IMMEDIATE"
    (#1065) открывает write-транзакцию на первом же UPDATE. Если запись
    события упадёт между UPDATE и commit, а откатить некому, то соединение
    держит write-лок ВСЕЙ базы до перезапуска: поллер и обработчики ходят
    своими соединениями и после busy_timeout получают «database is locked».
    Восстановление, задуманное как best-effort сигнал, тогда останавливает
    запись в хаб — цена сбоя больше, чем сам сбой. Поллер откатывает свой
    хвост тика ровно от этого (hub/poller.py), здесь коммит один на пачку и
    окно шире.

    Проверяется наблюдаемое состояние соединения, а не текст лога.
    """
    project_id = await _project(db, "shadow-recover-rollback")
    task_id = await _task(db, project_id)
    order = await order_run(db, task_id, 1)
    assert order is not None
    await _claimed(db, order["id"])

    boom = AsyncMock(side_effect=RuntimeError("лента событий недоступна"))
    with patch.object(repo, "insert_event", boom):
        with pytest.raises(RuntimeError):
            await sh.recover_dead_process_claims(db)

    assert not db.in_transaction, (
        "сбой восстановления оставил открытую транзакцию: write-лок всей базы"
    )
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (order["id"],))
    assert dict(rows[0])["agent_id"].startswith(sh.PENDING_PREFIX), (
        "откат вернул метку: незаписанное восстановление не считается сделанным"
    )


async def test_a_recovery_that_recovers_nothing_does_not_hold_the_write_lock(
    db: aiosqlite.Connection,
):
    """#1195 (ревью #305): нулевая жатва тоже закрывает транзакцию.

    Соседний путь того же класса, что и сбойный. UPDATE, не сменивший ни
    одной строки, всё равно открывает write-транзакцию: isolation_level=
    "IMMEDIATE" (#1065) начинает её на ЛЮБОМ DML, а не на удачном. Если
    гонка забрала все выбранные строки, recovered остаётся нулём — и коммит
    за условием `if recovered` не выполняется. Тогда успешный, ничего не
    нашедший подъём держал бы write-лок всей базы до перезапуска.

    Гонка здесь настоящая, а не воображаемая: заказ закрывают между SELECT и
    UPDATE — ровно та ветка, ради которой в коде стоит `rowcount != 1`.
    Сосед по файлу (start_run) свой нулевой UPDATE откатывает сразу же.
    """
    from hub.services.steward_dispatch import RUN_SUPERSEDED

    project_id = await _project(db, "shadow-recover-zero")
    task_id = await _task(db, project_id)
    order = await order_run(db, task_id, 1)
    assert order is not None
    await _claimed(db, order["id"])

    real_fetchall = sh.fetchall

    async def _closed_underneath(conn, sql, parameters=()):
        rows = await real_fetchall(conn, sql, parameters)
        # Вердикт пришёл раньше: слот закрыт после выборки, но до записи.
        await conn.execute(
            "UPDATE steward_runs SET status=? WHERE id=?",
            (RUN_SUPERSEDED, order["id"]),
        )
        await conn.commit()
        return rows

    with patch.object(sh, "fetchall", _closed_underneath):
        assert await sh.recover_dead_process_claims(db) == 0

    assert not db.in_transaction, (
        "подъём без единого восстановления оставил открытую транзакцию"
    )
    rows = await fetchall(db, "SELECT * FROM steward_runs WHERE id=?", (order["id"],))
    row = dict(rows[0])
    assert row["status"] == RUN_SUPERSEDED, "закрытый заказ остался закрытым"
    assert row["agent_id"].startswith(sh.PENDING_PREFIX), "метку никто не трогал"


# ---------------------------------------------------------------------------
# Живой пакет доказательств и схлопнувшийся дифф (#1239)
# ---------------------------------------------------------------------------
#
# У живого пакета сторожа не было ВОВСЕ. Не ломался он не потому, что защищён,
# а потому, что его субъект — ветка ДО мержа: схлопываться было нечему. Защита
# стечением обстоятельств не переживает смены обстоятельств, а цена ошибки
# здесь — суждение на ложных данных, а не пустой экран.
#
# Настоящий git, а не MockGitOps из conftest: предмет проверки — что ответит
# ``git diff base...branch`` на ветке, которая уже влита. Мок ответил бы то,
# что в него положили, и проверял бы фикстуру.


def _task_branch_clone(tmp_path, *, delivered: bool):
    """Клон с веткой задачи; ``delivered`` — влита ли она уже в базу."""
    import subprocess

    def run(*args, cwd):
        subprocess.run(args, cwd=cwd, check=True, capture_output=True)

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "develop", str(origin)],
        check=True,
        capture_output=True,
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    run("git", "clone", str(origin), str(seed), cwd=tmp_path)
    run("git", "config", "user.email", "t@e", cwd=seed)
    run("git", "config", "user.name", "t", cwd=seed)
    (seed / "hub").mkdir()
    (seed / "hub" / "base.py").write_text("base\n")
    run("git", "add", "-A", cwd=seed)
    run("git", "commit", "-qm", "baseline", cwd=seed)
    run("git", "branch", "-M", "develop", cwd=seed)
    run("git", "push", "-q", "origin", "develop", cwd=seed)

    branch = "task-9001/delivered"
    run("git", "checkout", "-qb", branch, cwd=seed)
    # Сдача трогает файл ВНЕ заявленных областей: если бы поверхность
    # измерялась на самом деле, сверка сказала бы «вне заявленного».
    (seed / "hub" / "secrets.py").write_text("undeclared\n")
    run("git", "add", "-A", cwd=seed)
    run("git", "commit", "-qm", "the submission", cwd=seed)
    run("git", "push", "-q", "origin", branch, cwd=seed)
    if delivered:
        run("git", "checkout", "-q", "develop", cwd=seed)
        run("git", "merge", "-q", "--no-ff", "-m", "deliver", branch, cwd=seed)
        run("git", "push", "-q", "origin", "develop", cwd=seed)

    clone = tmp_path / "hub-clone"
    run("git", "clone", str(origin), str(clone), cwd=tmp_path)
    run("git", "config", "user.email", "t@e", cwd=clone)
    run("git", "config", "user.name", "t", cwd=clone)
    return clone, branch


async def _packet_task(db: aiosqlite.Connection, clone, branch: str) -> int:
    """``clone=None`` — проект БЕЗ рабочей копии: та самая дыра из #1239."""
    project_id = await repo.create_project(
        db,
        slug="collapse-probe",
        name="collapse probe",
        workspace_path="" if clone is None else str(clone),
        default_branch="develop",
        status="active",
    )
    task_id = await repo.create_task(
        db,
        title="доставленная сдача",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="a",
        rationale="",
        status="review",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(
        db,
        task_id,
        project_id=project_id,
        branch=branch,
        affected_areas=json.dumps(["hub/base.py"]),
        risk_class="R1",
    )
    await db.commit()
    return task_id


def _real_git(monkeypatch) -> None:
    from hub.integrations.git_ops import GitOpsIntegration
    from hub.integrations.registry import plugins

    monkeypatch.setattr(plugins, "git_ops", GitOpsIntegration())


async def test_a_live_packet_does_not_call_a_collapsed_diff_within_bounds(
    db: aiosqlite.Connection, tmp_path, monkeypatch
) -> None:
    """AC-2: пустой дифф доставленной задачи — дыра, а не «в границах».

    До правки: ``branch_diff_paths`` возвращал ПУСТОЙ СПИСОК (не None),
    ``_surface_fact`` отвечал ``present`` с ``within_declared=True``, класс
    риска не пересчитывался — и судья получал зелёный факт о поверхности,
    которую никто не измерял. Сдача здесь трогает ``hub/secrets.py`` при
    заявленном ``hub/base.py``: настоящая сверка сказала бы «вне заявленного»,
    и именно это утверждение пустота стирала.

    Мутация «убрать вопрос о предке в ``_guard_collapsed_diff``» роняет
    именно этот тест и не трогает тесты карточки: это разные места
    применения одного правила.
    """
    from hub.services.steward_evidence import DIFF_COLLAPSED, build_evidence_packet

    _real_git(monkeypatch)
    clone, branch = _task_branch_clone(tmp_path, delivered=True)
    task_id = await _packet_task(db, clone, branch)

    packet = await build_evidence_packet(db, task_id)

    surface = packet.fact("diff_vs_areas")
    assert surface.is_absent
    assert surface.reason == DIFF_COLLAPSED
    assert "схлопнулся" in surface.detail
    # И класс риска не смеет считаться непревышенным по той же пустоте.
    risk = packet.fact("risk_class")
    assert risk.is_absent
    assert risk.reason == DIFF_COLLAPSED


async def test_a_live_packet_still_measures_a_branch_before_delivery(
    db: aiosqlite.Connection, tmp_path, monkeypatch
) -> None:
    """Сторож не съедает нормальный случай: неслитая ветка меряется как прежде.

    Без этого теста «дыра всегда» прошло бы как «дыра там, где надо» — а это
    сломало бы гейт на каждой живой сдаче.
    """
    from hub.services.steward_evidence import build_evidence_packet

    _real_git(monkeypatch)
    clone, branch = _task_branch_clone(tmp_path, delivered=False)
    task_id = await _packet_task(db, clone, branch)

    packet = await build_evidence_packet(db, task_id)

    surface = packet.fact("diff_vs_areas")
    assert surface.is_present
    assert surface.value["paths"] == ["hub/secrets.py"]
    assert surface.value["within_declared"] is False


async def test_a_live_packet_without_a_workspace_does_not_measure_a_foreign_clone(
    db: aiosqlite.Connection, tmp_path, monkeypatch
) -> None:
    """У проекта нет клона — поверхность НЕ измерена, а не «в границах».

    Находка 95907d9c52c474b8. Сторож обещает три ответа, но выходил четвёртым:
    без ``workspace`` он возвращал исходный ПУСТОЙ СПИСОК без дыры. А дальше
    цепочка уже сработала: ``_resolve_branch_diff`` зовёт ``branch_diff_paths``
    с ``repo=None``, та молча падает на ``_repo_root()`` — клон ХАБА, а не
    проекта. В нём ветка задачи вполне может быть уже влита, и пустой дифф
    ЧУЖОГО клона приезжал как ``[]``. ``_surface_fact`` писал ``present`` и
    ``within_declared=True``: судья видел зелёную поверхность, которой никто
    не мерил. Вход в ту же ложь другой — «нет рабочей копии» вместо «не
    спросили предка», а цена та же.

    Здесь это воспроизведено настоящим git: проект без ``workspace_path``,
    а подменённый ``_repo_root`` указывает на клон, где ветка уже доставлена.
    Сдача трогает ``hub/secrets.py`` при заявленном ``hub/base.py`` — будь
    поверхность измерена, сверка сказала бы «вне заявленного».
    """
    from hub.integrations import git_ops as git_ops_mod
    from hub.services.steward_evidence import DIFF_UNREADABLE, build_evidence_packet

    _real_git(monkeypatch)
    clone, branch = _task_branch_clone(tmp_path, delivered=True)
    monkeypatch.setattr(git_ops_mod, "_repo_root", lambda: str(clone))
    task_id = await _packet_task(db, None, branch)

    packet = await build_evidence_packet(db, task_id)

    surface = packet.fact("diff_vs_areas")
    assert surface.is_absent, (
        "без клона проекта поверхность не измерена — сказать «в границах» "
        f"по чужому клону нельзя: {surface.value}"
    )
    assert surface.reason == DIFF_UNREADABLE
    risk = packet.fact("risk_class")
    assert risk.is_absent
    assert risk.reason == DIFF_UNREADABLE


async def test_a_live_packet_names_an_unanswered_ancestry_as_unreadable(
    db: aiosqlite.Connection, tmp_path, monkeypatch
) -> None:
    """Третий ответ сторожа закрыт своим тестом (5d67b34d6bb2f77f).

    ``commit_in_base_history`` отвечает тремя значениями, и ветка ``None`` —
    «git не ответил» — до сих пор не была прогнана НИ ОДНИМ тестом живого
    пакета: мутация ``None`` -> ``[]`` в этой ветке оставляла набор зелёным.
    Дыра покрытия, а не продуктовый дефект, — но именно она и разрешает
    будущей правке свернуть три ответа в два, ровно против ограничения
    задачи.

    Ответ ``None`` здесь подставлен точечно: сам вопрос задаётся настоящему
    git, а «не ответил» на живом репозитории не ставится честно — оба конца
    диффа только что зарезолвились, иначе дифф не был бы пустым списком.
    """
    from hub.integrations.registry import plugins
    from hub.services.steward_evidence import DIFF_UNREADABLE, build_evidence_packet

    _real_git(monkeypatch)
    clone, branch = _task_branch_clone(tmp_path, delivered=True)
    task_id = await _packet_task(db, clone, branch)

    async def _no_answer(repo: str, base: str, sha: str) -> bool | None:
        return None

    monkeypatch.setattr(plugins.git_ops, "commit_in_base_history", _no_answer)

    packet = await build_evidence_packet(db, task_id)

    surface = packet.fact("diff_vs_areas")
    assert surface.is_absent
    assert surface.reason == DIFF_UNREADABLE, (
        "«git не ответил» — не «дифф схлопнулся» и не «изменений нет»: "
        f"{surface.reason}"
    )
    assert "не ответил" in surface.detail
    risk = packet.fact("risk_class")
    assert risk.is_absent
    assert risk.reason == DIFF_UNREADABLE


# --------------------------------------------------------------------------
# Судья выбирается из имён, чей ЗАПУСК наблюдали (#1237)
# --------------------------------------------------------------------------
#
# Значения hub/config.py, снятые ПРИ ИМПОРТЕ модуля — до того, как autouse-
# фикстура shadow_mode подменит STEWARD_MODEL своим. Тест про конфигурацию
# обязан читать конфигурацию, а не то, что подставили себе тесты: иначе
# подмену имени судьи он проспит, потому что смотрит на свою же подмену.
_JUDGE_LIST_AS_SHIPPED: tuple[str, ...] = (config.STEWARD_MODEL,) + tuple(
    config.STEWARD_MODEL_FALLBACKS
)


def test_the_judge_list_holds_only_launchable_models():
    """AC-2: у судьи стоят только имена, чей ЗАПУСК наблюдали попыткой создания.

    Проверка конфигурации, а не поведения: подбор замен построен в #1182 и
    покрыт выше, а разошлось содержимое списка — и разошлось потому, что
    имя туда пускали по каталогу list_models. Каталог отвечает на вопрос
    «имя существует», а нужен ответ на «имя запускается»; двенадцать недель
    разницы между этими вопросами никого не уронили.

    ЧТО ЗДЕСЬ НЕ ПРОВЕРЯЕТСЯ И ПОЧЕМУ. Раньше на этом месте стоял чёрный
    список из gpt-5.3-codex, gemini-3.1-pro и claude-sonnet-5 — со ссылкой
    на #1036, где их назвали гарантированным отказом. Живая попытка
    создания 09.09.2026 (#1237) создала все три; список снят как
    опровергнутый замером, а не как неудобный. Остаётся то, что замер
    подтверждает: имя у судьи — только из наблюдённых.
    """
    from hub.services.review_dispatch import _REVIEW_MODEL_PREFERENCES

    launchable = set(config.SUBSCRIPTION_LAUNCHABLE_MODELS)
    assert launchable, "список наблюдённых имён пуст — судьи не будет вовсе"

    outside = [m for m in _JUDGE_LIST_AS_SHIPPED if m not in launchable]
    assert not outside, (
        "судья и его запасные берутся только из имён, чей запуск наблюдали "
        f"попыткой создания: {outside} вне SUBSCRIPTION_LAUNCHABLE_MODELS "
        f"({sorted(launchable)})"
    )

    # Тот же признак для второго потребителя. НЕ равенство: список ревьюера
    # сужен решением владельца (#1036), и сужение остаётся — замер показал
    # лишь, что оно не вынуждено провайдером. Требуется другое: чтобы имя,
    # которое ревьюеру назначают, тоже было наблюдено запуском, а не взято
    # из каталога. Ровно на этой подмене разошлись два списка.
    unverified = sorted(set(_REVIEW_MODEL_PREFERENCES) - launchable)
    assert not unverified, (
        f"{unverified} у ревьюера не наблюдалось попыткой создания: имя "
        "попадает в любой из двух списков только после наблюдённого запуска"
    )


async def test_no_launchable_judge_is_named_not_silent(
    db: aiosqlite.Connection, with_identity, monkeypatch
):
    """AC-3: «запустить некого» названо событием и карточкой, а не молчит.

    Берётся ровно сегодняшняя конфигурация, не выдуманная: основной судья и
    его запасные из hub/config.py, обычная сдача (исполнитель claude,
    ревьюер grok-4.6). Провайдер отказывает по недоступности, замены из
    семейства ревьюера гейт не пускает — и прогон не начинается.

    До #1237 оба исхода — «бета моргнула» и «эта подписка не запускает ни
    одного судью» — уходили одной строкой run_failed «Cloud Agents API не
    принял запрос», без единого имени и без ответа провайдера. Ответ при
    этом был на руках: create_agent_attempt возвращает его вторым значением.
    Поэтому девяносто дней нулевых суждений не оставили в хабе ни одной
    записи о причине.
    """
    monkeypatch.setattr(config, "STEWARD_MODEL", _JUDGE_LIST_AS_SHIPPED[0])
    monkeypatch.setattr(
        config, "STEWARD_MODEL_FALLBACKS", tuple(_JUDGE_LIST_AS_SHIPPED[1:])
    )
    project_id = await _project(db, "shadow-no-judge")
    task_id = await _task(db, project_id)
    await order_run(db, task_id, 1)

    attempts: list[str] = []

    async def _attempt(**kwargs):
        attempts.append(kwargs["model_id"])
        assert len(attempts) <= 8, f"перебор не кончается: {attempts}"
        return None, _CAPACITY

    with patch("hub.integrations.cursor_cloud.create_agent_attempt", new=_attempt):
        assert await start_due_runs(db) == 0
        first_tick = len(attempts)
        # Второй тик: заказ остался открытым, и он придёт снова — раз в
        # полминуты всё окно старта.
        await start_due_runs(db)

    assert len(attempts) > first_tick, (
        "второй тик обязан состояться, иначе проверка «запись одна» ничего не "
        f"проверяет: {attempts}"
    )
    assert first_tick and attempts[0] == config.STEWARD_MODEL, (
        f"основной судья пробуется первым: {attempts}"
    )

    refused = await _events(db, "steward_run_refused")
    assert refused, "отказ без события — это и есть тишина"
    payload = json.loads(refused[-1]["payload"])
    assert payload["reason"] == sh.REFUSED_NO_LAUNCHABLE_JUDGE, (
        "«список кончился» и «провайдер моргнул» — разные исходы: первый сам "
        f"собой не пройдёт. Получено: {payload}"
    )
    detail = payload["detail"]
    assert attempts[0] in detail, f"в причине названы имена, а не «модель»: {detail}"
    assert "usage_limit_exceeded" in detail and "400" in detail, (
        f"ответ провайдера — то единственное, что отвечает на вопрос «какие "
        f"имена подписка разрешает сегодня»: {detail}"
    )

    run = (await _runs(db, task_id))[0]
    assert run["status"] == RUN_OPEN and run["agent_id"] == "", (
        "лимит провайдера — состояние временное: слот не закрывается"
    )

    said = [
        dict(u)["content"]
        for u in await fetchall(
            db, "SELECT content FROM task_updates WHERE task_id=?", (task_id,)
        )
    ]
    named = [c for c in said if "Судья стюарда не выбран" in c]
    assert len(named) == 1, (
        "человек, которому доводить эту сдачу, видит причину в карточке — и "
        f"ровно один раз за заказ, а не по записи на тик: {said}"
    )
    assert "usage_limit_exceeded" in named[0], (
        f"карточка называет ответ провайдера, а не «что-то пошло не так»: {named}"
    )


# ---------------------------------------------------------------------------
# #1601 AC-2 — approve стюарда только при concur советника на том же пакете
# ---------------------------------------------------------------------------


async def _act_granted(monkeypatch) -> None:
    """Режим act выдан тем же читателем, который его выдаёт в бою (#1107)."""

    async def _granted(_db):
        return "act"

    monkeypatch.setattr(sh, "effective_mode", _granted)
    monkeypatch.setattr(config, "STEWARD_MODE", "act")


async def _converged_evidence(monkeypatch) -> None:
    """Свидетельства сошлись и привратник молчит: остаётся ровно вопрос про советника."""
    from hub.services import steward_apply, steward_applied
    from tests.test_steward_applied import _decide

    async def _no_refusals(_db, _task_id, _generation=None):
        return []

    async def _converged(_db, _task_id, _generation):
        return _decide()

    monkeypatch.setattr(steward_apply, "apply_refusals", _no_refusals)
    monkeypatch.setattr(steward_applied, "self_approval_for", _converged)


async def test_act_approve_only_with_advisor_concur_on_same_packet(
    db: aiosqlite.Connection, monkeypatch
):
    """#1601 AC-2: вердикт появляется только после concur на том же пакете.

    Режим act разрешён, судья записал approve — и это ВСЁ, что происходит при
    записи: вердикта нет, в том числе на пути, которым approve раньше уезжал
    сразу после записи. Дальше ответ советника разветвляет исход:

    * concur с совпадающим хешем пакета — approved от стюарда, ровно один раз;
    * object, timeout и смена пакета после ответа — эскалация с доводами
      обоих, вердикта нет.

    Хеш пакета здесь настоящий: «смена пакета» — это появившийся отчёт и CI, а
    не подмена функции хеширования.
    """
    from hub.services.steward_advisor import apply_advisor_outcomes
    from hub.services.steward_dispatch import close_finished_runs
    from hub.services.steward_evidence import build_evidence_packet, packet_hash
    from tests.test_steward_apply import _green

    await _act_granted(monkeypatch)
    await _converged_evidence(monkeypatch)
    monkeypatch.setattr(config, "STEWARD_REVIEW_WAIT_MAX", 0)
    project_id = await _project(db, "advisor-ac2")

    async def _submission(slug: str) -> tuple[int, str]:
        task_id = await _task(db, project_id)
        await repo.update_task(db, task_id, review_job_id="")
        await db.commit()
        return task_id, packet_hash(await build_evidence_packet(db, task_id, 1))

    async def _verdicts(task_id: int) -> list[dict]:
        return [
            e
            for e in await _events(db, "review_verdict_recorded")
            if e["task_id"] == task_id
        ]

    async def _card(task_id: int) -> str:
        return " | ".join(
            dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
        )

    # (1) concur на том же пакете.
    agreed, h = await _submission("agreed")
    await _judge_run(db, agreed, packet=h)
    await _judge(db, agreed)
    assert await _verdicts(agreed) == [], "сразу после записи судьи вердикта нет"
    assert await apply_advisor_outcomes(db) == 0, "советник ещё не заказан/не ответил"
    await _advisor_run(db, agreed, packet=h)
    await _advise(db, agreed)
    assert await _verdicts(agreed) == [], "и после ответа — пока поллер не применил"
    assert await apply_advisor_outcomes(db) == 1
    approved = await _verdicts(agreed)
    assert len(approved) == 1 and approved[0]["actor"] == "steward"
    assert json.loads(approved[0]["payload"])["verdict"] == "approved"
    assert await apply_advisor_outcomes(db) == 0
    assert len(await _verdicts(agreed)) == 1, "ровно один раз"

    # (2) object.
    objected, h = await _submission("objected")
    await _judge_run(db, objected, packet=h)
    await _judge(db, objected)
    await _advisor_run(db, objected, packet=h)
    await _advise(
        db,
        objected,
        verdict="object",
        findings=[{"title": "тест не запускался на этом коммите"}],
    )
    assert await apply_advisor_outcomes(db) == 1
    assert await _verdicts(objected) == []
    card = await _card(objected)
    assert "тест не запускался на этом коммите" in card, "довод советника назван"
    assert "судья" in card and "approve" in card, "и довод судьи"

    # (3) timeout.
    timed_out, h = await _submission("timed-out")
    await _judge_run(db, timed_out, packet=h)
    await _judge(db, timed_out)
    order = await _advisor_run(db, timed_out, packet=h)
    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now','-1 minutes') WHERE id=?",
        (order["id"],),
    )
    await db.commit()
    await close_finished_runs(db)
    assert await apply_advisor_outcomes(db) == 1
    assert await _verdicts(timed_out) == []
    assert "timeout" in await _card(timed_out)

    # (4) concur, а потом пакет изменился (пришёл отчёт и зелёный CI).
    moved, h = await _submission("moved")
    await _judge_run(db, moved, packet=h)
    await _judge(db, moved)
    await _advisor_run(db, moved, packet=h)
    await _advise(db, moved)
    await _green(db, moved)
    assert packet_hash(await build_evidence_packet(db, moved, 1)) != h
    assert await apply_advisor_outcomes(db) == 1
    assert await _verdicts(moved) == []
    assert "изменился" in await _card(moved)


# ---------------------------------------------------------------------------
# #1601 AC-3 — act_refusals v2: точные формулы
# ---------------------------------------------------------------------------


async def test_act_refusals_v2_exact_formulas(db: aiosqlite.Connection, monkeypatch):
    """#1601 AC-3: знаменатель, числитель, доля и порог пар — ровно по постановке.

    Выборка: 20 пар (18 concur, 2 object), 10 changes_requested судьи, 5
    эскалаций по существу, 12 процедурных no_current_report, ноль ошибочных
    одобрений, ноль человеческих возвратов. Знаменатель 35 = 20+10+5 (12
    процедурных вне), числитель 5+2 = 7, доля 20% — отказов нет. Старое
    требование десяти человеческих возвратов не действует: возвратов ноль.
    С 19 парами — единственный отказ sample_too_small.
    """
    from hub.services.steward_exit import contour_counts
    from hub.services.steward_shadow import (
        REASON_SAMPLE_TOO_SMALL,
        act_refusals,
        effective_mode,
    )

    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    project_id = await _project(db, "advisor-ac3")
    # Старые одиночные суждения (до выката) не зачитываются — ни парами, ни долей.
    for _ in range(7):
        await _v2_row(db, project_id, verdict="approve", contour=1)
        await _v2_row(
            db, project_id, verdict="escalate", reason="precondition_failed", contour=1
        )
    await _v2_sample(
        db,
        project_id,
        concur=17,
        objects=2,
        changes=10,
        substantive=5,
        procedural=12,
    )

    assert await act_refusals(db) == [
        (REASON_SAMPLE_TOO_SMALL, (await act_refusals(db))[0][1])
    ], "19 пар — единственный отказ sample_too_small"
    assert "19" in (await act_refusals(db))[0][1]

    await _v2_sample(db, project_id, concur=1)
    counts = await contour_counts(db)
    assert counts.pairs == 20
    assert (counts.concur, counts.object) == (18, 2)
    assert counts.judged == 47
    assert counts.procedural == 12
    assert counts.denominator == 35, "20+10+5, процедурные вне знаменателя"
    assert counts.judge_escalate_substantive == 5
    assert counts.substantive == 7, "5 эскалаций по существу + 2 возражения"
    assert counts.share == pytest.approx(0.20)
    assert await act_refusals(db) == [], "ни одного отказа; человеческих возвратов 0"
    assert await effective_mode(db) == "act"


@pytest.mark.parametrize("reason", ["no_current_report", "report_incomplete"])
async def test_only_these_two_escalations_are_procedural(
    db: aiosqlite.Connection, reason: str
):
    """Процедурные — РОВНО no_current_report и report_incomplete, оба."""
    from hub.services.steward_exit import contour_counts

    project_id = await _project(db, f"advisor-proc-{reason}")
    await _v2_sample(db, project_id, procedural=3, procedural_reason=reason)

    counts = await contour_counts(db)

    assert counts.procedural == 3 and counts.denominator == 0
    assert counts.procedural_by_reason == {reason: 3}


@pytest.mark.parametrize(
    "reason",
    [
        "precondition_failed",
        "unclosed_finding",
        "ladder_surface",
        "same_family_as_implementer",
        "report_security_finding",
        "low_confidence",
        "no_grounds",
        "daily_cap",
        "run_timeout",
    ],
)
async def test_every_other_escalation_is_substantive(
    db: aiosqlite.Connection, reason: str
):
    """precondition_failed (красный CI, дрейф, области) и все прочие — по существу."""
    from hub.services.steward_exit import contour_counts

    project_id = await _project(db, f"advisor-subst-{reason}")
    for _ in range(2):
        await _v2_row(db, project_id, verdict="escalate", reason=reason)

    counts = await contour_counts(db)

    assert counts.procedural == 0
    assert counts.judge_escalate_substantive == 2
    assert counts.denominator == 2 and counts.substantive == 2


@pytest.mark.parametrize(
    ("substantive", "approves", "refused"),
    [
        (1, 19, None),  # 1 из 20 = 5%: на границе — можно
        (10, 10, None),  # 10 из 20 = 50%: на границе — можно
        (0, 20, "escalations_below_floor"),
        (11, 9, "escalations_above_ceiling"),
    ],
)
async def test_the_share_corridor_edges(
    db: aiosqlite.Connection, substantive: int, approves: int, refused: str | None
):
    """Доля по существу от 5 до 50% включительно; снаружи — названный отказ."""
    from hub.services.steward_exit import act_refusals_v2

    project_id = await _project(db, f"advisor-edge-{substantive}")
    await _v2_sample(db, project_id, concur=approves)
    for _ in range(substantive):
        await _v2_row(db, project_id, verdict="escalate", reason="precondition_failed")

    codes = {code for code, _ in await act_refusals_v2(db)} - {"sample_too_small"}

    assert codes == ({refused} if refused else set())


async def test_the_pairs_threshold_is_twenty_exactly(db: aiosqlite.Connection):
    from hub.services.steward_exit import act_refusals_v2

    project_id = await _project(db, "advisor-twenty")
    await _v2_sample(db, project_id, concur=18, objects=1, changes=5, substantive=1)
    assert [c for c, _ in await act_refusals_v2(db)] == ["sample_too_small"]

    await _v2_sample(db, project_id, objects=1)
    assert await act_refusals_v2(db) == []


async def test_an_empty_contour_is_no_sample_not_a_clean_share(
    db: aiosqlite.Connection,
):
    """Знаменатель ноль — «не измерено», а не ноль эскалаций (#762)."""
    from hub.services.steward_exit import (
        REASON_NO_SAMPLE,
        act_refusals_v2,
        contour_counts,
    )

    project_id = await _project(db, "advisor-empty")
    await _v2_sample(db, project_id, procedural=4)

    assert (await contour_counts(db)).share is None
    assert REASON_NO_SAMPLE in {c for c, _ in await act_refusals_v2(db)}


async def test_a_pair_is_counted_only_when_the_answer_is_bound_to_the_approve(
    db: aiosqlite.Connection,
):
    """Ответ без привязки к ЭТОМУ approve парой не считается."""
    from hub.services.steward_exit import contour_counts

    project_id = await _project(db, "advisor-unbound-count")
    await _v2_sample(db, project_id, concur=3)
    await db.execute(
        "UPDATE steward_judgements SET judged_id=NULL WHERE kind='advisor' "
        "AND id=(SELECT MIN(id) FROM steward_judgements WHERE kind='advisor')"
    )
    await db.commit()

    counts = await contour_counts(db)

    assert counts.pairs == 2 and counts.advisor_pending == 1


async def test_the_timeouts_and_refusals_are_counted_apart(db: aiosqlite.Connection):
    """timeout и refused — не пары; их видно отдельными числами."""
    from hub.services.steward_dispatch import (
        RUN_REFUSED,
        close_run,
        close_finished_runs,
    )
    from hub.services.steward_exit import contour_counts

    project_id = await _project(db, "advisor-apart")
    timed_out = await _v2_row(db, project_id, verdict="approve")
    order = await order_run(db, timed_out, 1, "advisor", model="gpt-5.3-codex")
    await _mark_started(db, order, model="gpt-5.3-codex")
    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now','-1 minutes') WHERE id=?",
        (order["id"],),
    )
    await db.commit()
    await close_finished_runs(db)
    refused = await _v2_row(db, project_id, verdict="approve")
    order = await order_run(db, refused, 1, "advisor", model="gpt-5.3-codex")
    await close_run(db, order, RUN_REFUSED, "нет советника")
    await _v2_row(db, project_id, verdict="approve")  # ждёт

    counts = await contour_counts(db)

    assert counts.pairs == 0
    assert (counts.advisor_timeout, counts.advisor_refused, counts.advisor_pending) == (
        1,
        1,
        1,
    )


async def test_the_weekly_control_uses_the_same_classification(
    db: aiosqlite.Connection,
):
    """Недельный контроль считает ТУ ЖЕ долю: по существу плюс возражения.

    Процедурные из его знаменателя вычтены, возражения советника — в числителе;
    старые суждения (contour=1) в неё не входят. Иначе два контроля одного
    судьи говорили бы о разных числах.
    """
    from hub.services.steward_shadow import weekly_sample

    project_id = await _project(db, "advisor-weekly")
    await _v2_sample(
        db, project_id, concur=4, objects=2, changes=3, substantive=1, procedural=9
    )
    await _v2_row(
        db, project_id, verdict="escalate", reason="precondition_failed", contour=1
    )

    sample = await weekly_sample(db)

    assert sample.judged == 10, "4+2+3+1; 9 процедурных вне"
    assert sample.escalated == 3, "1 по существу + 2 возражения"


async def test_the_weekly_window_excludes_old_judgements(db: aiosqlite.Connection):
    from hub.services.steward_shadow import weekly_sample

    project_id = await _project(db, "advisor-weekly-window")
    await _v2_sample(db, project_id, concur=2, substantive=1)
    await db.execute(
        "UPDATE steward_judgements SET created_at=datetime('now','-30 days') "
        "WHERE verdict='escalate'"
    )
    await db.commit()

    sample = await weekly_sample(db)

    assert (sample.judged, sample.escalated) == (2, 0)


# ---------------------------------------------------------------------------
# #1601 AC-4 — ошибочное одобрение пары: источники, липкость, глобальность
# ---------------------------------------------------------------------------


async def _clean_act_sample(db: aiosqlite.Connection, project_id: int) -> None:
    """Выборка, при которой act выдан: ни одного отказа (проверяется в тесте)."""
    await _v2_sample(db, project_id, concur=20, objects=2, changes=8, substantive=4)


async def _human(
    db: aiosqlite.Connection,
    task_id: int,
    verdict: str,
    generation: int = 1,
    *,
    days_ago: int = 0,
) -> int:
    """Человеческий вердикт; ``days_ago`` сдвигает время события. Возвращает id."""
    event_id = await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor="denis",
        payload={"verdict": verdict, "submission_generation": generation},
    )
    if days_ago:
        await db.execute(
            "UPDATE events SET created_at=datetime('now', ?) WHERE id=?",
            (f"-{days_ago} days", event_id),
        )
    await db.commit()
    return int(event_id or 0)


async def _age_pair(db: aiosqlite.Connection, task_id: int, days: int) -> None:
    """Пара ответила ``days`` дней назад (оба суждения)."""
    await db.execute(
        "UPDATE steward_judgements SET created_at=datetime('now', ?) WHERE task_id=?",
        (f"-{days} days", task_id),
    )
    await db.commit()


_PR = [9000]


async def _deliver(
    db: aiosqlite.Connection, task_id: int, days_ago: int, *, released: bool = True
) -> None:
    """Доставка до прода по учёту хаба: мерж гейта + выкат релиза с его sha.

    ``released=False`` — мерж есть, релиз ещё не выкатан: это НЕ доставка.
    """
    _PR[0] += 1
    sha = f"sha-{_PR[0]}"
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merged_at, "
        "released_sha) VALUES (1, ?, ?, datetime('now', ?), ?)",
        (_PR[0], task_id, f"-{days_ago} days", sha if released else None),
    )
    if released:
        await db.execute(
            "INSERT INTO releases (project_id, deployed_sha, status, deployed_at) "
            "VALUES (1, ?, 'success', datetime('now', ?))",
            (sha, f"-{days_ago} days"),
        )
    await db.commit()


async def _prod_defect(
    db: aiosqlite.Connection,
    caused_by: int,
    *,
    found_in: str = "prod",
    days_ago: int = 0,
) -> int:
    defect = await repo.create_task(
        db,
        title="дефект",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.execute(
        "UPDATE tasks SET found_in=?, caused_by_task_id=?, "
        "detected_at=datetime('now', ?) WHERE id=?",
        (found_in, caused_by, f"-{days_ago} days", defect),
    )
    await db.commit()
    return defect


def _refusal_text(refusals) -> str:
    return " ".join(detail for code, detail in refusals if code == "false_approve")


async def _active(db: aiosqlite.Connection) -> list[tuple[int, str, str]]:
    from hub.services.steward_exit import sticky_false_approvals

    return [(f.task_id, f.source, f.ref) for f in await sticky_false_approvals(db)]


async def test_false_approve_sources_are_sticky_and_global(
    db: aiosqlite.Connection, monkeypatch
):
    """#1601 AC-4: три источника ошибочного одобрения; липкое; глобальное.

    Задача, одобренная парой на проекте A, потом поочерёдно: (а) человеческий
    возврат более позднего поколения, (б) переоткрытие, (в) прод-дефект с
    caused_by_task_id. Отдельно — дефект с caused_by_task_id, найденный на
    ревью, ошибочным одобрением не считается. Каждый — отказ false_approve с
    номером задачи и источником, и режим shadow на любом проекте: выборка и
    режим глобальны. Отказ переживает исчезновение данных и снимается только
    явным решением человека.
    """
    from hub.services.steward_exit import clear_false_approval
    from hub.services.steward_shadow import act_refusals, effective_mode, mode_report

    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    project_a = await _project(db, "advisor-ac4-a")
    project_b = await _project(db, "advisor-ac4-b")
    await _clean_act_sample(db, project_a)
    assert await act_refusals(db) == [], "предусловие: до ошибок критерий выполнен"
    assert await effective_mode(db) == "act"

    task_a = await _v2_row(db, project_a, verdict="approve", advisor="concur")
    task_b = await _v2_row(db, project_a, verdict="approve", advisor="concur")
    task_c = await _v2_row(db, project_a, verdict="approve", advisor="concur")
    task_d = await _v2_row(db, project_a, verdict="approve", advisor="concur")
    for task in (task_b, task_c, task_d):
        await _age_pair(db, task, 5)
        await _deliver(db, task, 4)
    assert await effective_mode(db) == "act", "до ошибок пары чистые"

    # (а) человеческий возврат на более позднем поколении.
    await _human(db, task_a, "changes_requested", generation=2)
    assert await effective_mode(db) == "shadow"
    text = _refusal_text(await act_refusals(db))
    assert f"#{task_a}" in text and "human_changes_requested" in text
    report = await mode_report(db)
    assert report["contour"]["false_approve"] == 1
    assert report["contour"]["false_approve_tasks"][0]["task_id"] == task_a

    # Липкость: данные исчезли, человек «передумал» — отказ стоит.
    await db.execute("DELETE FROM events WHERE kind='review_verdict_recorded'")
    await db.commit()
    await _human(db, task_a, "approved", generation=2)
    assert await effective_mode(db) == "shadow"
    assert f"#{task_a}" in _refusal_text(await act_refusals(db))

    # (б) переоткрытие: доставлена, завершена и снова в работе.
    await db.execute(
        "UPDATE tasks SET status='running', completed_at=datetime('now','-3 days'), "
        "status_entered_at=datetime('now','-1 days') WHERE id=?",
        (task_b,),
    )
    await db.commit()
    text = _refusal_text(await act_refusals(db))
    assert f"#{task_b}" in text and "reopened" in text

    # (в) прод-дефект с caused_by_task_id.
    await db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_c,))
    defect = await _prod_defect(db, task_c, days_ago=1)
    await db.commit()
    text = _refusal_text(await act_refusals(db))
    assert f"#{task_c}" in text and "prod_defect" in text and f"#{defect}" in text

    # Дефект, найденный на ревью, ошибочным одобрением не считается.
    await _prod_defect(db, task_d, found_in="review", days_ago=1)
    assert f"#{task_d}" not in _refusal_text(await act_refusals(db))

    # Глобальность: режим один на хаб; чужой проект ошибку не гасит.
    report = await mode_report(db)
    assert report["requested"] == "act" and report["effective"] == "shadow"
    assert {t["task_id"] for t in report["contour"]["false_approve_tasks"]} == {
        task_a,
        task_b,
        task_c,
    }
    await _clean_act_sample(db, project_b)
    assert await effective_mode(db) == "shadow"

    # Снимает только явное решение человека — по названной задаче.
    assert await clear_false_approval(db, task_a, "denis", "разобрано") == 1
    assert await effective_mode(db) == "shadow", "b и c ещё стоят"
    await clear_false_approval(db, task_b, "denis")
    await clear_false_approval(db, task_c, "denis")
    assert await act_refusals(db) == []
    assert await effective_mode(db) == "act"


async def test_a_false_approve_is_not_a_sample_size_artifact(
    db: aiosqlite.Connection, monkeypatch
):
    """Отказ false_approve — отдельное слово, а не побочное следствие малой выборки."""
    from hub.services.steward_shadow import act_refusals

    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    project_id = await _project(db, "advisor-fa-alone")
    await _clean_act_sample(db, project_id)
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _human(db, task_id, "changes_requested")

    assert [code for code, _ in await act_refusals(db)] == ["false_approve"]


async def test_an_objecting_pair_is_not_a_false_approve(db: aiosqlite.Connection):
    """Пара возразила, человек вернул — пара оказалась права, ошибки нет."""
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-object")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="object")
    await _human(db, task_id, "changes_requested")

    assert await current_false_approvals(db) == []


async def test_a_judge_alone_returned_by_a_human_is_not_a_pair_error(
    db: aiosqlite.Connection,
):
    """Одиночный approve судьи без советника парой не был — старые не зачитываются."""
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-single")
    task_id = await _v2_row(db, project_id, verdict="approve")
    await _human(db, task_id, "changes_requested")

    assert await current_false_approvals(db) == []


async def test_a_human_return_of_an_earlier_generation_is_not_the_pairs_error(
    db: aiosqlite.Connection,
):
    """Возврат поколения 1, пара одобрила поколение 2 — это прошлое, не ошибка пары."""
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-earlier")
    task_id = await _v2_row(
        db, project_id, verdict="approve", advisor="concur", generation=2
    )
    await _human(db, task_id, "changes_requested", generation=1)

    assert await current_false_approvals(db) == []


async def test_a_return_stays_a_false_approve_after_a_later_approve(
    db: aiosqlite.Connection,
):
    """Возврат — факт: последующий approved человека его НЕ снимает (только clear).

    Прежняя версия читала лишь последний вердикт поколения, и «вернул, потом
    одобрил» стирало ошибку без решения человека. Теперь учитывается сам
    возврат и он закрепляется при обнаружении.
    """
    from hub.services.steward_exit import (
        clear_false_approval,
        current_false_approvals,
        record_false_approvals,
    )

    project_id = await _project(db, "advisor-fa-retaken")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _human(db, task_id, "changes_requested")
    await _human(db, task_id, "approved")

    assert [f.task_id for f in await current_false_approvals(db)] == [task_id]
    await record_false_approvals(db)
    await _human(db, task_id, "approved")
    assert [f.task_id for f in await current_false_approvals(db)] == [task_id]
    assert await clear_false_approval(db, task_id, "denis") == 1
    assert await current_false_approvals(db) == []


async def test_an_automatic_verdict_is_not_a_human_return(db: aiosqlite.Connection):
    """Подпись политики — не человеческий возврат (тот же список, что у таблицы тени)."""
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-policy")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor="policy",
        payload={"verdict": "changes_requested", "submission_generation": 1},
    )
    await db.commit()

    assert await current_false_approvals(db) == []


async def test_a_human_return_before_the_pair_answered_is_not_counted(
    db: aiosqlite.Connection,
):
    """Нижняя граница окна — момент одобрения: прежний возврат пару не обвиняет."""
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-before")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _human(db, task_id, "changes_requested", days_ago=2)
    await _age_pair(db, task_id, 0)
    await db.execute(
        "UPDATE steward_judgements SET created_at=datetime('now') WHERE task_id=?",
        (task_id,),
    )
    await db.commit()

    assert await current_false_approvals(db) == []


async def test_the_window_is_bounded_on_both_sides_and_anchored_to_the_delivery(
    db: aiosqlite.Connection,
):
    """Окно: [доставка, доставка+30 дней] для дефекта и переоткрытия; обе границы.

    Доставка берётся из реестра мержей гейта, а НЕ из ``completed_at``. Без
    зафиксированной доставки дефект и переоткрытие не считаются вовсе; дефект
    ДО доставки и позже окна — тоже.
    """
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-window")

    async def _case(*, delivered_days_ago: int | None, defect_days_ago: int) -> int:
        task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
        await _age_pair(db, task_id, 60)
        if delivered_days_ago is not None:
            await _deliver(db, task_id, delivered_days_ago)
        await _prod_defect(db, task_id, days_ago=defect_days_ago)
        return task_id

    inside = await _case(delivered_days_ago=40, defect_days_ago=20)  # +20 дн.
    edge = await _case(delivered_days_ago=40, defect_days_ago=11)  # +29 дн.
    after = await _case(delivered_days_ago=40, defect_days_ago=9)  # +31 дн.
    before = await _case(delivered_days_ago=10, defect_days_ago=20)  # до доставки
    undelivered = await _case(delivered_days_ago=None, defect_days_ago=1)

    flagged = {f.task_id for f in await current_false_approvals(db)}
    assert inside in flagged and edge in flagged
    assert after not in flagged, "позже окна"
    assert before not in flagged, "дефект раньше доставки"
    assert undelivered not in flagged, "доставки не зафиксировано"


async def test_a_second_completion_does_not_move_the_window(db: aiosqlite.Connection):
    """Повторное завершение задачи не двигает окно: якорь — мерж, а не completed_at."""
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-recomplete")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _age_pair(db, task_id, 60)
    await _deliver(db, task_id, 50)
    await _prod_defect(db, task_id, days_ago=1)  # +49 дней: вне окна
    # Повторное завершение «сегодня» сделало бы дефект «свежим» по completed_at.
    await db.execute(
        "UPDATE tasks SET status='completed', completed_at=datetime('now') WHERE id=?",
        (task_id,),
    )
    await db.commit()

    assert await current_false_approvals(db) == []


async def test_a_reopening_needs_a_recorded_delivery(db: aiosqlite.Connection):
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-reopen-nodelivery")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await db.execute(
        "UPDATE tasks SET status='running', completed_at=datetime('now','-3 days'), "
        "status_entered_at=datetime('now','-1 days') WHERE id=?",
        (task_id,),
    )
    await db.commit()
    assert await current_false_approvals(db) == [], "доставки нет — переоткрытия нет"

    await _age_pair(db, task_id, 5)
    await _deliver(db, task_id, 4)
    assert [f.source for f in await current_false_approvals(db)] == ["reopened"]


async def test_a_manual_delivery_anchors_the_window(db: aiosqlite.Connection):
    """Доставка мимо гейта (outside_gate) — тоже доставка: дефект в окне засчитан.

    Якорь берётся из учёта доставки хаба (``delivery_discrepancies``), а не из
    реестра мержей гейта; раньше такая пара давала «[]» при прод-дефекте.
    """
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-manual")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _age_pair(db, task_id, 6)
    await repo.record_delivery_discrepancy(
        db,
        task_id=task_id,
        state="delivered",
        reason="код в базовой ветке, но мерж прошёл мимо гейта",
        delivery_path="outside_gate",
    )
    await db.execute(
        "UPDATE delivery_discrepancies SET checked_at=datetime('now','-4 days') "
        "WHERE task_id=?",
        (task_id,),
    )
    defect = await _prod_defect(db, task_id, days_ago=1)

    found = await current_false_approvals(db)

    assert [(f.source, f.ref) for f in found] == [("prod_defect", str(defect))]


async def test_a_manual_delivery_found_before_the_approval_is_not_this_deliveries(
    db: aiosqlite.Connection,
):
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-manual-old")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _age_pair(db, task_id, 3)
    await repo.record_delivery_discrepancy(
        db, task_id=task_id, state="delivered", reason="", delivery_path="outside_gate"
    )
    await db.execute(
        "UPDATE delivery_discrepancies SET checked_at=datetime('now','-20 days') "
        "WHERE task_id=?",
        (task_id,),
    )
    await _prod_defect(db, task_id, days_ago=1)
    await db.commit()

    assert await current_false_approvals(db) == []


async def test_a_merge_without_a_release_is_not_a_delivery(db: aiosqlite.Connection):
    """Мерж гейта без выката в прод доставкой не считается (проект с релизами)."""
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-unreleased")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _age_pair(db, task_id, 5)
    await _deliver(db, task_id, 4, released=False)
    await _prod_defect(db, task_id, days_ago=1)

    assert await current_false_approvals(db) == []


async def _merge_is_delivery_project(db: aiosqlite.Connection, slug: str) -> int:
    project_id = await _project(db, slug)
    await db.execute(
        "UPDATE projects SET gate_policy=? WHERE id=?",
        (json.dumps({"verdict": "steward", "merge_is_delivery": True}), project_id),
    )
    await db.commit()
    return project_id


async def _bare_merge(
    db: aiosqlite.Connection, project_id: int, task_id: int, days_ago: int
) -> None:
    _PR[0] += 1
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merged_at) "
        "VALUES (?, ?, ?, datetime('now', ?))",
        (project_id, _PR[0], task_id, f"-{days_ago} days"),
    )
    await db.commit()


async def test_merge_is_delivery_makes_the_merge_the_delivery(
    db: aiosqlite.Connection,
):
    """Проект объявил «мерж = доставка» и релизов нет — окно от мержа (#1572)."""
    from hub.services.steward_exit import current_false_approvals

    project_id = await _merge_is_delivery_project(db, "advisor-fa-mid")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _age_pair(db, task_id, 6)
    await _bare_merge(db, project_id, task_id, 5)
    defect = await _prod_defect(db, task_id, days_ago=1)

    found = await current_false_approvals(db)

    assert [(f.source, f.ref) for f in found] == [("prod_defect", str(defect))]


async def test_a_separate_release_moves_the_window_to_the_deploy(
    db: aiosqlite.Connection,
):
    """Проект с отдельным релизом: окно от ВЫКАТА, а не от мержа.

    Дефект между мержем и выкатом в окно не входит (кода ещё нет в проде),
    дефект после выката — входит.
    """
    from hub.services.steward_exit import current_false_approvals

    project_id = await _merge_is_delivery_project(db, "advisor-fa-release")
    before = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    after = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    for task in (before, after):
        await _age_pair(db, task, 20)
        await _bare_merge(db, project_id, task, 15)
    await db.execute(
        "INSERT INTO releases (project_id, deployed_sha, status, deployed_at) "
        "VALUES (?, 'rel-1', 'success', datetime('now', '-10 days'))",
        (project_id,),
    )
    await db.commit()
    await _prod_defect(db, before, days_ago=12)  # между мержем и выкатом
    late = await _prod_defect(db, after, days_ago=5)  # после выката

    found = await current_false_approvals(db)

    assert [(f.task_id, f.ref) for f in found] == [(after, str(late))]


async def test_the_window_ends_thirty_days_after_the_deploy_not_the_merge(
    db: aiosqlite.Connection,
):
    from hub.services.steward_exit import current_false_approvals

    project_id = await _merge_is_delivery_project(db, "advisor-fa-release-end")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _age_pair(db, task_id, 80)
    await _bare_merge(db, project_id, task_id, 75)
    await db.execute(
        "INSERT INTO releases (project_id, deployed_sha, status, deployed_at) "
        "VALUES (?, 'rel-late', 'success', datetime('now', '-40 days'))",
        (project_id,),
    )
    await db.commit()
    # +35 дней от мержа, но +5 от выката: от выката — внутри окна.
    await _prod_defect(db, task_id, days_ago=35)

    assert [f.source for f in await current_false_approvals(db)] == ["prod_defect"]


async def test_the_reopening_case_is_stable_across_later_transitions(
    db: aiosqlite.Connection,
):
    """completed → open → найдено → clear → claimed: нового случая нет.

    Ref — завершение, из которого вернули задачу, а не время последнего
    перехода: его двигает любой transition_status_if.
    """
    from hub.services.steward_exit import (
        clear_false_approval,
        current_false_approvals,
        record_false_approvals,
    )

    project_id = await _project(db, "advisor-fa-reopen-stable")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _age_pair(db, task_id, 8)
    await _deliver(db, task_id, 7)
    await db.execute(
        "UPDATE tasks SET status='completed', completed_at=datetime('now','-6 days') "
        "WHERE id=?",
        (task_id,),
    )
    await db.commit()
    assert await current_false_approvals(db) == []

    await db.execute(
        "UPDATE tasks SET status='open', status_entered_at=datetime('now','-3 days') "
        "WHERE id=?",
        (task_id,),
    )
    await db.commit()
    found = await current_false_approvals(db)
    assert [f.source for f in found] == ["reopened"]
    await record_false_approvals(db)
    assert await clear_false_approval(db, task_id, "denis") == 1

    # Следующие переходы двигают status_entered_at, но не создают нового случая.
    for status, days in (("claimed", 2), ("running", 1)):
        await db.execute(
            "UPDATE tasks SET status=?, status_entered_at=datetime('now', ?) "
            "WHERE id=?",
            (status, f"-{days} days", task_id),
        )
        await db.commit()
        await record_false_approvals(db)
        assert await current_false_approvals(db) == [], status

    # Настоящее новое переоткрытие: завершили заново и снова вернули.
    await db.execute(
        "UPDATE tasks SET status='completed', completed_at=datetime('now','-1 days') "
        "WHERE id=?",
        (task_id,),
    )
    await db.commit()
    await db.execute(
        "UPDATE tasks SET status='open', status_entered_at=datetime('now') WHERE id=?",
        (task_id,),
    )
    await db.commit()
    again = await current_false_approvals(db)
    assert [f.source for f in again] == ["reopened"]


async def test_a_pair_with_a_closed_window_is_not_read_on_the_tick(
    db: aiosqlite.Connection, monkeypatch
):
    """Пара старше горизонта чтения на тике не разбирается — ни одним запросом.

    Закреплённые ошибки при этом читаются из таблицы и в отказах остаются.
    """
    from hub.services import steward_exit
    from hub.services.steward_exit import (
        PAIR_READ_HORIZON_DAYS,
        current_false_approvals,
        record_false_approvals,
    )

    project_id = await _project(db, "advisor-fa-horizon")
    old = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    fresh = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _human(db, old, "changes_requested")
    await record_false_approvals(db)  # закреплено, пока пара была свежей
    await _age_pair(db, old, 95)
    assert PAIR_READ_HORIZON_DAYS == 90, "30 дней окна + 60 запаса"
    inside = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _age_pair(db, inside, 85)

    read: list[int] = []
    real = steward_exit._detect_for_pair

    async def _spy(db_, pair, projects):
        read.append(pair.task_id)
        return await real(db_, pair, projects)

    monkeypatch.setattr(steward_exit, "_detect_for_pair", _spy)

    found = await current_false_approvals(db)

    assert sorted(read) == sorted([fresh, inside]), "пара с закрытым окном не читается"
    assert [f.task_id for f in found] == [old], "закреплённая остаётся в отказах"


async def test_an_old_deploy_is_not_the_delivery_of_a_new_approval(
    db: aiosqlite.Connection,
):
    """Выкат ДО ответа пары — доставка прошлого одобрения; нового одобрения ещё нет в проде."""
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-old-deploy")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _deliver(db, task_id, 20)  # старый выкат
    await _age_pair(db, task_id, 3)  # новое одобрение, ещё не выкачено
    await _prod_defect(db, task_id, days_ago=1)

    assert await current_false_approvals(db) == []


async def test_release_stamps_in_iso_form_are_compared_as_times(
    db: aiosqlite.Connection,
):
    """Релиз пишет метку ISO («…T…Z»); сравнивается время, а не строка.

    Выкат за два часа ДО ответа пары в тот же день строкой «больше» (T > пробел),
    временем — раньше: доставкой этого одобрения он быть не может.
    """
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-iso")
    early = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    late = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await db.execute("UPDATE steward_judgements SET created_at='2026-09-01 12:00:00'")
    for task, sha, stamp in (
        (early, "iso-early", "2026-09-01T10:00:00Z"),
        (late, "iso-late", "2026-09-01T14:00:00Z"),
    ):
        await db.execute(
            "INSERT INTO pipeline_merges (project_id, pr_number, task_id, "
            "released_sha) VALUES (1, ?, ?, ?)",
            (9500 + task, task, sha),
        )
        await db.execute(
            "INSERT INTO releases (project_id, deployed_sha, status, deployed_at) "
            "VALUES (1, ?, 'success', ?)",
            (sha, stamp),
        )
        await db.execute("UPDATE tasks SET found_in=found_in WHERE id=?", (task,))
    await db.commit()
    for task in (early, late):
        defect = await _prod_defect(db, task, days_ago=0)
        await db.execute(
            "UPDATE tasks SET detected_at='2026-09-02 09:00:00' WHERE id=?", (defect,)
        )
    await db.commit()

    flagged = {f.task_id for f in await current_false_approvals(db)}
    assert flagged == {late}


async def test_a_completion_before_the_delivery_is_not_a_reopening_of_it(
    db: aiosqlite.Connection,
):
    """Завершение раньше доставки — не то завершение, из которого вернули доставленное."""
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-reopen-early")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _age_pair(db, task_id, 12)
    await _deliver(db, task_id, 4)
    await db.execute(
        "UPDATE tasks SET status='open', completed_at=datetime('now','-10 days'), "
        "status_entered_at=datetime('now','-1 days') WHERE id=?",
        (task_id,),
    )
    await db.commit()

    assert await current_false_approvals(db) == []


async def test_a_human_return_after_the_window_is_not_counted(
    db: aiosqlite.Connection,
):
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-window-human")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _age_pair(db, task_id, 60)
    await _deliver(db, task_id, 50)
    await _human(db, task_id, "changes_requested")  # сегодня: +50 дней

    assert await current_false_approvals(db) == []


async def test_clearing_closes_the_case_not_the_task(db: aiosqlite.Connection):
    """Снятие закрывает КОНКРЕТНЫЙ случай; новый случай той же задачи — новая запись.

    Прежний вариант исключал задачу целиком: после clear любой следующий
    прод-дефект той же задачи оставался невидимым.
    """
    from hub.services.steward_exit import (
        clear_false_approval,
        current_false_approvals,
        record_false_approvals,
    )

    project_id = await _project(db, "advisor-fa-case")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _age_pair(db, task_id, 5)
    await _deliver(db, task_id, 4)
    event = await _human(db, task_id, "changes_requested")
    await record_false_approvals(db)

    assert await clear_false_approval(db, task_id, "denis", "возврат разобран") == 1
    assert await current_false_approvals(db) == [], "снятый случай не возвращается"
    await record_false_approvals(db)
    assert await current_false_approvals(db) == []

    defect = await _prod_defect(db, task_id, days_ago=1)
    now = await current_false_approvals(db)
    assert [(f.source, f.ref) for f in now] == [("prod_defect", str(defect))]
    assert event != defect
    await record_false_approvals(db)
    assert await _active(db) == [(task_id, "prod_defect", str(defect))]


async def test_a_second_return_after_a_clear_is_a_new_active_case(
    db: aiosqlite.Connection,
):
    """Тот же источник, новый случай: второй возврат после снятия первого — новая запись."""
    from hub.services.steward_exit import clear_false_approval, current_false_approvals

    project_id = await _project(db, "advisor-fa-second-return")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    first = await _human(db, task_id, "changes_requested")
    assert await clear_false_approval(db, task_id, "denis") == 1
    second = await _human(db, task_id, "changes_requested")

    found = await current_false_approvals(db)

    assert [(f.source, f.ref) for f in found] == [
        ("human_changes_requested", str(second))
    ]
    assert first != second


async def test_clearing_a_case_found_but_not_yet_recorded_closes_it(
    db: aiosqlite.Connection,
):
    """Найденный, но ещё не закреплённый случай снимается и не возвращается."""
    from hub.services.steward_exit import (
        clear_false_approval,
        current_false_approvals,
        record_false_approvals,
    )

    project_id = await _project(db, "advisor-fa-clear-unrecorded")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _human(db, task_id, "changes_requested")
    assert await _active(db) == [], "ещё не закреплено"

    assert await clear_false_approval(db, task_id, "denis") == 1

    await record_false_approvals(db)
    assert await current_false_approvals(db) == []


async def test_an_earlier_merge_does_not_become_the_anchor_of_a_later_approval(
    db: aiosqlite.Connection,
):
    """Мерж РАНЬШЕ ответа пары — доставка прошлого одобрения, а не этого.

    Якорь — первый мерж после одобрения: иначе дефект этого одобрения считался
    бы от чужой, давней доставки и выпадал из окна.
    """
    from hub.services.steward_exit import current_false_approvals

    project_id = await _project(db, "advisor-fa-anchor")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _age_pair(db, task_id, 5)
    await _deliver(db, task_id, 50)  # доставка прошлого поколения
    await _deliver(db, task_id, 4)  # доставка ЭТОГО одобрения
    await _prod_defect(db, task_id, days_ago=1)

    assert [f.source for f in await current_false_approvals(db)] == ["prod_defect"]


async def test_clearing_without_an_active_case_buys_no_immunity(
    db: aiosqlite.Connection,
):
    """Clear без активной записи — 0 и никакого запаса на будущее."""
    from hub.services.steward_exit import clear_false_approval, current_false_approvals

    project_id = await _project(db, "advisor-fa-no-immunity")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")

    assert await clear_false_approval(db, task_id, "denis", "на всякий случай") == 0

    await _human(db, task_id, "changes_requested")
    assert [f.task_id for f in await current_false_approvals(db)] == [task_id]


async def test_a_human_return_found_in_shadow_survives_the_event_retention(
    db: aiosqlite.Connection, monkeypatch
):
    """Тень → возврат → prune_events → запрос act → отказ false_approve.

    Возврат закрепляется тиком поллера (``sweep_steward_runs``) в ЛЮБОМ
    режиме, а не только при запросе act; к моменту, когда act запросят,
    события возврата уже могут быть вычищены через 14 дней.
    """
    from hub.services.steward_dispatch import sweep_steward_runs
    from hub.services.steward_shadow import act_refusals, effective_mode

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "advisor-fa-retention")
    await _clean_act_sample(db, project_id)
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _human(db, task_id, "changes_requested")

    await sweep_steward_runs(db)
    assert await _active(db) == [
        (task_id, "human_changes_requested", (await _active(db))[0][2])
    ]

    await db.execute(
        "UPDATE events SET created_at=datetime('now','-20 days') WHERE task_id=?",
        (task_id,),
    )
    await db.commit()
    assert await repo.prune_events(db, keep_days=14) >= 1
    await db.commit()
    rows = await fetchall(
        db,
        "SELECT 1 FROM events WHERE kind='review_verdict_recorded' AND task_id=?",
        (task_id,),
    )
    assert list(rows) == [], "основание в events исчезло"

    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    assert "false_approve" in {code for code, _ in await act_refusals(db)}
    assert await effective_mode(db) == "shadow"


async def test_the_pure_report_does_not_write_a_sticky_row(db: aiosqlite.Connection):
    """Сводка (GET) ничего не закрепляет: закрепляют поллер и effective_mode."""
    from hub.services.steward_shadow import mode_report

    project_id = await _project(db, "advisor-fa-pure")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await _human(db, task_id, "changes_requested")

    report = await mode_report(db)

    assert report["contour"]["false_approve"] == 1
    rows = await fetchall(db, "SELECT COUNT(*) AS n FROM steward_false_approvals")
    assert dict(rows[0])["n"] == 0
