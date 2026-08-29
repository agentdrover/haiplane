"""Reading a verdict's own words back to it (#1057).

Two input errors landed on production within one day, both from the form a
human types verdicts into, and the hub took both in silence:

* #1042 12:41 — a byte-for-byte re-paste of the 12:21 verdict, whose complaint
  the author's resubmission had already closed 18 minutes earlier. The two came
  through different fields (findings the first time, comments the second), so
  only the reviewer's words match, not the payload shape.
* #1041 06:33 — recorded as CHANGES_REQUESTED with a body opening on the word
  APPROVED. The reviewer said so himself in the next verdict.

Both are visible at write time from data the hub already holds. Neither is a
judgement about the review: what is compared is text the reviewer wrote, and
what is refused is a contradiction inside one call.
"""

from __future__ import annotations

import re

# The hub's own echo of a finding in the feed: "1. [medium] the words". It is
# rendering, not what the reviewer wrote, so it comes off before comparing —
# otherwise the same sentence sent as a finding and as a comment reads as two
# different verdicts, which is exactly how #1042 slipped through.
_FINDING_ECHO = re.compile(r"^\s*\d+\.\s*\[[^\]]*\]\s*")

APPROVED_WORD = "approved"
CHANGES_WORDS = ("changes_requested", "changes requested")


def verdict_body(comments: str, finding_messages: list[str]) -> str:
    """The reviewer's words, whichever field carried them."""
    parts = [comments or ""]
    parts.extend(finding_messages or [])
    return "\n".join(p for p in parts if p.strip())


def fingerprint(text: str) -> str:
    """Normalise for COMPARISON only — the stored text is never touched.

    Line endings, trailing spaces, case and the finding echo are rendering.
    A re-paste differs from its original in exactly those, and in nothing a
    reviewer would call a change of mind.
    """
    lines = []
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(_FINDING_ECHO.sub("", raw).split()).strip().lower()
        if line:
            lines.append(line)
    return "\n".join(lines)


def declared_outcome(text: str) -> str | None:
    """The outcome the body ANNOUNCES, or None when it announces nothing.

    Only the first meaningful line counts. A verdict that quotes the word
    APPROVED while discussing the last round is doing its job; a verdict that
    opens with it while being filed as changes_requested is a typo, and the
    difference between the two is where the word sits.
    """
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(_FINDING_ECHO.sub("", raw).split()).strip().lower()
        if not line:
            continue
        if line.startswith(APPROVED_WORD):
            return "approved"
        for word in CHANGES_WORDS:
            if line.startswith(word):
                return "changes_requested"
        return None
    return None


def previous_verdict_body(content: str) -> str:
    """A stored verdict update, minus the "Review verdict: X" line the hub adds.

    The rest of the line — notes the hub appends about a diverged tip or
    undisposed findings — stays in the comparison. When such a note is present
    on one side only the fingerprints differ and the repeat check does not
    fire: a miss, and a miss is the right way round. Refusing a verdict that
    only looks like a repeat would be the hub overruling a reviewer.
    """
    lines = (content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[0].strip().lower().startswith("review verdict:"):
        lines = lines[1:]
    return "\n".join(lines)
