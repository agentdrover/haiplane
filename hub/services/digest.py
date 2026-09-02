"""Autopilot daily digest and sampling audit (#739, epic #736).

The autopilot (#744/#745) removed pre-approval clicks; this module keeps
the OVERSIGHT: every project whose gate_policy delegates anything gets one
digest per UTC day of autopilot activity — what the policy approved and on
which grounds, what it escalated, what the pipeline delivered — plus a
deterministic sample marked for a human spot check — ~10% of decisions,
applied approvals oversampled to ~30% (#1144) — of tasks
check. A day with no autopilot transitions produces no digest: an empty
report read daily becomes noise, and noise is how oversight dies quietly.

Delivery is двойная: a ``digest_created`` event in the feed (so
hub_wait_events and the Stop hook can bring it into chat) and the /digests
page in the web panel. The spot-check results flow back into the
human_gates metric (#737) as the ``audit`` gate — the post-hoc signal the
expand-or-roll-back decision is supposed to read.

Since #1143 the steward (#994) is covered by the same digest rather than a
second one. Its decisions arrive with their GROUNDS, because a verdict on
its own gives a reader nothing to check, and "delegating" now means the
policy autopilot OR the steward: a project that hands only the verdict to
the steward used to fall outside the check and get no digest at all, which
is indistinguishable from a quiet day.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from hub.db import fetchall
from hub import repository as repo
from hub.services.gate_events import STEWARD_JUDGEMENT
from hub.services.project_policy import DELEGATED_VERDICTS

log = logging.getLogger(__name__)

_AUTOPILOT_EVENT_KINDS = (
    "task_approved",
    "review_verdict_recorded",
    "verdict_escalated",
    # #1143: the steward writes its own kind (#1023). Reading it here rather
    # than in a second query keeps ONE project-attribution walk: two walks
    # would eventually disagree about which project a task belongs to, and
    # the one that disagreed silently would be the oversight half.
    STEWARD_JUDGEMENT,
)

# Which gate_policy values mean "somebody other than a person decides here".
# "auto" was the only one while the policy autopilot was the only delegate;
# a project that hands the verdict to the steward has delegated exactly as
# much, and asking for "auto" specifically refused it a digest entirely
# (#1143).
#
# #1151: перечень переехал в project_policy и стал ОДНИМ на хаб — его же
# читают автовердикт, диспетчер ревью и замок #743. Здесь стоит псевдоним
# на общий: свой список рядом с общим означал бы, что новый делегат
# появится в одном месте и не появится в другом.
_DELEGATED_TO_MACHINE = DELEGATED_VERDICTS


# Порог совпадения по модулю 10 для обычной (базовой) выборки — ~10%. Число
# само по себе ничего не значит, важно только его отношение к _OVERSAMPLE_
# _THRESHOLD ниже: оба порога читают ОДИН и тот же остаток от ОДНОГО и того
# же sha256(tid:date), только сравнивают его с разными границами. Это и
# делает oversample «тем же хешем с другим порогом», а не вторым проходом.
_BASE_THRESHOLD = 1

# Порог для «применённого approve» — решения, которое уже изменило исход и
# которого человек не видел (#1144). Втрое шире базового: такое решение
# обязано попадать в спот-чек чаще среднего, но 3 из 10 — это всё ещё
# выборка, а не «взять всё» (тот было бы 10 из 10, то есть 100%). Если это
# число когда-нибудь дорастёт до _BASE_THRESHOLD * 10, oversample перестанет
# быть надзором и станет отменой делегирования — граница нарочно далека от
# этого края.
_OVERSAMPLE_THRESHOLD = 3


def deterministic_sample(
    task_ids: list[int],
    digest_date: str,
    *,
    oversample_ids: frozenset[int] | set[int] = frozenset(),
) -> list[int]:
    """~10% of ``task_ids``, at least one, stable for (ids, date).

    Hash-based rather than random on purpose: the same day recomputed must
    name the same tasks, or the audit trail cannot be reasoned about.

    ``oversample_ids`` names the subset that gets a wider window on the SAME
    hash (#1144): an applied approve — a machine decision that already took
    effect and that a person never saw — is the most expensive kind of
    mistake to miss, so it is checked against ``_OVERSAMPLE_THRESHOLD``
    (~30%) instead of ``_BASE_THRESHOLD`` (~10%). A task outside
    ``oversample_ids`` (an escalation, a changes-requested verdict, ...)
    still gets its 10% chance — the old bug was not "escalations should be
    rare in the sample", it was "escalations never entered the sample at
    all". Two categories, one pass, one hash: a second pass computing its
    own picks would give the escalations a DIFFERENT 10% than the approvals
    see, and the two sets would disagree about which day was audited.
    """
    if not task_ids:
        return []
    universe = sorted(set(task_ids))
    picked = [
        tid
        for tid in universe
        if int(hashlib.sha256(f"{tid}:{digest_date}".encode()).hexdigest(), 16) % 10
        < (_OVERSAMPLE_THRESHOLD if tid in oversample_ids else _BASE_THRESHOLD)
    ]
    if not picked:
        # Minimum one: an audit sample of zero is no audit at all. The
        # choice stays deterministic — lowest hash wins.
        picked = [
            min(
                universe,
                key=lambda tid: hashlib.sha256(
                    f"{tid}:{digest_date}".encode()
                ).hexdigest(),
            )
        ]
    return picked


# Значение вердикта автопилота, при котором решение уже применилось и
# пропустило задачу дальше — сравнивается со строкой из ReviewVerdict.value
# (hub/models.py), не с вокабуляром стюарда: у автопилота "approved" (с "d"),
# у стюарда "approve" — они пишутся в РАЗНЫЕ поля разными системами, и
# смешение написаний ничего не сломало бы явно, а просто тихо перестало бы
# ловить половину применённых approve.
_POLICY_VERDICT_APPROVED = "approved"

# Вердикт стюарда, который значит то же самое: решение применилось, а не
# ушло эскалацией. STEWARD_VERDICTS (hub/models.py) — "approve" без "d".
_STEWARD_VERDICT_APPROVE = "approve"


def _audit_pool_and_oversample(
    approvals: list[dict],
    verdicts: list[dict],
    escalations: list[dict],
    steward: list[dict],
) -> tuple[list[int], set[int]]:
    """Кого спот-чек вообще может выбрать, и кого — выбрать охотнее (#1144).

    Пул — это ВСЕ решения дня без разбора: одобрения DoR, вердикты ревью
    (любые, не только approved), эскалации автопилота и ЛЮБОЕ суждение
    стюарда, включая его собственные эскалации. До этой правки эскалации
    (обеих систем) и суждения стюарда в пул не попадали вовсе — эскалация
    трактовалась как «и так уйдёт человеку», а для стюарда эскалация — это
    его ОТКАЗ судить, который сам нуждается в проверке не меньше approve.

    Oversample — подмножество пула, у которого решение уже ПРИМЕНИЛОСЬ и
    пропустило задачу дальше без участия человека: одобрения DoR (они по
    определению применяются сразу), approved-вердикты автопилота и
    approve-суждения стюарда. Именно эта категория — самая дорогая ошибка:
    решение уже изменило исход. changes_requested и escalate туда не
    входят — это как раз решения «на всякий случай / отказ судить», ошибка
    в которых стоит дешевле (лишний цикл ревью или лишний взгляд человека),
    а не дороже.
    """
    approval_ids = [a["task_id"] for a in approvals]
    verdict_ids = [v["task_id"] for v in verdicts]
    escalation_ids = [e["task_id"] for e in escalations]
    steward_ids = [s["task_id"] for s in steward]
    pool = approval_ids + verdict_ids + escalation_ids + steward_ids

    oversample = set(approval_ids)
    oversample.update(
        v["task_id"]
        for v in verdicts
        if str(v["payload"].get("verdict") or "").lower() == _POLICY_VERDICT_APPROVED
    )
    oversample.update(
        s["task_id"] for s in steward if s.get("verdict") == _STEWARD_VERDICT_APPROVE
    )
    return pool, oversample


def _policy_delegates(gate_policy_raw: str | None) -> bool:
    """Does this project let a machine decide anything at all?

    True for the policy autopilot ("auto") and for the steward ("steward").
    The digest is the oversight of delegated decisions, so the question is
    "is anything delegated", not "is it delegated to the autopilot" — the
    narrower reading left a steward-only project with no digest at all, and
    a missing digest looks exactly like a quiet day (#1143).
    """
    try:
        policy = json.loads(gate_policy_raw or "{}")
    except ValueError:
        return False
    if not isinstance(policy, dict):
        return False
    return any(
        isinstance(value, str) and value in _DELEGATED_TO_MACHINE
        for value in policy.values()
    )


# The window the category debt is read over. Wider than a digest's own day on
# purpose: a class that recurs across three tasks does not do so within
# twenty-four hours, and a one-day window would report an empty debt every
# morning (#878).
_DEBT_WINDOW = "-90 days"


# The payload key the steward section lives under, and the question a reader
# of an OLD digest must be able to answer: was the steward silent that day,
# or was nobody counting? Digests written before #1143 carry no such key, and
# a count filter reads a missing key as 0 — a measured silence where nothing
# was measured. That is the #762/#750 mistake applied to my own section, and
# the answer belongs in code rather than in a template, next to the grounds
# vocabulary it mirrors.
STEWARD_SECTION_KEY = "steward_judgements"

MEASURED = "measured"
UNMEASURED = "unmeasured"


def steward_section_state(payload: dict) -> str:
    """Did this digest count the steward at all?

    ``measured`` — the key is there, and its length is a real number, zero
    included: a delegating project with a quiet steward genuinely judged
    nothing that day.
    ``unmeasured`` — the digest predates the section. Zero would be a claim
    about a day nobody looked at.
    """
    return MEASURED if STEWARD_SECTION_KEY in payload else UNMEASURED


async def _steward_entry(db: aiosqlite.Connection, entry: dict, payload: dict) -> dict:
    """One steward decision the way the digest must show it: WITH its grounds.

    A verdict on its own is not something a person can check — «одобрено»
    tells the reader what happened and nothing about whether it should have.
    The grounds are the whole reason a shadow decision is worth reading, so
    they are fetched here rather than left to whoever renders the section.

    Absence is spelled out. Grounds are stored as a JSON string and an empty
    list serialises to "[]", which a template reads as truthy; a judgement
    that attached nothing would then look exactly like one that attached
    everything (#762). ``grounds_state`` says which of the two it is.
    """
    judged = await repo.get_steward_judgement(
        db,
        entry["task_id"],
        int(payload.get("generation") or 0),
        str(payload.get("kind") or ""),
    )
    grounds: list = []
    if judged is not None:
        try:
            parsed = json.loads(dict(judged).get("grounds") or "[]")
        except (TypeError, ValueError):
            parsed = []
        grounds = parsed if isinstance(parsed, list) else []
    return {
        **entry,
        "verdict": str(payload.get("verdict") or ""),
        "judgement_kind": str(payload.get("kind") or ""),
        "generation": int(payload.get("generation") or 0),
        "grounds": grounds,
        "grounds_state": "present" if grounds else "absent",
    }


async def generate_due_digests(
    db: aiosqlite.Connection, *, now: datetime | None = None
) -> int:
    """Create digests for YESTERDAY (UTC) where autopilot activity exists.

    Idempotent: the UNIQUE(project_id, digest_date) key plus the insert
    guard make repeated poller passes harmless. Returns how many digests
    were created this pass.
    """
    moment = now or datetime.now(UTC)
    day = (moment - timedelta(days=1)).strftime("%Y-%m-%d")
    day_start = f"{day} 00:00:00"
    day_end = (moment - timedelta(days=1) + timedelta(days=1)).strftime(
        "%Y-%m-%d 00:00:00"
    )

    created = 0
    projects = await fetchall(
        db,
        "SELECT id, slug, gate_policy FROM projects "
        "WHERE archived=0 AND status='active'",
    )
    for project in projects:
        if not _policy_delegates(project["gate_policy"]):
            continue
        existing = await fetchall(
            db,
            "SELECT id FROM autopilot_digests WHERE project_id=? AND digest_date=?",
            (project["id"], day),
        )
        if existing:
            continue

        # One placeholder per entry of _AUTOPILOT_EVENT_KINDS. Written out
        # rather than generated: building SQL by concatenation is the shape
        # every injection review has to stop and read, and here it buys
        # nothing — a miscount raises on the first execute, so every digest
        # test in this suite fails loudly rather than the poller failing at
        # midnight.
        events = await fetchall(
            db,
            "SELECT id, kind, actor, task_id, payload, created_at FROM events "
            "WHERE kind IN (?, ?, ?, ?) AND created_at >= ? AND created_at < ? "
            "ORDER BY id ASC",
            (*_AUTOPILOT_EVENT_KINDS, day_start, day_end),
        )
        approvals: list[dict] = []
        verdicts: list[dict] = []
        escalations: list[dict] = []
        steward: list[dict] = []
        for event in events:
            if event["task_id"] is None:
                continue
            # Attribution walks the hierarchy — the same resolver as the
            # git conveyor and the human_gates metric (#747).
            owner = await repo.resolve_project_for_task(db, event["task_id"])
            if owner is None or owner["id"] != project["id"]:
                continue
            try:
                payload = json.loads(event["payload"] or "{}")
            except ValueError:
                payload = {}
            entry = {
                "task_id": event["task_id"],
                "at": event["created_at"],
                "payload": payload,
            }
            if event["kind"] == "task_approved" and event["actor"] == "policy":
                approvals.append(entry)
            elif (
                event["kind"] == "review_verdict_recorded"
                and event["actor"] == "policy"
            ):
                # Model diversity (#758): the digest shows WHO wrote and WHO
                # reviewed — the pair the monoculture rule compares.
                task_row = await repo.get_task(db, event["task_id"])
                mr = await repo.get_latest_machine_review(db, event["task_id"])
                entry["models"] = {
                    "implementer": (
                        dict(task_row).get("submission_model", "") if task_row else ""
                    ),
                    "reviewer": (dict(mr).get("model", "") if mr else ""),
                }
                verdicts.append(entry)
            elif event["kind"] == "verdict_escalated":
                escalations.append(entry)
            elif event["kind"] == STEWARD_JUDGEMENT and event["actor"] == "steward":
                steward.append(await _steward_entry(db, entry, payload))

        if not (approvals or verdicts or escalations or steward):
            # The empty-day rule now covers the steward too, in both
            # directions: a day of steward-only activity IS a day worth a
            # digest, and a day with neither still produces nothing. An
            # empty report read daily stops being read within a week, and
            # that is how oversight dies quietly (#739).
            continue

        merges = await fetchall(
            db,
            "SELECT pr_number, task_id, merge_sha, merged_at FROM pipeline_merges "
            "WHERE project_id=? AND merged_at >= ? AND merged_at < ?",
            (project["id"], day_start, day_end),
        )
        pool, oversample = _audit_pool_and_oversample(
            approvals, verdicts, escalations, steward
        )
        sample = deterministic_sample(pool, day, oversample_ids=oversample)
        # #878: the debt rides along with a digest that is being created for
        # other reasons. It does NOT cause one: this digest is per-project and
        # only exists on days with autopilot activity, while the debt is a
        # property of the practice. Making it a trigger would have it arrive
        # on some days and not others with no way to tell which.
        from hub.services.orchestration import (
            build_category_debt,
            recurring_categories,
        )

        debt = [
            d
            for d in await build_category_debt(
                db, await recurring_categories(db, _DEBT_WINDOW)
            )
            if not d["covered"]
        ]
        # #1020: the human queue rides along the same way the category debt
        # does, and for the same reason — it is a property of the practice,
        # not of this day's autopilot activity. Riding along has a real cost
        # to state plainly: a digest is only created for a delegating project
        # on a day with autopilot events, so this line appears when a digest
        # happens to exist. The reminder's dependable channel is the events
        # feed the poller writes; the digest is the summary, not the alarm.
        human_queue = await repo.human_queue_reminders_between(db, day_start, day_end)
        payload = {
            "date": day,
            "project": project["slug"],
            "category_debt": debt,
            "human_queue": human_queue,
            "auto_approvals": approvals,
            "auto_verdicts": verdicts,
            "escalations": escalations,
            STEWARD_SECTION_KEY: steward,
            "deliveries": [dict(m) for m in merges],
            "audit_sample": sample,
            "audit_results": {},
        }
        digest_id = await repo.create_digest(
            db,
            project_id=project["id"],
            digest_date=day,
            payload=json.dumps(payload, ensure_ascii=False),
        )
        if digest_id is None:
            continue
        await repo.insert_event(
            db,
            kind="digest_created",
            project_id=project["id"],
            actor="hub",
            payload={
                "digest_id": digest_id,
                "date": day,
                "auto_approvals": len(approvals),
                "auto_verdicts": len(verdicts),
                "escalations": len(escalations),
                "steward_judgements": len(steward),
                "audit_sample": sample,
            },
        )
        await db.commit()
        created += 1
        log.info(
            "digest #%s created for project %s (%s): %d approvals, "
            "%d verdicts, %d escalations, %d steward judgements, sample %s",
            digest_id,
            project["slug"],
            day,
            len(approvals),
            len(verdicts),
            len(escalations),
            len(steward),
            sample,
        )
    return created


async def record_audit_result(
    db: aiosqlite.Connection,
    digest_id: int,
    task_id: int,
    result: str,
    comment: str = "",
) -> dict:
    """Store a spot-check outcome for a sampled task (human gate, #739).

    Writes three places at once: the digest payload (the page shows the
    checkbox state), the task feed (the task carries its own audit trail)
    and the events feed (the human_gates metric reads the ``audit`` gate
    from there). Raises ValueError on an unknown digest or a task outside
    its sample — auditing a task nobody sampled would fabricate coverage.
    """
    row = await repo.get_digest(db, digest_id)
    if row is None:
        raise ValueError(f"digest #{digest_id} not found")
    payload = json.loads(row["payload"] or "{}")
    sample = payload.get("audit_sample") or []
    if task_id not in sample:
        raise ValueError(
            f"task #{task_id} is not in the audit sample of digest #{digest_id}"
        )
    payload.setdefault("audit_results", {})[str(task_id)] = result
    await repo.update_digest_payload(
        db, digest_id, json.dumps(payload, ensure_ascii=False)
    )
    verdict_text = "ок" if result == "ok" else "найдена проблема"
    await repo.add_task_update(
        db,
        task_id,
        "human",
        "decision",
        (
            f"Выборочный аудит автопилота (дайджест #{digest_id}, "
            f"{payload.get('date')}): {verdict_text}."
            + (f" {comment}" if comment else "")
        ),
    )
    await repo.insert_event(
        db,
        kind="audit_result",
        task_id=task_id,
        project_id=row["project_id"],
        actor="human",
        payload={"digest_id": digest_id, "result": result},
    )
    await db.commit()
    return payload
