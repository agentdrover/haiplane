"""«Принять» по графу происхождения отчётов (#1912).

В #1810 «Принять» выключили после семи кругов: каждый раз находился частный
случай ложного принятия. Здесь проверяется определение, а не случаи: граф
отчётов (текущее поколение, источники переносов, все отчёты поколений между
ними) целиком проходит один предикат, и любая неопределённость — «Проверить».

* AC-1 — каждый известный ложный «Принять» (круги 1–7 #1810, сценарий Codex
  20.10 и новые классы графа) отказан, причина называет узел;
* AC-2 — чистые сдачи дают «Принять», карточка и входящие говорят одно;
* AC-3 — корпус промахов #1864 не получает «Принять».
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiosqlite
import pytest

from hub import repository as repo
from hub import services, web
from hub.models import TaskDecide, TaskReturnToWork
from hub.integrations.registry import plugins
from hub.services import decision_brief, report_provenance
from hub.services.accept_proof import prove_accept
from hub.services.finding_identity import unresolved_uids
from scripts import review_eval as ev
from tests.test_decision_brief_whitelist import _task, observe_tip

ACCEPT = "Принять"
UNRESOLVED = '[{"title":"не проверено","why":"x"}]'
LOST = '["security: files not read"]'
CONFIRMED = (
    '[{"title":"сломан возврат","severity":"high","category":"correctness",'
    '"file":"hub/x.py","line":1,"locator":"lines","start_line":1,"detail":"d"}]'
)
CORPUS = Path(__file__).parent / "fixtures" / "review_eval" / "cases.json"


class World:
    """Отчёты задачи, расставленные по поколениям как угодно (в обход диспетчера)."""

    def __init__(self, db: aiosqlite.Connection, task_id: int, template: dict) -> None:
        self.db = db
        self.task_id = task_id
        self.template = template
        self.ids: list[int] = []

    async def report(
        self, generation: int, *, copy_of: int | None = None, **cols: object
    ) -> int:
        """Отчёт в поколении. ``copy_of`` — копия переноса #1361: без токенов, со ссылкой."""
        row = {k: v for k, v in self.template.items() if k != "id"}
        row["submission_generation"] = generation
        if copy_of is not None:
            row["provider_tokens"] = None
            row["carried_from_review_id"] = copy_of
        row.update(cols)
        names = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = await self.db.execute(
            f"INSERT INTO machine_reviews ({names}) VALUES ({marks})",  # nosec B608
            tuple(row.values()),
        )
        await self.db.commit()
        new_id = int(cur.lastrowid or 0)
        self.ids.append(new_id)
        return new_id

    async def now(self, generation: int) -> None:
        await self.db.execute(
            "UPDATE tasks SET submission_generation=? WHERE id=?",
            (generation, self.task_id),
        )
        await self.db.commit()

    async def verdict_event(
        self,
        generation: int,
        verdict: str = "approved",
        *,
        open_findings: object = 0,
        self_approved: bool = False,
        ghost: int | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO events (kind, task_id, actor, payload) VALUES "
            "('review_verdict_recorded', ?, 'denis', ?)",
            (
                self.task_id,
                json.dumps(
                    {
                        "verdict": verdict,
                        "submission_generation": generation,
                        "open_in_scope_findings": open_findings,
                        "self_approved": self_approved,
                        **({"ghost_generation": ghost} if ghost is not None else {}),
                    }
                ),
            ),
        )
        await self.db.commit()

    async def seed_approvals(self) -> None:
        """Одобрение на каждом поколении до текущего, где вердикта ещё нет."""
        task = await self.task()
        current = int(task["submission_generation"])
        rows = await self.db.execute_fetchall(
            "SELECT MIN(submission_generation) FROM machine_reviews WHERE task_id=?",
            (self.task_id,),
        )
        first = rows[0][0]
        if first is None:
            return
        have = {
            json.loads(r[0]).get("submission_generation")
            for r in await self.db.execute_fetchall(
                "SELECT payload FROM events WHERE task_id=? AND "
                "kind='review_verdict_recorded'",
                (self.task_id,),
            )
            if isinstance(json.loads(r[0]), dict)
        }
        for generation in range(int(first), current):
            if generation not in have:
                await self.verdict_event(generation)

    async def task(self) -> dict:
        return dict(await repo.get_task(self.db, self.task_id) or {})


async def _world(client, db, monkeypatch, slug: str) -> World:
    task_id, ids = await _task(client, db, monkeypatch, slug, 1)
    template = dict(await repo.get_machine_review(db, ids[0]) or {})
    await db.execute("DELETE FROM machine_reviews WHERE id=?", (ids[0],))
    await db.commit()
    return World(db, task_id, template)


# ---- AC-1: сценарии, каждый обязан дать отказ и назвать узел ------------------

Scenario = Callable[[World, pytest.MonkeyPatch], Awaitable[list[int]]]


async def s_full_then_incomplete(w, _m):  # круг 1.1
    await w.report(1)
    return [await w.report(1, incomplete=1)]


async def s_sibling_confirmed_next_to_clean(w, _m):  # круг 1.2
    bad = await w.report(1, findings_confirmed=CONFIRMED)
    await w.report(1)
    return [bad]


async def s_self_review_without_identity(w, _m):  # круг 1.3
    return [await w.report(1, self_reviewed=0, principal_id=None)]


async def s_unresolved_next_to_clean(w, _m):  # круг 2.1
    bad = await w.report(1, unresolved=UNRESOLVED)
    await w.report(1)
    return [bad]


async def s_empty_report_with_claimed_tokens(w, _m):  # круг 2.2а
    return [await w.report(1, raw_count=0)]


async def s_full_then_empty(w, _m):  # круг 2.2б
    await w.report(1)
    return [await w.report(1, raw_count=0)]


async def s_no_provider_tokens(w, _m):  # круг 3.1
    return [await w.report(1, raw_count=1, provider_tokens=None)]


async def s_lost_dimensions(w, _m):  # круг 3.2
    return [await w.report(1, incomplete=0, lost_dimensions=LOST)]


async def s_text_origin_then_clean(w, _m):  # круг 3.3
    bad = await w.report(1, orchestrator="cursor-cloud-result:claimed")
    await w.report(1)
    return [bad]


async def s_answered_unresolved_is_not_clean(w, _m):  # круг 4.1
    bad = await w.report(1, unresolved=UNRESOLVED)
    (uid,) = unresolved_uids(json.loads(UNRESOLVED))
    await repo.upsert_finding_outcome(
        w.db,
        review_id=bad,
        task_id=w.task_id,
        submission_generation=1,
        finding_uid=uid,
        finding_index=0,
        finding_title="не проверено",
        outcome="real_fixed",
        note="исправлено",
        linked_task_id=None,
        reported_by="author",
        finding_kind="unresolved",
    )
    await w.db.commit()
    return [bad]


async def s_standing_changes_requested(w, _m):  # круг 4.2
    await w.report(1)
    finding = {"id": 1, "severity": "high", "message": "x", "scope": "in_scope"}
    await w.db.execute(
        "UPDATE tasks SET review_verdict='changes_requested', "
        "review_verdict_generation=1, review_findings=? WHERE id=?",
        (json.dumps([finding]), w.task_id),
    )
    await w.db.commit()
    return []


async def s_branch_moved_off_the_submission(w, m):  # круг 4.3
    await w.report(1)

    async def moved(*_a, **_k):
        return "b" * 40

    m.setattr(plugins.git_ops, "head_sha", moved)
    await observe_tip(w.db, w.task_id)
    return []


async def _broken_section(w, section: str, value: str) -> list[int]:
    return [await w.report(1, **{section: value})]


async def s_broken_json_section(w, _m):  # круг 4.6
    return await _broken_section(w, "findings_confirmed", "not json")


async def s_object_instead_of_list(w, _m):  # круг 4.6
    return await _broken_section(w, "unresolved", "{}")


async def s_empty_string_section(w, _m):  # круг 5.4
    return await _broken_section(w, "lost_dimensions", "")


async def s_approved_with_in_scope_finding(w, _m):  # круг 5.3
    await w.report(1)
    finding = {"id": 1, "severity": "high", "message": "x", "scope": "in_scope"}
    await w.db.execute(
        "UPDATE tasks SET review_verdict='approved', review_verdict_generation=1, "
        "review_findings=? WHERE id=?",
        (json.dumps([finding]), w.task_id),
    )
    await w.db.commit()
    return []


async def s_carry_one_of_several(w, _m):  # круг 5.1
    bad = await w.report(1, unresolved=UNRESOLVED)
    clean = await w.report(1)
    await w.report(2, copy_of=clean)
    await w.now(2)
    return [bad]


async def s_double_carry_loses_the_middle(w, _m):  # круг 6.1
    root = await w.report(1)
    bad = await w.report(2, unresolved=UNRESOLVED)
    await w.report(2, copy_of=root)
    await w.report(3, copy_of=root)
    await w.now(3)
    return [bad]


async def _dirty_copy(w, **cols: str) -> list[int]:
    root = await w.report(1)
    bad = await w.report(2, copy_of=root, **cols)
    await w.now(2)
    return [bad]


async def s_copy_with_unresolved(w, _m):  # круг 6.3
    return await _dirty_copy(w, unresolved=UNRESOLVED)


async def s_copy_with_lost_dimensions(w, _m):  # круг 6.3
    return await _dirty_copy(w, lost_dimensions=LOST)


async def s_copy_with_confirmed(w, _m):  # круг 6.3
    return await _dirty_copy(w, findings_confirmed=CONFIRMED)


async def s_copy_with_unreadable_section(w, _m):  # круг 6.3
    return await _dirty_copy(w, unresolved="не json")


async def _returned(w, generation: int) -> None:
    await w.db.execute(
        "INSERT INTO events (kind, task_id, actor, payload) VALUES "
        "('review_verdict_recorded', ?, 'denis', ?)",
        (
            w.task_id,
            json.dumps(
                {"verdict": "changes_requested", "submission_generation": generation}
            ),
        ),
    )
    await w.db.commit()


async def s_returned_root_generation_then_carry(w, _m):  # свежий взгляд
    root = await w.report(1)
    await _returned(w, 1)
    await w.report(2, copy_of=root)
    await w.now(2)
    return []


async def s_steward_returned_root_generation_then_carry(w, _m):  # свежий взгляд
    root = await w.report(1)
    await repo.insert_steward_judgement(
        w.db,
        task_id=w.task_id,
        generation=1,
        kind="verdict",
        submitted_verdict="changes_requested",
        verdict="changes_requested",
        confidence="high",
    )
    await w.db.commit()
    await w.report(2, copy_of=root)
    await w.now(2)
    return []


async def s_unreadable_verdict_history_behind_a_carry(w, _m):
    root = await w.report(1)
    await w.db.execute(
        "INSERT INTO events (kind, task_id, actor, payload) VALUES "
        "('review_verdict_recorded', ?, 'denis', '[1]')",
        (w.task_id,),
    )
    await w.db.commit()
    await w.report(2, copy_of=root)
    await w.now(2)
    return []


async def s_codex_20_10(
    w, _m
):  # круг 7: gen1 #1 unresolved + #2 чистый; gen2 #4; gen3 #3 = перенос #2
    bad = await w.report(1, unresolved=UNRESOLVED)
    second = await w.report(1)
    await w.report(2)
    await w.report(3, copy_of=second)
    await w.now(3)
    return [bad]


async def s_dirty_report_beyond_the_nearest_generation(w, _m):
    """gen1 A чистый; gen2 B грязный; gen3 C чистый самостоятельный; gen4 перенос A."""
    root = await w.report(1)
    bad = await w.report(2, unresolved=UNRESOLVED)
    await w.report(3)
    await w.report(4, copy_of=root)
    await w.now(4)
    return [bad]


async def s_long_chain_of_clean_copies_over_a_dirty_root(w, _m):
    root = await w.report(1, lost_dimensions=LOST)
    for generation in (2, 3, 4):
        await w.report(generation, copy_of=root)
    await w.now(4)
    return [root]


async def s_second_root_in_a_branch(w, _m):
    first = await w.report(1)
    bad = await w.report(2, incomplete=1)
    second = await w.report(2, incomplete=0)
    await w.report(3, copy_of=first)
    await w.report(4, copy_of=second)
    await w.now(4)
    return [bad]


async def s_root_never_said_it_was_complete(w, _m):
    root = await w.report(1, incomplete=None)
    await w.report(2, copy_of=root)
    await w.now(2)
    return [root]


async def s_unreadable_independent_report_in_the_middle(w, _m):
    root = await w.report(1)
    bad = await w.report(2, findings_rejected="not json")
    await w.report(3, copy_of=root)
    await w.now(3)
    return [bad]


async def s_source_missing(w, _m):
    bad = await w.report(2, copy_of=987654)
    await w.now(2)
    return [bad]


async def s_source_of_another_task(w, _m):
    other = await repo.create_task(
        w.db,
        title="other",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="bot",
        rationale="",
        status="review",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    alien = await w.report(1, task_id=other)
    bad = await w.report(2, copy_of=alien)
    await w.now(2)
    return [bad]


async def s_source_from_the_same_generation(w, _m):
    root = await w.report(2)
    bad = await w.report(2, copy_of=root)
    await w.now(2)
    return [bad]


async def s_source_from_a_later_generation(w, _m):
    later = await w.report(3)
    bad = await w.report(2, copy_of=later)
    await w.now(2)
    return [bad]


async def s_self_reference(w, _m):
    bad = await w.report(2)
    await w.db.execute(
        "UPDATE machine_reviews SET carried_from_review_id=id WHERE id=?", (bad,)
    )
    await w.db.commit()
    await w.now(2)
    return [bad]


async def s_cycle(w, _m):
    first = await w.report(2)
    second = await w.report(2, copy_of=first)
    await w.db.execute(
        "UPDATE machine_reviews SET carried_from_review_id=? WHERE id=?",
        (second, first),
    )
    await w.db.commit()
    await w.now(2)
    return [first]


async def _garbage_link(w, value: object) -> list[int]:
    await w.report(1)
    bad = await w.report(2, carried_from_review_id=value)
    await w.now(2)
    return [bad]


async def s_link_zero(w, _m):
    return await _garbage_link(w, 0)


async def s_link_negative(w, _m):
    return await _garbage_link(w, -5)


async def s_link_not_a_number(w, _m):
    return await _garbage_link(w, "abc")


async def s_graph_too_large(w, m):
    m.setattr(report_provenance, "MAX_NODES", 3)
    for _ in range(4):
        await w.report(1)
    return []


async def _closed_by(w, m, how: str) -> None:
    """Одобрение поколения 1 закрыто человеком настоящим сервисом; затем перенос."""
    root = await w.report(1)
    await w.verdict_event(1)
    await w.now(1)
    if how == "return":
        await services.return_to_work(
            w.db, w.task_id, TaskReturnToWork(reason="переделать"), actor="denis"
        )
    else:
        await w.db.execute(
            "UPDATE tasks SET status='needs_decision' WHERE id=?", (w.task_id,)
        )
        await w.db.commit()

        async def submitted(*_a, **_k):
            return {"job_id": "j1"}

        m.setattr(plugins.dispatch, "submit_task", submitted)
        await services.decide_task(
            w.db, w.task_id, TaskDecide(action="rework", instructions="не то"), who=None
        )
    await w.db.execute("UPDATE tasks SET status='review' WHERE id=?", (w.task_id,))
    await w.db.commit()
    await w.report(2, copy_of=root)
    await w.now(2)


async def s_approval_closed_by_return_to_work(w, m):  # круг 2 ревью #1912
    await _closed_by(w, m, "return")
    return []


async def s_approval_closed_by_decide_rework(w, m):  # круг 2 ревью #1912
    await _closed_by(w, m, "decide")
    return []


async def s_approval_closed_only_by_the_column(w, _m):
    root = await w.report(1)
    await w.verdict_event(1)
    await w.report(2, copy_of=root)
    await w.db.execute(
        "UPDATE tasks SET review_verdict_closed_generation=1 WHERE id=?", (w.task_id,)
    )
    await w.db.commit()
    await w.now(2)
    return []


async def s_generation_without_any_verdict(w, _m):
    root = await w.report(1)
    await w.report(2, copy_of=root)
    await w.now(2)
    return []


async def s_approved_then_replaced_by_changes_requested(w, _m):
    root = await w.report(1)
    await w.verdict_event(1)
    await w.verdict_event(1, "changes_requested")
    await w.report(2, copy_of=root)
    await w.now(2)
    return []


async def s_middle_generation_without_approval(w, _m):
    root = await w.report(1)
    await w.verdict_event(1)
    await w.report(2)
    await w.report(3, copy_of=root)
    await w.now(3)
    return []


async def s_carried_approval_with_in_scope_findings(w, _m):  # свежий взгляд 2
    root = await w.report(1)
    await w.verdict_event(1, open_findings=1)
    await w.report(2, copy_of=root)
    await w.now(2)
    return []


async def s_carried_approval_without_findings_data(w, _m):  # событие до #1912
    root = await w.report(1)
    await w.verdict_event(1, open_findings=None)
    await w.report(2, copy_of=root)
    await w.now(2)
    return []


async def s_carried_self_approval(w, _m):
    root = await w.report(1)
    await w.verdict_event(1, self_approved=True)
    await w.report(2, copy_of=root)
    await w.now(2)
    return []


async def s_phantom_generation_with_a_submission_row(w, _m):
    """Поколение с настоящей сдачей, но без вердикта — не призрак."""
    root = await w.report(1)
    await w.verdict_event(1)
    await w.db.execute(
        "INSERT INTO submissions (task_id, generation, sha) VALUES (?, 2, ?)",
        (w.task_id, "c" * 40),
    )
    await w.db.commit()
    await w.report(3, copy_of=root)
    await w.now(3)
    return []


async def s_generation_raised_by_a_done_report(w, _m):  # круг 3, проба 1
    """Поколение 2 поднято done-отчётом: ни сдачи, ни метки; оно не призрак."""
    root = await w.report(1)
    await w.verdict_event(1)
    await repo.bump_submission_generation(w.db, w.task_id)
    await w.db.commit()
    await w.report(3, copy_of=root)
    await w.now(3)
    return []


async def s_poller_changes_requested_without_an_event(w, _m):  # круг 3, проба 2
    root = await w.report(1)
    await w.verdict_event(1)
    await repo.bump_submission_generation(w.db, w.task_id)
    assert await repo.record_review_verdict(
        w.db, w.task_id, "changes_requested", expected_generation=2
    )
    await w.db.commit()
    await w.report(3, copy_of=root)
    await w.now(3)
    return []


async def _steward_cr(w, generation: int) -> None:
    await repo.insert_steward_judgement(
        w.db,
        task_id=w.task_id,
        generation=generation,
        kind="verdict",
        submitted_verdict="changes_requested",
        verdict="changes_requested",
        confidence="high",
        grounds=json.dumps([{"source": "ci_report", "detail": ""}]),
    )
    await w.db.commit()


async def s_steward_changes_requested_on_an_unmarked_generation(
    w, _m
):  # круг 3, проба 3
    root = await w.report(1)
    await w.verdict_event(1)
    await _steward_cr(w, 2)
    await w.report(3, copy_of=root)
    await w.now(3)
    return []


async def s_steward_changes_requested_on_a_marked_ghost(w, _m):
    root = await w.report(1)
    await w.verdict_event(1, ghost=2)
    await _steward_cr(w, 2)
    await w.report(3, copy_of=root)
    await w.now(3)
    return []


async def s_ghost_mark_on_a_generation_with_a_report(w, _m):
    root = await w.report(1)
    await w.verdict_event(1, ghost=2)
    await w.report(2)
    await w.report(3, copy_of=root)
    await w.now(3)
    return []


async def s_ghost_mark_that_does_not_follow_its_verdict(w, _m):
    """Метка призрака на поколении, не следующем за вердиктом, — не метка."""
    root = await w.report(1)
    await w.verdict_event(1, ghost=3)
    await w.verdict_event(2)
    await w.report(4, copy_of=root)
    await w.now(4)
    return []


async def s_ghost_mark_on_a_changes_requested_verdict(w, _m):
    root = await w.report(1)
    await w.verdict_event(1, "changes_requested", ghost=2)
    await w.report(3, copy_of=root)
    await w.now(3)
    return []


async def _raw_verdict_generation(w, value: str) -> None:
    await w.db.execute(
        "INSERT INTO events (kind, task_id, actor, payload) VALUES "
        "('review_verdict_recorded', ?, 'denis', ?)",
        (
            w.task_id,
            '{"verdict":"approved","open_in_scope_findings":0,'
            f'"submission_generation":{value}}}',
        ),
    )
    await w.db.commit()


async def s_approval_with_a_boolean_generation(w, _m):
    root = await w.report(1)
    await _raw_verdict_generation(w, "true")
    await w.report(2, copy_of=root)
    await w.now(2)
    return []


async def s_approval_with_a_float_generation(w, _m):
    root = await w.report(1)
    await _raw_verdict_generation(w, "1.0")
    await w.report(2, copy_of=root)
    await w.now(2)
    return []


UNAPPROVED = {
    "approval_with_a_boolean_generation",
    "approval_with_a_float_generation",
    "generation_raised_by_a_done_report",
    "poller_changes_requested_without_an_event",
    "steward_changes_requested_on_an_unmarked_generation",
    "steward_changes_requested_on_a_marked_ghost",
    "ghost_mark_on_a_generation_with_a_report",
    "ghost_mark_that_does_not_follow_its_verdict",
    "ghost_mark_on_a_changes_requested_verdict",
    "carried_approval_with_in_scope_findings",
    "carried_approval_without_findings_data",
    "carried_self_approval",
    "phantom_generation_with_a_submission_row",
    "approval_closed_by_return_to_work",
    "approval_closed_by_decide_rework",
    "approval_closed_only_by_the_column",
    "generation_without_any_verdict",
    "approved_then_replaced_by_changes_requested",
    "middle_generation_without_approval",
}

SCENARIOS: dict[str, Scenario] = {
    name[2:]: fn for name, fn in sorted(globals().items()) if name.startswith("s_")
}

#: случай корпуса #1864 -> сценарии, которые его воспроизводят (AC-3)
CASE_SCENARIOS = {
    "t1810-g1": [
        "full_then_incomplete",
        "sibling_confirmed_next_to_clean",
        "self_review_without_identity",
    ],
    "t1810-g2": ["unresolved_next_to_clean", "empty_report_with_claimed_tokens"],
    "t1810-g3": ["no_provider_tokens", "lost_dimensions", "text_origin_then_clean"],
    "t1810-g4": [
        "answered_unresolved_is_not_clean",
        "standing_changes_requested",
        "branch_moved_off_the_submission",
        "empty_string_section",
    ],
    "t1810-g5": ["carry_one_of_several", "approved_with_in_scope_finding"],
    "t1810-g6": ["double_carry_loses_the_middle", "copy_with_unresolved"],
}


async def _refused(client, db, name: str) -> list[str]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        return await _refused_in(client, db, monkeypatch, name)


async def _refused_in(client, db, monkeypatch, name: str) -> list[str]:
    """Прогон одного сценария: пусто, если «Принять» отказан, иначе — что не так."""
    world = await _world(client, db, monkeypatch, f"prov-{name}".replace("_", "-"))
    bad_ids = await SCENARIOS[name](world, monkeypatch)
    if name not in UNAPPROVED:
        await world.seed_approvals()
    task = await world.task()
    proof, blockers, _ = await prove_accept(db, task)
    brief = await decision_brief.build(db, task)
    problems = []
    if proof is not None or brief["accept_proven"]:
        problems.append("доказательство выдано")
    if brief["recommend"]["action"] == ACCEPT:
        problems.append("справка говорит «Принять»")
    named = " ".join(f"{b.risk} {b.why}" for b in blockers)
    problems += [f"узел #{i} не назван" for i in bad_ids if f"#{i}" not in named]
    return [f"{name}: {p}" for p in problems]


async def test_every_known_false_accept_is_refused(client, db: aiosqlite.Connection):
    problems: list[str] = []
    for name in SCENARIOS:
        problems += await _refused(client, db, name)
    assert problems == []


# ---- AC-2: чистые сдачи -------------------------------------------------------


async def p_one_report(w):
    await w.report(1)


async def p_two_clean_reports(w):
    await w.report(1)
    await w.report(1)


async def p_chain_of_clean_copies(w):
    root = await w.report(1)
    for generation in (2, 3, 4):
        await w.report(generation, copy_of=root)
    await w.now(4)


async def p_chain_with_non_collapsed_links(w):
    root = await w.report(1)
    middle = await w.report(2, copy_of=root)
    await w.report(3, copy_of=middle)
    await w.now(3)


async def p_generations_without_reports(w):
    root = await w.report(1)
    await w.report(4, copy_of=root)
    await w.now(4)


async def p_branch_of_copies_and_independent_report(w):
    root = await w.report(1)
    await w.report(2, copy_of=root)
    await w.report(2)
    await w.report(3, copy_of=root)
    await w.now(3)


async def p_phantom_generation_after_an_approval_with_a_moved_tip(w):
    """Одобрение при ушедшей вершине подняло номер (#572): поколение 2 пустое, помечено."""
    root = await w.report(1)
    await w.verdict_event(1, ghost=2)
    await w.report(3, copy_of=root)
    await w.now(3)


async def p_old_approved_verdict_behind_a_carry(w):
    root = await w.report(1)
    await w.db.execute(
        "INSERT INTO events (kind, task_id, actor, payload) VALUES "
        "('review_verdict_recorded', ?, 'denis', ?)",
        (
            w.task_id,
            json.dumps(
                {
                    "verdict": "approved",
                    "submission_generation": 1,
                    "open_in_scope_findings": 0,
                }
            ),
        ),
    )
    await w.db.commit()
    await w.report(2, copy_of=root)
    await w.now(2)


async def p_copy_next_to_an_independent_report(w):
    root = await w.report(1)
    await w.report(2, copy_of=root)
    await w.report(2)
    await w.now(2)


P_UNSEEDED = {"phantom_generation_after_an_approval_with_a_moved_tip"}

CLEAN = {
    name[2:]: fn for name, fn in sorted(globals().items()) if name.startswith("p_")
}


async def test_clean_provenance_accepts(client, db: aiosqlite.Connection):
    problems: list[str] = []
    for name, build in CLEAN.items():
        with pytest.MonkeyPatch.context() as monkeypatch:
            world = await _world(
                client, db, monkeypatch, f"clean-{name}".replace("_", "-")
            )
        await build(world)
        if name not in P_UNSEEDED:
            await world.seed_approvals()
        task = await world.task()
        brief = await decision_brief.build(db, task)
        rec = brief["recommend"]
        if (rec["action"], rec["verdict"]) != (ACCEPT, "approved"):
            problems.append(f"{name}: {rec['action']} {rec['why']}")
        inbox = await services.get_inbox_data(db)
        (row,) = [r for r in inbox["decision_queue"] if r["task_id"] == world.task_id]
        card = await web._card_decision_brief(db, task, False)
        if card is None or row["brief_line"] != decision_brief.recommend_line(card):
            problems.append(f"{name}: карточка и входящие расходятся")
        proof, _, _ = await prove_accept(db, task)
        if proof is None or set(proof.report_ids) != set(world.ids):
            problems.append(f"{name}: граф не охватил все отчёты")
    assert problems == []


async def test_a_steward_approve_alone_is_not_accept(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Суждение стюарда — не доказательство: без отчёта «Принять» нет."""
    world = await _world(client, db, monkeypatch, "prov-steward-alone")
    task = await world.task()
    await repo.insert_steward_judgement(
        db,
        task_id=world.task_id,
        generation=int(task["submission_generation"]),
        kind="verdict",
        submitted_verdict="approve",
        verdict="approve",
        confidence="high",
        grounds=json.dumps([{"source": "ci_report", "detail": ""}]),
    )
    await db.commit()
    got = await decision_brief.build(db, task)
    assert got["recommend"]["action"] != ACCEPT


async def test_the_graph_never_touches_the_network(
    client, db: aiosqlite.Connection, monkeypatch
):
    world = await _world(client, db, monkeypatch, "prov-nonet")
    root = await world.report(1)
    await world.report(2, copy_of=root)
    await world.now(2)
    calls: list[str] = []

    def spy(name: str):
        async def _spy(*_a, **_k):
            calls.append(name)
            return ("b" * 40) if name == "head_sha" else (True, "")

        return _spy

    monkeypatch.setattr(plugins.git_ops, "fetch_base", spy("fetch_base"))
    monkeypatch.setattr(plugins.git_ops, "head_sha", spy("head_sha"))
    task = await world.task()
    await decision_brief.build(db, task)
    await services.get_inbox_data(db)
    await web._card_decision_brief(db, task, False)
    assert calls == []


# ---- AC-3: корпус промахов #1864 ----------------------------------------------


def _corpus_defects() -> list[dict]:
    corpus = ev.load_corpus(CORPUS)
    return [c for c in corpus["cases"] if c["expectation"] == "defect"]


def test_every_accept_case_of_the_corpus_has_a_scenario():
    """Новый случай ложного «Принять» в корпусе без сценария — красный тест."""
    ids = {
        c["case_id"]
        for c in _corpus_defects()
        if c["defect_class"] == "accept_false_positive"
    }
    assert ids == set(CASE_SCENARIOS)
    for names in CASE_SCENARIOS.values():
        assert set(names) <= set(SCENARIOS)


async def test_corpus_defects_never_accept(client, db: aiosqlite.Connection):
    problems: list[str] = []
    for case in _corpus_defects():
        if case["defect_class"] == "accept_false_positive":
            for name in CASE_SCENARIOS[case["case_id"]]:
                problems += [
                    f"{case['case_id']}/{p}" for p in await _refused(client, db, name)
                ]
            continue
        # Остальные дефекты: ревьюер нашёл (confirmed) или заподозрил (unresolved)
        # дефект по локатору случая — справка не принимает ни в одном из видов.
        locator = case["locators"][0] if case.get("locators") else {}
        finding = json.dumps(
            [
                {
                    "title": case["case_id"],
                    "severity": "high",
                    "category": "correctness",
                    "file": locator.get("file", "hub/x.py"),
                    "line": locator.get("start_line", 1),
                    "locator": "lines",
                    "start_line": locator.get("start_line", 1),
                    "detail": case.get("note", "d"),
                }
            ]
        )
        for kind, cols in (
            ("confirmed", {"findings_confirmed": finding}),
            ("unresolved", {"unresolved": UNRESOLVED}),
        ):
            with pytest.MonkeyPatch.context() as monkeypatch:
                world = await _world(
                    client, db, monkeypatch, f"corpus-{case['case_id']}-{kind}"
                )
            await world.report(1, **cols)
            proof, _, _ = await prove_accept(db, await world.task())
            if proof is not None:
                problems.append(f"{case['case_id']}/{kind}: доказательство выдано")
    assert problems == []


async def _real_verdict(client, w, findings: list[dict]) -> None:
    resp = await client.post(
        f"/api/tasks/{w.task_id}/review-verdict",
        json={
            "verdict": "approved",
            "agent": "denis",
            "findings": findings,
            "expected_generation": 1,
        },
    )
    assert resp.status_code == 200, resp.text


async def _resubmitted_with_carry(w, root: int) -> None:
    await w.db.execute("UPDATE tasks SET status='review' WHERE id=?", (w.task_id,))
    await w.db.commit()
    await w.report(2, copy_of=root)
    await w.now(2)


async def test_a_real_approval_carries_its_findings_count(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Событие настоящего одобрения несёт число открытых находок; чистое — «Принять», с находкой — нет."""
    outcomes = {}
    for label, findings in (
        ("clean", []),
        (
            "in_scope",
            [{"id": 1, "severity": "high", "message": "дефект", "scope": "in_scope"}],
        ),
    ):
        w = await _world(
            client, db, monkeypatch, f"prov-real-{label}".replace("_", "-")
        )
        root = await w.report(1)
        await _real_verdict(client, w, findings)
        await _resubmitted_with_carry(w, root)
        proof, _, _ = await prove_accept(db, await w.task())
        outcomes[label] = proof is not None
    assert outcomes == {"clean": True, "in_scope": False}


async def test_a_real_moved_tip_approval_marks_its_ghost_generation(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Настоящее одобрение при ушедшей вершине (#572) помечает поднятое поколение; пересдача и перенос — «Принять»."""
    w = await _world(client, db, monkeypatch, "prov-real-ghost")
    root = await w.report(1)
    pinned = (await w.task())["submission_sha"]
    plugins.git_ops._tip = "b" * 40
    try:
        resp = await client.post(
            f"/api/tasks/{w.task_id}/review-verdict",
            json={
                "verdict": "approved",
                "agent": "denis",
                "findings": [],
                "expected_generation": 1,
            },
        )
    finally:
        plugins.git_ops._tip = pinned
    assert resp.status_code == 200, resp.text
    task = await w.task()
    assert task["submission_generation"] == 2
    events = await db.execute_fetchall(
        "SELECT payload FROM events WHERE task_id=? AND kind='review_verdict_recorded'",
        (w.task_id,),
    )
    payload = json.loads(events[-1][0])
    assert (payload["submission_generation"], payload["ghost_generation"]) == (1, 2)
    await db.execute(
        "UPDATE tasks SET status='review', submission_sha=? WHERE id=?",
        (pinned, w.task_id),
    )
    await db.commit()
    await w.report(3, copy_of=root)
    await w.now(3)
    await observe_tip(db, w.task_id)
    proof, _, _ = await prove_accept(db, await w.task())
    assert proof is not None


async def test_the_event_names_the_generation_the_verdict_was_written_on(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Пересдача между чтением задачи и записью вердикта: событие называет записанную строку."""
    w = await _world(client, db, monkeypatch, "prov-race")
    await w.report(1)
    real_record = repo.record_review_verdict

    async def move_then_record(conn, *args, **kwargs):
        # пересдача между снимком задачи и записью: вердикт ляжет на поколение 2
        await repo.bump_submission_generation(conn, w.task_id)
        return await real_record(conn, *args, **kwargs)

    monkeypatch.setattr(repo, "record_review_verdict", move_then_record)
    resp = await client.post(
        f"/api/tasks/{w.task_id}/review-verdict",
        json={"verdict": "approved", "agent": "denis", "findings": []},
    )
    assert resp.status_code == 200, resp.text
    task = await w.task()
    events = await db.execute_fetchall(
        "SELECT payload FROM events WHERE task_id=? AND kind='review_verdict_recorded'",
        (w.task_id,),
    )
    assert (
        json.loads(events[-1][0])["submission_generation"]
        == (task["review_verdict_generation"])
    )
