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


async def test_done_pins_the_sha_the_critic_passed_or_refuses(world, monkeypatch):
    """done: гейт прошёл на A, а при закреплении голова B - 422 sha_changed, записей нет."""
    cloud = await co._cloud(world, None)
    await co._closed_order(world, cloud, co.SHA_A, "passed")
    seen: list[str] = []

    async def _moving(db, task_id, branch):
        seen.append(branch)
        return (co.SHA_A if len(seen) == 1 else co.SHA_B), ""

    monkeypatch.setattr(lifecycle, "resolve_branch_tip", _moving)
    before = await co._task_state(world, cloud.task_id)
    resp = await co._done(world, cloud)
    assert len(seen) >= 2, "закрепление должно наблюдать голову заново"
    assert co._refused(resp) == "sha_changed"
    assert await co._task_state(world, cloud.task_id) == before
