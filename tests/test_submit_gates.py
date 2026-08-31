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
# AC-4: расхождение pair и headless. Главная находка задачи.
# --------------------------------------------------------------------------

# Гейты, которые проходит СДАЧА PAIR-ЗАДАЧИ и не проходит headless-путь.
# Список зафиксирован здесь не как пожелание, а как факт, замеренный по коду:
# ни диспетчер transition_after_agent_done, ни его обработчики не вызывают ни _surface_check, ни
# finding_outcome, ни resolve_branch_tip, ни machine_review_gap, ни
# ac_tests_gap. Headless-задача уезжает в ревью без сверки объявленной
# области с диффом, без спроса об исходах находок прошлой сдачи, без правил
# сдачи и без закреплённого коммита.
#
# Это НЕ починено здесь намеренно: задача #1067 обещала сделать расхождение
# видимым, а не устранить его — свести пути к одному набору значит менять
# правила, а не форму. Тест существует, чтобы расхождение перестало быть
# незаметным и чтобы новое расхождение краснело.
GATES_PAIR_ONLY = (
    "branch_matches",
    "surfaces",
    "finding_outcomes",
    "submit_rules",
    "pin_submission_sha",
    "delivery_pr",
)


def test_the_headless_path_runs_none_of_the_submit_gates():
    """AC-4: headless-путь не прогоняет гейты сдачи, и это записано.

    Проверяется по исходнику, а не по вызову: у headless-пути нет своего
    списка шагов, который можно было бы сравнить, — в этом и состоит
    расхождение. Если он такой список заведёт, тест придётся переписать, и
    это правильный момент, чтобы наборы сравнили по-настоящему.
    """
    import inspect

    from hub.services import orchestration

    # Читается диспетчер И его обработчики. После #1067 тела веток живут в
    # отдельных функциях, и чтение одного transition_after_agent_done дало бы
    # зелёный тест по причине переезда кода, а не отсутствия гейтов — то есть
    # ровно та тихая поломка стража, которую этот файл и должен исключать.
    source = "".join(
        inspect.getsource(fn)
        for fn in (
            orchestration.transition_after_agent_done,
            orchestration._complete_without_review,
            orchestration._route_after_done,
            orchestration._deliver_completed_pair_task,
        )
    )
    markers = {
        "surfaces": "_surface_check",
        "finding_outcomes": "finding_outcome",
        "submit_rules": "code_without_tests",
        "pin_submission_sha": "resolve_branch_tip",
    }
    running = [gate for gate, marker in markers.items() if marker in source]
    assert running == [], (
        "headless-путь начал прогонять гейты сдачи: "
        f"{running}. Расхождение с pair-путём изменилось — обновите "
        "GATES_PAIR_ONLY и скажите об этом в сдаче, а не молча."
    )


def test_the_pair_only_gates_are_all_declared_in_the_submit_list():
    """Список расхождения не должен разъезжаться с реальным конвейером."""
    declared = {s.name for s in lifecycle.SUBMIT_STEPS}
    missing = [g for g in GATES_PAIR_ONLY if g not in declared]
    assert not missing, (
        f"GATES_PAIR_ONLY называет шаги, которых в SUBMIT_STEPS нет: {missing}"
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
