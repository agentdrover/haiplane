"""Критик k8 для облачного исполнителя (#1953): заказ по строке ленты, отчёт хоста, гейт сдачи.

Облачный исполнитель Cursor сдавал без независимого критика: критик k8 жил только
внутри прогона k8, а его отчёт оставался на хосте. Здесь хабовая сторона:

* **Заказ** - отдельная таблица ``critic_orders``. Заказы исполнителя
  (``executor_orders``) не годятся: им нужны задача и код implementer, а критик
  не получает ни кода, ни сессии хаба - он читает снимок sha с GitHub и говорит
  только с поставщиком модели.
* **Триггер** - самостоятельная строка ленты ``нужен критик: sha <40 hex>`` от
  живой implementer-сессии облачного держателя задачи. Исполнитель не создаёт,
  не закрывает и не пишет отчёт заказа сам: хаб проверяет строку.
* **Отчёт** - ключ хоста критика, один раз; поздний отчёт и гонка с истечением
  решает один условный UPDATE.
* **Гейт сдачи** - у облачной задачи при заданном ``critic_host`` сдача любым
  входом принимается только при ``passed`` на том же наблюдении sha, которое
  закрепляется как ``submission_sha``. Гарантия - на момент сдачи; доставка и
  перепривязка sha после merge базы не меняются (invariants.md).

Сбой чтения доказательства - не «локальная задача»: при включённой политике это
отказ (fail closed). Нет данных и «данные есть, не passed» - разные причины.
"""

from __future__ import annotations

import logging
import hashlib
import re
from dataclasses import dataclass
from typing import Any, NoReturn

import aiosqlite
from fastapi import HTTPException

from hub import config
from hub import repository as repo
from hub.db import fetchall, write_transaction
from hub.expected_sha import normalize_expected_sha
from hub.services import chat_pair, project_policy
from hub.services.untrusted_quote import sanitize_untrusted_quote, valid_host_ref

log = logging.getLogger("hub.services.critic_orders")

STATE_PENDING = "pending"
STATE_DELIVERED = "delivered"
STATE_CLOSED = "closed"
REPORT_OUTCOMES = ("passed", "findings", "failed")

#: Ключ, под которым вход done оставляет на задаче sha, по которому прошёл гейт:
#: закрепление sha внутри транзакции сверяется с ним (то же наблюдение).
CHECKED_KEY = "_critic_checked_sha"
#: Открытых заказов на задачу.
MAX_OPEN_PER_TASK = 1
SUBMITTABLE = ("running", "claimed", "pending_report")

_MENTION = "нужен критик"
_REQUEST = re.compile(r"нужен критик: sha ([0-9a-fA-F]{40})[ \t]*")

REFUSALS: dict[str, str] = {
    "request_line": (
        "строка должна быть отдельной, без цитаты и code block: "
        "«нужен критик: sha <40 hex>»"
    ),
    "request_ambiguous": "в записи несколько разных sha: оставьте одну строку",
    "no_session": "строку пишет живая сессия implementer облачного держателя задачи",
    "not_cloud_holder": "задачу держит не облачный прогон (или держатель сменился)",
    "proof_unreadable": "доказательство облачного держателя прочитать не удалось",
    "policy_off": "у проекта не задан critic_host",
    "quota_open": "по задаче уже есть открытый заказ критика на другой sha",
    "quota_hourly": "исчерпан лимит заказов критика на задачу в час",
    "duplicate": "заказ критика на этот sha уже открыт",
    "already_passed": "критик уже прошёл этот sha: сдавайте",
    "findings_unchanged": "по этому sha у критика есть находки: исправьте код, новый коммит",
    "head_moved": "голова ветки у хаба другая: пришлите строку с её sha",
    "head_unobserved": "голову ветки хаб не наблюдал",
    "race": "заказ на этот sha создан параллельно",
}


# ---------------------------------------------------------------------------
# Чтение: политика, держатель, строки
# ---------------------------------------------------------------------------


async def critic_host_for(db: aiosqlite.Connection, task_id: int) -> str:
    """Хост критика по политике проекта задачи; пусто - выключено. Ошибка чтения летит наружу."""
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None:
        return ""
    return project_policy.critic_host_of(project_policy.gate_policy_of(project))


async def cloud_holder(db: aiosqlite.Connection, task_id: int) -> int | None:
    """Прогон-держатель задачи, если он облачный; иначе None. Ошибка чтения летит наружу.

    Канал берётся по держателю задачи (tasks.executor_run_id -> executor_runs),
    а не по вызывающему: после ручного перехвата ключ держателя снят.
    """
    rows = await fetchall(
        db,
        "SELECT t.executor_run_id AS run_id, r.channel AS channel, "
        "r.task_id AS run_task FROM tasks t "
        "LEFT JOIN executor_runs r ON r.id = t.executor_run_id WHERE t.id = ?",
        (int(task_id),),
    )
    if not rows or rows[0]["run_id"] is None:
        return None
    row = rows[0]
    if row["channel"] != "cloud" or int(row["run_task"] or 0) != int(task_id):
        return None
    return int(row["run_id"])


_FENCE_OPEN = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")


def parse_request_line(content: str) -> tuple[str | None, str]:
    """``(sha, '')`` или ``(None, причина)``: строгая грамматика, не цитата и не код.

    Ограждение кода разбирается как в CommonMark: до трёх пробелов отступа, три и
    более обратных кавычек или тильд; закрывает его только тот же символ не короче открывшего.
    Строка с отступом и строка цитаты - не отдельная строка.
    """
    found: set[str] = set()
    fence = ""
    for raw in (content or "").splitlines():
        line = raw.rstrip()
        if fence:
            closing = re.fullmatch(
                r" {0,3}(%s{%d,})" % (re.escape(fence[0]), len(fence)), line
            )
            if closing:
                fence = ""
            continue
        opening = _FENCE_OPEN.fullmatch(line)
        if opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            fence = opening.group(1)
            continue
        if line.startswith((">", " ", "\t")):
            continue
        match = _REQUEST.fullmatch(line)
        if match:
            found.add(match.group(1).lower())
    if len(found) == 1:
        return next(iter(found)), ""
    return None, "request_ambiguous" if found else "request_line"


# ---------------------------------------------------------------------------
# Заказ по строке ленты
# ---------------------------------------------------------------------------


async def _alert(db: aiosqlite.Connection, task_id: int, text: str) -> None:
    async with write_transaction(db):
        await repo.add_task_update(db, task_id, "hub", "alert", text)


async def _refuse_request(
    db: aiosqlite.Connection, task_id: int, code: str, detail: str = ""
) -> str:
    text = f"Заказ критика не создан ({code}): {REFUSALS[code]}.{detail}"
    await _alert(db, task_id, text)
    return code


async def _open_orders_for(
    db: aiosqlite.Connection, task_id: int, sha: str
) -> tuple[int, int, int]:
    """(открытых на другой sha, за час, открытых на этот sha)."""
    rows = await fetchall(
        db,
        "SELECT "
        "(SELECT COUNT(*) FROM critic_orders WHERE task_id = ?1 AND state <> 'closed' "
        "AND sha <> ?2), "
        "(SELECT COUNT(*) FROM critic_orders WHERE task_id = ?1 "
        "AND created_at >= datetime('now', '-1 hour')), "
        "(SELECT COUNT(*) FROM critic_orders WHERE task_id = ?1 AND state <> 'closed' "
        "AND sha = ?2)",
        (int(task_id), sha),
    )
    return int(rows[0][0]), int(rows[0][1]), int(rows[0][2])


async def _same_sha_history(db: aiosqlite.Connection, task_id: int, sha: str) -> str:
    rows = await fetchall(
        db,
        "SELECT outcome FROM critic_orders WHERE task_id = ? AND sha = ? "
        "AND state = 'closed' ORDER BY id DESC",
        (int(task_id), sha),
    )
    outcomes = [r["outcome"] for r in rows]
    if "passed" in outcomes:
        return "already_passed"
    return "findings_unchanged" if outcomes[:1] == ["findings"] else ""


async def _holder_proof(
    db: aiosqlite.Connection, task_id: int, session_token: str
) -> tuple[str, int]:
    """``('', прогон вызывающего)`` - доказано; иначе ``(код отказа, 0)``."""
    try:
        holder = await cloud_holder(db, task_id)
        caller = (
            await chat_pair.run_of_session(db, session_token, task_id)
            if session_token
            else None
        )
    except Exception as exc:  # noqa: BLE001 - fail closed, не «локальный»
        log.warning("critic proof for #%s unreadable: %s", task_id, exc)
        return "proof_unreadable", 0
    if not session_token:
        return "no_session", 0
    if holder is None or caller is None or caller != holder:
        return "not_cloud_holder", 0
    return "", caller


async def request_from_feed(
    db: aiosqlite.Connection, task_id: int, content: str, *, session_token: str
) -> str:
    """Строка ленты как заказ критика: '' (тишина или заказ создан) либо код отказа.

    Порядок проверок несущий: сессия и держатель, политика, строка, квота,
    дедупликация - и только потом сетевое наблюдение головы. Заказ создаётся
    после повторной проверки квоты в одной транзакции с записью ленты.
    """
    if _MENTION not in (content or "").lower():
        return ""
    if not session_token and parse_request_line(content)[0] is None:
        return ""  # упоминание фразы без сессии и без строки заказа - обычный текст
    refused, caller_run = await _holder_proof(db, task_id, session_token)
    if refused:
        return await _refuse_request(db, task_id, refused)
    try:
        host = await critic_host_for(db, task_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("critic policy for #%s unreadable: %s", task_id, exc)
        return await _refuse_request(db, task_id, "proof_unreadable")
    if not host:
        return await _refuse_request(db, task_id, "policy_off")
    sha, problem = parse_request_line(content)
    if sha is None:
        return await _refuse_request(db, task_id, problem)
    other_open, last_hour, same_open = await _open_orders_for(db, task_id, sha)
    if other_open >= MAX_OPEN_PER_TASK:
        return await _refuse_request(db, task_id, "quota_open")
    if last_hour >= config.CRITIC_ORDERS_PER_HOUR:
        return await _refuse_request(db, task_id, "quota_hourly")
    if same_open:
        return await _refuse_request(db, task_id, "duplicate")
    history = await _same_sha_history(db, task_id, sha)
    if history:
        return await _refuse_request(db, task_id, history)
    return await _create_order(db, task_id, sha, host, caller_run)


async def _create_order(
    db: aiosqlite.Connection, task_id: int, sha: str, host: str, caller_run: int
) -> str:
    from hub.services import lifecycle
    from hub.services.orchestration import project_git_context

    task = dict(await repo.get_task(db, task_id) or {})
    branch = (task.get("branch") or "").strip()
    tip, why = await lifecycle.resolve_branch_tip(db, task_id, branch)
    try:
        observed = normalize_expected_sha(tip)
    except ValueError:
        observed = None
    if observed is None:
        return await _refuse_request(
            db, task_id, "head_unobserved", f" Причина: {why or 'нет sha'}."
        )
    if observed != sha:
        return await _refuse_request(
            db, task_id, "head_moved", f" Заявлен {sha[:12]}, голова {observed[:12]}."
        )
    ctx = await project_git_context(db, task_id)
    blocked = ""
    async with write_transaction(db):
        # Всё, что проверено до сети, проверяется заново под блокировкой записи:
        # держатель мог смениться, а параллельный заказ - закрыться passed.
        other_open, last_hour, same_open = await _open_orders_for(db, task_id, sha)
        if await cloud_holder(db, task_id) != caller_run:
            blocked = "not_cloud_holder"
        elif other_open >= MAX_OPEN_PER_TASK:
            blocked = "quota_open"
        elif last_hour >= config.CRITIC_ORDERS_PER_HOUR:
            blocked = "quota_hourly"
        elif same_open:
            blocked = "duplicate"
        else:
            blocked = await _same_sha_history(db, task_id, sha)
        if not blocked:
            cursor = await db.execute(
                "INSERT INTO critic_orders (task_id, executor_run_id, sha, repo, "
                "branch, base, host, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, "
                "datetime('now', ?))",
                (
                    task_id,
                    caller_run,
                    sha,
                    ctx.get("gh_repo", ""),
                    branch,
                    ctx.get("base_branch", ""),
                    host,
                    f"+{int(config.CRITIC_ORDER_TTL_MIN)} minutes",
                ),
            )
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "status",
                f"Заказ критика #{cursor.lastrowid} создан: sha {sha[:12]}, хост "
                f"{host}, срок {int(config.CRITIC_ORDER_TTL_MIN)} мин. Ждите отчёт "
                "хоста в ленте; сдавайте после «passed» на этом sha.",
            )
    if blocked:
        return await _refuse_request(db, task_id, blocked)
    return ""


# ---------------------------------------------------------------------------
# Хост: выдача, отчёт, истечение
# ---------------------------------------------------------------------------


def _view(row: Any) -> dict[str, Any]:
    keys = ("id", "task_id", "sha", "repo", "branch", "base", "state", "expires_at")
    return {k: row[k] for k in keys}


async def has_open(db: aiosqlite.Connection, host: str) -> bool:
    """Дешёвое чтение для long-poll: есть ли невыданный или выданный живой заказ."""
    rows = await fetchall(
        db,
        "SELECT 1 FROM critic_orders WHERE host = ? AND state <> 'closed' "
        "AND expires_at > datetime('now') LIMIT 1",
        (host,),
    )
    return bool(rows)


async def expire_due(db: aiosqlite.Connection) -> None:
    """Закрыть просроченные открытые заказы исходом expired; одна запись ленты на заказ."""
    rows = await fetchall(
        db,
        "SELECT id, task_id, sha FROM critic_orders WHERE state <> 'closed' "
        "AND expires_at <= datetime('now')",
    )
    for row in rows:
        async with write_transaction(db):
            cursor = await db.execute(
                "UPDATE critic_orders SET state = 'closed', outcome = 'expired', "
                "closed_reason = 'ttl' WHERE id = ? AND state <> 'closed' "
                "AND expires_at <= datetime('now')",
                (row["id"],),
            )
            if cursor.rowcount == 1:
                await repo.add_task_update(
                    db,
                    row["task_id"],
                    "hub",
                    "alert",
                    f"Заказ критика #{row['id']} на {row['sha'][:12]} истёк без "
                    "отчёта: пришлите строку «нужен критик» ещё раз на этом же "
                    "коде или на новом коммите.",
                )


async def deliver(db: aiosqlite.Connection, host: str) -> list[dict[str, Any]]:
    """Живые заказы хоста; выданный отдаётся повторно, пока не закрыт."""
    await expire_due(db)
    async with write_transaction(db):
        await db.execute(
            "UPDATE critic_orders SET state = 'delivered' "
            "WHERE host = ? AND state = 'pending' AND expires_at > datetime('now')",
            (host,),
        )
    rows = await fetchall(
        db,
        "SELECT * FROM critic_orders WHERE host = ? AND state <> 'closed' "
        "AND expires_at > datetime('now') ORDER BY id",
        (host,),
    )
    return [_view(r) for r in rows]


def _next_step(outcome: str | None, sha: str) -> str:
    if outcome == "passed":
        return f"Сдавайте (hub_submit_for_review) ровно этот sha {sha[:12]}."
    if outcome == "findings":
        return "Исправьте P1/P2, закоммитьте и пришлите строку «нужен критик» на новом sha."
    if outcome in ("failed", "expired"):
        return "Пришлите строку «нужен критик» ещё раз (тот же код допустим)."
    return "Ждите отчёт хоста критика; сдача до «passed» отказана."


def _report_digest(*parts: Any) -> str:
    """Отпечаток ПРИСЛАННОГО отчёта: усечённая выжимка различия не доказывает."""
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()


def _refuse_report(status: int, reason: str, message: str) -> NoReturn:
    raise HTTPException(
        status, detail={"reason": reason, "actor_hint": "agent", "message": message}
    )


async def report(
    db: aiosqlite.Connection,
    host: str,
    order_id: int,
    *,
    outcome: str,
    findings_p1: int,
    findings_p2: int,
    summary: str,
    host_ref: str,
) -> dict[str, Any]:
    """Отчёт хоста: один раз; идентичный повтор - no-op, изменённый - 409."""
    if outcome not in REPORT_OUTCOMES:
        _refuse_report(422, "critic_outcome_invalid", f"outcome: {REPORT_OUTCOMES}")
    if outcome == "passed" and (findings_p1 or findings_p2):
        _refuse_report(422, "critic_passed_with_findings", "passed только при P1=P2=0")
    if outcome == "findings" and not (findings_p1 or findings_p2):
        _refuse_report(422, "critic_findings_empty", "findings требует P1 или P2")
    if host_ref and not valid_host_ref(host_ref):
        _refuse_report(422, "critic_host_ref_invalid", "host_ref: короткий токен")
    clean = sanitize_untrusted_quote(summary)
    digest = _report_digest(outcome, findings_p1, findings_p2, summary, host_ref)
    late = False
    async with write_transaction(db):
        rows = await fetchall(
            db,
            "SELECT * FROM critic_orders WHERE id = ? AND host = ?",
            (order_id, host),
        )
        if not rows:
            _refuse_report(404, "critic_order_not_found", "заказа нет у этого хоста")
        row = rows[0]
        if row["state"] == STATE_CLOSED:
            if row["outcome"] != "expired" and row["report_digest"] == digest:
                return {"order_id": order_id, "outcome": outcome, "repeat": True}
            _refuse_report(
                409, "critic_order_closed", f"заказ закрыт: {row['outcome']}"
            )
        cursor = await db.execute(
            "UPDATE critic_orders SET state = 'closed', outcome = ?, findings_p1 = ?, "
            "findings_p2 = ?, summary = ?, host_ref = ?, report_digest = ?, "
            "reported_at = datetime('now'), closed_reason = 'reported' "
            "WHERE id = ? AND state <> 'closed' AND expires_at > datetime('now')",
            (outcome, findings_p1, findings_p2, clean, host_ref, digest, order_id),
        )
        if cursor.rowcount == 1:
            await repo.add_task_update(
                db,
                row["task_id"],
                "hub",
                "alert" if outcome != "passed" else "status",
                f"Критик {host} по sha {row['sha'][:12]}: {outcome} "
                f"(P1={findings_p1}, P2={findings_p2}). Выжимка хоста (недоверенная "
                f"цитата): {clean or '—'}. {_next_step(outcome, row['sha'])}",
            )
        else:
            late = True
    if late:
        await expire_due(db)
        _refuse_report(
            409, "critic_order_expired", "срок заказа вышел: отчёт не принят"
        )
    return {"order_id": order_id, "outcome": outcome, "repeat": False}


# ---------------------------------------------------------------------------
# Гейт сдачи
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """Результат проверки сдачи: применим ли гейт к задаче и чем отказ."""

    applicable: bool = False
    cause: str = ""
    sha: str = ""
    detail: str = ""


def _gate_detail(task_id: int, verdict: Verdict) -> dict[str, Any]:
    from hub.actionable_errors import enrich_error_payload

    return enrich_error_payload(
        {
            "reason": "critic_not_passed",
            "cause": verdict.cause,
            "actor_hint": "agent",
            "retry_by_same_caller": False,
            "task_id": task_id,
            "sha": verdict.sha,
            "message": (
                f"Task #{task_id}: сдача отказана - у критика нет passed на "
                f"{(verdict.sha or 'неизвестном sha')[:12]} ({verdict.cause}). "
                f"{verdict.detail}"
            ).strip(),
            "hint": (
                "Nothing was recorded: no generation, no feed row, the status did "
                "not change. Напишите отдельную строку ленты «нужен критик: sha "
                "<40 hex>» на голове ветки, дождитесь отчёта хоста и сдавайте."
            ),
        }
    )


def refuse(task_id: int, verdict: Verdict) -> NoReturn:
    raise HTTPException(422, detail=_gate_detail(task_id, verdict))


async def evaluate(
    db: aiosqlite.Connection, task: dict[str, Any], observed_sha: str
) -> Verdict:
    """Применим ли гейт и пройден ли он на наблюдённом sha. Ничего не пишет."""
    task_id = int(task["id"])
    try:
        host = await critic_host_for(db, task_id)
        if not host:
            return Verdict()
        holder = await cloud_holder(db, task_id)
    except Exception as exc:  # noqa: BLE001 - fail closed только при включённой политике
        log.warning("critic gate for #%s unreadable: %s", task_id, exc)
        return Verdict(
            True, "proof_unreadable", "", "Доказательство держателя не прочитано."
        )
    if holder is None:
        return Verdict()
    try:
        sha = normalize_expected_sha(observed_sha) or ""
    except ValueError:
        sha = ""
    if not sha:
        return Verdict(True, "head_unobserved", "", "Голову ветки хаб не наблюдал.")
    rows = await fetchall(
        db,
        "SELECT state, outcome, expires_at, expires_at <= datetime('now') AS late "
        "FROM critic_orders WHERE task_id = ? AND sha = ? ORDER BY id DESC",
        (task_id, sha),
    )
    if any(r["outcome"] == "passed" for r in rows):
        return Verdict(True, "", sha)
    if not rows:
        return Verdict(True, "no_order", sha, "Заказа критика на этот sha нет.")
    newest = rows[0]
    if newest["state"] != STATE_CLOSED:
        return Verdict(True, "expired" if newest["late"] else "order_open", sha)
    return Verdict(True, str(newest["outcome"]), sha)


async def submit_step(state: Any) -> None:
    """Шаг сдачи ``critic_passed`` (#1953): облачная сдача при critic_host - только при passed.

    Стоит сразу за ``pin_submission_sha`` и читает ТО ЖЕ наблюдение
    (``state.submission_sha``), которое станет ``submission_sha``: второго
    наблюдения, способного разойтись с закреплением, нет. До bump и любых
    записей. Выключенная политика, k8- и локальный держатель - без изменений.
    """
    verdict = await evaluate(state.db, state.task, state.submission_sha)
    if verdict.applicable and verdict.cause:
        refuse(int(state.task_id), verdict)


async def gate_done_report(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> dict[str, Any]:
    """Вход done (HTTP): отказ 422 ДО записей, либо задача с проверенным sha в ключе."""
    from hub.services.lifecycle import PREPARED_CI_SHA
    from hub.services.orchestration import review_approved_for_current_submission

    if task.get("status") not in SUBMITTABLE or review_approved_for_current_submission(
        task
    ):
        return task
    verdict = await evaluate(db, task, str(task.get(PREPARED_CI_SHA) or ""))
    if verdict.applicable and verdict.cause:
        refuse(int(task["id"]), verdict)
    if not verdict.applicable:
        return task
    return {**task, CHECKED_KEY: verdict.sha}


def assert_pin_matches(task: dict[str, Any], pinned_sha: str) -> None:
    """Закрепляемый sha - тот, по которому прошёл гейт; иначе откат внутри савпоинта."""
    checked = task.get(CHECKED_KEY)
    if checked and pinned_sha != checked:
        refuse(
            int(task["id"]),
            Verdict(
                True,
                "sha_changed",
                checked,
                f"Голова при закреплении {pinned_sha[:12] or 'не наблюдена'}.",
            ),
        )


async def automatic_gate(db: aiosqlite.Connection, task: dict[str, Any]) -> bool:
    """Автоматический вход (поллер, без HTTP): True - сдача удержана, задача в needs_decision."""
    from hub.services import watch_notices
    from hub.services.lifecycle import PREPARED_CI_SHA
    from hub.services.orchestration import review_approved_for_current_submission

    if CHECKED_KEY in task or review_approved_for_current_submission(task):
        return False
    verdict = await evaluate(db, task, str(task.get(PREPARED_CI_SHA) or ""))
    if verdict.applicable and not verdict.cause:
        task[CHECKED_KEY] = verdict.sha  # то же наблюдение сверит закрепление sha
    if not (verdict.applicable and verdict.cause):
        return False
    task_id = int(task["id"])
    await repo.update_task(db, task_id, status="needs_decision")
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Сдача удержана (critic_not_passed, {verdict.cause}): у критика нет passed "
        f"на {(verdict.sha or 'неизвестном sha')[:12]}. {_next_step(None, verdict.sha)} "
        "Решение за человеком (POST /api/tasks/<id>/decide).",
    )
    await watch_notices.emit_needs_decision(
        db,
        task_id=task_id,
        actor="hub",
        payload={"reason": "critic_not_passed", "cause": verdict.cause},
    )
    return True


# ---------------------------------------------------------------------------
# Карточка
# ---------------------------------------------------------------------------


async def card_view(db: aiosqlite.Connection, task_id: int) -> dict[str, Any] | None:
    """Последний заказ критика одним чтением: sha, состояние, срок, причина и шаг."""
    try:
        rows = await fetchall(
            db,
            "SELECT *, expires_at <= datetime('now') AS late FROM critic_orders "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (int(task_id),),
        )
    except Exception as exc:  # noqa: BLE001 - «не знаю» не равно «заказа нет»
        log.warning("critic card for #%s unreadable: %s", task_id, exc)
        return {"unavailable": "заказы критика не прочитаны"}
    if not rows:
        return None
    row = rows[0]
    overdue = row["state"] != STATE_CLOSED and bool(row["late"])
    outcome = "expired" if overdue else row["outcome"]
    return {
        "order_id": row["id"],
        "sha": row["sha"],
        "state": STATE_CLOSED if overdue else row["state"],
        "outcome": outcome,
        "expires_at": row["expires_at"],
        "findings_p1": row["findings_p1"],
        "findings_p2": row["findings_p2"],
        "closed_reason": "ttl" if overdue else row["closed_reason"],
        "next_step": _next_step(outcome, row["sha"]),
    }
