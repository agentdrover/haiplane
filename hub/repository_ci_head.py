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
SETTLED = ("observed", "declined", "exhausted")
#: Открытые состояния: по sha ещё нет прогона, и заявка может вернуться в очередь.
OPEN_STATES = ("seen", "reserved", "requested")
#: Ключ сохраняемого курсора очереди объявлений.
ANNOUNCE_CURSOR = "announce_cursor"


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
    branch: str,
    *,
    grace_seconds: int,
    retry_seconds: int,
) -> bool:
    """Занять попытку запроса по (задача, sha); True - попытка ваша.

    Первая - из состояния seen после grace; вторая - из reserved (исход первой
    неоднозначен) после срока повтора. Больше ``MAX_REQUESTS`` не бывает. Область
    задачи (running, эта ветка, нет сдачи и PR) проверяется В ЭТОМ ЖЕ UPDATE:
    выборка кандидатов и сетевые вызовы между ней и резервом задачу меняют.
    """
    async with write_transaction(db):
        cur = await db.execute(
            "UPDATE ci_head_runs SET state='reserved', attempts=attempts+1, "
            "reserved_at=datetime('now') WHERE task_id=? AND sha=? AND attempts<? "
            "AND ((state='seen' AND first_seen_at <= datetime('now', ?)) "
            "OR (state IN ('reserved', 'requested') "
            "AND reserved_at <= datetime('now', ?))) "
            "AND EXISTS (SELECT 1 FROM tasks t WHERE t.id=ci_head_runs.task_id "
            "AND " + _TASK_IN_SCOPE + ")",  # nosec B608 - constant fragment
            (
                task_id,
                sha,
                MAX_REQUESTS,
                f"-{int(grace_seconds)} seconds",
                f"-{int(retry_seconds)} seconds",
                branch,
            ),
        )
        return (cur.rowcount or 0) == 1


#: Условия «задача всё ещё в области»: общие для резерва и для проверки перед dispatch.
_TASK_IN_SCOPE = (
    "t.status='running' AND t.archived=0 AND t.branch=? "
    "AND t.submission_generation=0 AND COALESCE(t.submission_sha, '')='' "
    "AND (t.pr_number IS NULL OR t.pr_number=0)"
)


async def task_in_scope(db: aiosqlite.Connection, task_id: int, branch: str) -> bool:
    """Задача СЕЙЧАС в running, на этой ветке, без сдачи и PR (читается перед dispatch)."""
    rows = await fetchall(
        db,
        "SELECT 1 FROM tasks t WHERE t.id=? AND " + _TASK_IN_SCOPE,  # nosec B608 - constant
        (task_id, branch),
    )
    return bool(rows)


async def adopt_unplanned(
    db: aiosqlite.Connection, task_id: int, sha: str, branch: str
) -> None:
    """Голова сдвинулась, пока шёл запрос: прогон мог пойти по НОВОЙ голове.

    Новая голова записывается как «запрошена, исход неизвестен»: следующий цикл
    сначала ищет её прогон, а второй запрос возможен только после срока повтора.
    """
    async with write_transaction(db):
        await db.execute(
            "INSERT OR IGNORE INTO ci_head_runs (task_id, sha, branch) "
            "VALUES (?, ?, ?)",
            (task_id, sha, branch),
        )
        await db.execute(
            "UPDATE ci_head_runs SET state='reserved', attempts=1, "
            "reserved_at=datetime('now') WHERE task_id=? AND sha=? AND state='seen'",
            (task_id, sha),
        )


async def settle(
    db: aiosqlite.Connection,
    task_id: int,
    sha: str,
    state: str,
    *,
    note: str = "",
    only_from: tuple[str, ...] = OPEN_STATES,
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


#: Строк в одной странице объявлений и страниц за проход.
ANNOUNCE_PAGE = 50
ANNOUNCE_PAGES = 4


async def unannounced_verified(
    db: aiosqlite.Connection, limit: int = ANNOUNCE_PAGE, before_id: int | None = None
) -> list[dict[str, Any]]:
    """Подтверждённые попытки отчёта по головам журнала, ещё не объявленные.

    Новые первыми и постранично (``before_id``): строка, которую нельзя
    объявить (контракт сменился после подтверждения), остаётся в выборке, и
    пачка таких не должна вытеснять объявляемые за границу страницы.
    """
    rows = await fetchall(
        db,
        "SELECT a.* FROM ci_report_attempts a JOIN ci_head_runs j "
        "ON j.task_id=a.task_id AND j.sha=a.head_sha "
        "WHERE a.trust='verified' AND (? IS NULL OR a.id < ?) "
        "AND NOT EXISTS (SELECT 1 FROM ci_head_announced n "
        "WHERE n.task_id=a.task_id AND n.sha=a.head_sha "
        "AND n.attempt_key = a.run_id || ':' || a.run_attempt) "
        "ORDER BY a.id DESC LIMIT ?",
        (before_id, before_id, limit),
    )
    return [dict(r) for r in rows]


async def get_attempt(
    db: aiosqlite.Connection, attempt_id: int
) -> dict[str, Any] | None:
    """Строка попытки отчёта СЕЙЧАС (внутри транзакции объявления)."""
    rows = await fetchall(
        db, "SELECT * FROM ci_report_attempts WHERE id=?", (attempt_id,)
    )
    return dict(rows[0]) if rows else None


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


async def get_state(db: aiosqlite.Connection, key: str) -> int:
    """Сохраняемое число прохода (курсор); 0, если не записано."""
    rows = await fetchall(db, "SELECT value FROM ci_head_state WHERE key=?", (key,))
    return int(rows[0]["value"]) if rows else 0


async def set_state(db: aiosqlite.Connection, key: str, value: int) -> None:
    async with write_transaction(db):
        await db.execute(
            "INSERT INTO ci_head_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, int(value)),
        )


async def overdue_waits(
    db: aiosqlite.Connection, minutes: int, limit: int = 20
) -> list[dict[str, Any]]:
    """Головы, чьего итога ждут дольше срока и о ком ещё не сказано вслух.

    Только заявленные или замеченные (``requested``/``observed``) головы задачи
    в running, и только ПОСЛЕДНЯЯ голова журнала задачи: прежние перекрыты
    новыми коммитами и ждать от них нечего. Итог объявлен (любая реальная
    попытка в ``ci_head_announced``) или это ожидание уже названо
    (``timeout:<число заявок>``) - строки нет.
    """
    rows = await fetchall(
        db,
        "SELECT j.task_id, j.sha, j.branch, j.attempts, "
        "CAST((julianday('now') - julianday(COALESCE(j.reserved_at, "
        "j.first_seen_at))) * 1440 AS INTEGER) AS waited "
        "FROM ci_head_runs j JOIN tasks t ON t.id=j.task_id "
        "WHERE j.state IN ('requested', 'observed') AND t.status='running' "
        "AND t.archived=0 "
        "AND COALESCE(j.reserved_at, j.first_seen_at) <= datetime('now', ?) "
        "AND j.rowid = (SELECT MAX(x.rowid) FROM ci_head_runs x "
        "WHERE x.task_id=j.task_id) "
        "AND NOT EXISTS (SELECT 1 FROM ci_head_announced n "
        "WHERE n.task_id=j.task_id AND n.sha=j.sha AND "
        "(n.attempt_key NOT LIKE 'timeout:%' "
        "OR n.attempt_key = 'timeout:' || j.attempts)) "
        "ORDER BY j.task_id LIMIT ?",
        (f"-{int(minutes)} minutes", limit),
    )
    return [dict(r) for r in rows]


async def latest_run_id(db: aiosqlite.Connection, task_id: int, sha: str) -> str:
    """Номер прогона из самого свежего отчёта по голове; '' - отчёта нет."""
    rows = await fetchall(
        db,
        "SELECT run_id FROM ci_report_attempts WHERE task_id=? AND head_sha=? "
        "AND run_id != '' ORDER BY id DESC LIMIT 1",
        (task_id, sha),
    )
    return str(rows[0]["run_id"]) if rows else ""
