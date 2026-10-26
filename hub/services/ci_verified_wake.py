"""CI закреплённого коммита стал verified: будим тех, кто ждал именно этого (#2054).

Приём отчёта CI раньше никого не будил. Автопилота зовёт только приход машинного
отчёта, а отказ применения approve стюарда (``failed``) поллер больше не берёт - даже
когда причина была переходящей: ``ci_not_green`` (отчёт CI ещё не подтверждён) и
сбои потолка класса. Чистая сдача уходила человеку, хотя через минуту ничто не мешало.

Сюда входят по приёму отчёта CI, когда он применён к закреплённому коммиту и GitHub
его подтвердил. Переход ловится не сравнением «было / стало» (запись отчёта сама
сбрасывает доверие до подтверждения), а событием ``ci_verified_wake``: одно на
(задача, поколение, коммит). Второй отчёт того же коммита событие уже находит и
ничего не делает - ни звонков, ни записей.

Что делает ОДИН раз: спрашивает автопилота (если по текущей сдаче уже есть машинный
отчёт) и повторяет применение approve стюарда, упавшее переходящей причиной.
Постоянные отказы и гонки 409 не повторяются (см. ``refusal_reason``). Всё
best-effort: приём отчёта не должен падать оттого, что будить не получилось.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall, write_transaction

log = logging.getLogger("hub.ci_verified_wake")

EVENT_CI_VERIFIED_WAKE = "ci_verified_wake"


async def _first_wake(
    db: aiosqlite.Connection, task_id: int, generation: int, head_sha: str
) -> bool:
    """Занять переход «verified» для (поколение, коммит): ``True`` - занят этим вызовом."""
    async with write_transaction(db):
        seen = await fetchall(
            db,
            "SELECT 1 FROM events WHERE kind=? AND task_id=? "
            "AND json_extract(payload, '$.generation')=? "
            "AND json_extract(payload, '$.head_sha')=?",
            (EVENT_CI_VERIFIED_WAKE, task_id, generation, head_sha),
        )
        if seen:
            return False
        await repo.insert_event(
            db,
            kind=EVENT_CI_VERIFIED_WAKE,
            task_id=task_id,
            actor="hub",
            payload={"generation": generation, "head_sha": head_sha},
        )
    return True


async def _has_current_report(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> bool:
    row = await repo.get_latest_machine_review(db, task_id)
    return row is not None and dict(row).get("submission_generation") == generation


async def wake_on_verified(
    db: aiosqlite.Connection, task_id: int, head_sha: str, *, applied: bool
) -> bool:
    """Разбудить автопилота и повторить применение; ``True`` - это первый переход."""
    if not applied:
        return False
    try:
        task: dict[str, Any] = dict(await repo.get_task(db, task_id) or {})
        generation = int(task.get("submission_generation") or 0)
        if generation <= 0 or not await _first_wake(db, task_id, generation, head_sha):
            return False
        await _wake(db, task_id, generation)
    except Exception:  # noqa: BLE001 - degradation is the contract
        log.exception("CI-verified wake failed for task #%s", task_id)
        return False
    return True


async def _wake(db: aiosqlite.Connection, task_id: int, generation: int) -> None:
    from hub.services.auto_verdict import maybe_auto_verdict
    from hub.services.steward_advisor import retry_transient_apply

    if await _has_current_report(db, task_id, generation):
        try:
            await maybe_auto_verdict(db, task_id)
        except Exception:  # noqa: BLE001 - the retry below must still run
            log.exception("auto-verdict after CI failed for task #%s", task_id)
    await retry_transient_apply(db, task_id)
