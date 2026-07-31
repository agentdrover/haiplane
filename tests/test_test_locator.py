"""Unit tests for machine-resolvable AC test locators (#505)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from hub.services.test_locator import (
    is_valid_test_locator,
    parse_test_locator,
    validate_test_locators,
)


@pytest.mark.parametrize(
    "value, ok",
    [
        ("tests/test_poller.py::test_ci_absent", True),
        ("hub/tests/test_x.py::TestClass::test_method", True),
        ("tests/test_a.py::test_b[param-1]", True),
        ("tests/test_a.py::test_b[id with spaces]", True),
        ("tests/test_a.py", False),  # no node
        ("tests/test_a.py::", False),  # empty node
        ("test_ci", False),  # not a path
        ("some free text hint", False),
        ("", False),
        (None, False),
    ],
)
def test_is_valid_test_locator(value, ok):
    assert is_valid_test_locator(value) is ok


def test_parse_test_locator_best_effort():
    # A valid nodeid splits into (path, nodeid).
    assert parse_test_locator("tests/test_a.py::test_b") == (
        "tests/test_a.py",
        "tests/test_a.py::test_b",
    )
    # AC-3: legacy free text parses best-effort to None, never raises.
    assert parse_test_locator("legacy free-text ref") is None
    assert parse_test_locator(None) is None


class _AC:
    def __init__(self, ac_id, verifiable_by, test_ref):
        self.id = ac_id
        self.verifiable_by = verifiable_by
        self.test_ref = test_ref


def test_validate_test_locators_noop_when_not_enforced():
    # Default-off policy: never rejects, whatever the locator.
    validate_test_locators([_AC("AC-1", "test", None)], enforce=False)


def test_validate_test_locators_allows_non_test_ac():
    # AC-2: manual/log_check/ui_check never require a locator.
    validate_test_locators(
        [_AC("AC-1", "manual", None), _AC("AC-2", "ui_check", None)],
        enforce=True,
    )


def test_validate_test_locators_rejects_invalid_test_ac():
    # AC-1: an enforced verifiable_by=test AC without a valid locator is 422.
    good = _AC("AC-1", "test", "tests/test_a.py::test_b")
    bad = _AC("AC-2", "test", None)
    with pytest.raises(HTTPException) as exc:
        validate_test_locators([good, bad], enforce=True)
    assert exc.value.status_code == 422
    assert "AC-2" in exc.value.detail
    assert "AC-1" not in exc.value.detail


# ---- The rejection must own up to discarding the whole request (#573) ----


def test_locator_rejection_says_nothing_was_written():
    # AC-1 (#573): the refusal is total — the structured-field write is rolled
    # back with the criteria. Naming only the offending AC reads as a partial
    # failure, so a caller re-sends just the criteria and believes the rest
    # landed. The message has to say the whole request was discarded.
    with pytest.raises(HTTPException) as exc:
        validate_test_locators([_AC("AC-1", "test", None)], enforce=True)
    detail = exc.value.detail.lower()
    assert "no fields were written" in detail
    assert "resend the whole request" in detail


async def test_rejected_refine_leaves_structured_fields_untouched(client, monkeypatch):
    # AC-2 (#573): prove the claim the message makes, rather than trusting it.
    # A message can honestly promise a rollback while the code writes anyway —
    # asserting only the text would cover nothing. This also guards the
    # reordering in #573: validation moved ahead of the write, and the batch
    # path shares the same function.
    monkeypatch.setattr("hub.config.SDD_AC_LOCATOR", "require")
    task = (await client.post("/api/tasks", json={"title": "t"})).json()

    ok = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"problem_statement": "written before the rejection"},
    )
    assert ok.status_code == 200, ok.text

    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={
            "problem_statement": "must not survive",
            "business_value": "must not survive either",
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "given": "g",
                    "when": "w",
                    "then": "t",
                    "verifiable_by": "test",
                    "test_ref": None,
                }
            ],
        },
    )
    assert resp.status_code == 422, resp.text

    row = (await client.get(f"/api/tasks/{task['id']}")).json()
    assert row["problem_statement"] == "written before the rejection"
    assert row["business_value"] == ""
