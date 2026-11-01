"""Whether a submission is checked, and the author's word about runs (#1246, #2208).

Two facts live here, and neither is judged by the text of the submission:

* the author's lines about runs are QUOTED, not interpreted — three review
  rounds taught that no regular expression can tell "green claimed" from
  "failure described", so the comparison goes to a human (#1246);
* the standing itself is read from the prepass the hub ran — checks AND the
  card's validation_commands, which are two facts about one commit (#2208).

Split out of ``review_evidence`` when that module reached the size ceiling; the
functions are unchanged, and its callers import them from here.
"""

from __future__ import annotations

import re

# --- The prepass over the author's word (#1246) ------------------------------
#
# Spike #1168 went to review with a FAILED prepass while its text listed green
# runs of lint, security and the budget; the full suite was never run. The hub
# cannot see how an author ran a command, so an "rc=0" in a submission stays a
# word. The rule: the prepass the hub ran is the only basis for "checked"; the
# author's lines about runs stay visible, quoted as his.
#
# The hub does NOT judge what those lines mean. Three review rounds tried to
# tell "green claimed" from "failure described" with regular expressions and
# each round found a new negation ("0 passed, 3 failed", "rc=0 не получен",
# "rc=0, no failures", "expected rc=0, got rc=1"). The owner's decision of
# 24.09: extract broadly, quote, and hand the comparison to a human whenever
# the prepass failed and the author wrote about runs at all.

AUTHOR_WORD = "СЛОВО АВТОРА"

# Broad on purpose: any line that talks about a run, whatever it says of it.
_RUN_WORD_RE = re.compile(
    r"\brc\s*=|\bexit(?:\s+code)?\b|\bpassed\b|\bfailed\b|\bgreen\b|"
    r"зел[её]н|all checks",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_RE = re.compile(r"[;\n]|\.\s")
_MAX_CLAIMS = 5
_MAX_CLAIM_LEN = 100

VALIDATION_PASSED = "passed"
VALIDATION_FAILED = "failed"
VALIDATION_NOT_RUN = "not_run"
#: Checks ran and passed, but the card's validation_commands gave no outcome
#: (#2208). Neither a pass nor a failure: the fourth answer, not a synonym.
VALIDATION_UNKNOWN = "unknown"
#: The outcomes that make the submission NOT checked while checks stayed green.
_UNCHECKED_VALIDATION_WORDS = {
    "fail": "validation_commands: fail",
    VALIDATION_UNKNOWN: "validation_commands: неизвестно",
    # #2208: an answer exists, but for another command list.
    "stale": ("validation_commands: результат получен для других validation_commands"),
}


def author_run_lines(text: str) -> list[str]:
    """The clauses of a submission text that talk about a run — his word.

    No judgement of meaning: a clause is taken when it names a run at all.
    """
    out: list[str] = []
    for clause in _CLAUSE_SPLIT_RE.split(text or ""):
        clause = clause.strip()
        if clause and _RUN_WORD_RE.search(clause) and clause not in out:
            out.append(clause[:_MAX_CLAIM_LEN])
        if len(out) >= _MAX_CLAIMS:
            break
    return out


def _recorded_validation(prepass) -> tuple[str, str]:
    """``(status, why)`` of the card's validation_commands on this commit (#2208).

    ``("", "")`` is "not applicable": the card names no commands, so their
    absence neither proves nor accuses anything. Everything else is a real
    outcome — ``pass``, ``fail``, or ``unknown`` when the report named none.
    """
    if not getattr(prepass, "validation_declared", False):
        return "", ""
    status = str(getattr(prepass, "validation_status", "") or "").strip()
    why = str(getattr(prepass, "validation_reason", "") or "").strip()
    if status == "pass":
        return "pass", ""
    if status in ("fail", "stale"):
        return status, why
    return VALIDATION_UNKNOWN, why or "отчёт CI не назвал итог validation_commands"


def validation_standing(prepass, submission_text: str):
    """Whether the submission is checked: the prepass decides, the text does not.

    Checks and validation are TWO facts about one commit (#2208): a green
    ``checks`` list beside a red ``validation_commands`` is not "checked", and
    the brief names both halves instead of generalising from the flattering one.
    ``not_run`` (nothing ran) and ``unknown`` (checks ran, validation gave no
    outcome) stay apart for the same reason: a missing answer is not a failure
    and is not a pass. ``needs_human_compare`` is set only where the prepass
    FAILED and the author wrote about runs — the hub cannot tell whether his
    lines agree with the failure, so a human compares them.
    """
    from hub.models import ValidationStanding

    lines = author_run_lines(submission_text)
    word = f"{AUTHOR_WORD}: «{'; '.join(lines)}»" if lines else ""
    sha = (prepass.head_sha or "")[:12] or "—"
    if prepass.state == "covered":
        checks = f"checks прошли: {', '.join(prepass.passed)}"
        status, why = _recorded_validation(prepass)
        if status in _UNCHECKED_VALIDATION_WORDS:
            headline = (
                f"Сдача НЕ проверена: {checks}, но "
                f"{_UNCHECKED_VALIDATION_WORDS[status]}"
                + (f" — {why}" if why else "")
                + ". Проверки кода этого не отменяют и не заменяют."
            )
            if lines:
                headline += f" {word} — основанием не служит."
            return ValidationStanding(
                state=VALIDATION_FAILED if status == "fail" else status,
                author_run_lines=lines,
                headline=headline,
            )
        headline = (
            f"Сдача проверена предпасом хаба на {sha}: прошли "
            f"{', '.join(prepass.passed)}."
        )
        if status == "pass":
            headline += " validation_commands: прошли."
        if lines:
            headline += f" {word} — основанием не служит."
        return ValidationStanding(
            state=VALIDATION_PASSED,
            verified=True,
            author_run_lines=lines,
            headline=headline,
        )
    if prepass.state == "failed":
        headline = (
            f"Сдача НЕ проверена: предпас хаба на {sha} упал "
            f"({', '.join(prepass.failed)})."
        )
        if lines:
            headline += (
                f" Предпас упал; автор пишет: {word}. "
                "Сверяет человек — смысл строк хаб не судит."
            )
        return ValidationStanding(
            state=VALIDATION_FAILED,
            author_run_lines=lines,
            needs_human_compare=bool(lines),
            headline=headline,
        )
    headline = (
        f"Сдача НЕ проверена: предпас не запускался — {prepass.reason}. "
        "Это не «прошёл» и не «упал»."
    )
    if lines:
        headline += f" {word} — предпасом не подтверждено."
    return ValidationStanding(
        state=VALIDATION_NOT_RUN, author_run_lines=lines, headline=headline
    )
