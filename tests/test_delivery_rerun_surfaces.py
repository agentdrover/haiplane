"""История перезапуска CI видна на каждой поверхности и не человеческая (#1956)."""

from __future__ import annotations

import argparse
import json
from unittest.mock import AsyncMock, patch

from hub import cli, mcp_server
from hub.services import owner_minutes
from tests.delivery_rerun_support import RUN, rig_after_base_update


async def test_rerun_history_is_on_every_surface_and_not_human(
    db, client, monkeypatch, capsys
):
    rig = await rig_after_base_update(db, monkeypatch)
    before_minutes = await owner_minutes.task_estimate(db, rig.task_id)
    human_before = await db.execute_fetchall(
        "SELECT COUNT(*) FROM events WHERE task_id=? AND actor NOT IN "
        "('hub','policy','steward')",
        (rig.task_id,),
    )

    rig.red_first_attempt()
    await rig.sweep()
    assert rig.posts == 1

    rest = (await client.get(f"/api/tasks/{rig.task_id}")).json()
    view = rest["delivery_rerun"]
    assert view["state"] == "requested" and view["run_id"] == RUN
    assert view["attempt_before"] == 1 and view["history"]
    assert all(h["actor"] == "hub" for h in view["history"])

    page = await client.get(f"/tasks/{rig.task_id}")
    text = " ".join(page.text.split())
    assert "перезапуск" in text.lower() and RUN in text

    for full in (False, True):
        with patch.object(mcp_server, "_api_get", AsyncMock(return_value=rest)):
            out = await mcp_server.hub_task_status(rig.task_id, updates=0, full=full)
        assert out.structuredContent["task"]["delivery_rerun"] == view
        assert RUN in "\n".join(b.text for b in out.content)

    with patch.object(cli, "_api", return_value=rest):
        assert cli.cmd_status(argparse.Namespace(task_id=rig.task_id)) == 0
    shown = capsys.readouterr()
    assert RUN in shown.out + shown.err
    assert json.loads(json.dumps(view)) == view

    updates = rest.get("updates") or []
    assert not any(
        u.get("agent") not in ("hub", None, "")
        for u in updates
        if "перезапуск" in (u.get("content") or "").lower()
    )
    assert await owner_minutes.task_estimate(db, rig.task_id) == before_minutes
    human_after = await db.execute_fetchall(
        "SELECT COUNT(*) FROM events WHERE task_id=? AND actor NOT IN "
        "('hub','policy','steward')",
        (rig.task_id,),
    )
    assert dict(human_after[0]) == dict(human_before[0])
