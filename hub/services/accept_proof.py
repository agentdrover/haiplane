"""Один положительный предикат «сдачу можно принять» (#1810, круг 5).

«Принять» в справке — не отсутствие накопленных сомнений, а ДОКАЗАННЫЕ условия.
Прежние круги дописывали проверки по одной, и каждая смотрела свой кусок
(последний отчёт, агрегат поколения), поэтому ревью находило следующий путь.
Здесь путь один: ``prove_accept`` возвращает :class:`AcceptProof`, только если
выполнено ВСЁ; иначе — ``None`` и список причин (:class:`Blocker`).

Условия:

* все узлы графа происхождения (``report_provenance``: отчёты текущего
  поколения, источники переносов #1361 и все отчёты поколений между ними)
  СТРОГО читаются (битый раздел или битая связь — «не доказано», а не пусто)
  и проходят один и тот же белый список ``report_fit``;
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
    report_provenance,
    review_evidence,
    state_review,
)
from hub.services.finding_identity import unresolved_uids
from hub.services.finding_outcome import KIND_UNRESOLVED
from hub.services.report_provenance import UnreadableReport
from hub.services.review_availability import generation_review

CHECK = "Проверить"
REWORK = "На доработку"

CODE_CI = "ci"
CODE_INTEGRITY = "integrity"
CODE_REPORT = "report"
CODE_SHA = "sha"
CODE_VERDICT = "verdict"
CODE_STATE = "state"

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


def _unreadable_blocker(bad: UnreadableReport) -> Blocker:
    if bad.broken_link:
        text = f"Происхождение отчёта ревью #{bad.report_id} нарушено: проверка не доказана"
    else:
        text = f"Отчёт ревью #{bad.report_id} не читается: проверка не доказана"
    return Blocker(CHECK, "high", text, bad.why)


async def _report_blockers(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> tuple[list[Blocker], list[MachineReviewView]]:
    """Каждый узел графа происхождения проходит один предикат; причины называют узлы."""
    try:
        nodes = await report_provenance.provenance_graph(db, task)
    except UnreadableReport as bad:
        return [_unreadable_blocker(bad)], []
    if not nodes:
        headline = str((await generation_review(db, task)).headline)
        return [
            Blocker(CHECK, "high", "Отчёта машинного ревью по этой сдаче нет", headline)
        ], []
    found: dict[str, tuple[str, list[str]]] = {}
    for node in nodes:
        left = await _unanswered(db, int(task["id"]), node.view)
        for code, text in report_fit.report_gaps(node.view, left):
            found.setdefault(code, (text, []))[1].append(node.label)
    blockers = []
    for code, (text, labels) in found.items():
        level = REWORK if code == report_fit.GAP_CONFIRMED else CHECK
        blockers.append(Blocker(level, "high", text, "отчёты: " + ", ".join(labels)))
    blockers += await _carried_approvals(db, task, min(n.generation for n in nodes))
    return blockers, [n.view for n in nodes]


#: События, закрывающие одобрение: человек вернул работу в работу (#1286, #1356).
_CLOSING_EVENTS = ("task_returned_to_work", "task_decided")


def _approval_gap(
    task: dict[str, Any], events: list[tuple[int, str, dict[str, Any]]], generation: int
) -> str:
    """Почему поколение НЕ закончилось действующим одобрением; пусто — закончилось."""
    verdicts = [
        (event_id, payload)
        for event_id, kind, payload in events
        if kind == "review_verdict_recorded"
        and payload.get("submission_generation") == generation
    ]
    if not verdicts:
        return f"поколение {generation}: вердикта нет"
    last_id, last = verdicts[-1]
    if last.get("verdict") != "approved":
        return f"поколение {generation}: последний вердикт «{last.get('verdict')}»"
    if any(kind in _CLOSING_EVENTS and eid > last_id for eid, kind, _ in events):
        return f"поколение {generation}: одобрение закрыто возвратом в работу"
    if task.get("review_verdict_closed_generation") == generation:
        return f"поколение {generation}: одобрение закрыто решением"
    return ""


async def _task_events(
    db: aiosqlite.Connection, task_id: int
) -> list[tuple[int, str, dict[str, Any]]] | None:
    """Вердикты и возвраты задачи по порядку; ``None`` — запись не читается."""
    rows = await fetchall(
        db,
        "SELECT id, kind, payload FROM events WHERE task_id=? AND kind IN "
        "('review_verdict_recorded','task_returned_to_work','task_decided') "
        "ORDER BY id",
        (task_id,),
    )
    out: list[tuple[int, str, dict[str, Any]]] = []
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(item.get("payload") or "{}")
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        out.append((int(item["id"]), str(item["kind"]), payload))
    return out


async def _carried_approvals(
    db: aiosqlite.Connection, task: dict[str, Any], first: int
) -> list[Blocker]:
    """Белый список для переноса (#1912): каждое поколение диапазона — с одобрением.

    Перенос #1361 говорит «правка та же». Чистая копия оправдывает код, только
    если КАЖДОЕ поколение от самого раннего узла графа до текущего (не
    включая) закончилось действующим, не закрытым одобрением. Вердикт «на
    доработку», возврат в работу (``return_to_work``, ``decide`` rework),
    закрытие одобрения, поколение без вердикта и нечитаемая запись — всё это
    «не доказано»: чёрный список возвратов пропускал по одному пути за круг.
    """
    current = int(task.get("submission_generation") or 0)
    if first >= current:
        return []
    task_id = int(task["id"])
    events = await _task_events(db, task_id)
    if events is None:
        return [_history_blocker("запись ленты вердиктов не читается")]
    gaps = []
    for generation in range(first, current):
        gap = _approval_gap(task, events, generation)
        judged = await repo.get_steward_judgement(db, task_id, generation, "verdict")
        if not gap and judged is not None and judged["verdict"] == "changes_requested":
            gap = f"поколение {generation}: суждение стюарда «на доработку»"
        if gap:
            gaps.append(gap)
    return [_history_blocker("; ".join(gaps))] if gaps else []


def _history_blocker(why: str) -> Blocker:
    text = "Перенесённый отчёт: поколения до этой сдачи не закончились одобрением"
    return Blocker(CHECK, "high", text, why, CODE_VERDICT)


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
    if verdict != "approved":
        # Белый список: неизвестное значение вердикта — «не доказано».
        return Blocker(
            CHECK, "high", f"Неизвестный вердикт «{verdict}»", why, CODE_VERDICT
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
