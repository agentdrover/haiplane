"""The order-bound quota probe cannot issue or redeem an implementer code."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hub import brand, config
from hub.services.epic_plan_authority import WorkBinding
from tests import test_executor_orders as orders

world = orders.world
keys = orders.keys
_clean_limiter = orders._clean_limiter
_no_cloud = orders._no_cloud

BASE = "/api/executor-hosts/k8/orders"
PROBE_SWITCH = brand.ENV_PREFIX + "EPIC_PLAN_QUOTA_PROBE_ENABLED"


async def _feed(world, *, version: str | None = "1"):
    params = {} if version is None else {"admission_version": version}
    return await world.client.get(BASE, params=params, headers=world.k8_auth)


async def _phase(world, order_id, phase, *, version="1", auth=None, host="k8"):
    return await world.client.get(
        f"/api/executor-hosts/{host}/orders/{order_id}",
        params={"admission_version": version, "phase": phase},
        headers=world.k8_auth if auth is None else auth,
    )


def _binding(world):
    return WorkBinding(
        phase="executor",
        project="receipt-split-calculator",
        plan_version="e" * 64,
        task_id=2431,
        generation=1,
        sha="d" * 40,
        host="k8",
        model="probe-only",
        principal_id=world.human_id,
        actor_principal_id=world.human_id,
        attempt=1,
    )


@pytest.fixture
async def issued(world, monkeypatch):
    from hub.services import executor_quota_probe as probe

    monkeypatch.setenv(PROBE_SWITCH, "true")
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    kwargs = dict(
        binding=_binding(world), authority_ref="fixture-probe-ref-1", expires_at=expiry
    )
    result = await probe.issue(world.db, **kwargs)
    assert (await probe.issue(world.db, **kwargs)) == result
    return result


async def test_order_bound_quota_admission(world, monkeypatch):
    monkeypatch.delenv(PROBE_SWITCH, raising=False)
    ordinary = await _feed(world, version=None)
    assert ordinary.json() == {"host": "k8", "orders": []}

    explicit = await _feed(world)
    assert explicit.status_code == 200
    assert explicit.headers["Cache-Control"] == "no-store"
    assert explicit.json() == {"host": "k8", "orders": [], "probe_orders": []}
    assert (await _feed(world, version="2")).status_code == 422


async def test_probe_phases_bind_one_id_and_refuse_wrong_callers(world, issued):
    oid = issued["order_id"]
    assert (await _feed(world)).json()["probe_orders"] == [issued]
    assert (await _feed(world, version=None)).json() == {"host": "k8", "orders": []}
    assert (
        await _phase(world, oid, "before_redeem", auth=world.k9_auth)
    ).status_code == 403
    assert (
        await _phase(world, oid, "before_redeem", host="k9", auth=world.k9_auth)
    ).status_code == 404
    assert (
        await _phase(world, "probe-" + "a" * 32, "before_redeem")
    ).status_code == 404
    assert (await _phase(world, oid, "before_redeem", version="2")).status_code == 422
    premature = await _phase(world, oid, "before_exec")
    assert premature.json()["status"] == "wait"
    observed = await _phase(world, oid, "before_redeem")
    assert observed.headers["Cache-Control"] == "no-store"
    first = observed.json()
    second = (await _phase(world, oid, "before_exec")).json()
    assert first == (await _phase(world, oid, "before_redeem")).json()
    assert second == (await _phase(world, oid, "before_exec")).json()
    for response, phase in ((first, "before_redeem"), (second, "before_exec")):
        assert response["phase"] == phase and response["status"] == "ready"
        assert all(response[key] == value for key, value in issued.items())
        assert response["reset_at"] is None
    assert first["state_version"] <= second["state_version"]


async def test_probe_cannot_redeem_or_duplicate(world, issued):
    from hub.services import executor_quota_probe as probe

    oid = issued["order_id"]
    receipt_url = f"{BASE}/{oid}/probe-receipt"
    request = {
        "admission_version": 1,
        "authority_ref": issued["authority_ref"],
        "binding": issued["binding"],
        "checks": {"before_redeem": 1, "before_exec": 1},
    }

    async def record(payload, auth=None):
        return await world.client.post(
            receipt_url, json=payload, headers=world.k8_auth if auth is None else auth
        )

    assert (await record(request)).status_code == 409
    first = (await _phase(world, oid, "before_redeem")).json()
    second = (await _phase(world, oid, "before_exec")).json()
    request["checks"]["before_redeem"] = first["state_version"]
    request["checks"]["before_exec"] = second["state_version"]
    assert (await record(request, world.k9_auth)).status_code == 403
    assert (
        await record({**request, "authority_ref": "different-probe-ref"})
    ).status_code == 409
    saved, duplicate = await asyncio.gather(record(request), record(request))
    assert duplicate.json() == saved.json()
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "recorded" and saved.json()["order_id"] == oid
    await probe.revoke(world.db, oid)
    assert (await record(request)).json() == saved.json()
    assert (await _phase(world, oid, "before_exec")).json()["status"] == "revoked"
    assert (await _feed(world)).json()["probe_orders"] == []
    response = await world.client.post(
        f"{BASE}/{oid}/redeem", headers=world.k8_auth, json={"code": "not-a-code"}
    )
    assert response.status_code in (403, 404, 422)
    assert await orders._rows(world.db, "SELECT * FROM executor_orders") == []
    assert await orders._rows(world.db, "SELECT * FROM executor_runs") == []


async def test_probe_isolation_expiry_and_idempotent_revoke(world, issued):
    from hub.services import executor_quota_probe as probe
    from hub.services.executor_orders import HostError

    oid = issued["order_id"]
    live = await orders._task(world, "ordinary pending")
    launch = await orders._launch(world, live)
    assert launch.status_code == 200, launch.text
    pending = await orders._rows(world.db, "SELECT * FROM executor_orders")
    assert len(pending) == 1
    assert (await _feed(world)).json()["orders"] == []
    assert await orders._rows(world.db, "SELECT * FROM executor_orders") == pending
    assert len((await _feed(world, version=None)).json()["orders"]) == 1

    with pytest.raises(HostError) as exc:
        await probe.issue(
            world.db,
            binding=_binding(world),
            authority_ref="another-probe-ref",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    assert exc.value.status == 409

    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(
        timespec="microseconds"
    )
    await world.db.execute("UPDATE executor_quota_probe SET expires_at=?", (expired,))
    await world.db.commit()
    assert (await _feed(world)).json()["probe_orders"] == []
    assert (await _phase(world, oid, "before_redeem")).json()[
        "reason"
    ] == "probe_expired"

    for _ in range(2):
        await probe.revoke(world.db, oid)
    rows = await orders._rows(world.db, "SELECT * FROM executor_quota_probe")
    assert len(rows) == 1 and rows[0]["revoked_at"] is not None
    assert not any(
        rows[0][key] for key in ("before_redeem_at", "before_exec_at", "recorded_at")
    )


async def test_probe_operator_is_local_and_refuses_nonprivate_plan(
    world, issued, tmp_path, monkeypatch
):
    from hub.cli import build_parser
    from hub.cli_executor_quota_probe import _apply

    monkeypatch.setattr(config, "HUB_DB_PATH", Path(world.dsn))
    parsed = build_parser().parse_args(
        ["executor-quota-probe", "revoke", issued["order_id"]]
    )
    assert await _apply(parsed) == {"order_id": issued["order_id"], "status": "revoked"}
    plan = tmp_path / "plan.json"
    plan.write_text("{}")
    plan.chmod(0o644)
    parsed = build_parser().parse_args(
        ["executor-quota-probe", "issue", "--plan", str(plan)]
    )
    with pytest.raises(ValueError, match="private plan"):
        await _apply(parsed)
