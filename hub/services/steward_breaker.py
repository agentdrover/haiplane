"""Предохранитель автономии стюарда: откат влитого и красная база (#1865).

Возврат стюарда в тень (``steward_exit``) знал три факта: человеческий
``changes_requested``, переоткрытие и прод-дефект с ``caused_by_task_id``. Два
самых прямых признака ошибки он не видел вовсе:

``reverted``
    в первой линии базовой ветки появился коммит, в ПОЛНОМ сообщении которого
    стоит строка ``This reverts commit <sha>``, а ``<sha>`` - коммит доставки
    задачи, одобренной стюардом. Ref случая - sha откатанной доставки (merge_sha),
    поэтому тот же откат, вернувшийся телом релиза, нового случая не даёт; повторный
    откат той же доставки после reapply вне гейта - тоже (ограничение);
``base_red_after_merge``
    красный ``red_sha`` базы - ТОЧНО коммит доставки такой задачи, а его первый
    родитель подтверждённо зелёный. Ref случая - ``red_sha``.

ОДОБРЕНИЕ СТЮАРДОМ ДОКАЗЫВАЕТСЯ ОДНИМ СПОСОБОМ. Последний вердикт задачи не
позже слияния (``pipeline_merges.merged_at``) - событие
``review_verdict_recorded`` с ``verdict=approved``, ``actor=steward`` и меткой
``source=steward_applied``. Метку ставит само применение стюарда, имя актора
приходит в теле запроса и подделывается, поэтому нужны оба (как у
``steward_exit.actual_steward_verdicts``). Пара approve+concur без применения
(тень, вердикт поставил человек) стюардом не считается; одобрение человеком или
другого поколения - тоже: берётся ПОСЛЕДНИЙ вердикт до слияния. События чистятся
через ``EVENTS_RETENTION_DAYS``: связь, которую нечем доказать, не обвиняется.

ЗАПИСЬ. Случай пишется в ``steward_false_approvals`` тем же механизмом, что и
три прежних (ключ ``(task_id, source, ref)``, липкость, снятие
``clear_false_approval``), поэтому ``steward_exit.act_refusals_v2`` возвращает
хаб в тень без правок. Уведомление владельцу - в ТОЙ ЖЕ транзакции и ТОЛЬКО
тем вызовом, чей ``INSERT OR IGNORE`` вставил строку: событие
``steward_breaker_tripped`` (с ``project_id``), alert в ленту задачи и баннер на
странице входящих (читает активные строки, а не политику ``steward_watch``,
поэтому виден и при выключенном наблюдении). Повторный тик строки и уведомления
не плодит; падение до коммита откатывает всё вместе.

СБОРЩИК ОТКАТОВ - не часть ``effective_mode``/``record_false_approvals``: те
зовутся из чтения режима, а сеть (git) в чтение класть нельзя. Здесь свой
цикл поллера с курсором по проекту (``steward_breaker_cursor``): первый тик
ставит курсор на голову ветки и ничего не судит; дальше берётся не больше
``REVERT_BATCH`` самых старых коммитов после курсора, курсор переходит на
последний разобранный в той же транзакции, что и случаи.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.integrations.cursor_cloud import scrub_secrets
from hub.integrations.registry import plugins
from hub.services import ci_contract as ci_contract_mod
from hub.services import project_policy
from hub.services.project_policy import base_branch_of, forge_of
from hub.services.steward_exit import SOURCE_BASE_RED, SOURCE_REVERTED

log = logging.getLogger("hub.steward_breaker")

EVENT_TRIPPED = "steward_breaker_tripped"
NEW_SOURCES: tuple[str, ...] = (SOURCE_REVERTED, SOURCE_BASE_RED)

#: Коммитов за один тик на проект: хвост догоняется следующими тиками.
REVERT_BATCH = 100
CHECK_INTERVAL = 300  # секунд между тиками сборщика
#: «This reverts commit <sha>» с начала строки; короче 12 знаков не принимается.
_REVERTS = re.compile(r"^This reverts commit ([0-9a-f]{12,40})\b", re.MULTILINE)

#: Красный - только failure и timed_out. cancelled (ручная отмена, concurrency),
#: startup_failure и action_required про исход кода ничего не говорят: unknown.
_FAILED = ("failure", "timed_out")


# ---------------------------------------------------------------------------
# Доказательство: доставка одобрена применённым вердиктом стюарда
# ---------------------------------------------------------------------------


async def steward_delivery(
    db: aiosqlite.Connection, project_id: int, merge_sha: str
) -> tuple[int, int] | None:
    """``(task_id, поколение)``, если ``merge_sha`` - доставка, одобренная стюардом.

    None - не доказано: слияния нет в реестре, у него нет задачи, события
    вычищены или последний вердикт до слияния не стюардовский.
    """
    if not merge_sha:
        return None
    rows = await fetchall(
        db,
        "SELECT task_id, merged_at FROM pipeline_merges "
        "WHERE project_id = ? AND merge_sha = ? AND task_id IS NOT NULL LIMIT 1",
        (project_id, merge_sha),
    )
    if not rows:
        return None
    task_id, merged_at = int(rows[0]["task_id"]), str(rows[0]["merged_at"] or "")
    last = await fetchall(
        db,
        "SELECT actor, payload FROM events WHERE kind='review_verdict_recorded' "
        "AND task_id=? AND created_at <= datetime(?) ORDER BY id DESC LIMIT 1",
        (task_id, merged_at),
    )
    if not last:
        return None
    try:
        payload = json.loads(last[0]["payload"] or "{}")
    except ValueError:
        return None
    if not isinstance(payload, dict) or (
        last[0]["actor"] != "steward"
        or payload.get("verdict") != "approved"
        or payload.get("source") != "steward_applied"
    ):
        return None
    return task_id, int(payload.get("submission_generation") or 0)


# ---------------------------------------------------------------------------
# Запись случая и уведомление владельцу
# ---------------------------------------------------------------------------


async def record_case(
    db: aiosqlite.Connection,
    *,
    project: dict[str, Any],
    task_id: int,
    source: str,
    generation: int,
    ref: str,
    detail: str,
) -> bool:
    """Строка случая и уведомление - одной транзакцией; True - этот вызов вставил.

    Без commit: транзакцию закрывает вызывающий, и откат уносит строку,
    событие и alert вместе. Повтор (та же тройка) строки не вставляет и
    уведомление не пишет.
    """
    cur = await db.execute(
        "INSERT OR IGNORE INTO steward_false_approvals "
        "(task_id, source, generation, detail, ref) VALUES (?, ?, ?, ?, ?)",
        (task_id, source, generation, detail, ref),
    )
    if not cur.rowcount:
        return False
    await repo.insert_event(
        db,
        kind=EVENT_TRIPPED,
        task_id=task_id,
        project_id=int(project["id"]),
        actor="hub",
        payload={
            "source": source,
            "ref": ref,
            "generation": generation,
            "project": str(project.get("slug") or ""),
            "detail": detail,
        },
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Предохранитель стюарда сработал [{source}]: {detail}. Стюард возвращён "
        "в тень глобально; снимает только человек: "
        f"oc-hub steward-false-approve-clear {task_id}.",
    )
    return True


async def banner_rows(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Активные случаи новых источников - строки баннера страницы входящих."""
    marks = ",".join("?" for _ in NEW_SOURCES)
    rows = await fetchall(
        db,
        "SELECT task_id, source, ref, detail FROM steward_false_approvals "  # nosec B608 - placeholders only
        f"WHERE cleared_at IS NULL AND source IN ({marks}) ORDER BY id",
        NEW_SOURCES,
    )
    unchecked = [
        {
            "task_id": None,
            "source": "unchecked",
            "ref": str(pid),
            "line": "Откаты доставок стюарда НЕ проверяются (git не отвечает "
            f"{UNCHECKED_AFTER}+ тиков подряд): {reason}",
        }
        for pid, reason in _unchecked.items()
    ]
    return unchecked + [
        {
            "task_id": int(r["task_id"]),
            "source": str(r["source"]),
            "ref": str(r["ref"]),
            "line": f"Стюард возвращён в тень: #{r['task_id']} [{r['source']}] "
            f"{r['detail']}",
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Источник reverted: фоновый сборщик
# ---------------------------------------------------------------------------


async def _cursor_of(db: aiosqlite.Connection, project_id: int) -> str:
    rows = await fetchall(
        db, "SELECT sha FROM steward_breaker_cursor WHERE project_id=?", (project_id,)
    )
    return str(rows[0]["sha"]) if rows else ""


async def _set_cursor(db: aiosqlite.Connection, project_id: int, sha: str) -> None:
    await db.execute(
        "INSERT INTO steward_breaker_cursor (project_id, sha) VALUES (?, ?) "
        "ON CONFLICT(project_id) DO UPDATE SET sha=excluded.sha, "
        "updated_at=datetime('now')",
        (project_id, sha),
    )


async def _reverted_delivery(
    db: aiosqlite.Connection, project_id: int, prefix: str
) -> str:
    """Полный sha доставки по префиксу из текста отката; "" - нет или неоднозначно."""
    rows = await fetchall(
        db,
        "SELECT DISTINCT merge_sha FROM pipeline_merges "
        "WHERE project_id = ? AND task_id IS NOT NULL AND merge_sha <> '' "
        "AND substr(merge_sha, 1, ?) = ?",
        (project_id, len(prefix), prefix),
    )
    return str(rows[0]["merge_sha"]) if len(rows) == 1 else ""


async def _trip_revert(
    db: aiosqlite.Connection, project: dict[str, Any], sha: str, message: str
) -> int:
    """Случаи из одного коммита первой линии; число вставленных строк.

    Ключ случая (``ref``) - sha откатанной ДОСТАВКИ, а не revert-коммита: тот же
    откат возвращается в базу ещё раз (релиз сквошит develop в main с телами
    коммитов, возврат main->develop приходит merge-коммитом), и ключ по revert
    воскресил бы случай и снятое человеком. Следствие: второй откат той же
    доставки - тот же случай; новый случай даёт откат другой доставки.

    Ограничения: откат отката внутри ветки (P3-1) строкой не отличается от
    отката; находка зависит от того, что squash репозитория сохраняет тела
    коммитов или их строки отката (P3-5, настройка форжа).
    """
    tripped = 0
    for prefix in dict.fromkeys(_REVERTS.findall(message)):
        merge_sha = await _reverted_delivery(db, int(project["id"]), prefix)
        proof = await steward_delivery(db, int(project["id"]), merge_sha)
        if proof is None:
            continue
        task_id, generation = proof
        tripped += await record_case(
            db,
            project=project,
            task_id=task_id,
            source=SOURCE_REVERTED,
            generation=generation,
            ref=merge_sha,
            detail=(
                f"откат слияния {merge_sha[:12]} (поколение {generation}, "
                f"одобрено стюардом) коммитом {sha[:12]}"
            ),
        )
    return tripped


_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*")
_CREDENTIAL = re.compile(r"\S*@\S*")


def _safe(text: str, limit: int = 200) -> str:
    """Первая строка stderr без URL, учётных данных и известных секретов хаба.

    stderr ``git fetch`` печатает URL remote, а в нём бывает токен: такой текст
    не вправе попасть ни в баннер, ни в событие, ни в журнал.
    """
    first = (str(text or "").strip().splitlines() or [""])[0]
    first = _CREDENTIAL.sub("[redacted]", _URL.sub("[url]", first))
    return scrub_secrets(first)[:limit]


async def _walk(project: dict[str, Any], cursor: str) -> dict[str, Any]:
    """Свежая история базы после курсора; ``{"error": причина}``, если не прочли."""
    workspace = str(project.get("workspace_path") or "").strip()
    base = base_branch_of(project)
    ok, detail = await plugins.git_ops.fetch_base(workspace, base)
    if not ok:
        log.warning(
            "revert watch %s: fetch: %s", project.get("slug"), _safe(detail, 500)
        )
        return {"error": f"fetch: {_safe(detail)}"}
    walk = await plugins.git_ops.first_parent_after(
        workspace, base, cursor, REVERT_BATCH
    )
    return walk if walk is not None else {"error": "git недоступен"}


#: Подряд не прочитанных тиков, после которых «не проверено» становится видимым.
UNCHECKED_AFTER = 3
EVENT_UNCHECKED = "steward_breaker_unchecked"
_failures: dict[int, int] = {}
_unchecked: dict[int, str] = {}


async def _note_unchecked(
    db: aiosqlite.Connection, project: dict[str, Any], reason: str
) -> None:
    """Тик не прочитал историю: warning с причиной, счётчик, после N тиков - сигнал.

    Молчаливый ноль читался бы как «откатов нет» (#725). Сигнал - событие
    (один раз на серию) и строка баннера входящих, пока серия длится.
    """
    pid = int(project["id"])
    _failures[pid] = _failures.get(pid, 0) + 1
    log.warning(
        "revert watch %s: not checked (%s), %d in a row",
        project.get("slug"),
        reason,
        _failures[pid],
    )
    if _failures[pid] < UNCHECKED_AFTER:
        return
    _unchecked[pid] = f"{project.get('slug')}: {reason}"
    if _failures[pid] == UNCHECKED_AFTER:
        await repo.insert_event(
            db,
            kind=EVENT_UNCHECKED,
            project_id=pid,
            actor="hub",
            payload={"project": str(project.get("slug") or ""), "reason": reason},
        )
        await db.commit()


def _checked(project: dict[str, Any]) -> None:
    _failures.pop(int(project["id"]), None)
    _unchecked.pop(int(project["id"]), None)


async def collect_reverts(db: aiosqlite.Connection, project: dict[str, Any]) -> int:
    """Один проход по проекту: откаты доставок стюарда -> случаи. Число случаев.

    Не бросает: сбой откатывает транзакцию (курсор стоит, случаи не записаны) и
    логируется; следующий тик повторит с тем же курсором.
    """
    pid = int(project["id"])
    if not str(project.get("workspace_path") or "").strip():
        return 0
    try:
        cursor = await _cursor_of(db, pid)
        walk = await _walk(project, cursor)
        if "error" in walk:
            await _note_unchecked(db, project, str(walk["error"]))
            return 0
        if not cursor or walk["reset"]:
            if walk["reset"]:
                log.warning(
                    "revert watch %s: cursor %s is not an ancestor of the base; "
                    "history before %s is not judged",
                    project.get("slug"),
                    cursor[:12],
                    walk["head"][:12],
                )
            await _set_cursor(db, pid, walk["head"])
            await db.commit()
            _checked(project)
            return 0
        shas = list(walk["shas"])
        facts = await plugins.git_ops.commit_facts(
            str(project["workspace_path"]).strip(), shas
        )
        if facts is None or any(s not in facts for s in shas):
            await _note_unchecked(db, project, "сообщения коммитов не прочитаны")
            return 0
        tripped = 0
        for sha in shas:
            fact = facts[sha]
            text = f"{fact['message']}\n{fact.get('second_parent_message') or ''}"
            tripped += await _trip_revert(db, project, sha, text)
        if shas:
            await _set_cursor(db, pid, shas[-1])
        await db.commit()
        _checked(project)
        return tripped
    except Exception as exc:  # noqa: BLE001 - сборщик не роняет поллер; откат и повтор
        log.exception("revert watch %s failed", project.get("slug"))
        await db.rollback()
        # Сбой тоже «не проверено»; в причине только тип: в тексте может быть секрет.
        await _note_unchecked(db, project, f"исключение {type(exc).__name__}")
        return 0


# ---------------------------------------------------------------------------
# Источник base_red_after_merge: вызывается из red_base на красной базе
# ---------------------------------------------------------------------------


def _push_state(
    runs: list[dict[str, Any]] | None,
    contract: ci_contract_mod.CiContract,
    branch: str = "",
) -> str:
    """``green`` | ``red`` | ``unknown`` по push-прогонам workflow контракта.

    Прогоны других workflow не считаются: у прогона с названным путём он обязан
    совпасть с ``workflow_path`` контракта (форж уже фильтрует, это страховка).
    """
    if runs is None:
        return "unknown"
    finished = [
        r
        for r in runs
        if r.get("event") == "push"
        and str(r.get("status") or "") == "completed"
        and (not r.get("path") or ci_contract_mod.path_matches(contract, r["path"]))
        and (not branch or not r.get("head_branch") or r["head_branch"] == branch)
    ]
    if not finished:
        return "unknown"
    newest = max(finished, key=lambda r: str(r.get("run_started_at") or ""))
    conclusion = str(newest.get("conclusion") or "")
    if conclusion == "success":
        return "green"
    return "red" if conclusion in _FAILED else "unknown"


async def _contract_state(
    project: dict[str, Any], contract: ci_contract_mod.CiContract, sha: str
) -> str:
    runs = await plugins.git_ops.ci_runs_for_sha(
        sha,
        repo=(project.get("workspace_path") or "").strip() or None,
        gh_repo=(project.get("repo") or "").strip() or None,
        forge=forge_of(project),
        workflow_path=contract.workflow_path,
    )
    return _push_state(runs, contract, base_branch_of(project))


async def _first_parent(project: dict[str, Any], sha: str) -> str:
    workspace = str(project.get("workspace_path") or "").strip()
    if not workspace:
        return ""
    facts = await plugins.git_ops.commit_facts(workspace, [sha])
    parents = (facts or {}).get(sha, {}).get("parents") or []
    return str(parents[0]) if parents else ""


#: Отрицательный кэш в памяти процесса: (проект, red_sha) -> до какого момента не
#: спрашивать git и форж снова. Пока база красная, тик идёт каждые 3 минуты, а
#: ответ «не доказано/неизвестно» редко меняется.
NEGATIVE_TTL_SECONDS = 1800
_negative: dict[tuple[int, str], float] = {}
_NEGATIVE_MAX = 1000


def _remember_negative(key: tuple[int, str]) -> None:
    """Устойчивый отрицательный ответ - в кэш; размер ограничен."""
    now = time.monotonic()
    if len(_negative) >= _NEGATIVE_MAX:
        for stale in [k for k, until in _negative.items() if until <= now]:
            del _negative[stale]
        if len(_negative) >= _NEGATIVE_MAX:
            _negative.clear()
    _negative[key] = now + NEGATIVE_TTL_SECONDS


async def on_base_red(
    db: aiosqlite.Connection, project: dict[str, Any], red_sha: str
) -> bool:
    """Красная база на ``red_sha``: случай, если это доставка стюарда после зелёного.

    Идемпотентно и без обвинения при неполных данных: сеть спрашивается, только
    когда ``red_sha`` - доставка стюарда без записанного случая. Не бросает.
    """
    try:
        pid = int(project["id"])
        if _negative.get((pid, red_sha), 0.0) > time.monotonic():
            return False
        proof = await steward_delivery(db, pid, red_sha)
        if proof is None:
            return False
        task_id, generation = proof
        known = await fetchall(
            db,
            "SELECT 1 FROM steward_false_approvals "
            "WHERE task_id=? AND source=? AND ref=?",
            (task_id, SOURCE_BASE_RED, red_sha),
        )
        if known:
            _remember_negative((pid, red_sha))
            return False
        contract = project_policy.ci_contract_of(project)
        if not contract.workflow_path:
            return False
        parent = await _first_parent(project, red_sha)
        if not parent:
            return False
        # Кэшируется только устойчивый ответ: оба прогона завершены и пары
        # «зелёный родитель -> красная доставка» нет. Переходный unknown
        # (прогона ещё нет, red_sha не дошёл до клона) не кэшируется - иначе
        # fix-forward за окно кэша теряет случай навсегда.
        red_state = await _contract_state(project, contract, red_sha)
        if red_state == "unknown":
            return False
        if red_state != "red":
            _remember_negative((pid, red_sha))
            return False
        parent_state = await _contract_state(project, contract, parent)
        if parent_state == "unknown":
            return False
        if parent_state != "green":
            _remember_negative((pid, red_sha))
            return False
        written = await record_case(
            db,
            project=project,
            task_id=task_id,
            source=SOURCE_BASE_RED,
            generation=generation,
            ref=red_sha,
            detail=(
                f"база стала красной на {red_sha[:12]} (поколение {generation}, "
                f"одобрено стюардом) при зелёном родителе {parent[:12]}; "
                f"workflow {contract.workflow_path}"
            ),
        )
        await db.commit()
        return written
    except Exception:  # noqa: BLE001 - красная база всё равно объявляется
        log.exception("base-red breaker check failed for %s", red_sha[:12])
        await db.rollback()
        return False


# ---------------------------------------------------------------------------
# Цикл поллера
# ---------------------------------------------------------------------------


async def check_all_projects(db: aiosqlite.Connection) -> int:
    """Один тик: все активные проекты. Число новых случаев."""
    total = 0
    for row in await repo.list_projects(db):
        project = dict(row)
        if project.get("archived"):
            continue
        total += await collect_reverts(db, project)
    return total


async def run_loop(
    open_db: Callable[[], Any],
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Фоновая задача: интервал, тик, повтор. Первый тик - после интервала."""
    async with open_db() as db:
        while True:
            await sleep(CHECK_INTERVAL)
            try:
                await check_all_projects(db)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Steward breaker watch error")
