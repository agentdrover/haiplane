"""Допуск хоста к роли: граница курсора, отзыв, аудит, устаревшая попытка (#1973, круг 1).

Четыре замечания владельца: событие между выкатом и первым свипом не теряется
(границу ставит миграция); допуск перечитывается при старте и отзывается
аудируемым маршрутом; аудит пишется и без принципала в БД; устаревшая попытка
после перевыдачи получает ровно 409.
"""

from __future__ import annotations

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.services import role_orders
from tests import role_orders_support as _support
from tests.role_orders_support import (
    admit,
    age,
    enter_needs_decision,
    leased,
    order_row,
    order_rows,
    poll,
    report,
    rows,
    started,
    sweep,
    task_in_project,
)

world = _support.world
keys = _support.keys
_clean_limiter = _support._clean_limiter
_no_cloud = _support._no_cloud

REVOKE = "/api/admin/executor-hosts/{host}/role-admissions/revoke"
ADMIT = "/api/admin/executor-hosts/{host}/role-admissions"


async def _principal(world, host: str) -> int:
    got = await rows(
        world.db, "SELECT principal_id FROM executor_hosts WHERE name = ?", (host,)
    )
    return int(got[0]["principal_id"])


async def _revoke(world, host: str = "k8", auth=None):
    return await world.client.post(
        REVOKE.format(host=host),
        json={"role": "roadmap", "project": world.slug},
        headers=auth if auth is not None else world.human_auth,
    )


async def _lease_one(world, host: str = "k8") -> dict:
    task_id = await task_in_project(world, "заказ")
    await enter_needs_decision(world, task_id)
    assert await sweep(world) == 1
    return await leased(world, host)


async def test_an_event_before_the_first_sweep_is_ordered(world):
    """Граница курсора - миграция: событие после неё, до первого свипа, даёт заказ."""
    boundary = await rows(world.db, "SELECT cursor_event_id FROM role_orders_cursor")
    assert len(boundary) == 1, "миграция обязана завести строку курсора"
    task_id = await task_in_project(world, "до первого свипа")
    event = await enter_needs_decision(world, task_id)
    assert event > int(boundary[0]["cursor_event_id"])
    assert await sweep(world) == 1
    assert (await order_rows(world))[0]["source_key"] == f"needs_decision:{event}"


async def test_revoking_returns_a_leased_order_to_open_without_cost(world):
    """Отзыв допуска: не начатый lease возвращается в open без траты перевыдачи; аудит есть."""
    await admit(world, "k8")
    await admit(world, "k9")
    order = await _lease_one(world, "k8")
    gone = await _revoke(world, "k8")
    assert gone.status_code == 200, gone.text
    row = await order_row(world, order["id"])
    assert row["state"] == "open" and row["attempt"] == 1 and row["host"] is None
    audit = await rows(
        world.db, "SELECT * FROM admin_audit_log WHERE action = 'role_host_revoked'"
    )
    assert len(audit) == 1 and audit[0]["target_id"] == "k8"
    late = await started(world, "k8", order["id"], order["lease_id"])
    assert late.status_code == 409
    assert (await poll(world, "k8")).json()["orders"] == []
    fresh = await leased(world, "k9")
    assert fresh["id"] == order["id"] and fresh["attempt"] == 1


async def test_started_rereads_the_admission_inside_the_transaction(world):
    """Допуск снят прямо в базе, lease ещё жив: старт отказан, lease вернулся в open."""
    await admit(world, "k8")
    order = await _lease_one(world, "k8")
    await world.db.execute("DELETE FROM role_host_admissions WHERE host = 'k8'")
    await world.db.commit()
    refused = await started(world, "k8", order["id"], order["lease_id"])
    assert refused.status_code == 409
    row = await order_row(world, order["id"])
    assert row["state"] == "open" and row["attempt"] == 1 and row["host"] is None
    # Повторный опрос без допуска тоже ничего не возвращает.
    assert (await poll(world, "k8")).json()["orders"] == []


async def test_a_current_lease_poll_rechecks_the_admission(world):
    await admit(world, "k8")
    order = await _lease_one(world, "k8")
    await world.db.execute("DELETE FROM role_host_admissions WHERE host = 'k8'")
    await world.db.commit()
    got = await role_orders.lease(world.db, "k8", await _principal(world, "k8"))
    await world.db.commit()
    assert got is None
    assert (await order_row(world, order["id"]))["state"] == "open"


async def test_a_started_order_finishes_after_the_revocation(world):
    """Уже начатый заказ дорабатывает; перевыдачи нет, отчёт принимается."""
    await admit(world, "k8")
    order = await _lease_one(world, "k8")
    assert (
        await started(world, "k8", order["id"], order["lease_id"])
    ).status_code == 200
    assert (await _revoke(world, "k8")).status_code == 200
    await age(world, order["id"], lease_deadline="-1 hour")
    await role_orders.run_sweep(world.db)
    await world.db.commit()
    row = await order_row(world, order["id"])
    assert row["state"] == "started" and row["attempt"] == 1
    done = await report(world, "k8", order["id"], order["lease_id"])
    assert done.status_code == 200, done.text
    assert (await order_row(world, order["id"]))["state"] == "done"


async def test_revoke_is_human_only_and_validated(world):
    await admit(world, "k8")
    for auth in (world.agent_auth, world.k8_auth):
        assert (await _revoke(world, "k8", auth)).status_code == 403
    url = REVOKE.format(host="k8")
    bad_role = await world.client.post(
        url,
        json={"role": "implementer", "project": world.slug},
        headers=world.human_auth,
    )
    assert bad_role.status_code == 422
    no_project = await world.client.post(
        url, json={"role": "roadmap", "project": "нет-такого"}, headers=world.human_auth
    )
    assert no_project.status_code == 404
    none_to_revoke = await _revoke(world, "k9")
    assert none_to_revoke.status_code == 404  # допуска у k9 нет - это не успех


async def test_admission_changes_are_audited_without_a_db_principal(world, monkeypatch):
    """Bearer из HUB_TOKENS (principal_id=None): изменение допуска всё равно в аудите."""
    monkeypatch.setitem(
        config.HUB_TOKENS, "env-owner-token", TokenIdentity("env-owner", "human")
    )
    auth = {"Authorization": "Bearer env-owner-token"}
    body = {"role": "roadmap", "project": world.slug}
    admitted = await world.client.post(ADMIT.format(host="k8"), json=body, headers=auth)
    assert admitted.status_code == 200, admitted.text
    assert (await _revoke(world, "k8", auth)).status_code == 200
    audit = await rows(
        world.db,
        "SELECT * FROM admin_audit_log WHERE action IN "
        "('role_host_admitted', 'role_host_revoked') ORDER BY id",
    )
    assert [a["action"] for a in audit] == ["role_host_admitted", "role_host_revoked"]
    assert all(a["actor_principal_id"] is None for a in audit)
    assert all("env-owner" in f"{a['summary']} {a['detail']}" for a in audit)


async def test_a_stale_attempt_gets_exactly_409(world):
    """Заказ перевыдан другому хосту: старые started и report прежнего хоста - ровно 409."""
    await admit(world, "k8")
    await admit(world, "k9")
    old = await _lease_one(world, "k8")
    await age(world, old["id"], lease_deadline="-1 minute")
    await role_orders.run_sweep(world.db)
    await world.db.commit()
    new = await leased(world, "k9")
    assert new["id"] == old["id"] and new["lease_id"] != old["lease_id"]
    assert (await started(world, "k9", new["id"], new["lease_id"])).status_code == 200
    assert (await started(world, "k8", old["id"], old["lease_id"])).status_code == 409
    assert (await report(world, "k8", old["id"], old["lease_id"])).status_code == 409
    assert (await order_row(world, old["id"]))["state"] == "started"
    # Несуществующий заказ по-прежнему 404, а не 409.
    assert (await started(world, "k8", 999999, "x")).status_code == 404
    assert repo is not None


async def test_a_stranger_gets_404_and_a_former_holder_409(world):
    """Codex P2: чужой хост без прежней попытки не узнаёт о заказе - 404; прежний держатель - 409."""
    await admit(world, "k8")
    await admit(world, "k9")
    order = await _lease_one(world, "k8")
    # Заказ у k8 в lease: k9 с произвольным lease_id - как будто заказа нет.
    assert (await started(world, "k9", order["id"], "guess")).status_code == 404
    assert (await report(world, "k9", order["id"], "guess")).status_code == 404
    # Заказ вернулся в open (host пуст): посторонний k9 всё равно 404, прежний k8 - 409.
    await age(world, order["id"], lease_deadline="-1 minute")
    await role_orders.run_sweep(world.db)
    await world.db.commit()
    assert (await order_row(world, order["id"]))["state"] == "open"
    assert (
        await started(world, "k9", order["id"], order["lease_id"])
    ).status_code == 404
    assert (await report(world, "k9", order["id"], "guess")).status_code == 404
    assert (
        await started(world, "k8", order["id"], order["lease_id"])
    ).status_code == 409
    assert (
        await report(world, "k8", order["id"], order["lease_id"])
    ).status_code == 409


async def test_an_expired_lease_without_admission_costs_no_reissue(world):
    """Codex P2: допуск удалён, lease истёк - open с прежним attempt, не перевыдача и не lease_exhausted."""
    await admit(world, "k8")
    order = await _lease_one(world, "k8")
    await world.db.execute("DELETE FROM role_host_admissions WHERE host = 'k8'")
    await age(world, order["id"], lease_deadline="-1 minute")
    await role_orders.run_sweep(world.db)
    await world.db.commit()
    row = await order_row(world, order["id"])
    assert row["state"] == "open" and row["attempt"] == 1
    # На последней попытке допуск тоже не превращает истечение в failed.
    await admit(world, "k8")
    again = await leased(world, "k8")
    await world.db.execute(
        "UPDATE role_orders SET attempt = ? WHERE id = ?",
        (role_orders.MAX_REISSUES + 1, order["id"]),
    )
    await world.db.execute("DELETE FROM role_host_admissions WHERE host = 'k8'")
    await age(world, order["id"], lease_deadline="-1 minute")
    await role_orders.run_sweep(world.db)
    await world.db.commit()
    last = await order_row(world, order["id"])
    assert again["id"] == order["id"]
    assert last["state"] == "open" and last["attempt"] == role_orders.MAX_REISSUES + 1


async def test_the_report_note_passes_the_shared_untrusted_quote_sieve(world):
    """Находка 7c16ad71: note роли проходит то же сито, что выжимка критика (#1953)."""
    await admit(world, "k8")
    order = await _lease_one(world, "k8")
    await started(world, "k8", order["id"], order["lease_id"])
    note = "ok <script>x</script> ‮evil `code` [link](http://x)"
    done = await report(world, "k8", order["id"], order["lease_id"], note=note)
    assert done.status_code == 200, done.text
    stored = (await order_row(world, order["id"]))["note"]
    assert "<script>" not in stored and "‮" not in stored and "`" not in stored
    from hub.services.untrusted_quote import sanitize_untrusted_quote

    assert stored == sanitize_untrusted_quote(note, out_limit=role_orders.NOTE_MAX)
