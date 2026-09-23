"""Record a steward judgement. No transition (#1022); events are the audit (#1023)."""

from __future__ import annotations

import json
import logging

from fastapi import HTTPException

from hub import repository as repo
from hub.actionable_errors import (
    steward_closed_vocabulary_detail,
    steward_escalate_reason_required_detail,
    steward_judgement_exists_detail,
    steward_unknown_finding_uid_detail,
    steward_verdict_required_detail,
)
from hub.config import TokenIdentity
from hub.db import fetchall
from hub.models import (
    STEWARD_CLOSURE_TYPES,
    STEWARD_CONFIDENCE,
    STEWARD_ESCALATE_REASONS,
    STEWARD_GROUND_SOURCES,
    STEWARD_JUDGEMENT_KINDS,
    STEWARD_VERDICTS,
    StewardJudgementSubmit,
    StewardJudgementView,
)
from hub.services.finding_identity import finding_uids, unresolved_uids
from hub.services.gate_events import (
    STEWARD_APPLIED,
    STEWARD_ESCALATED,
    STEWARD_JUDGEMENT,
)


log = logging.getLogger(__name__)

# Почему у суждения нет числа токенов (#1328). Пустая строка — число есть.
# Ноль и «неизвестно» — разные состояния: ноль говорит провайдер, а
# «неизвестно» говорит хаб, и путать их значит выдать молчание за бесплатность.
TOKENS_PENDING = "pending"
TOKENS_PROVIDER_NO_ANSWER = "provider_no_answer"
TOKENS_NO_RUN = "no_run"
# Сколько ждать usage после записи суждения, прежде чем назвать молчание
# провайдера окончательным. Usage приходит с задержкой, и прогон может ещё
# дописывать свой ответ после того, как суждение легло.
USAGE_ANSWER_WINDOW_MIN = 60


def _require_member(field: str, got: str, allowed: tuple[str, ...]) -> None:
    if got not in allowed:
        raise HTTPException(
            422,
            detail=steward_closed_vocabulary_detail(field, got, allowed),
        )


def _downgrade_reason(verdict: str, confidence: str, grounds: list) -> str:
    """Why a verdict is stored as an escalation, or "" when it stands.

    ``low`` confidence beats any verdict (#1022). An approve or a return that
    names no ground, or states no confidence, cannot be checked by a human or
    by the hub — it is stored as an escalation too (#1327). An escalation
    needs neither: it already says "I cannot judge this".
    """
    if confidence == "low":
        return "low_confidence"
    if verdict == "escalate":
        return ""
    if not grounds:
        return "no_grounds"
    if not confidence:
        return "no_confidence"
    return ""


async def record_steward_judgement(
    db,
    task_id: int,
    body: StewardJudgementSubmit,
    identity: TokenIdentity,
    expected_generation: int | None = None,
) -> StewardJudgementView:
    """Validate and store a judgement. The task status is not touched.

    ``expected_generation`` is the pin the caller's session carries (#1120).
    When it is set, a judgement about any other generation is refused: a run
    ordered for one submission does not get to rule on the next one just
    because the author resubmitted while it was thinking.
    """
    row = await repo.get_task(db, task_id)
    if row is None:
        raise HTTPException(404, "task not found")
    if expected_generation is not None:
        # The SAME guard the evidence door uses (#1120): one rule, two
        # entrances. A judgement filed for another generation — or for one
        # that stopped being current while the run was thinking — is refused
        # here, not silently stored beside the live submission.
        from hub.services.steward_evidence import pinned_generation

        pinned_generation(identity, dict(row), body.generation)

    submitted_verdict = (body.verdict or "").strip()
    if not submitted_verdict:
        raise HTTPException(
            422,
            detail=steward_verdict_required_detail(),
        )
    _require_member("kind", body.kind, STEWARD_JUDGEMENT_KINDS)
    _require_member("verdict", submitted_verdict, STEWARD_VERDICTS)
    confidence = (body.confidence or "").strip()
    if confidence:
        _require_member("confidence", confidence, STEWARD_CONFIDENCE)
    for ground in body.grounds:
        _require_member("ground.source", ground.source, STEWARD_GROUND_SOURCES)
    for closure in body.closures:
        _require_member("closure.type", closure.type, STEWARD_CLOSURE_TYPES)

    escalate_reason = (body.escalate_reason or "").strip()
    downgrade = _downgrade_reason(submitted_verdict, confidence, body.grounds)
    if downgrade:
        effective_verdict = "escalate"
        effective_reason = downgrade
    else:
        effective_verdict = submitted_verdict
        if effective_verdict == "escalate":
            if not escalate_reason:
                raise HTTPException(
                    422,
                    detail=steward_escalate_reason_required_detail(
                        STEWARD_ESCALATE_REASONS
                    ),
                )
            _require_member(
                "escalate_reason", escalate_reason, STEWARD_ESCALATE_REASONS
            )
            effective_reason = escalate_reason
        else:
            if escalate_reason:
                _require_member(
                    "escalate_reason", escalate_reason, STEWARD_ESCALATE_REASONS
                )
            effective_reason = escalate_reason

    if body.closures:
        await _refuse_unknown_closure_uids(db, task_id, body.generation, body.closures)

    model, duration_ms, tokens_reason = await _cost_of_the_run(
        db, task_id, body.generation, body.kind, body.model
    )
    inserted = await repo.insert_steward_judgement(
        db,
        task_id=task_id,
        generation=body.generation,
        kind=body.kind,
        submitted_verdict=submitted_verdict,
        verdict=effective_verdict,
        confidence=confidence,
        escalate_reason=effective_reason,
        grounds=json.dumps([g.model_dump() for g in body.grounds], ensure_ascii=False),
        findings=json.dumps(body.findings, ensure_ascii=False),
        closures=json.dumps(
            [c.model_dump() for c in body.closures], ensure_ascii=False
        ),
        model=model,
        tokens_spent=None,
        duration_ms=duration_ms,
        submitted_by=identity.username[:100],
        principal_id=identity.principal_id,
        tokens_unknown_reason=tokens_reason,
    )
    if inserted is None:
        raise HTTPException(
            409,
            detail=steward_judgement_exists_detail(task_id, body.generation, body.kind),
        )
    await repo.add_task_update(
        db,
        task_id,
        identity.username,
        "status",
        f"Steward judgement recorded: {body.kind} {effective_verdict}.",
        principal_id=identity.principal_id,
        author_kind="steward",
    )
    payload = {
        "kind": body.kind,
        "verdict": effective_verdict,
        "generation": body.generation,
    }
    await repo.insert_event(
        db,
        kind=STEWARD_JUDGEMENT,
        task_id=task_id,
        actor="steward",
        payload=payload,
    )
    follow_up = (
        STEWARD_ESCALATED if effective_verdict == "escalate" else STEWARD_APPLIED
    )
    await repo.insert_event(
        db,
        kind=follow_up,
        task_id=task_id,
        actor="steward",
        payload=payload,
    )
    await _close_the_order(db, task_id, body.generation, body.kind)
    await db.commit()
    await _self_approve_if_everything_converged(
        db, task_id, body.generation, body.kind, effective_verdict
    )
    saved = await repo.get_steward_judgement_by_id(db, inserted)
    if saved is None:
        raise RuntimeError(
            f"steward judgement {inserted} missing after insert for task #{task_id}"
        )
    return StewardJudgementView(**dict(saved))


async def _cost_of_the_run(
    db, task_id: int, generation: int, kind: str, declared_model: str
) -> tuple[str, int | None, str]:
    """Модель, длительность и состояние токенов — по прогону, не по словам (#1328).

    Стюард декларирует модель, токены и длительность сам, и на проде
    22.09.2026 писал пустую строку или «codex-5.3», пока прогон шёл на
    gpt-5.3-codex. Хаб знает всё это лучше судьи: модель — в строке прогона
    (её пишет старт, с учётом замены судьи #1182), старт — там же.

    Длительность меряется от ``started_at``, а не от ``created_at``: второе —
    время заказа, и заказ мог ждать исполнителя сколько угодно (#1181).

    Токены при записи не известны НИКОМУ: провайдер отдаёт usage после
    конца прогона, а судья заявляет цифру, которую никто не сверял. Поэтому
    здесь только причина ``pending`` — число дописывает свип
    (:func:`stamp_judgement_usage`). Провайдер здесь не зовётся вовсе:
    запись суждения не ждёт его и не падает из-за него.

    Без начатого прогона (суждение, поданное мимо заказа) источника нет:
    модель остаётся заявленной, длительности нет, токены — ``no_run``.
    """
    from hub.services.steward_dispatch import open_run, run_has_started

    run = await open_run(db, task_id, generation, kind)
    if run is None or not run_has_started(run):
        return declared_model, None, TOKENS_NO_RUN
    duration_ms: int | None = None
    if run.get("started_at"):
        rows = await fetchall(
            db,
            "SELECT CAST(ROUND((julianday('now') - julianday(?)) * 86400000) "
            "AS INTEGER) AS ms",
            (run["started_at"],),
        )
        ms = dict(rows[0]).get("ms") if rows else None
        duration_ms = max(int(ms), 0) if ms is not None else None
    return str(run.get("model") or "") or declared_model, duration_ms, TOKENS_PENDING


async def stamp_judgement_usage(db) -> int:
    """Дописать токены суждениям, которые их ждут (#1328). Шаг свипа.

    Спрашивается ТОЛЬКО закончившийся прогон: судья кладёт суждение
    посреди своей работы и дописывает ответ после, и usage, снятый в эту
    секунду, был бы неполным числом, которое уже никто не поправит.

    Молчание провайдера не становится нулём. Пока окно ответа
    (``USAGE_ANSWER_WINDOW_MIN``) не вышло, суждение ждёт следующего прохода;
    после — причина ``provider_no_answer``, а ``tokens_spent`` остаётся NULL.
    Ошибка провайдера — то же молчание: свип best-effort и не падает.
    """
    from hub.integrations import cursor_cloud
    from hub.services.review_dispatch import (
        _TERMINAL_RUN_STATUSES,
        _provider_token_total,
    )

    rows = await fetchall(
        db,
        "SELECT j.id, r.agent_id, r.run_id, "
        "j.created_at <= datetime('now', ?) AS window_over "
        "FROM steward_judgements j JOIN steward_runs r "
        "ON r.task_id=j.task_id AND r.generation=j.generation AND r.kind=j.kind "
        "WHERE j.tokens_unknown_reason=? AND r.status != 'open'",
        (f"-{USAGE_ANSWER_WINDOW_MIN} minutes", TOKENS_PENDING),
    )
    stamped = 0
    for row in rows:
        item = dict(row)
        total: int | None = None
        try:
            run = await cursor_cloud.get_run(item["agent_id"], item["run_id"])
            if str((run or {}).get("status") or "").upper() in _TERMINAL_RUN_STATUSES:
                total = _provider_token_total(
                    await cursor_cloud.get_usage(
                        item["agent_id"], item["run_id"] or None
                    )
                )
        except Exception as exc:  # noqa: BLE001 — молчание, а не авария свипа
            log.warning("steward usage not read for judgement %s: %s", item["id"], exc)
        if total is not None:
            await repo.set_steward_judgement_tokens(db, item["id"], total, "")
        elif item["window_over"]:
            await repo.set_steward_judgement_tokens(
                db, item["id"], None, TOKENS_PROVIDER_NO_ANSWER
            )
        else:
            continue
        stamped += 1
    if stamped:
        await db.commit()
    return stamped


async def _self_approve_if_everything_converged(
    db, task_id: int, generation: int, kind: str, verdict: str
) -> None:
    """Записанный approve может уехать без человека — если всё сошлось (#1231).

    ТОЛЬКО ``kind=verdict`` и ТОЛЬКО ``approve``. Про драфт здесь решать
    нечего — у DoR свой привратник (#1159), он в scope_out #1231. А
    ``changes_requested`` и ``escalate`` самостоятельного одобрения не
    порождают по определению, и звать правило на них значило бы спрашивать
    «сошлись ли свидетельства» там, где судья уже сказал «нет».

    Вызов стоит здесь, а не в проходе поллера, ради at-most-once: запись
    суждения случается ровно один раз на тройку (задача, поколение, kind) —
    повтор отбивает 409 контракта #1022. Поллер писал бы строку в карточку
    каждые тридцать секунд, пока задача стоит в review.

    Best effort ТЕМ ЖЕ договором, что и закрытие заказа выше: суждение уже
    записано и стоит независимо от того, чем кончилось применение. Но
    молчать про отказ нельзя — проглоченное исключение здесь неотличимо от
    «правило посмотрело и не одобрило», а это разные вещи: во втором случае
    задача ждёт человека осознанно, в первом — по недосмотру.
    """
    from hub.services.steward_dispatch import KIND_VERDICT

    if kind != KIND_VERDICT or verdict != "approve":
        return
    try:
        from hub.services.steward_applied import apply_self_approval

        await apply_self_approval(db, task_id, generation)
    except Exception as exc:  # noqa: BLE001 — суждение стоит в любом случае
        log.warning(
            "self-approval not applied for task #%s gen %s: %s",
            task_id,
            generation,
            exc,
        )


async def _refuse_unknown_closure_uids(
    db, task_id: int, generation: int, closures
) -> None:
    """Закрытие может ссылаться только на находку ЭТОГО отчёта — из ЛЮБОГО раздела.

    Разделов два, а не один (#1170). Пока множество известных uid строилось
    по ``findings_confirmed``, закрытие неразрешённой находки отклонялось как
    unknown_finding_uid — то есть привратник применения не мог бы принять
    закрытие, которого сам же требует, и правило про unresolved осталось бы
    невыполнимым по контракту.
    """
    reports = await repo.machine_reviews_of_generation(db, task_id, generation)
    known: set[str] = set()
    # Свой расчёт идентичности на каждый раздел: у неразрешённой записи нет
    # ни файла, ни строки, и общий расчёт выдал бы ей чужой id (#1085).
    for column, uids_of in (
        ("findings_confirmed", finding_uids),
        ("unresolved", unresolved_uids),
    ):
        for report in reports:
            raw = report[column]
            if isinstance(raw, str):
                try:
                    entries = json.loads(raw or "[]")
                except ValueError:
                    entries = []
            else:
                entries = raw or []
            if not isinstance(entries, list):
                continue
            known.update(uids_of(entries))
    for closure in closures:
        if closure.finding_uid not in known:
            raise HTTPException(
                422,
                detail=steward_unknown_finding_uid_detail(closure.finding_uid),
            )


async def _close_the_order(db, task_id: int, generation: int, kind: str) -> None:
    """The judgement arrived — the slot it was ordered for is done (#1106).

    Without this the order sits open until its deadline and closes as
    ``run_timeout``: the work is finished and the state says "still waiting".
    That difference matters twice — the daily cap counts an occupied slot,
    and the evidence door (#1075) stays open on an order nobody is filling.

    Best effort by contract: the judgement is already stored, and a slot that
    fails to close is a wrong state, not a lost answer. It closes on the next
    poller tick by deadline anyway.
    """
    try:
        from hub.services.steward_dispatch import RUN_JUDGED, close_run, open_run

        order = await open_run(db, task_id, generation, kind)
        if order is not None:
            await close_run(db, order, RUN_JUDGED, f"суждение записано: {kind}")
    except Exception as exc:  # noqa: BLE001 — the judgement stands regardless
        # WITH the reason (#1106 review). A swallowed failure that names
        # nothing is indistinguishable from "there was no order to close",
        # and the two need different answers: one is normal, the other is a
        # slot that will now expire by deadline as if nobody judged.
        log.warning(
            "steward order not closed after judgement: task #%s gen %s kind %s: %s",
            task_id,
            generation,
            kind,
            exc,
            exc_info=True,
        )
