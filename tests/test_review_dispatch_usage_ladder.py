"""Hub-dispatched cross-model reviews (#757): usage_ladder (split of the former single file, #1670)."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.services.model_family import family
from hub.services.review_dispatch import (
    DEEP,
    ENVIRONMENT_REFUSAL_NOTE,
    LITE,
    REVIEW_FILE_LINE_CAP,
    USAGE_FINAL,
    USAGE_OPEN,
    USAGE_PARTIAL,
    USAGE_RECOUNTED,
    USAGE_RESTAMP_CEILING_HOURS,
    USAGE_RESTAMP_EVERY_MINUTES,
    changed_paths,
    diff_plan,
    file_line_counts,
    is_generated,
    recount_review_usage,
    rules_candidates,
    rules_char_cap,
    split_generated,
    sweep_review_dispatches,
)
from tests.review_dispatch_support import (
    _LOCAL_REPORT,
    _REAL_REFUSAL,
    _any_dispatch_row,
    _dispatch_row,
    _DispatchRecorder,
    _local_principal,
    _machine_report,
    _report_on,
    _reporting_stub,
    _stub_reviewer,
    _submitted,
    _wire,
)


# --- Счёт агента целиком (#1413) -------------------------------------------
# Deep-харнесс делает несколько прогонов под одним агентом, а провайдер
# начисляет их все. Хаб брал usage одного прогона (?runId=) и записывал долю:
# заказ 420 — 1 825 481 против 13 577 260 по выгрузке Cursor.


class _Provider:
    """Подставка провайдера для счёта: агрегат по агенту и его прогоны.

    ``aggregate`` — ответ ``/usage`` без runId (None: провайдер отказал или
    молчит); ``runs`` — список ``(run_id, status, tokens)`` (None: список
    прогонов не прочитан; tokens None: ``/usage?runId=`` не ответил).
    """

    def __init__(self, aggregate, runs):
        self.aggregate = aggregate
        self.runs = runs

    def wire(self, monkeypatch) -> None:
        monkeypatch.setattr(cursor_cloud, "get_usage", self.usage)
        monkeypatch.setattr(cursor_cloud, "list_runs", self.list_runs)
        monkeypatch.setattr(cursor_cloud, "get_run", self.get_run)

    @staticmethod
    def _body(tokens):
        return None if tokens is None else {"totalUsage": {"totalTokens": tokens}}

    async def usage(self, agent_id, run_id=None):
        if not run_id:
            return self._body(self.aggregate)
        for rid, _status, tokens in self.runs or []:
            if rid == run_id:
                return self._body(tokens)
        return None

    async def list_runs(self, agent_id):
        if self.runs is None:
            return None
        return [{"id": rid, "status": status} for rid, status, _ in self.runs]

    async def get_run(self, agent_id, run_id):
        for rid, status, _tokens in self.runs or []:
            if rid == run_id:
                return {"id": rid, "status": status}
        return None


async def _billed_task(client, db, monkeypatch, slug: str, agent: str) -> int:
    recorder = _DispatchRecorder({"agent": {"id": agent}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, slug)
    await _report_on(client, task_id)
    return task_id


@pytest.mark.parametrize("shape", ["aggregate", "refused_without_run_id"])
async def test_provider_tokens_sum_every_run_of_the_agent(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, shape: str
):
    """AC-1 (#1413): два прогона агента по 2 млн → 4 млн.

    Живая проба 27.09 подтвердила агрегат (/usage без runId → totalUsage и
    runs[]); отказ с суммой по прогонам остаётся запасным путём и покрыт тоже.
    """
    task_id = await _billed_task(client, db, monkeypatch, f"spike-{shape}", "bc-deep")
    runs = [("r-1", "FINISHED", 2_000_000), ("r-2", "FINISHED", 2_000_000)]
    _Provider(4_000_000 if shape == "aggregate" else None, runs).wire(monkeypatch)

    await sweep_review_dispatches(db)

    row = await _any_dispatch_row(db, task_id)
    assert row["status"] == "done"
    assert row["provider_tokens"] == 4_000_000, "не счёт одного прогона r-1"
    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["provider_tokens"] == 4_000_000


@pytest.mark.parametrize("answered", [True, False])
async def test_single_run_usage_unchanged_and_unknown_stays_null(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, answered: bool
):
    """AC-2 (#1413): lite с одним прогоном — как было; молчание — NULL.

    Пара 412 из сверки 25.09: хаб 811 071 и Cursor 811 071.
    """
    task_id = await _billed_task(client, db, monkeypatch, "spike-lite", "bc-lite")
    if answered:
        _Provider(811_071, [("r-1", "FINISHED", 811_071)]).wire(monkeypatch)
    else:
        _Provider(None, None).wire(monkeypatch)

    await sweep_review_dispatches(db)

    row = await _any_dispatch_row(db, task_id)
    assert row["status"] == "done"
    if answered:
        assert row["provider_tokens"] == 811_071
    else:
        assert row["provider_tokens"] is None, "не ноль и не старое значение"
        saved = dict(await repo.get_latest_machine_review(db, task_id))
        assert saved["provider_tokens"] is None


async def test_provider_tokens_restamped_after_agent_finishes(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1413): счёт, снятый на живом агенте, переснимается после конца."""
    task_id = await _billed_task(client, db, monkeypatch, "spike-restamp", "bc-live")
    provider = _Provider(1_000_000, [("r-1", "RUNNING", 1_000_000)])
    provider.wire(monkeypatch)

    await sweep_review_dispatches(db)
    row = await _any_dispatch_row(db, task_id)
    assert row["status"] == "done"
    assert row["provider_tokens"] == 1_000_000
    assert row["usage_scope"] == USAGE_OPEN

    # Агент дописал ещё два прогона и закончил.
    provider.aggregate = 4_000_000
    provider.runs = [
        ("r-1", "FINISHED", 1_000_000),
        ("r-2", "FINISHED", 2_500_000),
        ("r-3", "FINISHED", 500_000),
    ]
    await sweep_review_dispatches(db)
    held = await _any_dispatch_row(db, task_id)
    assert held["provider_tokens"] == 1_000_000, (
        f"опрос не чаще раза в {USAGE_RESTAMP_EVERY_MINUTES} мин"
    )

    await db.execute(
        "UPDATE review_dispatches SET usage_checked_at = datetime('now', ?)",
        (f"-{USAGE_RESTAMP_EVERY_MINUTES + 1} minutes",),
    )
    await db.commit()
    await sweep_review_dispatches(db)

    row = await _any_dispatch_row(db, task_id)
    assert row["provider_tokens"] == 4_000_000
    assert row["usage_scope"] == USAGE_FINAL
    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["provider_tokens"] == 4_000_000

    # Итог закреплён: следующий проход провайдера не спрашивает.
    provider.aggregate = 9_999_999
    await db.execute(
        "UPDATE review_dispatches SET usage_checked_at = datetime('now', '-1 day')"
    )
    await db.commit()
    await sweep_review_dispatches(db)
    assert (await _any_dispatch_row(db, task_id))["provider_tokens"] == 4_000_000


async def test_restamp_waits_while_any_run_of_the_agent_is_alive(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1413: свой прогон кончился, соседний идёт — счёт ещё не итог."""
    task_id = await _billed_task(client, db, monkeypatch, "spike-alive", "bc-alive")
    provider = _Provider(
        1_500_000, [("r-1", "FINISHED", 1_000_000), ("r-2", "RUNNING", 500_000)]
    )
    provider.wire(monkeypatch)
    await sweep_review_dispatches(db)
    await db.execute(
        "UPDATE review_dispatches SET usage_checked_at = datetime('now', '-1 day')"
    )
    await db.commit()
    await sweep_review_dispatches(db)

    row = await _any_dispatch_row(db, task_id)
    assert row["usage_scope"] == USAGE_OPEN
    assert row["provider_tokens"] == 1_500_000


async def _restamp_pass(db) -> dict:
    await db.execute(
        "UPDATE review_dispatches SET usage_checked_at = datetime('now', '-1 day')"
    )
    await db.commit()
    await sweep_review_dispatches(db)
    rows = await db.execute_fetchall("SELECT * FROM review_dispatches")
    return dict(rows[-1])


async def test_unread_run_list_does_not_settle_on_own_finished_run(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Находка ce0ad2369052fb62: список прогонов не прочитан — «не знаю»."""
    await _billed_task(client, db, monkeypatch, "spike-unread", "bc-unread")
    _Provider(1_000_000, None).wire(monkeypatch)
    await sweep_review_dispatches(db)

    async def _own_finished(agent_id, run_id):
        return {"id": run_id, "status": "FINISHED"}

    monkeypatch.setattr(cursor_cloud, "get_run", _own_finished)
    row = await _restamp_pass(db)
    assert row["usage_scope"] == USAGE_OPEN, "свой FINISHED — не итог агента"


async def test_zero_bill_of_a_finished_agent_is_not_named(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Находка e72f1c6229457e81: ноль у отработавшего агента — не итог (#1026)."""
    await _billed_task(client, db, monkeypatch, "spike-zero", "bc-zero")
    provider = _Provider(1_000_000, [("r-1", "RUNNING", 1_000_000)])
    provider.wire(monkeypatch)
    await sweep_review_dispatches(db)
    provider.aggregate, provider.runs = 0, [("r-1", "FINISHED", 0)]

    row = await _restamp_pass(db)
    assert (row["usage_scope"], row["provider_tokens"]) == (USAGE_OPEN, 1_000_000)


async def test_expired_open_bill_is_marked_partial_and_named_once(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Находка b9ab3c630ced1ed6: окно вышло — число помечено нижней границей."""
    task_id = await _billed_task(client, db, monkeypatch, "spike-late", "bc-late")
    _Provider(1_000_000, [("r-1", "RUNNING", 1_000_000)]).wire(monkeypatch)
    await sweep_review_dispatches(db)
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', ?)",
        (f"-{USAGE_RESTAMP_CEILING_HOURS + 1} hours",),
    )
    await db.commit()

    row = await _restamp_pass(db)
    await _restamp_pass(db)

    assert (row["usage_scope"], row["provider_tokens"]) == (USAGE_PARTIAL, 1_000_000)
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    named = [u for u in updates if "нижняя граница" in u["content"]]
    assert len(named) == 1 and named[0]["kind"] == "alert"


async def test_history_is_not_restamped_by_the_poller(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1413: строки до задачи (usage_scope пусто) — только командой владельца."""
    task_id = await _billed_task(client, db, monkeypatch, "spike-old", "bc-old")
    _Provider(1_000_000, [("r-1", "FINISHED", 1_000_000)]).wire(monkeypatch)
    await sweep_review_dispatches(db)
    await db.execute(
        "UPDATE review_dispatches SET usage_scope = '', "
        "usage_checked_at = datetime('now', '-1 day')"
    )
    await db.commit()
    _Provider(7_000_000, [("r-1", "FINISHED", 7_000_000)]).wire(monkeypatch)

    await sweep_review_dispatches(db)

    assert (await _any_dispatch_row(db, task_id))["provider_tokens"] == 1_000_000


async def test_owner_recount_marks_rows_and_leaves_unknown_alone(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1413: пересчёт истории — выборочно, по команде, с пометкой строки."""
    task_id = await _billed_task(client, db, monkeypatch, "spike-recount", "bc-420")
    _Provider(1_825_481, [("r-1", "FINISHED", 1_825_481)]).wire(monkeypatch)
    await sweep_review_dispatches(db)
    await db.execute("UPDATE review_dispatches SET usage_scope = ''")
    await db.commit()
    dispatch_id = (await _any_dispatch_row(db, task_id))["id"]

    _Provider(13_577_260, None).wire(monkeypatch)
    dry = await recount_review_usage(db, dispatch_ids=[dispatch_id], apply=False)
    assert [(r["id"], r["before"], r["after"]) for r in dry] == [
        (dispatch_id, 1_825_481, 13_577_260)
    ]
    assert (await _any_dispatch_row(db, task_id))["provider_tokens"] == 1_825_481

    done = await recount_review_usage(db, dispatch_ids=[dispatch_id], apply=True)
    assert done[0]["applied"] is True
    row = await _any_dispatch_row(db, task_id)
    assert row["provider_tokens"] == 13_577_260
    assert row["usage_scope"] == USAGE_RECOUNTED
    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["provider_tokens"] == 13_577_260

    # Агент уже удалён у провайдера: строку не трогаем, называем причину.
    _Provider(None, None).wire(monkeypatch)
    gone = await recount_review_usage(db, dispatch_ids=[dispatch_id], apply=True)
    assert gone[0]["after"] is None and gone[0]["applied"] is False
    assert (await _any_dispatch_row(db, task_id))["provider_tokens"] == 13_577_260


def test_recount_script_refuses_without_selection_and_names_unknown(monkeypatch):
    """#1413: скрипт владельца без выборки не бежит; молчание названо словами."""
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts/recount_review_usage.py"
    spec = importlib.util.spec_from_file_location("recount_review_usage", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr("sys.argv", ["recount_review_usage.py"])
    with pytest.raises(SystemExit):
        module.main()
    text = module._render(
        [
            {
                "id": 7,
                "task_id": 1,
                "profile": "deep",
                "agent_id": "bc-1",
                "before": 10,
                "after": None,
                "applied": False,
            },
        ],
        apply=True,
    )
    assert "не назван провайдером" in text and "[пропущен]" in text


async def test_list_runs_reads_the_items_page(monkeypatch):
    """#1413: список прогонов — форма ``items`` + ``nextCursor``, как у агентов."""
    pages = {
        "/v1/agents/bc-1/runs?limit=50": {
            "items": [{"id": "r-1", "status": "FINISHED"}],
            "nextCursor": "c2",
        },
        "/v1/agents/bc-1/runs?limit=50&cursor=c2": {
            "items": [{"id": "r-2", "status": "RUNNING"}]
        },
    }

    async def _request(method, path, json_body=None):
        return pages.get(path)

    monkeypatch.setattr(cursor_cloud, "_request", _request)
    assert await cursor_cloud.list_runs("bc-1") == [
        {"id": "r-1", "status": "FINISHED"},
        {"id": "r-2", "status": "RUNNING"},
    ]
    pages["/v1/agents/bc-1/runs?limit=50"] = {"runs": []}
    assert await cursor_cloud.list_runs("bc-1") is None, "чужая форма — не пусто"


# --- Repository review rules in the prompt (#873) ---------------------------
# The reviewer used to read the diff knowing nothing about the code it came
# from, while _PROCESS_SURFACES already listed the classes this repository
# burns itself on and spent them on routing alone.

_RULES_DIFF = "+++ b/hub/services/notes.py\n+одна строка кода\n"


def test_changed_paths_ignores_deletions_and_dedupes():
    diff = (
        "+++ b/hub/a.py\n+x\n+++ /dev/null\n+++ b/hub/a.py\n+y\n+++ b/docs/b.md\n+z\n"
    )
    assert changed_paths(diff) == ["hub/a.py", "docs/b.md"]


def test_rules_candidates_root_first_nearest_last():
    # The root file applies even to a diff nobody could read, and the file
    # closest to the change is read last so it can override.
    assert rules_candidates([]) == [".hub/REVIEW_RULES.md"]
    assert rules_candidates(["hub/services/notes.py"]) == [
        ".hub/REVIEW_RULES.md",
        "hub/.hub/REVIEW_RULES.md",
        "hub/services/.hub/REVIEW_RULES.md",
    ]


async def test_repo_rules_collected_up_the_tree(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#873): a root rules file and one in the changed file's directory
    # both reach the prompt, with the nearest one last.
    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-rules"}, "run": {"id": "r-rules"}}
    )
    _wire(monkeypatch, recorder)

    await _submitted(
        client,
        db,
        "spike-rules",
        diff=_RULES_DIFF,
        rules={
            ".hub/REVIEW_RULES.md": "ПРАВИЛО-КОРНЕВОЕ",
            "hub/services/.hub/REVIEW_RULES.md": "ПРАВИЛО-БЛИЖНЕЕ",
        },
    )

    prompt = recorder.calls[0]["prompt_text"]
    assert "ПРАВИЛО-КОРНЕВОЕ" in prompt and "ПРАВИЛО-БЛИЖНЕЕ" in prompt
    assert prompt.index("ПРАВИЛО-КОРНЕВОЕ") < prompt.index("ПРАВИЛО-БЛИЖНЕЕ"), (
        "the rules nearest the changed file must be read last"
    )
    # The framing is part of the contract, not decoration: a list presented as
    # exhaustive becomes the ceiling of the reviewer's attention.
    assert "СМОТРИ ШИРЕ" in prompt


async def test_review_rules_carry_finding_form(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#1866): the repository's own rules file, read from the base branch,
    # reaches the order with the "Форма находки" section whole.
    recorder = _DispatchRecorder({"agent": {"id": "bc-form"}, "run": {"id": "r-form"}})
    _wire(monkeypatch, recorder)
    rules = (Path(__file__).resolve().parents[1] / ".hub/REVIEW_RULES.md").read_text(
        "utf-8"
    )

    await _submitted(
        client,
        db,
        "spike-form",
        diff=_RULES_DIFF,
        rules={".hub/REVIEW_RULES.md": rules},
    )

    prompt = recorder.calls[0]["prompt_text"]
    assert "ОБРЕЗАНО" not in prompt, "the section must fit the rules cap"
    for needle in (
        "## Форма находки",
        "класс дефекта",
        "существовало до задачи",
        "out_of_scope",
        "всё ещё открыто",
        "третьего возврата",
        "сходится ли подход",
    ):
        assert needle in prompt, needle


async def test_missing_repo_rules_is_stated_not_silent(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#873): no rules file is a NAMED absence — in the prompt and in the
    # task update. Silence there reads as "nothing ever broke here".
    recorder = _DispatchRecorder({"agent": {"id": "bc-norules"}, "run": {"id": "r-nr"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-norules", diff=_RULES_DIFF)

    prompt = recorder.calls[0]["prompt_text"]
    assert "ПРАВИЛ РЕПОЗИТОРИЯ НЕТ" in prompt
    assert "отсутствие данных" in prompt
    updates = [dict(r) for r in await repo.get_task_updates(db, task_id)]
    dispatched = [u for u in updates if "Кросс-модельное ревью вызвано" in u["content"]]
    assert dispatched and "правил нет" in dispatched[-1]["content"]


async def test_repo_rules_are_capped(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#873): rules cannot eat the budget they were written to protect.
    # What did not fit is named — an unread rule is "not checked", not "absent".
    recorder = _DispatchRecorder({"agent": {"id": "bc-cap"}, "run": {"id": "r-cap"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_LITE_TOKEN_BUDGET", 400)
    assert rules_char_cap() == 400, "a quarter of the ceiling, counted in chars"

    task_id = await _submitted(
        client,
        db,
        "spike-cap",
        diff=_RULES_DIFF,
        rules={
            ".hub/REVIEW_RULES.md": "КОРОТКОЕ-ПРАВИЛО",
            "hub/services/.hub/REVIEW_RULES.md": "Д" * 500,
        },
    )

    prompt = recorder.calls[0]["prompt_text"]
    assert "КОРОТКОЕ-ПРАВИЛО" in prompt, "what fits is still delivered"
    assert "Д" * 500 not in prompt
    assert "ОБРЕЗАНО" in prompt
    assert "hub/services/.hub/REVIEW_RULES.md" in prompt, "the dropped file is named"
    updates = [dict(r) for r in await repo.get_task_updates(db, task_id)]
    dispatched = [u for u in updates if "Кросс-модельное ревью вызвано" in u["content"]]
    assert dispatched and "обрезано" in dispatched[-1]["content"]


# --- Diff hygiene: the budget goes on code (#874) ---------------------------
#
# The hub does not hand the reviewer a diff — it reads one for itself and tells
# the reviewer to run git. So for the reviewer the exclusion list is a pathspec
# in the command it is given plus the names of what was left out; inside the
# hub it is a real filter over the diff that picks the profile.

_LOCK_DIFF = (
    "diff --git a/uv.lock b/uv.lock\n"
    "--- a/uv.lock\n"
    "+++ b/uv.lock\n"
    "+    asyncio.gather(everything)\n"
    "diff --git a/hub/services/notes.py b/hub/services/notes.py\n"
    "--- a/hub/services/notes.py\n"
    "+++ b/hub/services/notes.py\n"
    "+одна строка кода\n"
)


def test_is_generated_knows_artefacts_from_code():
    assert is_generated("uv.lock") and is_generated("web/package-lock.json")
    assert is_generated("tests/__snapshots__/card.txt")
    assert is_generated("static/app.min.js")
    # The list must stay narrow: a broad mask would hide real code silently.
    assert not is_generated("hub/services/lockfile_reader.py")
    assert not is_generated("hub/config.json")


def test_split_generated_drops_only_the_artefact_hunks():
    kept, dropped = split_generated(_LOCK_DIFF)
    assert dropped == ["uv.lock"]
    assert "asyncio.gather(everything)" not in kept
    assert "одна строка кода" in kept


def test_file_line_counts_orders_by_size():
    diff = "+++ b/a.py\n+1\n+2\n+3\n+++ b/b.py\n-1\n+++ /dev/null\n-x\n"
    assert file_line_counts(diff) == [("a.py", 3), ("b.py", 1)]


def test_generated_files_excluded_and_named():
    # AC-1 (#874): the reviewer gets a command it can run as given, and every
    # exclusion is named where it will read it. A quiet exclusion would read as
    # "there was nothing there" (#824).
    block, note = diff_plan(_LOCK_DIFF, "develop", "task-874/x")

    assert "git diff develop...task-874/x --" in block
    assert "':(exclude)uv.lock'" in block
    assert "uv.lock" in block and "сгенерированны" in block
    assert "исключено сгенерированных: 1" in note


def test_unreadable_diff_says_so_instead_of_excluding_nothing():
    # "We could not read it" and "there was nothing to exclude" are different
    # answers, and the second one reads as a clean, complete subject (#725).
    block, note = diff_plan(None, "develop", "task-874/x")
    assert "не удалось" in block
    assert ":(exclude)" not in block
    assert "не прочитан" in note


async def test_generated_file_marker_does_not_buy_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#874): a marker inside a lock file is not code anyone wrote. Before
    # this, one such line was the cheapest possible way to buy the expensive
    # harness — and nobody would have noticed the bill.
    recorder = _DispatchRecorder({"agent": {"id": "bc-gen"}, "run": {"id": "r-gen"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-generated", diff=_LOCK_DIFF)

    assert (await _dispatch_row(db, task_id))["profile"] == "lite", (
        "asyncio.gather in uv.lock must not buy deep"
    )
    # And the same marker in real code still does.
    real = _LOCK_DIFF.replace("+++ b/uv.lock", "+++ b/hub/services/pool.py")
    other = await _submitted(client, db, "spike-real-marker", diff=real)
    assert (await _dispatch_row(db, other))["profile"] == "deep"


async def test_oversized_file_is_named_not_left_to_eat_budget(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#874): one huge file must not silently consume the pass the other
    # files were waiting for. The hub has no diff to truncate, so it names the
    # file and its size and demands the remainder in lost_dimensions.
    recorder = _DispatchRecorder({"agent": {"id": "bc-big"}, "run": {"id": "r-big"}})
    _wire(monkeypatch, recorder)
    big = "+++ b/hub/services/huge.py\n" + "+строка\n" * (REVIEW_FILE_LINE_CAP + 5)
    small = "+++ b/hub/services/small.py\n+одна строка\n"

    await _submitted(client, db, "spike-oversized", diff=big + small)

    prompt = recorder.calls[0]["prompt_text"]
    assert "НЕ ПОМЕСТЯТСЯ В ОДИН ПРОХОД" in prompt
    assert f"hub/services/huge.py ({REVIEW_FILE_LINE_CAP + 5} строк)" in prompt
    assert "lost_dimensions" in prompt
    assert "hub/services/small.py" not in prompt.split("НЕ ПОМЕСТЯТСЯ")[1], (
        "the small file is not the one that did not fit"
    )
    assert "git diff" in prompt, "the rest of the subject is still under review"


async def test_incomplete_lite_report_buys_a_deep_top_up(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#879): a cheap run that says it did not finish is topped up in the
    # SAME generation, and the human is not handed the unfinished work.
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-topup")
    assert (await _dispatch_row(db, task_id))["profile"] == "lite"

    await _machine_report(client, task_id, incomplete=True)

    assert len(recorder.calls) == 2, "the incomplete run bought a second one"
    row = await _dispatch_row(db, task_id)
    assert row["profile"] == "deep"
    task = dict(await repo.get_task(db, task_id))
    assert row["submission_generation"] == task["submission_generation"], (
        "the top-up belongs to the submission it tops up"
    )
    assert task["status"] == "review", "still under review, not handed over"
    assert "multi-agent-review" in recorder.calls[1]["prompt_text"]


async def test_complete_lite_report_buys_nothing(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # The ladder must not fire on a run that finished. Otherwise "lite by
    # default" becomes "always both", which costs more than always-deep did.
    recorder = _DispatchRecorder({"agent": {"id": "bc-2"}, "run": {"id": "r-2"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-complete")

    await _machine_report(client, task_id, incomplete=False)

    assert len(recorder.calls) == 1, "a finished run buys no top-up"


async def test_escalation_ladder_has_a_ceiling(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#879): a second incomplete report does NOT buy a third run. The
    # human gets a named cause instead of silence — a reviewer that keeps
    # declaring itself unfinished is a problem to look at, not to fund.
    recorder = _DispatchRecorder({"agent": {"id": "bc-3"}, "run": {"id": "r-3"}})
    _wire(monkeypatch, recorder)
    # #1243: an incomplete DEEP top-up now goes to the second axis (another
    # model) before this ceiling — finding 123ea6f62ddd2919. Switched off
    # here so the test keeps guarding what it always guarded: the ladder's
    # own ceiling on PROFILE steps. With the axis on, the same path is
    # test_a_deep_top_up_that_stays_incomplete_goes_to_the_second_axis.
    monkeypatch.setattr(config, "REVIEW_MODEL_CASCADE_MAX", 0)
    task_id = await _submitted(client, db, "spike-ceiling")

    await _machine_report(client, task_id, incomplete=True)
    assert len(recorder.calls) == 2
    # The top-up itself comes back unfinished.
    await _machine_report(client, task_id, incomplete=True)

    assert len(recorder.calls) == 2, "the ceiling holds at two runs"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    alerts = [u for u in updates if u["kind"] == "alert" and "потолок" in u["content"]]
    assert alerts, "the ceiling is announced, not silent"
    assert "решение за человеком" in alerts[-1]["content"].lower()


async def test_top_up_never_fires_on_an_unknown_profile(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # "We do not know how this was reviewed" is not "it was reviewed cheaply"
    # (#807). Topping up an unknown profile would buy a run on a guess.
    recorder = _DispatchRecorder({"agent": {"id": "bc-4"}, "run": {"id": "r-4"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-unknown-profile")
    await db.execute(
        "UPDATE review_dispatches SET profile = '' WHERE task_id = ?", (task_id,)
    )
    await db.commit()

    await _machine_report(client, task_id, incomplete=True)

    assert len(recorder.calls) == 1, "an unknown profile buys nothing"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    alerts = [
        u
        for u in updates
        if u["kind"] == "alert" and "добор не положен" in u["content"]
    ]
    assert alerts and "не заявлен" in alerts[-1]["content"], (
        "the refusal names its cause instead of passing in silence"
    )


# --- The second axis: same work, same profile, a stronger model (#1243) ------
#
# The ladder above climbs the PROFILE and stops at deep with «выше дешёвого
# подниматься некуда». Measured by spike #1168: 30 of 51 incomplete reports
# in the 90-day window were already deep, i.e. most of the unfinished reading
# sits exactly on the rung where the ladder has nothing to offer. The second
# axis keeps the profile and changes the MODEL, with its own ceiling per
# generation and its own outcome metric.

_CASCADE_MARK = "вторая ось"


async def _deep_submitted(
    client: AsyncClient, db: aiosqlite.Connection, slug: str
) -> int:
    """A submission whose first run was already deep — the ladder's dead end."""
    task_id = await _submitted(client, db, slug)
    await db.execute(
        "UPDATE review_dispatches SET profile = ? WHERE task_id = ?", (DEEP, task_id)
    )
    await db.commit()
    return task_id


async def _cascade_alerts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert" and _CASCADE_MARK in dict(u)["content"]
    ]


async def test_a_deep_incomplete_report_is_asked_again_of_a_stronger_model(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1243): неполный deep-отчёт переспрашивается ТОЙ ЖЕ работой, тем
    же профилем, но ДРУГОЙ моделью — и карточка называет причину и модель."""
    from hub.services import review_dispatch

    recorder = _DispatchRecorder({"agent": {"id": "bc-m1"}, "run": {"id": "r-m1"}})
    _wire(monkeypatch, recorder)
    task_id = await _deep_submitted(client, db, "spike-cascade")
    first_model = recorder.calls[0]["model_id"]

    await _machine_report(client, task_id, incomplete=True)

    assert len(recorder.calls) == 2, "the deep incomplete report bought a second run"
    second = recorder.calls[1]
    stronger = second["model_id"]
    assert stronger != first_model, "the same model would read the same way"
    assert stronger in config.SUBSCRIPTION_LAUNCHABLE_MODELS, (
        "only a model whose launch was observed may be bought"
    )
    assert stronger in review_dispatch.config.REVIEW_CASCADE_MODELS
    assert family(stronger) != family("claude-fable-5"), "monoculture gate held"
    assert "multi-agent-review" in second["prompt_text"], "the profile stays deep"
    row = await _dispatch_row(db, task_id)
    assert row["profile"] == DEEP and row["model"] == stronger
    task = dict(await repo.get_task(db, task_id))
    assert row["submission_generation"] == task["submission_generation"]
    assert task["status"] == "review", "still under review, not handed over"

    alerts = await _cascade_alerts(db, task_id)
    assert alerts, "the second purchase is visible in the card, not silent"
    assert stronger in alerts[-1] and first_model in alerts[-1], "models named"
    assert "неполн" in alerts[-1].lower(), "the cause is named"
    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert not [u for u in updates if "добор не положен" in u], (
        "the ladder's dead end is no longer the last word on a deep report"
    )


_NO_CLOUD_WORDS = "облако не заказано, решает человек"


async def test_incomplete_local_deep_does_not_buy_a_cloud_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1587 (D6): неполный отчёт ЛОКАЛЬНОГО deep облачным deep не лечится.

    Признак — канал заказа, который породил отчёт; обе двери оси (первая
    попытка и уже записанные попытки), отчёт контрактный и восстановленный
    из текста. Облачный отчёт идёт как раньше (защита от регресса), а
    неоднозначное сопоставление — как локальный: не покупать и назвать.
    """
    from hub.models import MachineReviewSubmit
    from hub.services import machine_review_intake as intake
    from hub.services import review_dispatch

    monkeypatch.setattr(config, "REVIEW_MODEL_CASCADE_MAX", 3)
    body = {
        "harness_skill": "multi-agent-review",
        "agent_count": 1,
        "tokens_spent": 12000,
        "model": "grok-4.6",
        "raw_count": 1,
        "incomplete": True,
        "lost_dimensions": ["hub/services/big.py"],
        "agent": "cursor-cloud-reviewer",
    }
    n = 0

    async def run_case(channel: str, attempts: int, text: bool, ambiguous=False):
        nonlocal n
        n += 1
        recorder = _DispatchRecorder(
            {"agent": {"id": f"bc-d6-{n}"}, "run": {"id": f"r-d6-{n}"}}
        )
        _wire(monkeypatch, recorder)
        task_id = await _deep_submitted(client, db, f"spike-d6-{n}")
        assert len(recorder.calls) == 1
        await db.execute(
            "UPDATE review_dispatches SET channel = ? WHERE task_id = ?",
            (channel, task_id),
        )
        generation = (dict(await repo.get_task(db, task_id)))["submission_generation"]
        for _ in range(attempts):
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "alert",
                review_dispatch.MODEL_CASCADE_MARK.format(generation=generation)
                + " прежняя попытка",
            )
        await db.commit()
        if ambiguous:
            # Две строки без принципала разных каналов: отчёт подходит обеим.
            await repo.create_review_dispatch(
                db,
                task_id=task_id,
                submission_generation=generation,
                agent_id="local:amb",
                run_id="amb",
                model="grok-4.6",
                profile=DEEP,
                channel="local",
            )
            await db.commit()
        await intake.record_machine_review(
            db,
            task_id,
            MachineReviewSubmit(**body),
            principal_id=None,
            username="hub",
            origin=intake.ORIGIN_LOCAL_TEXT if text else intake.ORIGIN_MCP,
        )
        alerts = [
            dict(u)["content"]
            for u in await repo.get_task_updates(db, task_id)
            if dict(u)["kind"] == "alert"
        ]
        return len(recorder.calls), alerts

    for attempts in (0, 1):
        for text in (False, True):
            where = f"attempts={attempts} text={text}"
            bought, alerts = await run_case("local", attempts, text)
            assert bought == 1, f"local deep bought a cloud run: {where}"
            named = [a for a in alerts if _NO_CLOUD_WORDS in a]
            assert named and "неполн" in named[-1].lower(), where
            assert len(named) == 1 and "#1587" in named[0], where

            # Regression guard: a CLOUD report keeps the second axis.
            bought, alerts = await run_case("cloud", attempts, text)
            assert bought == 2, f"cloud incomplete deep lost its second axis: {where}"
            assert not [a for a in alerts if _NO_CLOUD_WORDS in a], where

    bought, alerts = await run_case("cloud", 0, False, ambiguous=True)
    assert bought == 1, "an unmatched order must not buy the cloud"
    assert [a for a in alerts if _NO_CLOUD_WORDS in a and "однозначно" in a]
    await _second_door_shapes(client, db, monkeypatch, tmp_path)


async def _incomplete_local_report(db, task_id: int, principal_id: int) -> None:
    from hub.models import MachineReviewSubmit
    from hub.services import machine_review_intake as intake

    await intake.record_machine_review(
        db,
        task_id,
        MachineReviewSubmit(
            harness_skill="multi-agent-review",
            agent_count=4,
            tokens_spent=1_500_000,
            model="grok-4.6",
            raw_count=1,
            incomplete=True,
            lost_dimensions=["hub/services/big.py"],
            agent="local-reviewer",
        ),
        principal_id=principal_id,
        username="local-reviewer",
        origin=intake.ORIGIN_MCP,
    )


async def _second_door_shapes(client, db, monkeypatch, tmp_path) -> None:
    """Продовые формы: у поколения ДВЕ строки, и канал отчёта — не у первой/последней."""
    from hub.services.review_dispatch import wait_for_local_runs

    monkeypatch.setattr(config, "REVIEW_MODEL_CASCADE_MAX", 3)
    # 1. Реальный путь второй двери (#1252): облако без принципала кончилось без
    # отчёта и осталось, локальная замена новее; её неполный отчёт — local.
    recorder = _DispatchRecorder({"agent": {"id": "bc-sd1"}, "run": {"id": "r-sd1"}})
    _wire(monkeypatch, recorder)
    local_pid = await _local_principal(db, monkeypatch)
    incomplete = {**_LOCAL_REPORT, "incomplete": True, "lost_dimensions": ["a.py"]}
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub(incomplete))
    task_id = await _submitted(
        client,
        db,
        "spike-d6-second-door",
        policy={"review": "dispatch", "local_review_fallback": "on"},
        override="require",
    )
    assert len(recorder.calls) == 1
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()
    rows = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT channel, profile, reviewer_principal_id FROM review_dispatches "
            "WHERE task_id=? ORDER BY id",
            (task_id,),
        )
    ]
    assert [r["channel"] for r in rows] == ["cloud", "local"], rows
    assert rows[0]["reviewer_principal_id"] is None and rows[1]["profile"] == DEEP
    assert len(recorder.calls) == 1, (
        "неполный отчёт локальной замены облако второй осью не купил"
    )
    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert [a for a in alerts if _NO_CLOUD_WORDS in a]

    # 2. Старый local (с принципалом) и НОВЕЕ облачная строка без принципала:
    # отчёт принадлежит local, а «последний заказ поколения» — облачный.
    recorder = _DispatchRecorder({"agent": {"id": "bc-sd2"}, "run": {"id": "r-sd2"}})
    _wire(monkeypatch, recorder)
    task_id = await _deep_submitted(client, db, "spike-d6-late-local")
    generation = dict(await repo.get_task(db, task_id))["submission_generation"]
    await db.execute(
        "UPDATE review_dispatches SET channel='local', agent_id='local:x', "
        "reviewer_principal_id=? WHERE task_id=?",
        (local_pid, task_id),
    )
    await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id="bc-newer",
        run_id="rn",
        model="grok-4.6",
        profile=DEEP,
        channel="cloud",
    )
    await db.commit()
    await _incomplete_local_report(db, task_id, local_pid)
    assert len(recorder.calls) == 1, "late local report must not buy the cloud"
    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert [a for a in alerts if _NO_CLOUD_WORDS in a]


async def test_a_deep_top_up_that_stays_incomplete_goes_to_the_second_axis(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Finding 123ea6f62ddd2919 (#1243): lite → deep top-up → deep incomplete.

    That is the spike's main bucket — a deep report with nothing above it on
    the profile ladder. With the ladder's ceiling checked first, the two
    paid rungs silenced the second axis exactly there."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-m6"}, "run": {"id": "r-m6"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-cascade-after-ladder")
    assert (await _dispatch_row(db, task_id))["profile"] == LITE

    await _machine_report(client, task_id, incomplete=True)
    assert len(recorder.calls) == 2, "the ladder bought its deep top-up"
    await _machine_report(client, task_id, incomplete=True)

    assert len(recorder.calls) == 3, "the incomplete deep top-up asks another model"
    models = [c["model_id"] for c in recorder.calls]
    assert models[2] not in models[:2], "a model that has not read this submission"
    assert "multi-agent-review" in recorder.calls[2]["prompt_text"]
    alerts = await _cascade_alerts(db, task_id)
    assert alerts and models[2] in alerts[-1]
    events = await db.execute_fetchall(
        "SELECT id FROM events WHERE kind = 'review_model_cascade' AND task_id = ?",
        (task_id,),
    )
    assert len(list(events)) == 1
    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert not [u for u in updates if "потолок лестницы" in u], (
        "the ladder's ceiling is not the answer to a deep report"
    )


async def test_after_the_ladder_the_second_axis_ceiling_is_the_one_named(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Finding 123ea6f62ddd2919 (#1243): lite → deep → other model → still
    incomplete. The ceiling that stops it is the second axis's own."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-m7"}, "run": {"id": "r-m7"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-cascade-after-ladder-ceiling")

    for _ in range(3):
        await _machine_report(client, task_id, incomplete=True)

    assert len(recorder.calls) == 3, "no fourth agent is bought"
    ceiling = (await _cascade_alerts(db, task_id))[-1]
    limit = config.REVIEW_MODEL_CASCADE_MAX
    assert "потолок второй оси" in ceiling and f"{limit} из {limit}" in ceiling
    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert not [u for u in updates if "потолок лестницы" in u]


async def test_the_stronger_model_is_taken_only_from_observed_launches(
    monkeypatch,
):
    """Constraint (#1243): a name in the catalogue is not a launch.

    The shipped list is a subset of the observed launches, and a configured
    name outside them is skipped rather than bought — the same distinction
    #1237 got wrong."""
    from hub.services import review_dispatch

    assert set(config.REVIEW_CASCADE_MODELS) <= set(
        config.SUBSCRIPTION_LAUNCHABLE_MODELS
    )
    monkeypatch.setattr(
        config, "REVIEW_CASCADE_MODELS", ("gpt-9-imaginary", "gemini-3.1-pro")
    )
    assert review_dispatch.pick_cascade_model("claude-fable-5", {"grok-4.6"}) == (
        "gemini-3.1-pro"
    )
    # Monoculture on the second attempt too: the implementer's family is out.
    monkeypatch.setattr(
        config, "REVIEW_CASCADE_MODELS", ("gpt-5.3-codex", "gemini-3.1-pro")
    )
    assert review_dispatch.pick_cascade_model("gpt-5.3-codex", set()) == (
        "gemini-3.1-pro"
    )
    # A model that already read this generation is not "another" model.
    assert review_dispatch.pick_cascade_model("claude-fable-5", {"gpt-5.3-codex"}) == (
        "gemini-3.1-pro"
    )
    assert (
        review_dispatch.pick_cascade_model(
            "claude-fable-5", {"gpt-5.3-codex", "gemini-3.1-pro"}
        )
        == ""
    )


async def test_an_environment_refusal_is_not_asked_of_another_model(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Risk (#1243, #1238): another model in the same environment hits the same
    wall, so an environment refusal buys nothing and the card says why."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-m5"}, "run": {"id": "r-m5"}})
    _wire(monkeypatch, recorder)
    task_id = await _deep_submitted(client, db, "spike-cascade-env")
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "multi-agent-review",
            "raw_count": 0,
            "findings_confirmed": [],
            "findings_rejected": [],
            "incomplete": True,
            "incomplete_reason": "environment",
            "unresolved": [],
            "lost_dimensions": ["прогон тестов: в среде нет uv/pytest"],
            "agent": "cursor-cloud-reviewer",
            "model": "grok-4.6",
        },
    )
    assert resp.status_code == 200, resp.text

    assert len(recorder.calls) == 1, "an environment refusal buys no second model"
    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    dead_end = [u for u in updates if "добор не положен" in u]
    assert dead_end and ENVIRONMENT_REFUSAL_NOTE in dead_end[-1]


class _SecondOrderRefused:
    """Провайдер: первый заказ принят, дальше — 400 invalid_model (#1314)."""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return {"agent": {"id": "bc-f1"}, "run": {"id": "r-f1"}}, None
        return None, _REAL_REFUSAL


async def _refused_second_order(client, db, monkeypatch, slug: str) -> int:
    provider = _SecondOrderRefused()
    _wire(monkeypatch, _DispatchRecorder(None))
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", provider)
    task_id = await _deep_submitted(client, db, slug)
    await _machine_report(client, task_id, incomplete=True)
    assert len(provider.calls) == 2, "вторая модель была заказана и отвергнута"
    return task_id


async def test_cascade_failed_order_recorded(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1566): отказ провайдера на заказ второй модели пишет событие
    review_model_cascade с outcome=failed, dispatch_id=null и текстом отказа."""
    task_id = await _refused_second_order(client, db, monkeypatch, "spike-cascade-400")

    rows = await db.execute_fetchall(
        "SELECT payload FROM events WHERE kind = 'review_model_cascade' "
        "AND task_id = ?",
        (task_id,),
    )
    assert len(list(rows)) == 1
    payload = json.loads(dict(rows[0])["payload"])
    assert payload["outcome"] == "failed"
    assert payload["dispatch_id"] is None
    assert "HTTP 400, invalid_model" in payload["reason"]
    assert payload["attempt"] == 1 and payload["model"]
    assert (
        payload["generation"]
        == (dict(await repo.get_task(db, task_id))["submission_generation"])
    )


async def test_review_economy_ignores_failed_cascade(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1566): неудавшаяся попытка не делает первый прогон сдачи
    «cascade» — by_kind cascade runs=0, первый остаётся kind=first."""
    from hub.services.orchestration import practice_metrics

    task_id = await _refused_second_order(
        client, db, monkeypatch, "spike-cascade-economy"
    )
    # Читатель не должен верить id в payload неудавшейся попытки: прогона у
    # неё нет, а метить «cascade» первый прогон сдачи значило бы приписать ему
    # чужую цену. Подсовываем id первого прогона — худший случай.
    first_run = dict(
        (
            await db.execute_fetchall(
                "SELECT MIN(id) AS id FROM review_dispatches "
                "WHERE task_id = ? AND agent_id != ''",
                (task_id,),
            )
        )[0]
    )["id"]
    await repo.insert_event(
        db,
        kind="review_model_cascade",
        task_id=task_id,
        actor="policy",
        payload={"outcome": "failed", "dispatch_id": first_run, "attempt": 2},
    )
    await db.commit()

    by_kind = (await practice_metrics(db))["review_economy"]["runs"]["by_kind"]
    cascade = [r for r in by_kind if r["kind"] == "cascade"]
    assert sum(r["runs"] for r in cascade) == 0
    first = [r for r in by_kind if r["kind"] == "first"]
    assert first and first[0]["runs"] == 1


async def test_a_complete_report_is_not_asked_again(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1243): a complete report of ANY profile buys nothing.

    Mutation «переспрашивать всегда» must drop exactly this test."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-m2"}, "run": {"id": "r-m2"}})
    _wire(monkeypatch, recorder)
    deep_task = await _deep_submitted(client, db, "spike-cascade-complete-deep")
    await _machine_report(client, deep_task, incomplete=False)
    lite_task = await _submitted(client, db, "spike-cascade-complete-lite")
    assert (await _any_dispatch_row(db, lite_task))["profile"] == LITE
    await _machine_report(client, lite_task, incomplete=False)

    assert len(recorder.calls) == 2, "one run per submission, no second purchase"
    for task_id in (deep_task, lite_task):
        assert not await _cascade_alerts(db, task_id)
    events = await db.execute_fetchall(
        "SELECT id FROM events WHERE kind = 'review_model_cascade'"
    )
    assert not list(events)


async def test_the_second_axis_has_its_own_ceiling(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1243): the second attempt coming back incomplete buys no third
    agent; the task goes to the human with the cause and the attempt count."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-m3"}, "run": {"id": "r-m3"}})
    _wire(monkeypatch, recorder)
    task_id = await _deep_submitted(client, db, "spike-cascade-ceiling")

    await _machine_report(client, task_id, incomplete=True)
    assert len(recorder.calls) == 2
    await _machine_report(client, task_id, incomplete=True)

    assert len(recorder.calls) == 2, "no third agent is bought"
    alerts = await _cascade_alerts(db, task_id)
    ceiling = alerts[-1]
    assert "потолок" in ceiling
    limit = config.REVIEW_MODEL_CASCADE_MAX
    assert f"{limit} из {limit}" in ceiling, "the number of attempts is named"
    assert recorder.calls[1]["model_id"] in ceiling
    assert "решение за человеком" in ceiling.lower()


async def test_the_second_axis_reports_its_own_outcome(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-4 (#1243): how many second attempts there were and how many of them
    came back COMPLETE, both with the sample size; «недобор» instead of a
    number when the sample is short."""
    from hub.services import review_dispatch
    from hub.services.orchestration import practice_metrics

    empty = await review_dispatch.count_model_cascade_outcomes(db, since_days=30)
    assert empty["attempts"] == 0 and empty["complete_share"] is None
    assert "недобор" in empty["share_note"]

    recorder = _DispatchRecorder({"agent": {"id": "bc-m4"}, "run": {"id": "r-m4"}})
    _wire(monkeypatch, recorder)
    healed = await _deep_submitted(client, db, "spike-cascade-healed")
    await _machine_report(client, healed, incomplete=True)
    await _machine_report(client, healed, incomplete=False)
    stuck = await _deep_submitted(client, db, "spike-cascade-stuck")
    await _machine_report(client, stuck, incomplete=True)
    await _machine_report(client, stuck, incomplete=True)
    pending = await _deep_submitted(client, db, "spike-cascade-pending")
    await _machine_report(client, pending, incomplete=True)

    short = await review_dispatch.count_model_cascade_outcomes(db, since_days=30)
    assert short["attempts"] == 3
    assert short["reported"] == 2 and short["pending"] == 1
    assert short["complete"] == 1 and short["incomplete"] == 1
    assert short["complete_share"] is None, "a short sample prints no share"
    assert "недобор" in short["share_note"]
    assert "из 2" in short["share_note"], "the sample size stands by the words"

    monkeypatch.setattr(review_dispatch, "MODEL_CASCADE_MIN_SAMPLE", 2)
    enough = await review_dispatch.count_model_cascade_outcomes(db, since_days=30)
    assert enough["complete_share"] == 0.5
    assert "1 из 2" in enough["share_note"]
    assert "недобор" not in enough["share_note"]

    metrics = await practice_metrics(db, since_days=30)
    assert metrics["review_model_cascade"]["attempts"] == 3
    assert metrics["review_model_cascade"]["complete"] == 1
