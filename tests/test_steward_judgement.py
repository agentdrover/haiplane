"""Суждение без оснований или без уверенности не проходит как вердикт (#1327).

22.09.2026 первое одобрение стюарда на проде (#1271) записано с grounds=[] и
пустой confidence. Такое одобрение нельзя перепроверить, поэтому хаб
понижает его до эскалации — тем же приёмом и в том же месте, что
low_confidence, — а поданный вердикт остаётся в submitted_verdict.

AC-1: approve без оснований → escalate / no_grounds, submitted_verdict=approve.
AC-2: approve без confidence → escalate / no_confidence, submitted_verdict=approve.
AC-3: escalate без оснований принимается как есть: эскалация сама значит
      «судить не могу», требовать от неё фактов незачем.

Цена суждения (#1328). 22.09.2026 все суждения первого вечера тени записаны
с пустой моделью и без токенов и длительности, хотя прогон знал и то и
другое. Модель берётся из строки прогона, а не из слов стюарда; длительность
от старта прогона; токены — от провайдера, дописываются свипом, а неответ
провайдера назван причиной, а не записан нулём.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiosqlite

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.db import fetchall
from hub.models import STEWARD_ESCALATE_REASONS, StewardJudgementSubmit
from hub.services import steward_shadow
from hub.services.steward_dispatch import RUN_JUDGED, order_run, sweep_steward_runs
from hub.services.steward_judgement import (
    TOKENS_PENDING,
    TOKENS_PROVIDER_NO_ANSWER,
    USAGE_ANSWER_WINDOW_MIN,
    record_steward_judgement,
)
from tests.test_steward_shadow import _project, _task

_STEWARD = TokenIdentity("steward-bot", "steward", principal_id=42)


async def _record(db: aiosqlite.Connection, **fields):
    project_id = await _project(db, "grounds")
    task_id = await _task(db, project_id)
    body = {"generation": 1, "kind": "verdict", "model": "gpt-5.3-codex"}
    body.update(fields)
    view = await record_steward_judgement(
        db, task_id, StewardJudgementSubmit(**body), _STEWARD
    )
    row = await repo.get_steward_judgement(db, task_id, 1, "verdict")
    assert row is not None
    return view, row


async def test_an_approval_without_grounds_is_recorded_as_an_escalation(
    db: aiosqlite.Connection,
):
    """AC-1: approve, confidence=high, grounds=[] — случай #1271 без одной детали."""
    view, row = await _record(db, verdict="approve", confidence="high", grounds=[])
    assert view.verdict == "escalate"
    assert view.escalate_reason == "no_grounds"
    assert view.submitted_verdict == "approve"
    assert row["verdict"] == "escalate"
    assert row["escalate_reason"] == "no_grounds"
    assert row["submitted_verdict"] == "approve"
    assert "no_grounds" in STEWARD_ESCALATE_REASONS


async def test_a_return_without_grounds_is_recorded_as_an_escalation(
    db: aiosqlite.Connection,
):
    """AC-1, вторая половина: возврат без оснований понижается так же."""
    view, _ = await _record(
        db, verdict="changes_requested", confidence="high", grounds=[]
    )
    assert view.verdict == "escalate"
    assert view.escalate_reason == "no_grounds"
    assert view.submitted_verdict == "changes_requested"


async def test_an_approval_without_confidence_is_recorded_as_an_escalation(
    db: aiosqlite.Connection,
):
    """AC-2: основания названы, уверенности нет."""
    view, row = await _record(
        db, verdict="approve", grounds=[{"source": "ci_pinned_sha"}]
    )
    assert view.verdict == "escalate"
    assert view.escalate_reason == "no_confidence"
    assert view.submitted_verdict == "approve"
    assert row["verdict"] == "escalate"
    assert row["escalate_reason"] == "no_confidence"
    assert row["submitted_verdict"] == "approve"
    assert "no_confidence" in STEWARD_ESCALATE_REASONS


async def test_a_grounded_confident_approval_stays_an_approval(
    db: aiosqlite.Connection,
):
    """Контроль: правило не задевает обоснованное одобрение."""
    view, _ = await _record(
        db,
        verdict="approve",
        confidence="medium",
        grounds=[{"source": "ci_pinned_sha"}],
    )
    assert view.verdict == "approve"
    assert view.escalate_reason == ""


async def test_an_escalation_needs_no_grounds(db: aiosqlite.Connection):
    """AC-3: эскалация без оснований и без уверенности принята как есть."""
    view, row = await _record(
        db, verdict="escalate", escalate_reason="precondition_failed", grounds=[]
    )
    assert view.verdict == "escalate"
    assert view.escalate_reason == "precondition_failed"
    assert view.submitted_verdict == "escalate"
    assert row["escalate_reason"] == "precondition_failed"


# ---------------------------------------------------------------------------
# Цена суждения (#1328)
# ---------------------------------------------------------------------------

_CREATED = {"agent": {"id": "agent-1"}, "run": {"id": "run-1"}}


def _steward_on(monkeypatch) -> None:
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 20)
    monkeypatch.setattr(config, "STEWARD_RUN_DEADLINE_MIN", 30)
    monkeypatch.setattr(config, "STEWARD_MODEL", "gpt-5.3-codex")
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", "steward-token")
    monkeypatch.setattr(config, "CURSOR_API_KEY", "cursor-key")

    async def _delivery(_db, _task_id, _generation, _base_url):
        return "код доступа: ABC-123"

    monkeypatch.setattr(steward_shadow, "identity_delivery", _delivery)


async def _started_run(db: aiosqlite.Connection, monkeypatch, slug: str) -> int:
    """Заказ, начатый НАСТОЯЩИМ стартом: отметку старта ставит он, не тест."""
    _steward_on(monkeypatch)
    task_id = await _task(db, await _project(db, slug))
    assert await order_run(db, task_id, 1) is not None
    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ):
        assert await steward_shadow.start_due_runs(db) == 1
    return task_id


async def _judge(db: aiosqlite.Connection, task_id: int, **fields):
    body = {
        "generation": 1,
        "kind": "verdict",
        "verdict": "escalate",
        "escalate_reason": "precondition_failed",
    }
    body.update(fields)
    # Провайдер при записи НЕ зовётся: запись не ждёт его и не падает из-за него.
    with (
        patch(
            "hub.integrations.cursor_cloud.get_usage",
            new=AsyncMock(side_effect=AssertionError("usage при записи")),
        ),
        patch(
            "hub.integrations.cursor_cloud.get_run",
            new=AsyncMock(side_effect=AssertionError("run при записи")),
        ),
    ):
        await record_steward_judgement(
            db, task_id, StewardJudgementSubmit(**body), _STEWARD
        )
    row = await repo.get_steward_judgement(db, task_id, 1, "verdict")
    assert row is not None
    return dict(row)


async def _sweep(db: aiosqlite.Connection, *, usage, run=None) -> AsyncMock:
    got_usage = AsyncMock(return_value=usage)
    with (
        patch("hub.integrations.cursor_cloud.get_usage", new=got_usage),
        patch(
            "hub.integrations.cursor_cloud.get_run",
            new=AsyncMock(return_value=run or {"status": "FINISHED"}),
        ),
        patch(
            "hub.integrations.cursor_cloud.create_agent_attempt",
            new=AsyncMock(side_effect=AssertionError("свип не заказывает судью")),
        ),
    ):
        await sweep_steward_runs(db)
    return got_usage


async def test_a_judgement_records_the_model_of_its_run(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-1: модель суждения — модель прогона, а не то, что стюард назвал.

    На проде стюард писал пустую строку или «codex-5.3», а прогон при этом
    шёл на gpt-5.3-codex. Декларация здесь нарочно другая.
    """
    task_id = await _started_run(db, monkeypatch, "cost-model")
    row = await _judge(db, task_id, model="codex-5.3")
    assert row["model"] == "gpt-5.3-codex"

    silent = await _started_run(db, monkeypatch, "cost-model-silent")
    assert (await _judge(db, silent))["model"] == "gpt-5.3-codex"


async def test_a_judgement_records_tokens_and_duration(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-2: длительность — от старта прогона, токены — из ответа провайдера.

    Старт отодвинут на 90 секунд назад: время заказа (created_at) тут
    ни при чём, заказ мог ждать сколько угодно.
    """
    task_id = await _started_run(db, monkeypatch, "cost-tokens")
    run = (
        await fetchall(
            db, "SELECT started_at FROM steward_runs WHERE task_id=?", (task_id,)
        )
    )[0]
    assert run["started_at"], "старт прогона обязан поставить отметку"
    await db.execute(
        "UPDATE steward_runs SET started_at=strftime('%Y-%m-%d %H:%M:%f', "
        "'now', '-90 seconds'), created_at=datetime('now', '-1 hour') "
        "WHERE task_id=?",
        (task_id,),
    )
    await db.commit()

    row = await _judge(db, task_id, tokens_spent=7, duration_ms=1)
    assert 89_000 <= row["duration_ms"] < 150_000
    # Токены стюарда — не данные провайдера: до ответа провайдера их нет.
    assert row["tokens_spent"] is None
    assert row["tokens_unknown_reason"] == TOKENS_PENDING
    status = (
        await fetchall(
            db, "SELECT status FROM steward_runs WHERE task_id=?", (task_id,)
        )
    )[0]["status"]
    assert status == RUN_JUDGED

    asked = await _sweep(db, usage={"totalUsage": {"totalTokens": 12345}})
    assert asked.await_args.args == ("agent-1", "run-1")
    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert row["tokens_spent"] == 12345
    assert row["tokens_unknown_reason"] == ""


async def test_an_unanswered_usage_is_unknown_not_zero(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-3: провайдер молчит — суждение стоит, токены «неизвестно», не ноль.

    Пока окно ответа не вышло, причина остаётся pending: usage приходит с
    задержкой, и назвать молчание окончательным в первые секунды значило
    бы потерять цену, которую провайдер отдал бы через минуту.
    """
    task_id = await _started_run(db, monkeypatch, "cost-silent")
    row = await _judge(db, task_id)
    assert row["verdict"] == "escalate"
    assert row["tokens_spent"] is None

    await _sweep(db, usage=None)
    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert row["tokens_spent"] is None
    assert row["tokens_unknown_reason"] == TOKENS_PENDING

    await db.execute(
        "UPDATE steward_judgements SET created_at=datetime('now', ?) WHERE task_id=?",
        (f"-{USAGE_ANSWER_WINDOW_MIN + 1} minutes", task_id),
    )
    await db.commit()
    await _sweep(db, usage=None)
    row = dict(await repo.get_steward_judgement(db, task_id, 1, "verdict"))
    assert row["tokens_spent"] is None
    assert row["tokens_unknown_reason"] == TOKENS_PROVIDER_NO_ANSWER
