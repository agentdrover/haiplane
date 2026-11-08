"""Cloud deep reviewer: harness without MCP, live code, own reason for no access (#2280).

Three claims of the task, each held at the seam where it can break:

* AC-1: the deep prompt of a run that HAS an access code names no MCP tool at
  all, and says which reason to declare when the code does not buy a session.
* AC-3: the code the dispatch mints (``review_dispatch._access_code``) is
  still good after the cold-start window and is not good after its term or a
  second spend.
* the reason itself: ``access`` is its own word, it is not an environment
  refusal, and the card, the ladder note and the counter say so.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.models import (
    INCOMPLETE_REASON_ACCESS,
    INCOMPLETE_REASONS,
    normalise_incomplete_reason,
)
from hub.services import admin as admin_svc
from hub.services import chat_pair as cp
from hub.services.review_dispatch import (
    ACCESS_REFUSAL_NOTE,
    DEEP,
    LITE,
    _access_code,
    _delivery_block,
    _ladder_cause_note,
    _review_prompt,
    count_environment_refusals,
    is_environment_refusal,
)

_MCP_TOOL = re.compile(r"\bhub_[a-z_]+")
_COLD_START_SECONDS = 600
_DELIVERY_CODE = "ABCD-EFGH"


def _prompt(profile: str, http: bool) -> str:
    delivery = (
        _delivery_block(2280, _DELIVERY_CODE, "https://hub.example") if http else ""
    )
    return _review_prompt(
        2280,
        "task-2280/x",
        "grok-4.7",
        profile,
        "RULES",
        "DIFF",
        "PREPASS",
        delivery_block=delivery,
    )


# --- AC-1: the prompt of a run with a code names no MCP tool ---------------


@pytest.mark.parametrize("profile", [LITE, DEEP])
def test_a_prompt_with_an_access_code_names_no_mcp_tool(profile: str) -> None:
    """AC-1: ни одного hub_*-инструмента там, где у рана есть код доступа.

    Не только hub_get_skill: любое имя инструмента MCP учит ран тратить ходы на
    то, чего в облаке нет (#1036, #1084). Харнесс deep при этом приезжает в
    брифе (#1586), и промт говорит откуда.
    """
    http = _prompt(profile, True)
    assert _MCP_TOOL.findall(http) == []
    if profile == DEEP:
        assert "поля harness_skill брифа" in http


def test_the_mcp_prompt_still_names_its_tools() -> None:
    """Контроль: без кода шаг 1 прежний — иначе проверка выше проверяла бы пустоту."""
    mcp = _prompt(DEEP, False)
    assert "hub_get_skill('multi-agent-review')" in mcp
    assert set(_MCP_TOOL.findall(mcp)) >= {"hub_get_skill", "hub_submit_machine_review"}


@pytest.mark.parametrize("profile", [LITE, DEEP])
def test_the_prompt_names_the_reason_for_a_refused_redeem(profile: str) -> None:
    """Отказ обмена кода — своя причина ``access``, а не ``environment``.

    Промт говорит это в блоке доступа, ДО чтения кода: ран, которому код не
    подошёл, не должен выбирать между «нечем запустить проверки» и тишиной.
    """
    http = _prompt(profile, True)
    block = http[http.index("ЕСЛИ ДОСТУП НЕ ПОЛУЧЕН") :]
    block = block[: block.index("\n\n")].replace("\n", " ")
    assert 'incomplete_reason="access"' in block
    assert "chat_pair_invalid" in block
    assert "не отказ среды" in block
    assert "lost_dimensions" in block
    # Сам код в отчёт не просится: он одноразовый, но лежит в чужом транскрипте.
    assert "не пиши" in block and "код доступа" in block
    assert "ЕСЛИ ДОСТУП НЕ ПОЛУЧЕН" not in _prompt(profile, False)


def test_the_block_without_a_code_is_still_empty() -> None:
    """Нет кода — нет блока: инструкция про обмен несуществующего кода сбивала бы ран."""
    assert _delivery_block(2280, "", "https://hub.example") == ""
    assert _delivery_block(2280, _DELIVERY_CODE, "") == ""


# --- the word itself -------------------------------------------------------


def test_access_is_its_own_word_and_not_an_environment_refusal() -> None:
    assert INCOMPLETE_REASON_ACCESS == "access"
    assert INCOMPLETE_REASON_ACCESS in INCOMPLETE_REASONS
    assert normalise_incomplete_reason(" Access ") == "access"
    assert normalise_incomplete_reason("redeem") == "", "чужое слово — «не заявлена»"
    report = {"incomplete": True, "incomplete_reason": "access"}
    assert is_environment_refusal(report) is False
    assert _ladder_cause_note(report) == ACCESS_REFUSAL_NOTE
    assert "не отказ среды" in ACCESS_REFUSAL_NOTE


async def _reviewable_task(db) -> int:
    from hub import services
    from hub.models import TaskCreate

    task = await services.create_task(db, TaskCreate(title="access reason"))
    await repo.add_task_update(db, task.id, "dev", "status", "Plan: access")
    await db.commit()
    await services.pair_start_task(db, task.id, caller="dev")
    await services.submit_for_review(db, task.id)
    return task.id


def _refusal(reason: str, lost: str) -> dict:
    return {
        "raw_count": 1,
        "incomplete": True,
        "incomplete_reason": reason,
        "harness_skill": "multi-agent-review",
        "findings_confirmed": [],
        "findings_rejected": [],
        "unresolved": [],
        "lost_dimensions": [lost],
        "agent": "cursor-cloud-reviewer",
        "model": "grok-4.7",
    }


async def test_a_refused_redeem_is_named_on_the_card_and_counted_apart(client, db):
    """Карточка, счётчик и примечание лестницы разводят доступ и среду."""
    denied = await _reviewable_task(db)
    resp = await client.post(
        f"/api/tasks/{denied}/machine-review",
        json=_refusal("access", "доступ к хабу не получен: обмен кода дал 401"),
    )
    assert resp.status_code == 200, resp.text
    stored = dict(await repo.get_latest_machine_review(db, denied))
    assert stored["incomplete_reason"] == "access"
    assert is_environment_refusal(stored) is False

    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, denied)
        if dict(u)["kind"] == "alert"
    ]
    access_alerts = [a for a in alerts if "НЕ ПОЛУЧИЛ ДОСТУП" in a]
    assert len(access_alerts) == 1, alerts
    assert "обмен кода дал 401" in access_alerts[0]
    assert "chat_pair_redeem_refused" in access_alerts[0], "куда смотреть причину"
    assert not [a for a in alerts if "ОТКАЗУ СРЕДЫ" in a], "это не отказ среды"

    broken = await _reviewable_task(db)
    resp = await client.post(
        f"/api/tasks/{broken}/machine-review",
        json=_refusal("environment", "нет pytest в среде"),
    )
    assert resp.status_code == 200, resp.text
    env_alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, broken)
        if "ОТКАЗУ СРЕДЫ" in dict(u)["content"]
    ]
    assert len(env_alerts) == 1, "отказ среды называется как прежде"
    assert not [
        dict(u)
        for u in await repo.get_task_updates(db, broken)
        if "НЕ ПОЛУЧИЛ ДОСТУП" in dict(u)["content"]
    ]

    stats = await count_environment_refusals(db, since_days=30)
    assert stats["access_refusals"] == 1
    assert stats["environment_refusals"] == 1
    assert stats["reason_declared"] == 2
    assert stats["reason_unstated"] == 0


# --- AC-3: the code of the dispatch outlives the cold start ----------------


@pytest.fixture
async def reviewer(client, db, monkeypatch):
    """Принципал ревьюера и задача на ревью, поколение 1 — как у заказа хаба."""
    cp.chat_pair_limiter._buckets.clear()
    cp._unknown_audit_seen.clear()
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    # Not open mode: pairing is refused when there is no identity to carry.
    monkeypatch.setattr(
        config, "HUB_TOKENS", {"agent-token": TokenIdentity("bot", "agent")}
    )
    principal = await admin_svc.create_principal(
        db, kind="agent", username="cursor-cloud-reviewer", role_slug="operator"
    )
    task_id = await _reviewable_task(db)
    return SimpleNamespace(
        client=client, db=db, principal=principal["id"], task=task_id
    )


async def _age(db, code: str, seconds: int) -> None:
    """«Сейчас» позже на ``seconds``: сдвиг меток строки, а не ожидание."""
    await db.execute(
        "UPDATE chat_pair_codes SET expires_at = datetime(expires_at, ?) "
        "WHERE code_hash = ?",
        (f"-{seconds} seconds", cp.hash_pair_code(cp.normalize_pair_code(code))),
    )
    await db.commit()


async def _redeem(reviewer, code: str, ip: str):
    return await reviewer.client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": code},
        headers={"x-forwarded-for": ip},
    )


async def _mint(reviewer, generation: int = 1) -> str:
    code = await _access_code(
        reviewer.db, reviewer.task, generation, reviewer.principal
    )
    assert code, "принципал есть — код обязан быть"
    return code


async def test_the_dispatch_code_redeems_after_the_cold_start(reviewer):
    """AC-3: код заказа принимается спустя окно холодного старта; жил бы 300 с — нет."""
    assert config.CHAT_PAIR_CODE_SECONDS < _COLD_START_SECONDS
    code = await _mint(reviewer)
    await _age(reviewer.db, code, _COLD_START_SECONDS)

    first = await _redeem(reviewer, code, "198.51.100.1")
    assert first.status_code == 200, first.text
    session = {"Authorization": f"Bearer {first.json()['token']}"}
    brief = await reviewer.client.get(
        f"/api/tasks/{reviewer.task}/review-brief", headers=session
    )
    assert brief.status_code == 200, brief.text
    assert brief.json()["harness_skill"]["name"] == "multi-agent-review"

    # Одноразовость: тот же код второй раз — отказ.
    again = await _redeem(reviewer, code, "198.51.100.2")
    assert again.status_code == 401
    assert again.json()["detail"]["reason"] == "chat_pair_invalid"


async def test_the_dispatch_code_does_not_redeem_after_its_term(reviewer):
    """AC-3: после срока кода обмен отказывает — окно длиннее, но не бесконечное."""
    code = await _mint(reviewer)
    await _age(reviewer.db, code, config.CHAT_PAIR_REVIEWER_CODE_SECONDS + 60)
    late = await _redeem(reviewer, code, "198.51.100.3")
    assert late.status_code == 401
    assert late.json()["detail"]["reason"] == "chat_pair_invalid"


async def test_a_reissue_burns_the_unused_code_of_the_same_generation(reviewer):
    """Привязка и одноразовость целы: свежий код гасит прежний, чужое поколение — нет."""
    first = await _mint(reviewer)
    second = await _mint(reviewer)
    assert (await _redeem(reviewer, first, "198.51.100.4")).status_code == 401
    assert (await _redeem(reviewer, second, "198.51.100.5")).status_code == 200

    await reviewer.db.execute(
        "UPDATE tasks SET submission_generation = 2 WHERE id = ?", (reviewer.task,)
    )
    await reviewer.db.commit()
    stale = await _mint(reviewer, generation=1)
    assert (await _redeem(reviewer, stale, "198.51.100.6")).status_code == 401, (
        "код поколения 1 не годится для сдачи поколения 2"
    )
