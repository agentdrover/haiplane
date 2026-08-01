"""Machine-resolvable test locators for acceptance criteria (#505).

An AC with ``verifiable_by=test`` should point at a concrete test via a pytest
nodeid stored in ``test_ref`` — ``path/to/test_file.py::test_name[::Class][params]``
— instead of a free-text hint. This module parses and validates that locator so
downstream layers can check the test exists (#506) and run it (#507). It never
mutates data: the ``AcceptanceCriterion`` model stays permissive so reading
legacy rows can never fail; enforcement lives only in the refine path and is
gated by ``config.SDD_AC_LOCATOR``.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

# A pytest nodeid: a .py path, "::", then one or more dotted node segments,
# optionally a "[param-id]" suffix. Kept deliberately permissive on the param
# body (pytest allows spaces, dashes, unicode) but strict on the shape.
_TEST_LOCATOR_RE = re.compile(
    r"^[\w./\-]+\.py::[A-Za-z_]\w*(::[A-Za-z_]\w*)*(\[.+\])?$"
)

_EXAMPLE = "tests/test_poller.py::test_ci_absent_dispatches_review"


def is_valid_test_locator(value: str | None) -> bool:
    """Whether ``value`` is a well-formed pytest nodeid."""
    return bool(value and _TEST_LOCATOR_RE.match(value.strip()))


def parse_test_locator(value: str | None) -> tuple[str, str] | None:
    """Best-effort split into ``(file_path, nodeid)``; ``None`` if not a nodeid.

    Best-effort by design (#505 AC-3): a legacy free-text ``test_ref`` simply
    returns ``None`` instead of raising, so existing rows keep parsing.
    """
    if not is_valid_test_locator(value):
        return None
    nodeid = value.strip()
    path = nodeid.split("::", 1)[0]
    return path, nodeid


def _verifiable_by(ac: Any) -> str:
    vb = getattr(ac, "verifiable_by", None)
    return getattr(vb, "value", vb)


def _ac_id(ac: Any) -> str:
    return getattr(ac, "id", "?")


def validate_test_locators(acs: Any, *, enforce: bool) -> None:
    """Reject verifiable_by=test AC without a valid locator when ``enforce`` (#505).

    Applied to every AC WRITE path, never to DB reads. It used to guard only
    the bulk-refine payload, so the single add/upsert/replace calls let an
    unresolvable locator through with the policy set to require — 425 of the
    695 stored locators were unresolvable by the time that was noticed (#596).

    Never on reads: those 425 rows must keep loading. When ``enforce`` is
    False this is a no-op, so off/warn behaviour is unchanged. Non-test AC
    (manual/log_check/ui_check) never require a locator (AC-2).
    """
    if not enforce or not acs:
        return
    bad = [
        _ac_id(ac)
        for ac in acs
        if _verifiable_by(ac) == "test"
        and not is_valid_test_locator(getattr(ac, "test_ref", None))
    ]
    if bad:
        # A comma-separated list is the most common near-miss, and the plain
        # "no valid locator" wording reads as if the value were missing rather
        # than plural. Naming it stops the author guessing (#596).
        listish = [
            _ac_id(ac)
            for ac in acs
            if _ac_id(ac) in bad and "," in (getattr(ac, "test_ref", None) or "")
        ]
        extra = ""
        if listish:
            extra = (
                f" Criteria {', '.join(listish)} name several tests separated by "
                "commas: test_ref holds exactly ONE nodeid. Point it at the test "
                "that proves the criterion and mention the others in the 'then' "
                "text."
            )
        raise HTTPException(
            422,
            f"acceptance criteria {', '.join(bad)} have verifiable_by=test but "
            "no valid pytest test locator in test_ref. Provide a pytest nodeid, "
            f"e.g. '{_EXAMPLE}'.{extra} The whole request was rejected: no "
            "fields were written, not even the ones that validated, and in a "
            "bulk refine no task in the batch was touched — resend the whole "
            "request once the locators are in place.",
        )
