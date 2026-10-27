"""Окончательный отчёт CI без provenance: ждать подтверждения бессмысленно (#2027).

Отчёт, присланный без run_id/run_attempt (прямая отправка ключом записи),
хранится строкой попытки с пустыми идентификаторами, а ``ci_trust.check_attempt``
сразу выносит ему окончательное ``unverified(no_provenance)``: сколько ни жди,
подтверждения не будет. Читатель ожидания ``steward_ci_wait.ci_wait_view``
присоединяет строку попытки по ``attempt_key`` проекции, а у такого отчёта ключ
пуст, поэтому строка не находилась и стюард ждал весь потолок окна.

Модуль только читает. Он говорит «окончательно» лишь когда верно всё вместе:

* проекция по коммиту называет причину no_provenance и у неё НЕТ ключа попытки
  (иначе текущая попытка GitHub — другая и её отчёта ещё нет: ждём);
* у коммита есть строка попытки без идентификаторов, окончательная, с той же
  причиной;
* она вынесена под ТЕКУЩИМ контрактом проекта (после смены контракта вердикт
  устарел, перепроверка впереди: ждём).
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub.db import fetchall
from hub.services import ci_trust

STATE = "отчёт неподтверждаем: " + ci_trust.NO_PROVENANCE


async def is_final_no_provenance(
    db: aiosqlite.Connection,
    task_id: int,
    head_sha: str,
    projection: dict[str, Any],
    fingerprint: str,
) -> bool:
    """Окончательный ли no_provenance у отчёта этого коммита под контрактом ``fingerprint``."""
    if str(projection.get("attempt_key") or ""):
        return False
    if str(projection.get("trust_reason") or "") != ci_trust.NO_PROVENANCE:
        return False
    rows = await fetchall(
        db,
        "SELECT trust_final, trust_reason, contract_fp FROM ci_report_attempts "
        "WHERE task_id=? AND head_sha=? AND (run_id='' OR run_attempt='')",
        (task_id, head_sha),
    )
    return any(
        int(r["trust_final"] or 0)
        and r["trust_reason"] == ci_trust.NO_PROVENANCE
        and r["contract_fp"] == fingerprint
        for r in rows
    )
