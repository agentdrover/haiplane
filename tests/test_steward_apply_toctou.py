"""Право стюарда на автономное одобрение перепроверяется в транзакции записи (#2037).

Ранние проверки (делегирование вердикта и cap-handoff советника) читают базу ДО
записи вердикта. Между последней из них и ``_record`` второе соединение может
забрать вердикт себе (``verdict=human``) или передать сдачу человеку из-за
суточного потолка. Транзакция записи обязана спросить тех же читателей ещё раз
и отказать с той же причиной, не записав вердикт.

Сцена настоящая (файловая SQLite, два соединения): ``tests/steward_double_clean_support``.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi import HTTPException

from hub import config
from hub import db as db_module
from hub import repository as repo
from hub.models import ReviewVerdict, TaskReviewVerdict
from hub.services import steward_applied, steward_cap, verdict_evidence
from hub.services import steward_shadow as sh
from hub.services.refusal_reason import CodedRefusal
from hub.services.steward_advisor import apply_advisor_outcomes
from hub.services.steward_advisor import advisor_refusal
from hub.services.steward_apply import policy_refusal
from hub.services.steward_applied import APPLIED, apply_self_approval
from hub.services.lifecycle import record_review_verdict
from tests.steward_double_clean_support import (
    build_case,
    feed,
    policy_of,
    set_policy,
    verdict_events,
    verdict_of,
)


async def _switch_to_human(other, case, _monkeypatch) -> None:
    """Владелец забрал вердикт себе: политика проекта - verdict=human."""
    await set_policy(other, case.project_id, {**policy_of(), "verdict": "human"})


async def _cap_handoff(other, case, monkeypatch) -> None:
    """Сдача передана человеку: отказ ``daily_cap`` по заказу вердикта, потолок исчерпан."""
    later = steward_cap.utcnow() + timedelta(hours=1)
    monkeypatch.setattr(steward_cap, "utcnow", lambda: later)
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 0)
    await steward_cap.record_refusal(
        other, case.task_id, case.project_id, 1, "verdict", "потолок"
    )
    await other.commit()


async def _nothing(other, case, monkeypatch) -> None:
    """Контроль: между проверкой и записью второе соединение ничего не меняет."""


async def _apply_with_a_write_before_record(db, db_dsn, case, monkeypatch, change):
    """Применение стюарда; перед ``_record`` второе соединение делает ``change``.

    ``_record`` стоит сразу за последним ``advisor_refusal`` пути применения
    (``apply_judgement``): это и есть окно из постановки.
    """
    other = await db_module.connect(db_dsn)
    real_record = steward_applied._record

    async def record_after_the_other_writes(*args, **kwargs):
        await change(other, case, monkeypatch)
        return await real_record(*args, **kwargs)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(steward_applied, "_record", record_after_the_other_writes)
            return await apply_self_approval(db, case.task_id, 1)
    finally:
        await other.close()


async def test_control_without_a_change_the_approve_is_recorded(
    db, db_dsn, monkeypatch
):
    case = await build_case(db, monkeypatch, "toctou-control")

    result = await _apply_with_a_write_before_record(
        db, db_dsn, case, monkeypatch, _nothing
    )

    assert result is not None and result[0] == APPLIED, result
    assert await verdict_of(db, case.task_id) == ReviewVerdict.approved.value
    assert len(await verdict_events(db, case.task_id)) == 1


async def test_delegation_and_cap_rechecked_at_write(db, db_dsn, monkeypatch):
    """AC-1: оба случая окна - approve не записан, причина та же, что у ранней проверки."""
    seen: dict[str, str] = {}
    for label, change in (
        ("policy_not_delegated", _switch_to_human),
        ("cap", _cap_handoff),
    ):
        case = await build_case(db, monkeypatch, f"toctou-{label}")
        with pytest.raises(HTTPException) as caught:
            await _apply_with_a_write_before_record(
                db, db_dsn, case, monkeypatch, change
            )

        assert caught.value.status_code == 422, (label, caught.value.detail)
        assert await verdict_of(db, case.task_id) == "", label
        assert await verdict_events(db, case.task_id) == [], label
        # Причина - слово в слово ответ ранней проверки, прочитанный теперь.
        early = (
            await policy_refusal(db, case.task_id)
            if label == "policy_not_delegated"
            else await advisor_refusal(db, case.task_id, 1)
        )
        assert early is not None, label
        assert caught.value.detail == f"{early[0]}: {early[1]}", (
            label,
            caught.value.detail,
        )
        seen[label] = str(caught.value.detail)

    assert "policy_not_delegated" in seen["policy_not_delegated"]
    assert "потолок" in seen["cap"]


async def test_a_steward_return_for_rework_is_not_gated(db, monkeypatch):
    """Возврат правок стюардом права на АВТОНОМНОЕ ОДОБРЕНИЕ не требует: не затронут."""
    case = await build_case(db, monkeypatch, "toctou-changes-requested")
    await set_policy(db, case.project_id, {**policy_of(), "verdict": "human"})

    await record_review_verdict(
        db,
        case.task_id,
        TaskReviewVerdict(
            agent="steward",
            verdict=ReviewVerdict.changes_requested,
            comments="правки",
        ),
        expected_generation=1,
        applied_by_steward=True,
    )

    stored = dict(await repo.get_task(db, case.task_id))
    assert stored["review_verdict"] == ReviewVerdict.changes_requested.value
    assert json.dumps(stored["review_cycle"]) == "1"


_REAL_ACT_GRANTED_NOW = sh.act_granted_now


async def _real_global_act(monkeypatch) -> None:
    """Допуск ``act`` читает НАСТОЯЩИЙ чистый предикат: окружение и критерии выхода."""
    monkeypatch.setattr(sh, "act_granted_now", _REAL_ACT_GRANTED_NOW)
    monkeypatch.setattr(config, "STEWARD_MODE", "act")

    async def _no_unmet_criteria(_db):
        return []

    monkeypatch.setattr(sh, "act_refusals", _no_unmet_criteria)


async def _switch_off(other, case, monkeypatch) -> None:
    """Владелец выключил стюарда (HAIPLANE_STEWARD_MODE=off) после ранних проверок."""
    monkeypatch.setattr(config, "STEWARD_MODE", "off")


async def _criteria_revoked(other, case, monkeypatch) -> None:
    """Критерий выхода перестал выполняться (найдено ошибочное одобрение)."""

    async def _unmet(_db):
        return [("false_approve", "найдено ошибочное одобрение")]

    monkeypatch.setattr(sh, "act_refusals", _unmet)


async def test_global_act_rechecked_at_write(db, db_dsn, monkeypatch):
    """AC-1: стюарда выключили между ранней проверкой и записью - approve не записан."""
    # Контроль: ничего не менялось - approve записан (предикат настоящий).
    control = await build_case(db, monkeypatch, "toctou-act-control")
    await _real_global_act(monkeypatch)
    result = await _apply_with_a_write_before_record(
        db, db_dsn, control, monkeypatch, _nothing
    )
    assert result is not None and result[0] == APPLIED, result
    assert await verdict_of(db, control.task_id) == ReviewVerdict.approved.value

    for label, change in (("env_off", _switch_off), ("criteria", _criteria_revoked)):
        case = await build_case(db, monkeypatch, f"toctou-act-{label}")
        await _real_global_act(monkeypatch)
        with pytest.raises(HTTPException) as caught:
            await _apply_with_a_write_before_record(
                db, db_dsn, case, monkeypatch, change
            )

        assert caught.value.status_code == 422, (label, caught.value.detail)
        assert str(caught.value.detail).startswith("act_revoked: "), label
        assert "автономия act сейчас не выдана" in str(caught.value.detail), label
        if (
            label == "criteria"
        ):  # журнал получает НЕ выполненный критерий, а не общее слово
            assert "false_approve" in str(caught.value.detail), label
        assert await verdict_of(db, case.task_id) == "", label
        assert await verdict_events(db, case.task_id) == [], label


async def test_the_act_predicate_writes_nothing(db, monkeypatch):
    """Предикат допуска не пишет и не коммитит (в отличие от ``effective_mode``)."""
    monkeypatch.setattr(sh, "act_granted_now", _REAL_ACT_GRANTED_NOW)
    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    assert sh.configured_mode() == "act"
    events_before = len(await db.execute_fetchall("SELECT id FROM events"))
    changes_before = db.total_changes
    statements: list[str] = []
    real_execute = db.execute

    async def spy(sql, *args, **kwargs):
        statements.append(str(sql))
        return await real_execute(sql, *args, **kwargs)

    async def no_commit(*_args, **_kwargs):
        raise AssertionError("предикат допуска не имеет права коммитить")

    async def no_rollback(*_args, **_kwargs):
        raise AssertionError("предикат допуска не имеет права откатывать")

    real_fetch = db.execute_fetchall

    async def spy_fetch(sql, *args, **kwargs):
        statements.append(str(sql))
        return await real_fetch(sql, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(db, "execute", spy)
        scoped.setattr(db, "execute_fetchall", spy_fetch)
        scoped.setattr(db, "commit", no_commit)
        scoped.setattr(db, "rollback", no_rollback)
        granted = await sh.act_granted_now(db)

    assert granted is False, "выборка нового контура пуста - критерии не выполнены"
    assert statements, "предикат читал базу"
    assert all(s.lstrip().upper().startswith(("SELECT", "WITH")) for s in statements)
    assert db.total_changes == changes_before
    assert len(await db.execute_fetchall("SELECT id FROM events")) == events_before
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    assert await sh.act_granted_now(db) is False


async def _poll_with_a_revoked_delegation(db, db_dsn, case, monkeypatch) -> int:
    """Поллер применяет approve; перед ``_record`` вердикт отдан человеку."""
    other = await db_module.connect(db_dsn)
    real_record = steward_applied._record

    async def record_after_the_owner_changes_the_policy(*args, **kwargs):
        await _switch_to_human(other, case, monkeypatch)
        return await real_record(*args, **kwargs)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                steward_applied, "_record", record_after_the_owner_changes_the_policy
            )
            return await apply_advisor_outcomes(db)
    finally:
        await other.close()


async def test_failed_application_reason_is_shown(db, db_dsn, monkeypatch):
    """AC-2: причина failed - одна строка в ленте и в доказательствах, без дублей."""
    case = await build_case(db, monkeypatch, "toctou-reason")

    assert await _poll_with_a_revoked_delegation(db, db_dsn, case, monkeypatch) == 1

    row = dict(await repo.get_steward_judgement(db, case.task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "failed"
    assert row["advisor_outcome_detail"].startswith("policy_not_delegated: ")
    assert await verdict_of(db, case.task_id) == ""

    def failure_lines(text: str) -> list[str]:
        return [
            ln for ln in text.splitlines() if "Одобрение стюарда не применено" in ln
        ]

    lines = failure_lines(await feed(db, case.task_id))
    assert len(lines) == 1, lines
    assert "policy_not_delegated" in lines[0]

    evidence = await verdict_evidence.build(db, case.task_id, 1)
    shown = [
        ln
        for ln in verdict_evidence.format_block(evidence).splitlines()
        if "причина отказа применения" in ln
    ]
    assert len(shown) == 1 and "policy_not_delegated" in shown[0], shown

    # Повторные тики: failed поллер не выбирает - строка не дублируется.
    assert await apply_advisor_outcomes(db) == 0
    assert await apply_advisor_outcomes(db) == 0
    assert len(failure_lines(await feed(db, case.task_id))) == 1


async def test_failure_reason_never_carries_foreign_text(db, db_dsn, monkeypatch):
    """Чужой текст (исключение, значение из базы в ответе ворот) не попадает в базу, ленту, доказательства."""
    marker = "ЧУЖОЙ-ТЕКСТ-МАРКЕР-4711"
    for label, raised in (
        ("plain", RuntimeError(f"upstream said {marker}")),
        ("gate", HTTPException(422, f"ci_not_green: lint={marker}")),
    ):
        case = await build_case(db, monkeypatch, f"toctou-foreign-{label}")

        async def boom(*_args, _raised=raised, **_kwargs):
            raise _raised

        with monkeypatch.context() as scoped:
            scoped.setattr(steward_applied, "_record", boom)
            assert await apply_advisor_outcomes(db) == 1

        row = dict(await repo.get_steward_judgement(db, case.task_id, 1, "verdict"))
        assert row["advisor_outcome"] == "failed", label
        shown = row["advisor_outcome_detail"]
        assert ("RuntimeError" if label == "plain" else "ci_not_green") in shown, label
        evidence = verdict_evidence.format_block(
            await verdict_evidence.build(db, case.task_id, 1)
        )
        for surface in (shown, await feed(db, case.task_id), evidence):
            assert marker not in surface, label


async def test_a_failure_after_the_approve_is_recorded_is_not_called_unapplied(
    db, monkeypatch
):
    """Сбой ПОСЛЕ записи вердикта: в ленте не сказано, что одобрение не применено."""
    case = await build_case(db, monkeypatch, "toctou-after-record")
    real_apply = steward_applied.apply_judgement

    async def apply_then_fail(*args, **kwargs):
        await real_apply(*args, **kwargs)
        raise RuntimeError("служебная заметка не записалась")

    monkeypatch.setattr(steward_applied, "apply_judgement", apply_then_fail)
    assert await apply_advisor_outcomes(db) == 1

    assert await verdict_of(db, case.task_id) == ReviewVerdict.approved.value
    text = await feed(db, case.task_id)
    assert "Одобрение стюарда записано, но следующий шаг применения не выполнен" in text
    assert "не применено, решает человек" not in text


async def test_the_revoked_act_reason_is_shown_to_the_owner(db, db_dsn, monkeypatch):
    """Стюарда выключили перед записью: в карточке названо ЭТО, а не «внутренняя ошибка»."""
    case = await build_case(db, monkeypatch, "toctou-act-reason")
    await _real_global_act(monkeypatch)
    other = await db_module.connect(db_dsn)
    real_record = steward_applied._record

    async def record_after_the_owner_switches_it_off(*args, **kwargs):
        await _switch_off(other, case, monkeypatch)
        return await real_record(*args, **kwargs)

    try:
        monkeypatch.setattr(
            steward_applied, "_record", record_after_the_owner_switches_it_off
        )
        assert await apply_advisor_outcomes(db) == 1
    finally:
        await other.close()

    row = dict(await repo.get_steward_judgement(db, case.task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "failed"
    assert "автономия act больше не выдана" in row["advisor_outcome_detail"]
    assert "автономия act больше не выдана" in await feed(db, case.task_id)


async def test_a_human_approve_that_won_the_race_is_not_called_the_stewards(
    db, db_dsn, monkeypatch
):
    """Человек одобрил между проверкой и записью: алерт не приписывает запись стюарду."""
    case = await build_case(db, monkeypatch, "toctou-human-wins")
    other = await db_module.connect(db_dsn)
    real_record = steward_applied._record

    async def record_after_a_human_approves(*args, **kwargs):
        await record_review_verdict(
            other,
            case.task_id,
            TaskReviewVerdict(agent="Denis", verdict=ReviewVerdict.approved),
        )
        return await real_record(*args, **kwargs)

    try:
        monkeypatch.setattr(steward_applied, "_record", record_after_a_human_approves)
        assert await apply_advisor_outcomes(db) == 1
    finally:
        await other.close()

    row = dict(await repo.get_steward_judgement(db, case.task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "failed"
    assert "verdict_already_stands" in row["advisor_outcome_detail"]
    text = await feed(db, case.task_id)
    assert "Одобрение стюарда не применено, решает человек" in text
    assert "записано, но следующий шаг" not in text


_FOREIGN = "ЧУЖОЙ_ТЕКСТ_4711"


@pytest.mark.parametrize(
    "raised",
    [
        HTTPException(422, {"reason": _FOREIGN}),
        HTTPException(422, f"advisor_not_concurred: {_FOREIGN}"),
        HTTPException(409, f"{_FOREIGN}"),
        CodedRefusal(409, f"текст {_FOREIGN}", code=_FOREIGN),
        CodedRefusal(409, "x", code="verdict_taken"),
    ],
    ids=["dict-reason", "known-prefix", "no-code", "foreign-code", "known-code"],
)
async def test_the_reason_comes_from_a_closed_vocabulary(
    db, monkeypatch, raised: HTTPException
):
    """Закрытый словарь: текста из исключения нет ни в базе, ни в ленте, ни в доказательствах."""
    case = await build_case(db, monkeypatch, "toctou-closed-vocabulary")

    async def boom(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(steward_applied, "_record", boom)
    assert await apply_advisor_outcomes(db) == 1

    row = dict(await repo.get_steward_judgement(db, case.task_id, 1, "verdict"))
    evidence = verdict_evidence.format_block(
        await verdict_evidence.build(db, case.task_id, 1)
    )
    for surface in (
        row["advisor_outcome_detail"],
        await feed(db, case.task_id),
        evidence,
    ):
        assert _FOREIGN not in surface
    assert row["advisor_outcome_detail"].strip()


async def test_a_changed_packet_keeps_the_advisor_code(db, monkeypatch):
    """Пакет фактов изменился перед записью: код advisor_not_concurred не теряется."""
    case = await build_case(db, monkeypatch, "toctou-packet-changed")
    real_apply = steward_applied.apply_judgement

    async def change_the_packet_then_apply(*args, **kwargs):
        await db.execute(
            "UPDATE tasks SET description = description || ' (правка)' WHERE id=?",
            (case.task_id,),
        )
        await db.commit()
        return await real_apply(*args, **kwargs)

    monkeypatch.setattr(
        steward_applied, "apply_judgement", change_the_packet_then_apply
    )
    assert await apply_advisor_outcomes(db) == 1

    row = dict(await repo.get_steward_judgement(db, case.task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "failed"
    assert row["advisor_outcome_detail"].startswith("advisor_not_concurred: "), row
    assert "советник" in (await feed(db, case.task_id)).lower()


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (HTTPException(422, f"ac_tests_not_green: {_FOREIGN}"), "ac_tests_not_green"),
        (
            HTTPException(409, {"reason": "verdict_window_closed"}),
            "verdict_window_closed",
        ),
        (
            HTTPException(422, {"reason": "verdict_repeats_previous"}),
            "verdict_repeats_previous",
        ),
        (
            CodedRefusal(422, "x", code="machine_review_required"),
            "machine_review_required",
        ),
    ],
)
def test_the_useful_refusal_codes_have_fixed_texts(raised, code):
    from hub.services.refusal_reason import REASON_TEXTS, reason_line

    assert reason_line(raised) == f"{code}: {REASON_TEXTS[code]}"


async def test_the_machine_review_gate_names_its_code(db, monkeypatch):
    """Ворота обязательного машинного ревью отдают код, а не только текст."""
    from types import SimpleNamespace

    from hub.services import lifecycle
    from hub.services.refusal_reason import reason_line

    case = await build_case(db, monkeypatch, "toctou-mr-gate", pair=False, order=False)
    monkeypatch.setattr(config, "MACHINE_REVIEW_MODE", "require")
    task = dict(await repo.get_task(db, case.task_id))
    state = SimpleNamespace(
        db=db,
        task=task,
        body=SimpleNamespace(verdict=ReviewVerdict.approved),
    )
    with pytest.raises(HTTPException) as caught:
        await lifecycle._vstep_machine_review_present(state)

    assert reason_line(caught.value).startswith("machine_review_required: ")


def test_a_fixed_text_is_a_general_claim_with_a_pointer_to_the_log():
    """Текст кода - общее утверждение без перечня причин и с отсылкой к журналу (#2040).

    Перечень ветвей устаревает и врёт, как только ворота получают новую;
    точная причина живёт в журнале хаба.
    """
    from hub.services.refusal_reason import REASON_TEXTS, WHERE

    assert len(REASON_TEXTS) == 21
    for code, text in REASON_TEXTS.items():
        assert text.endswith("точная причина в журнале хаба"), code
        claim = text.split(" — ", 1)[0]
        assert " — " in text, code
        for enumeration in (",", " либо ", " или ", "(", ";"):
            assert enumeration not in claim, (code, enumeration)
    assert "журнале" in WHERE


async def test_a_cap_handoff_is_not_called_a_missing_consent(db, db_dsn, monkeypatch):
    """Согласие советника действительно, но сдача передана человеку из-за потолка.

    Код отказа - ``steward_cap_handoff``, а не ``advisor_not_concurred``: у
    второго текст «нет согласия», и здесь он был бы ложью.
    """
    from hub.services.refusal_reason import REASON_TEXTS

    case = await build_case(db, monkeypatch, "toctou-cap-code")
    assert await advisor_refusal(db, case.task_id, 1) is None, "согласие действительно"
    other = await db_module.connect(db_dsn)
    real_record = steward_applied._record

    async def record_after_the_cap_is_spent(*args, **kwargs):
        await _cap_handoff(other, case, monkeypatch)
        return await real_record(*args, **kwargs)

    try:
        monkeypatch.setattr(steward_applied, "_record", record_after_the_cap_is_spent)
        assert await apply_advisor_outcomes(db) == 1
    finally:
        await other.close()

    row = dict(await repo.get_steward_judgement(db, case.task_id, 1, "verdict"))
    assert row["advisor_outcome"] == "failed"
    assert row["advisor_outcome_detail"] == (
        f"steward_cap_handoff: {REASON_TEXTS['steward_cap_handoff']}"
    )
    early = await advisor_refusal(db, case.task_id, 1)
    assert early is not None and early[0] == "steward_cap_handoff", early


async def test_an_empty_report_appearing_before_the_write_refuses_approve(
    db, db_dsn, monkeypatch
):
    """AC-2: между ранней проверкой и записью актуальным стал пустой отчёт - approve не записан.

    Перепроверка в транзакции чистая: провайдера не спрашивает, а доказательство
    работы привязано к ИДЕНТИФИКАТОРУ отчёта, поэтому доказанный прежний отчёт
    нового не покрывает.
    """
    from tests.steward_empty_report_support import add_report, empty_case

    monkeypatch.setattr(config, "PROVEN_EMPTY_MAX_CLASS", "r1")

    async def new_empty_report(other, case, _monkeypatch) -> None:
        await add_report(other, case.task_id, raw_count=0)

    for label, raw_before in (("candidates", 2), ("proven_empty", 0)):
        case, calls = await empty_case(
            db, monkeypatch, f"toctou-empty-{label}", raw_count=raw_before
        )
        asked_before = len(calls)
        with pytest.raises(HTTPException) as caught:
            await _apply_with_a_write_before_record(
                db, db_dsn, case, monkeypatch, new_empty_report
            )
        assert caught.value.status_code == 422, (label, caught.value.detail)
        assert str(caught.value.detail).startswith("no_data_report:"), (
            label,
            caught.value.detail,
        )
        assert await verdict_of(db, case.task_id) == "", label
        assert await verdict_events(db, case.task_id) == [], label
        # Сети в транзакции нет: за всё применение провайдера спросили не более раза.
        assert len(calls) - asked_before <= 1, (label, calls)
