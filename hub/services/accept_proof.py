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
    """Строка отчёта, у которой каждый раздел — список; иначе UnreadableReport."""
    out = dict(row)
    for name in _LIST_SECTIONS:
        raw = out.get(name)
        try:
            value = (
                json.loads(raw or "[]") if isinstance(raw, (str, type(None))) else raw
            )
        except ValueError as exc:
            raise UnreadableReport(int(row["id"]), f"раздел {name}: не JSON") from exc
        if not isinstance(value, list):
            raise UnreadableReport(int(row["id"]), f"раздел {name}: не список")
        out[name] = value
    return out


async def _evidence_row(
    db: aiosqlite.Connection, row: dict[str, Any]
) -> dict[str, Any]:
    """Строка, по которой судят отчёт: у переноса (#1361) — исходный отчёт.

    Копия переноса не несёт токенов провайдера по контракту (перенос — не новое
    чтение), поэтому улика исполнения лежит в исходном отчёте. Нет исходного —
    отчёт нечитаем.
    """
    root = row.get("carried_from_review_id")
    if not root:
        return row
    source = await repo.get_machine_review(db, int(root))
    if source is None:
        raise UnreadableReport(int(row["id"]), f"исходный отчёт #{root} не найден")
    source_row = dict(source)
    source_row["id"] = row["id"]
    return source_row


async def generation_views(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> list[MachineReviewView]:
    """ВСЕ отчёты текущего поколения, прочитанные строго."""
    rows = await repo.machine_reviews_of_generation(
        db, int(task["id"]), int(task.get("submission_generation") or 0)
    )
    views: list[MachineReviewView] = []
    for row in rows:
        try:
            strict = _strict_row(await _evidence_row(db, dict(row)))
            views.append(MachineReviewView(**strict))
        except UnreadableReport:
            raise
        except (TypeError, ValueError) as exc:
            raise UnreadableReport(int(row["id"]), "отчёт не разобрался") from exc
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


async def _tip(
    db: aiosqlite.Connection, task: dict[str, Any], ctx: dict[str, Any]
) -> tuple[str, str]:
    if "branch_tip" in ctx:
        return ctx["branch_tip"]
    from hub.services.lifecycle import resolve_branch_tip

    return await resolve_branch_tip(db, int(task["id"]), str(task.get("branch") or ""))


async def _sha_blocker(
    db: aiosqlite.Connection, task: dict[str, Any], ctx: dict[str, Any]
) -> Blocker | None:
    branch = str(task.get("branch") or "").strip()
    pinned = str(task.get("submission_sha") or "").strip()
    tip, reason = await _tip(db, task, ctx) if (pinned and branch) else ("", "")
    state, why = review_evidence.sha_check_of(pinned, branch, tip, reason)
    if state == "match":
        return None
    risk = (
        "Ветка ушла от сдачи: проверялся не тот код"
        if state == "diverged"
        else "Вершина ветки не проверена: совпадение со сдачей не доказано"
    )
    return Blocker(CHECK, "high", risk, why, CODE_SHA)


def _verdict_blocker(task: dict[str, Any]) -> Blocker | None:
    generation = int(task.get("submission_generation") or 0)
    if task.get("review_verdict") != "changes_requested":
        return None
    if int(task.get("review_verdict_generation") or 0) != generation:
        return None  # вердикт прошлой сдачи: код с тех пор пересдан
    try:
        raw = json.loads(task.get("review_findings") or "[]")
    except ValueError:
        raw = None
    if not isinstance(raw, list):
        return Blocker(
            REWORK,
            "high",
            "Действует вердикт «на доработку», находки не читаются",
            "",
            CODE_VERDICT,
        )
    open_in_scope = [
        f
        for f in raw
        if isinstance(f, dict) and f.get("scope", "in_scope") == "in_scope"
    ]
    why = f"открытых находок в рамках задачи: {len(open_in_scope)}"
    return Blocker(
        REWORK, "high", "Действует вердикт «на доработку»", why, CODE_VERDICT
    )


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
