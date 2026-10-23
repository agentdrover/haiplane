"""Ждать ли судье стюарда отчёта CI по сдаче (#1695).

Наблюдено 14.10.2026: #1691 и #1692 — судья стартовал, пока шёл прогон PR, и
вынес escalate по ``ci_pinned_sha absent`` (``current_attempt_incomplete``).
Через 2–15 минут отчёт стал ``verified``, но суждение на (task, generation,
kind) одно (#1022) и не пересчитывается: эскалация ложная, прогон оплачен.

Этот модуль только ЧИТАЕТ: ответ даёт ``ci_wait_view``, а страж
``steward_dispatch.review_wait_gate`` решает, держать ли заказ. Читатель
смотрит в ``ci_run_reports`` (проекция по коммиту) и ``ci_report_attempts``
(попытки) и не ходит к форжу. Правило не зависит от ``ci_before_submit``: тот
ключ по умолчанию ``off`` и не означает «CI нет».

НЕ ждать:
  * ``trust=verified`` (зелёный или красный — судья прочтёт и то и другое);
  * ``trust=unverified`` и у строки ТЕКУЩЕЙ попытки (``attempt_key`` проекции)
    ``trust_final=1``. Окончательность прошлой попытки не в счёт;
  * окончательный ``no_provenance`` (#2027): ключа попытки нет, строка учёта без
    идентификаторов (``ci_final_unprovenanced``); состояние в ленте называет причину.

Ждать (в пределах окна STEWARD_REVIEW_WAIT_MAX, общего с ожиданием ревью):
  * отчёта по коммиту нет (неизвестность: первый прогон, missing_run, проект
    без CI) — отсутствие данных не данные;
  * ``attempt_key`` проекции пуст или без строки попытки;
  * ``trust=unverified`` и ``trust_final=0``, любой временный reason.

Сдача без закреплённого коммита ждать нечего: ответ «не ждём».
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall, write_transaction
from hub.services import ci_contract, ci_final_unprovenanced, ci_trust

EVENT_JUDGED_WITHOUT_CI = "steward_judges_without_verified_ci"


@dataclass(frozen=True)
class CiWait:
    """``waiting`` — отчёт ещё может прийти; ``state`` — фраза для ленты."""

    waiting: bool
    state: str = ""


NOT_WAITING = CiWait(False)


async def ci_wait_view(db: aiosqlite.Connection, task: dict[str, Any]) -> CiWait:
    """Единый читатель: есть ли смысл ждать подтверждённый CI этой сдачи."""
    task_id = int(task["id"])
    # Поллер стюарда выбирает только id и поколение (как и review_wait_view):
    # закреплённый коммит читается из строки задачи, а не из переданного среза.
    full = await repo.get_task(db, task_id)
    sha = str((dict(full) if full is not None else task).get("submission_sha") or "")
    sha = sha.strip()
    if not sha:
        return NOT_WAITING
    # ОДИН запрос: проекция и строка её текущей попытки читаются из одного
    # снимка. Двумя запросами обновление CI между ними подсунуло бы
    # окончательность попытки A под ключом уже переключённой проекции на B.
    rows = await fetchall(
        db,
        "SELECT r.trust, r.trust_reason, r.attempt_key, r.contract_fp, "
        "a.id AS attempt_id, a.trust_final, a.contract_fp AS attempt_fp FROM ci_run_reports r LEFT JOIN ci_report_attempts a "
        "ON a.task_id=r.task_id AND a.head_sha=r.head_sha "
        "AND r.attempt_key != '' AND a.run_id != '' AND a.run_attempt != '' "
        "AND 'ci:' || a.run_id || ':' || a.run_attempt = r.attempt_key "
        "WHERE r.task_id=? AND r.head_sha=?",
        (task_id, sha),
    )
    if not rows:
        return CiWait(True, "отчёта CI по закреплённому коммиту нет")
    row = dict(rows[0])
    fp = await ci_contract.fingerprint_for_task(db, task_id)
    trust, reason = ci_trust.trust_of(row, fp)
    if trust == ci_trust.VERIFIED:
        return NOT_WAITING
    if row["attempt_id"] is None:
        # #2027: отчёт без provenance не имеет ключа попытки, JOIN его не
        # находит, а подтверждения не будет никогда.
        if await ci_final_unprovenanced.is_final_no_provenance(
            db, task_id, sha, row, fp
        ):
            return CiWait(False, ci_final_unprovenanced.STATE)
        key = str(row.get("attempt_key") or "")
        return CiWait(
            True,
            f"unverified({reason}): у текущей попытки {key or '—'} нет строки учёта",
        )
    if int(row.get("trust_final") or 0) and row.get("attempt_fp") == fp:
        return NOT_WAITING
    return CiWait(True, f"unverified({reason}), проверка не окончательна")


async def note_judged_without_ci(
    db: aiosqlite.Connection, task_id: int, generation: int, note: str
) -> None:
    """Одна строка в ленте задачи на поколение: судим без подтверждённого CI.

    Строка — запись task_updates (её видит человек в карточке) и событие для
    надзора. Дедупликация по метке поколения в тексте записи, «проверил —
    вставил» под BEGIN IMMEDIATE: два тика не пишут две строки.
    """
    mark = f"[ci-wait gen {generation}]"
    async with write_transaction(db):
        rows = await fetchall(
            db,
            "SELECT 1 FROM task_updates WHERE task_id=? AND content LIKE ? LIMIT 1",
            (task_id, f"%{mark}%"),
        )
        if rows:
            return
        await repo.add_task_update(
            db, task_id, "hub", "status", f"{note} {mark}", author_kind="hub"
        )
        await repo.insert_event(
            db,
            kind=EVENT_JUDGED_WITHOUT_CI,
            task_id=task_id,
            actor="hub",
            payload={"generation": generation, "detail": note},
        )
