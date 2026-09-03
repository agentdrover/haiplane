"""Гейты сдачи и вердикта — объявленный список (#1067, эпик #1064).

До этой задачи ``submit_for_review`` была цепочкой if-ов на 538 строк при
цикломатике 46, и правка любого гейта была правкой этой функции целиком.
Здесь проверяется не то, что список красив, а четыре вещи, каждая из которых
может тихо сломаться при переносе:

* отказ не изменился — тот же код, то же машинное имя, тот же текст подсказки;
* добавить гейт можно записью в список, не трогая функцию перехода;
* режим ``warn`` оставляет заметку, ``require`` отказывает — как и раньше;
* наборы шагов pair-пути и headless-пути объявлены и сопоставлены.

Последнее — самое важное из найденного этой задачей, и оно не про форму.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from hub import models
from hub.services import lifecycle
from hub.services.gate_pipeline import ALWAYS, OFF, Step, run_steps


# --------------------------------------------------------------------------
# Механизм
# --------------------------------------------------------------------------


async def test_a_new_gate_is_a_list_entry_not_a_branch():
    """AC-2: гейт добавляется записью в список, функция перехода не правится.

    Ровно то, чего не было раньше: чтобы добавить проверку, приходилось
    вписывать блок в функцию, у которой уже было сорок шесть ветвлений.
    """
    seen: list[str] = []

    async def first(state):
        seen.append("first")

    async def added(state):
        seen.append("added")

    async def last(state):
        seen.append("last")

    steps = (Step("first", first), Step("added", added), Step("last", last))
    await run_steps(object(), steps)
    assert seen == ["first", "added", "last"], "порядок задаётся списком"


async def test_a_refusal_stops_the_pipeline():
    """Отказ прерывает прогон — это контракт, а не деталь.

    Дальше по списку идут сетевые резолвы (дифф ветки, её вершина,
    обнаружение PR). Гонять их ради отказа, который уже случился, значит
    платить за него временем и сетью; цепочка if-ов вела себя так же.
    """
    reached: list[str] = []

    async def refuses(state):
        raise HTTPException(422, "нет")

    async def never(state):
        reached.append("never")

    with pytest.raises(HTTPException):
        await run_steps(object(), (Step("refuses", refuses), Step("never", never)))
    assert reached == [], "шаг после отказа не должен выполняться"


async def test_an_off_policy_skips_the_step():
    """Политика ``off`` пропускает шаг целиком, остальные разбирает сам шаг."""
    ran: list[str] = []

    async def gated(state):
        ran.append("gated")

    await run_steps(object(), (Step("gated", gated, mode=lambda: OFF),))
    assert ran == []

    await run_steps(object(), (Step("gated", gated, mode=lambda: ALWAYS),))
    assert ran == ["gated"]


def test_every_step_declares_a_name_and_whether_it_refuses():
    """Список читают глазами чаще, чем правят: имя и намерение обязательны."""
    for step in lifecycle.SUBMIT_STEPS + lifecycle.VERDICT_STEPS:
        assert step.name and step.name == step.name.strip()
        assert isinstance(step.refuses, bool)
        assert step.describe()["name"] == step.name


# --------------------------------------------------------------------------
# Порядок: он был несущим и раньше, но держался тем, что никто не переставил
# блоки. Теперь его можно закрепить.
# --------------------------------------------------------------------------

EXPECTED_SUBMIT_ORDER = (
    "task_is_submittable",
    "canonical_branch",
    "branch_matches",
    "resolve_diff",
    "surfaces",
    "finding_outcomes",
    "submit_rules",
    "pin_submission_sha",
    "delivery_pr",
)

EXPECTED_VERDICT_ORDER = (
    "has_a_submission",
    "changes_requested_has_content",
    "verdict_matches_its_text",
    "verdict_is_not_a_repeat",
    "changes_requested_has_in_scope_finding",
    "machine_review_present",
    "ac_tests_green",
    "branch_tip_matches",
    "approval_blind_spots",
    "auto_draft_out_of_scope",
)


def test_the_submit_order_is_pinned():
    """AC-3 (порядок): перестановка краснит.

    Порядок load-bearing: дешёвые проверки до сетевых, отказ до записи.
    Раньше это держалось расположением блоков в функции.
    """
    assert tuple(s.name for s in lifecycle.SUBMIT_STEPS) == EXPECTED_SUBMIT_ORDER


def test_the_verdict_order_is_pinned():
    assert tuple(s.name for s in lifecycle.VERDICT_STEPS) == EXPECTED_VERDICT_ORDER


def test_the_network_walking_steps_come_last():
    """Сетевые шаги — после дешёвых отказов, иначе отказ оплачен сетью."""
    names = [s.name for s in lifecycle.SUBMIT_STEPS]
    refusing = [s.name for s in lifecycle.SUBMIT_STEPS if s.refuses]
    assert names.index("pin_submission_sha") > max(names.index(n) for n in refusing), (
        "пиннинг вершины ветки ходит в сеть и обязан идти после всех отказов"
    )
    assert names.index("delivery_pr") > names.index("pin_submission_sha")


# --------------------------------------------------------------------------
# Расхождение pair и headless — теперь по ДВУМ СПИСКАМ (#1122)
# --------------------------------------------------------------------------

EXPECTED_HEADLESS_ORDER = (
    "canonical_branch",
    "branch_matches",
    "resolve_diff",
    "surfaces",
    "finding_outcomes",
    "submit_rules",
    "pin_submission_sha",
    "delivery_pr",
)


def test_the_headless_order_is_pinned():
    assert tuple(s.name for s in lifecycle.HEADLESS_STEPS) == EXPECTED_HEADLESS_ORDER


def test_the_two_pipelines_are_compared_by_their_lists():
    """#1122 AC-2: расхождение читается из двух списков, а не из исходника.

    В #1067 сравнивать было не с чем — у headless-пути списка не было, и
    страж читал текст четырёх функций. Такой страж зеленел бы от переезда
    кода, а не от появления гейта; здесь он заменён на сопоставление.
    """
    submit = {s.name: s for s in lifecycle.SUBMIT_STEPS}
    headless = {s.name: s for s in lifecycle.HEADLESS_STEPS}

    # task_is_submittable — единственный шаг, которого у headless нет вовсе:
    # он проверяет, что задача pair и в статусе, из которого сдают.
    assert set(submit) - set(headless) == {"task_is_submittable"}
    assert set(headless) - set(submit) == set()

    active_here = {n for n, s in headless.items() if s.active}
    inactive_here = {n for n, s in headless.items() if not s.active}
    assert inactive_here == {"branch_matches", "finding_outcomes"}, (
        "набор неактивных на headless изменился — обновите матрицу решений в "
        "#1122 и скажите об этом в сдаче, а не молча"
    )
    assert "pin_submission_sha" in active_here, (
        "пиннинг коммита — то, ради чего #1122 заводилась: без него вердикт "
        "относится к номеру сдачи, а не к коду"
    )


def test_every_inactive_headless_step_explains_itself():
    """#1122 AC-3: объявленный и невыполняемый шаг обязан назвать причину.

    Иначе «не делаем» неотличимо от «забыли» — ровно то, из-за чего
    расхождение двух путей прожило незамеченным.
    """
    for step in lifecycle.HEADLESS_STEPS:
        if step.active:
            assert not step.inactive_reason
            continue
        assert len(step.inactive_reason) > 40, (
            f"шаг {step.name} объявлен неактивным без внятной причины"
        )
        assert step.describe()["inactive_reason"] == step.inactive_reason


async def test_an_inactive_step_never_runs():
    ran: list[str] = []

    async def never(state):
        ran.append("never")

    await run_steps(
        object(), (Step("x", never, inactive_reason="объявлен и намеренно не делаем"),)
    )
    assert ran == []


def test_the_headless_gates_never_refuse():
    """Отказ на headless оставил бы задачу стоять без человека рядом (#1122).

    Решение владельца — warn: поверхности и правила стоят под потолком, а
    остальные активные шаги не отказывают по своей природе.
    """
    for step in lifecycle.HEADLESS_STEPS:
        if not step.active:
            continue
        assert step.mode() in ("off", "warn", "always"), (
            f"шаг {step.name} на headless-пути может отказать: режим {step.mode()}"
        )


# --------------------------------------------------------------------------
# AC-1 и AC-3: отказы не изменились
# --------------------------------------------------------------------------


async def test_the_branch_gate_refuses_exactly_as_before(db):
    """AC-1: тот же код ответа, то же машинное имя, тот же текст подсказки."""
    task = await lifecycle.create_task(
        db, models.TaskCreate(title="ветка", source="agent", agent="bot")
    )
    await lifecycle.approve_task(db, task.id, models.TaskApprove(force=True))
    await lifecycle.pair_start_task(
        db,
        task.id,
        models.TaskPairStart(agent="a", branch_slug="mine", plan="Plan: сдать"),
    )

    with pytest.raises(HTTPException) as caught:
        await lifecycle.submit_for_review(
            db, task.id, models.TaskSubmitReview(branch="совсем-другая")
        )

    assert caught.value.status_code == 409
    detail = caught.value.detail
    assert detail["error"] == "branch_mismatch"
    assert detail["reported"] == "совсем-другая"
    assert detail["task_id"] == task.id
    assert detail["expected"] and detail["expected"] != detail["reported"]
    assert "git switch" in detail["hint"], (
        "подсказка называет команду, а не только факт"
    )


async def test_a_task_without_a_branch_report_passes_the_branch_gate(db):
    """Гейт сравнивает ОТЧЁТ: не назвал ветку — сравнивать нечего (#533)."""
    task = await lifecycle.create_task(
        db, models.TaskCreate(title="без отчёта", source="agent", agent="bot")
    )
    await lifecycle.approve_task(db, task.id, models.TaskApprove(force=True))
    await lifecycle.pair_start_task(
        db,
        task.id,
        models.TaskPairStart(agent="a", branch_slug="mine", plan="Plan: сдать"),
    )

    view = await lifecycle.submit_for_review(db, task.id)
    assert view.status == "review"


# --------------------------------------------------------------------------
# Регрессия рефакторинга, найденная на ревью PR #247
# --------------------------------------------------------------------------


def test_the_rules_mode_survives_a_skipped_step():
    """Заголовок отчёта называет режим даже когда шаг по нему пропущен.

    Регрессия выноса: присваивание ``rules_mode`` жило В ТЕЛЕ
    ``_step_submit_rules``, а при ``SUBMIT_RULES=off`` шаг не выполняется
    вовсе — заголовок выходил «режим правил: » с пустым местом там, где
    раньше стояло ``off``. Режим политики существует всегда, даже когда шаг
    по ней не запускается; это разные вещи.

    Проверяется на контексте, а не на прогоне: значение обязано быть верным
    ДО того, как конвейер решит, выполнять ли шаг.
    """
    from unittest.mock import patch

    for mode in ("off", "warn", "require"):
        with patch.object(lifecycle.config, "SUBMIT_RULES", mode):
            state = lifecycle.SubmitContext(
                db=None,  # type: ignore[arg-type]
                task_id=1,
                task={},
                body=models.TaskSubmitReview(),
            )
            assert state.rules_mode == mode, (
                f"при SUBMIT_RULES={mode} заголовок отчёта назвал бы "
                f"{state.rules_mode!r}"
            )


def test_the_rules_mode_defaults_to_warn_when_the_policy_is_unset():
    """Незаданная политика — warn: действующее правило хаба, не пустая строка."""
    from unittest.mock import patch

    with patch.object(lifecycle.config, "SUBMIT_RULES", ""):
        state = lifecycle.SubmitContext(
            db=None,  # type: ignore[arg-type]
            task_id=1,
            task={},
            body=models.TaskSubmitReview(),
        )
    assert state.rules_mode == "warn"
