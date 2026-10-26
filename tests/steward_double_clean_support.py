"""Общая сборка сцены для тестов двойной чистоты (#1986).

Сцена целиком настоящая: проект с политикой, задача в review, два отчёта
machine_reviews с пином (#1985), CI на закреплённом sha, заказ судьи (он
фиксирует пару), approve судьи и concur советника на ТОМ ЖЕ хеше пакета.
Подменены только факты, которых без клона репозитория не получить (вершина
ветки, дифф, пересчитанный класс): их возвращает ``clean_packets``.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.services import steward_apply, steward_applied, steward_evidence
from hub.services import steward_shadow as sh
from hub.services.ci_report import VALIDATION_PASS
from hub.services.steward_dispatch import order_run
from hub.services.steward_evidence import packet_hash, present
from tests.ci_trust_support import VERIFIED_ROW, seed_clean_integrity, upsert_hub_report
from tests.test_steward_shadow import (
    _advise,
    _advisor_run,
    _judge,
    _mark_started,
    _project,
    _task,
)

SHA = "a" * 40
IMPLEMENTER = 6
LISTED = 21
OTHER = 22
THIRD = 23

CEILING = "steward_approve_max_class"
CLEAN_CEILING = "steward_approve_max_class_double_clean"
REVIEWERS = "double_clean_reviewers"

FINDING = {"title": "гонка", "severity": "high", "category": "correctness"}
UNRESOLVED = {"title": "не судили", "why": "верификатор не ответил"}


def policy_of(
    ordinary: str | None = "r1",
    clean: str | None = "r3",
    reviewers: Any = (LISTED,),
) -> dict[str, Any]:
    out: dict[str, Any] = {"verdict": "steward"}
    if ordinary is not None:
        out[CEILING] = ordinary
    if clean is not None:
        out[CLEAN_CEILING] = clean
    if reviewers is not None:
        out[REVIEWERS] = list(reviewers) if isinstance(reviewers, tuple) else reviewers
    return out


async def set_policy(db: aiosqlite.Connection, project_id: int, policy: dict) -> None:
    await repo.update_project(db, project_id, gate_policy=json.dumps(policy))
    await db.commit()


async def add_report(
    db: aiosqlite.Connection,
    task_id: int,
    principal: int | None,
    *,
    pinned: bool | str = True,
    sha: str = SHA,
    generation: int = 1,
    confirmed: tuple = (),
    unresolved: tuple = (),
    lost: tuple = (),
    incomplete: bool | None = False,
    self_reviewed: bool = False,
    carried_from: int | None = None,
    evidence: bool = True,
    raw: int | None = None,
) -> int:
    """Отчёт ревью; ``pinned`` - с пином, проверенным хабом (#1985)."""
    review_id = await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill="multi-agent-review",
        harness_version=1,
        agent_count=3 if evidence else None,
        duration_ms=1000,
        orchestrator="cursor",
        model="grok-4.6",
        # Исполнение видно: ревьюер видел кандидатов; пустой отчёт - частный случай.
        raw_count=raw
        if raw is not None
        else (len(confirmed) + 1 if evidence else 0),
        findings_confirmed=json.dumps(list(confirmed)),
        findings_rejected="[]",
        unresolved=json.dumps(list(unresolved)),
        lost_dimensions=json.dumps(list(lost)),
        incomplete=incomplete,
        submitted_by="reviewer",
        self_reviewed=self_reviewed,
        principal_id=principal,
        carried_from_review_id=carried_from,
    )
    if pinned == "claimed":  # sha назван, но хаб пин не проверял
        await db.execute(
            "UPDATE machine_reviews SET reviewed_sha=? WHERE id=?", (sha, review_id)
        )
    elif pinned:
        await db.execute(
            "UPDATE machine_reviews SET reviewed_sha=?, pin_verified=1 WHERE id=?",
            (sha, review_id),
        )
    await db.commit()
    return review_id


async def seed_ci(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    status: str = VALIDATION_PASS,
    attempt: str = "ci:1:1",
) -> None:
    await upsert_hub_report(
        db,
        task_id=task_id,
        head_sha=SHA,
        ac_results="[]",
        validation_status=status,
        validation_log="",
        reason="",
        reported_by="ci",
        **{**VERIFIED_ROW, "attempt_key": attempt},
    )
    await seed_clean_integrity(db, task_id, SHA)
    await db.commit()


def clean_packets(monkeypatch, *, stored: str = "R3", recomputed: str = "R3") -> None:
    """Живой сборщик пакета с подставленными фактами, которых нет без клона."""
    real = steward_evidence.build_evidence_packet

    async def build(db, task_id, generation=None):
        packet = await real(db, task_id, generation)
        if packet is None:
            return None
        task = dict(await repo.get_task(db, task_id) or {})
        sha = str(task.get("submission_sha") or SHA)
        facts = dict(packet.facts)
        facts["branch_tip"] = present(
            "branch_tip", "вершина на месте", tip=sha, pinned_sha=sha, moved=False
        )
        facts["diff_vs_areas"] = present(
            "diff_vs_areas",
            "дифф в границах",
            paths=["hub/services/x.py"],
            undeclared=[],
            within_declared=True,
        )
        facts["risk_class"] = present(
            "risk_class",
            "класс",
            stored=str(task.get("risk_class") or stored),
            recomputed=recomputed,
            raised=False,
            reasons=[],
        )
        return dataclasses.replace(packet, facts=facts)

    for module in (steward_apply, steward_evidence, steward_applied):
        if hasattr(module, "build_evidence_packet"):
            monkeypatch.setattr(module, "build_evidence_packet", build)


async def grant_act(monkeypatch) -> None:
    async def _act(_db):
        return "act"

    async def _granted_now(_db):
        return True

    monkeypatch.setattr(sh, "effective_mode", _act)
    monkeypatch.setattr(sh, "act_granted_now", _granted_now)  # #2040
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 50)
    monkeypatch.setattr(config, "STEWARD_ADVISOR_WAIT_MAX", 60)
    monkeypatch.setattr(config, "STEWARD_MODEL", "gpt-5.3-codex")
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)

    async def _converged(_db, _task_id, _generation):
        from tests.test_steward_applied import _decide

        return _decide()

    monkeypatch.setattr(steward_applied, "self_approval_for", _converged)


@dataclass
class Case:
    project_id: int
    task_id: int
    main: int | None = None
    extra: int | None = None


async def build_case(
    db: aiosqlite.Connection,
    monkeypatch,
    slug: str,
    *,
    policy: dict[str, Any] | None = None,
    risk: str = "R3",
    implementer: int | None = IMPLEMENTER,
    ci: str = VALIDATION_PASS,
    main: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    pair: bool = True,
    order: bool = True,
    concur: str | None = "concur",
    after_order: Callable[[Case], Awaitable[None]] | None = None,
) -> Case:
    """Сдача с парой отчётов, заказом судьи, approve и ответом советника.

    ``main`` и ``extra`` - поля ``add_report`` (принципал и порча);
    ``order=False`` оставляет сцену до заказа, чтобы тест добавил отчёты сам.
    """
    await grant_act(monkeypatch)
    clean_packets(monkeypatch, stored=risk, recomputed=risk)
    project_id = await _project(db, slug)
    await set_policy(db, project_id, policy if policy is not None else policy_of())
    task_id = await _task(db, project_id)
    await repo.update_task(
        db,
        task_id,
        status="review",
        review_job_id="",
        risk_class=risk,
        implementer_principal_id=implementer,
    )
    await db.commit()
    await seed_ci(db, task_id, status=ci)
    case = Case(project_id, task_id)
    if pair:
        case.main = await add_report(
            db, task_id, **{"principal": OTHER, **(main or {})}
        )
        case.extra = await add_report(
            db, task_id, **{"principal": LISTED, **(extra or {})}
        )
    if order:

        async def late() -> None:
            if after_order is not None:
                await after_order(case)

        await judge_and_advise(db, task_id, concur=concur, after_order=late)
    return case


async def judge_and_advise(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    concur: str | None = "concur",
    generation: int = 1,
    after_order: Callable[[], Awaitable[None]] | None = None,
) -> str:
    """Заказ судьи (фиксирует пару), approve, ответ советника на хеше пакета.

    ``after_order`` - то, что происходит между заказом судьи и его суждением:
    отчёт, пришедший после заказа.
    """
    run = await order_run(db, task_id, generation)
    assert run is not None
    if after_order is not None:
        await after_order()
    packet = await steward_evidence.build_evidence_packet(db, task_id, generation)
    digest = packet_hash(packet)
    await _mark_started(db, run, model="gpt-5.3-codex", packet=digest)
    await _judge(db, task_id, generation=generation)
    if concur is not None:
        advisor = await _advisor_run(db, task_id, packet=digest, generation=generation)
        assert advisor is not None
        await _advise(db, task_id, verdict=concur, generation=generation)
    return digest


async def verdict_events(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    rows = await db.execute_fetchall(
        "SELECT * FROM events WHERE kind='review_verdict_recorded' AND task_id=? "
        "ORDER BY id",
        (task_id,),
    )
    out = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item["payload"] or "{}")
        out.append(item)
    return out


async def verdict_of(db: aiosqlite.Connection, task_id: int) -> str:
    return str(dict(await repo.get_task(db, task_id)).get("review_verdict") or "")


async def feed(db: aiosqlite.Connection, task_id: int) -> str:
    return "\n".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )


def ceiling_detail(refusals: list[tuple[str, str]]) -> str:
    """Подробность отказа по потолку класса, или пусто, если его нет."""
    return " | ".join(d for c, d in refusals if c == "class_above_policy_ceiling")
