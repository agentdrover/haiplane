"""Привратник применения: что стоит между суждением и изменением исхода (#1147).

Пока стюард советует, ошибка стоит лишней строки в карточке. Как только он
начнёт применять — дефекта в develop без человека. Здесь проверяется ровно
та разница: право применять, а не само применение.
"""

from __future__ import annotations

import json
import pathlib
import re

import aiosqlite

from hub import config
from hub import repository as repo
from hub.models import STEWARD_ESCALATE_REASONS, TaskRefine
from hub.services import gate_grounds as grounds
from hub.services.auto_approve import LADDER_SURFACES
from hub.services.ci_report import VALIDATION_PASS
from hub.services.steward_apply import (
    PRECONDITION_FACTS,
    REFUSED_LADDER,
    REFUSED_PRECONDITION,
    REFUSED_UNCLOSED,
    apply_refusals,
)
from tests.test_steward_shadow import _project, _task

_PINNED = "a" * 40


async def _green(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    diff_paths: list[str] | None = None,
    confirmed: list[dict] | None = None,
    rejected: list[dict] | None = None,
    unresolved: list[dict] | None = None,
    incomplete: bool = False,
    tokens_spent: int | None = None,
    self_reviewed: bool = False,
) -> None:
    """Сдача, у которой привратнику не к чему придраться.

    Заводится ЦЕЛИКОМ, а не по кусочкам: тест, где чисто всё, кроме
    проверяемого, отличает сработавшее предусловие от несобранного пакета.
    Именно поэтому каждый тест ниже начинается с зелёного и портит ровно
    один факт.
    """
    await repo.upsert_ci_run_report(
        db,
        task_id=task_id,
        head_sha=_PINNED,
        ac_results="[]",
        validation_status=VALIDATION_PASS,
        validation_log="",
        reason="",
        reported_by="ci",
    )
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        harness_skill="multi-agent-review",
        harness_version=1,
        agent_count=11,
        tokens_spent=tokens_spent,
        duration_ms=1000,
        orchestrator="cursor",
        model="grok-4.6",
        raw_count=7,
        findings_confirmed=json.dumps(confirmed or []),
        findings_rejected=json.dumps(rejected or []),
        unresolved=json.dumps(unresolved or []),
        lost_dimensions=json.dumps([]),
        incomplete=incomplete,
        submitted_by="cursor-cloud-reviewer",
        self_reviewed=self_reviewed,
    )
    await db.commit()


def _codes(refusals: list[tuple[str, str]]) -> set[str]:
    return {code for code, _ in refusals}


# ---------------------------------------------------------------------------
# AC-1 — предусловия по фактам пакета
# ---------------------------------------------------------------------------


async def test_precondition_failure_escalates(
    db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-1: каждый факт пакета проверяется СВОИМ прогоном, а не одним примером.

    Пакет собирается в окружении без клона репозитория, поэтому дифф и
    вершина ветки честно приходят отсутствующими — и это само по себе
    отказ: «хаб не смог посмотреть» не есть «хаб посмотрел, там чисто»
    (#762). Тест перечисляет случаи, а не показывает удачный.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "apply-preconditions")

    # Ни одного факта: нет CI, нет отчёта, нет клона.
    bare = await _task(db, project_id)
    refusals = await apply_refusals(db, bare)
    assert _codes(refusals) == {REFUSED_PRECONDITION}
    sources = " ".join(detail for _, detail in refusals)
    for fact in PRECONDITION_FACTS:
        assert fact in sources, (
            f"предусловие {fact} обязано назвать себя в отказе, "
            "иначе человек не узнает, что чинить"
        )

    # Красный CI на закреплённом sha — отдельный прогон.
    red = await _task(db, project_id)
    await repo.upsert_ci_run_report(
        db,
        task_id=red,
        head_sha=_PINNED,
        ac_results="[]",
        validation_status="fail",
        validation_log="",
        reason="",
        reported_by="ci",
    )
    await db.commit()
    red_refusals = await apply_refusals(db, red)
    assert any("ci_pinned_sha" in detail for _, detail in red_refusals)

    # Отчёт другой генерации — не более слабое свидетельство, а свидетельство
    # о другом коде.
    stale = await _task(db, project_id)
    await _green(db, stale)
    await repo.update_task(db, stale, submission_generation=2)
    await db.commit()
    stale_refusals = await apply_refusals(db, stale)
    assert any("machine_review_report" in detail for _, detail in stale_refusals)


async def test_incomplete_report_is_not_a_clean_one(
    db: aiosqlite.Connection, monkeypatch
):
    """Незавершённый харнесс — не чистый отчёт, а прерванный.

    Отдельно от AC-1, потому что это единственный случай, где отчёт
    ПРИСУТСТВУЕТ и всё равно не пускает.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "apply-incomplete")
    task_id = await _task(db, project_id)
    await _green(db, task_id, incomplete=True)

    refusals = await apply_refusals(db, task_id)

    assert any("incomplete" in detail for _, detail in refusals)


# ---------------------------------------------------------------------------
# AC-2 — громкие основания наследуются, каждое со своим кодом
# ---------------------------------------------------------------------------


async def test_auto_verdict_grounds_inherited(db: aiosqlite.Connection, monkeypatch):
    """AC-2: каждое громкое основание автовердикта даёт СВОЙ код.

    Общий код на все пять читался бы как одна причина, и человек, увидев
    его дважды, не отличил бы security-находку от монокультуры — а лечатся
    они по-разному.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "REVIEW_TOKEN_BUDGET", 1000)
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "forbid")
    project_id = await _project(db, "apply-loud")

    security = await _task(db, project_id)
    await _green(db, security, rejected=[{"title": "утечка", "category": "security"}])
    assert "report_security_finding" in _codes(await apply_refusals(db, security))

    budget = await _task(db, project_id)
    await _green(db, budget, tokens_spent=5000)
    assert "report_token_budget" in _codes(await apply_refusals(db, budget))

    solo = await _task(db, project_id)
    await _green(db, solo, self_reviewed=True)
    assert "self_authored" in _codes(await apply_refusals(db, solo))

    mono = await _task(db, project_id, implementer="grok-4.6", reviewer="grok-4.6")
    await _green(db, mono)
    assert "same_family_as_reviewer" in _codes(await apply_refusals(db, mono))

    sibling = await _task(db, project_id)
    # Порядок значим: «текущим» считается ПОСЛЕДНИЙ отчёт, поэтому находки
    # кладёт сосед, вставленный раньше, а чистый отчёт приходит после него.
    await repo.insert_machine_review(
        db,
        task_id=sibling,
        submission_generation=1,
        harness_skill="multi-agent-review",
        harness_version=1,
        agent_count=3,
        tokens_spent=None,
        duration_ms=1,
        orchestrator="cursor",
        model="grok-4.6",
        raw_count=3,
        findings_confirmed=json.dumps([{"title": "нашёл сосед", "severity": "medium"}]),
        findings_rejected=json.dumps([]),
        unresolved=json.dumps([]),
        lost_dimensions=json.dumps([]),
        incomplete=False,
        submitted_by="cursor-cloud-reviewer",
        self_reviewed=False,
    )
    await db.commit()
    await _green(db, sibling)
    assert "report_sibling_mismatch" in _codes(await apply_refusals(db, sibling))


def test_every_loud_ground_is_enumerated():
    """Перечень громких оснований и их предикаты обязаны совпадать.

    Правило, добавленное в общий источник и забытое у стюарда, тихо
    расширяет автономию — то есть ошибается в сторону, где ошибка дороже.
    Проверяется ПЕРЕЧИСЛЕНИЕМ, а не примером: список кодов сверяется с
    числом предикатов в модуле.
    """
    predicates = [
        name
        for name in dir(grounds)
        if name.endswith("_ground") and callable(getattr(grounds, name))
    ]
    assert len(predicates) == len(grounds.LOUD_GROUND_CODES), (
        f"предикатов {sorted(predicates)}, кодов {list(grounds.LOUD_GROUND_CODES)} — "
        "новое основание добавлено, а код к нему нет (или наоборот)"
    )
    for code in grounds.LOUD_GROUND_CODES:
        assert code in STEWARD_ESCALATE_REASONS, (
            f"код {code} вне закрытого словаря #1022 — новых не заводится"
        )


def test_no_new_escalate_codes_are_invented():
    """Ни один код привратника не изобретён на месте.

    Словарь #1022 закрыт, и это ограничение проверяется перебором того, что
    модуль реально объявляет, а не обещанием в комментарии.
    """
    source = pathlib.Path("hub/services/steward_apply.py").read_text()
    declared = re.findall(r'^REFUSED_[A-Z_]+ = "([a-z_]+)"', source, re.M)
    assert declared, "коды привратника должны быть объявлены константами"
    for code in declared:
        assert code in STEWARD_ESCALATE_REASONS, f"{code} вне словаря #1022"


# ---------------------------------------------------------------------------
# AC-3 — ladder по фактическому диффу
# ---------------------------------------------------------------------------


def test_ladder_checked_by_diff_not_declaration():
    """AC-3: широко заявленная область не проносит правку гейта мимо проверки.

    Сегодняшний дефект автопилота: _touches_ladder сверяет поверхности с
    ЗАЯВЛЕННЫМИ областями, и задача, объявившая «hub/services/», меняет
    hub/auth.py, не задев проверку. Здесь сверяется дифф.

    Проверяется без базы: предикат чистый, и держать вокруг него прогон
    пакета значило бы проверять сборку пакета, а не правило.
    """
    from hub.services.auto_approve import ladder_hits

    declared_broadly = ["hub/services/"]
    actual_diff = ["hub/services/orchestration.py", "hub/auth.py"]

    assert ladder_hits(declared_broadly) == [], (
        "широкая декларация сама по себе ladder не трогает — "
        "именно поэтому по ней нельзя судить"
    )
    assert ladder_hits(actual_diff) == ["hub/auth.py"], (
        "по фактическому диффу правка auth обязана быть видна"
    )


def test_the_steward_cannot_approve_its_own_boundaries():
    """Модули самого судьи входят в ladder-поверхности.

    Иначе суждение, меняющее предусловия стюарда, его режим или политику,
    применилось бы наравне с любым другим — автономия, умеющая расширять
    себя. Перечислением, а не примером: проверяются все модули контура.
    """
    from hub.services.auto_approve import ladder_hits

    steward_modules = [
        "hub/services/steward_apply.py",
        "hub/services/steward_dispatch.py",
        "hub/services/steward_evidence.py",
        "hub/services/steward_judgement.py",
        "hub/services/steward_shadow.py",
        "hub/services/gate_grounds.py",
    ]
    for module in steward_modules:
        assert module in LADDER_SURFACES, (
            f"{module} — часть контура стюарда и обязан быть ladder-поверхностью"
        )
    assert ladder_hits(steward_modules) == sorted(steward_modules)


# ---------------------------------------------------------------------------
# AC-4 — нераспознанный режим читается как off
# ---------------------------------------------------------------------------


async def test_unrecognised_mode_reads_as_off(db: aiosqlite.Connection, monkeypatch):
    """AC-4: опечатка в drop-in не имеет права включать контур (#835).

    И проверяется в обе стороны: при нераспознанном значении отказ идёт
    ДО чтения фактов (иначе правило неотличимо от совпадения), а при
    рабочем режиме тот же вызов до фактов доходит.
    """
    project_id = await _project(db, "apply-mode")
    task_id = await _task(db, project_id)
    await _green(db, task_id)

    for value in ("off", "", "shadoww", "ACT!", "on"):
        monkeypatch.setattr(config, "STEWARD_MODE", value)
        refusals = await apply_refusals(db, task_id)
        assert len(refusals) == 1, (
            f"при STEWARD_MODE={value!r} отказ обязан быть один и до фактов"
        )
        code, detail = refusals[0]
        assert code == REFUSED_PRECONDITION
        assert "контур закрыт" in detail

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    live = await apply_refusals(db, task_id)
    assert not any("контур закрыт" in detail for _, detail in live), (
        "рабочий режим обязан пропускать к фактам — проверка, умеющая "
        "только отказывать, неотличима от выключателя"
    )


# ---------------------------------------------------------------------------
# Полнота: перечень предусловий и разбор фактов не расходятся
# ---------------------------------------------------------------------------


def test_precondition_list_matches_the_packet_sources():
    """Каждое имя в перечне предусловий — настоящий источник пакета.

    Опечатка в имени факта не падает: неизвестный источник просто никогда
    не находится в пакете. Поэтому перечень сверяется со словарём источников
    #1022, а не проверяется примером.
    """
    from hub.models import STEWARD_GROUND_SOURCES

    for fact in PRECONDITION_FACTS:
        assert fact in STEWARD_GROUND_SOURCES, (
            f"{fact} не источник пакета — привратник ждал бы факта, "
            "которого не бывает, и молчал бы вместо отказа"
        )


async def test_ladder_refusal_uses_its_own_code(db: aiosqlite.Connection, monkeypatch):
    """Ladder отказывает своим кодом, а не общим precondition_failed.

    Разные причины лечатся по-разному: предусловие чинит автор, ladder
    решает владелец, и один код на двоих отправил бы владельца чинить CI.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    assert REFUSED_LADDER in STEWARD_ESCALATE_REASONS
    assert REFUSED_LADDER != REFUSED_PRECONDITION


def _packet_with_diff(paths: list[str], areas: list[str]) -> object:
    """Пакет с ПРИСУТСТВУЮЩИМ диффом — то, чего нет в тестовом окружении.

    Без клона репозитория факт diff_vs_areas всегда приходит отсутствующим,
    и ветка ladder в привратнике не исполняется ни одним тестом выше. Сборку
    пакета проверяет #1074; здесь нужен сам факт, чтобы проверить ПРАВИЛО.
    """
    from hub.services.steward_evidence import EvidencePacket, present

    fact = present(
        "diff_vs_areas",
        f"дифф из {len(paths)} путей",
        paths=list(paths),
        undeclared=[],
        within_declared=True,
    )
    return EvidencePacket(
        task_id=1,
        generation=1,
        brief=None,
        facts={"diff_vs_areas": fact},
        quotes=(),
    )


def test_the_gatekeeper_reads_the_diff_not_the_declaration():
    """Привратник кормит ladder ДИФФОМ, а не заявленными областями.

    Отдельно от проверки предиката: предикат честно работает на любом
    списке, и тест на нём не отличает «правило верное» от «правилу дают не
    те данные». Именно этот разрыв и есть сегодняшний дефект автопилота,
    который задача закрывает, — значит проверять надо перекладывание, а не
    сравнение.
    """
    from hub.services.steward_apply import ladder_refusal

    caught = ladder_refusal(
        _packet_with_diff(["hub/services/orchestration.py", "hub/auth.py"], ["hub/"])
    )
    assert caught is not None, "правка auth в диффе обязана остановить применение"
    code, detail = caught
    assert code == REFUSED_LADDER
    assert "hub/auth.py" in detail

    clean = ladder_refusal(
        _packet_with_diff(["hub/services/orchestration.py"], ["hub/services/"])
    )
    assert clean is None, (
        "дифф вне ladder-поверхностей обязан проходить: проверка, умеющая "
        "только отказывать, неотличима от выключателя"
    )


# ---------------------------------------------------------------------------
# Ladder через НАСТОЯЩИЙ путь применения (#1147 ревью, отчёт 207)
# ---------------------------------------------------------------------------


async def test_ladder_stops_apply_through_the_real_entry(
    db: aiosqlite.Connection, monkeypatch
):
    """Отказ лестницы приходит из apply_refusals, а не только из своей функции.

    Прошлая версия этой проверки звала ladder_refusal напрямую и строила
    пакет руками. Функция была верна, а путь до неё — не проверен: в
    окружении без клона факт диффа всегда отсутствует, и ветка ladder
    внутри привратника не исполнялась ни одним тестом. Правило можно
    написать правильно и не позвать — ровно это ревью и назвало.

    Поэтому здесь настоящий вход: apply_refusals на задаче, чей ФАКТИЧЕСКИЙ
    дифф трогает hub/auth.py при широко заявленной области hub/. Дифф даёт
    подменённый git_ops — тем же способом, каким его дают тесты автовердикта:
    подделан обход ветки, а не решение привратника.
    """
    from hub.integrations.noop import NoopGitOps
    from hub.integrations.registry import plugins

    class _DiffOps(NoopGitOps):
        """Ветка, чей дифф трогает правила гейтов."""

        async def branch_diff_paths(self, branch, base_branch=None, repo=None):
            return ["hub/services/orchestration.py", "hub/auth.py"]

        async def head_sha(self, repo: str, base: str) -> str:
            return _PINNED

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(plugins, "git_ops", _DiffOps())

    project_id = await _project(db, "apply-ladder")
    task_id = await _task(db, project_id)
    await _green(db, task_id)
    # Область заявлена ШИРОКО — по декларации ladder не виден, и в этом
    # весь смысл: сегодняшняя проверка автопилота такую задачу пропускает.
    await repo.update_task_structured(db, task_id, TaskRefine(affected_areas=["hub/"]))
    await db.commit()

    refusals = await apply_refusals(db, task_id)

    assert REFUSED_LADDER in _codes(refusals), (
        f"дифф трогает hub/auth.py при широко заявленном hub/ — привратник "
        f"обязан отказать по лестнице, получено: {refusals}"
    )
    ladder_detail = next(d for c, d in refusals if c == REFUSED_LADDER)
    assert "hub/auth.py" in ladder_detail, "отказ обязан назвать, что именно тронуто"
    assert "hub/services/orchestration.py" not in ladder_detail, (
        "остальной дифф лестницы не трогает и в отказе ему не место"
    )


# ---------------------------------------------------------------------------
# #1148 — грязный approve только с закрытыми находками
# ---------------------------------------------------------------------------

_FINDING_A = {
    "title": "страж не читает пин",
    "severity": "high",
    "category": "correctness",
    "locator": "lines",
    "file": "hub/services/steward_apply.py",
    "start_line": 5,
    "end_line": 6,
    "line": 5,
}
_FINDING_B = {
    "title": "вторая находка того же отчёта",
    "severity": "medium",
    "category": "correctness",
    "locator": "lines",
    "file": "hub/services/steward_apply.py",
    "start_line": 15,
    "end_line": 16,
    "line": 15,
}


async def _judged_with(
    db: aiosqlite.Connection, task_id: int, closures: list[dict]
) -> None:
    """Суждение стюарда с заявленными закрытиями — контрактом #1022.

    Через настоящий записывающий путь, а не строкой в таблице: подделка
    записи проверяла бы согласие теста с самим собой.
    """
    from hub.config import TokenIdentity
    from hub.models import StewardJudgementSubmit
    from hub.services.steward_judgement import record_steward_judgement

    await record_steward_judgement(
        db,
        task_id,
        StewardJudgementSubmit(
            generation=1,
            kind="verdict",
            verdict="approve",
            confidence="high",
            closures=closures,
            model="gpt-5.3-codex",
        ),
        TokenIdentity("steward-bot", "steward", principal_id=42),
    )


def _uid(finding: dict) -> str:
    from hub.services.finding_identity import finding_uids

    return finding_uids([finding])[0]


async def _review_id(db: aiosqlite.Connection, task_id: int) -> int:
    from hub import repository as repo

    row = await repo.get_latest_machine_review(db, task_id)
    return int(dict(row)["id"])


async def test_dirty_approve_requires_closures(db: aiosqlite.Connection, monkeypatch):
    """AC-1: одной незакрытой находки из нескольких достаточно, чтобы не пустить.

    Отчёт с находками — не препятствие сам по себе: грязный путь ради того
    и существует. Препятствие — находка, про судьбу которой не сказано
    ничего. Проверяется на ДВУХ находках, где закрыта одна: правило про
    каждую, а не про наличие хоть одного закрытия.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "apply-unclosed")
    task_id = await _task(db, project_id)
    await _green(db, task_id, confirmed=[_FINDING_A, _FINDING_B])
    await _judged_with(
        db, task_id, [{"finding_uid": _uid(_FINDING_A), "type": "human_disposition"}]
    )

    refusals = await apply_refusals(db, task_id)

    assert REFUSED_UNCLOSED in _codes(refusals)
    unclosed = [d for c, d in refusals if c == REFUSED_UNCLOSED]
    assert any(_uid(_FINDING_B) in d and "не закрыта ничем" in d for d in unclosed), (
        f"незакрытая находка обязана назвать себя: {unclosed}"
    )


async def test_closure_fact_is_verified_by_the_hub(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-2: закрытие ОБЪЯВЛЕНО — не значит подтверждено.

    Три типа, три источника факта, и ни одного, который сообщал бы сам
    себя. Здесь каждый заявлен и ни один не подтверждён: диспозиции нет,
    связанной задачи нет, правки в строках находки нет. Заявление без
    факта — это слово, а грязный путь стоит на фактах.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "apply-unproven")

    for kind in ("human_disposition", "out_of_scope_linked", "fixed"):
        task_id = await _task(db, project_id)
        await _green(db, task_id, confirmed=[_FINDING_A])
        await _judged_with(
            db, task_id, [{"finding_uid": _uid(_FINDING_A), "type": kind}]
        )

        refusals = await apply_refusals(db, task_id)

        unclosed = [d for c, d in refusals if c == REFUSED_UNCLOSED]
        assert unclosed, f"закрытие {kind} не подтверждено, но пропущено"
        assert any(kind in d and "хаб его не подтверждает" in d for d in unclosed), (
            f"отказ обязан назвать тип и то, чего не хватило: {unclosed}"
        )


async def test_a_link_to_a_task_that_does_not_exist_is_not_a_closure(
    db: aiosqlite.Connection, monkeypatch
):
    """Ссылка на несозданную задачу — обещание, а не вынос.

    Отдельный тест, потому что это единственный случай, где запись
    закрытия ЕСТЬ и всё равно не считается: linked_task_id стоит, а задачи
    по нему нет. Зеркало рядом: существующая задача закрытие подтверждает.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "apply-dead-link")

    async def _with_link(link: int) -> list[tuple[str, str]]:
        task_id = await _task(db, project_id)
        await _green(db, task_id, confirmed=[_FINDING_A])
        await repo.upsert_finding_outcome(
            db,
            review_id=await _review_id(db, task_id),
            task_id=task_id,
            submission_generation=1,
            finding_uid=_uid(_FINDING_A),
            finding_index=0,
            finding_title=_FINDING_A["title"],
            outcome="deferred",
            note="вынесено отдельной задачей",
            linked_task_id=link,
            reported_by="pda_claude",
        )
        await db.commit()
        await _judged_with(
            db,
            task_id,
            [{"finding_uid": _uid(_FINDING_A), "type": "out_of_scope_linked"}],
        )
        return await apply_refusals(db, task_id)

    dead = await _with_link(999_999)
    assert REFUSED_UNCLOSED in _codes(dead), "ссылка в никуда закрытием не является"

    alive = await _task(db, project_id)
    live = await _with_link(alive)
    assert REFUSED_UNCLOSED not in _codes(live), (
        "существующая связанная задача закрытие подтверждает — иначе проверка "
        "умеет только отказывать и неотличима от выключателя"
    )


async def test_all_closures_verified_lets_approve_through(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-3: подтверждённые закрытия ПРОПУСКАЮТ.

    Проверка, умеющая только отказывать, неотличима от выключателя. Две
    находки, два разных типа закрытия, оба подтверждены записями хаба — и
    ни одного unclosed_finding в ответе привратника.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "apply-closed")
    task_id = await _task(db, project_id)
    await _green(db, task_id, confirmed=[_FINDING_A, _FINDING_B])
    review_id = await _review_id(db, task_id)

    await repo.upsert_finding_disposition(
        db,
        review_id=review_id,
        task_id=task_id,
        submission_generation=1,
        finding_index=0,
        finding_title=_FINDING_A["title"],
        disposition="wont_fix",
        note="человек посмотрел и решил жить с этим",
        decided_by="Denis",
        finding_uid=_uid(_FINDING_A),
    )
    linked = await _task(db, project_id)
    await repo.upsert_finding_outcome(
        db,
        review_id=review_id,
        task_id=task_id,
        submission_generation=1,
        finding_uid=_uid(_FINDING_B),
        finding_index=1,
        finding_title=_FINDING_B["title"],
        outcome="deferred",
        note="вынесено отдельной задачей",
        linked_task_id=linked,
        reported_by="pda_claude",
    )
    await db.commit()
    await _judged_with(
        db,
        task_id,
        [
            {"finding_uid": _uid(_FINDING_A), "type": "human_disposition"},
            {"finding_uid": _uid(_FINDING_B), "type": "out_of_scope_linked"},
        ],
    )

    refusals = await apply_refusals(db, task_id)

    assert REFUSED_UNCLOSED not in _codes(refusals), (
        f"оба закрытия подтверждены — по находкам претензий быть не должно: "
        f"{[d for c, d in refusals if c == REFUSED_UNCLOSED]}"
    )


def test_every_closure_type_names_its_evidence():
    """Словарь типов и перечень доказательств обязаны совпадать.

    Тип, добавленный в #1022 и забытый здесь, стал бы закрытием без
    доказательства — словом вместо факта, и молча. Проверяется
    перечислением, а не примером (#1107, #1120 — тем же приёмом).
    """
    from hub.models import STEWARD_CLOSURE_TYPES
    from hub.services.steward_apply import CLOSURE_EVIDENCE

    assert set(CLOSURE_EVIDENCE) == set(STEWARD_CLOSURE_TYPES)
    assert REFUSED_UNCLOSED in STEWARD_ESCALATE_REASONS


async def test_an_outcome_without_a_link_closes_nothing(
    db: aiosqlite.Connection, monkeypatch
):
    """Запись исхода БЕЗ linked_task_id — не вынос, а намерение.

    Отдельно от мёртвой ссылки: там задача названа и не существует, здесь
    не названа вовсе. Ветка «ссылки нет» иначе не исполняется ни одним
    тестом — запись есть, поле пустое, и без проверки это читалось бы как
    закрытие.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "apply-linkless")
    task_id = await _task(db, project_id)
    await _green(db, task_id, confirmed=[_FINDING_A])
    await repo.upsert_finding_outcome(
        db,
        review_id=await _review_id(db, task_id),
        task_id=task_id,
        submission_generation=1,
        finding_uid=_uid(_FINDING_A),
        finding_index=0,
        finding_title=_FINDING_A["title"],
        outcome="deferred",
        note="починим позже, задачу пока не завёл",
        linked_task_id=None,
        reported_by="pda_claude",
    )
    await db.commit()
    await _judged_with(
        db, task_id, [{"finding_uid": _uid(_FINDING_A), "type": "out_of_scope_linked"}]
    )

    refusals = await apply_refusals(db, task_id)

    assert REFUSED_UNCLOSED in _codes(refusals), (
        "исход без ссылки — намерение вынести, а не вынос"
    )


async def test_a_disposition_nobody_signed_is_not_a_human_one(
    db: aiosqlite.Connection, monkeypatch
):
    """Диспозиция с пустым decided_by человеческой не является.

    Тип закрытия называется human_disposition, и человек в нём — не
    формальность: это ссылка на чужое решение вместо собственного. Запись
    без подписи такой ссылкой не работает, и ветка проверки подписи иначе
    не исполняется.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "apply-unsigned")
    task_id = await _task(db, project_id)
    await _green(db, task_id, confirmed=[_FINDING_A])
    await repo.upsert_finding_disposition(
        db,
        review_id=await _review_id(db, task_id),
        task_id=task_id,
        submission_generation=1,
        finding_index=0,
        finding_title=_FINDING_A["title"],
        disposition="wont_fix",
        note="строка есть, решения за ней нет",
        decided_by="",
        finding_uid=_uid(_FINDING_A),
    )
    await db.commit()
    await _judged_with(
        db, task_id, [{"finding_uid": _uid(_FINDING_A), "type": "human_disposition"}]
    )

    refusals = await apply_refusals(db, task_id)

    assert REFUSED_UNCLOSED in _codes(refusals), (
        "запись без подписи — не решение человека, а строка в таблице"
    )


async def test_a_link_to_this_very_task_carries_nothing_away(
    db: aiosqlite.Connection, monkeypatch
):
    """Самоссылка не выносит работу никуда.

    Найдено кросс-модельным ревью. Проверка существования связанной задачи
    самоссылку пропускала — задача, разумеется, существует. Но
    out_of_scope_linked означает, что работа уехала ОТСЮДА; указание на эту
    же задачу оставляет её здесь и при этом объявляет закрытой.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "apply-selflink")
    task_id = await _task(db, project_id)
    await _green(db, task_id, confirmed=[_FINDING_A])
    review_id = await _review_id(db, task_id)
    await repo.upsert_finding_outcome(
        db,
        review_id=review_id,
        task_id=task_id,
        submission_generation=1,
        finding_uid=_uid(_FINDING_A),
        finding_index=0,
        finding_title=_FINDING_A["title"],
        outcome="deferred",
        note="как будто вынесено",
        linked_task_id=task_id,
        reported_by="pda_claude",
    )
    await db.commit()
    await _judged_with(
        db, task_id, [{"finding_uid": _uid(_FINDING_A), "type": "out_of_scope_linked"}]
    )

    refusals = await apply_refusals(db, task_id)

    assert REFUSED_UNCLOSED in _codes(refusals)


async def test_two_words_about_one_finding_close_nothing(
    db: aiosqlite.Connection, monkeypatch
):
    """Противоречивое заявление — не закрытие, и порядок записей его не решает.

    Найдено кросс-модельным ревью: сборка словарём оставляла последнюю
    запись, то есть порядок в списке решал, что считается закрытием.
    Проверяется ОБОИМИ порядками: подтверждённое закрытие рядом с
    неподтверждённым не должно проходить ни первым, ни вторым.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "apply-contradiction")

    good = {"finding_uid": None, "type": "human_disposition"}
    bad = {"finding_uid": None, "type": "fixed"}
    for order in ((good, bad), (bad, good)):
        task_id = await _task(db, project_id)
        await _green(db, task_id, confirmed=[_FINDING_A])
        review_id = await _review_id(db, task_id)
        await repo.upsert_finding_disposition(
            db,
            review_id=review_id,
            task_id=task_id,
            submission_generation=1,
            finding_index=0,
            finding_title=_FINDING_A["title"],
            disposition="wont_fix",
            note="человек посмотрел",
            decided_by="Denis",
            finding_uid=_uid(_FINDING_A),
        )
        await db.commit()
        await _judged_with(
            db,
            task_id,
            [{**e, "finding_uid": _uid(_FINDING_A)} for e in order],
        )

        refusals = await apply_refusals(db, task_id)

        assert REFUSED_UNCLOSED in _codes(refusals), (
            f"порядок {[e['type'] for e in order]} не должен решать исход"
        )


async def test_the_refusal_says_whether_the_hub_could_look(
    db: aiosqlite.Connection, monkeypatch
):
    """«Не смотрели» и «смотрели, не трогали» — разные отказы (#762).

    Найдено кросс-модельным ревью: один шаблон накрывал оба случая, и
    текст читался как «коммиты смотрели, строк они не трогали» даже когда
    заглянуть в дифф не удалось вовсе. Для того, кто разбирает, это разница
    между «правки не было» и «мы не наблюдали».
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "apply-unknown-vs-untouched")
    task_id = await _task(db, project_id)
    await _green(db, task_id, confirmed=[_FINDING_A])
    await _judged_with(
        db, task_id, [{"finding_uid": _uid(_FINDING_A), "type": "fixed"}]
    )

    refusals = await apply_refusals(db, task_id)

    unclosed = [d for c, d in refusals if c == REFUSED_UNCLOSED]
    assert unclosed
    assert any("посмотреть НЕ СМОГ" in d for d in unclosed), (
        f"клона нет — отказ обязан назвать это отсутствием наблюдения: {unclosed}"
    )


async def test_no_closures_at_all_refuses_every_finding(
    db: aiosqlite.Connection, monkeypatch
):
    """Пустой список закрытий отказывает КАЖДУЮ находку, а не молчит.

    Пробел, названный кросс-модельным ревью: соседний тест держит список
    закрытий непустым, поэтому ранний выход «нечего проверять — пропускаем»
    остался бы зелёным. Здесь заявлено ноль закрытий при двух находках, и
    отказов обязано быть два.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "apply-no-closures")
    task_id = await _task(db, project_id)
    await _green(db, task_id, confirmed=[_FINDING_A, _FINDING_B])
    await _judged_with(db, task_id, [])

    refusals = await apply_refusals(db, task_id)

    unclosed = [d for c, d in refusals if c == REFUSED_UNCLOSED]
    assert len(unclosed) == 2, f"по одному отказу на находку: {unclosed}"
    assert any(_uid(_FINDING_A) in d for d in unclosed)
    assert any(_uid(_FINDING_B) in d for d in unclosed)
