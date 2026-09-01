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
from hub.models import STEWARD_ESCALATE_REASONS
from hub.services import gate_grounds as grounds
from hub.services.auto_approve import LADDER_SURFACES
from hub.services.ci_report import VALIDATION_PASS
from hub.services.steward_apply import (
    PRECONDITION_FACTS,
    REFUSED_LADDER,
    REFUSED_PRECONDITION,
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
