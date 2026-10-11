"""Hub-dispatched cross-model reviews (#757): incremental (split of the former single file, #1670)."""

from __future__ import annotations

import json

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import auth as hub_auth
from hub import config
from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.integrations.registry import plugins
from hub.services.model_family import family
from hub.services.review_dispatch import (
    _REVIEW_MODEL_PREFERENCES,
    maybe_dispatch_review,
    pick_review_model,
    pick_review_profile,
    sweep_review_dispatches,
)
from tests.review_dispatch_support import (
    _AUTHOR_FILE,
    _HARMLESS_DIFF,
    _PREV_SHA,
    _TIP,
    _agent_key,
    _AncestryGitOps,
    _dispatch_row,
    _DispatchRecorder,
    _expire_grace,
    _finished_run_with,
    _pinned_setup,
    _PinnedGitOps,
    _report_block,
    _second_generation,
    _submitted,
    _wire,
)

_DELTA = "+++ b/hub/services/fixed.py\n+исправление\n"
_BASE_FILE = "hub/services/readiness.py"
_MERGED_DELTA = (
    f"+++ b/{_AUTHOR_FILE}\n"
    "+исправление\n"
    f"+++ b/{_BASE_FILE}\n"
    "+        workspace_path = project.workspace_path\n"
)
_OWN_PLAIN = f"+++ b/{_AUTHOR_FILE}\n+исправление\n"
_OWN_WITH_SURFACE = (
    f"+++ b/{_AUTHOR_FILE}\n+        workspace_path = project.workspace_path\n"
)


async def test_submit_records_the_generation_in_the_ledger(
    client: AsyncClient, db: aiosqlite.Connection
):
    # The delta rests entirely on this row, and it is written by the real
    # submission path — not by the tests that reason about it. Asserted here
    # on purpose: every fallback in generation_delta is SAFE, so if the write
    # disappeared nothing would go red. Reviews would quietly go back to
    # reading the whole branch every round, and the only trace would be a
    # reason line saying the previous submission was never recorded.
    task_id = await _submitted(client, db, "spike-ledger")

    row = await repo.previous_submission(db, task_id, generation=2)

    assert row is not None, "submit_for_review must write the submission it pinned"
    recorded = dict(row)
    assert recorded["generation"] == 1
    assert recorded["sha"] == _TIP, "the ledger pins the tip the submission pinned"
    assert recorded["base_branch"], "the base travels with the sha or the delta lies"


async def test_resubmission_reviews_generation_delta(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#880): the second run reads the files this round of fixes touched,
    # whole and against the BASE — and says that is what it read.
    recorder = _DispatchRecorder({"agent": {"id": "bc-d"}, "run": {"id": "r-d"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-delta")
    plugins.git_ops = _AncestryGitOps(_TIP, ["docs/notes.md"], delta=_DELTA)

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert "'hub/services/fixed.py'" in prompt, "the command is narrowed to the delta"
    assert "прочитана ДЕЛЬТА" in prompt
    assert "дельта к поколению #1" in prompt
    # Whole files against the base: old code beside the new edit stays visible.
    assert "git diff develop...task-" in prompt
    assert "ЦЕЛИКОМ и против базовой ветки" in prompt


async def test_rebase_invalidates_delta(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#880): after a rebase the two commits no longer share a history,
    # so "what changed since last time" is unanswerable — full diff, and the
    # reason is named rather than left to be inferred from a wider command.
    recorder = _DispatchRecorder({"agent": {"id": "bc-r"}, "run": {"id": "r-r"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-rebase")
    plugins.git_ops = _AncestryGitOps(
        _TIP, ["docs/notes.md"], ancestor=False, delta=_DELTA
    )

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert "не предок текущего" in prompt and "перебазировали" in prompt
    assert "прочитан ВЕСЬ дифф" in prompt
    assert "прочитана ДЕЛЬТА" not in prompt


async def test_changed_base_branch_invalidates_delta(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # The same two commits answer a different question once the base moves.
    recorder = _DispatchRecorder({"agent": {"id": "bc-b"}, "run": {"id": "r-b"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-base", base_branch="main")
    plugins.git_ops = _AncestryGitOps(_TIP, ["docs/notes.md"], delta=_DELTA)

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert "базовая ветка сменилась" in prompt
    assert "прочитан ВЕСЬ дифф" in prompt


async def test_unrecorded_previous_submission_reads_everything(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # Tasks that were already in flight when the ledger appeared have no
    # previous row. Claiming a delta against a commit nobody recorded would be
    # reading less than the report says.
    recorder = _DispatchRecorder({"agent": {"id": "bc-n"}, "run": {"id": "r-n"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(
        client, db, "spike-noledger", record_previous=False
    )
    plugins.git_ops = _AncestryGitOps(_TIP, ["docs/notes.md"], delta=_DELTA)

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert "предыдущая сдача не записана" in prompt
    assert "прочитан ВЕСЬ дифф" in prompt


async def test_unreadable_ancestry_reads_everything(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # "Could not look" is not "they are related" (#725). An unanswerable
    # ancestry question must widen the review, never narrow it.
    recorder = _DispatchRecorder({"agent": {"id": "bc-u"}, "run": {"id": "r-u"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-unknown-ancestry")
    plugins.git_ops = _AncestryGitOps(
        _TIP, ["docs/notes.md"], ancestor=None, delta=_DELTA
    )

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert "историю проверить не удалось" in prompt
    assert "прочитана ДЕЛЬТА" not in prompt


# --- The delta starts where a recorded review ended (#1400) ------------------
#
# #1378, 25.09: generation 2 carried the fixes and got a deep run whose report
# was discarded (#1260) because generation 3 — a test tweak — arrived three
# minutes later. Generation 3 was reviewed as "delta to #2", so the fixes of
# generation 2 were read by nobody, and the clean report of generation 3 made
# the task look ready. The doubles below are that shape: gen2 touches a
# process-surface file, gen3 only a test.
_GEN2_SHA = "d" * 40
_GEN2_FILE = "hub/services/fs.py"
_GEN3_FILE = "tests/test_fs.py"
_GEN2_PART = f"+++ b/{_GEN2_FILE}\n+        workspace_path = project.workspace_path\n"
_GEN3_PART = f"+++ b/{_GEN3_FILE}\n+правка теста\n"


class _ThreeGenerationsGitOps(_AncestryGitOps):
    """Git whose answers depend on WHICH generation the delta starts from."""

    def _since(self, base: str) -> str | None:
        if base == _PREV_SHA:
            return _GEN2_PART + _GEN3_PART
        if base == _GEN2_SHA:
            return _GEN3_PART
        return None

    async def branch_diff(self, repo, base, branch, context=0):
        since = self._since(base)
        if since is not None:
            return since
        # #1418: the whole-branch diff goes through _PinnedGitOps, which gives
        # every pinned sha other than the tip its OWN author edit. Returning
        # self._diff for any sha read gen3 as "only the base was merged", and
        # #1361 carried the report over instead of ordering a review.
        return await _PinnedGitOps.branch_diff(self, repo, base, branch)

    async def delta_without_base(self, repo, base, prev, current):
        return self._since(prev)


async def _third_generation(
    client: AsyncClient,
    db: aiosqlite.Connection,
    slug: str,
    *,
    reviewed: tuple[int, ...],
    uncounted: dict[int, dict] | None = None,
) -> int:
    """A task on its THIRD submission; ``reviewed`` names generations with a report.

    ``uncounted`` adds reports that exist but do not count as reading the
    code (#1400, finding 452b39e570a0620b): generation -> extra columns.
    """
    task_id = await _second_generation(client, db, slug, reviewed=False)
    for generation, extra in (uncounted or {}).items():
        await repo.insert_machine_review(
            db, task_id=task_id, submission_generation=generation, **extra
        )
    await repo.record_submission(
        db, task_id=task_id, generation=2, sha=_GEN2_SHA, base_branch="develop"
    )
    for generation in reviewed:
        await repo.insert_machine_review(
            db,
            task_id=task_id,
            submission_generation=generation,
            findings_confirmed=json.dumps(
                [{"title": f"finding of gen{generation}", "file": _GEN2_FILE}]
            ),
            incomplete=False,
        )
    await db.execute(
        "UPDATE tasks SET submission_generation = 3 WHERE id = ?", (task_id,)
    )
    await db.commit()
    return task_id


async def _subject_card(client: AsyncClient, task_id: int) -> str:
    data = (await client.get(f"/api/tasks/{task_id}")).json()
    cards = [
        u["content"] for u in data["updates"] or [] if "Предмет ревью" in u["content"]
    ]
    assert cards, "the dispatch card must state the subject"
    return cards[-1]


async def test_delta_base_skips_generation_without_recorded_review(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#1400): gen2 has no recorded report, so the delta of gen3 starts
    # at gen1 — the last generation a review actually covered — and carries
    # gen2's fixes. The card names the base and why it skipped a generation,
    # and the profile is bought for the WIDENED delta: gen2's process surface
    # buys deep even though gen3 alone is a test tweak.
    recorder = _DispatchRecorder({"agent": {"id": "bc-g1"}, "run": {"id": "r-g1"}})
    _wire(monkeypatch, recorder)
    # The widened delta is a few lines: the small-resubmission rule (#1416)
    # would make it lite whatever it holds. What is witnessed here is WHICH
    # delta the rule of #820 reads, so the size rule is switched off.
    monkeypatch.setattr(config, "REVIEW_SMALL_DELTA_LINES", "0")
    task_id = await _third_generation(client, db, "spike-skip-gen2", reviewed=(1,))
    plugins.git_ops = _ThreeGenerationsGitOps(_TIP, ["docs/notes.md"])

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert f"'{_GEN2_FILE}'" in prompt, "gen2's fixes are in the subject"
    assert f"'{_GEN3_FILE}'" in prompt
    assert "прочитана ДЕЛЬТА" in prompt
    card = await _subject_card(client, task_id)
    assert "дельта к поколению #1" in card, card
    assert "отчёт по #2 не записан" in card, card
    assert "finding of gen1" in prompt, "the base review's findings travel along"
    assert (await _dispatch_row(db, task_id))["profile"] == "deep", (
        "the profile is judged on the widened delta, not on gen3 alone"
    )


async def test_delta_without_any_recorded_review_reads_whole_diff(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#1400): no earlier generation has a recorded report, so there is
    # no point a delta could honestly start from — the whole diff, named.
    recorder = _DispatchRecorder({"agent": {"id": "bc-g2"}, "run": {"id": "r-g2"}})
    _wire(monkeypatch, recorder)
    task_id = await _third_generation(client, db, "spike-no-review", reviewed=())
    plugins.git_ops = _ThreeGenerationsGitOps(_TIP, ["docs/notes.md"])

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert "прочитан ВЕСЬ дифф" in prompt
    assert "прочитана ДЕЛЬТА" not in prompt
    card = await _subject_card(client, task_id)
    assert "ни по одному прежнему поколению" in card and "не записан" in card, card


@pytest.mark.parametrize(
    "extra",
    [{"incomplete": True}, {"incomplete": False, "self_reviewed": True}],
    ids=["incomplete", "self_reviewed"],
)
async def test_an_uncounted_report_is_not_a_delta_base(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, extra
):
    # #1400, finding 452b39e570a0620b: gen2's only report is incomplete, or
    # the author's own. Neither is reading the code — the same rule as the
    # second-read guard (CODE_READ) — so the delta still starts at gen1,
    # carries gen2's files and gen1's findings, and the card says why.
    recorder = _DispatchRecorder({"agent": {"id": "bc-g4"}, "run": {"id": "r-g4"}})
    _wire(monkeypatch, recorder)
    task_id = await _third_generation(
        client, db, "spike-uncounted", reviewed=(1,), uncounted={2: extra}
    )
    plugins.git_ops = _ThreeGenerationsGitOps(_TIP, ["docs/notes.md"])

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert f"'{_GEN2_FILE}'" in prompt, "gen2's unread fixes stay in the subject"
    assert "finding of gen1" in prompt, "findings come from the base that was read"
    card = await _subject_card(client, task_id)
    assert "дельта к поколению #1" in card, card
    assert "отчёт по #2 неполный или самоотчёт" in card, card


@pytest.mark.parametrize(
    "extra",
    [{"incomplete": True}, {"incomplete": False, "self_reviewed": True}],
    ids=["incomplete", "self_reviewed"],
)
async def test_only_uncounted_reports_read_the_whole_diff(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, extra
):
    # #1400: when every earlier report is incomplete or self-written, no
    # generation was read — the whole diff, not a delta to one of them.
    recorder = _DispatchRecorder({"agent": {"id": "bc-g5"}, "run": {"id": "r-g5"}})
    _wire(monkeypatch, recorder)
    task_id = await _third_generation(
        client, db, "spike-only-uncounted", reviewed=(), uncounted={1: extra, 2: extra}
    )
    plugins.git_ops = _ThreeGenerationsGitOps(_TIP, ["docs/notes.md"])

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert "прочитан ВЕСЬ дифф" in prompt
    assert "прочитана ДЕЛЬТА" not in prompt


async def test_delta_base_is_previous_generation_when_its_review_is_recorded(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#1400): when gen2's report IS recorded nothing changes — the delta
    # is to gen2, narrow, with no skip named, and gen3's test tweak stays lite.
    recorder = _DispatchRecorder({"agent": {"id": "bc-g3"}, "run": {"id": "r-g3"}})
    _wire(monkeypatch, recorder)
    task_id = await _third_generation(
        client, db, "spike-gen2-reviewed", reviewed=(1, 2)
    )
    plugins.git_ops = _ThreeGenerationsGitOps(_TIP, ["docs/notes.md"])

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert f"'{_GEN3_FILE}'" in prompt
    assert f"'{_GEN2_FILE}'" not in prompt, "gen2 was reviewed, its files stay out"
    card = await _subject_card(client, task_id)
    assert "дельта к поколению #2" in card, card
    assert "не записан" not in card, card
    assert (await _dispatch_row(db, task_id))["profile"] == "lite"


# --- The delta is split by AUTHORSHIP, not trimmed (#1249) -------------------


async def test_a_surface_that_came_with_the_base_does_not_raise_the_profile(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#1249): the deep harness is bought for the AUTHOR's change. A
    # process surface that arrived with the base branch is somebody else's
    # work — it has already passed its own gate — and buying ~0.6M extra
    # provider tokens on its account is paying twice for one review.
    #
    # MUTATION that must kill this test: return the whole delta as the
    # author's work from generation_delta. Then the workspace_path line in
    # the file the base brought decides, and the profile goes deep.
    recorder = _DispatchRecorder({"agent": {"id": "bc-o1"}, "run": {"id": "r-o1"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-origin-base")
    plugins.git_ops = _AncestryGitOps(
        _TIP, ["docs/notes.md"], delta=_MERGED_DELTA, own=_OWN_PLAIN
    )

    assert await maybe_dispatch_review(db, task_id)

    assert (await _dispatch_row(db, task_id))["profile"] == "lite", (
        "a marker only the base brought must not buy the expensive profile"
    )
    data = (await client.get(f"/api/tasks/{task_id}")).json()
    notes = [u["content"] for u in data["updates"] or [] if "профиль" in u["content"]]
    assert "workspace_path" not in notes[-1], (
        "the reason must not quote a marker the author never wrote"
    )


async def test_a_surface_written_by_the_author_still_raises_the_profile(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#1249): the distinction sharpens the rule's INPUT, it does not
    # weaken the rule. The same marker, written by the author this round,
    # still buys the deep harness and still says which surface bought it.
    recorder = _DispatchRecorder({"agent": {"id": "bc-o2"}, "run": {"id": "r-o2"}})
    _wire(monkeypatch, recorder)
    # One author line: the small-resubmission rule (#1416) would buy lite on
    # size alone. The surface rule is what is witnessed, so size is off here.
    monkeypatch.setattr(config, "REVIEW_SMALL_DELTA_LINES", "0")
    task_id = await _second_generation(client, db, "spike-origin-author")
    plugins.git_ops = _AncestryGitOps(
        _TIP, ["docs/notes.md"], delta=_MERGED_DELTA, own=_OWN_WITH_SURFACE
    )

    assert await maybe_dispatch_review(db, task_id)

    assert (await _dispatch_row(db, task_id))["profile"] == "deep"
    data = (await client.get(f"/api/tasks/{task_id}")).json()
    notes = [
        u["content"] for u in data["updates"] or [] if "профиль deep" in u["content"]
    ]
    assert notes and "workspace_path" in notes[-1], (
        "the surface the author wrote still names itself in the card"
    )


async def test_the_subject_names_both_halves(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#1249): what came with the base does not disappear from sight.
    # #1238 found a defect that exists ONLY where two branches meet; a
    # reviewer who never learns the base moved cannot look for one. So the
    # card carries BOTH numbers and the prompt names the foreign files.
    recorder = _DispatchRecorder({"agent": {"id": "bc-o3"}, "run": {"id": "r-o3"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-origin-both")
    plugins.git_ops = _AncestryGitOps(
        _TIP, ["docs/notes.md"], delta=_MERGED_DELTA, own=_OWN_PLAIN
    )

    assert await maybe_dispatch_review(db, task_id)

    data = (await client.get(f"/api/tasks/{task_id}")).json()
    cards = [
        u["content"] for u in data["updates"] or [] if "Предмет ревью" in u["content"]
    ]
    assert cards, "the dispatch card must state the subject"
    assert "1 файл(ов) автора и 1 привезено базой" in cards[-1], (
        "both halves are counted where a person reads the card"
    )
    prompt = recorder.calls[-1]["prompt_text"]
    assert "ПРИВЕЗЕНО БАЗОЙ" in prompt and _BASE_FILE in prompt, (
        "the foreign half is named to the reviewer, not removed"
    )
    assert f"'{_AUTHOR_FILE}'" in prompt, "the command narrows to the author's file"
    assert f"'{_BASE_FILE}'" not in prompt, (
        "the command itself is the author's work; the base half is named beside it"
    )


async def test_unknown_origin_reads_everything_and_says_so(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#1249): a clone that cannot answer "whose commit is this" gets no
    # split at all. Everything in the delta is read, exactly as before, and
    # the reason is named — a silently narrowed subject is worse than a wide
    # one, because the report would claim a coverage nobody had.
    recorder = _DispatchRecorder({"agent": {"id": "bc-o4"}, "run": {"id": "r-o4"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-origin-unknown")
    plugins.git_ops = _AncestryGitOps(
        _TIP, ["docs/notes.md"], delta=_MERGED_DELTA, own=None
    )

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert f"'{_AUTHOR_FILE}'" in prompt and f"'{_BASE_FILE}'" in prompt, (
        "nothing is dropped when origin could not be established"
    )
    data = (await client.get(f"/api/tasks/{task_id}")).json()
    cards = [
        u["content"] for u in data["updates"] or [] if "Предмет ревью" in u["content"]
    ]
    assert "происхождение коммитов установить не удалось" in cards[-1], (
        "the wider subject states its cause instead of being inferred"
    )
    # "as before" is asserted against the rule itself, not against a literal:
    # with no split the profile is whatever the whole submitted diff buys.
    row = dict(await repo.get_task(db, task_id))
    assert (await _dispatch_row(db, task_id))["profile"] == (
        pick_review_profile(row, _HARMLESS_DIFF)[0]
    ), "an unanswerable origin changes the subject's width, not the rule"


async def test_an_empty_brought_half_claims_only_what_was_measured(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Карточка называет измеренное, а не вывод из него (6ea44f6f, 21ad18b5).

    Пустая привезённая половина НЕ значит «база не двигалась». Спрошена
    достижимость, и тот же пустой набор даёт ещё два случая: база влита
    сжатием — её коммиты не предки по хешу (#1240) — и полное перекрытие
    путей, когда автор тронул ровно те же файлы, что приехали, а разбор идёт
    по путям. Оба раза база двигалась, а прежняя формулировка это отрицала.

    Здесь воспроизведено полное перекрытие: автор сам написал ОБА файла
    дельты, привезённого сверх них нет.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-o6"}, "run": {"id": "r-o6"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-origin-overlap")
    plugins.git_ops = _AncestryGitOps(
        _TIP, ["docs/notes.md"], delta=_MERGED_DELTA, own=_MERGED_DELTA
    )

    assert await maybe_dispatch_review(db, task_id)

    data = (await client.get(f"/api/tasks/{task_id}")).json()
    cards = [
        u["content"] for u in data["updates"] or [] if "Предмет ревью" in u["content"]
    ]
    assert "файлов только из базы нет" in cards[-1], (
        "измерено отсутствие ФАЙЛОВ чужого происхождения, не покой базы"
    )
    assert "база не двигалась" not in cards[-1], (
        "про саму базу разбор по достижимости путей ничего не знает"
    )


async def test_unknown_origin_still_lets_the_submitted_diff_buy_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Вторая половина AC-4, которой не было (находка b4bf6acce462a2a6).

    Сосед выше сверяет профиль с ``pick_review_profile(row, _HARMLESS_DIFF)``,
    а безобидный дифф даёт ``lite`` — ровно столько же, сколько ПУСТАЯ
    авторская дельта. Поэтому мутация, снимающая запасной вариант
    (``profile_diff = subject.author_diff`` без ``or diff``), оставляла тот
    assert зелёным: обе стороны равенства сходились на ``lite``.

    Здесь дифф сдачи сам по себе стоит ``deep`` — в нём процессная поверхность
    автора, — а происхождение коммитов не установлено. Если запасной вариант
    сломать, профиль посчитается по пустой строке и упадёт до ``lite``:
    неустановленное происхождение расширяет предмет, но правило не смягчает.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-o5"}, "run": {"id": "r-o5"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-origin-fallback")
    plugins.git_ops = _AncestryGitOps(
        _TIP,
        ["docs/notes.md"],
        diff=_OWN_WITH_SURFACE,
        delta=_MERGED_DELTA,
        own=None,
    )

    assert await maybe_dispatch_review(db, task_id)

    row = dict(await repo.get_task(db, task_id))
    assert pick_review_profile(row, _OWN_WITH_SURFACE)[0] == "deep", (
        "предпосылка теста: дифф сдачи сам по себе покупает дорогой профиль"
    )
    assert (await _dispatch_row(db, task_id))["profile"] == "deep", (
        "происхождение неизвестно — решает весь дифф сдачи, как и раньше, "
        "а не пустая авторская дельта"
    )


async def test_an_empty_brought_half_names_the_squash_case(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Сжатие базы названо в карточке, а не только в комментарии кода (#1240).

    Постановка #1249 требует: на форже, где доставка идёт сжатием, коммиты
    базы не предки по хешу, и этот случай «должен быть назван, а не пройти
    мимо». Разбор по достижимости тогда целиком отдаёт привезённое автору —
    промах в безопасную сторону, но человек, читающий карточку, должен знать,
    что «файлов только из базы нет» может означать и это.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-o7"}, "run": {"id": "r-o7"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-origin-squash")
    plugins.git_ops = _AncestryGitOps(
        _TIP, ["docs/notes.md"], delta=_MERGED_DELTA, own=_MERGED_DELTA
    )

    assert await maybe_dispatch_review(db, task_id)

    data = (await client.get(f"/api/tasks/{task_id}")).json()
    cards = [
        u["content"] for u in data["updates"] or [] if "Предмет ревью" in u["content"]
    ]
    assert "сжатием" in cards[-1] and "#1240" in cards[-1], (
        "случай сжатия назван там, где его читает человек"
    )


async def test_a_bare_merge_of_the_base_reads_the_whole_delta_and_says_why(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Круг без своей правки — голый мерж базы (#1249).

    Сузить предмет до нуля файлов значило бы выдать ревьюеру пустую команду.
    Поэтому читается вся дельта, а карточка говорит, почему она такая
    широкая: всё привезено базой, своей правки в этом круге нет.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-o8"}, "run": {"id": "r-o8"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-origin-bare-merge")
    plugins.git_ops = _AncestryGitOps(
        _TIP, ["docs/notes.md"], delta=_MERGED_DELTA, own=""
    )

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert f"'{_AUTHOR_FILE}'" in prompt and f"'{_BASE_FILE}'" in prompt, (
        "голый мерж не сужает предмет до пустой команды"
    )
    data = (await client.get(f"/api/tasks/{task_id}")).json()
    cards = [
        u["content"] for u in data["updates"] or [] if "Предмет ревью" in u["content"]
    ]
    assert "2 файл(ов), все привезены базой" in cards[-1], (
        "ширина предмета объяснена числом и причиной"
    )
    assert "своей правки в этом круге нет" in cards[-1]


async def test_generation_delta_splits_origin_with_real_git(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Место вызова ``delta_without_base`` — настоящим git (находка #1249).

    Все AC диспатча подменяют git двойником, который отвечает вложенное и
    не смотрит на аргументы. Перестановка ``prev``/``current`` на месте
    вызова тогда невидима. Здесь ``generation_delta`` идёт по клону, собранному
    настоящим git: автор сдал ``mine.py``, слил базу с чужим ``theirs.py`` и
    дописал второй круг. Переставь концы — ``git log prev --not current``
    пуст, и разбор скажет «всё привезено базой».
    """
    from hub.integrations.git_ops import GitOpsIntegration
    from hub.services import review_dispatch as rd
    from tests.test_git_ops import _delta_clone

    clone, prev, current = _delta_clone(tmp_path)
    task_id = await _second_generation(client, db, "spike-origin-real-git")
    await repo.record_submission(
        db, task_id=task_id, generation=1, sha=prev, base_branch="develop"
    )
    await db.execute(
        "UPDATE tasks SET submission_sha = ? WHERE id = ?", (current, task_id)
    )
    await db.commit()

    async def _clone_context(_db, _task_id):
        return str(clone), "develop"

    monkeypatch.setattr(rd, "_git_context", _clone_context)
    plugins.git_ops = GitOpsIntegration()
    task = dict(await repo.get_task(db, task_id))

    subject = await rd.generation_delta(db, task, "develop")

    assert subject.paths == ["mine.py"], subject.note
    assert subject.base_paths == ["theirs.py"], subject.note
    assert "починка второго круга" in subject.author_diff
    assert "1 файл(ов) автора и 1 привезено базой" in subject.note


async def test_previous_findings_travel_with_the_delta(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # The delta tells the reviewer to skip files it did not touch — so what the
    # last run confirmed has to arrive with it, or the fixes go unchecked.
    recorder = _DispatchRecorder({"agent": {"id": "bc-f"}, "run": {"id": "r-f"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-prior")
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        harness_skill="lite-diff-review",
        raw_count=1,
        findings_confirmed=json.dumps(
            [{"title": "race on retry", "severity": "high", "file": "hub/a.py"}]
        ),
        incomplete=False,
    )
    await db.commit()
    plugins.git_ops = _AncestryGitOps(_TIP, ["docs/notes.md"], delta=_DELTA)

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert "НА ПРОШЛОМ ПОКОЛЕНИИ БЫЛИ ПОДТВЕРЖДЕНЫ" in prompt
    assert "hub/a.py: race on retry" in prompt
    assert "не считай их закрытыми по факту" in prompt


async def test_reviewer_is_told_which_findings_are_deferred_and_to_what(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1448 AC-3: отложенная находка доезжает до ревьюера с задачей.

    Без этого ревьюер не отличает осознанную отсрочку от открытой находки и
    подтверждает её на каждом поколении. Парой идёт исправленная находка:
    её по-прежнему надо проверить на фикс, и смешивать два списка нельзя.
    """
    from hub.services.finding_identity import finding_uids

    recorder = _DispatchRecorder({"agent": {"id": "bc-d"}, "run": {"id": "r-d"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-prior-deferred")
    deferred = {
        "title": "orchestrator not called from CLI",
        "severity": "low",
        "file": "hub/a.py",
    }
    fixed = {"title": "race on retry", "severity": "high", "file": "hub/b.py"}
    follow_up = await repo.create_task(
        db,
        title="сюда отложено",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="open",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    review_id = await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        harness_skill="lite-diff-review",
        raw_count=2,
        findings_confirmed=json.dumps([deferred, fixed]),
        incomplete=False,
    )
    for finding, outcome, linked in (
        (deferred, "deferred", follow_up),
        (fixed, "fixed", None),
    ):
        await repo.upsert_finding_outcome(
            db,
            review_id=review_id,
            task_id=task_id,
            submission_generation=1,
            finding_uid=finding_uids([finding])[0],
            finding_index=0,
            finding_title=finding["title"],
            outcome=outcome,
            note="",
            linked_task_id=linked,
            reported_by="pda_claude",
        )
    await db.commit()
    plugins.git_ops = _AncestryGitOps(_TIP, ["docs/notes.md"], delta=_DELTA)

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    confirmed_line = next(
        line for line in prompt.splitlines() if "БЫЛИ ПОДТВЕРЖДЕНЫ" in line
    )
    assert "hub/b.py: race on retry" in confirmed_line, "фикс проверяют как прежде"
    assert "orchestrator not called from CLI" not in confirmed_line, (
        "отложенная не должна читаться как находка, которую надо проверить на фикс"
    )
    deferred_line = next(
        line for line in prompt.splitlines() if "orchestrator not called" in line
    )
    assert f"отложена до #{follow_up}" in deferred_line
    assert "подтверждай" in deferred_line and "изменился" in deferred_line


async def test_a_finding_confirmed_by_two_reports_of_one_generation_is_listed_once(
    client: AsyncClient, db: aiosqlite.Connection
):
    """#1448 (deep #740, ab7f32e95620d2b3): добор лестницы даёт два отчёта на поколение.

    Одна и та же находка в обоих отчётах получала во втором ординал-близнеца,
    то есть другой uid без исхода, и попадала в открытые рядом с отложенной.
    Отложенная побеждает, дубля нет ни в одном из списков.
    """
    from hub.services.finding_identity import finding_uids
    from hub.services.review_dispatch import previous_findings

    task_id = await _second_generation(client, db, "spike-prior-two-reports")
    deferred = {"title": "deferred once", "severity": "low", "file": "hub/a.py"}
    other = {"title": "open once", "severity": "high", "file": "hub/b.py"}
    follow_up = await repo.create_task(
        db,
        title="сюда отложено",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="open",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    first = 0
    for findings in ([deferred, other], [deferred]):
        rid = await repo.insert_machine_review(
            db,
            task_id=task_id,
            submission_generation=1,
            raw_count=2,
            findings_confirmed=json.dumps(findings),
            incomplete=False,
        )
        first = first or rid
    await repo.upsert_finding_outcome(
        db,
        review_id=first,
        task_id=task_id,
        submission_generation=1,
        finding_uid=finding_uids([deferred])[0],
        finding_index=0,
        finding_title=deferred["title"],
        outcome="deferred",
        note="",
        linked_task_id=follow_up,
        reported_by="pda_claude",
    )
    await db.commit()

    open_titles, deferred_titles = await previous_findings(db, task_id, 2)

    assert open_titles == ["hub/b.py: open once"]
    assert deferred_titles == [f"hub/a.py: deferred once (отложена до #{follow_up})"]


def test_instance_base_url_never_names_the_vendor_host(monkeypatch):
    """#1005: this hub answers with its own address, never with the authors'.

    The fallback that used to sit here could not fire — ``hub_base_url``
    always answers, if only with ``http://HOST:PORT`` — so a constant naming
    the vendor's server was dead code that would have become a live default
    the moment the branch above it changed. The reviewer agent is sent to the
    URL this function returns, and sending somebody else's reviewer to our hub
    is the failure this test exists to prevent.
    """
    from hub.services.review_dispatch import instance_base_url

    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.example.test")
    assert instance_base_url() == "https://hub.example.test"

    monkeypatch.delenv("HAIPLANE_HUB_URL", raising=False)
    resolved = instance_base_url()
    assert resolved, "an unconfigured hub still answers with its own bind address"

    # The half that actually discriminates. Everything above stays green on the
    # pre-fix line as well: ``hub_base_url`` never returns an empty string —
    # HUB_HOST/HUB_PORT are resolved at import and always compose one — so the
    # ``or "<vendor host>"`` branch could not fire and both implementations
    # answered identically. Only an echo that carries NO base_url at all
    # reaches the branch the fix removed, and there the old code named the
    # authors' server. Mutating this module back to the pre-fix line must turn
    # this assertion red; if it does not, the test has stopped guarding #1005.
    import hub.hub_instance as hub_instance

    monkeypatch.setattr(
        hub_instance,
        "instance_echo_fields",
        lambda: {"instance": "local", "server_id": "somebody-elses-box"},
    )
    assert instance_base_url() == ""


# --- Report ↔ dispatch identity (#1025) --------------------------------------
#
# The sweep used to accept ANY report of the right task+generation as the
# dispatched run's own: the author's parallel report closed the hub's dispatch
# as done, silenced the no-report alert and fed the author's numbers to the
# usage cross-check (#1011 gen 1). The dispatch now pins the reviewer
# principal from the TOKEN and matches reports on it — rung by rung within
# the ladder; a dispatch without a pin (history, unresolved token, open mode)
# keeps the old rule.
#
# Auth is real in these tests (open mode off): the pin resolves only where
# intake can read the bearer — in open mode every report lands with
# principal_id NULL, so pinning there would make the dispatch unsatisfiable.


_FOREIGN_REPORT = {
    "harness_skill": "lite-diff-review",
    "raw_count": 1,
    "findings_confirmed": [],
    "findings_rejected": [{"title": "x", "category": "correctness", "reason": "no"}],
    "incomplete": False,
    "unresolved": [],
    "lost_dimensions": [],
    "agent": "author-run-harness",
    "model": "claude-fable-5",
    "tokens_spent": 1000,
}


async def test_foreign_report_does_not_settle_dispatch(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#1025): a foreign report of the right generation leaves the
    # dispatch waiting, skips the usage cross-check and is named in the feed
    # exactly once.
    recorder = _DispatchRecorder({"agent": {"id": "bc-f1"}, "run": {"id": "run-f1"}})
    _wire(monkeypatch, recorder)
    expected_pid, _, author_token = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(
        client, db, "spike-foreign", policy={"review": "dispatch"}
    )
    assert (await _dispatch_row(db, task_id))["reviewer_principal_id"] == expected_pid

    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=_FOREIGN_REPORT,
        headers={"Authorization": f"Bearer {author_token}"},
    )
    assert resp.status_code == 200, resp.text

    usage_calls: list[str] = []

    async def _usage(agent_id, run_id=None):
        usage_calls.append(agent_id)
        return {"totalUsage": {"totalTokens": 100_000}}

    async def _running(agent_id, run_id):
        return {"id": run_id, "status": "RUNNING"}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)
    monkeypatch.setattr(cursor_cloud, "get_run", _running)

    await sweep_review_dispatches(db)
    assert await repo.list_active_review_dispatches(db), "dispatch keeps waiting"
    assert not usage_calls, "a foreign report must not feed the cross-check"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert len([u for u in updates if "другого принципала" in u["content"]]) == 1, (
        "the collision is named once, at intake"
    )
    assert not [u for u in updates if "расходится с данными провайдера" in u["content"]]


async def test_own_report_settles_dispatch(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#1025): the pinned principal's report settles the dispatch, goes
    # through the usage cross-check, and the provider's bill lands on THAT
    # report's row — with no collision noise in the feed.
    recorder = _DispatchRecorder({"agent": {"id": "bc-f2"}, "run": {"id": "run-f2"}})
    _wire(monkeypatch, recorder)
    expected_pid, reviewer_token, _ = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "spike-own", policy={"review": "dispatch"})

    own = dict(_FOREIGN_REPORT, agent="cloud-reviewer", model="grok-4.6")
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=own,
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )
    assert resp.status_code == 200, resp.text
    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["principal_id"] == expected_pid, "owner recorded from the token"

    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 100_000}}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)

    await sweep_review_dispatches(db)
    assert not await repo.list_active_review_dispatches(db), "dispatch settles"
    stamped = dict(await repo.get_latest_machine_review(db, task_id))
    assert stamped["provider_tokens"] == 100_000, "the bill lands on the matched row"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert [u for u in updates if "расходится с данными провайдера" in u["content"]]
    assert not [u for u in updates if "другого принципала" in u["content"]]
    assert not [u for u in updates if "Второй отчёт" in u["content"]], (
        "the dispatched run's own report must pass without ceremony"
    )


async def test_grace_alert_fires_despite_foreign_report(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#1025): the exact #1011 shape — terminal run, only a foreign
    # report, grace expired. The safety net must fire instead of being
    # silenced by the foreign report.
    recorder = _DispatchRecorder({"agent": {"id": "bc-f3"}, "run": {"id": "run-f3"}})
    _wire(monkeypatch, recorder)
    _, _, author_token = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "spike-grace", policy={"review": "dispatch"})

    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=_FOREIGN_REPORT,
        headers={"Authorization": f"Bearer {author_token}"},
    )
    assert resp.status_code == 200, resp.text
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _finished(agent_id, run_id):
        return {"id": run_id, "status": "FINISHED"}

    monkeypatch.setattr(cursor_cloud, "get_run", _finished)

    await sweep_review_dispatches(db)
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert len([u for u in updates if "отчёт НЕ сдан" in u["content"]]) == 1
    assert not await repo.list_active_review_dispatches(db)


async def test_top_up_ignores_foreign_report(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#1025): a foreign incomplete report buys no top-up; the pinned
    # run's own incomplete report still does — and the deep rung then waits
    # for ITS OWN report instead of settling on the lite one.
    recorder = _DispatchRecorder({"agent": {"id": "bc-f4"}, "run": {"id": "run-f4"}})
    _wire(monkeypatch, recorder)
    _, reviewer_token, author_token = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "spike-topup", policy={"review": "dispatch"})
    assert len(recorder.calls) == 1

    foreign = dict(_FOREIGN_REPORT, incomplete=True)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=foreign,
        headers={"Authorization": f"Bearer {author_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert len(recorder.calls) == 1, "no ladder step on a foreign declaration"

    own = dict(
        _FOREIGN_REPORT, incomplete=True, agent="cloud-reviewer", model="grok-4.6"
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=own,
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert len(recorder.calls) == 2, "our own incomplete run buys the deep top-up"

    # Both rungs pin the same principal. The lite report must settle only
    # the lite rung — before rung-ordered matching the SAME report settled
    # the deep dispatch too, before its run ever reported (#1025 review).
    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 100_000}}

    async def _running(agent_id, run_id):
        return {"id": run_id, "status": "RUNNING"}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)
    monkeypatch.setattr(cursor_cloud, "get_run", _running)

    await sweep_review_dispatches(db)
    active = [dict(r) for r in await repo.list_active_review_dispatches(db)]
    assert len(active) == 1, "the deep rung keeps waiting for its own report"
    settled = await repo.get_settled_review_dispatch(db, task_id, 1)
    assert settled is not None and int(dict(settled)["id"]) != int(active[0]["id"])


async def test_open_mode_pins_nothing(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#1029): open mode never reads the bearer, so every report lands
    # with principal_id NULL. Pinning a dispatch there would make it
    # unsatisfiable — its own reviewer's report would be flagged foreign and
    # the dispatch would die by a FALSE "отчёт НЕ сдан". So no pin is taken,
    # and the old task+generation rule stands: degradation must never be
    # worse than the behaviour it degrades from.
    recorder = _DispatchRecorder({"agent": {"id": "bc-om"}, "run": {"id": "run-om"}})
    _wire(monkeypatch, recorder)
    # A resolvable reviewer key EXISTS in the db — the only thing missing is
    # an auth mode that would let a report carry its owner.
    _, reviewer_token = await _agent_key(db, "cloud-reviewer")
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", reviewer_token)
    assert hub_auth._is_open_mode(), "the fixture client runs in open mode"

    task_id = await _submitted(
        client, db, "spike-openmode", policy={"review": "dispatch"}
    )
    dispatch = await _dispatch_row(db, task_id)
    assert dispatch["reviewer_principal_id"] is None, "no pin under open mode"

    # And the legacy rule still settles the dispatch: any report of the
    # generation counts, exactly as before #1025.
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review", json=_FOREIGN_REPORT
    )
    assert resp.status_code == 200, resp.text

    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 100_000}}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)

    await sweep_review_dispatches(db)
    assert not await repo.list_active_review_dispatches(db), "legacy rule settles"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert not [u for u in updates if "другого принципала" in u["content"]], (
        "an unpinned dispatch must not accuse anyone of being foreign"
    )


async def test_report_recovered_from_run_result(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#1036): a finished run with no contract report, but a valid block
    # in its text — the report is stored, marked as transcribed, and the
    # dispatch settles instead of failing.
    recorder = _DispatchRecorder({"agent": {"id": "bc-r1"}, "run": {"id": "run-r1"}})
    _wire(monkeypatch, recorder)
    reviewer_pid, _, _ = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "spike-rec", policy={"review": "dispatch"})
    await _expire_grace(db)
    await _finished_run_with(monkeypatch, _report_block())

    await sweep_review_dispatches(db)

    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["raw_count"] == 2
    assert saved["principal_id"] == reviewer_pid, "owner is the dispatch's reviewer"
    assert saved["orchestrator"].startswith("cursor-cloud-result"), (
        "origin must live in the data, not only in a feed line"
    )
    assert not await repo.list_active_review_dispatches(db), "dispatch settles"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert [u for u in updates if "восстановлен из текста" in u["content"]]
    assert not [u for u in updates if "отчёт НЕ сдан" in u["content"]]


async def test_unparsable_result_is_kept_as_text_not_invented(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#1036): prose without a block stores NOTHING. The text survives in
    # the feed and the dispatch fails exactly as before — "could not read" must
    # never become "read and clean".
    recorder = _DispatchRecorder({"agent": {"id": "bc-r2"}, "run": {"id": "run-r2"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "spike-prose", policy={"review": "dispatch"})
    await _expire_grace(db)
    await _finished_run_with(
        monkeypatch,
        "Нашёл две проблемы: гонка в ретраях и незакрытый файл. Отчёт сдать "
        "не смог, MCP не примонтирован.",
    )

    await sweep_review_dispatches(db)

    assert await repo.get_latest_machine_review(db, task_id) is None, (
        "findings must never be invented out of prose"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert [u for u in updates if "нет разбираемого блока" in u["content"]]
    assert [u for u in updates if "отчёт НЕ сдан" in u["content"]]
    assert not await repo.list_active_review_dispatches(db), (
        "the dispatch still fails — nothing was recovered"
    )


async def test_mcp_report_wins_over_result_fallback(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#1036): when the contract path worked, the text is not read at all
    # — one submission must not end up with two reports.
    recorder = _DispatchRecorder({"agent": {"id": "bc-r3"}, "run": {"id": "run-r3"}})
    _wire(monkeypatch, recorder)
    _, reviewer_token, _ = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "spike-both", policy={"review": "dispatch"})

    own = dict(_FOREIGN_REPORT, agent="cloud-reviewer", model="grok-4.6")
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=own,
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )
    assert resp.status_code == 200, resp.text
    await _expire_grace(db)
    await _finished_run_with(monkeypatch, _report_block(raw_count=99))

    await sweep_review_dispatches(db)

    rows = await repo.machine_reviews_of_generation(db, task_id, 1)
    assert len(rows) == 1, "the contract report stands alone"
    assert dict(rows[0])["raw_count"] != 99


async def test_recovered_report_settles_its_own_dispatch(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#1036): the transcribed report must read as the dispatch's OWN on
    # the next pass too — a foreign-looking one would raise the false "report
    # not submitted" alert this area already fixed once (#1025).
    recorder = _DispatchRecorder({"agent": {"id": "bc-r4"}, "run": {"id": "run-r4"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "spike-own2", policy={"review": "dispatch"})
    await _expire_grace(db)
    await _finished_run_with(monkeypatch, _report_block())

    await sweep_review_dispatches(db)
    await sweep_review_dispatches(db)  # second pass must find nothing to do

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert not [u for u in updates if "отчёт НЕ сдан" in u["content"]]
    assert not [u for u in updates if "другого принципала" in u["content"]]
    assert len([u for u in updates if "восстановлен из текста" in u["content"]]) == 1


def test_pick_review_model_stays_within_available_models(monkeypatch):
    # AC-5 (#1036): every preference must be a model the account can actually
    # launch. Before this, a grok implementer sent the diversity rule to
    # gpt-5.3-codex, which refuses at creation with usage_limit_exceeded.
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL", "")
    available = {"grok-4.6", "grok-4.5", "composer-2.5"}

    assert set(_REVIEW_MODEL_PREFERENCES) <= available
    assert pick_review_model("claude-opus-5") in available
    grok_reviewer = pick_review_model("grok-4.6")
    assert grok_reviewer in available
    assert family(grok_reviewer) != family("grok-4.6"), "diversity still holds"
