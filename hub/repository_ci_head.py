"""SQL журнала запусков CI на голове ветки задачи до сдачи (#1862).

Рядом с ``hub.repository``, а не в нём: файл у потолка размера
(docs/agent-context/file-size-budget.json), а новая тема - как раз тот случай,
когда бюджет просит выносить, а не растить.

Ключ журнала - пара (задача, sha). Резерв запроса ставится ОДНИМ атомарным
UPDATE в транзакции записи и коммитится до платного вызова наружу: два цикла на
двух соединениях не могут оба занять одну попытку.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub.db import fetchall, write_transaction

#: Всего запросов на (задача, sha), включая повтор после неоднозначного исхода.
MAX_REQUESTS = 2
#: Состояния, из которых запрос больше не просят.
SETTLED = ("requested", "observed", "declined", "exhausted")


async def running_candidates(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Задачи в running с веткой, без PR и без сдачи, по возрастанию id."""
    rows = await fetchall(
        db,
        "SELECT id, branch FROM tasks WHERE status='running' AND archived=0 "
        "AND COALESCE(branch, '') != '' AND submission_generation=0 "
        "AND COALESCE(submission_sha, '') = '' "
        "AND (pr_number IS NULL OR pr_number=0) ORDER BY id",
        (),
    )
    return [dict(r) for r in rows]


async def observe_head(
    db: aiosqlite.Connection,
    task_id: int,
    sha: str,
    branch: str,
    *,
    grace_seconds: int,
    retry_seconds: int,
) -> dict[str, Any]:
    """Записать голову (первое наблюдение остаётся) и вернуть строку с признаками сроков."""
    async with write_transaction(db):
        await db.execute(
            "INSERT OR IGNORE INTO ci_head_runs (task_id, sha, branch) "
            "VALUES (?, ?, ?)",
            (task_id, sha, branch),
        )
    rows = await fetchall(
        db,
        "SELECT *, first_seen_at <= datetime('now', ?) AS grace_over, "
        "COALESCE(reserved_at <= datetime('now', ?), 0) AS retry_over "
        "FROM ci_head_runs WHERE task_id=? AND sha=?",
        (
            f"-{int(grace_seconds)} seconds",
            f"-{int(retry_seconds)} seconds",
            task_id,
            sha,
        ),
    )
    return dict(rows[0])


async def reserve_request(
    db: aiosqlite.Connection,
    task_id: int,
    sha: str,
    *,
    grace_seconds: int,
    retry_seconds: int,
) -> bool:
    """Занять попытку запроса по (задача, sha); True - попытка ваша.

    Первая - из состояния seen после grace; вторая - из reserved (исход первой
    неоднозначен) после срока повтора. Больше ``MAX_REQUESTS`` не бывает.
    """
    async with write_transaction(db):
        cur = await db.execute(
            "UPDATE ci_head_runs SET state='reserved', attempts=attempts+1, "
            "reserved_at=datetime('now') WHERE task_id=? AND sha=? AND attempts<? "
            "AND ((state='seen' AND first_seen_at <= datetime('now', ?)) "
            "OR (state='reserved' AND reserved_at <= datetime('now', ?)))",
            (
                task_id,
                sha,
                MAX_REQUESTS,
                f"-{int(grace_seconds)} seconds",
                f"-{int(retry_seconds)} seconds",
            ),
        )
        return (cur.rowcount or 0) == 1


async def settle(
    db: aiosqlite.Connection,
    task_id: int,
    sha: str,
    state: str,
    *,
    note: str = "",
    only_from: tuple[str, ...] = ("seen", "reserved"),
) -> bool:
    """Перевести строку в итоговое состояние; True, если перевод сделан этим вызовом."""
    marks = ",".join("?" for _ in only_from)
    async with write_transaction(db):
        cur = await db.execute(
            "UPDATE ci_head_runs SET state=?, note=? WHERE task_id=? AND sha=? "  # nosec B608 - placeholders only
            f"AND state IN ({marks})",
            (state, note, task_id, sha, *only_from),
        )
        return (cur.rowcount or 0) == 1


async def unannounced_verified(
    db: aiosqlite.Connection, limit: int = 50
) -> list[dict[str, Any]]:
    """Подтверждённые попытки отчёта по головам журнала, ещё не объявленные.

    Новые первыми: строка, которую нельзя объявить (контракт сменился после
    подтверждения), остаётся в выборке, и старые такие не должны вытеснять новые.
    """
    rows = await fetchall(
        db,
        "SELECT a.* FROM ci_report_attempts a JOIN ci_head_runs j "
        "ON j.task_id=a.task_id AND j.sha=a.head_sha "
        "WHERE a.trust='verified' AND NOT EXISTS (SELECT 1 FROM ci_head_announced n "
        "WHERE n.task_id=a.task_id AND n.sha=a.head_sha "
        "AND n.attempt_key = a.run_id || ':' || a.run_attempt) "
        "ORDER BY a.id DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


async def claim_announcement(
    db: aiosqlite.Connection, task_id: int, sha: str, attempt_key: str
) -> bool:
    """Занять объявление (задача, sha, попытка); False - его уже сделали."""
    cur = await db.execute(
        "INSERT OR IGNORE INTO ci_head_announced (task_id, sha, attempt_key) "
        "VALUES (?, ?, ?)",
        (task_id, sha, attempt_key),
    )
    return (cur.rowcount or 0) == 1
