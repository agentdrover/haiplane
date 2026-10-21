"""Поверхности критика облачной сдачи (#1953): политика, карточка, закрепление sha, санитайзер.

Дополняют tests/test_critic_orders.py: те четыре теста доказывают AC; здесь - углы,
которые эти AC не называют поимённо, но на которых гейт можно сломать молча.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hub import mcp_task_card
from hub.models import validated_gate_policy
from hub.services import lifecycle, project_policy
from hub.services.untrusted_quote import (
    OUT_LIMIT,
    sanitize_untrusted_quote,
    valid_host_ref,
)
from tests import test_critic_orders as co

world = co.world
keys = co.keys
tip = co.tip
_clean_limiter = co._clean_limiter
_no_cloud = co._no_cloud

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "untrusted_quote_vectors.json").read_text(
        encoding="utf-8"
    )
)["vectors"]


def test_critic_host_policy_accepts_only_a_host_name_or_empty():
    """Значение, которое читатель прочёл бы как «выключено», запись отвергает."""
    assert validated_gate_policy({"critic_host": "k8"}) == {"critic_host": "k8"}
    assert validated_gate_policy({"critic_host": ""}) == {"critic_host": ""}
    for bad in ("K8", "k 8", "k8/../x", "x" * 40, 5, None, ["k8"]):
        with pytest.raises(ValueError, match="critic_host"):
            validated_gate_policy({"critic_host": bad})
    assert project_policy.critic_host_of({}) == ""
    assert project_policy.critic_host_of({"critic_host": "k8"}) == "k8"
    assert project_policy.critic_host_of({"critic_host": "bad name"}) == ""
    assert project_policy.critic_host_of({"critic_host": 7}) == ""


def test_the_sanitizer_is_one_bounded_function():
    """Векторы общего набора вычищены; размер ограничен; не-строка - пусто."""
    for vector in VECTORS:
        out = sanitize_untrusted_quote(vector["text"])
        for code in vector["forbidden_chars"]:
            assert chr(code) not in out, vector["name"]
        for part in vector["forbidden_text"]:
            assert part not in out, vector["name"]
    long_text = "<b>&" * 5000
    out = sanitize_untrusted_quote(long_text)
    assert len(out) <= OUT_LIMIT and out.endswith("…")
    assert not out[:-1].endswith(
        ("&", "&l", "&lt", "&am")
    )  # экранирование не разорвано
    for empty in (None, 5, b"x", "", "\x00‮"):
        assert sanitize_untrusted_quote(empty) == ""
    assert valid_host_ref("vm-1:abc/1") and not valid_host_ref("a b")
    assert not valid_host_ref("") and not valid_host_ref("x" * 81)


async def test_the_card_shows_the_last_order_and_computes_overdue_without_writing(
    world, tip
):
    """REST и строки карточки: sha, состояние, срок и шаг; просрочка видна без записи."""
    cloud = await co._cloud(world, tip)
    resp = await world.client.get(
        f"/api/tasks/{cloud.task_id}", headers=world.human_auth
    )
    assert resp.json()["critic_order"] is None
    assert (await co._say(world, cloud, co._line(co.SHA_A))).status_code == 200
    card = (
        await world.client.get(f"/api/tasks/{cloud.task_id}", headers=world.human_auth)
    ).json()["critic_order"]
    assert (card["sha"], card["state"], card["outcome"]) == (co.SHA_A, "pending", None)
    assert card["next_step"] and card["expires_at"]
    (line,) = mcp_task_card.critic_order_lines({"critic_order": card})
    assert co.SHA_A[:12] in line
    assert mcp_task_card.critic_order_lines({}) == []

    await world.db.execute(
        "UPDATE critic_orders SET expires_at = datetime('now', '-1 minute')"
    )
    await world.db.commit()
    late = (
        await world.client.get(f"/api/tasks/{cloud.task_id}", headers=world.human_auth)
    ).json()["critic_order"]
    assert (late["state"], late["outcome"]) == ("closed", "expired")
    stored = (await co._orders(world, cloud.task_id))[0]
    assert (stored["state"], stored["outcome"]) == (
        "pending",
        None,
    )  # читатель не пишет


async def test_done_pins_the_accepted_sha_not_a_later_observation(world, monkeypatch):
    """done: приняли A (passed), потом голова B: пин - принятый A; второе наблюдение не пин."""
    cloud = await co._cloud(world, None)
    await co._closed_order(world, cloud, co.SHA_A, "passed")
    seen: list[str] = []

    async def _moving(db, task_id, branch):
        seen.append(branch)
        return (co.SHA_A if len(seen) == 1 else co.SHA_B), ""

    monkeypatch.setattr(lifecycle, "resolve_branch_tip", _moving)
    resp = await co._done(world, cloud)
    assert resp.status_code == 200, resp.text
    status, generation, pinned, _ = await co._task_state(world, cloud.task_id)
    assert (status in ("review", "ci_check"), generation, pinned) == (True, 1, co.SHA_A)


def test_a_request_line_inside_a_longer_entry_is_not_a_request():
    """Ограждение разбирается как в CommonMark: тип, длина, отступ, незакрытое."""
    from hub.services.critic_orders import parse_request_line

    line = co._line(co.SHA_A)
    for text in (
        f"````\n~~~\n{line}\n````",  # ~~~ внутри обратных кавычек не закрывает блок
        f"   ```\n{line}\n   ```",  # ограждение с отступом
        f"```\n{line}",  # блок не закрыт
        f"~~~~\n{line}\n~~~",  # закрытие короче открытия
        f"> {line}",
        f"{line} и ещё слова",
    ):
        assert parse_request_line(text)[0] is None, text
    assert parse_request_line(f"{line}\n{co._line(co.SHA_B)}") == (None, "request_line")
    assert parse_request_line(line) == (co.SHA_A, "")


async def test_a_changed_report_differing_past_the_cut_is_still_a_conflict(world, tip):
    """Повтор сравнивается с присланным отчётом, а не с усечённой выжимкой."""
    cloud = await co._cloud(world, tip)
    assert (await co._say(world, cloud, co._line(co.SHA_A))).status_code == 200
    oid = (await co._orders(world, cloud.task_id))[0]["id"]
    head = "x" * 900
    assert (
        await co._report(world, oid, co._passed(summary=head + "a"))
    ).status_code == 200
    assert (
        await co._report(world, oid, co._passed(summary=head + "a"))
    ).status_code == 200
    assert (
        await co._report(world, oid, co._passed(summary=head + "b"))
    ).status_code == 409


async def test_the_order_is_rechecked_under_the_write_lock(world, tip):
    """Держатель сменился или заказ уже passed, пока шла сеть: заказа нет, причина названа."""
    # Держатель сменился на другой облачный прогон.
    cloud = await co._cloud(world, tip)
    other_run = await co.repo.create_executor_run(
        world.db,
        task_id=cloud.task_id,
        submission_generation=0,
        agent_id="cursor",
        run_id="bc-other",
        model="m",
        channel="cloud",
    )
    real = lifecycle.resolve_branch_tip

    async def _swap(db, task_id, branch):
        await db.execute(
            "UPDATE tasks SET executor_run_id=? WHERE id=?", (other_run, task_id)
        )
        await db.commit()
        return co.SHA_A, ""

    lifecycle.resolve_branch_tip = _swap
    try:
        assert (await co._say(world, cloud, co._line(co.SHA_A))).status_code == 200
        assert await co._orders(world, cloud.task_id) == []
        assert any(
            "not_cloud_holder" in a for a in await co._alerts(world, cloud.task_id)
        )

        # Параллельный заказ успел закрыться passed, пока второй ждал сеть.
        second = await co._cloud(world, tip)

        async def _raced(db, task_id, branch):
            await co._closed_order(world, second, co.SHA_A, "passed")
            return co.SHA_A, ""

        lifecycle.resolve_branch_tip = _raced
        assert (await co._say(world, second, co._line(co.SHA_A))).status_code == 200
        assert [o["outcome"] for o in await co._orders(world, second.task_id)] == [
            "passed"
        ]
        assert any(
            "already_passed" in a for a in await co._alerts(world, second.task_id)
        )
    finally:
        lifecycle.resolve_branch_tip = real


async def test_every_door_pins_the_sha_the_critic_passed(world, monkeypatch):
    """pending_report и автоматический вход закрепляют принятый sha."""
    from hub.services import orchestration

    # pending_report: ревью привязано к sha, по которому прошёл критик, а не к старому.
    cloud = await co._cloud(world, None)
    await co.repo.update_task(
        world.db, cloud.task_id, status="pending_report", submission_sha=co.SHA_A
    )
    await world.db.commit()
    await co._closed_order(world, cloud, co.SHA_B, "passed")

    async def _head_b(db, task_id, branch):
        return co.SHA_B, ""

    monkeypatch.setattr(lifecycle, "resolve_branch_tip", _head_b)
    ok = await co._done(world, cloud)
    assert ok.status_code == 200, ok.text
    status, generation, pinned, _ = await co._task_state(world, cloud.task_id)
    assert (status, generation, pinned) == ("review", 1, co.SHA_B)

    # Автоматический вход: принят A; второе наблюдение (B) в пин не попадает.
    auto = await co._cloud(world, None)
    await co._closed_order(world, auto, co.SHA_A, "passed")
    seen: list[int] = []

    async def _moving(db, task_id, branch):
        seen.append(task_id)
        return (co.SHA_A if len(seen) == 1 else co.SHA_B), ""

    monkeypatch.setattr(lifecycle, "resolve_branch_tip", _moving)
    row = dict(
        (await co._rows(world.db, "SELECT * FROM tasks WHERE id=?", (auto.task_id,)))[0]
    )
    await orchestration.transition_after_agent_done(world.db, row, has_done=True)
    await world.db.commit()
    status, generation, pinned, _feed = await co._task_state(world, auto.task_id)
    assert (status != "needs_decision", generation, pinned) == (True, 1, co.SHA_A)


async def test_a_plain_key_gets_a_reason_but_a_bare_mention_stays_text(world, tip):
    """Строка заказа от ключа без сессии - alert no_session; просто слова - без записи."""
    cloud = await co._cloud(world, tip)
    body = {"agent": "x", "kind": "status", "content": co._line(co.SHA_A)}
    posted = await world.client.post(
        f"/api/tasks/{cloud.task_id}/updates", json=body, headers=world.human_auth
    )
    assert posted.status_code == 200
    assert await co._orders(world, cloud.task_id) == []
    assert any("no_session" in a for a in await co._alerts(world, cloud.task_id))
    before = len(await co._alerts(world, cloud.task_id))
    body["content"] = "когда нужен критик, я напишу строку отдельно"
    assert (
        await world.client.post(
            f"/api/tasks/{cloud.task_id}/updates", json=body, headers=world.human_auth
        )
    ).status_code == 200
    assert len(await co._alerts(world, cloud.task_id)) == before


async def test_a_session_revoked_during_the_network_step_orders_nothing(world, tip):
    """Сессия вызывающего отозвана, пока шла сеть: под write-локом доказательство перечитано."""
    cloud = await co._cloud(world, tip)
    real = lifecycle.resolve_branch_tip

    async def _revoke(db, task_id, branch):
        await db.execute("UPDATE chat_pair_sessions SET revoked_at = datetime('now')")
        await db.commit()
        return co.SHA_A, ""

    lifecycle.resolve_branch_tip = _revoke
    try:
        assert (await co._say(world, cloud, co._line(co.SHA_A))).status_code == 200
    finally:
        lifecycle.resolve_branch_tip = real
    assert await co._orders(world, cloud.task_id) == []
    assert any("not_cloud_holder" in a for a in await co._alerts(world, cloud.task_id))
