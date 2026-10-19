"""Один положительный предикат «сдачу можно принять» (#1810, круг 5).

«Принять» в справке — не отсутствие накопленных сомнений, а ДОКАЗАННЫЕ условия.
Прежние круги дописывали проверки по одной, и каждая смотрела свой кусок
(последний отчёт, агрегат поколения), поэтому ревью находило следующий путь.
Здесь путь один: ``prove_accept`` возвращает :class:`AcceptProof`, только если
выполнено ВСЁ; иначе — ``None`` и список причин (:class:`Blocker`).

Условия:

* все отчёты текущего поколения СТРОГО читаются (битый раздел — нечитаемый
  отчёт, а не пустой список) и проходят белый список ``report_fit``; отчёт,
  перенесённый на пересдачу (#1361), читается через исходный отчёт;
* вершина ветки наблюдена сейчас и равна ``submission_sha``;
* нет действующего (текущего поколения) ``changes_requested`` и открытых
  in_scope-находок;
* нет незавершённых проверок: ревью не идёт, CI доверенно зелёный на sha сдачи,
  целостность CI доказана.

Доказательство нельзя собрать руками: конструктор требует приватный токен,
поэтому «Принять» в ``decision_brief`` невозможно выдать мимо этой функции.
Задача-состояние (#1647) доказывается отдельным предикатом ``prove_state_accept``
в том же месте и тем же способом.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.models import FindingOutcome, MachineReviewView
from hub.services import (
    ci_contract,
    ci_integrity,
    ci_trust,
    report_fit,
    review_evidence,
    state_review,
)
from hub.services.finding_identity import unresolved_uids
from hub.services.finding_outcome import KIND_UNRESOLVED
from hub.services.review_availability import generation_review

CHECK = "Проверить"
REWORK = "На доработку"

CODE_CI = "ci"
CODE_INTEGRITY = "integrity"
CODE_REPORT = "report"
CODE_SHA = "sha"
CODE_VERDICT = "verdict"
CODE_STATE = "state"

#: Разделы отчёта, обязанные быть списками. Битый JSON или объект вместо
#: массива мягкий декодер модели превращает в [] — а пустой список читается
#: как «находок нет».
_LIST_SECTIONS = (
    "findings_confirmed",
    "findings_rejected",
    "unresolved",
    "lost_dimensions",
)

_MINT = object()


@dataclass(frozen=True)
class Blocker:
    """Причина, по которой «Принять» не доказано."""

    level: str  # REWORK или CHECK
    severity: str
    risk: str
    why: str
    code: str = CODE_REPORT


@dataclass(frozen=True)
class AcceptProof:
    """Доказанное «сдачу можно принять». Создаётся только предикатами этого модуля."""

    sha: str
    report_ids: tuple[int, ...] = ()
    _mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise TypeError(
                "AcceptProof выдаёт только prove_accept / prove_state_accept"
            )


class UnreadableReport(Exception):
    """Отчёт не разобрался строго: «неизвестно», а не «чисто»."""

    def __init__(self, report_id: int, why: str) -> None:
        super().__init__(f"#{report_id}: {why}")
        self.report_id = report_id
        self.why = why


# ---- строгое чтение отчётов ---------------------------------------------------


def _strict_row(row: dict[str, Any]) -> dict[str, Any]:
    """Строка отчёта, у которой каждый раздел — список; иначе UnreadableReport.

    Пустая строка и ``None`` — тоже нечитаемо (json.loads("") падает, None не
    список): «раздел не записан» не равно «находок нет».
    """
    out = dict(row)
    for name in _LIST_SECTIONS:
        raw = out.get(name)
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError as exc:
            raise UnreadableReport(int(row["id"]), f"раздел {name}: не JSON") from exc
        if not isinstance(value, list):
            raise UnreadableReport(int(row["id"]), f"раздел {name}: не список")
        out[name] = value
    return out


async def _earlier_generation(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> int | None:
    """Ближайшее более раннее поколение, в котором есть отчёты: из него и переносят."""
    rows = await fetchall(
        db,
        "SELECT MAX(submission_generation) AS g FROM machine_reviews "
        "WHERE task_id=? AND submission_generation<?",
        (task_id, generation),
    )
    value = dict(rows[0]).get("g") if rows else None
    return int(value) if value is not None else None


async def _judged_rows(
    db: aiosqlite.Connection, row: dict[str, Any], seen: set[tuple[int, int]]
) -> list[dict[str, Any]]:
    """Строки, по которым судят отчёт. ЕДИНЫЙ разворот переносов (#1361).

    Копия переноса стоит за ВСЕМИ отчётами ближайшего более раннего поколения
    (ссылка ``carried_from_review_id`` схлопывается до корня и промежуточные
    поколения теряет, поэтому по ней цепочку не развернуть). Разворот
    рекурсивный: копия в том поколении разворачивается дальше, и каждый
    отчёт на каждом шаге проверяется.

    Сама копия — тоже отчёт: её разделы читаются строго и проходят белый
    список. Единственное, что берётся у корня, — ``provider_tokens``: перенос не
    чтение и токенов по контракту не несёт. Нет корня или нет более раннего
    поколения — отчёт нечитаем.
    """
    root = row.get("carried_from_review_id")
    if not root:
        return [row]
    source = await repo.get_machine_review(db, int(root))
    if source is None:
        raise UnreadableReport(int(row["id"]), f"исходный отчёт #{root} не найден")
    copy = dict(row)
    copy["provider_tokens"] = dict(source).get("provider_tokens")
    task_id, generation = int(row["task_id"]), int(row["submission_generation"])
    earlier = await _earlier_generation(db, task_id, generation)
    if earlier is None:
        raise UnreadableReport(int(row["id"]), "перенос без более раннего поколения")
    out = [copy]
    if (task_id, earlier) in seen:
        return out
    seen.add((task_id, earlier))
    for sibling in await repo.machine_reviews_of_generation(db, task_id, earlier):
        out.extend(await _judged_rows(db, dict(sibling), seen))
    return out


async def generation_views(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> list[MachineReviewView]:
    """ВСЕ отчёты текущего поколения (и за копиями переноса), прочитанные строго."""
    rows = await repo.machine_reviews_of_generation(
        db, int(task["id"]), int(task.get("submission_generation") or 0)
    )
    judged: dict[int, dict[str, Any]] = {}
    seen: set[tuple[int, int]] = set()
    for row in rows:
        for found in await _judged_rows(db, dict(row), seen):
            judged.setdefault(int(found["id"]), found)
    views: list[MachineReviewView] = []
    for report_id, found in judged.items():
        try:
            views.append(MachineReviewView(**_strict_row(found)))
        except UnreadableReport:
            raise
        except (TypeError, ValueError) as exc:
            raise UnreadableReport(report_id, "отчёт не разобрался") from exc
    return views


async def _unanswered(
    db: aiosqlite.Connection, task_id: int, view: MachineReviewView
) -> int:
    """Неотвеченные unresolved одного отчёта: исход ищется по uid, под которым он пишется."""
    left = 0
    for uid in unresolved_uids(view.unresolved):
        outcome = await repo.finding_outcome_for_uid(
            db, task_id, uid, finding_kind=KIND_UNRESOLVED
        )
        left += outcome is None or (
            str(dict(outcome).get("outcome") or "") == FindingOutcome.not_judged.value
        )
    return left


async def _report_blockers(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> tuple[list[Blocker], list[MachineReviewView]]:
    try:
        views = await generation_views(db, task)
    except UnreadableReport as bad:
        text = f"Отчёт ревью #{bad.report_id} не читается: проверка не доказана"
        return [Blocker(CHECK, "high", text, bad.why)], []
    if not views:
        headline = str((await generation_review(db, task)).headline)
        return [
            Blocker(CHECK, "high", "Отчёта машинного ревью по этой сдаче нет", headline)
        ], []
    found: dict[str, tuple[str, list[int]]] = {}
    for view in views:
        left = await _unanswered(db, int(task["id"]), view)
        for code, text in report_fit.report_gaps(view, left):
            found.setdefault(code, (text, []))[1].append(view.id)
    blockers = []
    for code, (text, ids) in found.items():
        level = REWORK if code == report_fit.GAP_CONFIRMED else CHECK
        which = ", ".join(f"#{i}" for i in ids)
        blockers.append(Blocker(level, "high", text, f"отчёты: {which}"))
    return blockers, views


# ---- ветка, вердикт, проверки --------------------------------------------------


def _tip(task: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    """Вершина ветки из последнего НАБЛЮДЕНИЯ хаба: показ в сеть не ходит.

    Карточка перерисовывается каждые 15 с, а входящие зовут справку по каждой
    задаче: ``git fetch`` на каждый показ — нагрузка и зависание. Нет свежего
    наблюдения (его пишут сдача, бриф, гейт, стюард) — «не сверена», а не «совпала».
    """
    if "branch_tip" in ctx:
        return ctx["branch_tip"]
    from hub.services.lifecycle import observed_branch_tip

    return observed_branch_tip(int(task["id"]), str(task.get("branch") or ""))


async def _sha_blocker(
    db: aiosqlite.Connection, task: dict[str, Any], ctx: dict[str, Any]
) -> Blocker | None:
    branch = str(task.get("branch") or "").strip()
    pinned = str(task.get("submission_sha") or "").strip()
    tip, reason = _tip(task, ctx) if (pinned and branch) else ("", "")
    state, why = review_evidence.sha_check_of(pinned, branch, tip, reason)
    if state == "match":
        return None
    risk = (
        "Ветка ушла от сдачи: проверялся не тот код"
        if state == "diverged"
        else "Вершина ветки не сверена: совпадение со сдачей не доказано"
    )
    return Blocker(CHECK, "high", risk, why, CODE_SHA)


def _verdict_blocker(task: dict[str, Any]) -> Blocker | None:
    """Действующий вердикт и открытые in_scope-находки ТЕКУЩЕГО поколения.

    «На доработку» блокирует всегда. in_scope-находки блокируют при ЛЮБОМ
    вердикте: «одобрено» с открытыми находками в рамках задачи — противоречие,
    которое решает человек, а не справка. out_of_scope — рекомендации, не блок.
    """
    generation = int(task.get("submission_generation") or 0)
    verdict = task.get("review_verdict")
    if not verdict or int(task.get("review_verdict_generation") or 0) != generation:
        return None  # вердикта нет или он про прошлую сдачу: код с тех пор пересдан
    try:
        raw = json.loads(task.get("review_findings") or "[]")
    except ValueError:
        raw = None
    if not isinstance(raw, list):
        return Blocker(
            REWORK if verdict == "changes_requested" else CHECK,
            "high",
            "Находки вердикта не читаются",
            "",
            CODE_VERDICT,
        )
    # Блокирует всё, что не названо out_of_scope: неизвестное значение scope и
    # не-словарь — не «безопасно», а «не доказано».
    open_in_scope = [
        f
        for f in raw
        if not (isinstance(f, dict) and f.get("scope", "in_scope") == "out_of_scope")
    ]
    why = f"открытых находок в рамках задачи: {len(open_in_scope)}"
    if verdict == "changes_requested":
        return Blocker(
            REWORK, "high", "Действует вердикт «на доработку»", why, CODE_VERDICT
        )
    if open_in_scope:
        return Blocker(
            CHECK,
            "high",
            f"Вердикт «{verdict}», но есть открытые находки в рамках задачи",
            why,
            CODE_VERDICT,
        )
    return None


async def _ci_blocker(db: aiosqlite.Connection, task: dict[str, Any]) -> Blocker | None:
    sha = str(task.get("submission_sha") or "").strip()
    if not sha:
        return Blocker(
            CHECK, "high", "CI не доказан: коммит сдачи не закреплён", "", CODE_CI
        )
    gap = await ci_trust.pass_gap(db, int(task["id"]), sha)
    if gap is None:
        return None
    row = await repo.get_ci_run_report(db, int(task["id"]), sha)
    fingerprint = await ci_contract.fingerprint_for_task(db, int(task["id"]))
    if row is not None and ci_trust.is_verified(dict(row), fingerprint):
        if str(dict(row).get("validation_status") or "").strip() == "fail":
            return Blocker(REWORK, "high", "CI красный на коммите сдачи", gap, CODE_CI)
    return Blocker(
        CHECK,
        "high",
        "CI не доказан: нет подтверждённого зелёного отчёта",
        gap,
        CODE_CI,
    )


async def _integrity_blocker(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> Blocker | None:
    gap = (await ci_integrity.standing_of(db, task)).gap
    if gap is None:
        return None
    return Blocker(
        CHECK,
        "high",
        "Изменены входы CI или целостность не проверена",
        gap,
        CODE_INTEGRITY,
    )


async def prove_accept(
    db: aiosqlite.Connection, task: dict[str, Any], ctx: dict[str, Any] | None = None
) -> tuple[AcceptProof | None, list[Blocker], list[MachineReviewView]]:
    """``(доказательство | None, причины, отчёты)`` для задачи в review.

    Доказательство есть, только если причин нет. Читает, ничего не пишет.
    """
    ctx = ctx or {}
    blockers, views = await _report_blockers(db, task)
    # Платный прогон ещё идёт (или должна открыться вторая дверь): отчёт
    # поколения может измениться, а совет «Принять» опирается на него.
    flight = await review_evidence.inflight_view(db, task, include_owed=True)
    if flight is not None:
        blockers.append(
            Blocker(CHECK, "high", "Ревью этой сдачи ещё идёт", str(flight.headline))
        )
    for found in (
        _verdict_blocker(task),
        await _sha_blocker(db, task, ctx),
        await _ci_blocker(db, task),
        await _integrity_blocker(db, task),
    ):
        if found is not None:
            blockers.append(found)
    if blockers:
        return None, blockers, views
    proof = AcceptProof(
        str(task["submission_sha"]), tuple(v.id for v in views), _mint=_MINT
    )
    return proof, [], views


async def prove_state_accept(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> tuple[AcceptProof | None, list[Blocker], state_review.StateReviewView]:
    """То же для задачи-состояния: полный комплект доказательств и назван откат."""
    view = await state_review.state_view_of(db, task)
    blockers = []
    if not view.evidence_complete:
        blockers.append(
            Blocker(CHECK, "high", f"Доказательства: {view.headline}", "", CODE_STATE)
        )
    if not view.rollback:
        blockers.append(
            Blocker(CHECK, "high", "Откат (rollback): не назван", "", CODE_STATE)
        )
    if _verdict_blocker(task) is not None:
        blockers.append(_verdict_blocker(task))  # type: ignore[arg-type]
    if blockers:
        return None, blockers, view
    return AcceptProof(f"state:{view.generation}", _mint=_MINT), [], view
