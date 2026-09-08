"""Unit tests for machine-resolvable AC test locators (#505)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from hub.services.test_locator import (
    RUNNERS,
    is_valid_test_locator,
    parse_test_locator,
    runner_of,
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


# ---- The hub knows more than one test runner (#1203) ----


def test_accepted_shape_always_has_a_resolver():
    # AC-2. Not a style rule. Widening the accepted shape without teaching the
    # hub to look inside that kind of file was MEASURED to be worse than
    # refusing outright: with the shape widened alone, resolve_ac_locators
    # answered `missing`, "locator does not match any collected test", about a
    # test that plainly existed in the repository. An honest "I do not know
    # this form" became a false accusation (#725, #498).
    #
    # So the two registries are bound together here: a runner added to RUNNERS
    # with no entry in SOURCE_RESOLVERS fails this test rather than shipping.
    from hub.services.test_existence import SOURCE_RESOLVERS

    for runner in RUNNERS:
        assert runner.name in SOURCE_RESOLVERS, (
            f"runner {runner.name} is accepted by the locator shape but nothing "
            "can look inside its files — that combination reports existing "
            "tests as missing"
        )
        # The registry must also be self-consistent: an entry whose own example
        # does not parse as its own runner would let a shape through that no
        # resolver is ever handed.
        assert runner_of(runner.example) == runner.name
        assert is_valid_test_locator(runner.example)


def test_vitest_locators_are_accepted_with_free_text_names():
    # A vitest test name is whatever was passed to it()/test(): spaces,
    # punctuation, non-Latin scripts. Carrying pytest's identifier rule across
    # would reject nearly every real name while looking like a shape check.
    assert is_valid_test_locator("src/lib/recent.test.ts::still toggles")
    assert is_valid_test_locator("src/a.test.tsx::раскрывает подсписок")
    assert is_valid_test_locator("src/a.spec.js::renders <Foo /> once")
    assert runner_of("src/a.test.ts::x") == "vitest"
    # An empty or blank node names no test, whatever the runner.
    assert not is_valid_test_locator("src/a.test.ts::")
    assert not is_valid_test_locator("src/a.test.ts::   ")
    # A file no runner in the registry owns is still not a locator.
    assert not is_valid_test_locator("docs/testing.md::some heading")
    assert runner_of("docs/testing.md::some heading") == ""


@pytest.mark.parametrize(
    "value, ok",
    [
        ("tests/test_poller.py::test_ci_absent", True),
        ("hub/tests/test_x.py::TestClass::test_method", True),
        ("tests/test_a.py::test_b[param-1]", True),
        ("tests/test_a.py::test_b[id with spaces]", True),
        ("tests/test_a.py", False),
        ("tests/test_a.py::", False),
        ("test_ci", False),
        ("some free text hint", False),
        ("", False),
        (None, False),
    ],
)
def test_python_locators_unchanged(value, ok):
    # AC-4. Adding a second runner must be observably empty for Python
    # projects: the same answers, and the same (path, nodeid) split. This
    # repeats the table above on purpose — if a future change to the shared
    # shape machinery moves a Python answer, the criterion that promised
    # nothing would move has to fail, not the general table alone.
    assert is_valid_test_locator(value) is ok
    assert runner_of(value) in ("pytest", "")
    if ok:
        assert parse_test_locator(value) == (value.split("::", 1)[0], value)
    else:
        assert parse_test_locator(value) is None
