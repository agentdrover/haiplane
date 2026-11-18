"""Slot orders persist the active implementer instructions at issue time."""

import asyncio
from contextlib import asynccontextmanager

import aiosqlite
import pytest

from hub import db as hub_db
from hub import repository as repo
from hub.services import executor_launch
from hub.services import executor_skill_bundle as skills
from hub.services import executor_slot_launch
from tests.test_executor_orders import (  # noqa: F401 - shared pytest fixtures
    _launch,
    _launch_redeem,
    _orders,
    _reference,
    _rows,
    _task,
    keys,
    world as slot_world,
)

from tests.test_slot_repair_running import _after_cr, _footprint, _go

SKILLS = (
    "executor-pair-discipline",
    "hub-operator",
    "hub-review-cycle",
    "hub-submit-task",
    "hub-review-report-reading",
    "mcp-write-resilience",
)


async def _publish(db, name: str, content: str, *, status: str = "active") -> int:
    _, version = await repo.create_skill_version(
        db,
        name=name,
        kind="skill",
        content=content,
        status=status,
        created_by="test-owner",
    )
    return version


@pytest.fixture
async def skill_world(slot_world):  # noqa: F811 - imported pytest fixture
    for name in SKILLS:
        await _publish(slot_world.db, name, f"\nИнструкция для {name}\r\n")
    await slot_world.db.commit()
    return slot_world


async def test_existing_order_keeps_its_skill_snapshot(skill_world):
    world = skill_world
    first = await _task(world)
    assert (await _launch(world, first)).status_code == 200
    old = (await _orders(world)).json()["orders"][0]
    assert "Инструкция для hub-operator" in old["order_text"]

    await _publish(world.db, "hub-operator", "Новая версия инструкции")
    await world.db.commit()
    second = await _task(world)
    assert (await _launch(world, second, "k9")).status_code == 200
    same = (await _orders(world)).json()["orders"][0]
    new = (await _orders(world, "k9")).json()["orders"][0]
    assert same["id"] == old["id"]
    assert same["order_text"].encode() == old["order_text"].encode()
    assert "Новая версия инструкции" in new["order_text"]
    assert "Инструкция для hub-operator" not in new["order_text"]


async def test_bundle_snapshot_uses_consistent_versions(skill_world, monkeypatch):
    world = skill_world
    task_id = await _task(world)
    waiting_for_writer = asyncio.Event()
    write_transaction = executor_slot_launch.write_transaction

    @asynccontextmanager
    async def observe_slot_writer(db):
        waiting_for_writer.set()
        async with write_transaction(db):
            yield

    monkeypatch.setattr(executor_slot_launch, "write_transaction", observe_slot_writer)
    publisher = await hub_db.connect(world.dsn)
    try:
        await publisher.execute("BEGIN IMMEDIATE")
        versions = {
            name: await _publish(publisher, name, f"Следующий снимок {name}")
            for name in SKILLS
        }
        async with asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(_go(world.db, world, task_id))
            await asyncio.wait_for(waiting_for_writer.wait(), timeout=10)
            await publisher.commit()
        launch = pending.result()
    finally:
        await publisher.close()

    assert launch.launched
    order = (await _orders(world)).json()["orders"][0]
    for name, version in versions.items():
        assert f"slug={name} version={version} " in order["order_text"]
        assert f"Следующий снимок {name}" in order["order_text"]
    run = await _rows(
        world.db,
        "SELECT ordered_skill_version FROM executor_runs WHERE id = ?",
        (order["executor_run_id"],),
    )
    assert run[0]["ordered_skill_version"] == versions["executor-pair-discipline"]


@pytest.mark.parametrize("rework", [False, True])
@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("missing", "missing"),
        ("read", "unreadable"),
        ("blank", "invalid"),
        ("placeholder", "invalid"),
        ("size", "oversize"),
    ],
)
async def test_slot_bundle_failures_are_atomic(
    skill_world, monkeypatch, fault, reason, rework
):
    world = skill_world
    task_id = await _after_cr(world, "Доработка") if rework else await _task(world)
    before = await _footprint(world, task_id)

    if fault == "read":

        async def unavailable(*args, **kwargs):
            raise aiosqlite.OperationalError("Не удалось прочитать БД")

        monkeypatch.setattr(skills, "fetchall", unavailable)
    elif fault == "missing":
        await world.db.execute(
            "UPDATE skills SET status = 'draft' WHERE name = ?", ("hub-operator",)
        )
    else:
        content = {
            "blank": " \t\r\n ",
            "placeholder": "{{PAIR_CODE}}",
            "size": "э" * 65536,
        }[fault]
        await _publish(world.db, "hub-operator", content)

    if not world.db.in_transaction:
        await world.db.execute("BEGIN IMMEDIATE")
    launch = await _go(world.db, world, task_id)
    assert not launch.launched
    assert launch.reason.startswith(f"executor_skill_bundle_{reason}:")
    assert world.db.in_transaction
    await world.db.commit()
    assert await _footprint(world, task_id) == before
    assert (await _orders(world)).json()["orders"] == []
    assert (
        await _rows(
            world.db,
            "SELECT id FROM chat_pair_sessions WHERE bound_task_id = ?",
            (task_id,),
        )
        == []
    )


async def test_implementer_uses_embedded_bundle_without_library_access(skill_world):
    world = skill_world
    task_id = await _task(world)
    lived = await _launch_redeem(world, task_id)
    text = lived["order"]["order_text"]
    for name in SKILLS:
        assert f"Инструкция для {name}" in text
    brief = await world.client.get(
        f"/api/tasks/{task_id}/review-brief", headers=lived["session"]
    )
    assert brief.status_code == 200
    library = await world.client.get(
        "/api/skills/hub-operator", headers=lived["session"]
    )
    assert library.status_code == 403


async def test_bundle_keeps_other_launch_paths_unchanged(skill_world):
    world = skill_world
    task_id = await _after_cr(world, "Доработка после ревью")
    lived = await _launch_redeem(world, task_id)
    order = lived["order"]
    text = order["order_text"]
    assert text.startswith("ПЕРВЫЙ ШАГ")
    assert text.index("/api/auth/chat-pair/redeem") < text.index(
        "HAIPLANE_SKILL_BUNDLE_BEGIN"
    )
    assert text.count("{{PAIR_CODE}}") == 1
    assert lived["code"] not in text
    filled = _reference().fill_order_text(text, lived["code"])
    assert filled == text.replace("{{PAIR_CODE}}", lived["code"], 1)
    assert order["generation"] == 2
    assert (await repo.get_task(world.db, task_id))["status"] == "open"

    cloud = executor_launch._prompt(
        {"id": task_id, "title": "Продолжение"},
        "develop",
        "CODE",
        "https://example.test",
        "Инструкция для executor-pair-discipline",
    )
    assert cloud.startswith("ПЕРВЫЙ ШАГ")
    assert "Инструкция для executor-pair-discipline" in cloud
    assert "HAIPLANE_SKILL_BUNDLE_BEGIN" not in cloud
