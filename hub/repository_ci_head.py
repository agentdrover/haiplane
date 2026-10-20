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
#: Ключи сохраняемого состояния очереди объявлений: курсор (верхняя граница
#: следующей страницы, 0 - с вершины) и наибольший id, при котором курсор сброшен.
ANNOUNCE_CURSOR = "announce_cursor"
ANNOUNCE_TOP = "announce_top"


async def running_candidates(
    db: aiosqlite.Connection,
    *,
    after_id: int = 0,
    up_to_id: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Задачи в running с веткой, без PR и без сдачи, по возрастанию id.

    Выборка ограничена В SQL: ``id > after_id`` (и ``id <= up_to_id``, если
    задано) и не больше ``limit`` строк. Проход идёт по кругу страницами, а не
    читает все задачи за раз.
    """
    rows = await fetchall(
        db,
        "SELECT id, branch FROM tasks WHERE status='running' AND archived=0 "
        "AND COALESCE(branch, '') != '' AND submission_generation=0 "
        "AND COALESCE(submission_sha, '') = '' "
        "AND (pr_number IS NULL OR pr_number=0) AND id > ? "
        "AND (? IS NULL OR id <= ?) ORDER BY id LIMIT ?",
        (int(after_id), up_to_id, up_to_id, int(limit)),
    )
    return [dict(r) for r in rows]


async def _mark_current(db: aiosqlite.Connection, task_id: int, sha: str) -> None:
    """Сделать sha ТЕКУЩЕЙ головой задачи: наибольший порядок наблюдения.

    Возврат A->B->A снова ставит A последней; повторное наблюдение текущей
    головы ничего не меняет.
    """
    await db.execute(
        "UPDATE ci_head_runs SET seen_seq=(SELECT COALESCE(MAX(seen_seq), 0) + 1 "
        "FROM ci_head_runs) WHERE task_id=? AND sha=? AND (seen_seq=0 OR seen_seq < "
        "(SELECT MAX(seen_seq) FROM ci_head_runs WHERE task_id=?))",
        (task_id, sha, task_id),
    )


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
        await _mark_current(db, task_id, sha)
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


async def release_reservation(
    db: aiosqlite.Connection,
    task_id: int,
    sha: str,
    *,
    state: str,
    attempts: int,
    reserved_at: str | None,
) -> None:
    """Вернуть резерв, за которым dispatch НЕ последовал: попытка не потрачена.

    Откат к тому, что было до резерва; чужой переход (строка уже не reserved с
    нашей попыткой) не затирается.
    """
    async with write_transaction(db):
        await db.execute(
            "UPDATE ci_head_runs SET state=?, attempts=?, reserved_at=? "
            "WHERE task_id=? AND sha=? AND state='reserved' AND attempts=?",
            (state, attempts, reserved_at, task_id, sha, attempts + 1),
        )


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
        await _mark_current(db, task_id, sha)


async def settle(
    db: aiosqlite.Connection,
    task_id: int,
    sha: str,
    state: str,
    *,
    note: str = "",
    only_from: tuple[str, ...] = OPEN_STATES,
    attempts: int | None = None,
) -> bool:
    """Перевести строку в итоговое состояние; True, если перевод сделан этим вызовом.

    ``attempts`` - вернуть счётчик попыток к этому значению (отказ до отправки
    не тратит попытку и в итоговом состоянии).
    """
    marks = ",".join("?" for _ in only_from)
    async with write_transaction(db):
        cur = await db.execute(
            "UPDATE ci_head_runs SET state=?, note=?, "  # nosec B608 - placeholders only
            "attempts=COALESCE(?, attempts) WHERE task_id=? AND sha=? "
            f"AND state IN ({marks})",
            (state, note, attempts, task_id, sha, *only_from),
        )
        return (cur.rowcount or 0) == 1


#: Строк в одной странице объявлений и страниц за проход.
ANNOUNCE_PAGE = 50
ANNOUNCE_PAGES = 4


async def unannounced_verified(
    db: aiosqlite.Connection,
    limit: int = ANNOUNCE_PAGE,
    before_id: int | None = None,
    after_id: int = 0,
) -> list[dict[str, Any]]:
    """Подтверждённые попытки отчёта по головам журнала, ещё не объявленные.

    Новые первыми и постранично (``before_id``): строка, которую нельзя
    объявить (контракт сменился после подтверждения), остаётся в выборке, и
    пачка таких не должна вытеснять объявляемые за границу страницы.
    ``after_id`` - нижняя граница (строго выше): так читаются только НОВЫЕ строки.
    """
    rows = await fetchall(
        db,
        "SELECT a.* FROM ci_report_attempts a JOIN ci_head_runs j "
        "ON j.task_id=a.task_id AND j.sha=a.head_sha "
        "WHERE a.trust='verified' AND (? IS NULL OR a.id < ?) AND a.id > ? "
        "AND NOT EXISTS (SELECT 1 FROM ci_head_announced n "
        "WHERE n.task_id=a.task_id AND n.sha=a.head_sha "
        "AND n.attempt_key = a.run_id || ':' || a.run_attempt) "
        "ORDER BY a.id DESC LIMIT ?",
        (before_id, before_id, int(after_id), limit),
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
    db: aiosqlite.Connection,
    minutes: int,
    limit: int = 20,
    only: tuple[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Головы, чьего итога ждут дольше срока и о ком ещё не сказано вслух.

    Только головы задачи в running, по которым ждут итога (всё, кроме ``seen`` и
    ``declined``), и только ТЕКУЩАЯ голова (наибольший ``seen_seq``): прежние перекрыты
    новыми коммитами и ждать от них нечего. Итог объявлен (любая реальная
    попытка в ``ci_head_announced``) или это ожидание уже названо
    (``timeout:<число заявок>``) - строки нет. ``only`` - (задача, sha): то же
    условие для ОДНОЙ строки, чтобы перепроверить его внутри транзакции записи.
    """
    pin_task, pin_sha = only if only else (None, None)
    rows = await fetchall(
        db,
        "SELECT j.task_id, j.sha, j.branch, j.attempts, "
        "CAST((julianday('now') - julianday(COALESCE(j.reserved_at, "
        "j.first_seen_at))) * 1440 AS INTEGER) AS waited "
        "FROM ci_head_runs j JOIN tasks t ON t.id=j.task_id "
        "WHERE j.state IN ('reserved', 'requested', 'observed', 'exhausted') "
        "AND t.status='running' "
        "AND t.archived=0 AND (? IS NULL OR (j.task_id=? AND j.sha=?)) "
        "AND COALESCE(j.reserved_at, j.first_seen_at) <= datetime('now', ?) "
        "AND j.seen_seq = (SELECT MAX(x.seen_seq) FROM ci_head_runs x "
        "WHERE x.task_id=j.task_id) "
        "AND NOT EXISTS (SELECT 1 FROM ci_head_announced n "
        "WHERE n.task_id=j.task_id AND n.sha=j.sha AND "
        "(n.attempt_key NOT LIKE 'timeout:%' "
        "OR n.attempt_key = 'timeout:' || j.attempts)) "
        "ORDER BY j.task_id LIMIT ?",
        (pin_task, pin_task, pin_sha, f"-{int(minutes)} minutes", limit),
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


async def top_attempt_id(db: aiosqlite.Connection) -> int:
    """Наибольший id попытки отчёта: новая попытка возвращает очередь к вершине."""
    rows = await fetchall(
        db, "SELECT COALESCE(MAX(id), 0) AS top FROM ci_report_attempts"
    )
    return int(rows[0]["top"])


#: Задач, журналы которых очистка убирает за вызов (бюджет SQL).
PRUNE_BATCH = 200


async def prune_finished(
    db: aiosqlite.Connection, *, days: int, batch: int = PRUNE_BATCH
) -> int:
    """Убрать журналы задач, ЗАВЕРШЁННЫХ дольше ``days`` суток, порцией задач.

    Архивность сама по себе не повод: архив обратим, а журнал держит
    идемпотентность (число запросов CI на sha, объявленные прогоны). Задача,
    разархивированная после очистки, иначе заказала бы CI заново и объявила
    тот же прогон второй раз. Завершённая (терминальная) задача из архива не
    возвращается в работу, поэтому её журнал уходит независимо от архива.

    Порция - целые ЗАДАЧИ, обе таблицы сразу: маркер объявления не может уйти
    раньше своей головы, иначе отчёт по оставшейся голове объявился бы снова.
    """
    async with write_transaction(db):
        rows = await fetchall(
            db,
            "SELECT t.id FROM tasks t WHERE t.status IN "
            "('completed', 'failed', 'rejected') AND t.updated_at < datetime('now', ?) "
            "AND (EXISTS (SELECT 1 FROM ci_head_runs r WHERE r.task_id=t.id) "
            "OR EXISTS (SELECT 1 FROM ci_head_announced n WHERE n.task_id=t.id)) "
            "LIMIT ?",
            (f"-{int(days)} days", int(batch)),
        )
        ids = [int(r["id"]) for r in rows]
        removed = 0
        if ids:
            marks = ",".join("?" for _ in ids)
            for table in ("ci_head_announced", "ci_head_runs"):
                cur = await db.execute(
                    f"DELETE FROM {table} WHERE task_id IN ({marks})",  # nosec B608 - fixed names, placeholders
                    ids,
                )
                removed += cur.rowcount or 0
    return removed
