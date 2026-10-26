"""CI закреплённого коммита стал verified: будим тех, кто ждал именно этого (#2054).

Приём отчёта CI раньше никого не будил. Автопилота зовёт только приход машинного
отчёта, а отказ применения approve стюарда (``failed``) поллер больше не берёт - даже
когда причина была переходящей: ``ci_not_green`` (отчёт CI ещё не подтверждён) и
сбои потолка класса. Чистая сдача уходила человеку, хотя через минуту ничто не мешало.

Сюда входят по приёму отчёта CI, когда он применён к закреплённому коммиту и GitHub
его подтвердил. Переход ловится не сравнением «было / стало» (запись отчёта сама
сбрасывает доверие до подтверждения): вход зовут при КАЖДОМ разборе проверенного
отчёта закреплённого коммита в review, а дедуп держат события. Автопилот - событие
``ci_verified_wake`` на (задача, поколение, коммит), и только когда по сдаче уже есть
машинный отчёт. Повтор стюарда - событие ``steward_apply_retry`` на суждение, и только
когда есть упавшее переходящей причиной применение. Из чужой транзакции или из
применения не будят: следующий разбор разбудит.
Постоянные отказы и гонки 409 не повторяются (см. ``refusal_reason``). Всё
best-effort: приём отчёта не должен падать оттого, что будить не получилось.
"""

from __future__ import annotations

import logging

import aiosqlite

from hub import repository as repo
from hub.db import fetchall, write_transaction

log = logging.getLogger("hub.ci_verified_wake")

EVENT_CI_VERIFIED_WAKE = "ci_verified_wake"


async def _first_wake(
    db: aiosqlite.Connection, task_id: int, generation: int, head_sha: str
) -> bool:
    """Занять переход «verified» для (поколение, коммит): ``True`` - занят этим вызовом.

    Одна транзакция: задача перечитывается и должна всё ещё стоять на том же
    поколении и закреплённом коммите, на которых отчёт стал verified, - иначе
    пересдача между проверкой и пробуждением съела бы чужой повтор.
    """
    async with write_transaction(db):
        task = dict(await repo.get_task(db, task_id) or {})
        if (
            int(task.get("submission_generation") or 0) != generation
            or (task.get("submission_sha") or "").strip() != head_sha
        ):
            return False
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


def _nested() -> bool:
    """Внутри применения стюарда или автопилота: будить из него нельзя (рекурсия)."""
    from hub.services import steward_apply

    return steward_apply._EARLY_RISK.get() is not None


async def wake_on_verified(
    db: aiosqlite.Connection, task_id: int, head_sha: str, generation: int
) -> bool:
    """Разбудить автопилота и повторить применение; ``True`` - это первый переход.

    ``generation`` - поколение, под которым проверка CI началась: пробуждение
    идёт только пока задача стоит на нём и на ``head_sha``. Вызов из чужой
    открытой транзакции или из применения не будит: проекция там не закоммичена
    либо будить из себя же нельзя.
    """
    if generation <= 0 or db.in_transaction or _nested():
        return False
    try:
        asked = await _ask_autopilot(db, task_id, generation, head_sha)
        retried = await _retry_steward(db, task_id, generation, head_sha)
    except Exception:  # noqa: BLE001 - degradation is the contract
        log.exception("CI-verified wake failed for task #%s", task_id)
        return False
    return asked or retried


async def _ask_autopilot(
    db: aiosqlite.Connection, task_id: int, generation: int, head_sha: str
) -> bool:
    """Автопилот - один раз на (поколение, коммит), и только когда есть что решать."""
    from hub.services.auto_verdict import maybe_auto_verdict

    if not await _has_current_report(db, task_id, generation):
        return False  # отчёта нет: автопилота позовёт сам приход отчёта
    if not await _first_wake(db, task_id, generation, head_sha):
        return False
    try:
        await maybe_auto_verdict(db, task_id)
    except Exception:  # noqa: BLE001 - the retry must still run
        log.exception("auto-verdict after CI failed for task #%s", task_id)
    return True


async def _retry_steward(
    db: aiosqlite.Connection, task_id: int, generation: int, head_sha: str
) -> bool:
    """Повтор применения стюарда: свой счётчик (одно на суждение), не событие пробуждения."""
    from hub.services.steward_advisor import retry_transient_apply

    return await retry_transient_apply(db, task_id, generation, head_sha)
