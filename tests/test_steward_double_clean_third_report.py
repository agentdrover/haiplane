"""Один предикат чистоты для всех отчётов поколения; решатели в реестрах (#1986, круг 2).

Находка независимого ревью: прочие отчёты поколения проверялись уже, чем пара.
Третий отчёт с саморевью, без принципала, перенесённый или без свидетельств
исполнения не отменял исключение. Здесь каждый из четырёх признаков у третьего
отчёта проверяется на ОБОИХ этапах: раннем (``apply_refusals``) и
транзакционном (запись вердикта после того, как отчёт пришёл со второго
соединения).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from hub import db as db_module
from tests.steward_double_clean_support import (
    THIRD,
    add_report,
    build_case,
    ceiling_detail,
    verdict_of,
)
from tests.test_steward_double_clean_races import _between

from hub.services.steward_apply import apply_refusals

_THIRD = {
    "self_reviewed": ({"self_reviewed": True}, "self_reviewed:"),
    "unknown_principal": ({"principal": None}, "identity:"),
    "carried": ({"carried_from": 1}, "carried:"),
    "no_evidence": ({"evidence": False}, "no_data"),
}


@pytest.mark.parametrize("label", sorted(_THIRD))
async def test_a_third_report_with_any_of_the_four_flaws_cancels_early(
    db, monkeypatch, label
):
    fields, needle = _THIRD[label]

    async def third(case):
        await add_report(db, case.task_id, **{"principal": THIRD, **fields})

    case = await build_case(db, monkeypatch, f"dc3-early-{label}", after_order=third)

    detail = ceiling_detail(await apply_refusals(db, case.task_id, 1))

    assert needle in detail, (label, detail)


@pytest.mark.parametrize("label", sorted(_THIRD))
async def test_a_third_report_with_any_of_the_four_flaws_cancels_in_the_transaction(
    db, db_dsn, monkeypatch, label
):
    fields, needle = _THIRD[label]
    case = await build_case(db, monkeypatch, f"dc3-txn-{label}")
    other = await db_module.connect(db_dsn)
    try:

        async def late():
            await add_report(other, case.task_id, **{"principal": THIRD, **fields})

        err = await _between(db, case, late)
    finally:
        await other.close()

    assert err is not None, label
    assert needle in str(err.detail), (label, err.detail)
    assert await verdict_of(db, case.task_id) == ""


def _imported_steward_modules(path: str) -> set[str]:
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("hub.services.steward_"):
                found.add(node.module)
            elif node.module == "hub.services":
                found |= {
                    f"hub.services.{a.name}"
                    for a in node.names
                    if a.name.startswith("steward_")
                }
    return found


def test_every_module_the_approval_delegates_to_is_in_both_registries():
    """steward_apply решает об одобрении через эти модули: правка любого - правка гейта."""
    from hub.services.auto_approve import APPROVAL_DECIDERS, LADDER_SURFACES

    delegates = _imported_steward_modules("hub/services/steward_apply.py")
    assert "hub.services.steward_double_clean" in delegates
    for module in sorted(delegates):
        path = module.replace(".", "/") + ".py"
        assert path in LADDER_SURFACES, f"{path} вне LADDER_SURFACES"
        assert path in APPROVAL_DECIDERS, f"{path} вне APPROVAL_DECIDERS"


@pytest.mark.parametrize("column", ["title", "description", "scope_in"])
async def test_a_statement_edit_after_the_early_check_cancels_the_exception(
    db, db_dsn, monkeypatch, column
):
    """Согласие советника было о прежней постановке: правка между этапами снимает исключение."""
    case = await build_case(db, monkeypatch, f"dc3-statement-{column}")
    other = await db_module.connect(db_dsn)
    value = '["новое"]' if column == "scope_in" else "новое значение"
    try:

        async def edit():
            await other.execute(
                f"UPDATE tasks SET {column}=? WHERE id=?",  # nosec B608 - literal column
                (value, case.task_id),
            )
            await other.commit()

        err = await _between(db, case, edit)
    finally:
        await other.close()

    assert err is not None, column
    assert "statement:" in str(err.detail), (column, err.detail)
    assert await verdict_of(db, case.task_id) == ""


async def test_a_statement_edit_right_after_the_packet_is_built_cancels_the_exception(
    db, db_dsn, monkeypatch
):
    """Окно между сборкой пакета и снятием отпечатка: согласие о ПРЕЖНЕЙ постановке."""
    from hub.services import steward_apply

    case = await build_case(db, monkeypatch, "dc3-statement-window")
    other = await db_module.connect(db_dsn)
    real = steward_apply.build_evidence_packet

    async def edit_after_build(conn, task_id, generation=None):
        packet = await real(conn, task_id, generation)
        await other.execute(
            "UPDATE tasks SET title='правка сразу после сборки' WHERE id=?",
            (task_id,),
        )
        await other.commit()
        return packet

    monkeypatch.setattr(steward_apply, "build_evidence_packet", edit_after_build)
    try:
        err = await _between_unchecked(db, case)
    finally:
        await other.close()

    assert err is not None
    assert "statement:" in str(err.detail), err.detail
    assert await verdict_of(db, case.task_id) == ""


async def _between_unchecked(db, case):
    """Как ``_between``, но ранний этап не обязан быть пустым: проверяется итог записи."""
    from fastapi import HTTPException

    from hub.models import ReviewVerdict, TaskReviewVerdict
    from hub.services.lifecycle import record_review_verdict
    from hub.services.steward_apply import ceiling_scope

    async with ceiling_scope():
        await apply_refusals(db, case.task_id, 1)
        try:
            await record_review_verdict(
                db,
                case.task_id,
                TaskReviewVerdict(agent="steward", verdict=ReviewVerdict.approved),
                expected_generation=1,
                applied_by_steward=True,
            )
        except HTTPException as err:
            return err
    return None
