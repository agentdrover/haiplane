"""Влитый релиз не доехал до прода за срок — тревога стюарду (#1828).

17.10.2026 после #1785 job deploy пропускался на каждом push в main (#1796).
Хаб умел вычислять «в проде / не в проде» только по запросу (#497, #499), и
пропуск заметил человек. Здесь это делает свип поллера.

Факт релизного мержа хранится в ``release_merges`` и пишется самим релизом
(``record_merge``) сразу после успешного merge_pr — до чтения sha. Свип
проверяет достижимость КОНКРЕТНОГО ``release_sha`` строки от последнего
успешного выката проекта. Ровно три ветки, и ни одна не выдаёт незнание за
отрицание:

* достижим — молчание (а если по этой строке уже была тревога — одно событие
  ``release_deployed_late``, без сообщения);
* недостижим при известном успешном выкате — ``release_not_in_prod``;
* нет выката, git не ответил, sha релиза не прочитан —
  ``release_deploy_unobserved``: «нет данных о выкате», не «не выкатилось».

Сообщение стюарду уходит через ``notify_steward(reason="release")`` и только
при разрешённом политикой поводе. Дедуп — по (id строки, ветка).
Только репозиторий самого хаба: для чужого репозитория выкат хаб не пишет.
Задачи релиза в проверке не участвуют.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.integrations.registry import plugins
from hub.services import watch_notices
from hub.services.project_policy import forge_of

log = logging.getLogger("hub.release_deploy_watch")

NOT_IN_PROD = "release_not_in_prod"
UNOBSERVED = "release_deploy_unobserved"
DEPLOYED_LATE = "release_deployed_late"
_REACHABLE = "reachable"

# Строки старше окна не смотрятся: лента событий живёт 14 дней, и по более
# старой строке дедуп по событию потерялся бы.
LOOKBACK_DAYS = 7
# Репозиторий хаба живёт на GitHub (см. forge_of: так читается и отсутствие значения).
HUB_FORGE = "github"
# Недоступный коммит повторно тянется не чаще раза в это время: сетевой
# таймаут на каждую строку каждого тика задерживал бы остальные свипы.
FETCH_RETRY_SECONDS = 30 * 60
_fetch_failed: dict[tuple[int, str], float] = {}
_clock = time.monotonic


async def record_merge(
    db: aiosqlite.Connection, project_id: int, release_pr: int
) -> None:
    """Записать факт релизного мержа; sha ещё не прочитан. Повтор не плодит строк."""
    await db.execute(
        "INSERT OR IGNORE INTO release_merges (project_id, release_pr) VALUES (?, ?)",
        (project_id, int(release_pr)),
    )
    await db.commit()


async def set_sha(
    db: aiosqlite.Connection, project_id: int, release_pr: int, release_sha: str
) -> None:
    """Заполнить release_sha прочитанным значением; прочитанное не затирается."""
    await db.execute(
        "UPDATE release_merges SET release_sha = ? WHERE project_id = ? "
        "AND release_pr = ? AND COALESCE(release_sha, '') = ''",
        (release_sha.strip(), project_id, int(release_pr)),
    )
    await db.commit()


def _is_hub_project(project: dict[str, Any]) -> bool:
    mine = (project.get("repo") or "").strip().lower()
    hub = (config.REPO_NAME or "").strip().lower()
    # Имя без площадки не определяет репозиторий: GitVerse-проект с тем же
    # owner/name — другой репозиторий, и выката хаба для него нет.
    return bool(mine and hub and mine == hub and forge_of(project) == HUB_FORGE)


async def _due_rows(db: aiosqlite.Connection, project_id: int) -> list[dict[str, Any]]:
    rows = await repo.fetchall(
        db,
        "SELECT id, release_pr, release_sha FROM release_merges "
        "WHERE project_id = ? AND merged_at <= datetime('now', ?) "
        "AND merged_at >= datetime('now', ?) ORDER BY id",
        (
            project_id,
            f"-{int(config.DEPLOY_SLA_MINUTES)} minutes",
            f"-{LOOKBACK_DAYS} days",
        ),
    )
    return [dict(r) for r in rows]


async def _read_sha(
    db: aiosqlite.Connection, project: dict[str, Any], row: dict[str, Any]
) -> str:
    """Повторное чтение merge_commit_sha — только заполнить release_sha."""
    try:
        sha = await plugins.git_ops.merge_commit_sha(
            int(row["release_pr"]),
            repo=(project.get("workspace_path") or "").strip() or None,
            gh_repo=(project.get("repo") or "").strip() or None,
            forge=forge_of(project),
        )
    except Exception:  # noqa: BLE001 - не прочитан значит не прочитан
        log.exception("release_deploy_watch: sha of PR #%s", row["release_pr"])
        return ""
    sha = (sha or "").strip()
    if sha:
        await set_sha(db, int(project["id"]), int(row["release_pr"]), sha)
    return sha


async def _ensure_commits(
    db: aiosqlite.Connection, project_id: int, shas: list[tuple[str, str]]
) -> None:
    """Подтянуть коммиты; неудача по (проект, sha) запоминается на FETCH_RETRY_SECONDS.

    Кеш общий для строк одного прохода (один deployed_sha — один fetch) и для
    следующих проходов: уже объявленная тревога не оплачивается сетью каждый тик.
    """
    from hub.services.delivery_state import ensure_commit_available

    for sha, ref in shas:
        key = (project_id, sha)
        failed_at = _fetch_failed.get(key)
        if failed_at is not None and _clock() - failed_at < FETCH_RETRY_SECONDS:
            continue
        ok = False
        with contextlib.suppress(Exception):  # best effort: git скажет None
            ok = await ensure_commit_available(db, sha, ref, project_id=project_id)
        if ok:
            _fetch_failed.pop(key, None)
        else:
            _fetch_failed[key] = _clock()


async def _classify(
    db: aiosqlite.Connection, project: dict[str, Any], row: dict[str, Any]
) -> tuple[str, str, dict[str, Any]]:
    """(ветка, причина, детали) для одной строки."""
    project_id = int(project["id"])
    sha = (row["release_sha"] or "").strip() or await _read_sha(db, project, row)
    if not sha:
        return UNOBSERVED, "sha релиза не прочитан", {}
    deployed = await repo.latest_successful_release(db, project_id)
    if deployed is None:
        return UNOBSERVED, "нет ни одного успешного выката", {"release_sha": sha}
    deployed_sha = str(deployed.get("deployed_sha") or "")
    details = {"release_sha": sha, "deployed_sha": deployed_sha}
    if sha == deployed_sha:  # равенство — достижимость без git и без копии
        return _REACHABLE, "", details
    workspace = (project.get("workspace_path") or "").strip()
    if not workspace:
        return UNOBSERVED, "у проекта нет рабочей копии на этом хосте", details
    await _ensure_commits(
        db, project_id, [(sha, ""), (deployed_sha, str(deployed.get("ref") or ""))]
    )
    try:
        reachable = await plugins.git_ops.is_ancestor(workspace, sha, deployed_sha)
    except Exception:  # noqa: BLE001 - исключение git — тоже «нет данных»
        reachable = None
    if reachable is None:
        return UNOBSERVED, "git не смог ответить о достижимости", details
    if reachable:
        return _REACHABLE, "", details
    return NOT_IN_PROD, "релиз не достижим от последнего успешного выката", details


async def _event_exists(
    db: aiosqlite.Connection, kinds: tuple[str, ...], project_id: int, merge_id: int
) -> bool:
    rows = await repo.fetchall(
        db,
        "SELECT 1 FROM events WHERE kind IN (?, ?) AND project_id = ? "
        "AND json_extract(payload, '$.release_merge_id') = ? LIMIT 1",
        (kinds[0], kinds[-1], project_id, merge_id),
    )
    return bool(list(rows))


async def _emit(
    db: aiosqlite.Connection,
    project_id: int,
    row: dict[str, Any],
    kind: str,
    reason: str,
    details: dict[str, Any],
    *,
    notify: bool,
) -> None:
    """Событие и (когда нужно) уведомление в одной транзакции, затем commit."""
    try:
        tasks = await repo.tasks_released_with(db, details.get("release_sha", ""))
    except Exception:  # noqa: BLE001 - состав только для текста
        tasks = []
    event_id = await repo.insert_event(
        db,
        kind=kind,
        project_id=project_id,
        actor="hub",
        payload={
            "release_merge_id": int(row["id"]),
            "release_pr": int(row["release_pr"]),
            "reason": reason,
            "tasks": tasks,
            **details,
        },
    )
    if notify:
        await watch_notices.notify_steward(
            db,
            "release",
            project_id=project_id,
            source_key=f"release_deploy:{row['id']}:{kind}",
            source_event_id=event_id,
        )
    await db.commit()


async def _judge_row(
    db: aiosqlite.Connection, project: dict[str, Any], row: dict[str, Any]
) -> None:
    project_id = int(project["id"])
    merge_id = int(row["id"])
    branch, reason, details = await _classify(db, project, row)
    if branch == _REACHABLE:
        alarmed = await _event_exists(
            db, (NOT_IN_PROD, UNOBSERVED), project_id, merge_id
        )
        if alarmed and not await _event_exists(
            db, (DEPLOYED_LATE,), project_id, merge_id
        ):
            await _emit(
                db,
                project_id,
                row,
                DEPLOYED_LATE,
                "доехал позже",
                details,
                notify=False,
            )
        return
    if await _event_exists(db, (branch,), project_id, merge_id):
        return  # эта ветка по этой строке уже объявлена
    await _emit(db, project_id, row, branch, reason, details, notify=True)


async def sweep(db: aiosqlite.Connection) -> None:
    """Один проход: каждая строка release_merges хаба старше срока — сама за себя."""
    for project_row in await repo.list_projects(db):
        project = dict(project_row)
        if not _is_hub_project(project):
            continue
        for row in await _due_rows(db, int(project["id"])):
            try:
                await _judge_row(db, project, row)
            except Exception:  # noqa: BLE001 - одна строка не валит свип
                log.exception("release_deploy_watch: merge #%s failed", row["id"])
                with contextlib.suppress(Exception):
                    await db.rollback()
