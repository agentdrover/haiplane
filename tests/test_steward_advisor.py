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

import json
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.db import fetchall
from hub.integrations import cursor_cloud
from hub.models import StewardJudgementSubmit
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
    ):
        assert await sh.start_due_runs(db) == 0

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

    async def _boom(_db, _task_id, _generation):
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
    db: aiosqlite.Connection,
):
    """Поколение ушло — согласие о прежнем коде не применяется (само по себе)."""
    task_id = await _in_review(db, "advisor-superseded-consent")
    await _pair_on_packet(db, task_id)
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
