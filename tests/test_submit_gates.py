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

import json

import pytest
from fastapi import HTTPException

import aiosqlite

from hub import config, models
from hub import repository as repo
from hub import services
from hub.db import deserialize_str_list
from hub.models import TaskCreate, TaskSubmitReview
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
    "pin_submission_sha",
    "same_sha_from_review_is_current",
    "resolve_diff",
    "surfaces",
    "finding_outcomes",
    "submit_rules",
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
    """Сетевые шаги — после дешёвых, НЕИЗМЕННЫХ во времени отказов.

    До #1265 круга 2 это звучало «после ВСЕХ отказов»: единственным
    сетевым шагом был ``pin_submission_sha``, и цена отказа до него всегда
    была дешевле сети. #1265 добавила сюда второй смысл: пиннинг обязан
    идти ДО surfaces/finding_outcomes/submit_rules — эти гейты отказывают
    не только по коду, но и по ВРЕМЕНИ (новая находка, смена политики), и
    повтор того же запроса не должен встретить отказ, которого не получил
    оригинал (находка Codex #1 на 41159735). Отказы task_is_submittable и
    branch_matches — дешёвые и неизменные во времени для одного и того же
    запроса, поэтому пиннинг остаётся ПОСЛЕ них и только их.
    """
    names = [s.name for s in lifecycle.SUBMIT_STEPS]
    cheap_and_stable = ("task_is_submittable", "canonical_branch", "branch_matches")
    assert names.index("pin_submission_sha") > max(
        names.index(n) for n in cheap_and_stable if n in names
    ), "пиннинг вершины ветки ходит в сеть и обязан идти после дешёвых отказов"
    mutable_gates = ("surfaces", "finding_outcomes", "submit_rules")
    assert names.index("pin_submission_sha") < min(
        names.index(n) for n in mutable_gates
    ), (
        "пиннинг и распознавание повтора обязаны идти ДО гейтов, чей ответ "
        "меняется со временем — иначе повтор рискует их отказом"
    )
    assert names.index("same_sha_from_review_is_current") < min(
        names.index(n) for n in mutable_gates
    ), "распознавание повтора — ДО изменчивых гейтов, а не после них"
    assert names.index("delivery_pr") > names.index("pin_submission_sha")
    assert names.index("delivery_pr") == len(names) - 1, (
        "доставка — последний шаг: повтор, ушедший в no-op раньше, не "
        "должен дойти до пуша/PR"
    )


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

    # task_is_submittable — шаг, которого у headless нет вовсе: он проверяет,
    # что задача pair и в статусе, из которого сдают. same_sha_from_review_is_
    # current — тоже только pair (#1265): headless сдаёт done-отчётом, у него
    # нет ни статуса review, ни повторной сдачи того же коммита через этот
    # путь — решение не трогать headless названо в постановке прямо.
    assert set(submit) - set(headless) == {
        "task_is_submittable",
        "same_sha_from_review_is_current",
    }
    assert set(headless) - set(submit) == set()

    active_here = {n for n, s in headless.items() if s.active}
    inactive_here = {n for n, s in headless.items() if not s.active}
    # #1155: finding_outcomes ушёл отсюда в активные — у отчёта о готовности
    # появилось поле исходов, и причина «ответить негде» перестала быть верной.
    # Матрица решений #1122 обновлена этой задачей, и сдача называет перемену.
    assert inactive_here == {"branch_matches"}, (
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


# --- unresolved отчёта #227: потолок и молчание пиннинга ---


async def test_the_warn_cap_actually_caps_a_require_policy(monkeypatch):
    """Потолок warn обязан ГЛУШИТЬ отказ, а не только объявлять о себе.

    Первая редакция объявляла capped_at_warn в списке headless-шагов, но сами
    шаги читали политику из config мимо потолка: при SDD_SURFACES=require
    headless-путь поднимал бы 422 вопреки докстроке потолка «этот путь читает
    ту же политику, но никогда не отказывает по ней».

    Ревью оставило это в unresolved: опровергатель снял как латентное («на
    проде warn, ветка не берётся»), валидатор оставил. Латентность — не
    отсутствие дефекта, а обещание, записанное и не исполненное, хуже
    отсутствующего. Решаю замером, а не голосованием.
    """
    from hub.services.gate_pipeline import OFF, capped_at_warn

    monkeypatch.setattr(config, "SDD_SURFACES", "require")
    assert capped_at_warn("SDD_SURFACES")() == "warn", (
        "политика require обязана прийти к шагу как warn"
    )
    monkeypatch.setattr(config, "SDD_SURFACES", "off")
    assert capped_at_warn("SDD_SURFACES")() == OFF, (
        "off остаётся off: потолок ограничивает строгость, а не включает шаг"
    )


async def test_the_capped_mode_reaches_the_step(monkeypatch):
    """И потолок ДОХОДИТ до шага, а не теряется в конвейере.

    Это вторая половина, и без неё первая ничего не значит: capped_at_warn
    можно объявить верно и не передать никуда — ровно так дефект и выглядел.
    run_steps использовал mode() только чтобы пропустить шаг при off.
    """
    from dataclasses import dataclass, field as dc_field

    from hub.services.gate_pipeline import Step, capped_at_warn, run_steps

    seen: list[str] = []

    @dataclass
    class _Ctx:
        gate_mode: str = dc_field(default="")

    async def _record(ctx: _Ctx) -> None:
        seen.append(ctx.gate_mode)

    monkeypatch.setattr(config, "SDD_SURFACES", "require")
    await run_steps(
        _Ctx(), (Step("surfaces", _record, mode=capped_at_warn("SDD_SURFACES")),)
    )

    assert seen == ["warn"], (
        f"шаг обязан получить действующий режим, а не перечитывать политику: {seen}"
    )


async def test_the_headless_path_says_when_the_tip_was_not_pinned(
    db: aiosqlite.Connection,
):
    """Сорвавшийся пиннинг на headless-пути НЕ молчит.

    Пара говорит об этом в тексте самой сдачи («Branch tip NOT pinned: …»), а
    headless такого текста не имеет вовсе — он зовёт только заметки. Ревью
    оставило находку в unresolved: опровергатель счёл её вне скоупа («задача
    про список гейтов»), валидатор — нарушением честности #572/#767.

    В скоупе она потому, что задача как раз про РАВЕНСТВО двух путей: молча
    незакреплённая вершина means вердикт относится к номеру сдачи, а не к коду.
    """
    from hub.services.lifecycle import SubmitContext, write_submission_notices

    tv = await services.create_task(db, TaskCreate(title="Не закрепилось"))
    await db.commit()

    state = SubmitContext(
        db=db,
        task_id=tv.id,
        task=dict(await repo.get_task(db, tv.id)),
        body=TaskSubmitReview(model="claude-opus-5"),
    )
    state.submission_sha = ""
    state.sha_reason = "could not fetch task-1/x: сеть недоступна"

    await write_submission_notices(state)
    await db.commit()

    said = " ".join(u["content"] for u in await repo.get_task_updates(db, tv.id))
    assert "НЕ закреплена" in said, "молчание о незакреплённой вершине недопустимо"
    assert "сеть недоступна" in said, (
        "причина обязана быть названа: «не смогли посмотреть» и «нечего было "
        "закреплять» лечатся по-разному"
    )


async def test_the_surfaces_step_obeys_the_cap_instead_of_rereading_the_policy(
    db: aiosqlite.Connection, monkeypatch
):
    """И САМ ШАГ соблюдает потолок, а не перечитывает политику.

    Этот тест дописан после мутационной проверки: две первые проверки —
    «потолок считается» и «режим доходит до шага» — обе оставались зелёными,
    когда _step_surfaces возвращали к чтению config напрямую. То есть предмет
    находки покрыт не был, а таблица мутаций выглядела бы полной.

    Ошибка атрибуции ловится только раздельной мутацией: проверять «потолок
    объявлен» и «потолок применён» надо разными тестами, потому что сломаться
    они могут порознь.
    """
    from hub.services.lifecycle import SubmitContext, _step_surfaces

    monkeypatch.setattr(config, "SDD_SURFACES", "require")

    tv = await services.create_task(db, TaskCreate(title="Поверхности"))
    await repo.update_task_structured(
        db, tv.id, models.TaskRefine(affected_areas=["docs/notes.md"])
    )
    await db.commit()

    state = SubmitContext(
        db=db,
        task_id=tv.id,
        task=dict(await repo.get_task(db, tv.id)),
        body=TaskSubmitReview(model="claude-opus-5"),
    )
    # Дифф ушёл за объявленную область — при require это отказ 422.
    state.diff_paths = ["hub/services/somewhere_else.py"]
    state.diff_reason = ""
    state.gate_mode = "warn"

    await _step_surfaces(state)

    assert state.surface_note, (
        "под потолком warn шаг обязан НАПИСАТЬ о расхождении, а не смолчать"
    )

    # Контроль: без потолка та же ситуация действительно отказывает, иначе
    # предыдущее утверждение зелено по посторонней причине.
    state.gate_mode = ""
    with pytest.raises(HTTPException) as refused:
        await _step_surfaces(state)
    assert refused.value.status_code == 422


async def test_the_rules_step_obeys_the_cap_instead_of_rereading_the_policy(
    db: aiosqlite.Connection, monkeypatch
):
    """И шаг ПРАВИЛ СДАЧИ соблюдает потолок — симметрично шагу поверхностей.

    Найдено ревью (отчёт #229) и подтверждено мутацией: снятие потолка в
    _step_submit_rules не роняло НИ ОДНОГО теста из 3184. То есть первая
    редакция закрыла потолок у поверхностей, а у правил сдачи оставила его
    ровно так же непроверенным, как он был до задачи.

    Мой собственный урок здесь повторился: я нашёл эту дыру мутацией для
    _step_surfaces, написал тест на него — и не проверил соседний шаг с тем
    же потолком. Два шага объявлены через один capped_at_warn, но ломаются
    порознь, и проверять их надо порознь.
    """
    from hub.services.lifecycle import SubmitContext, _step_submit_rules

    monkeypatch.setattr(config, "SUBMIT_RULES", "require")

    tv = await services.create_task(db, TaskCreate(title="Правила сдачи"))
    await db.commit()

    state = SubmitContext(
        db=db,
        task_id=tv.id,
        task=dict(await repo.get_task(db, tv.id)),
        body=TaskSubmitReview(model="claude-opus-5"),
    )
    # Код без единого теста — при require это отказ 422.
    state.diff_paths = ["hub/services/somewhere.py"]
    state.diff_reason = ""
    state.gate_mode = "warn"

    await _step_submit_rules(state)

    assert any("Тесты рядом с кодом" in line for line in state.rule_lines), (
        "под потолком warn шаг обязан НАПИСАТЬ о нарушении, а не смолчать: "
        "потолок ограничивает строгость, а не выключает проверку"
    )

    # Контроль: без потолка та же ситуация действительно отказывает. Без него
    # предыдущее утверждение было бы зелено по посторонней причине — например
    # если бы code_without_tests вовсе ничего не нашла.
    state.rule_lines = []
    state.gate_mode = ""
    with pytest.raises(HTTPException) as refused:
        await _step_submit_rules(state)
    assert refused.value.status_code == 422


# --------------------------------------------------------------------------
# #1265: пересдача того же коммита из review не рождает новое поколение
# --------------------------------------------------------------------------
#
# Образец — #1172, 13.09.2026: сдачи №6 и №7, один и тот же sha
# b530268a24c6, исполнитель и стюард сдали независимо. Хаб принял вторую как
# новую работу: поколение выросло с 6 до 7, диспетч ревью вызван повторно
# (отказан лимитом), а вердикт по «заменённой» сдаче перестал быть текущим,
# хотя код не менялся ни на байт.


async def _pair_task_ready_to_submit(
    db: aiosqlite.Connection, title: str, *, agent: str = "dev"
):
    task = await lifecycle.create_task(
        db, models.TaskCreate(title=title, source="agent", agent="bot")
    )
    await lifecycle.approve_task(db, task.id, models.TaskApprove(force=True))
    await lifecycle.pair_start_task(
        db,
        task.id,
        models.TaskPairStart(agent=agent, branch_slug="mine", plan="Plan: сдать"),
    )
    return task


def _install_fixed_tip_git(
    monkeypatch, tip: str, *, diff_paths: list[str] | None = None
):
    """Git-двойник, чья вершина не меняется между сдачами, пока тест не велит.

    ``diff_paths`` — для AC-5 (accept_areas): по умолчанию NoopGitOps отдаёт
    None («дифф не прочитан»), и мержить в affected_areas нечего. Явный
    список делает дифф наблюдаемым, не трогая остальные тесты, которым он
    не нужен.
    """
    from unittest.mock import AsyncMock

    from hub.integrations.noop import NoopGitOps
    from hub.integrations.registry import plugins
    from hub.services import orchestration

    class _Git(NoopGitOps):
        def __init__(self, tip: str) -> None:
            self.tip = tip

        async def fetch_base(self, repo: str, base: str):
            return (True, "")

        async def head_sha(self, repo: str, base: str) -> str:
            return self.tip

        async def branch_diff_paths(
            self, branch: str, base_branch: str | None = None, repo: str | None = None
        ) -> list[str] | None:
            return diff_paths

    git = _Git(tip)
    monkeypatch.setattr(plugins, "git_ops", git)
    monkeypatch.setattr(
        orchestration,
        "project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
    )
    return git


async def test_resubmitting_the_same_sha_from_review_is_not_a_new_generation(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-1: тот же коммит из review не открывает новое поколение (#1265, #1172).

    Круг 2 (владелец, вариант A): решение о заказе ревью на этом поколении
    НЕ отключается — оно принимается ровно так, как #1150/#1152 всегда его
    принимали, только на неизменившемся поколении, а не на новом. Здесь
    проверяется, что решение ДОХОДИТ до диспетча (шпион на самой функции
    принятия решения) — а КАКОЕ это решение при разном покрытии отчётом,
    без единой правки, доказывают шесть тестов tests/test_review_dispatch.py
    (#1150/#1152), которые эта задача не трогает.
    """
    from hub.services import review_dispatch

    _install_fixed_tip_git(monkeypatch, "same-tip")

    task = await _pair_task_ready_to_submit(db, "Дубль сдачи")
    first = await lifecycle.submit_for_review(
        db, task.id, models.TaskSubmitReview(agent="dev", model="claude-opus-5")
    )
    assert first.submission_generation == 1
    assert first.submission_sha == "same-tip"

    # Вердикт закреплён за поколением 1 напрямую через repo, а не через
    # record_review_verdict сервиса: клиентское ревью на APPROVED само уводит
    # задачу review->running (#3433 lifecycle.py — «report done on APPROVED»),
    # а #1172 воспроизводит именно гонку ДВУХ СДАЧ, пока задача ещё в review
    # и вердикт по ней уже есть (машинное ревью его и оставляет там).
    await repo.record_review_verdict(db, task.id, "approved")
    await db.commit()

    dispatch_calls: list[int] = []

    async def _spy(db_, task_id_, **kwargs):
        dispatch_calls.append(task_id_)
        return False

    monkeypatch.setattr(review_dispatch, "maybe_dispatch_review", _spy)

    second = await lifecycle.submit_for_review(
        db, task.id, models.TaskSubmitReview(agent="dev", model="claude-opus-5")
    )

    assert second.submission_generation == 1, "поколение не растёт — код не менялся"
    assert second.submission_sha == "same-tip"
    assert second.review_approved_current is True, (
        "вердикт по прежней сдаче остаётся текущим — это ТА ЖЕ сдача"
    )
    assert dispatch_calls == [task.id], (
        "решение о заказе ревью принимается на дубле — по #1150/#1152, на "
        "том же поколении, а не пропущено (находка Codex #1 на 41159735)"
    )
    hint = second.lifecycle_hint or ""
    assert "поколение 1" in hint, "ответ называет поколение, на котором уже стоит сдача"
    assert "bot" in hint, (
        "ответ называет, кто сдал это поколение (assigned_agent задачи)"
    )


async def test_resubmitting_a_new_sha_from_review_still_bumps_the_generation(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-2: ветка сдвинулась — пересдача открывает новое поколение, как сегодня."""
    git = _install_fixed_tip_git(monkeypatch, "first-tip")

    task = await _pair_task_ready_to_submit(db, "Новый коммит")
    first = await lifecycle.submit_for_review(
        db, task.id, models.TaskSubmitReview(agent="dev")
    )
    assert first.submission_generation == 1

    git.tip = "second-tip"
    second = await lifecycle.submit_for_review(
        db, task.id, models.TaskSubmitReview(agent="dev")
    )

    assert second.submission_generation == 2, "новый коммит — новое поколение"
    assert second.submission_sha == "second-tip"


async def test_same_sha_resubmission_answer_is_idempotent(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-3: одна и та же сдача X дважды подряд — один ответ, и не ошибка.

    Круг 2 (Codex #1, #3 на 41159735): и после того, как на поколение легла
    находка при FINDING_OUTCOME=require — гейт finding_outcomes отказал бы
    ОБЫЧНОЙ сдаче без ответа на неё (422), а повтор того же sha не должен
    встретить отказ, которого не получил оригинал; и без пуша/открытия PR.
    """
    from hub import config
    from hub.services import orchestration

    _install_fixed_tip_git(monkeypatch, "idem-tip")

    task = await _pair_task_ready_to_submit(db, "Идемпотентный повтор")
    await lifecycle.submit_for_review(db, task.id, models.TaskSubmitReview(agent="dev"))

    first_retry = await lifecycle.submit_for_review(
        db, task.id, models.TaskSubmitReview(agent="dev")
    )
    second_retry = await lifecycle.submit_for_review(
        db, task.id, models.TaskSubmitReview(agent="dev")
    )

    assert first_retry.submission_generation == second_retry.submission_generation == 1
    assert first_retry.submission_sha == second_retry.submission_sha == "idem-tip"
    assert first_retry.status == "review"
    assert second_retry.status == "review"
    assert first_retry.lifecycle_hint == second_retry.lifecycle_hint, (
        "повтор по таймауту обязан получить ТОТ ЖЕ ответ, а не новый текст"
    )

    # Находка легла на поколение 1, режим — require.
    await repo.insert_machine_review(
        db,
        task_id=task.id,
        submission_generation=1,
        harness_skill="lite-diff-review",
        model="grok-4.6",
        raw_count=1,
        findings_confirmed=json.dumps(
            [{"title": "утечка", "severity": "high", "file": "hub/x.py"}]
        ),
        submitted_by="cursor-cloud-reviewer",
    )
    await db.commit()
    monkeypatch.setattr(config, "FINDING_OUTCOME", "require")

    ensure_pr_calls: list[int] = []

    async def _ensure_spy(db_, task_, canonical, diff_paths):
        ensure_pr_calls.append(task_["id"])
        return None, ""

    monkeypatch.setattr(orchestration, "ensure_delivery_pr", _ensure_spy)

    third_retry = await lifecycle.submit_for_review(
        db, task.id, models.TaskSubmitReview(agent="dev")
    )

    assert third_retry.submission_generation == 1, (
        "находка + require не роняют повтор в новую генерацию"
    )
    assert third_retry.status == "review"
    assert ensure_pr_calls == [], (
        "повтор не пушит ветку и не открывает PR (находка Codex #3 на 41159735)"
    )


async def test_same_sha_resubmission_still_applies_its_payload(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-5: accept_areas и finding_outcomes на том же sha не теряются молча.

    Находка Codex #2 на 41159735: ``_same_sha_noop_response`` раньше
    возвращала ответ, минуя ``_apply_submission`` целиком, — accept_areas и
    finding_outcomes из тела повторной сдачи тихо выбрасывались, а ответ
    всё равно выглядел успехом.
    """
    from hub.services.finding_identity import finding_uid

    routine = sorted(lifecycle.commit_scope.ROUTINE_PATHS)[0]
    _install_fixed_tip_git(
        monkeypatch, "payload-tip", diff_paths=["hub/x.py", "hub/new_area.py", routine]
    )

    task = await _pair_task_ready_to_submit(db, "Применение данных")
    # Признать объём можно только против ОБЪЯВЛЕННОЙ области — как и у
    # обычной сдачи: без неё сверка «unknown», и дописывать нечего (#890).
    await repo.update_task_structured(
        db, task.id, models.TaskRefine(affected_areas=["hub/x.py"])
    )
    await db.commit()
    await lifecycle.submit_for_review(db, task.id, models.TaskSubmitReview(agent="dev"))

    finding = {"title": "утечка", "severity": "high", "file": "hub/x.py"}
    review_id = await repo.insert_machine_review(
        db,
        task_id=task.id,
        submission_generation=1,
        harness_skill="lite-diff-review",
        model="grok-4.6",
        raw_count=1,
        findings_confirmed=json.dumps([finding]),
        submitted_by="cursor-cloud-reviewer",
    )
    await db.commit()
    uid = finding_uid(finding)

    retry = await lifecycle.submit_for_review(
        db,
        task.id,
        models.TaskSubmitReview(
            agent="dev",
            accept_areas=True,
            finding_outcomes=[{"finding_uid": uid, "outcome": "fixed"}],
        ),
    )

    assert retry.submission_generation == 1, "данные применились без нового поколения"

    outcomes = await repo.list_finding_outcomes(db, review_id)
    recorded = [dict(o) for o in outcomes]
    assert any(o["finding_uid"] == uid and o["outcome"] == "fixed" for o in recorded), (
        "исход находки записан на текущее поколение, а не выброшен"
    )

    fresh = dict(await repo.get_task(db, task.id))
    areas = deserialize_str_list(fresh.get("affected_areas"))
    assert "hub/new_area.py" in areas, (
        "accept_areas дописал affected_areas тем же диффом, что и обычная сдача"
    )
    assert routine not in areas, (
        "служебный путь не дописывается — тот же фильтр ROUTINE_PATHS, что у "
        "обычной сдачи (Cursor #383, 56fc0a823ab5e1a2)"
    )
    feed = " ".join(
        (dict(u)["content"] or "") for u in await repo.get_task_updates(db, task.id)
    )
    assert lifecycle.commit_scope.SCOPE_GROWTH_MARKER in feed, (
        "рост объёма на повторе записан так же, как на обычной сдаче "
        "(Cursor #383, 5c3ba1c53664357f)"
    )


async def test_accept_areas_on_a_retry_names_an_unreadable_diff(
    db: aiosqlite.Connection, monkeypatch
):
    """Cursor #383 (40af8ae7af0f7943): accept_areas на повторе при
    непрочитанном диффе не молчит об успехе — лента называет, что сверка
    области НЕ выполнялась, как и у обычной сдачи, и область не расширена."""
    _install_fixed_tip_git(monkeypatch, "unreadable-tip")  # дифф: None

    task = await _pair_task_ready_to_submit(db, "Непрочитанный дифф")
    await repo.update_task_structured(
        db, task.id, models.TaskRefine(affected_areas=["hub/x.py"])
    )
    await db.commit()
    await lifecycle.submit_for_review(db, task.id, models.TaskSubmitReview(agent="dev"))
    before = len(await repo.get_task_updates(db, task.id))

    retry = await lifecycle.submit_for_review(
        db, task.id, models.TaskSubmitReview(agent="dev", accept_areas=True)
    )

    assert retry.submission_generation == 1
    new = [
        (dict(u)["content"] or "")
        for u in (await repo.get_task_updates(db, task.id))[before:]
    ]
    assert any("НЕ выполнялась" in c for c in new), (
        f"повтор с accept_areas обязан назвать, что сверка не выполнялась: {new}"
    )
    fresh = dict(await repo.get_task(db, task.id))
    assert deserialize_str_list(fresh.get("affected_areas")) == ["hub/x.py"]


async def test_the_second_read_refusal_is_said_once_per_report(
    db: aiosqlite.Connection, monkeypatch
):
    """Cursor #383 (78312fbb487b30ca): повтор того же sha — штатный путь, и
    отказ #1152 на нём не копится в ленте: один раз на отчёт."""
    from hub.services import review_dispatch

    async def _covers(db_, task_):
        return 42

    monkeypatch.setattr(review_dispatch, "_report_already_covers_this_sha", _covers)
    task = await _pair_task_ready_to_submit(db, "Отказ один раз")
    row = dict(await repo.get_task(db, task.id))

    assert await review_dispatch._this_code_was_already_read(db, row) is True
    assert await review_dispatch._this_code_was_already_read(db, row) is True

    said = [
        (dict(u)["content"] or "")
        for u in await repo.get_task_updates(db, task.id)
        if "отчёт #42 покрывает ту же вершину" in (dict(u)["content"] or "")
    ]
    assert len(said) == 1, (
        f"отказ сказан ровно один раз, а не на каждом повторе: {said}"
    )


async def test_a_replayed_payload_is_not_an_error_but_a_typo_is(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-3 и AC-5 на одном теле (Cursor #380: db8880bcc6c939e2, 97ee0d78bda22c1c).

    Повтор ТОГО ЖЕ тела с finding_outcomes, которые первый вызов уже записал,
    — не ошибка: эти uid отвечены на поколении. А uid, которого у поколения
    нет вовсе, — опечатка, и на неё ответ тот же 422, что у обычной сдачи,
    а не молчаливый успех над выброшенными данными.
    """
    from fastapi import HTTPException

    from hub.services.finding_identity import finding_uid

    _install_fixed_tip_git(monkeypatch, "replay-tip")

    task = await _pair_task_ready_to_submit(db, "Повтор тела и опечатка")
    await lifecycle.submit_for_review(db, task.id, models.TaskSubmitReview(agent="dev"))

    finding = {"title": "утечка", "severity": "high", "file": "hub/x.py"}
    await repo.insert_machine_review(
        db,
        task_id=task.id,
        submission_generation=1,
        harness_skill="lite-diff-review",
        model="grok-4.6",
        raw_count=1,
        findings_confirmed=json.dumps([finding]),
        submitted_by="cursor-cloud-reviewer",
    )
    await db.commit()
    body = models.TaskSubmitReview(
        agent="dev",
        finding_outcomes=[{"finding_uid": finding_uid(finding), "outcome": "fixed"}],
    )

    first = await lifecycle.submit_for_review(db, task.id, body)
    replay = await lifecycle.submit_for_review(db, task.id, body)

    assert first.submission_generation == replay.submission_generation == 1
    assert first.lifecycle_hint == replay.lifecycle_hint, (
        "повтор тела с уже записанными исходами — тот же ответ, а не ошибка"
    )

    with pytest.raises(HTTPException) as refused:
        await lifecycle.submit_for_review(
            db,
            task.id,
            models.TaskSubmitReview(
                agent="dev",
                finding_outcomes=[
                    {"finding_uid": "0000deadbeef0000", "outcome": "fixed"}
                ],
            ),
        )
    assert refused.value.status_code == 422, (
        "uid, которого у поколения нет, — не повтор, а опечатка: 422, как у обычной сдачи"
    )


async def test_the_second_read_refusal_does_not_claim_a_bumped_generation(
    db: aiosqlite.Connection,
):
    """Cursor #380 (e1e65dee8cdf6635): отказ #1152 теперь пишется и на пути
    #1265, где поколение НЕ поднималось. Текст обязан быть правдой на обоих
    путях — «код не изменился», а не «пересдача подняла поколение»."""
    from hub.services.review_dispatch import _refuse_second_read

    task = await _pair_task_ready_to_submit(db, "Текст отказа")
    await _refuse_second_read(db, task.id, 42)

    said = " ".join(
        (dict(u)["content"] or "") for u in await repo.get_task_updates(db, task.id)
    )
    assert "отчёт #42" in said
    assert "подняла" not in said, (
        f"на пути #1265 поколение не поднималось — текст не вправе это утверждать: {said}"
    )


async def test_same_sha_from_fix_requested_is_untouched(db: aiosqlite.Connection):
    """AC-4: правило не срабатывает вне review — сдача из fix_requested не тронута.

    fix_requested — headless-статус; ``_step_task_is_submittable`` отказывает
    headless-задаче (``job_id`` установлен) раньше, чем конвейер дойдёт до
    пин-шага, так что сегодняшняя сдача из fix_requested этим шагом вообще не
    задета. Опасная мутация из review-чеклиста — «сравнивать sha из любого
    статуса» — это про ЯДРО условия: без привязки к
    ``resubmitted_from_review`` (=``status == "review"``, #1054) шаг пометил
    бы дублем и пересдачу того же sha из fix_requested, если бы конвейер до
    него когда-нибудь дошёл. Проверяется поэтому на самом шаге напрямую, а
    не только через отказ headless-задачи выше по списку.
    """
    from hub.services.lifecycle import (
        SubmitContext,
        _step_same_sha_from_review_is_current,
    )

    state = SubmitContext(
        db=db,
        task_id=1,
        task={"status": "fix_requested", "submission_sha": "same-sha"},
        body=models.TaskSubmitReview(),
    )
    # fix_requested, не review: #1054 связывает resubmitted_from_review
    # ИМЕННО со статусом review, и здесь он выставлен вручную ровно так же,
    # как это делает _step_task_is_submittable для fix_requested — False.
    state.resubmitted_from_review = False
    state.replaced_sha = "same-sha"
    state.submission_sha = "same-sha"

    await _step_same_sha_from_review_is_current(state)

    assert state.same_sha_noop is False, (
        "пересдача того же sha из fix_requested не должна помечаться дублем "
        "ревью — правило действует только из статуса review, #1265 scope_out"
    )
