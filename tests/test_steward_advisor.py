"""Советник-критик стюарда: запись ответа, старт, состояния, применение (#1601).

Судья в одиночку approve вердиктом не делает. Проверяется не «умеет ли советник
ответить», а границы, на которых держится недоверие к одному судье:

* канал — суждение советника пишет только сессия советника, судья за него не
  ответит;
* привязка — ответ хаб привязывает сам (суждение судьи, поколение, хеш пакета,
  фактическая модель), а не принимает со слов;
* каждый путь к записи вердикта проходит мимо одного вопроса — согласен ли
  советник на том же пакете;
* применение — ровно один раз, и переживает перезапуск.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.db import fetchall
from hub.integrations import cursor_cloud, local_reviewer
from hub.models import StewardJudgementSubmit
from hub.services import chat_pair
from hub.services import steward_shadow as sh
from hub.services.steward_advisor import (
    REFUSED_ADVISOR,
    STATE_NOT_ORDERED,
    STATE_PENDING,
    STATE_RECEIVED,
    STATE_REFUSED,
    STATE_TIMEOUT,
    advisor_refusal,
    advisor_state,
    apply_advisor_outcomes,
    order_due_advisors,
)
from hub.services.steward_dispatch import (
    KIND_ADVISOR,
    RUN_OPEN,
    close_finished_runs,
    order_run,
)
from hub.services.steward_evidence import build_evidence_packet, packet_hash
from hub.services.steward_judgement import record_steward_judgement
from tests.local_advisor_support import GLM as _GLM
from tests.local_advisor_support import beat_caps as _beat_caps
from tests.local_advisor_support import capabilities as _capabilities
from tests.test_steward_apply import _green
from tests.test_steward_shadow import (
    _CREATED,
    _DELIVERY,
    _advise,
    _advisor_identity,
    _advisor_run,
    _events,
    _judge,
    _judge_run,
    _pair_on_packet,
    _project,
    _runs,
    _same_packet,
    _task,
)


@pytest.fixture(autouse=True)
def shadow_mode(monkeypatch):
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 50)
    monkeypatch.setattr(config, "STEWARD_RUN_DEADLINE_MIN", 30)
    monkeypatch.setattr(config, "STEWARD_ADVISOR_WAIT_MAX", 60)
    monkeypatch.setattr(config, "STEWARD_MODEL", "gpt-5.3-codex")
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", "steward-token")
    monkeypatch.setattr(config, "CURSOR_API_KEY", "cursor-key")
    monkeypatch.setattr(config, "STEWARD_REVIEW_WAIT_MAX", 0)


@pytest.fixture
def delivery_kinds(monkeypatch) -> list[str]:
    """Канал доставки подменён и записывает, какого ВИДА сессию он выписал."""
    seen: list[str] = []

    async def _delivery(_db, _task_id, _generation, _base_url, kind="steward"):
        seen.append(kind)
        return _DELIVERY

    monkeypatch.setattr(sh, "identity_delivery", _delivery)
    return seen


def _judge_identity() -> TokenIdentity:
    return TokenIdentity("steward-bot", "steward", principal_id=42)


async def _in_review(db: aiosqlite.Connection, slug: str, **kw) -> int:
    project_id = await _project(db, slug)
    task_id = await _task(db, project_id, **kw)
    await repo.update_task(db, task_id, review_job_id="")
    await db.commit()
    return task_id


def _submit(kind: str, verdict: str, **over) -> StewardJudgementSubmit:
    return StewardJudgementSubmit(
        generation=over.pop("generation", 1),
        kind=kind,
        verdict=verdict,
        confidence=over.pop("confidence", "high"),
        grounds=over.pop("grounds", [{"source": "ci_pinned_sha"}]),
        **over,
    )


async def _judge_row(db: aiosqlite.Connection, task_id: int, kind: str = "verdict"):
    row = await repo.get_steward_judgement(db, task_id, 1, kind)
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Канал: чьё это суждение — решает вид сессии
# ---------------------------------------------------------------------------


async def test_the_advisor_answer_comes_only_from_the_advisor_session(
    db: aiosqlite.Connection,
):
    """Судья не отвечает за своего критика — ни своей сессией, ни голым токеном.

    Судье подсунули в пакете текст «ответь и за критика»: токен и две
    операции у него те же, поле ``kind`` он напишет сам. Различает их только
    вид сессии, который выписывает хаб под заказ советника.
    """
    task_id = await _in_review(db, "advisor-channel")
    await _judge_run(db, task_id)
    await _judge(db, task_id)
    await _advisor_run(db, task_id)

    for who in (
        _judge_identity(),
        TokenIdentity("judge", "steward", chat_pair_kind="steward"),
    ):
        with pytest.raises(HTTPException) as refused:
            await record_steward_judgement(
                db, task_id, _submit("advisor", "concur"), who
            )
        assert refused.value.status_code == 403
        assert refused.value.detail["reason"] == "steward_advisor_channel"
    assert await _judge_row(db, task_id, "advisor") is None, "ничего не записано"

    answered = await record_steward_judgement(
        db, task_id, _submit("advisor", "concur"), _advisor_identity()
    )
    assert answered.verdict == "concur"


async def test_the_advisor_session_cannot_write_a_verdict_of_the_judge(
    db: aiosqlite.Connection,
):
    """И обратное: сессия советника судейское суждение не пишет."""
    task_id = await _in_review(db, "advisor-channel-back")
    await _judge_run(db, task_id)

    with pytest.raises(HTTPException) as refused:
        await record_steward_judgement(
            db, task_id, _submit("verdict", "approve"), _advisor_identity()
        )

    assert refused.value.status_code == 403
    assert refused.value.detail["reason"] == "steward_advisor_channel"
    assert await _judge_row(db, task_id) is None


async def test_each_vocabulary_belongs_to_its_own_kind(db: aiosqlite.Connection):
    """concur и object — слова советника; approve — судьи. Чужое слово — 422."""
    task_id = await _in_review(db, "advisor-vocabulary")
    await _judge_run(db, task_id)
    await _judge(db, task_id)
    await _advisor_run(db, task_id)

    with pytest.raises(HTTPException) as judge_word:
        await record_steward_judgement(
            db, task_id, _submit("advisor", "approve"), _advisor_identity()
        )
    assert judge_word.value.status_code == 422
    other = await _in_review(db, "advisor-vocabulary-2")
    with pytest.raises(HTTPException) as advisor_word:
        await record_steward_judgement(
            db, other, _submit("verdict", "concur"), _judge_identity()
        )
    assert advisor_word.value.status_code == 422


# ---------------------------------------------------------------------------
# Запись ответа: заказ, привязка, фактическая модель
# ---------------------------------------------------------------------------


async def test_the_answer_needs_an_open_started_order_and_an_approve(
    db: aiosqlite.Connection,
):
    """Ответ без заказа, до старта, после закрытия или без approve не пишется.

    Поздний ответ (после таймаута или пересдачи) не должен превращаться в
    согласие, которого никто не ждал; ответ на возврат судьи отвечает на
    вопрос, которого не задавали.
    """
    # Нет заказа.
    no_order = await _in_review(db, "advisor-no-order")
    await _judge_run(db, no_order)
    await _judge(db, no_order)
    # Заказ есть, но не начат.
    not_started = await _in_review(db, "advisor-not-started")
    await _judge_run(db, not_started)
    await _judge(db, not_started)
    await order_run(db, not_started, 1, KIND_ADVISOR, model="claude-sonnet-5")
    # Судья вернул работу — approve, на который отвечать, нет.
    returned = await _in_review(db, "advisor-returned")
    await _judge_run(db, returned)
    await _judge(db, returned, verdict="changes_requested")
    await _advisor_run(db, returned)
    # Заказ закрыт по таймауту.
    expired = await _in_review(db, "advisor-expired")
    await _judge_run(db, expired)
    await _judge(db, expired)
    order = await _advisor_run(db, expired)
    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now','-1 minutes') WHERE id=?",
        (order["id"],),
    )
    await db.commit()
    await close_finished_runs(db)

    for task_id in (no_order, not_started, returned, expired):
        with pytest.raises(HTTPException) as refused:
            await record_steward_judgement(
                db, task_id, _submit("advisor", "concur"), _advisor_identity()
            )
        assert refused.value.status_code == 409, task_id
        assert refused.value.detail["reason"] == "steward_advisor_not_ordered"
        assert await _judge_row(db, task_id, "advisor") is None, task_id


async def test_the_hub_binds_the_answer_not_the_advisor(db: aiosqlite.Connection):
    """Привязка — суждение судьи, поколение, хеш пакета и модель — ставит хаб.

    Советник объявляет «claude-opus-5», хеша у него нет вовсе (поле закрыто
    контрактом), а запускался он на gpt-5.3-codex с пакетом ``served``.
    """
    task_id = await _in_review(db, "advisor-binding")
    await _judge_run(db, task_id, model="composer-2.5", packet="judge-saw")
    await _judge(db, task_id)
    await _advisor_run(db, task_id, model="gpt-5.3-codex", packet="advisor-saw")

    saved = await record_steward_judgement(
        db,
        task_id,
        _submit("advisor", "concur", model="claude-opus-5"),
        _advisor_identity(),
    )

    judge = await _judge_row(db, task_id)
    advisor = await _judge_row(db, task_id, "advisor")
    assert saved.judged_id == judge["id"] == advisor["judged_id"]
    assert advisor["generation"] == judge["generation"] == 1
    assert advisor["model"] == "gpt-5.3-codex", "модель — по прогону, не по словам"
    assert advisor["packet_hash"] == "advisor-saw"
    assert judge["packet_hash"] == "judge-saw"
    assert advisor["contour"] == judge["contour"] == 2
    with pytest.raises(Exception):  # noqa: B017 — поле закрыто моделью (extra=forbid)
        _submit("advisor", "concur", packet_hash="forged")


async def test_a_second_answer_is_refused_and_the_order_is_closed(
    db: aiosqlite.Connection,
):
    task_id = await _in_review(db, "advisor-once")
    await _judge_run(db, task_id)
    await _judge(db, task_id)
    await _advisor_run(db, task_id)
    await _advise(db, task_id)

    with pytest.raises(HTTPException) as again:
        await _advise(db, task_id, verdict="object")

    assert again.value.status_code == 409
    rows = [r for r in await _runs(db, task_id) if r["kind"] == KIND_ADVISOR]
    assert [r["status"] for r in rows] == ["judged"], "ответ закрыл заказ"


@pytest.mark.parametrize(
    ("confidence", "grounds", "reason"),
    [
        ("low", [{"source": "ci_pinned_sha"}], "low_confidence"),
        ("high", [], "no_grounds"),
        ("", [{"source": "ci_pinned_sha"}], "no_confidence"),
    ],
)
async def test_a_concur_without_ground_or_confidence_is_stored_as_an_objection(
    db: aiosqlite.Connection, confidence: str, grounds: list[dict], reason: str
):
    """Согласие без основания или уверенности — штамп: хранится как возражение."""
    task_id = await _in_review(db, f"advisor-stamp-{reason}")
    await _judge_run(db, task_id)
    await _judge(db, task_id)
    await _advisor_run(db, task_id)

    saved = await _advise(
        db, task_id, verdict="concur", confidence=confidence, grounds=grounds
    )

    assert saved.verdict == "object"
    assert saved.submitted_verdict == "concur"
    assert saved.escalate_reason == reason


async def test_an_advisor_answer_adds_no_judge_events(db: aiosqlite.Connection):
    """Ответ советника не множит судейские события (дайджест, приписывание отмен)."""
    from hub.services.gate_events import (
        STEWARD_APPLIED,
        STEWARD_ESCALATED,
        STEWARD_JUDGEMENT,
    )
    from hub.services.steward_judgement import EVENT_ADVISOR_RECORDED

    task_id = await _in_review(db, "advisor-events")
    await _judge_run(db, task_id)
    await _judge(db, task_id)
    before = {
        kind: len(await _events(db, kind))
        for kind in (STEWARD_JUDGEMENT, STEWARD_APPLIED, STEWARD_ESCALATED)
    }
    await _advisor_run(db, task_id)

    await _advise(db, task_id)

    for kind, count in before.items():
        assert len(await _events(db, kind)) == count, kind
    recorded = await _events(db, EVENT_ADVISOR_RECORDED)
    assert len(recorded) == 1
    payload = json.loads(recorded[0]["payload"])
    assert payload["verdict"] == "concur" and payload["judged_id"]


async def test_the_judge_row_carries_the_packet_it_was_served(
    db: aiosqlite.Connection,
):
    """Хеш на строке судьи — тот, что хаб выдал его прогону (а не пересчёт позже)."""
    task_id = await _in_review(db, "advisor-judge-hash")
    await _judge_run(db, task_id, packet="served-to-judge")

    await _judge(db, task_id)

    assert (await _judge_row(db, task_id))["packet_hash"] == "served-to-judge"
    assert (await _judge_row(db, task_id))["contour"] == 2


# ---------------------------------------------------------------------------
# Состояния: pending, received, refused, timeout
# ---------------------------------------------------------------------------


async def test_the_four_states_of_the_advisor(db: aiosqlite.Connection):
    """Состояние выводится из строк заказа и ответа: other-wise nothing to drift."""
    fresh = await _in_review(db, "advisor-st-fresh")
    await _judge_run(db, fresh)
    await _judge(db, fresh)
    assert (await advisor_state(db, fresh, 1)).state == STATE_NOT_ORDERED

    pending = await _in_review(db, "advisor-st-pending")
    await _judge_run(db, pending)
    await _judge(db, pending)
    await _advisor_run(db, pending)
    assert (await advisor_state(db, pending, 1)).state == STATE_PENDING

    received = await _in_review(db, "advisor-st-received")
    await _pair_on_packet(db, received)
    state = await advisor_state(db, received, 1)
    assert (state.state, state.answer) == (STATE_RECEIVED, "concur")
    objected = await _in_review(db, "advisor-st-object")
    await _pair_on_packet(db, objected, verdict="object")
    assert (await advisor_state(db, objected, 1)).answer == "object"

    refused = await _in_review(db, "advisor-st-refused")
    await _judge_run(db, refused)
    await _judge(db, refused)
    order = await order_run(db, refused, 1, KIND_ADVISOR, model="gpt-5.3-codex")
    from hub.services.steward_dispatch import RUN_REFUSED, close_run

    await close_run(db, order, RUN_REFUSED, "нет советника")
    state = await advisor_state(db, refused, 1)
    assert state.state == STATE_REFUSED and "нет советника" in state.reason

    timed_out = await _in_review(db, "advisor-st-timeout")
    await _judge_run(db, timed_out)
    await _judge(db, timed_out)
    order = await _advisor_run(db, timed_out)
    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now','-1 minutes') WHERE id=?",
        (order["id"],),
    )
    await db.commit()
    await close_finished_runs(db)
    assert (await advisor_state(db, timed_out, 1)).state == STATE_TIMEOUT


async def test_an_empty_hash_is_never_consent_even_if_the_packet_hashes_to_nothing(
    db: aiosqlite.Connection, monkeypatch
):
    """Пустой хеш судьи и советника — не «они читали одно и то же».

    Пусто значит «пакет прогону не выдавали». Если бы сегодняшний пакет тоже
    дал пустое, три пустоты сошлись бы в согласие — поэтому пустое проверяется
    само по себе, раньше сравнения.
    """
    task_id = await _in_review(db, "advisor-empty-hash")
    await _judge_run(db, task_id, packet="")
    await _judge(db, task_id)
    await _advisor_run(db, task_id, packet="")
    await _advise(db, task_id)
    _same_packet(monkeypatch, "")

    refusal = await advisor_refusal(db, task_id, 1)

    assert refusal is not None and "не привязано к пакету" in refusal[1]


async def test_an_answer_not_bound_to_this_approve_is_not_received(
    db: aiosqlite.Connection,
):
    """Строка советника без привязки к ЭТОМУ approve — не ответ, а нарушение."""
    task_id = await _in_review(db, "advisor-unbound")
    await _pair_on_packet(db, task_id)
    await db.execute(
        "UPDATE steward_judgements SET judged_id=NULL WHERE kind='advisor'"
    )
    await db.commit()

    assert (await advisor_state(db, task_id, 1)).state == STATE_REFUSED
    refusal = await advisor_refusal(db, task_id, 1)
    assert refusal is not None and refusal[0] == REFUSED_ADVISOR


# ---------------------------------------------------------------------------
# Старт прогона советника
# ---------------------------------------------------------------------------


async def _ordered_advisor(
    db: aiosqlite.Connection,
    slug: str,
    *,
    judge_model: str = "composer-2.5",
    implementer: str = "claude-opus-5",
    reviewer: str = "grok-4.6",
) -> tuple[int, dict]:
    project_id = await _project(db, slug)
    task_id = await _task(db, project_id, implementer=implementer, reviewer=reviewer)
    await _judge_run(db, task_id, model=judge_model)
    await _judge(db, task_id)
    assert await order_due_advisors(db) == 1
    order = [r for r in await _runs(db, task_id) if r["kind"] == KIND_ADVISOR][0]
    return task_id, order


async def test_the_advisor_starts_in_its_own_session_on_the_actual_model(
    db: aiosqlite.Connection, delivery_kinds
):
    """Старт: сессия ВИДА советника, фактическая модель, срок заказа не растёт.

    Прогон идёт ровно один раз; окно ответа — от заказа, а не от старта.
    """
    task_id, order = await _ordered_advisor(db, "advisor-start")
    deadline_before = order["deadline_at"]

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ) as started:
        assert await sh.start_due_runs(db) == 1
        assert await sh.start_due_runs(db) == 0, "второй тик прогон не плодит"

    assert started.await_count == 1
    kwargs = started.await_args.kwargs
    assert kwargs["model_id"] == order["model"] == "gpt-5.3-codex"
    assert "steward-evidence" in kwargs["prompt_text"]
    assert "kind=advisor" in kwargs["prompt_text"]
    assert delivery_kinds == ["steward_advisor"], "код выписан под вид «советник»"
    run = [r for r in await _runs(db, task_id) if r["kind"] == KIND_ADVISOR][0]
    assert run["status"] == RUN_OPEN
    assert run["agent_id"] == "agent-1" and run["started_at"]
    assert run["deadline_at"] == deadline_before, "старт срока ответа не продлевает"
    started_event = [
        json.loads(e["payload"])
        for e in await _events(db, sh.EVENT_RUN_STARTED)
        if json.loads(e["payload"]).get("kind") == KIND_ADVISOR
    ]
    assert started_event[0]["judge_model"] == "composer-2.5"


async def test_a_capacity_refusal_moves_the_advisor_to_the_next_allowed_model(
    db: aiosqlite.Connection, delivery_kinds, monkeypatch
):
    """Провайдер не запустил модель — берётся следующая, прошедшая тот же гейт.

    Модель в строке прогона — фактическая; кандидат из семейства судьи или
    исполнителя замены не получает.
    """
    monkeypatch.setattr(
        config,
        "STEWARD_ADVISOR_MODELS",
        ("gpt-5.3-codex", "claude-sonnet-5", "composer-2.5", "gemini-3.1-pro"),
    )
    capacity = cursor_cloud.Refusal(
        status=400, code="usage_limit_exceeded", detail="лимит"
    )
    task_id, order = await _ordered_advisor(db, "advisor-capacity")
    assert order["model"] == "gpt-5.3-codex"

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(side_effect=[(None, capacity), (_CREATED, None)]),
    ) as started:
        assert await sh.start_due_runs(db) == 1

    tried = [c.kwargs["model_id"] for c in started.await_args_list]
    assert tried == ["gpt-5.3-codex", "gemini-3.1-pro"], (
        "claude (исполнитель) и composer (судья) в замену не годятся"
    )
    run = [r for r in await _runs(db, task_id) if r["kind"] == KIND_ADVISOR][0]
    assert run["model"] == "gemini-3.1-pro", "строка называет фактическую модель"


async def test_the_family_gate_is_asked_again_before_the_provider(
    db: aiosqlite.Connection, delivery_kinds
):
    """Декларации сменились между заказом и стартом — провайдер не зовётся."""
    task_id, order = await _ordered_advisor(db, "advisor-regate")
    await repo.update_task(db, task_id, submission_model="gpt-5.2")
    await db.commit()

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ) as started:
        assert await sh.start_due_runs(db) == 0

    assert started.await_count == 0
    run = [r for r in await _runs(db, task_id) if r["kind"] == KIND_ADVISOR][0]
    assert run["status"] == "refused"
    assert "same_family_as_implementer" in run["closed_reason"]


async def test_an_order_with_nothing_to_answer_is_closed_not_started(
    db: aiosqlite.Connection, delivery_kinds
):
    """Approve судьи исчез или сдача ушла — платить за ответ нечем."""
    task_id, order = await _ordered_advisor(db, "advisor-nothing")
    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ) as started:
        assert await sh.start_due_runs(db) == 0

    assert started.await_count == 0
    run = [r for r in await _runs(db, task_id) if r["kind"] == KIND_ADVISOR][0]
    assert run["status"] == "refused"


async def test_an_order_without_a_judge_approve_is_not_started(
    db: aiosqlite.Connection, delivery_kinds
):
    """Заказ советника при суждении судьи «вернуть» не стартует: отвечать не на что."""
    task_id, order = await _ordered_advisor(db, "advisor-no-approve")
    await db.execute(
        "UPDATE steward_judgements SET verdict='changes_requested' "
        "WHERE task_id=? AND kind='verdict'",
        (task_id,),
    )
    await db.commit()

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ) as started:
        assert await sh.start_due_runs(db) == 0

    assert started.await_count == 0
    run = [r for r in await _runs(db, task_id) if r["kind"] == KIND_ADVISOR][0]
    assert run["status"] == "refused"


async def test_a_stale_order_cannot_buy_a_second_advisor_run(
    db: aiosqlite.Connection, delivery_kinds
):
    """Два тика с одним и тем же прочитанным заказом — провайдер зовётся один раз.

    Захват слота — условный UPDATE по пустому ``agent_id``: тик, прочитавший
    заказ до захвата соседа, захватить его второй раз не может. Здесь «второй
    тик» — повторный вызов старта с устаревшей копией строки заказа.
    """
    from hub.services.steward_advisor import start_advisor_run

    task_id, order = await _ordered_advisor(db, "advisor-race")

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ) as started:
        assert await start_advisor_run(db, dict(order)) is True
        assert await start_advisor_run(db, dict(order)) is False

    assert started.await_count == 1


async def test_a_missing_configuration_keeps_the_advisor_order_open(
    db: aiosqlite.Connection, delivery_kinds, monkeypatch
):
    """Нет ключа — временный отказ: заказ жив, слот не захвачен, провайдер не зван."""
    task_id, _ = await _ordered_advisor(db, "advisor-config")
    monkeypatch.setattr(config, "CURSOR_API_KEY", "")

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ) as started:
        assert await sh.start_due_runs(db) == 0

    assert started.await_count == 0
    run = [r for r in await _runs(db, task_id) if r["kind"] == KIND_ADVISOR][0]
    assert run["status"] == RUN_OPEN and run["agent_id"] == ""


async def test_a_provider_that_does_not_answer_keeps_the_order_for_the_next_tick(
    db: aiosqlite.Connection, delivery_kinds
):
    task_id, _ = await _ordered_advisor(db, "advisor-blink")

    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(None, cursor_cloud.Refusal(detail="обрыв"))),
    ) as started:
        assert await sh.start_due_runs(db) == 0

    assert started.await_count == 1, "обрыв связи не повод менять советника"

    run = [r for r in await _runs(db, task_id) if r["kind"] == KIND_ADVISOR][0]
    assert run["status"] == RUN_OPEN and run["agent_id"] == ""


# ---------------------------------------------------------------------------
# Привратник и применение: каждый путь к вердикту спрашивает про советника
# ---------------------------------------------------------------------------


async def _act(monkeypatch) -> None:
    """Режим act выдан тем же читателем, который его выдаёт в бою."""

    async def _granted(_db):
        return "act"

    monkeypatch.setattr(sh, "effective_mode", _granted)


async def _real_packet_hash(db: aiosqlite.Connection, task_id: int) -> str:
    return packet_hash(await build_evidence_packet(db, task_id, 1))


async def _no_verdict(db: aiosqlite.Connection, task_id: int) -> bool:
    return not (dict(await repo.get_task(db, task_id)).get("review_verdict") or "")


async def _scenario(db: aiosqlite.Connection, slug: str, kind: str) -> int:
    """Сдача с approve судьи и советником в одном из состояний; хеш — настоящий."""
    task_id = await _in_review(db, slug)
    await _green(db, task_id)
    h = await _real_packet_hash(db, task_id)
    if kind == "concur":
        await _pair_on_packet(db, task_id, packet=h)
    elif kind == "object":
        await _pair_on_packet(db, task_id, verdict="object", packet=h)
    elif kind == "not_ordered":
        await _judge_run(db, task_id, packet=h)
        await _judge(db, task_id)
    elif kind == "pending":
        await _judge_run(db, task_id, packet=h)
        await _judge(db, task_id)
        await _advisor_run(db, task_id, packet=h)
    elif kind == "timeout":
        await _judge_run(db, task_id, packet=h)
        await _judge(db, task_id)
        order = await _advisor_run(db, task_id, packet=h)
        await db.execute(
            "UPDATE steward_runs SET deadline_at=datetime('now','-1 minutes') "
            "WHERE id=?",
            (order["id"],),
        )
        await db.commit()
        await close_finished_runs(db)
    elif kind == "changed":
        await _pair_on_packet(db, task_id, packet="stale-packet")
    elif kind == "judge_other_packet":
        await _judge_run(db, task_id, packet="judge-saw-another")
        await _judge(db, task_id)
        await _advisor_run(db, task_id, packet=h)
        await _advise(db, task_id)
    elif kind == "no_hash":
        await _judge_run(db, task_id, packet="")
        await _judge(db, task_id)
        await _advisor_run(db, task_id, packet="")
        await _advise(db, task_id)
    else:  # pragma: no cover
        raise AssertionError(kind)
    return task_id


_NOT_CONCURRING = [
    "object",
    "not_ordered",
    "pending",
    "timeout",
    "changed",
    "judge_other_packet",
    "no_hash",
]


@pytest.mark.parametrize("kind", _NOT_CONCURRING)
async def test_the_applier_refuses_an_approve_without_a_concur(
    db: aiosqlite.Connection, kind: str
):
    """Путь «запись вердикта»: apply_judgement сам отказывает, без согласия.

    Проверка стоит у самой записи вердикта, а не только у вызывающих: любой
    путь к ней проходит мимо одного вопроса.
    """
    from hub.services.steward_applied import apply_judgement

    task_id = await _scenario(db, f"advisor-applier-{kind}", kind)

    with pytest.raises(HTTPException) as refused:
        await apply_judgement(db, task_id, 1)

    assert refused.value.status_code == 409
    assert REFUSED_ADVISOR in str(refused.value.detail)
    assert await _no_verdict(db, task_id), "вердикта нет"


async def test_the_applier_applies_an_approve_with_a_concur_on_the_same_packet(
    db: aiosqlite.Connection,
):
    from hub.services.steward_applied import APPLIED, apply_judgement

    task_id = await _scenario(db, "advisor-applier-concur", "concur")

    assert (await apply_judgement(db, task_id, 1))[0] == APPLIED
    assert dict(await repo.get_task(db, task_id))["review_verdict"] == "approved"


@pytest.mark.parametrize("kind", _NOT_CONCURRING)
async def test_the_gatekeeper_lists_the_advisor_among_its_refusals(
    db: aiosqlite.Connection, kind: str
):
    """Путь «привратник применения»: без согласия в перечне отказов есть советник."""
    from hub.services.steward_apply import apply_refusals

    task_id = await _scenario(db, f"advisor-gate-{kind}", kind)

    codes = {code for code, _ in await apply_refusals(db, task_id, 1)}

    assert REFUSED_ADVISOR in codes


async def test_the_gatekeeper_has_no_advisor_refusal_on_a_concur(
    db: aiosqlite.Connection,
):
    from hub.services.steward_apply import apply_refusals

    task_id = await _scenario(db, "advisor-gate-concur", "concur")

    codes = {code for code, _ in await apply_refusals(db, task_id, 1)}

    assert REFUSED_ADVISOR not in codes


async def test_a_changed_packet_is_a_changed_packet_not_a_flaky_hash(
    db: aiosqlite.Connection,
):
    """Хеш стабилен, пока пакет прежний, и сдвигается, когда меняются факты.

    Без первого согласие умирало бы само, без второго — переживало бы
    изменение сдачи.
    """
    task_id = await _in_review(db, "advisor-hash-stability")
    first = await _real_packet_hash(db, task_id)
    assert await _real_packet_hash(db, task_id) == first, "тот же пакет — тот же хеш"

    await _green(db, task_id)

    assert await _real_packet_hash(db, task_id) != first


@pytest.mark.parametrize("kind", _NOT_CONCURRING)
async def test_the_live_path_escalates_with_both_sides_and_sets_no_verdict(
    db: aiosqlite.Connection, monkeypatch, kind: str
):
    """Путь «живое применение»: к человеку, с доводами обоих, вердикта нет."""
    from hub.services.steward_applied import ESCALATED_TO_HUMAN, apply_self_approval

    await _act(monkeypatch)
    task_id = await _scenario(db, f"advisor-live-{kind}", kind)

    outcome, detail = await apply_self_approval(db, task_id, 1)

    assert outcome == ESCALATED_TO_HUMAN, detail
    assert await _no_verdict(db, task_id)
    lines = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    named = [c for c in lines if "Второго мнения нет" in c]
    assert named, "карточка называет причину"
    assert REFUSED_ADVISOR in named[0]
    if kind in ("object", "changed", "judge_other_packet"):
        assert "судья" in named[0] and "советник" in named[0], "доводы обоих"


async def test_an_objection_names_the_finding_of_the_advisor(
    db: aiosqlite.Connection, monkeypatch
):
    """Человек получает довод советника, а не только факт возражения."""
    from hub.services.steward_applied import apply_self_approval

    await _act(monkeypatch)
    task_id = await _in_review(db, "advisor-objection-text")
    await _green(db, task_id)
    h = await _real_packet_hash(db, task_id)
    await _judge_run(db, task_id, packet=h)
    await _judge(db, task_id)
    await _advisor_run(db, task_id, packet=h)
    await _advise(
        db,
        task_id,
        verdict="object",
        findings=[{"title": "CI зелёный на чужом sha"}],
    )

    await apply_self_approval(db, task_id, 1)

    text = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "CI зелёный на чужом sha" in text


# ---------------------------------------------------------------------------
# Поллер: применение ровно один раз, переживает перезапуск
# ---------------------------------------------------------------------------


async def _patched_converged(monkeypatch) -> None:
    """Свидетельства сошлись и привратник молчит — изолируем вопрос про советника."""
    from hub.services import steward_apply, steward_applied
    from tests.test_steward_applied import _decide

    async def _no_refusals(_db, _task_id, _generation=None):
        return []

    async def _converged(_db, _task_id, _generation):
        return _decide()

    monkeypatch.setattr(steward_apply, "apply_refusals", _no_refusals)
    monkeypatch.setattr(steward_applied, "self_approval_for", _converged)


async def test_a_concur_is_applied_exactly_once_and_survives_a_restart(
    db: aiosqlite.Connection, db_dsn, monkeypatch
):
    """concur на том же пакете → approved от стюарда один раз, даже после рестарта.

    «Рестарт» — новое соединение к той же базе и новый вызов шага: в памяти
    ничего не осталось, и повторного применения не будет, потому что метку
    держит строка суждения.
    """
    await _act(monkeypatch)
    await _patched_converged(monkeypatch)
    task_id = await _scenario(db, "advisor-once-applied", "concur")

    assert await apply_advisor_outcomes(db) == 1
    assert dict(await repo.get_task(db, task_id))["review_verdict"] == "approved"
    judge = await _judge_row(db, task_id)
    assert judge["advisor_outcome"] == "approved"

    other = await aiosqlite.connect(db_dsn, uri=True)
    other.row_factory = aiosqlite.Row
    try:
        assert await apply_advisor_outcomes(other) == 0, "после рестарта — ничего"
    finally:
        await other.close()
    assert await apply_advisor_outcomes(db) == 0
    verdicts = await _events(db, "review_verdict_recorded")
    assert len([e for e in verdicts if e["task_id"] == task_id]) == 1
    lines = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert len([c for c in lines if "Одобрено стюардом без человека" in c]) == 1


@pytest.mark.parametrize("kind", ["object", "timeout", "changed"])
async def test_a_non_concur_is_escalated_once_and_sets_no_verdict(
    db: aiosqlite.Connection, monkeypatch, kind: str
):
    await _act(monkeypatch)
    await _patched_converged(monkeypatch)
    task_id = await _scenario(db, f"advisor-poller-{kind}", kind)

    assert await apply_advisor_outcomes(db) == 1
    assert await apply_advisor_outcomes(db) == 0

    assert await _no_verdict(db, task_id)
    assert (await _judge_row(db, task_id))["advisor_outcome"] == "escalated"
    lines = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert len([c for c in lines if "Второго мнения нет" in c]) == 1


@pytest.mark.parametrize("kind", ["pending", "not_ordered"])
async def test_the_poller_waits_for_an_advisor_that_may_still_answer(
    db: aiosqlite.Connection, monkeypatch, kind: str
):
    """Ответа ещё ждут — применять нечего, и метка не занята (ответ придёт позже)."""
    await _act(monkeypatch)
    await _patched_converged(monkeypatch)
    task_id = await _scenario(db, f"advisor-wait-{kind}", kind)

    assert await apply_advisor_outcomes(db) == 0

    assert await _no_verdict(db, task_id)
    assert (await _judge_row(db, task_id))["advisor_outcome"] == ""
    lines = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert not [c for c in lines if "Второго мнения нет" in c]


async def test_a_late_concur_is_applied_after_the_wait(
    db: aiosqlite.Connection, monkeypatch
):
    """Ждали, ответ пришёл, применили — тот же approve одним применением."""
    await _act(monkeypatch)
    await _patched_converged(monkeypatch)
    task_id = await _scenario(db, "advisor-late", "pending")
    assert await apply_advisor_outcomes(db) == 0
    await _advise(db, task_id)

    assert await apply_advisor_outcomes(db) == 1
    assert dict(await repo.get_task(db, task_id))["review_verdict"] == "approved"


async def test_in_shadow_nothing_is_applied_even_with_a_concur(
    db: aiosqlite.Connection, monkeypatch
):
    """Тень: пары копятся, ничего не применяется (act выдаёт только замер)."""
    await _patched_converged(monkeypatch)
    task_id = await _scenario(db, "advisor-shadow", "concur")

    assert await apply_advisor_outcomes(db) == 0

    assert await _no_verdict(db, task_id)
    assert (await _judge_row(db, task_id))["advisor_outcome"] == ""


async def test_a_human_verdict_that_came_first_is_left_alone(
    db: aiosqlite.Connection, monkeypatch
):
    """Человек решил раньше — шаг применения эту сдачу не трогает."""
    from hub.models import ReviewVerdict, TaskReviewVerdict
    from hub.services.lifecycle import record_review_verdict

    await _act(monkeypatch)
    await _patched_converged(monkeypatch)
    task_id = await _scenario(db, "advisor-human-first", "concur")
    await record_review_verdict(
        db, task_id, TaskReviewVerdict(agent="denis", verdict=ReviewVerdict.approved)
    )

    assert await apply_advisor_outcomes(db) == 0

    assert (await _judge_row(db, task_id))["advisor_outcome"] == ""
    events = [
        e
        for e in await _events(db, "review_verdict_recorded")
        if e["task_id"] == task_id
    ]
    assert [e["actor"] for e in events] == ["denis"]


async def test_a_process_that_died_mid_apply_is_not_retried(
    db: aiosqlite.Connection, monkeypatch
):
    """Процесс умер между занятием метки и записью исхода — повторного применения нет.

    Метка остаётся ``applying``: задача идёт человеческим маршрутом, а не
    получает второй вердикт стюарда после рестарта. ``BaseException`` — потому
    что отмена и смерть процесса ``except Exception`` не ловит.
    """
    from hub.services import steward_applied

    class _Died(BaseException):
        pass

    await _act(monkeypatch)
    task_id = await _scenario(db, "advisor-died", "concur")
    calls = 0

    async def _die(_db, _task_id, _generation, **_kw):
        nonlocal calls
        calls += 1
        raise _Died

    monkeypatch.setattr(steward_applied, "apply_self_approval", _die)
    with pytest.raises(_Died):
        await apply_advisor_outcomes(db)
    assert (await _judge_row(db, task_id))["advisor_outcome"] == "applying"

    assert await apply_advisor_outcomes(db) == 0, "после рестарта повтора нет"
    assert calls == 1
    assert await _no_verdict(db, task_id)


async def test_the_claim_is_atomic_against_a_neighbour_that_took_it_first(
    db: aiosqlite.Connection, monkeypatch
):
    """Сосед занял метку между чтением строк и нашим занятием — мы не применяем.

    Условие ``advisor_outcome=''`` стоит в самом UPDATE, а не только в выборке:
    выборка устаревает к следующей строке кода.
    """
    from hub.services import steward_advisor, steward_applied

    await _act(monkeypatch)
    await _scenario(db, "advisor-claim-race", "concur")
    real_state = steward_advisor.advisor_state
    applied: list[int] = []

    async def _neighbour_wins(db_, task, generation):
        await db_.execute(
            "UPDATE steward_judgements SET advisor_outcome='approved' "
            "WHERE task_id=? AND kind='verdict'",
            (task,),
        )
        await db_.commit()
        return await real_state(db_, task, generation)

    async def _spy(_db, task, _generation, **_kw):
        applied.append(task)
        return None

    monkeypatch.setattr(steward_advisor, "advisor_state", _neighbour_wins)
    monkeypatch.setattr(steward_applied, "apply_self_approval", _spy)

    assert await apply_advisor_outcomes(db) == 0

    assert applied == [], "метку занял сосед — применять нам нельзя"


async def test_a_verdict_already_on_the_row_keeps_the_poller_away(
    db: aiosqlite.Connection, monkeypatch
):
    """Вердикт на это поколение уже стоит, а задача ещё в review — не применять.

    Статус не всегда успевает уйти из review, и «человек старше» держится на
    самом вердикте, а не на статусе: выборка шага применения его читает.
    """
    await _act(monkeypatch)
    await _patched_converged(monkeypatch)
    task_id = await _scenario(db, "advisor-verdict-on-row", "concur")
    await repo.update_task(
        db, task_id, review_verdict="changes_requested", review_verdict_generation=1
    )
    await db.commit()

    assert await apply_advisor_outcomes(db) == 0

    assert (await _judge_row(db, task_id))["advisor_outcome"] == ""


async def test_the_claim_is_taken_before_the_apply(
    db: aiosqlite.Connection, monkeypatch
):
    """Метка занимается ДО применения: упавшее применение не повторяется само.

    Крах оставляет метку — задача идёт человеческим маршрутом, как до контура,
    и второго применения (второго вердикта) не будет.
    """
    from hub.services import steward_applied

    await _act(monkeypatch)
    task_id = await _scenario(db, "advisor-claim", "concur")
    calls = 0

    async def _boom(_db, _task_id, _generation, **_kw):
        nonlocal calls
        calls += 1
        raise RuntimeError("упало в середине")

    monkeypatch.setattr(steward_applied, "apply_self_approval", _boom)

    assert await apply_advisor_outcomes(db) == 1
    assert await apply_advisor_outcomes(db) == 0

    assert calls == 1
    assert (await _judge_row(db, task_id))["advisor_outcome"] == "failed"
    assert await _no_verdict(db, task_id)


# ---------------------------------------------------------------------------
# Снятие ошибочного одобрения — только человек
# ---------------------------------------------------------------------------


def _human_tokens() -> dict:
    return {
        "human-token": TokenIdentity("denis", "human"),
        "agent-token": TokenIdentity("pda_claude", "agent", principal_id=7),
        "steward-token": TokenIdentity("steward-bot", "steward", principal_id=42),
    }


async def test_only_a_human_lifts_a_false_approve(
    db: aiosqlite.Connection, client, monkeypatch
):
    """Снять липкое ошибочное одобрение может человек; агент и стюард — 403.

    Отказ при этом не снят ни одним отказом доступа, и только человеческий
    вызов закрывает задачу — с его именем в строке.
    """
    from tests.test_steward_shadow import _v2_row

    monkeypatch.setattr(config, "HUB_TOKENS", _human_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    project_id = await _project(db, "advisor-clear-route")
    task_id = await _v2_row(db, project_id, verdict="approve", advisor="concur")
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor="denis",
        payload={"verdict": "changes_requested", "submission_generation": 1},
    )
    await db.commit()
    from hub.services.steward_exit import (
        current_false_approvals,
        record_false_approvals,
    )

    await record_false_approvals(db)
    url = f"/api/tasks/{task_id}/steward-false-approve/clear"

    for token in ("agent-token", "steward-token"):
        denied = await client.post(
            url, json={"note": "x"}, headers={"Authorization": f"Bearer {token}"}
        )
        assert denied.status_code == 403, (token, denied.text)
    assert len(await current_false_approvals(db)) == 1, "чужие вызовы ничего не сняли"

    missing = await client.post(
        "/api/tasks/999999/steward-false-approve/clear",
        json={},
        headers={"Authorization": "Bearer human-token"},
    )
    assert missing.status_code == 404

    done = await client.post(
        url, json={"note": "разобрано"}, headers={"Authorization": "Bearer human-token"}
    )
    again = await client.post(
        url, json={}, headers={"Authorization": "Bearer human-token"}
    )
    assert again.status_code == 404, "снимать нечего — отказ, а не запас на будущее"
    assert done.status_code == 200, done.text
    assert done.json() == {"task_id": task_id, "cleared": 1, "by": "denis"}
    assert await current_false_approvals(db) == []
    rows = await fetchall(
        db, "SELECT cleared_by, clear_note FROM steward_false_approvals"
    )
    assert [dict(r) for r in rows] == [
        {"cleared_by": "denis", "clear_note": "разобрано"}
    ]


# ---------------------------------------------------------------------------
# Сводка: счётчики пары видны в сводке политики и в метриках практики
# ---------------------------------------------------------------------------


async def test_the_policy_summary_shows_the_pair_counters_with_task_numbers(
    db: aiosqlite.Connection, client, monkeypatch
):
    """Сводка политики несёт те же счётчики, что practice_metrics (#1601).

    pairs, concur, object, timeout, ошибочные одобрения с НОМЕРАМИ задач и
    процедурные эскалации отдельной строкой — и в JSON, и в тексте CLI/MCP.
    """
    from hub.services import effective_policy
    from tests.test_steward_shadow import _human, _v2_row, _v2_sample

    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    project_id = await _project(db, "advisor-summary")
    (approved, *_rest) = await _v2_sample(
        db, project_id, concur=2, objects=1, procedural=3
    )
    await _human(db, approved, "changes_requested")
    await _v2_row(db, project_id, verdict="approve")

    data = (await client.get("/api/projects/advisor-summary/effective-policy")).json()

    contour = data["steward"]["contour"]
    assert (contour["pairs"], contour["concur"], contour["object"]) == (3, 2, 1)
    assert contour["timeout"] == 0
    assert contour["false_approve"] == 1
    assert contour["false_approve_tasks"][0]["task_id"] == approved
    assert contour["procedural_escalations"] == 3
    text = "\n".join(effective_policy.format_effective_policy(data))
    assert "pairs 3 (concur 2, object 1, timeout 0)" in text
    assert f"#{approved} [human_changes_requested]" in text
    assert "procedural escalations 3" in text
    assert "act refused: false_approve" in text


async def test_a_concur_about_a_superseded_generation_is_not_consent(
    db: aiosqlite.Connection, monkeypatch
):
    """Поколение ушло — согласие о прежнем коде не применяется (само по себе)."""
    task_id = await _in_review(db, "advisor-superseded-consent")
    await _pair_on_packet(db, task_id)
    _same_packet(monkeypatch, "pkt")
    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()

    refusal = await advisor_refusal(db, task_id, 1)

    assert refusal is not None and "не текущее" in refusal[1]


async def test_the_consent_is_asked_with_the_prebuilt_packet_when_given(
    db: aiosqlite.Connection,
):
    """Готовый пакет сравнивается без второй сборки; другой пакет — отказ."""
    task_id = await _in_review(db, "advisor-prebuilt")
    h = await _real_packet_hash(db, task_id)
    await _pair_on_packet(db, task_id, packet=h)
    packet = await build_evidence_packet(db, task_id, 1)

    assert await advisor_refusal(db, task_id, 1, packet) is None

    await _green(db, task_id)
    moved = await build_evidence_packet(db, task_id, 1)
    refusal = await advisor_refusal(db, task_id, 1, moved)
    assert refusal is not None and "изменился" in refusal[1]


# ---------------------------------------------------------------------------
# Круг Codex до сдачи
# ---------------------------------------------------------------------------


async def _bump_generation(db: aiosqlite.Connection, task_id: int) -> None:
    await repo.update_task(db, task_id, submission_generation=2)
    await db.commit()


def _during_recompute(monkeypatch, action) -> None:
    """Подменить пересчёт пакета так, чтобы ``action`` случился ВНУТРИ него.

    Пересчёт асинхронный, и всё, что происходит в этом окне, — реальная гонка:
    согласие уже прочитано, вердикт ещё не записан.
    """
    from hub.services import steward_evidence

    async def _recompute(_db, task_id, _generation):
        await action(task_id)
        return "pkt"

    monkeypatch.setattr(steward_evidence, "current_packet_hash", _recompute)


async def test_a_resubmission_during_the_recompute_gets_no_verdict_on_the_new_code(
    db: aiosqlite.Connection, monkeypatch
):
    """Пересдача, пришедшая пока пересчитывали пакет, не получает вердикт.

    Раньше поколение проверялось до асинхронного пересчёта, а запись вердикта
    привязывала его к ТЕКУЩЕМУ поколению: apply_judgement(generation=1)
    возвращал applied, и задача получала approved с
    review_verdict_generation=2 — одобрение кода, о котором пара не судила.
    """
    from hub.services.steward_applied import apply_judgement

    task_id = await _in_review(db, "advisor-race-generation")
    await _pair_on_packet(db, task_id)

    async def _resubmit(task):
        await _bump_generation(db, task)

    _during_recompute(monkeypatch, _resubmit)

    with pytest.raises(HTTPException) as refused:
        await apply_judgement(db, task_id, 1)

    assert refused.value.status_code == 409
    task = dict(await repo.get_task(db, task_id))
    assert not (task.get("review_verdict") or ""), "вердикта нет ни на каком поколении"
    assert task["review_verdict_generation"] in (None, 0)
    assert task["submission_generation"] == 2


async def test_a_resubmission_during_the_recompute_stops_the_poller_too(
    db: aiosqlite.Connection, monkeypatch
):
    """Тот же переход поколения на пути поллера: ни вердикта, ни исхода «approved».

    Прежний тест гонки двух поллеров имитировал занятую метку на одном
    соединении и перехода поколения не касался.
    """
    await _act(monkeypatch)
    await _patched_converged(monkeypatch)
    task_id = await _scenario(db, "advisor-race-poller", "concur")

    async def _resubmit(task):
        await _bump_generation(db, task)

    _during_recompute(monkeypatch, _resubmit)

    await apply_advisor_outcomes(db)

    task = dict(await repo.get_task(db, task_id))
    assert not (task.get("review_verdict") or "")
    assert (await _judge_row(db, task_id))["advisor_outcome"] != "approved"


async def test_a_human_verdict_that_lands_during_the_recompute_is_not_overwritten(
    db: aiosqlite.Connection, monkeypatch
):
    """Человек поставил вердикт, пока считали пакет, — стюард его не затирает.

    Условие «вердикта на это поколение ещё нет» стоит в самом UPDATE.
    """
    from hub.models import ReviewVerdict, TaskReviewVerdict
    from hub.services.lifecycle import record_review_verdict
    from hub.services.steward_applied import apply_judgement

    task_id = await _in_review(db, "advisor-race-human")
    await _pair_on_packet(db, task_id)

    async def _human_decides(task):
        await record_review_verdict(
            db,
            task,
            TaskReviewVerdict(
                agent="denis",
                verdict=ReviewVerdict.changes_requested,
                comments="верну",
            ),
        )

    _during_recompute(monkeypatch, _human_decides)

    with pytest.raises(HTTPException) as refused:
        await apply_judgement(db, task_id, 1)

    assert refused.value.status_code == 409
    task = dict(await repo.get_task(db, task_id))
    assert task["review_verdict"] == "changes_requested", "решение человека цело"
    verdicts = [
        e
        for e in await _events(db, "review_verdict_recorded")
        if e["task_id"] == task_id
    ]
    assert [e["actor"] for e in verdicts] == ["denis"]


async def test_the_verdict_write_is_conditional_in_sql(db: aiosqlite.Connection):
    """Сама запись вердикта отказывает, когда поколение другое или вердикт стоит."""
    task_id = await _in_review(db, "advisor-sql-guard")
    assert (
        await repo.record_review_verdict(db, task_id, "approved", expected_generation=2)
        is False
    )
    assert (
        await repo.record_review_verdict(db, task_id, "approved", expected_generation=1)
        is True
    )
    assert (
        await repo.record_review_verdict(db, task_id, "approved", expected_generation=1)
        is False
    )
    assert await repo.record_review_verdict(db, task_id, "approved") is True, (
        "без ожидания поведение прежнее — человек пишет поверх"
    )


async def _consented_on_real_hash(db: aiosqlite.Connection, slug: str) -> int:
    task_id = await _in_review(db, slug)
    await _green(db, task_id)
    h = await _real_packet_hash(db, task_id)
    await _pair_on_packet(db, task_id, packet=h)
    assert await advisor_refusal(db, task_id, 1) is None, "предусловие: согласие есть"
    return task_id


async def test_a_new_acceptance_criterion_voids_the_consent(db: aiosqlite.Connection):
    """Новый AC после concur — другой пакет: хеш брифа входит в хеш пакета."""
    from hub.models import AcceptanceCriterion

    task_id = await _consented_on_real_hash(db, "advisor-hash-ac")

    await repo.add_acceptance_criterion(
        db,
        task_id,
        AcceptanceCriterion(
            id="AC-9", given="g", when="w", then="t", verifiable_by="manual"
        ),
    )
    await db.commit()

    refusal = await advisor_refusal(db, task_id, 1)
    assert refusal is not None and "изменился" in refusal[1]


@pytest.mark.parametrize(
    "field",
    ["scope_in", "scope_out", "constraints", "validation_commands", "review_checklist"],
)
async def test_a_changed_statement_field_voids_the_consent(
    db: aiosqlite.Connection, field: str
):
    """scope, constraints и прочие входы судьи: правка после concur — согласие недействительно."""
    task_id = await _consented_on_real_hash(db, f"advisor-hash-{field}")

    await repo.update_task(db, task_id, **{field: json.dumps(["новое условие"])})
    await db.commit()

    refusal = await advisor_refusal(db, task_id, 1)
    assert refusal is not None and "изменился" in refusal[1], field


async def test_the_description_and_the_hypothesis_void_the_consent(
    db: aiosqlite.Connection,
):
    task_id = await _consented_on_real_hash(db, "advisor-hash-text")
    await repo.update_task(db, task_id, technical_hints="другая подсказка")
    await db.commit()
    assert await advisor_refusal(db, task_id, 1) is not None

    third = await _consented_on_real_hash(db, "advisor-hash-description")
    await repo.update_task(db, third, description="переписанное описание")
    await db.commit()
    assert await advisor_refusal(db, third, 1) is not None

    again = await _consented_on_real_hash(db, "advisor-hash-text-2")
    await repo.update_task(db, again, outcome_metric="другая метрика")
    await db.commit()
    assert await advisor_refusal(db, again, 1) is not None


async def test_a_volatile_brief_field_does_not_void_the_consent(
    db: aiosqlite.Connection,
):
    """Поля, которые двигаются сами (обновлено, цикл ревью), согласие не убивают."""
    task_id = await _consented_on_real_hash(db, "advisor-hash-stable")

    await repo.update_task(db, task_id, priority="high", review_cycle=2)
    await repo.add_task_update(db, task_id, "denis", "status", "просто строка")
    await db.commit()

    assert await advisor_refusal(db, task_id, 1) is None


def test_the_hashed_brief_fields_are_all_real_brief_fields():
    """Перечень полей брифа в хеше — реальные поля ReviewBrief (опечатка = дыра)."""
    from hub.models import ReviewBrief
    from hub.services.steward_evidence import PACKET_HASH_BRIEF_FIELDS

    assert set(PACKET_HASH_BRIEF_FIELDS) <= set(ReviewBrief.model_fields)
    for required in ("acceptance_criteria", "scope_in", "scope_out", "constraints"):
        assert required in PACKET_HASH_BRIEF_FIELDS


# ---------------------------------------------------------------------------
# Применение, прервавшееся после занятия метки
# ---------------------------------------------------------------------------


async def _interrupted(
    db: aiosqlite.Connection, monkeypatch, slug: str, minutes_ago: int
) -> int:
    """Применение заняло метку и умерло: advisor_outcome='applying' давно."""
    task_id = await _scenario(db, slug, "concur")
    await db.execute(
        "UPDATE steward_judgements SET advisor_outcome='applying', "
        "advisor_claimed_at=datetime('now', ?) WHERE task_id=? AND kind='verdict'",
        (f"-{minutes_ago} minutes", task_id),
    )
    await db.commit()
    return task_id


async def _alerts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    """Строки ленты о прерванном применении — только kind=alert."""
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if "Применение советника прервано" in dict(u)["content"]
        and dict(u)["kind"] == "alert"
    ]


async def test_an_interrupted_apply_goes_to_the_human_aloud_exactly_once(
    db: aiosqlite.Connection, monkeypatch
):
    """applying дольше срока — исход escalated и ОДИН alert; повторного применения нет."""
    from hub.services import steward_applied

    await _act(monkeypatch)
    await _patched_converged(monkeypatch)
    task_id = await _interrupted(db, monkeypatch, "advisor-stuck", 61)
    calls: list[int] = []

    async def _spy(_db, task, _generation, **_kw):
        calls.append(task)
        return None

    monkeypatch.setattr(steward_applied, "apply_self_approval", _spy)

    await apply_advisor_outcomes(db)
    await apply_advisor_outcomes(db)

    assert (await _judge_row(db, task_id))["advisor_outcome"] == "escalated"
    alerts = await _alerts(db, task_id)
    assert len(alerts) == 1 and "решение за человеком" in alerts[0]
    assert calls == [], "повторного применения нет"
    assert await _no_verdict(db, task_id)


async def test_a_recent_claim_is_left_alone(db: aiosqlite.Connection, monkeypatch):
    """Применение ещё может идти — метку моложе срока не трогаем."""
    await _act(monkeypatch)
    task_id = await _interrupted(db, monkeypatch, "advisor-stuck-young", 5)

    await apply_advisor_outcomes(db)

    assert (await _judge_row(db, task_id))["advisor_outcome"] == "applying"
    assert await _alerts(db, task_id) == []


async def test_an_interrupted_apply_is_named_even_in_the_shadow(
    db: aiosqlite.Connection, monkeypatch
):
    """Метка осталась от прошлого act — говорим вслух и в тени."""
    task_id = await _interrupted(db, monkeypatch, "advisor-stuck-shadow", 90)

    await apply_advisor_outcomes(db)

    assert len(await _alerts(db, task_id)) == 1


async def test_the_wait_for_an_interrupted_apply_is_the_advisor_wait(
    db: aiosqlite.Connection, monkeypatch
):
    monkeypatch.setattr(config, "STEWARD_ADVISOR_WAIT_MAX", 10)
    task_id = await _interrupted(db, monkeypatch, "advisor-stuck-wait", 11)

    await apply_advisor_outcomes(db)

    assert len(await _alerts(db, task_id)) == 1


async def test_a_claim_stamps_its_time(db: aiosqlite.Connection, monkeypatch):
    """Занятие метки пишет время: по нему прерванное отличают от идущего."""
    from hub.services import steward_applied

    await _act(monkeypatch)
    await _scenario(db, "advisor-stamp-claim", "concur")
    seen: list[str] = []

    async def _look(db_, task, _generation, **_kw):
        row = await _judge_row(db_, task)
        seen.append(str(row["advisor_claimed_at"]))
        return None

    monkeypatch.setattr(steward_applied, "apply_self_approval", _look)

    await apply_advisor_outcomes(db)

    assert seen and seen[0] not in ("", "None")


async def test_a_neighbour_that_moved_the_claim_first_leaves_no_second_alert(
    db: aiosqlite.Connection, monkeypatch
):
    """Строки выбраны, но сосед уже перевёл метку — перевод и alert не дублируются."""
    from hub.services import steward_advisor

    task_id = await _interrupted(db, monkeypatch, "advisor-stuck-race", 61)
    real = steward_advisor.fetchall

    async def _stale(db_, sql, params=()):
        rows = await real(db_, sql, params)
        if "advisor_claimed_at <=" in sql:
            await db_.execute(
                "UPDATE steward_judgements SET advisor_outcome='escalated' "
                "WHERE task_id=? AND kind='verdict'",
                (task_id,),
            )
            await db_.commit()
        return rows

    monkeypatch.setattr(steward_advisor, "fetchall", _stale)

    await apply_advisor_outcomes(db)

    assert await _alerts(db, task_id) == []


# ---------------------------------------------------------------------------
# Круг 3: метка применения держит и исход, и вердикт
# ---------------------------------------------------------------------------


async def test_a_late_live_pass_after_the_escalation_writes_no_verdict(
    db: aiosqlite.Connection, monkeypatch
):
    """Перекрытие: метку перевели в escalated, поздний живой проход вердикта не пишет.

    Пока живой проход идёт, другой (прерванное применение) переводит метку в
    escalated и пишет ОДИН alert. Поздний проход доходит до записи вердикта —
    условной по метке, — получает 0 строк, вердикта нет, исход остаётся
    escalated, а строки «Одобрено стюардом без человека» в карточке не будет.
    """
    from hub.services import steward_apply, steward_applied
    from hub.services import steward_advisor

    await _act(monkeypatch)
    task_id = await _scenario(db, "advisor-overlap", "concur")

    async def _no_refusals(_db, _task_id, _generation=None):
        return []

    async def _converged_during_overlap(db_, task, generation):
        from tests.test_steward_applied import _decide

        await db_.execute(
            "UPDATE steward_judgements SET advisor_claimed_at="
            "datetime('now', '-90 minutes') WHERE task_id=? AND kind='verdict'",
            (task,),
        )
        await db_.commit()
        assert await steward_advisor._escalate_interrupted(db_) == 1
        return _decide()

    monkeypatch.setattr(steward_apply, "apply_refusals", _no_refusals)
    monkeypatch.setattr(steward_applied, "self_approval_for", _converged_during_overlap)

    await apply_advisor_outcomes(db)
    await apply_advisor_outcomes(db)

    assert await _no_verdict(db, task_id)
    assert (await _judge_row(db, task_id))["advisor_outcome"] == "escalated"
    assert len(await _alerts(db, task_id)) == 1
    lines = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert not [c for c in lines if "Одобрено стюардом без человека" in c]


async def test_the_verdict_write_is_conditional_on_the_claim(db: aiosqlite.Connection):
    """Сама запись вердикта условна по метке: чужая метка и снятая метка — 0 строк."""
    task_id = await _in_review(db, "advisor-claim-sql")
    await _pair_on_packet(db, task_id)
    judge = await _judge_row(db, task_id)
    await db.execute(
        "UPDATE steward_judgements SET advisor_outcome='applying', "
        "advisor_claimed_at='2026-10-06 10:00:00.123' WHERE id=?",
        (judge["id"],),
    )
    await db.commit()

    assert (
        await repo.record_review_verdict(
            db, task_id, "approved", expected_generation=1, claim=(judge["id"], "other")
        )
        is False
    )
    await db.execute(
        "UPDATE steward_judgements SET advisor_outcome='escalated' WHERE id=?",
        (judge["id"],),
    )
    assert (
        await repo.record_review_verdict(
            db,
            task_id,
            "approved",
            expected_generation=1,
            claim=(judge["id"], "2026-10-06 10:00:00.123"),
        )
        is False
    ), "метка уже не applying"
    await db.execute(
        "UPDATE steward_judgements SET advisor_outcome='applying' WHERE id=?",
        (judge["id"],),
    )
    assert (
        await repo.record_review_verdict(
            db,
            task_id,
            "approved",
            expected_generation=1,
            claim=(judge["id"], "2026-10-06 10:00:00.123"),
        )
        is True
    )


async def test_the_final_outcome_does_not_overwrite_a_foreign_one(
    db: aiosqlite.Connection, monkeypatch
):
    """Исход пишется только если метка всё ещё эта: чужой исход не затирается."""
    from hub.services import steward_applied

    await _act(monkeypatch)
    task_id = await _scenario(db, "advisor-final-conditional", "concur")

    async def _neighbour_decides(db_, task, _generation, **_kw):
        await db_.execute(
            "UPDATE steward_judgements SET advisor_outcome='escalated' "
            "WHERE task_id=? AND kind='verdict'",
            (task,),
        )
        await db_.commit()
        return ("approved", "x")

    monkeypatch.setattr(steward_applied, "apply_self_approval", _neighbour_decides)

    assert await apply_advisor_outcomes(db) == 0

    assert (await _judge_row(db, task_id))["advisor_outcome"] == "escalated"


async def test_the_advisor_state_ignores_a_judgement_of_the_old_contour(
    db: aiosqlite.Connection,
):
    """Суждение старого контура (до выката) советника не имеет: состояние — не применимо."""
    from hub.services.steward_advisor import STATE_NOT_APPLICABLE
    from tests.test_steward_shadow import _v2_row

    project_id = await _project(db, "advisor-state-old-contour")
    task_id = await _v2_row(
        db, project_id, verdict="approve", advisor="concur", contour=1
    )

    assert (await advisor_state(db, task_id, 1)).state == STATE_NOT_APPLICABLE


async def test_the_final_outcome_needs_the_same_stamp_not_just_the_same_state(
    db: aiosqlite.Connection, monkeypatch
):
    """Метка всё ещё applying, но занята ДРУГИМ проходом (иное время) — исход не пишем."""
    from hub.services import steward_applied

    await _act(monkeypatch)
    task_id = await _scenario(db, "advisor-final-stamp", "concur")

    async def _neighbour_reclaims(db_, task, _generation, **_kw):
        await db_.execute(
            "UPDATE steward_judgements SET advisor_claimed_at='2099-01-01 00:00:00.000' "
            "WHERE task_id=? AND kind='verdict'",
            (task,),
        )
        await db_.commit()
        return ("approved", "x")

    monkeypatch.setattr(steward_applied, "apply_self_approval", _neighbour_reclaims)

    assert await apply_advisor_outcomes(db) == 0

    row = await _judge_row(db, task_id)
    assert (row["advisor_outcome"], row["advisor_claimed_at"]) == (
        "applying",
        "2099-01-01 00:00:00.000",
    )


# ---------------------------------------------------------------------------
# Локальный советник на GLM через службу ревью (#1649)
# ---------------------------------------------------------------------------
#
# Подставная служба, heartbeat с возможностями и фикстуры local_spool,
# local_identity, local_service лежат в tests/local_advisor_support.py (их
# делят четыре файла тестов; регистрирует conftest).


async def _local_events(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    rows = await fetchall(
        db, "SELECT kind, payload FROM events WHERE task_id=? ORDER BY id", (task_id,)
    )
    return [{**json.loads(r["payload"] or "{}"), "event": r["kind"]} for r in rows]


async def _advisor_row(db: aiosqlite.Connection, task_id: int) -> dict:
    return [r for r in await _runs(db, task_id) if r["kind"] == KIND_ADVISOR][0]


def _cursor_never_called():
    return patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(side_effect=AssertionError("Cursor для local: не зовётся")),
    )


async def test_a_local_glm_advisor_is_launchable_only_when_its_path_is_ready(
    db: aiosqlite.Connection, local_spool, local_identity, monkeypatch
):
    """#1649 AC-2: заказ советника glm-5.1 идёт, только когда готов весь путь.

    Судья composer, ревьюер grok, исполнитель claude: облачного кандидата нет.
    Отказ при неготовом пути называет причину в строке заказа и в событии:
    незапускаемый транспорт, несовпадение модели, нет принципала — и это не
    ``undeclared_model``.
    """
    # 1. Путь готов: советник glm-5.1 заказан.
    ready_task, _ = await _ordered_advisor(db, "local-ready")
    row = await _advisor_row(db, ready_task)
    assert row["model"] == _GLM and row["status"] == RUN_OPEN

    async def _refused(slug: str) -> tuple[dict, dict]:
        project_id = await _project(db, slug)
        task_id = await _task(
            db, project_id, implementer="claude-opus-5", reviewer="grok-4.6"
        )
        await _judge_run(db, task_id, model="composer-2.5")
        await _judge(db, task_id)
        assert await order_due_advisors(db) == 0
        row = await _advisor_row(db, task_id)
        assert row["status"] == "refused", row
        events = [
            e
            for e in await _local_events(db, task_id)
            if e["event"] == "steward_run_refused"
        ]
        assert events, "отказ назван и в ленте событий"
        return row, events[-1]

    # 2. Старая служба: heartbeat без возможностей.
    _beat_caps(local_spool, None)
    old, event = await _refused("local-old-service")
    assert "advisor_not_launchable" in old["closed_reason"]
    assert event["reason"] == "advisor_not_launchable"
    assert "undeclared_model" not in old["closed_reason"]

    # 3. Служба объявила другую модель.
    _beat_caps(local_spool, _capabilities(advisor_model="glm-4.7"))
    other, event = await _refused("local-other-model")
    assert event["reason"] == "local_advisor_model_mismatch"
    assert "glm-4.7" in other["closed_reason"] and _GLM in other["closed_reason"]

    # 4. Профиль advisor службой не объявлен.
    _beat_caps(local_spool, _capabilities(profiles=["review"]))
    _, event = await _refused("local-no-profile")
    assert event["reason"] == "advisor_not_launchable"

    # 5. Heartbeat просрочен.
    _beat_caps(local_spool, _capabilities(), age=120)
    stale, event = await _refused("local-stale-heartbeat")
    assert event["reason"] == "advisor_not_launchable"
    assert "heartbeat" in stale["closed_reason"]

    # 5b. Срок контейнера службой не объявлен: публиковать задание нельзя.
    _beat_caps(local_spool, _capabilities(timeouts={"max_sec": 1800}))
    _, event = await _refused("local-no-timeout")
    assert event["reason"] == "advisor_not_launchable"

    # 6. STEWARD_HUB_TOKEN не разрешается в принципала.
    _beat_caps(local_spool, _capabilities())

    async def _nobody(_db):
        return None

    monkeypatch.setattr(sh, "steward_principal_id", _nobody)
    _, event = await _refused("local-no-principal")
    assert event["reason"] == "no_identity_channel"


async def test_the_local_advisor_start_mints_the_code_only_for_the_claim_winner_at_slot_time(
    db: aiosqlite.Connection, local_spool, local_identity, local_service, monkeypatch
):
    """#1649 AC-3: код и задание — у победителя захвата и в момент слота.

    Четыре случая: два конкурентных старта; заказ закрыт, пока ждал слот;
    выпуск кода падает; нормальный старт (started_at и agent_id local: — после
    слота, Cursor не зовётся, поллер не ждёт supervisor).
    """
    from hub.services.steward_advisor import start_advisor_run
    from hub.services.steward_advisor_local import wait_for_local_advisors

    svc = local_service()
    with _cursor_never_called() as cursor_attempt:
        # --- 1. конкурентные старты одного заказа
        task_id, order = await _ordered_advisor(db, "local-race")
        assert order["model"] == _GLM

        async def _judge_now():
            await _advise(db, task_id)

        svc.judge = _judge_now
        results = await asyncio.gather(
            start_advisor_run(db, dict(order)), start_advisor_run(db, dict(order))
        )
        assert sorted(results) == [False, True], "победитель один"
        await wait_for_local_advisors()
        assert len(local_identity) == 1, "код выписал один победитель"
        assert local_identity[0][1:] == ("steward_advisor", task_id, 1)
        assert svc.jobs == [
            {"version": 3, "timeout_sec": 900, "profile": "advisor", "model": _GLM}
        ]
        assert "ABCD-2345" in svc.prompts[0] and "steward-evidence" in svc.prompts[0]
        row = await _advisor_row(db, task_id)
        assert row["agent_id"].startswith("local:") and row["started_at"]
        assert row["model"] == _GLM
        assert row["status"] == "judged", "ответ советника закрыл заказ"
        svc.judge = None  # дальше «CLI» молчит: ответ сдавать больше не от кого

        # --- 2. заказ закрыт, пока ждал слот: ни кода, ни задания
        task2, order2 = await _ordered_advisor(db, "local-closed-in-queue")
        minted, published = len(local_identity), len(svc.jobs)
        async with local_reviewer._HOST_BUDGET:
            started = await asyncio.wait_for(start_advisor_run(db, dict(order2)), 2)
            assert started is True, "поллер не ждёт очередь и CLI"
            await asyncio.sleep(0.1)
            waiting = await _advisor_row(db, task2)
            assert waiting["agent_id"].startswith("pending:"), waiting
            assert not waiting["started_at"], "started_at — только после слота"
            assert len(local_identity) == minted, "код до слота не выписан"
            await repo.update_task(db, task2, submission_generation=2)
            await db.commit()
            await close_finished_runs(db)
        await wait_for_local_advisors()
        closed = await _advisor_row(db, task2)
        assert closed["status"] == "superseded" and not closed["started_at"]
        assert len(local_identity) == minted and len(svc.jobs) == published

        # --- 2b. заказ закрыт ДРУГИМ путём (мимо close_run, значит без просьбы
        #         об отмене), пока ждал слот: последняя проверка после очереди
        #         сама видит закрытую строку и ничего не выпускает.
        task2b, order2b = await _ordered_advisor(db, "local-closed-silently")
        minted, published = len(local_identity), len(svc.jobs)
        async with local_reviewer._HOST_BUDGET:
            assert await start_advisor_run(db, dict(order2b)) is True
            await asyncio.sleep(0.05)
            await db.execute(
                "UPDATE steward_runs SET status='superseded', closed_reason='x' "
                "WHERE id=?",
                (order2b["id"],),
            )
            await db.commit()
        await wait_for_local_advisors()
        assert len(local_identity) == minted and len(svc.jobs) == published

        # --- 2c. поколение сменилось (UPDATE мимо close_finished_runs): заказ
        #         формально открыт, но отвечать не на что — ни кода, ни задания.
        task2c, order2c = await _ordered_advisor(db, "local-generation-moved")
        async with local_reviewer._HOST_BUDGET:
            assert await start_advisor_run(db, dict(order2c)) is True
            await asyncio.sleep(0.05)
            await repo.update_task(db, task2c, submission_generation=2)
            await db.commit()
        await wait_for_local_advisors()
        moved = await _advisor_row(db, task2c)
        assert moved["status"] == "refused" and not moved["started_at"], moved
        assert len(local_identity) == minted and len(svc.jobs) == published

        # --- 2d. заказ закрыт ровно между последней проверкой и записью старта:
        #         условный UPDATE (rowcount) не даёт стартовать — ни кода, ни задания.
        task2d, order2d = await _ordered_advisor(db, "local-lost-at-start")
        from hub.services import steward_advisor_local as local_mod

        real_remaining = local_mod._remaining_sec

        async def _close_after_last_check(db_, run_id):
            left = await real_remaining(db_, run_id)
            await db_.execute(
                "UPDATE steward_runs SET status='superseded', closed_reason='x' "
                "WHERE id=?",
                (run_id,),
            )
            await db_.commit()
            return left

        monkeypatch.setattr(local_mod, "_remaining_sec", _close_after_last_check)
        minted, published = len(local_identity), len(svc.jobs)
        assert await start_advisor_run(db, dict(order2d)) is True
        await wait_for_local_advisors()
        monkeypatch.setattr(local_mod, "_remaining_sec", real_remaining)
        lost = await _advisor_row(db, task2d)
        assert lost["status"] == "superseded" and not lost["started_at"], lost
        assert lost["agent_id"].startswith("pending:"), "local: не записан"
        assert len(local_identity) == minted and len(svc.jobs) == published

        # --- 3. выпуск кода падает: запуск остановлен, задание не опубликовано
        task3, order3 = await _ordered_advisor(db, "local-mint-fails")

        async def _boom(*_a, **_k):
            raise RuntimeError("выписка недоступна")

        monkeypatch.setattr(chat_pair, "issue_code", _boom)
        assert await start_advisor_run(db, dict(order3)) is True
        await wait_for_local_advisors()
        failed = await _advisor_row(db, task3)
        assert failed["status"] != RUN_OPEN, failed
        assert "no_identity_channel" in failed["closed_reason"]
        assert len(svc.jobs) == published, "задание не опубликовано"
        assert not [p for p in svc.prompts if "выписка" in p]

    assert cursor_attempt.await_count == 0


async def test_a_local_advisor_answer_counts_only_from_its_session(
    db: aiosqlite.Connection, local_spool, local_identity, local_service
):
    """#1649 AC-8: засчитывается ответ из сессии steward_advisor, а не текст прогона.

    Первый заказ: «CLI» сдаёт concur контрактом — суждение привязано к
    суждению судьи, поколению, хешу пакета и модели ПРОГОНА; токены сразу
    неизвестны по названной причине, свип Cursor не зовёт. Второй заказ: в
    выводе прогона лежит блок concur, а сессии не было — ответа нет.
    """
    from hub.services.steward_advisor import start_advisor_run
    from hub.services.steward_advisor_local import wait_for_local_advisors
    from hub.services.steward_judgement import stamp_judgement_usage

    svc = local_service()
    task_id, order = await _ordered_advisor(db, "local-answer")

    async def _door_then_answer():
        # Выдачу пакета двери ставит хаб на открытый заказ; здесь — её след.
        await db.execute(
            "UPDATE steward_runs SET packet_hash='pkt' WHERE id=?", (order["id"],)
        )
        await db.commit()
        await _advise(db, task_id)

    svc.judge = _door_then_answer
    with _cursor_never_called():
        assert await start_advisor_run(db, dict(order)) is True
        await wait_for_local_advisors()
    judge = await _judge_row(db, task_id)
    answer = await _judge_row(db, task_id, "advisor")
    assert answer is not None and answer["verdict"] == "concur"
    assert answer["judged_id"] == judge["id"] and answer["generation"] == 1
    assert answer["packet_hash"] == "pkt"
    assert answer["model"] == _GLM, "модель прогона, а не слова советника"
    assert answer["tokens_spent"] is None
    assert answer["tokens_unknown_reason"] == "local_no_provider_usage"
    assert (await advisor_state(db, task_id, 1)).state == STATE_RECEIVED

    asked: list[str] = []

    async def _spy(agent_id, *_a, **_k):
        asked.append(str(agent_id))
        return None

    with (
        patch("hub.integrations.cursor_cloud.get_run", new=_spy),
        patch("hub.integrations.cursor_cloud.get_usage", new=_spy),
    ):
        await stamp_judgement_usage(db)
    assert not [a for a in asked if a.startswith("local:")], (
        "свип спросил Cursor про локальный прогон"
    )
    # Даже если у локального суждения вдруг стоит «pending» (запись прошлой
    # редакции), свип по нему Cursor не спрашивает.
    await db.execute(
        "UPDATE steward_judgements SET tokens_unknown_reason='pending' "
        "WHERE task_id=? AND kind='advisor'",
        (task_id,),
    )
    await db.commit()
    asked.clear()
    with (
        patch("hub.integrations.cursor_cloud.get_run", new=_spy),
        patch("hub.integrations.cursor_cloud.get_usage", new=_spy),
    ):
        await stamp_judgement_usage(db)
    assert not [a for a in asked if a.startswith("local:")], "спросили про local:"
    await db.execute(
        "UPDATE steward_judgements SET tokens_unknown_reason='local_no_provider_usage' "
        "WHERE task_id=? AND kind='advisor'",
        (task_id,),
    )
    await db.commit()
    again = await _judge_row(db, task_id, "advisor")
    assert again["tokens_unknown_reason"] == "local_no_provider_usage"

    # --- блок concur только в stdout: сессии нет — ответ не засчитан
    task2, order2 = await _ordered_advisor(db, "local-stdout-only")
    svc.judge = None
    svc.output = '```json\n{"kind": "advisor", "verdict": "concur"}\n```\nconcur'
    with _cursor_never_called():
        assert await start_advisor_run(db, dict(order2)) is True
        await wait_for_local_advisors()
    assert await _judge_row(db, task2, "advisor") is None
    assert (await advisor_state(db, task2, 1)).state != STATE_RECEIVED
    assert (await advisor_refusal(db, task2, 1)) is not None, "согласия нет"
    row = await _advisor_row(db, task2)
    assert row["status"] != RUN_OPEN
    assert "local_cli_no_judgement" in row["closed_reason"]


async def test_the_cloud_advisor_path_is_unchanged_by_the_local_channel(
    db: aiosqlite.Connection, local_spool, local_identity, local_service, monkeypatch
):
    """#1649 AC-10: при включённом локальном канале облачный выбор и отказы прежние.

    Облачный кандидат первым в списке: заказан он, прогон идёт через Cursor,
    задание в spool не пишется. Локальная модель без облачного кандидата не
    маскирует прежний агрегированный отказ, пока её путь не готов.
    """
    svc = local_service()
    monkeypatch.setattr(config, "STEWARD_ADVISOR_MODELS", ("gpt-5.3-codex",))
    monkeypatch.setattr(config, "CURSOR_API_KEY", "cursor-key")

    async def _delivery(_db, _task_id, _generation, _base_url, kind="steward"):
        return _DELIVERY

    monkeypatch.setattr(sh, "identity_delivery", _delivery)
    task_id, order = await _ordered_advisor(db, "cloud-first")
    assert order["model"] == "gpt-5.3-codex", "облачный выбор не изменился"
    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(_CREATED, None)),
    ) as started:
        assert await sh.start_due_runs(db) == 1
    assert started.await_count == 1
    row = await _advisor_row(db, task_id)
    assert row["agent_id"] == "agent-1" and row["model"] == "gpt-5.3-codex"
    assert svc.jobs == [] and not list(local_spool.glob("job-*"))

    # Облачного кандидата нет. Локальная модель названа, путь не готов: отказ
    # называет причину локального пути. Локальной модели нет — прежний
    # агрегированный отказ no_advisor_family, слово в слово.
    monkeypatch.setattr(config, "STEWARD_ADVISOR_MODELS", ())
    _beat_caps(local_spool, None)
    project_id = await _project(db, "cloud-none-local-named")
    named = await _task(
        db, project_id, implementer="claude-opus-5", reviewer="grok-4.6"
    )
    await _judge_run(db, named, model="composer-2.5")
    await _judge(db, named)
    assert await order_due_advisors(db) == 0
    refusal = [
        e
        for e in await _local_events(db, named)
        if e["event"] == "steward_run_refused" and e.get("kind") == KIND_ADVISOR
    ]
    assert refusal and refusal[-1]["reason"] == "advisor_not_launchable", refusal

    monkeypatch.setattr(config, "STEWARD_ADVISOR_LOCAL_MODEL", "")
    project_id = await _project(db, "cloud-none")
    other = await _task(
        db, project_id, implementer="claude-opus-5", reviewer="grok-4.6"
    )
    await _judge_run(db, other, model="composer-2.5")
    await _judge(db, other)
    assert await order_due_advisors(db) == 0
    refusal = [
        e
        for e in await _local_events(db, other)
        if e["event"] == "steward_run_refused" and e.get("kind") == KIND_ADVISOR
    ]
    assert refusal and refusal[-1]["reason"] == "no_advisor_family", refusal
    # Протокол ревью v1 не тронут: задание ревью несёт ровно прежние поля.
    jobdir = local_reviewer._submit_job(str(local_spool), "промт", 60)
    assert json.loads(Path(jobdir, "job.json").read_text()) == {
        "version": 1,
        "timeout_sec": 60,
    }


async def test_the_start_update_binds_generation_approve_and_window(
    db: aiosqlite.Connection, local_spool, local_identity, local_service, monkeypatch
):
    """#1649 (Codex P2-2): переход к запуску — ОДИН условный UPDATE.

    Пересдача, уход из review, пропавший approve или сжавшееся окно между
    последней проверкой и UPDATE: ни кода, ни задания, и причина названа. А
    после выпуска кода перед публикацией проверка идёт ещё раз.
    """
    from hub.services import steward_advisor_local as local_mod
    from hub.services.steward_advisor import start_advisor_run
    from hub.services.steward_advisor_local import wait_for_local_advisors

    svc = local_service()
    real = local_mod._remaining_sec

    async def _case(slug: str, sql: str) -> dict:
        task_id, order = await _ordered_advisor(db, slug)

        async def _hook(db_, run_id):
            left = await real(db_, run_id)
            await db_.execute(sql, (task_id,))
            await db_.commit()
            return left

        monkeypatch.setattr(local_mod, "_remaining_sec", _hook)
        minted, published = len(local_identity), len(svc.jobs)
        assert await start_advisor_run(db, dict(order)) is True
        await wait_for_local_advisors()
        monkeypatch.setattr(local_mod, "_remaining_sec", real)
        row = await _advisor_row(db, task_id)
        assert not row["started_at"], (slug, row)
        assert len(local_identity) == minted and len(svc.jobs) == published, slug
        return row

    moved = await _case(
        "start-gen", "UPDATE tasks SET submission_generation=2 WHERE id=?"
    )
    assert moved["status"] == "refused"
    left_review = await _case(
        "start-status", "UPDATE tasks SET status='running' WHERE id=?"
    )
    assert left_review["status"] == "refused"
    no_approve = await _case(
        "start-approve",
        "UPDATE steward_judgements SET verdict='changes_requested' "
        "WHERE task_id=? AND kind='verdict'",
    )
    assert no_approve["status"] == "refused"
    short = await _case(
        "start-window",
        "UPDATE steward_runs SET deadline_at=datetime('now','+5 minutes') "
        "WHERE task_id=? AND kind='advisor'",
    )
    assert short["status"] == "never_started"

    # --- после выпуска кода, перед публикацией: проверка повторяется
    task_id, order = await _ordered_advisor(db, "start-after-mint")
    real_mint = local_mod._mint_code

    async def _mint_then_resubmit(db_, handle):
        result = await real_mint(db_, handle)
        await db_.execute(
            "UPDATE tasks SET submission_generation=2 WHERE id=?", (task_id,)
        )
        await db_.commit()
        return result

    monkeypatch.setattr(local_mod, "_mint_code", _mint_then_resubmit)
    published = len(svc.jobs)
    assert await start_advisor_run(db, dict(order)) is True
    await wait_for_local_advisors()
    row = await _advisor_row(db, task_id)
    assert row["agent_id"].startswith("local:") and row["status"] == "refused", row
    assert len(svc.jobs) == published, "задание опубликовано под старую сдачу"


async def test_a_closed_order_is_not_published_while_the_publication_waits_for_the_lock(
    db: aiosqlite.Connection, local_spool, local_identity, local_service
):
    """#1649 (Codex P2-3): заказ закрыт, пока поток ждал .drain.lock, — job.json не пишется."""
    import fcntl

    from hub.services import steward_advisor_local as local_mod
    from hub.services.steward_advisor import start_advisor_run
    from hub.services.steward_advisor_local import wait_for_local_advisors

    svc = local_service()
    task_id, order = await _ordered_advisor(db, "close-while-publishing")
    fd = os.open(local_spool / ".drain.lock", os.O_RDWR | os.O_CREAT, 0o660)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert await start_advisor_run(db, dict(order)) is True
        handle = None
        for _ in range(300):
            handle = local_mod._HANDLES.get(order["id"])
            if handle is not None and handle.phase == "running":
                break
            await asyncio.sleep(0.02)
        assert handle is not None and handle.phase == "running"
        await asyncio.sleep(0.3)  # поток публикации уже крутится на замке
        await repo.update_task(db, task_id, submission_generation=2)
        await db.commit()
        await close_finished_runs(db)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    await wait_for_local_advisors()
    assert svc.jobs == [] and not list(local_spool.glob("job-*")), "заказ опубликован"
    assert (await _advisor_row(db, task_id))["status"] == "superseded"


async def test_a_finished_supervisor_forgets_only_its_own_handle():
    """#1649 (Codex P2-5): после возврата заказа новый supervisor не теряет регистрацию."""
    from hub.services import steward_advisor_local as local_mod

    def _handle() -> local_mod._Handle:
        return local_mod._Handle(
            run_id=987001, task_id=1, generation=1, model=_GLM, db_path="", live_db=None
        )

    old, new = _handle(), _handle()
    local_mod._HANDLES[987001] = new
    local_mod._forget_handle(old)  # старый завершается позже нового
    assert local_mod._HANDLES.get(987001) is new, "стёрт чужой handle"
    local_mod._forget_handle(new)
    assert 987001 not in local_mod._HANDLES


async def test_the_local_advisor_doors_refuse_a_state_task(
    db: aiosqlite.Connection, local_spool, local_identity, local_service
):
    """#1647: локальный советник (#1649) не берёт задачу-состояние ни на входе,
    ни в последних проверках после очереди и перед публикацией."""
    from hub.services import steward_advisor_local as local_mod
    from hub.services.result_kind import AUTOMATION_REFUSAL

    svc = local_service()
    with _cursor_never_called() as cursor_attempt:
        # --- 1. вход: захвата нет, заказ закрыт названной причиной
        task_id, order = await _ordered_advisor(db, "state-local-entry")
        await db.execute("UPDATE tasks SET result_kind='state' WHERE id=?", (task_id,))
        await db.commit()
        assert not await local_mod.start_local_advisor(db, dict(order))
        row = await _advisor_row(db, task_id)
        assert row["status"] == "refused" and AUTOMATION_REFUSAL in row["closed_reason"]
        assert not row["agent_id"].startswith(("pending:", "local:")), row

        # --- 2. задача стала state, пока заказ ждал слот: ни кода, ни задания
        task2, order2 = await _ordered_advisor(db, "state-local-slot")
        minted, published = len(local_identity), len(svc.jobs)
        async with local_reviewer._HOST_BUDGET:
            assert await local_mod.start_local_advisor(db, dict(order2)) is True
            await asyncio.sleep(0.05)
            await db.execute(
                "UPDATE tasks SET result_kind='state' WHERE id=?", (task2,)
            )
            await db.commit()
        await local_mod.wait_for_local_advisors()
        closed = await _advisor_row(db, task2)
        assert closed["status"] == "refused" and not closed["started_at"], closed
        assert len(local_identity) == minted and len(svc.jobs) == published
    assert cursor_attempt.await_count == 0
