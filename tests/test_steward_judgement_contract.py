"""Судья без оснований и уверенности: 422 в том же прогоне, а не к владельцу (#1924).

8 из 17 передач стюарда за 17-20.10 — no_grounds: судья слал «голое» тело
(без grounds и confidence), потому что в промпте стояла заглушка вместо
образца. Теперь образец в промпте, а approve/changes_requested без засчитанных
оснований (detail непуст) или без confidence получают 422 и ничего не пишут.
Правило #1327 ужесточено: такой ответ не хранится и не применяется никогда.

AC-1: 422 с образцом и списком полей, затем исправленное тело принято.
AC-2: пустой detail / нет confidence -> 422; escalate принят; не исправил до
      дедлайна -> прогон закрыт, суждения нет.
AC-3: в промпте образец тела, заглушки нет.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import repository as repo
from hub.db import fetchall
from hub.models import StewardGround, StewardJudgementSubmit
from hub.services.steward_judgement_sample import (
    DETAIL_MARKER,
    steward_judgement_body_sample,
)
from hub.services import steward_shadow
from hub.services.steward_dispatch import (
    RUN_OPEN,
    RUN_TIMEOUT,
    close_finished_runs,
    order_run,
)
from hub.services.steward_judgement import record_steward_judgement
from tests.test_steward_identity import _steward_principal
from tests.test_steward_judgement import (
    _STEWARD,
    _started_run,
    ci_is_not_the_subject,  # noqa: F401 — autouse fixture
)

_REAL_IDENTITY_DELIVERY = steward_shadow.identity_delivery

_GOOD = {"source": "ci_pinned_sha", "detail": "ci_run 1234 success on sha abc123"}


async def _post(db, task_id: int, **fields):
    body = {"generation": 1, "kind": "verdict", "model": "gpt-5.3-codex"}
    body.update(fields)
    return await record_steward_judgement(
        db, task_id, StewardJudgementSubmit(**body), _STEWARD
    )


async def _run_status(db, task_id: int) -> str:
    rows = await fetchall(
        db, "SELECT status FROM steward_runs WHERE task_id=?", (task_id,)
    )
    return rows[0]["status"]


async def _stored(db, task_id: int):
    return await repo.get_steward_judgement(db, task_id, 1, "verdict")


async def test_ungrounded_judge_gets_422_and_can_fix_in_the_same_run(
    db: aiosqlite.Connection, monkeypatch
):
    task_id = await _started_run(db, monkeypatch, "c1924-ac1")

    with pytest.raises(HTTPException) as err:
        await _post(db, task_id, verdict="approve")
    assert err.value.status_code == 422
    detail = err.value.detail
    assert detail["reason"] == "steward_judgement_incomplete"
    assert detail["missing"] == ["grounds", "confidence"]
    assert detail["sample"] == steward_judgement_body_sample(1)
    assert await _stored(db, task_id) is None, "ничего не записано"
    refused = await fetchall(
        db,
        "SELECT payload FROM events WHERE task_id=? AND kind='steward_judgement_refused'",
        (task_id,),
    )
    assert len(refused) == 1
    assert json.loads(refused[0]["payload"])["missing"] == ["grounds", "confidence"]
    assert await _run_status(db, task_id) == RUN_OPEN, "заказ открыт для исправления"

    view = await _post(
        db, task_id, verdict="approve", confidence="high", grounds=[_GOOD]
    )
    assert view.verdict == "approve"
    assert view.escalate_reason == ""
    assert await _stored(db, task_id) is not None


_EMPTY = [
    {"source": "ci_pinned_sha", "detail": ""},
    {"source": "risk_class", "detail": "   "},
]


@pytest.mark.parametrize("verdict", ["approve", "changes_requested"])
@pytest.mark.parametrize(
    ("fields", "missing"),
    [
        ({"confidence": "high", "grounds": _EMPTY}, ["grounds[].detail"]),
        ({"confidence": "high", "grounds": []}, ["grounds"]),
        ({"grounds": [_GOOD]}, ["confidence"]),
        ({"grounds": _EMPTY}, ["grounds[].detail", "confidence"]),
    ],
)
async def test_ungrounded_answers_never_stand(
    db: aiosqlite.Connection, monkeypatch, verdict, fields, missing
):
    task_id = await _started_run(db, monkeypatch, f"c1924-ac2-{verdict}-{len(missing)}")
    with pytest.raises(HTTPException) as err:
        await _post(db, task_id, verdict=verdict, **fields)
    assert err.value.status_code == 422
    assert err.value.detail["missing"] == missing
    assert await _stored(db, task_id) is None
    events = await fetchall(
        db,
        "SELECT kind FROM events WHERE task_id=? AND kind LIKE 'steward%'",
        (task_id,),
    )
    assert [e["kind"] for e in events if e["kind"] in ("steward_applied",)] == []


async def test_escalation_without_grounds_is_still_accepted(
    db: aiosqlite.Connection, monkeypatch
):
    task_id = await _started_run(db, monkeypatch, "c1924-ac2-esc")
    view = await _post(
        db, task_id, verdict="escalate", escalate_reason="precondition_failed"
    )
    assert view.verdict == "escalate"
    assert view.escalate_reason == "precondition_failed"


async def test_low_confidence_still_downgrades_instead_of_422(
    db: aiosqlite.Connection, monkeypatch
):
    task_id = await _started_run(db, monkeypatch, "c1924-ac2-low")
    view = await _post(db, task_id, verdict="approve", confidence="low")
    assert view.verdict == "escalate"
    assert view.escalate_reason == "low_confidence"


async def test_judge_that_never_fixes_the_body_leaves_no_judgement(
    db: aiosqlite.Connection, monkeypatch
):
    task_id = await _started_run(db, monkeypatch, "c1924-ac2-deadline")
    with pytest.raises(HTTPException):
        await _post(db, task_id, verdict="approve")
    await db.execute(
        "UPDATE steward_runs SET deadline_at=datetime('now', '-1 minute') "
        "WHERE task_id=?",
        (task_id,),
    )
    await db.commit()
    assert await close_finished_runs(db) == 1
    assert await _run_status(db, task_id) == RUN_TIMEOUT
    assert await _stored(db, task_id) is None, (
        "ответа без оснований нет ни в каком виде"
    )


async def test_judge_prompt_carries_a_body_sample(
    db: aiosqlite.Connection, monkeypatch
):
    """Настоящий старт прогона: промпт, ушедший провайдеру, несёт образец тела."""
    from tests.test_steward_judgement import _CREATED, _steward_on
    from tests.test_steward_shadow import _project, _task

    _steward_on(monkeypatch)
    monkeypatch.setattr(steward_shadow, "identity_delivery", _REAL_IDENTITY_DELIVERY)
    await _steward_principal(db, monkeypatch)
    task_id = await _task(db, await _project(db, "c1924-ac3"))
    assert await order_run(db, task_id, 1) is not None
    create = AsyncMock(return_value=(_CREATED, None))
    with patch("hub.integrations.cursor_cloud.create_agent_attempt", new=create):
        assert await steward_shadow.start_due_runs(db) == 1
    prompt = create.await_args.kwargs["prompt_text"]

    assert "<суждение по контракту>" not in prompt
    sample = json.loads(steward_judgement_body_sample(1))
    for key in ("verdict", "confidence", "grounds", "closures", "generation", "kind"):
        assert key in sample
        assert f'"{key}"' in prompt, key
    assert '"detail"' in prompt and '"source"' in prompt
    assert "422" in prompt
    assert '"verdict": "escalate"' in prompt, "образец эскалации в промпте"


def _body_from_sample(**over) -> dict:
    body = json.loads(steward_judgement_body_sample(1))
    body.update(over)
    return body


async def test_the_sample_is_not_executable_as_it_stands(
    db: aiosqlite.Connection, monkeypatch
):
    """Образец как есть не проходит: подсказки не допустимые значения."""
    task_id = await _started_run(db, monkeypatch, "c1924-sample-verbatim")
    with pytest.raises(HTTPException) as err:
        await record_steward_judgement(
            db, task_id, StewardJudgementSubmit(**_body_from_sample()), _STEWARD
        )
    assert err.value.status_code == 422
    assert await _stored(db, task_id) is None


_MARKER = DETAIL_MARKER


@pytest.mark.parametrize(
    "detail",
    [
        _MARKER,
        f"{_MARKER} проверено",
        f"ci_run 1 {_MARKER}.",
        f"  {_MARKER}\n",
        _MARKER.lower(),
        "__ЗАПОЛНИ\u200b__",
        "__\u200bЗАПОЛНИ__\u200b",
        "__ЗАПОЛНИ__\u00a0факт",
        "\u00a0__ЗАПОЛНИ__\u00a0",
        "＿＿ЗАПОЛНИ＿＿",
        "__ЗА ПОЛНИ__",
        "__ЗА\u034fПОЛНИ__",
        "__ЗАП\ufe0fОЛНИ__",
        "__ЗА\u3164ПОЛНИ__",
        "__ЗА.ПОЛНИ__",
    ],
)
async def test_a_hint_marker_is_not_a_ground_in_any_spelling(
    db: aiosqlite.Connection, monkeypatch, detail
):
    """Класс: маркер образца в detail. Содержание, а не вид: любая запись."""
    task_id = await _started_run(db, monkeypatch, f"c1924-ph-{abs(hash(detail))}")
    body = _body_from_sample(
        verdict="approve",
        confidence="high",
        grounds=[{"source": "ci_pinned_sha", "detail": detail}],
    )
    with pytest.raises(HTTPException) as err:
        await record_steward_judgement(
            db, task_id, StewardJudgementSubmit(**body), _STEWARD
        )
    assert err.value.status_code == 422
    assert err.value.detail["reason"] == "steward_judgement_incomplete"
    assert err.value.detail["missing"] == ["grounds[].detail"]
    assert await _stored(db, task_id) is None


_REAL_FACTS = [
    "<none>",
    "<ci_run=1234, conclusion=success, sha=abc123>",
    "<none> is the actual output <exit=0>",
    "факт пакета: значение, число, sha или поле",
    "concrete fact of the packet",
]


@pytest.mark.parametrize("detail", _REAL_FACTS)
async def test_real_facts_count_for_the_judge(
    db: aiosqlite.Connection, monkeypatch, detail
):
    task_id = await _started_run(db, monkeypatch, f"c1924-fact-{abs(hash(detail))}")
    view = await _post(
        db,
        task_id,
        verdict="approve",
        confidence="high",
        grounds=[{"source": "ci_pinned_sha", "detail": detail}],
    )
    assert view.verdict == "approve"


def test_real_facts_count_for_the_advisor():
    from hub.services.steward_judgement import _downgrade_reason

    for detail in _REAL_FACTS:
        ground = StewardGround(source="ci_pinned_sha", detail=detail)
        assert (
            _downgrade_reason("concur", "high", [ground], detail_required=True) == ""
        ), detail


async def test_refusals_are_counted_next_to_escalations(
    db: aiosqlite.Connection, monkeypatch
):
    from hub.services.orchestration import practice_metrics

    task_id = await _started_run(db, monkeypatch, "c1924-counter")
    with pytest.raises(HTTPException):
        await _post(db, task_id, verdict="approve")
    rows = (await practice_metrics(db))["human_gates"]
    steward = [r for r in rows if r.get("gate") == "steward"]
    assert steward and sum(r["refused"] for r in steward) == 1


async def test_the_sample_needs_only_hint_replacement_to_be_accepted(
    db: aiosqlite.Connection, monkeypatch
):
    """Находка b860d65c89867fd8: в образце нет подставных uid.

    Судья заменяет подсказки значениями и сдаёт то же тело: никакой строки,
    которую надо ещё и удалять (closures пуст на чистом пакете).
    """
    task_id = await _started_run(db, monkeypatch, "c1924-fillable")
    body = _body_from_sample(
        verdict="approve",
        confidence="high",
        grounds=[_GOOD],
    )
    assert body["closures"] == []
    view = await record_steward_judgement(
        db, task_id, StewardJudgementSubmit(**body), _STEWARD
    )
    assert view.verdict == "approve"


@pytest.mark.parametrize(
    "detail", ["\u200b", "\u034f", "\u3164", "...", "\u00a0\u200b"]
)
async def test_an_invisible_detail_is_not_a_ground_for_the_judge(
    db: aiosqlite.Connection, monkeypatch, detail
):
    task_id = await _started_run(db, monkeypatch, f"c1924-inv-{abs(hash(detail))}")
    with pytest.raises(HTTPException) as err:
        await _post(
            db,
            task_id,
            verdict="approve",
            confidence="high",
            grounds=[{"source": "ci_pinned_sha", "detail": detail}],
        )
    assert err.value.status_code == 422
    assert await _stored(db, task_id) is None


def test_the_advisor_keeps_its_1671_rule_marker_or_not():
    """Советник возвращён к gen1: только strip, ни маркера, ни «видимости»."""
    from hub.services.steward_judgement import _effective_verdict

    for detail in (DETAIL_MARKER, "​", "..."):
        body = StewardJudgementSubmit(
            generation=1,
            kind="advisor",
            verdict="concur",
            confidence="high",
            grounds=[{"source": "ci_pinned_sha", "detail": detail}],
        )
        assert _effective_verdict(body, "concur", "high") == ("concur", ""), detail
