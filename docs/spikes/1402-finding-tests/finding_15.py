"""_claims_green treats a negated rc=0 as a green claim (#1246).

_EXIT_ZERO_RE hits first and never consults _NEGATION_RE, so «not rc=0»
becomes author_claims; a failed prepass then sets discrepancy=True even
though the text did not claim green.
"""

from __future__ import annotations

from hub.models import PrepassState
from hub.services import review_evidence

_NEGATED_ZERO = (
    "not rc=0",
    "tests were not rc=0",
    "did not exit code 0",
    "expected rc=0, got rc=1",
)


def test_a_negated_exit_zero_is_not_a_green_claim():
    wrong = [text for text in _NEGATED_ZERO if review_evidence.author_green_claims(text)]
    assert not wrong, (
        f"_EXIT_ZERO_RE matched through negation, so these became author_claims: {wrong}"
    )


def test_negated_rc_zero_is_not_a_discrepancy_when_prepass_failed():
    prepass = PrepassState(
        state="failed",
        failed=["tests"],
        passed=[],
        skipped=[],
        head_sha="deadbeefdead",
        reason="tests failed",
    )
    standing = review_evidence.validation_standing(prepass, "tests were not rc=0")
    assert standing.verified is False
    assert standing.author_claims == []
    assert standing.discrepancy is False, (
        "discrepancy is only when the text claims green; a negated rc=0 was "
        "counted as a claim because _claims_green returns on _EXIT_ZERO_RE "
        "without looking at _NEGATION_RE"
    )
