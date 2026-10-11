"""#1246: проза провала с rc=0 / отрицание после green-слова — не заявление о зелёном."""

from __future__ import annotations

from hub.models import PrepassState
from hub.services import review_evidence

_NOT_GREEN = (
    "expected rc=0, got rc=1",
    "зелёный не получен",
    "All checks passed не вышло",
    "green not confirmed",
)


def test_failure_prose_is_not_a_green_claim():
    wrong = [text for text in _NOT_GREEN if review_evidence.author_green_claims(text)]
    assert wrong == [], (
        "текст не утверждает зелёное, а описывает провал, но _claims_green=True: "
        f"{wrong}"
    )


def test_failed_prepass_does_not_discrep_on_expected_rc0_got_rc1():
    standing = review_evidence.validation_standing(
        PrepassState(state="failed", failed=["tests"], head_sha="deadbeefcafe"),
        "expected rc=0, got rc=1",
    )
    assert standing.verified is False
    assert standing.author_claims == []
    assert standing.discrepancy is False, (
        "discrepancy только когда текст утверждает зелёное; "
        "«expected rc=0, got rc=1» — описание провала, не claim"
    )
