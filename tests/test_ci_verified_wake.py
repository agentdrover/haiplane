"""CI стал verified: автопилот и повтор применения стюарда (#2054), крайние случаи."""

from __future__ import annotations

import aiosqlite
from fastapi import HTTPException

from tests import ci_wake_support as wake
from hub.services import steward_apply
from tests.steward_double_clean_support import build_case, policy_of, verdict_of


async def _breaks(*_args, **_kwargs):
    """Поздняя проверка потолка падает переходящей причиной: повтор состоялся и отказал."""
    raise PermissionError("policy read")


async def _case(db, monkeypatch, slug):
    return await build_case(
        db, monkeypatch, slug, policy=policy_of("r2", None, None), risk="R2"
    )


async def test_a_missing_risk_class_fact_is_retried_when_ci_turns_verified(
    db: aiosqlite.Connection, monkeypatch
):
    """Факт ранней проверки класса не привязан к сдаче: переходящий сбой, как ceiling_check_failed."""
    from hub.services import steward_advisor, steward_apply

    case = await _case(db, monkeypatch, "wake-unbound")

    async def forgets(*_args, **_kwargs):
        return None  # ранняя проверка не оставила факта для этой сдачи

    with monkeypatch.context() as scoped:
        scoped.setattr(steward_apply, "remember_risk_fact", forgets)
        assert await steward_advisor.apply_advisor_outcomes(db) == 1
    row = await wake.judgement_of(db, case.task_id)
    assert row["advisor_outcome"] == "failed"
    assert row["advisor_outcome_detail"].startswith("ceiling_fact_unbound: ")

    await wake.ci_report_arrives(db, monkeypatch, case.task_id)

    assert await verdict_of(db, case.task_id) == "approved"
    assert len(await wake.events_of(db, wake.RETRY_EVENT, case.task_id)) == 1


async def test_an_actual_class_above_the_ceiling_is_not_retried(
    db: aiosqlite.Connection, monkeypatch
):
    """Постоянный отказ (класс выше потолка) остаётся failed и уходит к человеку."""
    case = await _case(db, monkeypatch, "wake-permanent")
    above = HTTPException(
        422, "class_above_policy_ceiling: steward_approve_max_class=r1: выше потолка"
    )
    await wake.fail_the_apply(db, monkeypatch, exc=above)
    before = await wake.judgement_of(db, case.task_id)
    assert before["advisor_outcome"] == "failed"

    await wake.ci_report_arrives(db, monkeypatch, case.task_id)

    after = await wake.judgement_of(db, case.task_id)
    assert after["advisor_outcome"] == "failed"
    assert after["advisor_outcome_detail"] == before["advisor_outcome_detail"]
    assert await wake.events_of(db, wake.RETRY_EVENT, case.task_id) == []
    assert await verdict_of(db, case.task_id) == ""


async def test_the_retry_happens_once_per_generation(
    db: aiosqlite.Connection, monkeypatch
):
    """Повтор тоже отказал переходящей причиной - второго повтора нет, события не множатся."""
    case = await _case(db, monkeypatch, "wake-once")
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")

    with monkeypatch.context() as scoped:
        scoped.setattr(steward_apply, "ceiling_gate", _breaks)
        await wake.ci_report_arrives(db, monkeypatch, case.task_id)
    assert len(await wake.events_of(db, wake.RETRY_EVENT, case.task_id)) == 1
    again = await wake.judgement_of(db, case.task_id)
    assert again["advisor_outcome"] == "failed"

    before = await wake.count_events(db, case.task_id)
    await wake.ci_report_arrives(db, monkeypatch, case.task_id)
    assert await wake.count_events(db, case.task_id) == before
    assert len(await wake.events_of(db, wake.RETRY_EVENT, case.task_id)) == 1
    assert await verdict_of(db, case.task_id) == ""


async def test_a_verdict_already_written_is_not_retried(
    db: aiosqlite.Connection, monkeypatch
):
    """Вердикт этой сдачи уже стоит - повтор не нужен и не пишется."""
    from hub import repository as repo

    case = await _case(db, monkeypatch, "wake-written")
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")
    await repo.update_task(
        db, case.task_id, review_verdict="approved", review_verdict_generation=1
    )
    await db.commit()

    await wake.ci_report_arrives(db, monkeypatch, case.task_id)

    assert await wake.events_of(db, wake.RETRY_EVENT, case.task_id) == []
    assert (await wake.judgement_of(db, case.task_id))["advisor_outcome"] == "failed"


async def test_a_race_is_not_marked_failed(db: aiosqlite.Connection, monkeypatch):
    """B-2: каждая из гонок 409 - не failed, без ухода к человеку и без повторов."""
    for code in ("verdict_taken", "submission_moved", "generation_moved"):
        case = await _case(db, monkeypatch, f"wake-race-{code}")
        await wake.fail_the_apply(
            db, monkeypatch, exc=HTTPException(409, f"{code}: гонка")
        )
        row = await wake.judgement_of(db, case.task_id)
        assert row["advisor_outcome"] == "skipped", (code, row)
        assert await wake.events_of(db, "steward_apply_failed", case.task_id) == []


async def test_the_retry_itself_is_recorded_and_runs_once(
    db: aiosqlite.Connection, monkeypatch
):
    """Повтор пишет событие и не выполняется второй раз, даже если зовут напрямую."""
    from hub.services.steward_advisor import retry_transient_apply

    case = await _case(db, monkeypatch, "wake-direct")
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")

    with monkeypatch.context() as scoped:
        scoped.setattr(steward_apply, "ceiling_gate", _breaks)
        assert await retry_transient_apply(db, case.task_id, 1, wake.SHA) is True
        assert await retry_transient_apply(db, case.task_id, 1, wake.SHA) is False
    assert len(await wake.events_of(db, wake.RETRY_EVENT, case.task_id)) == 1


async def test_a_resubmission_during_the_check_does_not_consume_the_next_retry(
    db: aiosqlite.Connection, monkeypatch
):
    await _resubmitted_during_the_check(db, monkeypatch, "wake-resubmit", 2, wake.SHA)


async def test_a_repin_during_the_check_wakes_nobody(
    db: aiosqlite.Connection, monkeypatch
):
    await _resubmitted_during_the_check(db, monkeypatch, "wake-repin", 1, "b" * 40)


async def _resubmitted_during_the_check(db, monkeypatch, slug, new_gen, new_sha):
    """P2-1: пересдача, пока шла проверка CI, - старое поколение не съедает повтор нового."""
    from hub import repository as repo
    from hub.services import ci_trust

    case = await _case(db, monkeypatch, slug)
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")
    await repo.set_ci_run_report_trust(
        db, case.task_id, wake.SHA, trust="unverified", trust_reason="p", attempt_key=""
    )
    await db.commit()

    async def resubmitted_meanwhile(_db, task_id, head_sha, *_a, **_k):
        # Поколение 2 уже стоит, его failed-суждение ждёт своего CI.
        await db.execute(
            "UPDATE steward_judgements SET generation=? WHERE task_id=?",
            (new_gen, task_id),
        )
        await repo.update_task(
            db, task_id, submission_generation=new_gen, submission_sha=new_sha
        )
        await db.commit()
        return ci_trust.Standing(trust=ci_trust.VERIFIED, reason="")

    monkeypatch.setattr(ci_trust, "_refresh_locked", resubmitted_meanwhile)
    await ci_trust.refresh(db, case.task_id, wake.SHA)

    assert await wake.wake_rows(db, case.task_id) == []
    assert await wake.events_of(db, wake.RETRY_EVENT, case.task_id) == []
    row = dict(await repo.get_steward_judgement(db, case.task_id, new_gen, "verdict"))
    assert row["advisor_outcome"] == "failed"
    from hub.services.steward_advisor import retry_transient_apply

    if new_gen != 1:
        assert await retry_transient_apply(db, case.task_id, 1, wake.SHA) is False
    assert await wake.events_of(db, wake.RETRY_EVENT, case.task_id) == []


async def test_a_refresh_that_turns_trust_verified_wakes_once(
    db: aiosqlite.Connection, monkeypatch
):
    """P2-2: отчёт принят при недоступном GitHub, проверка позже даёт verified - одно пробуждение."""
    from hub import repository as repo
    from hub.services import ci_trust

    case = await _case(db, monkeypatch, "wake-refresh")
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")
    await repo.set_ci_run_report_trust(
        db, case.task_id, wake.SHA, trust="unverified", trust_reason="p", attempt_key=""
    )
    await db.commit()
    await wake.verified_by_github(monkeypatch)

    await ci_trust.refresh(db, case.task_id, wake.SHA)  # путь поллера (sweep)

    assert len(await wake.events_of(db, wake.RETRY_EVENT, case.task_id)) == 1
    assert await verdict_of(db, case.task_id) == "approved"
    before = await wake.count_events(db, case.task_id)
    await ci_trust.refresh(db, case.task_id, wake.SHA)
    assert await wake.count_events(db, case.task_id) == before


async def test_a_wake_skipped_under_a_guard_happens_on_the_next_refresh(
    db: aiosqlite.Connection, monkeypatch
):
    """P2-a: проверка прошла внутри применения - пробуждение не потеряно, а ждёт следующего прохода."""
    from hub import repository as repo
    from hub.services import ci_trust
    from hub.services.steward_apply import ceiling_scope

    case = await _case(db, monkeypatch, "wake-guarded")
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")
    await repo.set_ci_run_report_trust(
        db, case.task_id, wake.SHA, trust="unverified", trust_reason="p", attempt_key=""
    )
    await db.commit()
    await wake.verified_by_github(monkeypatch)

    async with ceiling_scope():  # проверка внутри применения: будить нельзя
        await ci_trust.refresh(db, case.task_id, wake.SHA)
    assert await wake.events_of(db, wake.RETRY_EVENT, case.task_id) == []

    await ci_trust.refresh(db, case.task_id, wake.SHA)  # следующий проход
    assert len(await wake.events_of(db, wake.RETRY_EVENT, case.task_id)) == 1
    assert await verdict_of(db, case.task_id) == "approved"
    before = await wake.count_events(db, case.task_id)
    await ci_trust.refresh(db, case.task_id, wake.SHA)
    assert await wake.count_events(db, case.task_id) == before


async def test_a_repin_during_the_autopilot_call_does_not_consume_the_retry(
    db: aiosqlite.Connection, monkeypatch
):
    """P2-b: ветку перезакрепили, пока шёл вызов автопилота, - повтор нового коммита не тронут."""
    from hub import repository as repo
    from hub.services import auto_verdict, ci_verified_wake

    case = await _case(db, monkeypatch, "wake-repin-autopilot")
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")

    async def has_report(*_a, **_k):
        return True

    async def repins(_db, task_id):
        await repo.update_task(db, task_id, submission_sha="b" * 40)
        await db.commit()
        return False

    monkeypatch.setattr(ci_verified_wake, "_has_current_report", has_report)
    monkeypatch.setattr(auto_verdict, "maybe_auto_verdict", repins)

    await ci_verified_wake.wake_on_verified(db, case.task_id, wake.SHA, 1)

    assert await wake.events_of(db, wake.RETRY_EVENT, case.task_id) == []
    assert (await wake.judgement_of(db, case.task_id))["advisor_outcome"] == "failed"


async def test_dedup_survives_the_event_retention(
    db: aiosqlite.Connection, monkeypatch
):
    """Через две недели журнал пуст: ни второго вызова автопилота, ни второго повтора."""
    from hub import repository as repo
    from hub.services import auto_verdict, ci_trust, ci_verified_wake

    case = await _case(db, monkeypatch, "wake-retention")
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")
    asked: list[int] = []

    async def has_report(*_a, **_k):
        return True

    async def spy(_db, task_id):
        asked.append(task_id)
        return False

    monkeypatch.setattr(ci_verified_wake, "_has_current_report", has_report)
    monkeypatch.setattr(auto_verdict, "maybe_auto_verdict", spy)

    monkeypatch.setattr(steward_apply, "ceiling_gate", _breaks)
    await repo.set_ci_run_report_trust(
        db, case.task_id, wake.SHA, trust="unverified", trust_reason="p", attempt_key=""
    )
    await db.commit()
    await wake.verified_by_github(monkeypatch)
    await ci_trust.refresh(db, case.task_id, wake.SHA)
    assert asked == [case.task_id]
    assert len(await wake.events_of(db, wake.RETRY_EVENT, case.task_id)) == 1
    assert (await wake.judgement_of(db, case.task_id))["advisor_outcome"] == "failed"

    await wake.prune_all_events(db)  # prune_events через 14 дней
    await ci_trust.refresh(db, case.task_id, wake.SHA)
    await ci_trust.refresh(db, case.task_id, wake.SHA)

    assert asked == [case.task_id], "автопилота второй раз не зовут"
    assert await wake.events_of(db, wake.RETRY_EVENT, case.task_id) == []
    row = await wake.judgement_of(db, case.task_id)
    assert row["transient_retry_at"], "повтор был - метка на самом суждении"


async def test_an_already_deduped_refresh_asks_nothing_global(
    db: aiosqlite.Connection, monkeypatch
):
    """P2-2: разбор с уже занятым дедупом - без effective_mode, записей и событий."""
    from hub.services import ci_trust, ci_verified_wake, steward_shadow

    case = await _case(db, monkeypatch, "wake-cheap")
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")
    await db.execute(  # повтор уже был, пробуждение автопилота уже занято
        "UPDATE steward_judgements SET transient_retry_at=datetime('now') "
        "WHERE task_id=?",
        (case.task_id,),
    )
    await db.execute(
        "INSERT INTO ci_verified_wakes (task_id, generation, head_sha) VALUES (?, 1, ?)",
        (case.task_id, wake.SHA),
    )
    await db.commit()

    async def has_report(*_a, **_k):
        return True

    modes: list[int] = []

    async def mode_asked(*_a, **_k):
        modes.append(1)
        return "act"

    monkeypatch.setattr(ci_verified_wake, "_has_current_report", has_report)
    monkeypatch.setattr(steward_shadow, "effective_mode", mode_asked)
    await wake.verified_by_github(monkeypatch)
    statements: list[str] = []
    await db.set_trace_callback(statements.append)
    try:
        await ci_trust.refresh(db, case.task_id, wake.SHA)
    finally:
        await db.set_trace_callback(None)

    assert modes == [], "effective_mode спрошен до проверки допустимости"
    # После записи самой проверки (первый COMMIT) - только чтения.
    mine = statements[statements.index("COMMIT") + 1 :]
    assert not [s for s in mine if s.startswith(("INSERT", "UPDATE", "BEGIN"))], mine
    assert not [s for s in mine if "events" in s or "steward_runs" in s], mine


async def test_the_wake_claim_is_taken_once(db: aiosqlite.Connection, monkeypatch):
    """Занять строку пробуждения можно один раз; второй заход видит rowcount 0."""
    from hub.services import ci_verified_wake

    case = await _case(db, monkeypatch, "wake-claim")
    assert await ci_verified_wake._claim_wake(db, case.task_id, 1, wake.SHA) is True
    assert await ci_verified_wake._claim_wake(db, case.task_id, 1, wake.SHA) is False
    assert await wake.wake_rows(db, case.task_id) == [(1, wake.SHA)]


async def test_a_deleted_task_takes_its_wake_rows_with_it(db: aiosqlite.Connection):
    """Строка пробуждения не переживает задачу и не мешает её удалить (Codex P3:
    сирота навсегда; ссылка без каскада превратила бы удаление в ошибку FK)."""
    from hub import repository as repo
    from hub.services import ci_verified_wake

    task_id = await repo.create_task(
        db,
        title="wake-cascade",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status="review",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.execute(
        "INSERT INTO ci_verified_wakes (task_id, generation, head_sha) VALUES (?, 1, ?)",
        (task_id, wake.SHA),
    )
    await db.commit()
    assert await ci_verified_wake._claim_wake(db, task_id, 1, wake.SHA) is False
    assert await repo.delete_task_subtree(db, task_id) == 1
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT 1 FROM ci_verified_wakes WHERE task_id = ?", (task_id,)
    )
    assert rows == []


_OURS = ("create_ci_verified_wakes", "add_steward_judgements_transient_retry_at")


async def _wake_schema(conn) -> tuple[list[str], list[str]]:
    key = [
        r[1]
        for r in sorted(
            await conn.execute_fetchall("PRAGMA table_info(ci_verified_wakes)"),
            key=lambda r: r[5],
        )
        if r[5]
    ]
    cols = [
        r[1]
        for r in await conn.execute_fetchall("PRAGMA table_info(steward_judgements)")
    ]
    return key, cols


async def test_the_dedup_schema_exists_on_a_fresh_and_an_upgraded_database(
    db: aiosqlite.Connection, monkeypatch
):
    """Миграция #2054: свежая база и обновление базы, где строки суждений уже есть."""
    import hub.db as db_module
    from hub.db import _SCHEMA, _migrate

    key, cols = await _wake_schema(db)
    assert key == ["task_id", "generation", "head_sha"]
    assert "transient_retry_at" in cols

    old = [(n, q) for n, q in db_module._MIGRATIONS if n not in _OURS]
    async with aiosqlite.connect(":memory:") as conn:
        monkeypatch.setattr(db_module, "_MIGRATIONS", old)
        await conn.executescript(_SCHEMA)
        await _migrate(conn)
        assert (await _wake_schema(conn))[0] == []  # таблицы ещё нет
        monkeypatch.undo()
        await _migrate(conn)
        key, cols = await _wake_schema(conn)
        assert key == ["task_id", "generation", "head_sha"]
        assert "transient_retry_at" in cols
        await conn.execute(
            "INSERT INTO ci_verified_wakes (task_id, generation, head_sha) VALUES (1, 1, 'a')"
        )
        dup = await conn.execute(
            "INSERT OR IGNORE INTO ci_verified_wakes (task_id, generation, head_sha) "
            "VALUES (1, 1, 'a')"
        )
        assert dup.rowcount == 0


async def test_a_verified_red_report_spends_neither_the_ask_nor_the_retry(
    db: aiosqlite.Connection, monkeypatch
):
    """F1: GitHub подтвердил КРАСНЫЙ отчёт - это не зелёный пропуск; зелёная пересдача на том же sha будит."""
    from hub import repository as repo
    from hub.services import auto_verdict, ci_trust, ci_verified_wake

    case = await _case(db, monkeypatch, "wake-red")
    await wake.fail_the_apply(db, monkeypatch, gap="нет отчёта CI")
    asked: list[int] = []

    async def has_report(*_a, **_k):
        return True

    async def spy(_db, task_id):
        asked.append(task_id)
        return False

    monkeypatch.setattr(ci_verified_wake, "_has_current_report", has_report)
    monkeypatch.setattr(auto_verdict, "maybe_auto_verdict", spy)
    await wake.verified_by_github(monkeypatch)

    async def mark(status: str) -> None:
        await db.execute(
            "UPDATE ci_run_reports SET validation_status=? WHERE task_id=?",
            (status, case.task_id),
        )
        await repo.set_ci_run_report_trust(
            db,
            case.task_id,
            wake.SHA,
            trust="unverified",
            trust_reason="p",
            attempt_key="",
        )
        await db.commit()

    await mark("fail")  # verified, но validation_status=fail: pass_gap не пуст
    await ci_trust.refresh(db, case.task_id, wake.SHA)
    assert asked == []
    assert await wake.wake_rows(db, case.task_id) == []
    assert (await wake.judgement_of(db, case.task_id))["transient_retry_at"] is None
    assert await wake.events_of(db, wake.RETRY_EVENT, case.task_id) == []

    await mark("pass")  # зелёная пересдача того же sha
    await ci_trust.refresh(db, case.task_id, wake.SHA)
    await ci_trust.refresh(db, case.task_id, wake.SHA)
    assert asked == [case.task_id]
    assert len(await wake.events_of(db, wake.RETRY_EVENT, case.task_id)) == 1
    assert await verdict_of(db, case.task_id) == "approved"


async def test_a_race_that_says_someone_else_moved_first_is_not_a_failure(
    db: aiosqlite.Connection, monkeypatch
):
    """F2: verdict_already_stands (и прочие 409 «уже сдвинули») - skipped, без alert и повтора."""
    for code in ("verdict_already_stands", "verdict_window_closed"):
        case = await _case(db, monkeypatch, f"wake-{code}")
        await wake.fail_the_apply(
            db, monkeypatch, exc=HTTPException(409, f"{code}: уже сдвинули")
        )
        row = await wake.judgement_of(db, case.task_id)
        assert row["advisor_outcome"] == "skipped", (code, row)
        assert await wake.events_of(db, "steward_apply_failed", case.task_id) == []
        feed = [dict(u)["content"] for u in await repo_updates(db, case.task_id)]
        assert not [c for c in feed if "не применено" in c], feed
        await wake.ci_report_arrives(db, monkeypatch, case.task_id)
        assert await wake.events_of(db, wake.RETRY_EVENT, case.task_id) == []


async def repo_updates(db, task_id):
    from hub import repository as repo

    return await repo.get_task_updates(db, task_id)
