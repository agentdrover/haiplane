"""Пустой отчёт и режим двойной чистоты (#2082): у режима своё правило о пустоте.

Пара отчётов двойной чистоты обязана нести признаки исполнения и пин (#1986);
правило пустого отчёта автопилота (биллинг, потолок пустого ревью) к ней не
применяется, и это закреплено здесь, чтобы не сломать режим молча.
"""

from __future__ import annotations

from hub import repository as repo
from hub.models import ReviewVerdict
from hub.services.steward_applied import APPLIED, apply_self_approval
from tests.steward_double_clean_support import build_case


async def test_double_clean_pair_with_zero_candidates_keeps_its_own_rule(
    db, monkeypatch
):
    case = await build_case(
        db,
        monkeypatch,
        "dc-empty-pair",
        main={"raw": 0},
        extra={"raw": 0},
    )

    result = await apply_self_approval(db, case.task_id, 1)

    assert result is not None and result[0] == APPLIED, result
    task = dict(await repo.get_task(db, case.task_id))
    assert task["review_verdict"] == ReviewVerdict.approved.value
