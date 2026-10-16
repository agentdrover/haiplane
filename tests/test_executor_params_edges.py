"""Края заказа исполнителя с params (#1769): находки независимого Codex.

AC-тесты живут в test_executor_dispatch.py (их test_ref); здесь — края,
вынесенные ради потолка размера файла.
"""

from __future__ import annotations

from hub import config
from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.services import executor_launch as el
from tests.test_executor_dispatch import (  # noqa: F401
    _CREATED,
    _EXEC_MODEL,
    _VARIANT_RAW,
    _creator,
    _human,
    _last_model,
    _launch_config,
    _launch_project,
    cursor_key,
)


async def test_a_blind_order_keeps_the_sent_variant(db, monkeypatch):
    """Находка Codex P2: слепой исход бронь держит, модель — отправленный вариант."""
    _launch_config(monkeypatch)
    monkeypatch.setattr(config, "EXECUTOR_MODEL_PARAMS", _VARIANT_RAW)

    async def _blind(_name, pages=3):
        return cursor_cloud.Reconciliation("", "", False)

    monkeypatch.setattr(cursor_cloud, "find_agent_by_name", _blind)
    _creator(monkeypatch, [cursor_cloud.Refusal(status=0, detail="ReadTimeout")])
    project, task_id = await _launch_project(db, slug="exec-var-blind")
    human = await _human(db)
    result = await el.launch_executor(db, project, issuer_principal_id=human)
    assert not result.launched
    assert await _last_model(db, task_id) == f"{_EXEC_MODEL} {_VARIANT_RAW}"


def test_the_reservation_term_covers_the_order_without_params(monkeypatch):
    """Находка Codex P1: заказ без params идёт сверх попыток — бронь живёт дольше."""
    from datetime import UTC, datetime, timedelta

    monkeypatch.setattr(config, "EXECUTOR_LAUNCH_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(config, "EXECUTOR_LAUNCH_PAUSE_S", 10)
    timeout = cursor_cloud.CREATE_TIMEOUT_S

    def _age(seconds: float) -> dict:
        at = datetime.now(UTC) - timedelta(seconds=seconds)
        return {"started_at": at.replace(tzinfo=None).isoformat()}

    assert not el._reservation_abandoned(_age(timeout + 10 + 60 + 30))
    assert el._reservation_abandoned(_age(2 * timeout + 10 + 60 + 30))


async def test_an_order_does_not_outlive_the_implementer_code(db, monkeypatch):
    """Находка Codex P2: цепочка 400 → 429 → … дольше срока кода — агент не
    заказывается, отказ называет причину, бронь снята, ушедший вариант записан."""
    _launch_config(monkeypatch)
    monkeypatch.setattr(config, "EXECUTOR_MODEL_PARAMS", _VARIANT_RAW)
    now = [1000.0]
    monkeypatch.setattr(el, "_clock", lambda: now[0])
    bad = cursor_cloud.Refusal(status=400, code="invalid_argument")
    rate = cursor_cloud.Refusal(status=429, code="rate_limit_exceeded")
    calls = _creator(monkeypatch, [bad, rate, rate, _CREATED])
    real = cursor_cloud.create_agent_attempt

    async def _slow(**kw):
        now[0] += 110  # каждый вызов провайдера идёт 110 с
        return await real(**kw)

    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", _slow)
    project, task_id = await _launch_project(db, slug="exec-code-ttl")
    result = await el.launch_executor(db, project, issuer_principal_id=await _human(db))

    assert not result.launched
    assert result.reason.startswith(el.REASON_CODE_EXPIRING), result
    assert len(calls) == 2, "третий вызов с просроченным кодом не ушёл"
    runs = [dict(r) for r in await repo.list_executor_runs(db, task_id)]
    assert runs[-1]["outcome"] == "failed"
    assert runs[-1]["model"] == _EXEC_MODEL, "последним ушёл голый заказ"


async def test_a_failed_order_records_the_variant_it_sent(db, monkeypatch):
    """Находка Codex P2: исчерпание попыток и отказ — модель строки несёт
    вариант с params, хотя агента нет."""
    _launch_config(monkeypatch)
    monkeypatch.setattr(config, "EXECUTOR_MODEL_PARAMS", _VARIANT_RAW)
    rate = cursor_cloud.Refusal(status=429, code="rate_limit_exceeded")
    forbidden = cursor_cloud.Refusal(status=403)
    human = await _human(db)
    variant = f"{_EXEC_MODEL} {_VARIANT_RAW}"
    for slug, answers in (("exec-fail-rate", [rate]), ("exec-fail-403", [forbidden])):
        _creator(monkeypatch, answers)
        project, task_id = await _launch_project(db, slug=slug)
        result = await el.launch_executor(db, project, issuer_principal_id=human)
        assert not result.launched
        assert await _last_model(db, task_id) == variant, slug

    async def _empty(_name, pages=3):
        return cursor_cloud.Reconciliation("", "", True)

    monkeypatch.setattr(cursor_cloud, "find_agent_by_name", _empty)
    _creator(monkeypatch, [cursor_cloud.Refusal(status=0, detail="ReadTimeout")])
    project, task_id = await _launch_project(db, slug="exec-fail-lost")
    assert not (
        await el.launch_executor(db, project, issuer_principal_id=human)
    ).launched
    assert await _last_model(db, task_id) == variant


async def test_expiry_after_the_rejected_order_keeps_the_params_variant(
    db, monkeypatch
):
    """Находка Codex P2: голый заказ не ушёл (код истекает) — в строке вариант
    с params, который был отправлен."""
    _launch_config(monkeypatch)
    monkeypatch.setattr(config, "EXECUTOR_MODEL_PARAMS", _VARIANT_RAW)
    now = [1000.0]
    monkeypatch.setattr(el, "_clock", lambda: now[0])
    calls = _creator(monkeypatch, [cursor_cloud.Refusal(status=400), _CREATED])
    real = cursor_cloud.create_agent_attempt

    async def _slow(**kw):
        now[0] += 200
        return await real(**kw)

    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", _slow)
    project, task_id = await _launch_project(db, slug="exec-expire-after-400")
    result = await el.launch_executor(db, project, issuer_principal_id=await _human(db))

    assert result.reason.startswith(el.REASON_CODE_EXPIRING), result
    assert len(calls) == 1
    assert await _last_model(db, task_id) == f"{_EXEC_MODEL} {_VARIANT_RAW}"


async def test_the_order_call_has_a_total_deadline(db, monkeypatch):
    """Находка Codex P2: зависший вызов обрывается общим сроком, исход
    сверяется по имени, а не считается успехом."""
    import asyncio

    _launch_config(monkeypatch)
    monkeypatch.setattr(cursor_cloud, "CREATE_TIMEOUT_S", 0.05)
    seen: list[str] = []

    async def _hang(**kw):
        await asyncio.sleep(5)

    async def _empty(name, pages=3):
        seen.append(name)
        return cursor_cloud.Reconciliation("", "", True)

    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", _hang)
    monkeypatch.setattr(cursor_cloud, "find_agent_by_name", _empty)
    project, _ = await _launch_project(db, slug="exec-total-deadline")
    result = await el.launch_executor(db, project, issuer_principal_id=await _human(db))

    assert not result.launched
    assert result.reason.startswith(el.REASON_ANSWER_LOST), result
    assert len(seen) == 1
