"""Матрица действий карточки (#1813): какие кнопки есть в каком состоянии.

Чистая сборка ``hub/services/card_actions.build``: ничего не читает и не пишет.
Каждый лист ведёт ровно на один из существующих человеческих маршрутов; новых
маршрутов матрица не вводит.
"""

from __future__ import annotations

from hub.services import card_actions
from hub.services.decision_brief import ACCEPT, REWORK

ROUTES = {
    "web-approve",
    "web-reject",
    "web-review-verdict",
    "web-decide",
    "web-return-to-work",
}


def _task(status: str, **kw):
    base = {
        "id": 7,
        "status": status,
        "result_kind": "commit",
        "statement_generation": 2,
        "submission_generation": 3,
        "review_verdict": None,
        "review_verdict_generation": None,
        "review_verdict_revision": 0,
        "review_job_id": None,
        "job_id": None,
        "claimed_by": None,
    }
    return {**base, **kw}


def _ids(items):
    return [a["sheet"] for a in items]


def test_every_sheet_posts_to_an_existing_human_route():
    seen = set()
    for status in ("draft", "review", "needs_decision", "running", "fix_requested"):
        t = _task(
            status,
            review_verdict="approved" if status == "running" else None,
            review_verdict_generation=3 if status == "running" else None,
        )
        for sheet in card_actions.build(t)["sheets"]:
            seen.add(sheet["action"].rsplit("/", 1)[-1])
            assert sheet["action"].startswith("/tasks/7/web-")
    assert seen <= ROUTES and seen, seen


def test_draft_offers_approve_and_reject_guarded_by_statement_generation():
    got = card_actions.build(_task("draft"))
    assert _ids(got["actions"]) == ["sh-approve", "sh-reject"]
    for sheet in got["sheets"]:
        assert sheet["hidden"]["expected_statement_generation"] == "2"


def test_review_offers_verdict_pair_with_submission_generation():
    got = card_actions.build(_task("review"))
    assert set(_ids(got["actions"])) == {"sh-cr", "sh-ok"}
    for sheet in got["sheets"]:
        if sheet["action"].endswith("web-review-verdict"):
            assert sheet["hidden"]["expected_generation"] == "3"
            assert "supersedes_revision" not in sheet["hidden"]


def test_recommendation_orders_the_pair_and_none_means_nobody_is_marked():
    rework = card_actions.build(_task("review"), recommend=REWORK)
    assert _ids(rework["actions"])[0] == "sh-cr"
    assert rework["actions"][0]["recommended"] is True
    accept = card_actions.build(_task("review"), recommend=ACCEPT)
    assert _ids(accept["actions"])[0] == "sh-ok"
    unknown = card_actions.build(_task("review"), recommend="")
    assert not any(a["recommended"] for a in unknown["actions"])


def test_high_risk_accept_is_a_separate_sheet_with_confirmation():
    risks = [{"severity": "high", "text": "r"}]
    got = card_actions.build(_task("review"), recommend=ACCEPT, risks=risks)
    assert "sh-ok-risk" in _ids(got["actions"]) and "sh-ok" not in _ids(got["actions"])
    sheet = next(s for s in got["sheets"] if s["id"] == "sh-ok-risk")
    assert sheet["confirm"] is True
    assert sheet["submit_label"].startswith("Принять")


def test_standing_verdict_turns_sheets_into_a_replacement_with_revision():
    t = _task(
        "running",
        review_verdict="changes_requested",
        review_verdict_generation=3,
        review_verdict_revision=4,
    )
    got = card_actions.build(t)
    verdict_sheets = [
        s for s in got["sheets"] if s["action"].endswith("review-verdict")
    ]
    assert verdict_sheets
    for sheet in verdict_sheets:
        assert sheet["hidden"]["supersedes_revision"] == "4"
        assert sheet["hidden"]["expected_generation"] == "3"
    assert any(
        f["name"] == "acknowledge_repeat" for s in verdict_sheets for f in s["fields"]
    )


def test_needs_decision_uses_decide_route_with_expected_state():
    got = card_actions.build(_task("needs_decision"))
    assert set(_ids(got["actions"])) == {"sh-accept", "sh-rework"}
    for sheet in got["sheets"]:
        if sheet["action"].endswith("web-decide"):
            assert sheet["hidden"]["expected_status"] == "needs_decision"
            assert sheet["hidden"]["expected_submission_generation"] == "3"


def test_return_to_work_is_secondary_and_pinned_to_the_state():
    got = card_actions.build(_task("review", claimed_by="bot"))
    assert "sh-return" in _ids(got["more"]) and "sh-return" not in _ids(got["actions"])
    sheet = next(s for s in got["sheets"] if s["id"] == "sh-return")
    assert sheet["hidden"]["expected_status"] == "review"
    assert sheet["hidden"]["expected_submission_generation"] == "3"
    assert any(f["name"] == "reason" and f["required"] for f in sheet["fields"])


def test_states_without_a_human_route_get_no_buttons():
    for status in ("open", "ci_check", "rejected", "needs_info", "failed"):
        got = card_actions.build(_task(status))
        assert got["actions"] == [] and got["sheets"] == [], status


def test_findings_become_pickable_lines_in_the_rework_sheet():
    findings = [
        {"severity": "high", "title": "гонка", "file": "hub/a.py", "line": 12},
        {"severity": "weird", "title": "без места"},
    ]
    got = card_actions.build(_task("review"), findings=findings)
    sheet = next(s for s in got["sheets"] if s["id"] == "sh-cr")
    picks = next(f for f in sheet["fields"] if f["type"] == "findings")["options"]
    assert picks[0]["line"] == "high: гонка @ hub/a.py:12"
    assert picks[1]["line"] == "medium: без места"


def test_live_marker_carries_the_generations_the_sheets_were_built_on():
    live = card_actions.build(_task("review"))["live"]
    assert live == {
        "status": "review",
        "submission_generation": "3",
        "statement_generation": "2",
        "review_verdict_revision": "0",
    }


def test_findings_of_an_earlier_submission_are_not_offered():
    from types import SimpleNamespace

    finding = SimpleNamespace(severity="high", title="старая", file="a.py", line=1)
    stale = SimpleNamespace(is_current=False, findings_confirmed=[finding])
    fresh = SimpleNamespace(is_current=True, findings_confirmed=[finding])
    view = {"recommend": None, "risks": []}
    for report, expected in ((stale, 0), (fresh, 1), (None, 0)):
        got = card_actions.for_page(_task("review"), view, report)
        sheet = next(s for s in got["sheets"] if s["id"] == "sh-cr")
        picks = [f for f in sheet["fields"] if f["type"] == "findings"]
        assert (len(picks[0]["options"]) if picks else 0) == expected


def test_every_sheet_form_says_it_came_from_a_sheet():
    for status in ("draft", "review", "needs_decision"):
        for sheet in card_actions.build(_task(status))["sheets"]:
            assert sheet["hidden"]["c2_sheet"] == "1"
