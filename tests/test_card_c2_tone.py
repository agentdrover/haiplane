"""Тон карточки C2 (#1812): светофор считается из справки, а не из статуса.

Чистая функция ``card_c2.card_view`` без базы. Главное правило одно: зелёный
значит «доказано» (доставка подтверждена либо состояние принято), а отсутствие
данных, сомнение в проверках и недоверенное - никогда не зелёное.
"""

from __future__ import annotations

import pytest

from hub.services import card_c2, decision_brief
from hub.services.decision_brief import (
    ACCEPT,
    CHECK,
    DELIVERED,
    HUMAN_CALL,
    MERGED_NOT_SHIPPED,
    NO_ACTION,
    REWORK,
    RERUN,
    WORKING,
)


def _brief(action, *, why=None, checks=None, risks=None, proven=False, alt=""):
    rec = {"action": action, "verdict": "", "why": why or ["причина"]}
    if alt:
        rec["alt"] = alt
    return {
        "accept_proven": proven,
        "happened": "что-то произошло",
        "need": "что-то нужно",
        "risks": risks or [],
        "recommend": rec,
        "checks": checks or [],
    }


def _view(brief, status="review", **kw):
    task = {"id": 7, "status": status, "title": "Заголовок", "result_kind": "commit"}
    task.update(kw.pop("task", {}))
    return card_c2.card_view(task, brief, **kw)


def test_green_only_with_proven_delivery_or_accepted_state():
    delivered = _view(_brief(DELIVERED), "completed", delivery={"state": "in_prod"})
    assert delivered["tone"] == "green"
    assert delivered["tone_label"] == "Доставлено"

    state = _view(_brief(DELIVERED), "completed", task={"result_kind": "state"})
    assert state["tone"] == "green"
    assert state["tone_label"] == "Принято"

    # Справка сказала «доставлено», а доставка не доказана - не зелёный.
    unproven = _view(_brief(DELIVERED), "completed", delivery=None)
    assert unproven["tone"] != "green"
    unknown = _view(_brief(DELIVERED), "completed", delivery={"state": "unknown"})
    assert unknown["tone"] != "green"


@pytest.mark.parametrize(
    "action, tone",
    [
        (REWORK, "red"),
        (CHECK, "amber"),
        (HUMAN_CALL, "amber"),
        (MERGED_NOT_SHIPPED, "amber"),
        (ACCEPT, "blue"),
        (RERUN, "blue"),
        (WORKING, "blue"),
        (NO_ACTION, "gray"),
    ],
)
def test_tone_per_recommendation(action, tone):
    proven = action == ACCEPT
    got = _view(_brief(action, proven=proven))
    assert got["tone"] == tone, action
    assert got["tone_label"]
    assert got["shape"], "форма значка: тон не держится на цвете одном"


def test_accept_with_doubt_or_unproven_is_not_calm():
    doubt = [{"name": "CI", "state": "doubt", "detail": "не доверен"}]
    assert _view(_brief(ACCEPT, proven=True, checks=doubt))["tone"] == "amber"
    assert _view(_brief(ACCEPT, proven=False))["tone"] == "amber"


def test_review_in_flight_wins_over_a_stale_recommendation():
    got = _view(_brief(CHECK), in_flight=True)
    assert got["tone"] == "blue"
    assert "проверк" in got["tone_label"].lower()


def test_missing_brief_is_gray_and_says_so():
    got = _view(None, "review")
    assert got["tone"] == "gray"
    assert "не собран" in got["tone_label"]
    assert got["recommend"] is None


def test_shapes_differ_per_tone():
    shapes = {
        _view(_brief(a, proven=a == ACCEPT), delivery={"state": "in_prod"})["shape"]
        for a in (REWORK, CHECK, WORKING, NO_ACTION)
    }
    shapes.add(
        _view(_brief(DELIVERED), "completed", delivery={"state": "in_prod"})["shape"]
    )
    assert len(shapes) == 5


def test_lights_follow_check_states_and_never_hide_a_doubt():
    checks = [
        {"name": "CI", "state": "ok", "detail": "зелёный"},
        {"name": "Машинное ревью", "state": "doubt", "detail": "отчётов нет"},
    ]
    got = _view(_brief(CHECK, checks=checks))
    assert [light["name"] for light in got["lights"]] == ["CI", "Машинное ревью"]
    assert got["lights"][1]["tone"] == "amber"
    assert got["lights"][0]["shape"] != got["lights"][1]["shape"]
    assert got["lights_summary"] == "1 из 2"


def test_about_uses_plain_summary_only():
    own = _view(_brief(CHECK), plain_summary="Простыми словами")
    assert own["about"] == "Простыми словами"
    assert not own["about_missing"]
    empty = _view(_brief(CHECK), plain_summary="  ")
    assert empty["about"] == "Простым языком не описано"
    assert empty["about_missing"]
    assert empty["original_title"] == "Заголовок"


def test_header_has_number_and_human_project_name():
    got = _view(_brief(CHECK), project_name="Мой дом")
    assert got["number"] == 7
    assert got["project"] == "Мой дом"
    assert _view(_brief(CHECK))["project"] == ""


def test_the_module_uses_the_brief_vocabulary():
    # Словарь действий один: справка владеет им, карточка его не копирует.
    assert card_c2.DELIVERED is decision_brief.DELIVERED
