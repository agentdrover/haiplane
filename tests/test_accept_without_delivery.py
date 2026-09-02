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
