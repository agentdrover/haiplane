"""checks_done и текст прогона: честное «чисто» машинного ревью (#2336).

Тесты критериев приёмки названы в постановке и лежат в ``test_machine_review.py``,
``test_auto_verdict.py`` и ``test_review_dispatch.py``. Здесь — то, что держит их
границы: форма списка, лестница исходов, пороги автовердикта и стюарда, момент
сохранения текста, показ на карточке, в MCP и в брифе, схема.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from httpx import AsyncClient

import hub.db as db_module
from hub import config
from hub import repository as repo
from hub.db import _migrate
from hub.integrations import cursor_cloud
from hub.mcp_server import hub_submit_machine_review
from hub.migrations import FROZEN_MIGRATIONS, assemble
from hub.migrations.steps import load_step_files
from hub.models import ReviewVerdict
from hub.review_checks import (
    CHECK_RESULT_LIMIT,
    CHECK_WHAT_LIMIT,
    CHECKS_MAX,
    RUN_TEXT_CUT_MARK,
    RUN_TEXT_LIMIT,
    cap_run_text,
    checks_receipt,
    clean_checks,
)
from hub.services import review_brief_projection as proj
from hub.services.review_dispatch import sweep_review_dispatches
from hub.services import review_dispatch
from hub.services.auto_verdict import checked_clean_report
from hub.services.digest import _report_outcome_of
from hub.services.steward_advisor import apply_advisor_outcomes
from hub.services.steward_corridor import (
    CLEAN_OUTCOMES,
    OUTCOME_CHECKED_CLEAN,
    OUTCOME_CLEAN,
    OUTCOME_CONFIRMED,
    OUTCOME_INCOMPLETE,
    OUTCOME_NO_DATA,
    OUTCOME_UNRESOLVED,
    REPORT_OUTCOMES,
    names_checked,
    names_clean,
    outcome_label,
    report_outcome,
)
from tests.auto_verdict_support import (
    _post_review,
    _settled_dispatch,
    _submitted_task,
)
from tests.review_checks_support import CHECKS, report
from tests.review_dispatch_support import (
    _any_dispatch_row,
    _DispatchRecorder,
    _node,
    _submitted,
    _wire,
)
from tests.steward_double_clean_support import build_case, judge_and_advise, policy_of
from tests.steward_empty_report_support import PROVEN, add_report, settle_dispatch
from tests.test_db_migrations import _make_db
from tests.test_machine_review import _no_candidates_alerts, _reviewable_task
from tests.test_review_brief_projection import _brief, _machine
from tests.web_support import _web_task_in_review_with_test_ac

ONE = [{"what": "тесты", "how": "uv run pytest -q", "result": "rc 0"}]


# --- форма списка ---------------------------------------------------------------


def test_only_well_named_entries_count_and_long_words_are_cut():
    """Запись без одного из трёх слов не проверка; длинное слово режется, а не отказывает."""
    raw = [
        {"what": "a", "how": "b", "result": "c"},
        {"what": "a", "how": "b"},
        {"what": "a", "how": "   ", "result": "c"},
        {"what": "a", "how": "b", "result": 5},
        "строка",
        None,
        {"what": "w" * 999, "how": "h", "result": "r" * 999},
    ]
    kept = clean_checks(raw)
    assert [c["what"][:1] for c in kept] == ["a", "w"]
    assert len(kept[1]["what"]) == CHECK_WHAT_LIMIT and kept[1]["what"].endswith("…")
    assert len(kept[1]["result"]) == CHECK_RESULT_LIMIT
    assert clean_checks("не json") == [] and clean_checks({"what": "x"}) == []
    assert clean_checks(json.dumps(ONE)) == ONE, "строка БД читается как список"
    many = [{"what": str(i), "how": "h", "result": "r"} for i in range(CHECKS_MAX + 5)]
    assert len(clean_checks(many)) == CHECKS_MAX


def test_a_dropped_entry_is_named_in_the_receipt():
    bad = {"what": "без итога"}
    assert checks_receipt([], 0) == ""
    assert "принят: 2" in checks_receipt(ONE * 2, 2)
    text = checks_receipt(ONE + [bad, bad], 1)
    assert "принято 1 из 3" in text and "2 без одного из слов" in text
    assert "сверх потолка" not in text


def test_the_receipt_names_the_real_reason_when_the_ceiling_cut_the_tail():
    """41 полная запись: хвост отрезан потолком, а слов хватало (находка 16b483a9)."""
    full = [{"what": f"w{i}", "how": "h", "result": "r"} for i in range(CHECKS_MAX + 1)]
    kept = len(clean_checks(full))
    text = checks_receipt(full, kept)
    assert f"принято {CHECKS_MAX} из {CHECKS_MAX + 1}" in text
    assert f"1 сверх потолка {CHECKS_MAX}" in text
    assert "без одного из слов" not in text, "слов хватало: причина названа неверно"
    # Смешанные причины: одна запись без слова и одна сверх потолка.
    mixed = full + [{"what": "x"}]
    text = checks_receipt(mixed, len(clean_checks(mixed)))
    assert f"принято {CHECKS_MAX} из {CHECKS_MAX + 2}" in text
    assert "1 без одного из слов" in text and f"1 сверх потолка {CHECKS_MAX}" in text


def test_the_run_text_cap_cuts_and_says_so():
    short = "ответ прогона"
    assert cap_run_text(f"  {short}\n") == short
    cut = cap_run_text("я" * (RUN_TEXT_LIMIT * 2))
    assert len(cut) == RUN_TEXT_LIMIT and cut.endswith(RUN_TEXT_CUT_MARK)


# --- лестница исходов -----------------------------------------------------------


def test_the_ladder_puts_a_list_of_checks_between_no_data_and_clean():
    assert report_outcome(confirmed=[], unresolved=[], checks_done=ONE) == (
        OUTCOME_CHECKED_CLEAN
    )
    assert report_outcome(confirmed=[], unresolved=[]) == OUTCOME_NO_DATA
    assert report_outcome(confirmed=[], unresolved=[], checks_done=[]) == (
        OUTCOME_NO_DATA
    )
    loud = {"confirmed": [{"title": "c"}], "unresolved": []}
    assert report_outcome(**loud, checks_done=ONE) == OUTCOME_CONFIRMED
    assert (
        report_outcome(
            confirmed=[], unresolved=[{"title": "u"}], checks_done=ONE, raw_count=0
        )
        == OUTCOME_UNRESOLVED
    )
    assert (
        report_outcome(confirmed=[], unresolved=[], incomplete=True, checks_done=ONE)
        == OUTCOME_INCOMPLETE
    )
    assert report_outcome(confirmed=[], unresolved=[], raw_count=3) == OUTCOME_CLEAN
    # Слово ревьюера не чистота: поверхности её не красят цветом «находок нет».
    assert OUTCOME_CHECKED_CLEAN in REPORT_OUTCOMES
    assert not names_clean(OUTCOME_CHECKED_CLEAN)
    assert names_checked(OUTCOME_CHECKED_CLEAN) and not names_checked(OUTCOME_NO_DATA)
    assert OUTCOME_CHECKED_CLEAN not in CLEAN_OUTCOMES
    assert outcome_label(OUTCOME_CHECKED_CLEAN) != outcome_label(OUTCOME_CLEAN)
    assert "не перепроверял" in outcome_label(OUTCOME_CHECKED_CLEAN)


async def test_a_checked_report_does_not_hide_a_later_empty_one(
    client: AsyncClient, db: aiosqlite.Connection
):
    """Алерт «нет данных» пишется по ПЕРВОЙ непроверенной нулевой строке поколения."""
    base = {"raw_count": 0, "incomplete": False, "harness_skill": "lite-diff-review"}
    checked = {"checks_done": CHECKS}
    # Проверенный, пустой, пустой - и пустой, проверенный, проверенный: алерт
    # один, и проверенный отчёт после пустого второго алерта не вызывает.
    for order in (
        ((checked, 0), ({}, 1), ({}, 1)),
        (({}, 1), (checked, 1), (checked, 1)),
    ):
        task_id = await _reviewable_task(client, db)
        for body, alerts in order:
            resp = await client.post(
                f"/api/tasks/{task_id}/machine-review", json={**base, **body}
            )
            assert resp.status_code == 200, resp.text
            assert len(await _no_candidates_alerts(db, task_id)) == alerts, order


async def test_the_digest_names_a_checked_report_apart(db: aiosqlite.Connection):
    ids = {}
    for label, checks in (("checked", ONE), ("empty", [])):
        ids[label] = await repo.insert_machine_review(
            db,
            task_id=1,
            submission_generation=1,
            incomplete=False,
            checks_done=json.dumps(checks, ensure_ascii=False),
        )
    for label, outcome, checked in (
        ("checked", OUTCOME_CHECKED_CLEAN, True),
        ("empty", OUTCOME_NO_DATA, False),
    ):
        view = _report_outcome_of(await repo.get_machine_review(db, ids[label]))
        assert view["outcome"] == outcome, label
        assert view["names_clean"] is False and view["names_checked"] is checked


# --- автовердикт ----------------------------------------------------------------


async def _verdict_of(client, db, monkeypatch, slug, *, usage, **overrides):
    task_id = await _submitted_task(
        client, db, slug, {"verdict": "auto"}, areas=["tests/test_x.py"]
    )
    if usage is not None:
        await _settled_dispatch(db, task_id, usage_total=usage, monkeypatch=monkeypatch)
    await _post_review(client, task_id, raw_count=0, findings_rejected=[], **overrides)
    return (await client.get(f"/api/tasks/{task_id}")).json()["review_verdict"]


def test_only_a_complete_clean_row_with_checks_is_a_checked_report():
    """Строка отчёта: ступень та же, что на карточке; чужие признаки её не создают."""
    row = {"raw_count": 0, "incomplete": 0, "checks_done": json.dumps(ONE)}
    assert checked_clean_report(row) is True
    for change in (
        {"incomplete": 1},
        {"checks_done": "[]"},
        {"checks_done": json.dumps([{"what": "x"}])},
        {"raw_count": 2},
        {"findings_confirmed": json.dumps([{"title": "c"}])},
        {"unresolved": json.dumps([{"title": "u"}])},
    ):
        assert checked_clean_report({**row, **change}) is False, change


async def test_a_kept_run_text_is_never_replaced_by_the_store(
    db: aiosqlite.Connection,
):
    review = await repo.insert_machine_review(db, task_id=1, submission_generation=1)
    await repo.set_machine_review_run_text(db, review, "первый")
    await repo.set_machine_review_run_text(db, review, "второй")
    assert dict(await repo.get_machine_review(db, review))["run_text"] == "первый"


async def test_the_proof_of_a_checked_report_is_the_bill_and_nothing_else(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    monkeypatch.setattr(config, "PROVEN_EMPTY_MAX_CLASS", "r1")
    monkeypatch.setattr(config, "EMPTY_REVIEW_MIN_USAGE", 200_000)

    async def verdict(slug, usage, **overrides):
        return await _verdict_of(
            client, db, monkeypatch, slug, usage=usage, **overrides
        )

    # Заглушка tokens_spent (как в живых отчётах) счёту не мешает.
    assert await verdict("c-stub", 250_000, checks_done=ONE, tokens_spent=100_000) == (
        "approved"
    )
    # Ровно порог проходит, на единицу ниже — нет.
    assert await verdict("c-floor", 200_000, checks_done=ONE) == "approved"
    assert await verdict("c-under", 199_999, checks_done=ONE) is None
    # Без диспетча хаба нет и счёта: слово ревьюера само не доказывает ничего.
    assert await verdict("c-nobill", None, checks_done=ONE) is None
    # Запись без одного из трёх слов — не список: прежнее правило, прежний отказ.
    assert await verdict("c-bad", 250_000, checks_done=[{"what": "x"}]) is None
    # Без списка правило прежнее: расхождение токенов не доказывает пустоту,
    # а совпадающее число доказывает (#769).
    assert await verdict("p-none", 250_000) is None
    assert await verdict("p-stub", 250_000, tokens_spent=100_000) is None
    assert await verdict("p-agree", 250_000, tokens_spent=240_000) == "approved"
    # Неполный отчёт со списком автовердикта не получает никогда.
    assert (
        await verdict("c-incomplete", 250_000, checks_done=ONE, incomplete=True) is None
    )


async def test_the_steward_reads_the_same_proof_for_a_checked_report(
    db: aiosqlite.Connection, monkeypatch
):
    """Стюард и автопилот делят доказательство: список проверок вместо сверки токенов."""
    monkeypatch.setattr(config, "PROVEN_EMPTY_MAX_CLASS", "r1")
    monkeypatch.setattr(config, "EMPTY_REVIEW_MIN_USAGE", 200_000)
    outcomes = {}
    for label, checks in (("with-checks", CHECKS), ("without", None)):
        case = await build_case(
            db,
            monkeypatch,
            f"steward-{label}",
            risk="R1",
            policy=policy_of(ordinary=None, clean=None),
            pair=False,
            order=False,
        )
        review = await add_report(db, case.task_id, tokens_spent=None)
        if checks:
            await db.execute(
                "UPDATE machine_reviews SET checks_done=? WHERE id=?",
                (json.dumps(checks, ensure_ascii=False), review),
            )
        await settle_dispatch(
            db, case.task_id, monkeypatch, usage=PROVEN, report_id=review
        )
        await judge_and_advise(db, case.task_id)
        assert await apply_advisor_outcomes(db) == 1, label
        outcomes[label] = dict(await repo.get_task(db, case.task_id))
    assert outcomes["with-checks"]["review_verdict"] == ReviewVerdict.approved.value
    assert not outcomes["without"].get("review_verdict")
    assert outcomes["without"]["verdict_owner_reason"].startswith("no_data_report")


# --- текст прогона --------------------------------------------------------------


async def _report_then_run(
    client, db, monkeypatch, slug, *, status, text, **report_extra
):
    recorder = _DispatchRecorder({"agent": {"id": "bc-w"}, "run": {"id": "r-w"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, slug)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review", json=report(**report_extra)
    )
    assert resp.status_code == 200, resp.text
    state = {"status": status, "text": text}

    async def _run(agent_id, run_id):
        return {"id": run_id, "status": state["status"], "result": state["text"]}

    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 900_000}}

    async def _runs(agent_id):
        return [{"id": "r-w", "status": state["status"]}]

    monkeypatch.setattr(cursor_cloud, "get_run", _run)
    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)
    monkeypatch.setattr(cursor_cloud, "list_runs", _runs)
    return task_id, state


async def _kept(db, task_id) -> str:
    return dict(await repo.get_latest_machine_review(db, task_id))["run_text"]


async def test_the_run_text_waits_for_a_finished_run_and_is_written_once(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    task_id, state = await _report_then_run(
        client, db, monkeypatch, "run-late", status="RUNNING", text="черновик"
    )
    await sweep_review_dispatches(db)
    assert (await _any_dispatch_row(db, task_id))["status"] == "done"
    assert await _kept(db, task_id) == "", "незаконченный прогон текста не отдаёт"

    # Прогон кончился позже отчёта: текст подбирает пересъём счёта (#1413).
    state.update(status="FINISHED", text="итог: проверено тесты и места вызова")
    await db.execute("UPDATE review_dispatches SET usage_checked_at='2000-01-01'")
    await db.commit()
    await sweep_review_dispatches(db)
    assert await _kept(db, task_id) == "итог: проверено тесты и места вызова"

    # Записанное не перезаписывается более поздним ответом.
    state["text"] = "другой текст"
    await db.execute(
        "UPDATE review_dispatches SET usage_checked_at='2000-01-01', "
        "usage_scope='agent_open'"
    )
    await db.commit()
    await sweep_review_dispatches(db)
    assert await _kept(db, task_id) == "итог: проверено тесты и места вызова"


async def test_a_secret_on_the_cut_is_scrubbed_before_the_cap(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Сначала scrub по всему тексту, потом потолок: секрет на границе не остаётся началом."""
    at = RUN_TEXT_LIMIT - len(RUN_TEXT_CUT_MARK) - 3
    text = "x" * at + "test-key" + "я" * 100
    task_id, _ = await _report_then_run(
        client, db, monkeypatch, "run-cut", status="FINISHED", text=text
    )
    await sweep_review_dispatches(db)
    kept = await _kept(db, task_id)
    assert kept.endswith(RUN_TEXT_CUT_MARK) and "tes" + RUN_TEXT_CUT_MARK not in kept
    assert "test-key" not in kept


async def test_a_failure_before_the_dispatch_closes_leaves_nothing_committed(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Текст, счёт, алерт #757 и закрытие заказа - одна транзакция свипа (находка).

    Сбой между сохранением текста и закрытием заказа, затем откат конца тика
    (``_poll_once``): ни алерта, ни счёта на отчёте, заказ active; повторный
    проход пишет ровно один алерт и закрывает заказ.
    """
    task_id, _ = await _report_then_run(
        client,
        db,
        monkeypatch,
        "run-atomic",
        status="FINISHED",
        text="итог прогона",
        tokens_spent=120_000,
    )
    real = repo.set_review_dispatch_status
    failing = {"on": True}

    async def _flaky(conn, dispatch_id, status):
        if failing["on"] and status == "done":
            raise RuntimeError("сбой между текстом и закрытием")
        return await real(conn, dispatch_id, status)

    monkeypatch.setattr(repo, "set_review_dispatch_status", _flaky)
    with pytest.raises(RuntimeError):
        await sweep_review_dispatches(db)
    await db.rollback()  # конец тика поллера: открытая транзакция откатывается

    review = dict(await repo.get_latest_machine_review(db, task_id))
    assert await _usage_alerts_of(db, task_id) == []
    assert review["provider_tokens"] is None and review["run_text"] == ""
    assert (await _any_dispatch_row(db, task_id))["status"] == "active"

    failing["on"] = False
    await sweep_review_dispatches(db)
    assert len(await _usage_alerts_of(db, task_id)) == 1, "второго алерта нет"
    assert (await _any_dispatch_row(db, task_id))["status"] == "done"
    assert await _kept(db, task_id) == "итог прогона"


async def _usage_alerts_of(db, task_id: int) -> list[str]:
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    return [
        u["content"]
        for u in updates
        if "расходится с данными провайдера" in u["content"]
    ]


async def test_an_empty_or_failed_run_text_is_not_a_kept_text(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    task_id, state = await _report_then_run(
        client, db, monkeypatch, "run-empty", status="FINISHED", text="   "
    )
    await sweep_review_dispatches(db)
    assert await _kept(db, task_id) == "" and (
        (await _any_dispatch_row(db, task_id))["status"] == "done"
    )

    async def _boom(agent_id, run_id):
        raise RuntimeError("provider down")

    other, _ = await _report_then_run(
        client, db, monkeypatch, "run-boom", status="FINISHED", text="x"
    )
    monkeypatch.setattr(cursor_cloud, "get_run", _boom)
    await sweep_review_dispatches(db)
    assert await _kept(db, other) == "", "сбой текста не мешает закрыть заказ"
    assert (await _any_dispatch_row(db, other))["status"] == "done"


# --- показ ----------------------------------------------------------------------


async def test_the_card_shows_the_checks_and_the_run_text_as_the_reviewers_word(
    client: AsyncClient, db: aiosqlite.Connection
):
    task_id = await _web_task_in_review_with_test_ac(client, db)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={**report(), "agent": "reviewer-bot"},
    )
    assert resp.status_code == 200, resp.text
    await db.execute(
        "UPDATE machine_reviews SET run_text=? WHERE task_id=?",
        ("прогнал <script>alert(1)</script> и прочитал дифф", task_id),
    )
    await db.commit()

    page = (await client.get(f"/tasks/{task_id}")).text
    strip = page[page.index("task-review-strip") : page.index("task-evidence")]
    tile_at = strip.index("Машинное ревью")
    tile = strip[strip.rfind('<div class="task-review-tile ', 0, tile_at) : tile_at]
    assert "task-review-tile--warn" in tile, "слово ревьюера: не красная и не зелёная"
    assert outcome_label(OUTCOME_CHECKED_CLEAN) in strip
    note = page[page.index("Machine review:") :]
    assert "badge-self-approved" in note[: note.index("</span>")]
    assert "проверки ревьюера (2) — слово ревьюера, хаб их не проверял" in note
    assert (
        "изменённые тесты проходят — uv run pytest -q tests/test_x.py → 12 passed"
        in note
    )
    assert "слово ревьюера о себе" in note
    assert "<script>alert(1)</script>" not in page, "текст прогона экранируется"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in note


async def test_mcp_receipt_forwards_the_checks_and_names_dropped_entries():
    stored = {
        "raw_count": 0,
        "findings_confirmed": [],
        "findings_rejected": [],
        "checks_done": ONE,
        "outcome_label": outcome_label(OUTCOME_CHECKED_CLEAN),
        "submission_generation": 1,
    }
    with patch("hub.mcp_server._api_post", new_callable=AsyncMock) as post:
        post.return_value = stored
        sent = ONE + [{"what": "без итога"}]
        receipt = await hub_submit_machine_review(
            7, raw_count=0, incomplete=False, checks_done=sent
        )
    assert post.await_args.args[1]["checks_done"] == sent
    text = receipt.content[0].text
    assert outcome_label(OUTCOME_CHECKED_CLEAN) in text
    assert "принято 1 из 2" in text and "1 без одного из слов" in text
    with patch("hub.mcp_server._api_post", new_callable=AsyncMock) as post:
        post.return_value = {**stored, "checks_done": []}
        silent = await hub_submit_machine_review(7, raw_count=0, incomplete=False)
    assert post.await_args.args[1]["checks_done"] == []
    assert "Список проверок" not in silent.content[0].text


def test_the_brief_projection_shows_five_checks_and_pages_the_rest():
    checks = [{"what": f"w{i}", "how": f"h{i}", "result": f"r{i}"} for i in range(8)]
    machine = _machine(
        0,
        0,
        incomplete=False,
        checks_done=checks,
        run_text="прогнал и прочитал " + "я" * 900,
        outcome="checked_clean",
    )
    brief = _brief(machine_review=machine)
    answer = proj.brief_result(brief)
    text = json.loads(answer.content[0].text)["message"]
    part = answer.structuredContent["brief"]["machine_review"]
    assert part["checks_done_count"] == 8
    assert part["checks_done"]["shown"] == 5 and part["checks_done"]["omitted"] == 3
    assert "how" not in part["checks_done"]["items"][0], "как проверял — в разделе"
    assert "Checks done: 8 (the reviewer's own word, not verified by the hub)" in text
    assert "- w0 -> r0" in text and "w5" not in text
    assert "… and 3 more (section=machine_review)" in text
    assert "слово ревьюера о себе" in text and part["run_text_chars"] > 900
    assert len(part["run_text_excerpt"]) <= proj.RUN_EXCERPT_CAP
    section = proj.brief_result(brief, "machine_review")
    assert "h7" in json.dumps(section.structuredContent, ensure_ascii=False)
    # Без списка и текста ответ не растёт ни на знак.
    bare = proj.brief_result(_brief(machine_review=_machine(0, 0, incomplete=False)))
    bare_part = bare.structuredContent["brief"]["machine_review"]
    assert "checks_done" not in bare_part and "run_text_chars" not in bare_part


# --- схема ----------------------------------------------------------------------


async def test_the_columns_arrive_by_the_step_and_old_rows_read_as_not_attached(
    monkeypatch,
):
    steps = [s for s in load_step_files() if "_2336_" in s.name]
    assert len(steps) == 1 and steps[0].name.startswith("m")

    async def columns(conn) -> dict[str, tuple]:
        rows = await conn.execute_fetchall("PRAGMA table_info(machine_reviews)")
        return {r[1]: (r[2], r[3], r[4]) for r in rows}

    monkeypatch.setattr(db_module, "_MIGRATIONS", list(FROZEN_MIGRATIONS))
    old = await _make_db()
    try:
        assert "checks_done" not in await columns(old)
        await old.execute(
            "INSERT INTO machine_reviews (task_id, submission_generation) VALUES (1, 1)"
        )
        await old.commit()
        monkeypatch.setattr(db_module, "_MIGRATIONS", assemble(FROZEN_MIGRATIONS))
        await _migrate(old)
        cols = await columns(old)
        assert cols["checks_done"] == ("TEXT", 1, "'[]'")
        assert cols["run_text"] == ("TEXT", 1, "''")
        row = (
            await old.execute_fetchall(
                "SELECT checks_done, run_text FROM machine_reviews"
            )
        )[0]
        assert tuple(row) == ("[]", ""), "старая строка: не приложено"
        await _migrate(old)  # повтор - no-op
        assert (await columns(old)) == cols
    finally:
        await old.close()


async def test_a_fresh_database_has_the_columns(db: aiosqlite.Connection):
    rows = await db.execute_fetchall("PRAGMA table_info(machine_reviews)")
    names = {r[1] for r in rows}
    assert {"checks_done", "run_text"} <= names


async def test_a_carried_report_keeps_its_checks(db: aiosqlite.Connection, monkeypatch):
    """Перенос отчёта на пересдачу копирует список: копия не теряет то, что показывала."""
    task_id = await _node(db, title="carry", task_type="task", parent_id=None)
    source = {
        "id": 11,
        "submission_generation": 1,
        "raw_count": 0,
        "checks_done": json.dumps(ONE, ensure_ascii=False),
    }

    async def _found(_db, _task):
        return source, "правка та же"

    monkeypatch.setattr(review_dispatch, "_carry_source", _found)
    task = {"id": task_id, "submission_generation": 2}
    assert await review_dispatch._carry_the_report_over(db, task) is True
    carried = dict(await repo.get_latest_machine_review(db, task_id))
    assert carried["carried_from_review_id"] == 11
    assert json.loads(carried["checks_done"]) == ONE
