"""Accepting a task by hand must not lose its work in silence (#897).

21.08.2026, the run these tests are built from. Task #885's feed, verbatim:

    18:44:41  hub  alert     Done report NOT completed: PR #444 is not
                             delivered — ci_pending: workflow_runs_running.
                             Fix the cause and report done again, or decide
                             the task by hand.
    18:46:30  human decision  Human accepted task after arbiter review.

The refusal was right — the CI run really was still going. The acceptance was
right too: deciding by hand is the exit the refusal itself offers, and an owner
is sometimes cancelling work on purpose. Minutes later CI went green and the PR
was MERGEABLE and CLEAN, with nobody left to merge it: the gate delivers on a
done report, and a completed task never files another one. #878 and #885 sat
``completed`` for two hours with their code outside develop, and what found it
was a person comparing open PRs against the board by eye.

So these tests hold two things and one line between them. Manual acceptance is
never blocked — not in one test here does the decision fail. What changes is
that it stops being silent, and that the discrepancy is a list somebody can
read. And the line: "could not ask GitHub" stays its own answer, because a list
that cries wolf whenever the network blinks is a list that gets ignored, and
then it is quiet in the case that mattered.
"""

from __future__ import annotations

from typing import Any

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.registry import plugins
from hub.services.delivery_state import (
    DELIVERED,
    PR_CLOSED,
    PR_OPEN,
    UNKNOWN,
    scan_completed_deliveries,
    task_delivery,
    undelivered_completed_tasks,
)


def _pr_states(monkeypatch: pytest.MonkeyPatch, answers: dict[int, str]) -> None:
    """Make the provider answer exactly what each PR number is set to.

    An unlisted number answers ``""`` — the provider's own way of saying "could
    not look" (#802). Tests that want that case simply leave the number out.
    """

    async def fake_pr_state(
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> str:
        return answers.get(int(pr_number), "")

    monkeypatch.setattr(plugins.git_ops, "pr_state", fake_pr_state, raising=False)


async def _task_awaiting_decision(
    client: AsyncClient, db: aiosqlite.Connection, *, title: str, pr: int
) -> int:
    """A task with a pinned PR, parked where the arbiter leaves it."""
    task_id = (await client.post("/api/tasks", json={"title": title})).json()["id"]
    await repo.update_task(
        db,
        task_id,
        status="needs_decision",
        pr_number=pr,
        branch=f"task-{task_id}/work",
    )
    await db.commit()
    return task_id


async def _completed_task(
    db: aiosqlite.Connection,
    client: AsyncClient,
    *,
    title: str,
    pr: int,
    merged_by_gate: bool = False,
) -> int:
    """A task already sitting in ``completed``, with or without a gate merge."""
    task_id = (await client.post("/api/tasks", json={"title": title})).json()["id"]
    await repo.update_task(db, task_id, status="completed", pr_number=pr)
    if merged_by_gate:
        await db.execute(
            "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
            "VALUES (?, ?, ?, ?)",
            (1, pr, task_id, f"{task_id:040d}"),
        )
    await db.commit()
    return task_id


async def _alerts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    rows = await repo.get_task_updates(db, task_id)
    return [dict(r)["content"] for r in rows if dict(r)["kind"] == "alert"]


# --- AC-1: a manual acceptance says what it is leaving behind ---------------


async def test_manual_accept_records_that_the_pr_is_still_open(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = await _task_awaiting_decision(client, db, title="Accept me", pr=444)
    _pr_states(monkeypatch, {444: "open"})

    resp = await client.post(
        f"/api/tasks/{task_id}/decide",
        json={"action": "accept", "decision_summary": "CI will be green shortly."},
    )

    # The acceptance itself is untouched: it is the owner's way out, and this
    # task explicitly refuses to take it away in any form.
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"

    # But it is no longer silent about what it left behind.
    alerts = await _alerts(db, task_id)
    assert any("НЕ доставлена" in a and "444" in a for a in alerts), alerts
    assert any("Судьба PR не выбрана" in a for a in alerts), alerts

    stored = await repo.get_delivery_discrepancy(db, task_id)
    assert stored is not None
    assert stored["state"] == PR_OPEN
    assert stored["pr_number"] == 444
    assert stored["accepted_via"] == "decide_accept"


async def test_manual_accept_records_the_owners_choice_for_the_pr(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fork this task had to pick: the owner names the PR's fate, and the
    hub records it as a declaration — it merges nothing and closes nothing."""
    task_id = await _task_awaiting_decision(client, db, title="Cancelled", pr=901)
    _pr_states(monkeypatch, {901: "open"})

    resp = await client.post(
        f"/api/tasks/{task_id}/decide",
        json={"action": "accept", "pr_disposition": "abandon"},
    )
    assert resp.status_code == 200

    stored = await repo.get_delivery_discrepancy(db, task_id)
    assert stored is not None
    assert stored["disposition"] == "abandon"
    assert any("работа отменена" in a for a in await _alerts(db, task_id))

    # A declaration is not a fact (#484): the PR is still open, so the row is
    # still a discrepancy. Saying "abandon" does not close a pull request, and
    # the list must not pretend otherwise.
    assert stored["state"] == PR_OPEN


async def test_force_complete_is_not_the_forgotten_door(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force-complete is the other way a human closes a task without the gate.

    This codebase has closed two of three entrances before and reported the
    class fixed, so the third is held by a test rather than by intent.
    """
    task_id = (await client.post("/api/tasks", json={"title": "Stuck"})).json()["id"]
    await repo.update_task(db, task_id, status="running", pr_number=902)
    await db.commit()
    _pr_states(monkeypatch, {902: "open"})

    resp = await client.post(
        f"/api/tasks/{task_id}/force-complete",
        json={"comment": "Superseded by another branch.", "pr_disposition": "deliver"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"

    stored = await repo.get_delivery_discrepancy(db, task_id)
    assert stored is not None
    assert stored["state"] == PR_OPEN
    assert stored["accepted_via"] == "force_complete"
    assert stored["disposition"] == "deliver"


async def test_a_delivered_task_is_not_accused(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accepting a task whose PR the gate already merged says nothing at all.

    A warning that fires in the ordinary case teaches the reader to skip the
    line — and then it is invisible in the case that matters (#885's lesson).
    """
    task_id = await _task_awaiting_decision(client, db, title="Delivered", pr=903)
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (?, ?, ?, ?)",
        (1, 903, task_id, "a" * 40),
    )
    await db.commit()
    _pr_states(monkeypatch, {903: "merged"})

    resp = await client.post(f"/api/tasks/{task_id}/decide", json={"action": "accept"})
    assert resp.status_code == 200

    assert await _alerts(db, task_id) == []
    stored = await repo.get_delivery_discrepancy(db, task_id)
    assert stored is not None and stored["state"] == DELIVERED


# --- AC-2: the discrepancy exists as a list, not as a memory ----------------


async def test_undelivered_completed_tasks_are_listed(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_pr = await _completed_task(db, client, title="Left open", pr=444)
    merged = await _completed_task(
        db, client, title="Delivered by the gate", pr=445, merged_by_gate=True
    )
    closed = await _completed_task(db, client, title="Cancelled", pr=446)
    _pr_states(monkeypatch, {444: "open", 445: "merged", 446: "closed"})

    await scan_completed_deliveries(db)
    listed = await undelivered_completed_tasks(db)

    ids = [row["task_id"] for row in listed["undelivered"]]
    assert ids == [open_pr]
    assert merged not in ids
    # A PR closed without a merge is work dropped on purpose, not a
    # discrepancy — putting it here would raise an alarm about a decision
    # somebody already took.
    assert closed not in ids
    assert (await repo.get_delivery_discrepancy(db, closed))["state"] == PR_CLOSED

    row = listed["undelivered"][0]
    assert row["pr_number"] == 444
    assert row["age_hours"] is not None  # AC-2: number of the PR and its age
    assert "444" in row["reason"]

    # And the same list through the door the owner and the agents actually use.
    api = await client.get("/api/delivery/discrepancies")
    assert api.status_code == 200
    assert [r["task_id"] for r in api.json()["undelivered"]] == [open_pr]


async def test_the_list_never_calls_the_provider_on_read(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading the list costs one SELECT — the sweep pays for the network.

    The constraint is not a preference: the inbox renders on every dashboard
    load, and a GitHub call per row would make the board hostage to a provider
    that has no opinion about how often people refresh a page.
    """
    await _completed_task(db, client, title="Left open", pr=444)
    _pr_states(monkeypatch, {444: "open"})
    await scan_completed_deliveries(db)

    calls: list[int] = []

    async def counting_pr_state(
        pr_number: int, repo: str | None = None, gh_repo: str | None = None
    ) -> str:
        calls.append(pr_number)
        return "open"

    monkeypatch.setattr(plugins.git_ops, "pr_state", counting_pr_state, raising=False)

    assert (await client.get("/api/delivery/discrepancies")).status_code == 200
    assert (await client.get("/partials/inbox")).status_code == 200
    assert (await client.get("/")).status_code == 200
    assert calls == []


async def test_the_owner_sees_it_where_they_look_at_the_board(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a report you have to know to ask for.

    The cost of finding this on 21.08 was one person deciding, unprompted, to
    compare open PRs against the board. A list that lives behind a query
    nobody runs would leave that cost exactly where it was.
    """
    task_id = await _completed_task(db, client, title="Left open", pr=444)
    _pr_states(monkeypatch, {444: "open"})
    await scan_completed_deliveries(db)

    page = await client.get("/partials/inbox")
    assert page.status_code == 200
    assert f"#{task_id}" in page.text
    assert "PR #444" in page.text
    # The inbox badge counts it too — a section below the fold that does not
    # move the number is a section you see only if you already scrolled to it.
    assert "Completed, PR still open" in page.text


async def test_the_sweep_alerts_once_per_state_not_once_per_pass(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Damped like the stale sweeps in the poller — news, not a metronome."""
    task_id = await _completed_task(db, client, title="Left open", pr=444)
    _pr_states(monkeypatch, {444: "open"})

    await scan_completed_deliveries(db)
    await scan_completed_deliveries(db)
    await scan_completed_deliveries(db)

    assert len(await _alerts(db, task_id)) == 1


async def test_a_delivered_task_leaves_the_list(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The list is driven by what the PR is doing, not by what was declared."""
    task_id = await _completed_task(db, client, title="Later merged", pr=444)
    _pr_states(monkeypatch, {444: "open"})
    await scan_completed_deliveries(db)
    assert (await undelivered_completed_tasks(db))["undelivered"]

    _pr_states(monkeypatch, {444: "merged"})
    await scan_completed_deliveries(db)

    assert (await undelivered_completed_tasks(db))["undelivered"] == []
    assert (await repo.get_delivery_discrepancy(db, task_id))["state"] == DELIVERED


async def test_a_discrepancy_row_does_not_block_deleting_its_task(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new table hanging off ``tasks`` can break deletion, and quietly.

    ``delete_task_subtree`` clears ``task_updates`` by hand; it knows nothing
    about this table. With foreign keys on — and they are — a bare reference
    would turn "delete this task" into a constraint error for exactly the
    tasks this feature marks, which is a fine way to make a new oversight
    mechanism the reason people stop trusting the old ones.
    """
    task_id = await _completed_task(db, client, title="Doomed", pr=444)
    _pr_states(monkeypatch, {444: "open"})
    await scan_completed_deliveries(db)
    assert await repo.get_delivery_discrepancy(db, task_id) is not None

    assert (await client.delete(f"/api/tasks/{task_id}")).status_code in (200, 204)
    assert await repo.get_task(db, task_id) is None
    assert await repo.get_delivery_discrepancy(db, task_id) is None


# --- AC-3: "could not look" is an answer of its own -------------------------


async def test_unknown_pr_state_is_its_own_answer(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = await _completed_task(db, client, title="Provider is down", pr=447)

    async def provider_is_down(
        pr_number: int, repo: str | None = None, gh_repo: str | None = None
    ) -> str:
        return ""  # #802: empty is "could not ask", never "closed"

    monkeypatch.setattr(plugins.git_ops, "pr_state", provider_is_down, raising=False)

    row = await repo.get_task(db, task_id)
    answer = await task_delivery(db, dict(row))

    assert answer["state"] == UNKNOWN
    assert answer["state"] not in (DELIVERED, PR_OPEN, PR_CLOSED)
    assert answer["reason"], "an unknown without a cause is just a shrug"
    assert "447" in answer["reason"]
    assert answer["delivery_path"] == "unknown"

    # And it does not sneak into the discrepancy list through the back door:
    # a question the hub could not ask is not a task somebody failed to
    # deliver, and folding the two together is how a list stops being read.
    await scan_completed_deliveries(db)
    listed = await undelivered_completed_tasks(db)
    assert [r["task_id"] for r in listed["undelivered"]] == []
    assert [r["task_id"] for r in listed["unknown"]] == [task_id]


async def test_a_provider_that_raises_is_unknown_not_a_verdict(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _completed_task(db, client, title="Provider explodes", pr=448)

    async def boom(
        pr_number: int, repo: str | None = None, gh_repo: str | None = None
    ) -> str:
        raise RuntimeError("gh: connection reset")

    monkeypatch.setattr(plugins.git_ops, "pr_state", boom, raising=False)

    await scan_completed_deliveries(db)
    listed = await undelivered_completed_tasks(db)
    assert [r["task_id"] for r in listed["undelivered"]] == []
    assert len(listed["unknown"]) == 1


async def test_a_task_without_a_pinned_pr_is_not_a_discrepancy(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stated in the task's own assumptions: with no PR pinned, undelivered
    work is indistinguishable from work that never needed a PR. #498's warning
    covers that case, and two findings under one name is one too many."""
    task_id = (await client.post("/api/tasks", json={"title": "A spike"})).json()["id"]
    await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()
    _pr_states(monkeypatch, {})

    assert (
        await client.post(f"/api/tasks/{task_id}/decide", json={"action": "accept"})
    ).status_code == 200

    assert await _alerts(db, task_id) == []
    await scan_completed_deliveries(db)
    assert (await undelivered_completed_tasks(db))["undelivered"] == []


# --- AC-4: the run of 21.08.2026, replayed ----------------------------------


async def test_the_incident_of_21_08_would_be_caught(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#878 and #885, step by step, against the mechanism that now exists.

    Not a synthetic pair of rows: the same shape, the same order, the same
    texts the feed carried. #885 is the bitter one — the task that fixed lying
    about blocker delivery, whose own delivery was the thing that went missing.
    """
    incident: dict[str, dict[str, Any]] = {
        "878": {
            "title": "Категория, закрытая проверкой, перестаёт стоить денег",
            "pr": 443,
        },
        "885": {
            "title": "Источник факта доставки блокера: смержено или нет",
            "pr": 444,
        },
    }
    for entry in incident.values():
        entry["id"] = await _task_awaiting_decision(
            client, db, title=entry["title"], pr=entry["pr"]
        )
        # 18:44:41 — the gate refuses, correctly, and says why.
        await repo.add_task_update(
            db,
            entry["id"],
            "hub",
            "alert",
            f"Done report NOT completed: PR #{entry['pr']} is not delivered — "
            "ci_pending: workflow_runs_running. Fix the cause and report done "
            "again, or decide the task by hand.",
        )
    await db.commit()

    # The PRs are open at this moment and stay open: CI is still running, so
    # nothing has merged them, and nothing will once the tasks are closed.
    _pr_states(monkeypatch, {443: "open", 444: "open"})

    # 18:46:30 — the human takes the exit the refusal offered. Both decisions
    # must still succeed: this is a legitimate move, and the fix is not a ban.
    for entry in incident.values():
        resp = await client.post(
            f"/api/tasks/{entry['id']}/decide",
            json={"action": "accept", "decision_summary": "Accepted after arbiter."},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    # Minutes later CI goes green. The PRs are MERGEABLE and CLEAN — and still
    # open, because the gate delivers on a done report and no further report
    # ever comes: that is the exact moment the work went missing on the day.
    found = await scan_completed_deliveries(db)
    listed = await undelivered_completed_tasks(db)

    caught = {row["task_id"] for row in listed["undelivered"]}
    assert caught == {incident["878"]["id"], incident["885"]["id"]}
    assert {f["task_id"] for f in found} == caught

    # Two hours of "completed" while the code sat outside develop — that is
    # what the age column is for, and what nobody could see on the day.
    for row in listed["undelivered"]:
        assert row["pr_number"] in (443, 444)
        assert row["age_hours"] is not None
        assert row["accepted_via"] == "decide_accept"

    # And the task itself carries the trace, so the discrepancy is findable
    # from either end — the list, or the task somebody happens to open.
    alerts = await _alerts(db, incident["885"]["id"])
    assert any("444" in a and "НЕ доставлена" in a for a in alerts), alerts


# --- deliver actually delivers, under the gate's conditions (#1037) ---------
#
# 28.08.2026: #1036 was approved, its CI failed once for two seconds on
# infrastructure, and the gate sent it to a human. The human accepted it — and
# the code stayed in an open PR with nobody left to merge it, because the gate
# only looks inside the conveyor. The manual merge that closed it was an
# exception to the rule that the gate, not a person, merges into develop.
#
# So `deliver` acts. What it must never become is a way AROUND the gate: a task
# reaches the human along paths that ARE failed conditions. Hence one rule —
# the same conditions, asked by calling the gate's own function — and four
# tests below that each try to get work merged without one of them.


class _MergeSpy:
    """Records what the gate's merge entry point was asked to do."""

    def __init__(self, ci: str = "passed", merges: bool = True) -> None:
        self.ci = ci
        self.merges = merges
        self.merged: list[int] = []

    async def check_pr_ci(self, pr_number, repo=None, gh_repo=None, forge: str = ""):
        from hub.integrations.protocols import CIProbeOutcome, CIProbeResult

        outcome = (
            CIProbeOutcome.passed if self.ci == "passed" else CIProbeOutcome.failed
        )
        return CIProbeResult(outcome=outcome, reason=f"probe says {self.ci}")

    async def merge_pr(
        self, pr_number, task_id, title, repo=None, gh_repo=None, forge: str = ""
    ):
        if not self.merges:
            return False
        self.merged.append(int(pr_number))
        return True

    async def merge_pr_with_detail(
        self,
        pr_number,
        task_id,
        title,
        repo=None,
        gh_repo=None,
        delete_branch=True,
        forge: str = "",
    ):
        # #1116: гейт спрашивает причину отказа, а не только факт. Дублёр
        # отвечает согласованно со своим merge_pr — иначе он рассказывал бы
        # о доставке две разные истории.
        ok = await self.merge_pr(pr_number, task_id, title, repo=repo, gh_repo=gh_repo)
        return (ok, "" if ok else "")

    async def merge_commit_sha(
        self, pr_number, repo=None, gh_repo=None, forge: str = ""
    ):
        return f"{int(pr_number):040d}"

    async def head_sha(self, repo, ref):
        return "a" * 40

    async def pull_main(self, repo=None, base_branch=None):
        return True

    async def delete_branch(self, branch, repo=None, base_branch=None):
        return True


async def _approved_task(
    client: AsyncClient, db: aiosqlite.Connection, *, title: str, pr: int
) -> int:
    """A task parked at the decision gate WITH an approved current submission."""
    task_id = await _task_awaiting_decision(client, db, title=title, pr=pr)
    await repo.update_task(
        db,
        task_id,
        submission_generation=1,
        submission_sha="a" * 40,
        review_verdict="approved",
        review_verdict_generation=1,
    )
    await db.commit()
    return task_id


async def _decide_deliver(client: AsyncClient, task_id: int):
    return await client.post(
        f"/api/tasks/{task_id}/decide",
        json={"action": "accept", "pr_disposition": "deliver"},
    )


def _install(monkeypatch, spy: _MergeSpy) -> None:
    for name in (
        "check_pr_ci",
        "merge_pr",
        # #1116: гейт зовёт детальный вариант — без него подменялся бы один
        # метод, а работал бы другой, и дублёр молча переставал бы дублировать.
        "merge_pr_with_detail",
        "merge_commit_sha",
        "head_sha",
        "pull_main",
        "delete_branch",
    ):
        monkeypatch.setattr(plugins.git_ops, name, getattr(spy, name), raising=False)


async def test_deliver_merges_and_records_like_the_gate(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1: every condition met — the PR is merged and the delivery is written
    # to pipeline_merges, the same table the undelivered report reads.
    spy = _MergeSpy()
    _install(monkeypatch, spy)
    task_id = await _approved_task(client, db, title="deliverable", pr=901)

    resp = await _decide_deliver(client, task_id)

    assert resp.status_code == 200, resp.text
    assert spy.merged == [901]
    assert await repo.pipeline_merge_recorded(db, task_id, 901), (
        "the delivery must be recorded where the gate records it"
    )


async def test_deliver_refuses_on_red_ci(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2: red CI is one of the ways a task REACHES this gate. Delivering
    # anyway would make the decision a way past CI.
    spy = _MergeSpy(ci="failed")
    _install(monkeypatch, spy)
    task_id = await _approved_task(client, db, title="red ci", pr=902)

    resp = await _decide_deliver(client, task_id)

    assert resp.status_code == 200, "the acceptance itself still stands"
    assert spy.merged == [], "nothing may be merged on red CI"
    assert any("ci_" in a for a in await _alerts(db, task_id)), (
        "the refusal must name CI, not just say no"
    )


async def test_deliver_refuses_without_approved_review(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3: the hole this task exists to avoid. A human decision accepts the
    # task; it does not stand in for a reviewer's verdict.
    spy = _MergeSpy()
    _install(monkeypatch, spy)
    task_id = await _task_awaiting_decision(client, db, title="unreviewed", pr=903)

    resp = await _decide_deliver(client, task_id)

    assert resp.status_code == 200
    assert spy.merged == []
    assert any("одобренного ревью" in a for a in await _alerts(db, task_id))


async def test_deliver_refuses_when_the_tip_moved_after_approval(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#612): approved, then pushed. The verdict still reads as current
    # because the generation never changed — only comparing the code catches
    # it, and that comparison lives in the gate's function, not here.
    spy = _MergeSpy()
    _install(monkeypatch, spy)
    task_id = await _approved_task(client, db, title="moved tip", pr=904)
    await repo.update_task(db, task_id, submission_sha="b" * 40)
    await db.commit()
    # The branch now stands somewhere the reviewer never saw. The comparison
    # reads the tip through resolve_branch_tip, so that is what has to answer.
    from hub.services import lifecycle as lifecycle_mod

    async def moved_tip(db_, task_id_, branch_):
        return "c" * 40, ""

    monkeypatch.setattr(lifecycle_mod, "resolve_branch_tip", moved_tip)

    resp = await _decide_deliver(client, task_id)

    assert resp.status_code == 200
    assert spy.merged == [], "code that nobody approved must not be delivered"


async def test_deliver_survives_a_refused_merge(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-5: GitHub can refuse for reasons the hub cannot see. The refusal is a
    # named outcome, not an exception, and the acceptance stays.
    spy = _MergeSpy(merges=False)
    _install(monkeypatch, spy)
    task_id = await _approved_task(client, db, title="github says no", pr=905)

    resp = await _decide_deliver(client, task_id)

    assert resp.status_code == 200
    assert not await repo.pipeline_merge_recorded(db, task_id, 905)
    assert any("merge_failed" in a for a in await _alerts(db, task_id))


async def test_deliver_goes_through_the_gates_own_entry_point(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-6: the conditions are not re-derived here. If deliver ever grows its
    # own set, the two will drift and the weaker one becomes the real one
    # (#519, #546) — so the call itself is the thing asserted.
    calls: list[int] = []
    from hub.services import lifecycle as lifecycle_mod
    from hub.services import orchestration as orch

    real = orch.merge_before_completion

    async def spy_merge(db_, task_):
        calls.append(int(task_["id"]))
        return await real(db_, task_)

    monkeypatch.setattr(orch, "merge_before_completion", spy_merge)
    _install(monkeypatch, _MergeSpy())
    task_id = await _approved_task(client, db, title="same entry", pr=906)

    await lifecycle_mod.deliver_on_disposition(db, task_id, "deliver", via="test")

    assert calls == [task_id], "deliver must go through the gate's own function"


async def test_delivered_task_leaves_the_undelivered_report(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-7: the alert that named the discrepancy must stop naming it once the
    # work is in. The report reads pipeline_merges, which the gate's function
    # writes — another reason not to merge on the side.
    spy = _MergeSpy()
    _install(monkeypatch, spy)
    _pr_states(monkeypatch, {907: PR_OPEN})
    task_id = await _approved_task(client, db, title="leaves report", pr=907)

    await _decide_deliver(client, task_id)

    delivered = await task_delivery(db, dict(await repo.get_task(db, task_id)))
    assert delivered["state"] == DELIVERED
    listed = await undelivered_completed_tasks(db)
    assert task_id not in [row["task_id"] for row in listed["undelivered"]]


async def test_force_complete_deliver_behaves_like_decide(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-8: force-complete writes the same field, so it gets the same door —
    # otherwise it stays the quiet one people reach for when the first refuses.
    spy = _MergeSpy()
    _install(monkeypatch, spy)
    good = await _approved_task(client, db, title="forced ok", pr=908)

    resp = await client.post(
        f"/api/tasks/{good}/force-complete",
        json={"comment": "closing by hand", "pr_disposition": "deliver"},
    )
    assert resp.status_code == 200, resp.text
    assert spy.merged == [908]

    spy.ci = "failed"
    bad = await _approved_task(client, db, title="forced red", pr=909)
    resp = await client.post(
        f"/api/tasks/{bad}/force-complete",
        json={"comment": "closing by hand", "pr_disposition": "deliver"},
    )
    assert resp.status_code == 200
    assert 909 not in spy.merged, "force-complete is not a way past the gate"


# --- #1215: a row nobody is left to ask -------------------------------------
#
# 09.09.2026. #878 (PR #443), #875 (PR #461) and #909 (PR #468) had stood in
# ``unknown`` for 441-453 hours. Not because the provider blinked: the former
# repository, where those PRs lived, is deleted, and in the current one the
# numbering started over. No pipeline_merges row, no answer from the base
# branch (squash), no provider.
# Every source closed at once, and ``unknown`` was the correct answer to a
# question that will never be answered again.
#
# Their delivery was nevertheless verified, and by something stronger than a
# PR state: the code of all three is present in the deployed commit
# 19ee3f6faf9f, checked by their own AC tests by name. The registry knew
# nothing about it, because it had nowhere to put an observation.
#
# The one exit that existed — archiving — is worse than the illness: the same
# ``archived = 0`` filter reads the outcome debt, so archiving would have
# removed the visible row at the price of a silently dropped outcome review.
# These tests hold the exit that does not make that trade.


async def _unanswerable_row(
    client: AsyncClient,
    db: aiosqlite.Connection,
    *,
    title: str,
    pr: int,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    """A completed task whose three sources are all closed, as of 09.09.2026.

    No ``pipeline_merges`` row, a base branch that cannot answer, and a
    provider that answers "" — the shape #878/#875/#909 are actually in.
    """
    task_id = await _completed_task(db, client, title=title, pr=pr)
    _pr_states(monkeypatch, {})  # unlisted number => "could not look"
    await scan_completed_deliveries(db)
    return task_id


_PROBE = "git show 19ee3f6faf9f --stat | grep hub/services/delivery_state.py"
_SAW = "файл присутствует в раскатанном коммите, AC-тест задачи зелёный"
_SHA = "19ee3f6faf9f"


async def test_a_named_observation_closes_an_unanswerable_row(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1. The row leaves the list, and the reader can see WHO closed it.

    Both halves matter and they pull against each other. Leaving is the point:
    a list that cannot return to zero stops meaning "nothing is undelivered",
    and then the first real loss leaves with the noise. Staying visible is the
    guard: if closing a row made it vanish, the record would be a button
    labelled "remove this line" rather than an attributed act.
    """
    task_id = await _unanswerable_row(
        client, db, title="Маховик", pr=443, monkeypatch=monkeypatch
    )
    assert [
        r["task_id"] for r in (await undelivered_completed_tasks(db))["unknown"]
    ] == [task_id]

    result = (
        await client.post(
            f"/api/delivery/discrepancies/{task_id}/observation",
            json={"probe": _PROBE, "observation": _SAW, "sha": _SHA},
        )
    ).json()
    assert result["closed_state"] == UNKNOWN

    listed = await undelivered_completed_tasks(db)
    assert [r["task_id"] for r in listed["unknown"]] == []
    assert [r["task_id"] for r in listed["undelivered"]] == []

    # Gone from the list, not gone. And what the reader sees is that a PERSON
    # answered, naming a commit — not that the hub finally established it.
    closed = listed["closed_by_observation"]
    assert [r["task_id"] for r in closed] == [task_id]
    assert closed[0]["observed_by"]
    assert closed[0]["observed_sha"] == _SHA
    assert closed[0]["observed_probe"] == _PROBE
    assert closed[0]["observed_evidence"] == _SAW
    # The distinction is in the reader's payload, not only in a column: the
    # closed row still carries the hub's own answer, which is what makes
    # "confirmed by observation" different from "established by the hub".
    assert closed[0]["state"] == UNKNOWN

    # A mirror, not a gate: the row moved, the task did not.
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed"
    assert not task["archived"], "archiving takes the outcome debt with it"


async def test_a_stamp_without_evidence_closes_nothing(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2. "Проверено, всё хорошо" is a stamp, and a stamp closes nothing.

    Four refusals, because the stamp has four shapes and only the first one is
    obvious: empty; a stamp split across both fields so that neither is empty;
    a real observation with no commit named; and a "commit" that is not one.
    """
    task_id = await _unanswerable_row(
        client, db, title="Предпас", pr=461, monkeypatch=monkeypatch
    )
    url = f"/api/delivery/discrepancies/{task_id}/observation"

    refusals = [
        {"probe": "", "observation": "", "sha": _SHA},
        {"probe": "   ", "observation": _SAW, "sha": _SHA},
        # The stamp, split in two so that neither field is empty.
        {"probe": "проверено", "observation": "всё хорошо", "sha": _SHA},
        # A real observation that names no place to check it.
        {"probe": _PROBE, "observation": _SAW, "sha": ""},
        {"probe": _PROBE, "observation": _SAW, "sha": "не помню"},
    ]
    for payload in refusals:
        resp = await client.post(url, json=payload)
        assert resp.status_code == 422, payload
        # A refusal that does not say why is its own kind of stamp.
        assert resp.json()["detail"], payload
        # And it must never offer the exit that loses the outcome review.
        assert "архив" not in str(resp.json()).lower(), payload

    # The row is exactly where it was: refused is refused, not "half recorded".
    listed = await undelivered_completed_tasks(db)
    assert [r["task_id"] for r in listed["unknown"]] == [task_id]
    assert listed["closed_by_observation"] == []


async def test_closing_a_row_keeps_what_it_used_to_say(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3. Closing appends to the history; it does not overwrite it.

    The temptation is to write ``delivered`` over the row and be done. That
    would erase the fact that the hub could not answer, and with it the reason
    — turning an attributed human observation into an indistinguishable fourth
    source of truth, which is exactly what the task forbids.
    """
    task_id = await _unanswerable_row(
        client, db, title="Паспорт дефекта", pr=468, monkeypatch=monkeypatch
    )
    before = await repo.get_delivery_discrepancy(db, task_id)
    assert before["state"] == UNKNOWN
    was_reason = before["reason"]
    assert "468" in was_reason

    await client.post(
        f"/api/delivery/discrepancies/{task_id}/observation",
        json={"probe": _PROBE, "observation": _SAW, "sha": _SHA},
    )

    after = await repo.get_delivery_discrepancy(db, task_id)
    assert after["state"] == UNKNOWN, "the hub's own answer was overwritten"
    assert after["reason"] == was_reason, "the cause of the unknown was erased"
    assert after["observed_state"] == UNKNOWN

    # Readable as history, in the feed the owner actually reads.
    note = " ".join(await _alerts(db, task_id))
    assert was_reason in note
    assert _SHA in note

    # The closure is tied to the FACT, not to the task forever. If the
    # provider ever comes back and says something nobody observed, the row
    # returns: an observation of "could not tell" says nothing about
    # "the PR is open".
    await repo.record_delivery_discrepancy(
        db,
        task_id=task_id,
        state=PR_OPEN,
        reason="PR #468 открыт",
        pr_number=468,
        delivery_path="none",
    )
    listed = await undelivered_completed_tasks(db)
    assert [r["task_id"] for r in listed["undelivered"]] == [task_id]
    assert listed["closed_by_observation"] == []


async def test_a_row_past_the_sweep_window_still_has_the_exit(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What happens to a row older than the sweep window — said, not implied.

    The sweep takes candidates completed within DELIVERY_SCAN_LOOKBACK_DAYS.
    Past that edge the sources are never re-asked and the row freezes on
    whatever they last said. For #878/#875/#909 that edge falls around
    20-21.09.2026, and the statement's whole deadline is that after it nobody
    could rewrite the row at all.

    Two things are asserted, and the second is the one with the deadline on
    it: the reader is TOLD the row is frozen rather than being left to notice
    a motionless ``checked_at``, and the observation path does not consult the
    window, so the exit outlives the boundary.
    """
    task_id = await _unanswerable_row(
        client, db, title="Старая строка", pr=470, monkeypatch=monkeypatch
    )
    # Push it past the edge: completed long before the lookback window.
    await db.execute(
        "UPDATE tasks SET completed_at = datetime('now', '-400 days') WHERE id = ?",
        (task_id,),
    )
    await db.commit()

    # The sweep no longer even considers it — this is the freeze, reproduced.
    candidates = await repo.completed_tasks_awaiting_delivery(db)
    assert task_id not in [dict(r)["id"] for r in candidates]

    listed = await undelivered_completed_tasks(db)
    frozen = [r for r in listed["unknown"] if r["task_id"] == task_id]
    assert frozen, "a frozen row must stay visible, not fall off the list"
    assert frozen[0]["still_swept"] is False, (
        "the reader must be told the sources are no longer being re-asked"
    )
    assert listed["sweep_lookback_days"] >= 1

    # And the exit still opens, which is the entire point of the deadline.
    assert (
        await client.post(
            f"/api/delivery/discrepancies/{task_id}/observation",
            json={"probe": _PROBE, "observation": _SAW, "sha": _SHA},
        )
    ).status_code == 200
    listed = await undelivered_completed_tasks(db)
    assert [r["task_id"] for r in listed["closed_by_observation"]] == [task_id]


async def test_an_observation_needs_a_row_to_close(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    """No stored discrepancy means there is nothing to close, and saying so
    beats inventing a row: the registry must not learn to hold observations
    about tasks it never had an opinion on."""
    task_id = (await client.post("/api/tasks", json={"title": "Never swept"})).json()[
        "id"
    ]
    resp = await client.post(
        f"/api/delivery/discrepancies/{task_id}/observation",
        json={"probe": _PROBE, "observation": _SAW, "sha": _SHA},
    )
    assert resp.status_code == 422
    assert await repo.get_delivery_discrepancy(db, task_id) is None


async def test_a_refusal_names_the_right_cause(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each refusal names ITS cause, not merely some cause.

    Found by mutation: with the emptiness check removed, every test still
    passed, because the length floor refuses an empty field too. Same verdict,
    different sentence — and the sentence is the whole product here. "Слишком
    коротко" sends someone to pad a stamp out to twelve characters; "нужно и
    то, что запускали, и то, что увидели" sends them to run something. This
    codebase has already paid for a refusal naming the wrong cause once, in
    the acknowledgement path, where three spaces got "строки нет".
    """
    from hub.services.delivery_state import ObservationRefused
    from hub.services.delivery_state import (
        record_delivery_observation as record_observation,
    )

    task_id = await _unanswerable_row(
        client, db, title="Причины отказов", pr=471, monkeypatch=monkeypatch
    )

    cases = {
        "incomplete_evidence": ("", _SAW, _SHA),
        "evidence_too_thin": ("коротко", "тоже", _SHA),
        "missing_commit": (_PROBE, _SAW, ""),
    }
    for expected, (probe, seen, sha) in cases.items():
        with pytest.raises(ObservationRefused) as caught:
            await record_observation(
                db, task_id, by="tester", probe=probe, evidence=seen, sha=sha
            )
        assert caught.value.reason == expected, (probe, seen, sha)
        assert caught.value.hint, "a refusal with no way forward is a dead end"
        assert "архив" not in caught.value.hint.lower()


async def test_a_live_check_closes_the_row_it_already_answered(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second door, and the reason it exists.

    A live check (#813) is already a named, attributed observation: what was
    run, what came back, in which commit, recorded by whom. For #878/#875/#909
    such records were written on 09.09.2026, and they are stronger evidence of
    delivery than a PR state — the code of all three is in the deployed commit,
    checked by their own AC tests by name. Making someone write the same thing
    again under a different verb would be charging for the form.

    Both doors write the same columns through the same function, so there is
    no second answer to "is this row closed".
    """
    task_id = await _unanswerable_row(
        client, db, title="Живая проверка", pr=472, monkeypatch=monkeypatch
    )
    monkeypatch.setattr(
        "hub.services.delivery_state.delivery_state",
        _in_prod,
        raising=False,
    )

    assert (
        await client.post(
            f"/api/tasks/{task_id}/live-check",
            json={"probe": _PROBE, "observation": _SAW, "sha": _SHA},
        )
    ).status_code == 200

    listed = await undelivered_completed_tasks(db)
    closed = listed["closed_by_observation"]
    assert [r["task_id"] for r in closed] == [task_id]
    assert closed[0]["observed_sha"] == _SHA
    assert closed[0]["observed_evidence"] == _SAW
    assert closed[0]["state"] == UNKNOWN, "the hub's own answer is still there"


async def _in_prod(db: Any, task_id: int) -> dict[str, Any]:
    """The task's code is deployed — the case the three live rows are in."""
    return {"state": "in_prod", "reason": "в раскатанном коммите"}


async def test_a_source_that_still_answers_is_not_silenced(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An observation is the exit for a row with NOBODY left to ask.

    Where a source does answer — "PR #473 is open" — closing the row by
    observation would be a way to argue with a fact instead of fixing it. That
    row has its own exits already: deliver the work, or acknowledge the
    discrepancy with a reason (#1198). Both doors refuse here, and the refusal
    says which exit to use.
    """
    task_id = await _completed_task(db, client, title="PR всё ещё открыт", pr=473)
    _pr_states(monkeypatch, {473: "open"})
    await scan_completed_deliveries(db)
    assert [
        r["task_id"] for r in (await undelivered_completed_tasks(db))["undelivered"]
    ] == [task_id]

    resp = await client.post(
        f"/api/delivery/discrepancies/{task_id}/observation",
        json={"probe": _PROBE, "observation": _SAW, "sha": _SHA},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "source_still_answers"

    # The live-check door obeys the same rule, and the check itself survives.
    monkeypatch.setattr(
        "hub.services.delivery_state.delivery_state", _in_prod, raising=False
    )
    assert (
        await client.post(
            f"/api/tasks/{task_id}/live-check",
            json={"probe": _PROBE, "observation": _SAW, "sha": _SHA},
        )
    ).status_code == 200

    listed = await undelivered_completed_tasks(db)
    assert [r["task_id"] for r in listed["undelivered"]] == [task_id]
    assert listed["closed_by_observation"] == []


async def test_the_registry_cannot_break_a_live_check(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live check is a finished record in its own right.

    It must not fail because the registry had no row for it, or because the
    closure blew up. Those are reasons not to close a row, never reasons to
    lose the observation — and the same discipline the sweep keeps.
    """
    task_id = (
        await client.post("/api/tasks", json={"title": "Никакой строки"})
    ).json()["id"]
    monkeypatch.setattr(
        "hub.services.delivery_state.delivery_state", _in_prod, raising=False
    )

    async def explodes(*a: Any, **k: Any) -> None:
        raise RuntimeError("реестр упал")

    monkeypatch.setattr(
        "hub.services.delivery_state.record_delivery_observation",
        explodes,
        raising=False,
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/live-check",
        json={"probe": _PROBE, "observation": _SAW, "sha": _SHA},
    )
    assert resp.status_code == 200
    assert resp.json()["observation"] == _SAW


async def test_the_backfill_closes_rows_from_observations_already_written(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollout is checked on the three live rows, so it must close them.

    Without this, shipping would leave a mechanism with not one closed case
    and three rows to close by hand, repeating records that already exist.
    Only ``unknown`` rows, and only from a live check that passes the same
    evidence bar as a direct write — a stamp backfills nothing.
    """
    from hub.db import _MIGRATIONS

    unanswerable = await _unanswerable_row(
        client, db, title="Есть наблюдение", pr=474, monkeypatch=monkeypatch
    )
    stamped = await _unanswerable_row(
        client, db, title="Только штамп", pr=475, monkeypatch=monkeypatch
    )
    # A row whose source still answers, with evidence as good as the first
    # one's. The backfill must leave it alone for the same reason the code
    # does: nobody is arguing with a fact by hand here either.
    still_open = await _completed_task(db, client, title="PR открыт", pr=476)
    _pr_states(monkeypatch, {476: "open"})
    await scan_completed_deliveries(db)

    for task_id, probe, seen, sha in (
        (unanswerable, _PROBE, _SAW, _SHA),
        (stamped, "", "", ""),  # a live check with nothing to check
        (still_open, _PROBE, _SAW, _SHA),
    ):
        await db.execute(
            "INSERT INTO live_checks (task_id, sha, outcome, probe, observation, "
            "recorded_agent) VALUES (?, ?, 'done', ?, ?, 'pda_claude')",
            (task_id, sha, probe, seen),
        )
    await db.commit()

    sql = dict(_MIGRATIONS)["backfill_delivery_observed_from_live_checks"]
    await db.execute(sql)
    await db.commit()

    listed = await undelivered_completed_tasks(db)
    assert [r["task_id"] for r in listed["closed_by_observation"]] == [unanswerable]
    assert [r["task_id"] for r in listed["unknown"]] == [stamped]
    assert [r["task_id"] for r in listed["undelivered"]] == [still_open]
    closed = listed["closed_by_observation"][0]
    assert closed["observed_sha"] == _SHA
    assert closed["observed_by"] == "pda_claude"
    assert closed["state"] == UNKNOWN


# --- Находки ревью #352 -----------------------------------------------------


async def test_a_sweep_that_speaks_mid_write_does_not_get_closed_by_it(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Находка 493a0ee9: наблюдение unknown не имеет права закрыть pr_open.

    Служба читает состояние строки одним запросом, а пишет наблюдение другим,
    и SELECT транзакции не открывает. Между ними законно вклинивается свип:
    #1065 даёт запросу и опросчику РАЗНЫЕ соединения, поэтому провайдер может
    заговорить и свип успевает зафиксировать ``pr_open`` до записи. Запись
    ставила ``observed_state = state`` уже поверх нового факта — и строка,
    источник которой отвечает, вычиталась из списка как «закрытая
    наблюдением», хотя pr_open не наблюдал никто. Хуже, чем шумная строка:
    список сам себя чистит от настоящего расхождения и не выздоравливает,
    пока pr_open стабилен.

    Окно воспроизводится в самой его точке — свип исполняется между чтением и
    записью, — а не одновременным запуском: параллельный прогон дал бы
    взаимную блокировку, а не наблюдаемый исход. Проверяется ИНВАРИАНТ:
    закрытым может оказаться только тот факт, который наблюдали.
    """
    from hub import services

    task_id = await _unanswerable_row(
        client, db, title="Гонка со свипом", pr=443, monkeypatch=monkeypatch
    )
    real = repo.record_delivery_observation

    async def _sweep_speaks_meanwhile(conn, tid, **kwargs):
        # Ровно то, что делает опросчик: провайдер ожил и назвал PR открытым.
        _pr_states(monkeypatch, {443: "open"})
        await scan_completed_deliveries(conn)
        return await real(conn, tid, **kwargs)

    monkeypatch.setattr(
        repo, "record_delivery_observation", _sweep_speaks_meanwhile, raising=True
    )

    with pytest.raises(services.ObservationRefused) as refused:
        await services.record_delivery_observation(
            db, task_id, by="pda_claude", probe=_PROBE, evidence=_SAW, sha=_SHA
        )
    assert "архив" not in str(refused.value).lower()

    row = await repo.get_delivery_discrepancy(db, task_id)
    assert row["state"] == PR_OPEN, "свип должен был успеть сказать своё"
    assert row["observed_state"] != row["state"], (
        "наблюдали unknown — закрытым может быть только unknown; "
        "иначе строка, у которой источник ОТВЕЧАЕТ, молча уходит из списка"
    )

    listed = await undelivered_completed_tasks(db)
    assert [r["task_id"] for r in listed["undelivered"]] == [task_id], (
        "строка с отвечающим источником обязана остаться расхождением"
    )
    assert listed["closed_by_observation"] == []


async def test_the_sweep_stays_quiet_after_observation_closes_the_row(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Находка ревью #1215, fa215699bb39343a.

    Наблюдение закрыло строку. Свип продолжает её проверять (наблюдение не
    отменяет вопроса), и без предохранителя следующий пройденный возрастной
    рубеж заново писал «Доставку подтвердить НЕ УДАЛОСЬ ... проверьте
    вручную» — тем же голосом, каким свип говорит про НЕзакрытые строки,
    хотя ровно это уже проверили и приписали поимённо.
    """
    task_id = await _unanswerable_row(
        client, db, title="Свип после закрытия", pr=1215, monkeypatch=monkeypatch
    )
    # Уже старше первого рубежа (24ч), чтобы первый прогон свипа заговорил.
    await db.execute(
        "UPDATE tasks SET completed_at = datetime('now', '-25 hours') WHERE id = ?",
        (task_id,),
    )
    await db.commit()
    await scan_completed_deliveries(db)
    before = await _alerts(db, task_id)
    assert any("НЕ УДАЛОСЬ" in a for a in before)

    await client.post(
        f"/api/delivery/discrepancies/{task_id}/observation",
        json={"probe": _PROBE, "observation": _SAW, "sha": _SHA},
    )
    listed = await undelivered_completed_tasks(db)
    assert [r["task_id"] for r in listed["closed_by_observation"]] == [task_id]

    # Пересечь следующий рубеж (72ч) ПОСЛЕ закрытия наблюдением.
    await db.execute(
        "UPDATE tasks SET completed_at = datetime('now', '-73 hours') WHERE id = ?",
        (task_id,),
    )
    await db.commit()
    await scan_completed_deliveries(db)
    after = await _alerts(db, task_id)
    new_alerts = after[len(before) :]
    voiced_again = [a for a in new_alerts if "НЕ УДАЛОСЬ" in a]
    assert voiced_again == [], (
        "свип заново озвучил unknown уже ПОСЛЕ того, как строку закрыло "
        f"наблюдение: {voiced_again}"
    )

    # Если источник ОЖИВЁТ и заговорит другое, наблюдение относилось не к
    # этому факту — строка обязана заговорить снова, а не молчать вечно.
    _pr_states(monkeypatch, {1215: "open"})
    await scan_completed_deliveries(db)
    after_source_speaks = await _alerts(db, task_id)
    assert any(
        "PR" in a and "НЕ доставлена" in a for a in after_source_speaks[len(after) :]
    ), "источник заговорил pr_open — про НОВЫЙ факт молчать нельзя"


async def test_a_failed_journal_write_does_not_leave_a_silently_closed_row(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Находка ревью Codex, сдача 2: наблюдение коммитится одним актом.

    ``repo.record_delivery_observation`` раньше коммитило само сразу после
    UPDATE, отдельно от журнала и события, которые пишет вызывающий сервис.
    Если ``insert_event`` падал ПОСЛЕ этого коммита, HTTP-вызов возвращал бы
    500, но строка в БД уже читалась бы как закрытая наблюдением — без
    записи в ленту и без события, и повторный вызов переписал бы наблюдение
    заново, не будучи идемпотентным. Один коммит на весь акт делает падение
    честным: либо всё, либо ничего.
    """
    from hub import services

    task_id = await _unanswerable_row(
        client, db, title="Атомарность записи", pr=1216, monkeypatch=monkeypatch
    )

    async def boom(*_a, **_kw):
        raise RuntimeError("simulated failure after the repo-layer UPDATE")

    monkeypatch.setattr(repo, "insert_event", boom)
    with pytest.raises(RuntimeError):
        await services.record_delivery_observation(
            db, task_id, by="tester", probe=_PROBE, evidence=_SAW, sha=_SHA
        )

    # В проде на этом месте REST-обработчик отдаёт исключение выше, и
    # RequestConnectionMiddleware закрывает соединение запроса в finally —
    # SQLite откатывает незакоммиченную транзакцию сама (hub/app.py:381-394,
    # #1065: своё соединение на запрос). Тестовое соединение переживает всю
    # функцию, поэтому откат здесь делается явно — тем же эффектом.
    await db.rollback()
    row = await repo.get_delivery_discrepancy(db, task_id)
    assert not row["observed_at"], (
        "запись легла в базу, хотя вызывающий получил исключение — "
        "строка закрылась молча, без истории и без события"
    )
    listed = await undelivered_completed_tasks(db)
    assert [r["task_id"] for r in listed["unknown"]] == [task_id], (
        "строка обязана остаться расхождением: акт наблюдения не завершился"
    )
    assert listed["closed_by_observation"] == []


async def test_the_backfill_holds_the_same_evidence_bar_as_the_live_door(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Находка 80fcb9c9: засыпка обещает «тот же порог» и не держит его.

    Комментарий миграции говорит: берётся проверка, «прошедшая тот же порог
    доказательства, что и прямая запись». SQL проверял только ``TRIM != ''``,
    а ``_check_evidence`` требует двенадцати знаков в каждом поле и семи
    шестнадцатеричных в коммите. Живая проверка вида «ок / норм / zzz» живой
    дверью отвергается как штамп — и той же записью закрывала строку через
    засыпку. Одноразовость миграции этого не лечит: закрытые ею строки живут
    дальше, а расхождение между двумя порогами и есть та самая подмена
    доказательства штампом, против которой написан AC-2.

    Мутация по местам применения: ослабление ЛЮБОГО из трёх условий SQL
    роняет ровно этот тест.
    """
    from hub import services
    from hub.db import _MIGRATIONS

    good = await _unanswerable_row(
        client, db, title="Настоящее наблюдение", pr=443, monkeypatch=monkeypatch
    )
    # По строке на КАЖДОЕ условие порога, и каждая проваливает ровно одно:
    # ослабление любого из трёх мест применения роняет этот тест поимённо.
    thin_probe = await _unanswerable_row(
        client, db, title="Короткий probe", pr=461, monkeypatch=monkeypatch
    )
    thin_seen = await _unanswerable_row(
        client, db, title="Короткий observation", pr=468, monkeypatch=monkeypatch
    )
    bad_sha = await _unanswerable_row(
        client, db, title="Коммит не коммит", pr=469, monkeypatch=monkeypatch
    )
    short_sha = await _unanswerable_row(
        client, db, title="Коммит в три знака", pr=470, monkeypatch=monkeypatch
    )
    rows = (
        (good, _PROBE, _SAW, _SHA),
        (thin_probe, "ок", _SAW, _SHA),
        (thin_seen, _PROBE, "норм", _SHA),
        (bad_sha, _PROBE, _SAW, "не помню"),
        # Шестнадцатеричный, но неоднозначный: длину проверяет ОТДЕЛЬНОЕ
        # условие, и без этой строки его можно было бы снять незамеченным.
        (short_sha, _PROBE, _SAW, "19e"),
    )
    for task_id, probe, seen, sha in rows:
        await db.execute(
            "INSERT INTO live_checks (task_id, sha, outcome, probe, observation, "
            "recorded_agent) VALUES (?, ?, 'done', ?, ?, 'pda_claude')",
            (task_id, sha, probe, seen),
        )
    await db.commit()

    # Те же три записи, поданные в живую дверь, отвергаются — это и есть порог,
    # который засыпка обязана держать, раз обещает его в своём комментарии.
    for task_id, probe, seen, sha in rows[1:]:
        with pytest.raises(services.ObservationRefused):
            await services.record_delivery_observation(
                db, task_id, by="pda_claude", probe=probe, evidence=seen, sha=sha
            )

    sql = dict(_MIGRATIONS)["backfill_delivery_observed_from_live_checks"]
    await db.execute(sql)
    await db.commit()

    listed = await undelivered_completed_tasks(db)
    assert [r["task_id"] for r in listed["closed_by_observation"]] == [good]
    assert sorted(r["task_id"] for r in listed["unknown"]) == sorted(
        [thin_probe, thin_seen, bad_sha, short_sha]
    ), (
        "штамп не закрывает строку ни прямой записью, ни засыпкой: "
        "два порога у одного глагола — это два разных ответа"
    )
