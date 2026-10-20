"""Критик k8 для облачного исполнителя (#1953): заказ по строке ленты, отчёт хоста, гейт сдачи.

AC-1: строка «нужен критик: sha …» от облачной сессии даёт ровно один заказ; все отказы - alert.
AC-2: хост забирает заказ и сообщает итог своим ключом; выжимка санитизируется.
AC-3: сдача облачной задачи принимается только при passed на наблюдённом sha.
AC-4: миграция применяется один раз; замороженные части не тронуты.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

from hub import config
from hub import repository as repo
from hub.db import _migrate
from hub.services import chat_pair as cp
from hub.services import lifecycle
from tests import test_executor_orders as _eo
from tests.test_db_migrations import _make_db

world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud
_task = _eo._task
_rows = _eo._rows

ROOT = Path(__file__).resolve().parent.parent
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
VECTORS = json.loads(
    (ROOT / "tests/fixtures/untrusted_quote_vectors.json").read_text(encoding="utf-8")
)["vectors"]


def _line(sha: str) -> str:
    return f"нужен критик: sha {sha}"


@pytest.fixture
def tip(monkeypatch):
    """Единственное наблюдение головы: значение, сбой и счётчик вызовов."""
    state = SimpleNamespace(sha=SHA_A, fail=False, calls=[])

    async def _observe(db, task_id, branch):
        state.calls.append(task_id)
        if state.fail:
            return "", "tip resolution failed: boom"
        return state.sha, ""

    monkeypatch.setattr(lifecycle, "resolve_branch_tip", _observe)
    return state


async def _policy(world, **policy) -> None:
    await repo.update_project(
        world.db,
        world.pid,
        gate_policy=json.dumps({"executor_launch": "manual", **policy}),
    )
    await world.db.commit()


async def _session(world, task_id: int, run_id: int) -> dict:
    token = cp.generate_session_token()
    acting = await cp.get_acting_agent(world.db)
    await world.db.execute(
        "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at, kind, "
        "bound_task_id, bound_generation, executor_run_id, channel, slot, "
        "acting_principal_id, first_used_at) VALUES "
        "(?, ?, datetime('now', '+1 hour'), 'implementer', ?, 1, ?, ?, '', ?, "
        "datetime('now'))",
        (
            world.human_id,
            cp.hash_pair_code(token),
            task_id,
            run_id,
            "cloud",
            int(acting["id"]),
        ),
    )
    await world.db.commit()
    return {"Authorization": f"Bearer {token}"}


async def _cloud(world, tip, *, critic_host: str = "k8", auto_review: bool = True):
    """Облачная задача в running: прогон channel=cloud, держатель и живая сессия."""
    await _policy(world, critic_host=critic_host) if critic_host else await _policy(world)
    task_id = await _task(world, f"облако {secrets.token_hex(3)}")
    run_id = await repo.create_executor_run(
        world.db,
        task_id=task_id,
        submission_generation=0,
        agent_id="cursor",
        run_id=f"bc-{secrets.token_hex(4)}",
        model="m",
        channel="cloud",
    )
    await repo.update_task(
        world.db,
        task_id,
        status="running",
        claimed_by=config.CHAT_PAIR_AGENT,
        assigned_agent=config.CHAT_PAIR_AGENT,
        executor_run_id=run_id,
        auto_review=1 if auto_review else 0,
    )
    await world.db.commit()
    return SimpleNamespace(
        task_id=task_id, run_id=run_id, auth=await _session(world, task_id, run_id)
    )


async def _say(world, cloud, content: str, kind: str = "status", auth=None):
    return await world.client.post(
        f"/api/tasks/{cloud.task_id}/updates",
        json={"agent": "cloud", "kind": kind, "content": content},
        headers=auth or cloud.auth,
    )


async def _orders(world, task_id: int | None = None) -> list[dict]:
    await world.db.commit()
    sql = "SELECT * FROM critic_orders"
    try:
        if task_id is None:
            return await _rows(world.db, sql + " ORDER BY id")
        return await _rows(world.db, sql + " WHERE task_id = ? ORDER BY id", (task_id,))
    except sqlite3.OperationalError:  # до миграции таблицы нет: заказов нет
        return []


async def _alerts(world, task_id: int) -> list[str]:
    await world.db.commit()
    rows = await _rows(
        world.db,
        "SELECT content FROM task_updates WHERE task_id = ? AND kind = 'alert' "
        "ORDER BY id",
        (task_id,),
    )
    return [r["content"] for r in rows]


async def _executor_state(world) -> tuple:
    await world.db.commit()
    runs = await _rows(world.db, "SELECT * FROM executor_runs ORDER BY id")
    eorders = await _rows(world.db, "SELECT * FROM executor_orders ORDER BY id")
    return (runs, eorders)


async def _get_orders(world, host="k8", wait=0, auth=None):
    return await world.client.get(
        f"/api/executor-hosts/{host}/critic-orders?wait={wait}",
        headers=auth or getattr(world, f"{host}_auth"),
    )


async def _report(world, oid: int, body: dict, host="k8", auth=None):
    return await world.client.post(
        f"/api/executor-hosts/{host}/critic-orders/{oid}/report",
        json=body,
        headers=auth or getattr(world, f"{host}_auth"),
    )


def _passed(**extra) -> dict:
    return {
        "outcome": "passed",
        "findings_p1": 0,
        "findings_p2": 0,
        "summary": "чисто",
        "host_ref": "vm-1",
        **extra,
    }


async def _closed_order(world, cloud, sha: str, outcome: str, p1=0, p2=0) -> int:
    """Закрытый заказ напрямую в таблице - для гейта, когда путь заказа не предмет."""
    await world.db.execute(
        "INSERT INTO critic_orders (task_id, executor_run_id, sha, repo, branch, base, "
        "host, state, expires_at, outcome, findings_p1, findings_p2, summary, "
        "reported_at, closed_reason) VALUES (?, ?, ?, 'o/r', 'b', 'develop', 'k8', "
        "'closed', datetime('now', '+30 minutes'), ?, ?, ?, 's', datetime('now'), "
        "'reported')",
        (cloud.task_id, cloud.run_id, sha, outcome, p1, p2),
    )
    await world.db.commit()
    return (await _orders(world, cloud.task_id))[-1]["id"]


# ---------------------------------------------------------------------------
# AC-1
# ---------------------------------------------------------------------------


async def test_a_cloud_critic_request_orders_one_critic(world, tip, monkeypatch):
    """AC-1: один заказ у облачного держателя; все отказы - alert с причиной и без сети."""
    cloud = await _cloud(world, tip)
    before = await _executor_state(world)

    # Цитата и code block - не заказ; сети нет.
    for body in (f"> {_line(SHA_A)}", f"```\n{_line(SHA_A)}\n```", f"см. {_line(SHA_A)} выше"):
        assert (await _say(world, cloud, body)).status_code == 200
    assert await _orders(world) == [] and tip.calls == []
    assert any("request_line" in a for a in await _alerts(world, cloud.task_id))

    # Чужая сессия (не облачная: ручной прогон) и ручной перехват.
    manual = await _cloud(world, tip)
    await world.db.execute("UPDATE executor_runs SET channel='slot' WHERE id=?", (manual.run_id,))
    await world.db.commit()
    assert (await _say(world, manual, _line(SHA_A))).status_code == 200
    assert await _orders(world) == [] and tip.calls == []
    assert any("not_cloud_holder" in a for a in await _alerts(world, manual.task_id))
    taken = await _cloud(world, tip)
    await repo.update_task(world.db, taken.task_id, claimed_by=None)  # перехват снимает прогон
    await world.db.commit()
    assert (await _say(world, taken, _line(SHA_A))).status_code == 200
    assert await _orders(world) == [] and tip.calls == []
    # Имя agent доказательством не служит: человеческий ключ без сессии - не облако.
    assert (await _say(world, cloud, _line(SHA_A), auth=world.human_auth)).status_code == 200
    assert await _orders(world) == [] and tip.calls == []

    # critic_host пуст: заказа нет, причина названа.
    off = await _cloud(world, tip, critic_host="")
    assert (await _say(world, off, _line(SHA_A))).status_code == 200
    assert await _orders(world) == [] and tip.calls == []
    assert any("policy_off" in a for a in await _alerts(world, off.task_id))
    await _policy(world, critic_host="k8")

    # Голова не sha строки: заказа нет, сеть - один раз и только после остальных проверок.
    assert (await _say(world, cloud, _line(SHA_B))).status_code == 200
    assert await _orders(world) == [] and len(tip.calls) == 1
    assert any("head_moved" in a for a in await _alerts(world, cloud.task_id))
    tip.fail = True
    assert (await _say(world, cloud, _line(SHA_A))).status_code == 200
    assert await _orders(world) == []
    assert any("head_unobserved" in a for a in await _alerts(world, cloud.task_id))
    tip.fail = False

    # Верная строка: ровно один заказ с неизменяемым контекстом.
    tip.calls.clear()
    assert (await _say(world, cloud, _line(SHA_A))).status_code == 200
    (order,) = await _orders(world, cloud.task_id)
    assert (order["sha"], order["state"], order["host"]) == (SHA_A, "pending", "k8")
    assert order["executor_run_id"] == cloud.run_id and order["branch"].startswith("task-")
    assert order["repo"] and order["base"] and order["expires_at"]
    assert len(tip.calls) == 1

    # Повтор той же строки, заказ на другой sha при открытом: заказа нет.
    tip.calls.clear()
    assert (await _say(world, cloud, _line(SHA_A))).status_code == 200
    tip.sha = SHA_B
    assert (await _say(world, cloud, _line(SHA_B))).status_code == 200
    assert len(await _orders(world, cloud.task_id)) == 1 and tip.calls == []
    texts = await _alerts(world, cloud.task_id)
    assert any("duplicate" in a for a in texts) and any("quota_open" in a for a in texts)

    # Квота в час: после закрытия failed лимит 1 в час не даёт второго заказа.
    monkeypatch.setattr(config, "CRITIC_ORDERS_PER_HOUR", 1, raising=False)
    await world.db.execute(
        "UPDATE critic_orders SET state='closed', outcome='failed', closed_reason='reported' "
        "WHERE id=?", (order["id"],)
    )
    await world.db.commit()
    tip.sha, tip.calls[:] = SHA_A, []
    assert (await _say(world, cloud, _line(SHA_A))).status_code == 200
    assert len(await _orders(world, cloud.task_id)) == 1 and tip.calls == []
    assert any("quota_hourly" in a for a in await _alerts(world, cloud.task_id))

    # Повтор неизменного кода после failed допустим, когда квота позволяет.
    monkeypatch.setattr(config, "CRITIC_ORDERS_PER_HOUR", 10, raising=False)
    assert (await _say(world, cloud, _line(SHA_A))).status_code == 200
    assert [o["state"] for o in await _orders(world, cloud.task_id)] == ["closed", "pending"]

    # Заказы исполнителя и прогоны не тронуты ни одной веткой.
    runs_now, eorders_now = await _executor_state(world)
    assert eorders_now == before[1]
    assert [r["id"] for r in runs_now if r["id"] in {cloud.run_id}] == [cloud.run_id]


# ---------------------------------------------------------------------------
# AC-2
# ---------------------------------------------------------------------------


async def test_only_the_host_reports_and_the_quote_is_sanitized(world, tip):
    """AC-2: заказ выдаётся хосту, отчёт принят один раз, цитата санитизирована."""
    cloud = await _cloud(world, tip)
    assert (await _say(world, cloud, _line(SHA_A))).status_code == 200
    (order,) = await _orders(world, cloud.task_id)
    oid = order["id"]

    # Чужой хост, исполнитель, облачная сессия: не открывают ни выдачу, ни отчёт.
    for auth in (world.k9_auth, world.agent_auth, world.human_auth, cloud.auth):
        got = await _get_orders(world, auth=auth)
        assert got.status_code in (403, 404), got.text
        sent = await _report(world, oid, _passed(), auth=auth)
        assert sent.status_code in (403, 404), sent.text
    foreign = await _report(world, oid, _passed(), host="k9")
    assert foreign.status_code in (403, 404), foreign.text
    assert (await _orders(world, cloud.task_id))[0]["state"] != "closed"

    # Хост получает заказ; delivered выдаётся повторно до закрытия.
    first = (await _get_orders(world)).json()["orders"]
    again = (await _get_orders(world)).json()["orders"]
    assert [o["id"] for o in first] == [o["id"] for o in again] == [oid]
    keys_ = {"id", "task_id", "sha", "repo", "branch", "base", "state", "expires_at"}
    assert set(first[0]) == keys_ and first[0]["sha"] == SHA_A
    assert (await _orders(world, cloud.task_id))[0]["state"] == "delivered"
    assert (await _get_orders(world, host="k9")).json()["orders"] == []

    # Отчёт: passed при P1>0 - 422; плохой host_ref и слишком длинная выжимка - 422.
    assert (await _report(world, oid, _passed(findings_p1=1))).status_code == 422
    assert (await _report(world, oid, _passed(host_ref="a b\n"))).status_code == 422
    assert (await _report(world, oid, _passed(summary="x" * 20000))).status_code == 422
    assert (await _orders(world, cloud.task_id))[0]["state"] == "delivered"

    # Санитизация: выжимка с опасным содержимым; в ленте одна запись, векторы вычищены.
    nasty = " ".join(v["text"] for v in VECTORS)
    feed_before = len(await _rows(world.db, "SELECT id FROM task_updates WHERE task_id=?", (cloud.task_id,)))
    ok = await _report(world, oid, _passed(summary=nasty))
    assert ok.status_code == 200, ok.text
    stored = (await _orders(world, cloud.task_id))[0]
    assert (stored["state"], stored["outcome"], stored["host_ref"]) == ("closed", "passed", "vm-1")
    rows = await _rows(
        world.db,
        "SELECT content FROM task_updates WHERE task_id=? AND author_kind='hub' ORDER BY id DESC",
        (cloud.task_id,),
    )
    feed_after = len(await _rows(world.db, "SELECT id FROM task_updates WHERE task_id=?", (cloud.task_id,)))
    assert feed_after == feed_before + 1
    for text in (stored["summary"], rows[0]["content"]):
        for vector in VECTORS:
            for code in vector["forbidden_chars"]:
                assert chr(code) not in text, (vector["name"], code)
            for part in vector["forbidden_text"]:
                assert part not in text, (vector["name"], part)
        assert not any(
            unicodedata.category(ch) in {"Cc", "Cf"} and ch not in "\n" for ch in text
        )
        assert len(stored["summary"]) <= 1000

    # Идентичный повтор - no-op, изменённый - 409; ленты не прибавилось.
    assert (await _report(world, oid, _passed(summary=nasty))).status_code == 200
    assert (await _report(world, oid, _passed(summary="иное"))).status_code == 409
    assert (await _report(world, oid, _passed(host_ref="vm-2"))).status_code == 409
    assert len(await _rows(world.db, "SELECT id FROM task_updates WHERE task_id=?", (cloud.task_id,))) == feed_after
    assert (await _get_orders(world)).json()["orders"] == []  # закрытый не выдаётся

    # Поздний отчёт: срок вышел - отказ, заказ expired (его ставит только хаб).
    tip.sha = SHA_B
    assert (await _say(world, cloud, _line(SHA_B))).status_code == 200
    late = (await _orders(world, cloud.task_id))[-1]
    await world.db.execute(
        "UPDATE critic_orders SET expires_at = datetime('now', '-1 minute') WHERE id=?",
        (late["id"],),
    )
    await world.db.commit()
    refused = await _report(world, late["id"], _passed())
    assert refused.status_code == 409, refused.text
    after = (await _orders(world, cloud.task_id))[-1]
    assert (after["state"], after["outcome"]) == ("closed", "expired")
    assert after["closed_reason"] and after["findings_p1"] == 0
    # Отчёт раньше срока не отменяется свипом: passed остаётся passed.
    tip.sha = SHA_C
    assert (await _say(world, cloud, _line(SHA_C))).status_code == 200
    fresh = (await _orders(world, cloud.task_id))[-1]
    assert (await _report(world, fresh["id"], _passed())).status_code == 200
    from hub.services import critic_orders

    await critic_orders.expire_due(world.db)
    await world.db.commit()
    assert (await _orders(world, cloud.task_id))[-1]["outcome"] == "passed"
    # Незнакомый заказ того же хоста - 404.
    assert (await _report(world, 99999, _passed())).status_code == 404


# ---------------------------------------------------------------------------
# AC-3
# ---------------------------------------------------------------------------


async def _task_state(world, task_id: int) -> tuple:
    await world.db.commit()
    task = (await _rows(world.db, "SELECT * FROM tasks WHERE id=?", (task_id,)))[0]
    feed = await _rows(world.db, "SELECT id FROM task_updates WHERE task_id=?", (task_id,))
    return (task["status"], task["submission_generation"], task["submission_sha"], len(feed))


async def _submit(world, cloud, **extra):
    return await world.client.post(
        f"/api/tasks/{cloud.task_id}/submit-review",
        json={"agent": config.CHAT_PAIR_AGENT, "summary": "s", **extra},
        headers=cloud.auth,
    )


async def _done(world, cloud, auth=None):
    return await world.client.post(
        f"/api/tasks/{cloud.task_id}/updates",
        json={"agent": config.CHAT_PAIR_AGENT, "kind": "done", "content": "готово"},
        headers=auth or cloud.auth,
    )


def _refused(resp) -> str:
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "critic_not_passed", detail
    return detail["cause"]


async def test_a_cloud_submission_needs_a_passed_critic_on_its_sha(world, tip):
    """AC-3: сдача облачной задачи - только при passed на наблюдённом sha; иначе 422 без записей."""
    for gate in ("submit", "done", "done_no_review"):
        cloud = await _cloud(world, tip, auto_review=gate != "done_no_review")
        send = (lambda c: _submit(world, c)) if gate == "submit" else (lambda c: _done(world, c))
        tip.sha, tip.fail = SHA_A, False

        # Заказа нет / открытый / findings / failed / expired: отказ и ни одной записи.
        before = await _task_state(world, cloud.task_id)
        assert _refused(await send(cloud)) == "no_order"
        assert await _task_state(world, cloud.task_id) == before
        await world.db.execute(
            "INSERT INTO critic_orders (task_id, executor_run_id, sha, repo, branch, base, "
            "host, state, expires_at) VALUES (?, ?, ?, 'o/r', 'b', 'develop', 'k8', "
            "'pending', datetime('now', '+30 minutes'))", (cloud.task_id, cloud.run_id, SHA_A),
        )
        await world.db.commit()
        assert _refused(await send(cloud)) == "order_open"
        await world.db.execute("DELETE FROM critic_orders WHERE task_id=?", (cloud.task_id,))
        await world.db.commit()
        for outcome, cause, p1 in (("findings", "findings", 1), ("failed", "failed", 0), ("expired", "expired", 0)):
            oid = await _closed_order(world, cloud, SHA_A, outcome, p1=p1)
            assert _refused(await send(cloud)) == cause, outcome
            await world.db.execute("DELETE FROM critic_orders WHERE id=?", (oid,))
            await world.db.commit()
        assert await _task_state(world, cloud.task_id) == before

        # passed(A) при голове B: нужен passed(B).
        await _closed_order(world, cloud, SHA_A, "passed")
        tip.sha = SHA_B
        assert _refused(await send(cloud)) == "no_order"
        # Наблюдение головы упало: блок, а не пропуск.
        tip.fail = True
        assert _refused(await send(cloud)) == "head_unobserved"
        assert await _task_state(world, cloud.task_id) == before
        tip.fail = False

        # passed на ТОМ ЖЕ sha, что наблюдён и будет закреплён: принято.
        tip.sha = SHA_A
        ok = await send(cloud)
        assert ok.status_code == 200, (gate, ok.text)
        status, generation, pinned, _feed = await _task_state(world, cloud.task_id)
        if gate == "submit":
            assert (status, generation, pinned) == ("review", 1, SHA_A)
        elif gate == "done":
            assert (status, generation) == ("review", 1)

    # Автоматический вход (поллер): удержание с alert, без исключения и без записи сдачи.
    from hub.services import orchestration

    auto = await _cloud(world, tip)
    row = dict((await _rows(world.db, "SELECT * FROM tasks WHERE id=?", (auto.task_id,)))[0])
    assert await orchestration.transition_after_agent_done(world.db, row, has_done=True) == "needs_decision"
    await world.db.commit()
    held = await _task_state(world, auto.task_id)
    assert held[0] == "needs_decision" and held[1] == 0
    assert any("critic_not_passed" in a for a in await _alerts(world, auto.task_id))

    # k8-, локальная сдача и выключенная политика: как до правки.
    for label in ("slot", "off", "no_run"):
        other = await _cloud(world, tip, critic_host="" if label == "off" else "k8")
        if label == "slot":
            await world.db.execute("UPDATE executor_runs SET channel='slot' WHERE id=?", (other.run_id,))
        if label == "no_run":
            await world.db.execute("UPDATE tasks SET executor_run_id=NULL WHERE id=?", (other.task_id,))
        await world.db.commit()
        ok = await _submit(world, other)
        assert ok.status_code == 200, (label, ok.text)
        assert (await _task_state(world, other.task_id))[0] == "review"
        assert await _orders(world, other.task_id) == []


# ---------------------------------------------------------------------------
# AC-4
# ---------------------------------------------------------------------------


async def test_the_migration_applies_once(monkeypatch):
    """AC-4: таблица с FK/CHECK/индексами на свежей базе и на базе с данными; повтор - no-op."""
    import hub.db as db_module
    from hub.migrations import FROZEN_MIGRATIONS
    from hub.migrations.steps import load_step_files

    steps = [s for s in load_step_files() if "_1953_" in s.name]
    assert len(steps) == 1 and steps[0].name.startswith("m")
    for part in ("part_1", "part_2", "part_3"):
        text = (ROOT / "hub" / "migrations" / f"{part}.py").read_text(encoding="utf-8")
        assert "critic_orders" not in text, part
    assert not [n for n, _ in FROZEN_MIGRATIONS if "critic" in n]

    async def _probe(conn):
        tables = await conn.execute_fetchall(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='critic_orders'"
        )
        assert len(tables) == 1 and "CHECK" in tables[0][0]
        fks = {r[2] for r in await conn.execute_fetchall("PRAGMA foreign_key_list(critic_orders)")}
        assert fks == {"tasks", "executor_runs"}
        idx = {r[1] for r in await conn.execute_fetchall("PRAGMA index_list(critic_orders)")}
        assert any(i.startswith("uq_critic_orders_open") for i in idx) and len(idx) >= 3

    # Свежая база.
    fresh = await _make_db()
    try:
        await _probe(fresh)
        applied = [tuple(r) for r in await fresh.execute_fetchall("SELECT name, applied_at FROM _migrations ORDER BY name")]
        executed: list[str] = []
        real = fresh.execute

        async def spy(sql, *a, **k):
            executed.append(sql)
            return await real(sql, *a, **k)

        fresh.execute = spy  # type: ignore[method-assign]
        await _migrate(fresh)
        fresh.execute = real  # type: ignore[method-assign]
        assert not [s for s in executed if "critic_orders" in s and s.lstrip().upper().startswith("CREATE")]
        assert [tuple(r) for r in await fresh.execute_fetchall("SELECT name, applied_at FROM _migrations ORDER BY name")] == applied
        # CHECK: passed с P1>0 и кривой sha отвергаются самой таблицей.
        await fresh.execute("INSERT INTO tasks (title, description) VALUES ('t', 'd')")
        await fresh.execute("INSERT INTO executor_runs (task_id) VALUES (1)")
        base = ("INSERT INTO critic_orders (task_id, executor_run_id, sha, repo, branch, base, host, "
                "state, expires_at, outcome, findings_p1) VALUES (1, 1, ?, 'r', 'b', 'd', 'k8', ?, "
                "datetime('now'), ?, ?)")
        for sha, state, outcome, p1 in ((SHA_A[:39], "pending", None, 0), (SHA_A, "bogus", None, 0), (SHA_A, "closed", "passed", 1)):
            with pytest.raises(Exception):
                await fresh.execute(base, (sha, state, outcome, p1))
        await fresh.rollback()
    finally:
        await fresh.close()

    # База на момент замороженных частей, с данными: шаг достраивает таблицу, данные целы.
    monkeypatch.setattr(db_module, "_MIGRATIONS", list(FROZEN_MIGRATIONS))
    old = await _make_db()
    try:
        await old.execute("INSERT INTO tasks (title, description) VALUES ('control', 'row')")
        await old.commit()
        before = [list(r) for r in await old.execute_fetchall("SELECT id, title, description FROM tasks")]
        assert not await old.execute_fetchall("SELECT 1 FROM sqlite_master WHERE name='critic_orders'")
        from hub.migrations import assemble

        monkeypatch.setattr(db_module, "_MIGRATIONS", assemble(FROZEN_MIGRATIONS))
        await _migrate(old)
        await _probe(old)
        assert [list(r) for r in await old.execute_fetchall("SELECT id, title, description FROM tasks")] == before
        await _migrate(old)
        await _probe(old)
    finally:
        await old.close()
