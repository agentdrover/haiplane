"""Доказательства для вердикта: суждение стюарда, отчёт поколения, доверие CI (#1697).

Человек (и сессия, готовящая вердикт) раньше читала это SQL-запросами к
прод-базе: почему стюард вернул задачу, что было в машинном отчёте ИМЕННО
этого поколения, подтверждён ли отчёт CI по сдаче. Здесь это одно чтение
поверх существующих таблиц; новых запросов в repository нет.

Каждая часть независимо ``None``: нет суждения — это не «нет отчёта». Для
задачи-состояния код не проверяется, и обе кодовые части названы словом
``not_applicable``, а не пустотой (#1648).

Текст суждения стюарда читают только роли с правом ``verdict.evidence.read``
(маршрут за ``require_permission``). В ``ReviewBrief`` и в пакет стюарда оно
не попадает: судья не должен читать своё прошлое суждение как факт.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.models import to_iso_utc
from hub.services import ci_contract, ci_trust, verdict_owner
from hub.services.result_kind import automation_not_applicable

PERMISSION = "verdict.evidence.read"
NOT_APPLICABLE = "not_applicable"
GROUNDS_LIMIT = 5
DETAIL_LIMIT = 300
SOURCE_LIMIT = 60
REASON_LIMIT = 300
#: Строка, которой читатель текста отвечает на отказ по праву.
ROLE_REFUSED_LINE = "Для вердикта: доказательства недоступны этой роли"


def _clean(value: Any, cap: int) -> tuple[str, bool]:
    """Текст без управляющих символов и переносов, не длиннее ``cap``.

    ``(текст, урезан)``. Переносы и прочие управляющие символы заменяются
    пробелом: основание — чужие слова, и строка, вставленная в текст брифа,
    не должна начать в нём новую «строку» с чужим заголовком.
    """
    text = "".join(
        " " if (ord(ch) < 32 or ord(ch) in (127, 0x85, 0x2028, 0x2029)) else ch
        for ch in str(value or "")
    )
    if len(text) <= cap:
        return text, False
    return text[: cap - 1] + "…", True


def parse_grounds(raw: Any) -> list[Any]:
    """Основания суждения как список: в базе это JSON-строка, ``[]`` — «пусто».

    Общий разбор для карточки (web) и для этого маршрута: два разбора одной
    колонки разъезжаются первым же нестандартным значением.
    """
    try:
        parsed = json.loads(raw or "[]") if isinstance(raw, (str, type(None))) else raw
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _ground(item: Any) -> tuple[dict[str, str], bool]:
    if isinstance(item, dict):
        source, cut_a = _clean(item.get("source"), SOURCE_LIMIT)
        detail, cut_b = _clean(item.get("detail"), DETAIL_LIMIT)
        return {"source": source, "detail": detail}, cut_a or cut_b
    detail, cut = _clean(item, DETAIL_LIMIT)
    return {"source": "", "detail": detail}, cut


def _steward_part(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    grounds_all = parse_grounds(data.get("grounds"))
    grounds: list[dict[str, str]] = []
    truncated = len(grounds_all) > GROUNDS_LIMIT
    for item in grounds_all[:GROUNDS_LIMIT]:
        ground, cut = _ground(item)
        grounds.append(ground)
        truncated = truncated or cut
    reason, cut = _clean(data.get("escalate_reason"), REASON_LIMIT)
    return {
        "verdict": data.get("verdict") or "",
        "confidence": data.get("confidence") or "",
        "escalate_reason": reason,
        "grounds": grounds,
        "grounds_total": len(grounds_all),
        "truncated": truncated or cut,
        "model": data.get("model") or "",
        "advisor_outcome": data.get("advisor_outcome") or "",
        "advisor_outcome_detail": _clean(
            data.get("advisor_outcome_detail"), REASON_LIMIT
        )[0],
        "judged_at": to_iso_utc(data.get("created_at")),
    }


def _owner_part(task: dict[str, Any], generation: int) -> dict[str, str] | None:
    """Записанный владелец ЭТОЙ сдачи, если вердикт передан человеку: причина видна и при ранней эскалации."""
    if generation != int(task.get("submission_generation") or 0):
        return None
    owner, reason = verdict_owner.written_owner(task)
    if owner != verdict_owner.OWNER_HUMAN:
        return None
    return {"owner": owner, "reason": _clean(reason, REASON_LIMIT)[0]}


def _count(raw: Any) -> int | None:
    """Длина JSON-списка отчёта; ``None`` — не читается, а не «ноль»."""
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return len(loaded) if isinstance(loaded, list) else None


def _machine_part(rows: list[Any]) -> dict[str, Any] | None:
    if not rows:
        return None
    row = dict(rows[-1])
    incomplete = row.get("incomplete")
    return {
        "review_id": row.get("id"),
        "profile": row.get("profile") or "",
        "model": row.get("model") or "",
        "raw_count": int(row.get("raw_count") or 0),
        "confirmed": _count(row.get("findings_confirmed")),
        "rejected": _count(row.get("findings_rejected")),
        "unresolved": _count(row.get("unresolved")),
        "incomplete": None if incomplete is None else bool(incomplete),
        "incomplete_reason": row.get("incomplete_reason") or "",
    }


async def _sha_of(db: aiosqlite.Connection, task: dict[str, Any], generation: int):
    pinned = (task.get("submission_sha") or "").strip()
    if generation and generation == int(task.get("submission_generation") or 0):
        if pinned:
            return pinned
    ledger = await repo.get_submission(db, int(task["id"]), generation)
    return ((dict(ledger).get("sha") if ledger else "") or "").strip()


async def _ci_part(
    db: aiosqlite.Connection, task_id: int, sha: str
) -> dict[str, Any] | None:
    """CI по сдаче: проекция коммита и строка ЕЁ ТЕКУЩЕЙ попытки (#1687).

    Время подтверждения — ``checked_at`` строки попытки, не ``reported_at``
    проекции: проекцию пишет и приём отчёта, а проверку GitHub — только
    попытка. Строки попытки нет — поля пусты и ``attempt_pending``.
    """
    if not sha:
        return None
    report = await repo.get_ci_run_report(db, task_id, sha)
    if report is None:
        return None
    data = dict(report)
    trust, reason = ci_trust.trust_of(
        data, await ci_contract.fingerprint_for_task(db, task_id)
    )
    key = str(data.get("attempt_key") or "")
    attempt = None
    for candidate in await repo.list_ci_report_attempts(db, task_id, sha):
        row = dict(candidate)
        if key and f"ci:{row['run_id']}:{row['run_attempt']}" == key:
            attempt = row
            break
    return {
        "sha": sha,
        "validation_status": data.get("validation_status") or "",
        "trust": trust,
        "trust_reason": reason,
        "attempt_key": key,
        "attempt_pending": attempt is None,
        "reported_at": to_iso_utc(attempt["reported_at"]) if attempt else None,
        "checked_at": to_iso_utc(attempt.get("checked_at")) if attempt else None,
    }


def ci_confirmed_after(steward: Any, ci: Any) -> bool:
    """Стюард судил раньше, чем GitHub подтвердил CI («по данным попытки»).

    Повторный POST той же попытки перезаписывает время, поэтому ответ
    описывает данные попытки на сейчас, а не историю.
    """
    if not isinstance(steward, dict) or not isinstance(ci, dict):
        return False
    judged, checked = steward.get("judged_at"), ci.get("checked_at")
    return bool(
        ci.get("trust") == ci_trust.VERIFIED and judged and checked and checked > judged
    )


async def build(
    db: aiosqlite.Connection, task_id: int, generation: int | None = None
) -> dict[str, Any] | None:
    """Доказательства поколения; ``None`` — задачи нет (404 решает вызывающий)."""
    row = await repo.get_task(db, task_id)
    if row is None:
        return None
    task = dict(row)
    asked = int(
        generation if generation is not None else task.get("submission_generation") or 0
    )
    judged = (
        await repo.get_steward_judgement(db, task_id, asked, "verdict")
        if asked
        else None
    )
    steward = _steward_part(judged)
    if automation_not_applicable(task):
        machine: Any = NOT_APPLICABLE
        ci: Any = NOT_APPLICABLE
    else:
        machine = (
            _machine_part(await repo.machine_reviews_of_generation(db, task_id, asked))
            if asked
            else None
        )
        ci = (
            await _ci_part(db, task_id, await _sha_of(db, task, asked))
            if asked
            else None
        )
    return {
        "task_id": task_id,
        "generation": asked,
        "steward": steward,
        "machine_review": machine,
        "ci": ci,
        "ci_after_judgement": ci_confirmed_after(steward, ci),
        "verdict_owner": _owner_part(task, asked),
    }


# --- text: one formatter for MCP and CLI ---------------------------------------


def _hhmm(stamp: Any) -> str:
    text = str(stamp or "")
    return text[11:16] if len(text) >= 16 else "?"


def _steward_lines(steward: dict[str, Any] | None) -> list[str]:
    if steward is None:
        return ["  Стюард: суждения этого поколения нет"]
    head = f"  Стюард: {steward['verdict'] or '?'}"
    if steward["confidence"]:
        head += f" (уверенность {steward['confidence']})"
    head += f", модель {steward['model'] or '?'}, судил {_hhmm(steward['judged_at'])}"
    if steward["advisor_outcome"]:
        head += f", исход применения: {steward['advisor_outcome']}"
    lines = [head]
    if steward["advisor_outcome_detail"]:
        lines.append(
            f"    причина отказа применения: {steward['advisor_outcome_detail']}"
        )
    if steward["escalate_reason"]:
        lines.append(f"    причина эскалации: {steward['escalate_reason']}")
    shown = len(steward["grounds"])
    if shown:
        cut = ", урезано" if steward["truncated"] else ""
        lines.append(f"    основания ({shown} из {steward['grounds_total']}{cut}):")
        lines.extend(
            f"      {g['source'] or '—'}: {g['detail']}" for g in steward["grounds"]
        )
    else:
        lines.append("    оснований не приложено")
    return lines


def _machine_line(machine: Any) -> str:
    if machine == NOT_APPLICABLE:
        return "  Машинный отчёт: не применимо (задача-состояние)"
    if machine is None:
        return "  Машинный отчёт: отчёта этого поколения нет"

    def num(value: Any) -> str:
        return "?" if value is None else str(value)

    state = {None: "не заявлена", True: "НЕПОЛНЫЙ", False: "полный"}[
        machine["incomplete"]
    ]
    reason = (
        f" ({machine['incomplete_reason']})" if machine["incomplete_reason"] else ""
    )
    return (
        f"  Машинный отчёт: профиль {machine['profile'] or '?'}, модель "
        f"{machine['model'] or '?'}, raw {machine['raw_count']} → confirmed "
        f"{num(machine['confirmed'])} / rejected {num(machine['rejected'])} / "
        f"unresolved {num(machine['unresolved'])}; полнота: {state}{reason}"
    )


def _ci_line(ci: Any) -> str:
    if ci == NOT_APPLICABLE:
        return "  CI: не применимо (задача-состояние)"
    if ci is None:
        return "  CI: отчёта по сдаче нет"
    trust = ci["trust"] + (f" ({ci['trust_reason']})" if ci["trust_reason"] else "")
    head = f"  CI: {ci['validation_status'] or 'без статуса'}, trust {trust}"
    if ci["attempt_pending"]:
        return head + "; строки текущей попытки нет"
    return (
        f"{head}; попытка {ci['attempt_key']}: отчёт {_hhmm(ci['reported_at'])}, "
        f"проверка {_hhmm(ci['checked_at'])}"
    )


def format_block(evidence: Any) -> str:
    """Блок «Для вердикта» — один текст для MCP и CLI.

    Ответ неожиданной формы не роняет бриф: читатель получает строку о том,
    что доказательства не разобраны.
    """
    try:
        return _format(evidence)
    except (KeyError, TypeError, AttributeError):
        return "Для вердикта: доказательства не получены (ответ неожиданной формы)"


def _format(evidence: dict[str, Any]) -> str:
    lines = [f"Для вердикта (поколение {evidence['generation']}):"]
    lines.extend(_steward_lines(evidence["steward"]))
    owner = evidence.get("verdict_owner")
    if owner:
        lines.append(f"  Вердикт передан человеку: {owner['reason']}")
    lines.append(_machine_line(evidence["machine_review"]))
    lines.append(_ci_line(evidence["ci"]))
    if evidence["ci_after_judgement"]:
        lines.append(
            "  ВНИМАНИЕ: стюард судил до подтверждения CI "
            f"({_hhmm(evidence['steward']['judged_at'])} → "
            f"{_hhmm(evidence['ci']['checked_at'])})"
        )
    return "\n".join(lines)


def append_to_result(result: Any, block: str) -> Any:
    """Дописать блок в текст MCP-ответа брифа (``{"message": ...}``-эхо).

    Структурная часть не меняется: блок — подсказка читателю, а не поле брифа.
    Текст, который не разобрался как эхо, получает блок в конец как есть.
    """
    content = list(getattr(result, "content", None) or [])
    for index, part in enumerate(content):
        text = getattr(part, "text", None)
        if text is None:
            continue
        try:
            echo = json.loads(text)
            echo["message"] = f"{echo['message']}\n\n{block}"
            text = json.dumps(echo, ensure_ascii=False)
        except (ValueError, KeyError, TypeError):
            text = f"{text}\n\n{block}"
        content[index] = part.model_copy(update={"text": text})
        return result.model_copy(update={"content": content})
    return result
