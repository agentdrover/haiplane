"""Recommendations engine for task readiness.

Given a DoR evaluation (#36) and the readiness scoring config (#37),
this module produces a sorted, actionable list of suggestions for the
human or AI Analyst preparing the task.

Design choices:
- We do NOT generate recommendations for risks. Risks already cost
  score in the readiness calculator; suggesting "remove this risk"
  would either be dishonest (you can't wish risks away) or trivial
  ("write a better mitigation"). Mitigation quality lives at task
  authoring level, not in the engine.
- We do NOT use an LLM. Every message is a deterministic template.
  This keeps recommendations cheap, repeatable, and reviewable.
- ``expected_score_delta`` mirrors the ReadinessConfig penalty so the
  numbers shown to a user actually match what the score would become
  after fixing the field.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hub.db import deserialize_str_list
from hub.models import (
    DoRCheckItem,
    Recommendation,
    RecommendationSeverity,
)
from hub.services.dor import DOR_ADVISORY_KEYS, DoREvaluation, evaluate_dor
from hub import repository as repo
from hub.models import ReadinessReport
from hub.services.readiness import (
    DEFAULT_CONFIG,
    ReadinessConfig,
    calculate_score_from_data,
    parse_risks_from_row,
)

# Static templates per check key. ``field`` is the task field a user
# would edit to satisfy the check; ``minutes`` is a rough per-item
# effort hint shown to the author. The total time to refine a task is
# NOT a sum of these numbers — many recommendations share context and
# can be answered together. Treat ``minutes`` as a per-step upper
# bound, never as a scheduling input.
#
# Style guide for ``message``:
#   1) Tell the author what to do (verb-first).
#   2) Tell them WHY in plain English (no internal jargon like
#      "verifiable_by", "DoR", "WIP" without expansion).
#   3) Give a concrete example so the author isn't blocked on form.
CHECK_RECOMMENDATIONS: dict[str, dict[str, Any]] = {
    "has_user_story": {
        "field": "user_story",
        "message": (
            "Add a user story so the developer knows who the change is for "
            "and what outcome they want. Use the form: "
            "'As a <role>, I want <action>, so that <value>.'"
        ),
        "minutes": 5,
    },
    "has_problem_statement": {
        "field": "problem_statement",
        "message": (
            "Describe the problem this task solves and why it matters now, "
            "so the developer can judge trade-offs without asking back."
        ),
        "minutes": 5,
    },
    "has_business_value": {
        "field": "business_value",
        "message": (
            "Explain why this task is worth doing right now. One concrete "
            "outcome is enough — for example: 'unblocks 3 paying customers', "
            "'cuts onboarding time from 10 to 2 minutes', or "
            "'eliminates daily on-call alert about queue X'."
        ),
        "minutes": 3,
    },
    "has_scope_in": {
        "field": "scope_in",
        "message": (
            "List in-scope items (modules, files, behaviors) so the "
            "developer knows where to act and where to stop."
        ),
        "minutes": 5,
    },
    "has_affected_areas": {
        "field": "affected_areas",
        "message": (
            "Name the files or directories this work touches. Four "
            "mechanisms read them: the risk class is derived from them "
            "(#582), the review profile is chosen by that class (#807), "
            "the statement-freshness check compares them against what has "
            "shipped since, and the submit-time check asks whether the diff "
            "stayed inside them. Leave them empty and all four go quiet."
        ),
        "minutes": 3,
    },
    "has_acceptance_criteria": {
        "field": "acceptance_criteria",
        "message": (
            "Define at least one acceptance criterion using Given/When/Then "
            "and say HOW it will be checked (a test name, a CLI command, a "
            "manual UI step, or a metric). This is the contract the "
            "developer will sign off on."
        ),
        "minutes": 10,
    },
    "has_validation_commands": {
        "field": "validation_commands",
        "message": (
            "Add the commands that prove the change actually works "
            "(e.g. 'uv run pytest hub/tests/test_dor.py', "
            "'curl -fsS http://localhost:8765/healthz'). Linters alone "
            "do not count — pick something that exercises behavior."
        ),
        "minutes": 3,
    },
    "has_size": {
        "field": "size",
        "message": (
            "Pick a T-shirt size (XS/S/M/L/XL) so we can plan capacity "
            "and avoid taking on more work than the team can finish."
        ),
        "minutes": 1,
    },
    "has_wip_tag": {
        "field": "wip_tag",
        "message": (
            "Set a wip_tag (feature_work / bugfix / tech_debt / support) "
            "so this task counts against the right capacity bucket."
        ),
        "minutes": 1,
    },
    "has_outcome_hypothesis": {
        "field": "outcome_metric",
        "message": (
            "State which number should move once this ships, and by when — "
            "for example 'median time from task created to first commit, "
            "from 3 days to 1, checked 4 weeks after release'. Without it "
            "the value of the task can be argued but never checked."
        ),
        "minutes": 5,
    },
    "has_redesign_decision": {
        "field": "redesign_decision",
        "message": (
            "Record whether this adapts the current process or reshapes it "
            "(adapt / redesign), and why. Choosing 'adapt' is fine; choosing "
            "it without noticing is how an old process gets automated onto "
            "new technology."
        ),
        "minutes": 3,
    },
    "has_agent_fit": {
        "field": "agent_fit",
        "message": (
            "Say how much agency this work wants: deterministic (scripted), "
            "assistant (a human drives), sdd_native (the spec is the "
            "contract), or agentic (the agent picks the steps). It decides "
            "who does the work, not just how it is written down."
        ),
        "minutes": 2,
    },
}

# Sort order for rendering — blocking first, low last.
SEVERITY_ORDER: dict[RecommendationSeverity, int] = {
    "blocking": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}

# Acceptance-criteria quality heuristics (feedback #6). DoR only checks that
# ACs EXIST, not whether they say anything meaningful — so a task can pass DoR
# with formally-valid but empty-by-meaning criteria. These warnings are
# strictly NON-blocking (severity="low", expected_score_delta=0): they never
# change the score or dor_passed, they just nudge the author. Final quality
# judgement still belongs to the reviewer.
AC_QUALITY_MIN_LEN = 12
_AC_PLACEHOLDER_TOKENS = {
    "tbd",
    "tba",
    "todo",
    "na",
    "n/a",
    "-",
    "--",
    "xxx",
    "?",
    "...",
    "none",
}


def _ac_clause_is_thin(text: str | None) -> bool:
    """True when a Given/When/Then clause is too short or a placeholder.

    Placeholder tokens are checked BEFORE the length cutoff: every token in
    ``_AC_PLACEHOLDER_TOKENS`` is shorter than ``AC_QUALITY_MIN_LEN``, so a
    length-first check would short-circuit and the set would be dead code. We
    also treat a clause built only from placeholder tokens (e.g. "n/a n/a n/a")
    as thin even though it clears the length cutoff.
    """
    t = (text or "").strip().lower()
    if not t:
        return True
    tokens = t.split()
    if t in _AC_PLACEHOLDER_TOKENS or all(
        tok in _AC_PLACEHOLDER_TOKENS for tok in tokens
    ):
        return True
    return len(t) < AC_QUALITY_MIN_LEN


def _row_value(row: Any, key: str) -> str:
    """Read one column from an sqlite Row or a dict, missing key included."""
    try:
        keys = row.keys() if hasattr(row, "keys") else []
        if key not in keys:
            return ""
        return str(row[key] or "")
    except (KeyError, IndexError, TypeError):
        return ""


@dataclass(frozen=True)
class StatementInputs:
    """Everything a statement-defect producer is allowed to look at (#1172).

    One shape for every producer so the set of producers can be checked by
    ENUMERATION rather than by example: a test iterates
    ``STATEMENT_DEFECT_PRODUCERS`` and drives each one through the same call.
    With per-producer signatures such a test could only sample, and a new
    producer speaking outside the vocabulary would pass unnoticed — which is
    the exact failure #1172 exists to prevent.

    ``repo_path`` is the project's working copy (from ``project_git_context``),
    or None when the project declares none. None means "no ground to judge on",
    not "nothing found".
    """

    ac_rows: list[Any] = field(default_factory=list)
    scope_in: list[str] = field(default_factory=list)
    affected_areas: list[str] = field(default_factory=list)
    outcome_metric: str = ""
    repo_path: str | None = None


def build_ac_quality_warnings(inputs: StatementInputs) -> list[Recommendation]:
    """Emit at most one low-severity warning when some ACs look hollow.

    ``inputs.ac_rows`` are rows from ``repo.list_acceptance_criteria`` (columns
    ``ac_id``/``given``/``when_clause``/``then_clause``). Returns an empty
    list when every AC has substantive clauses.
    """
    weak: list[str] = []
    for row in inputs.ac_rows:
        if (
            _ac_clause_is_thin(row["given"])
            or _ac_clause_is_thin(row["when_clause"])
            or _ac_clause_is_thin(row["then_clause"])
        ):
            weak.append(row["ac_id"])
    if not weak:
        return []
    return [
        Recommendation(
            field="acceptance_criteria",
            severity="low",
            message=(
                f"Acceptance criteria {', '.join(weak)} look thin (very short "
                "or placeholder Given/When/Then). The Definition of Ready only "
                "checks that criteria exist, not their quality — strengthen "
                "them so a reviewer can actually sign off."
            ),
            expected_score_delta=0,
            estimated_minutes=5,
            defect_code="ac_clause_thin",
        )
    ]


def build_expectation_source_warnings(inputs: StatementInputs) -> list[Recommendation]:
    """Flag criteria whose expected behaviour has no stated source (#595).

    Strictly non-blocking: severity="low", expected_score_delta=0, no effect
    on dor_passed — the same contract as the AC-quality warnings above. A
    charge here would drop every task in the backlog on the day this ships,
    since no criterion written before today can carry the field.

    ``implementation`` is warned about but not forbidden. Sometimes the code
    is the only source there is; saying so lets a reviewer weigh the
    assertion instead of assuming it came from a requirement. Silence is what
    hides the difference.
    """
    unstated: list[str] = []
    from_code: list[str] = []
    for row in inputs.ac_rows:
        keys = row.keys() if hasattr(row, "keys") else []
        source = row["expectation_source"] if "expectation_source" in keys else None
        if source == "implementation":
            from_code.append(row["ac_id"])
        elif not source:
            unstated.append(row["ac_id"])

    out: list[Recommendation] = []
    if from_code:
        out.append(
            Recommendation(
                field="expectation_source",
                severity="low",
                message=(
                    f"Acceptance criteria {', '.join(from_code)} take their "
                    "expected behaviour from the implementation. That is "
                    "allowed and honestly stated, but a test written from it "
                    "can only confirm what the code already does — including "
                    "a defect. Where a requirement, contract or incident "
                    "exists, derive the expectation from that instead."
                ),
                expected_score_delta=0,
                estimated_minutes=3,
                defect_code="expectation_source_is_implementation",
            )
        )
    if unstated:
        out.append(
            Recommendation(
                field="expectation_source",
                severity="low",
                message=(
                    f"Acceptance criteria {', '.join(unstated)} do not say "
                    "where their expected behaviour comes from. Name the "
                    "source (requirement / contract / incident / bug_report / "
                    "implementation) so a reviewer can tell a checked "
                    "requirement from a restatement of the code."
                ),
                expected_score_delta=0,
                estimated_minutes=2,
                defect_code="expectation_source_unstated",
            )
        )
    return out


# A significant word is at least this long.
_SCOPE_MIN_WORD_LEN = 4

# Russian inflection has to be stripped, not guessed at by prefix length.
#
# The first cut of this matcher kept the first five characters of a word. In
# short words the ending falls INSIDE that window, so «сдаче»/«сдачи» and
# «зонда»/«зондом» — one word each, both pairs taken from live statements —
# looked like different words and a covered scope item got named. The second
# cut answered that by ALSO keeping the first four characters. Measured over
# 435 live statements that window rescued 337 pairs of one word and glued
# together 408 pairs of different words («задание»/«задача», «словарь»/«слово»,
# «разбор»/«разбирает»): it bought silence in the wrong places.
#
# So the ending is removed as an ending. Endings that end in a consonant, or
# run to two or more letters, must leave four characters behind; a lone vowel
# or soft sign — the whole ending of most short nouns — may leave three. The
# tables are deliberately small: this is a nudge worth zero points, not a
# morphological analyser.
_SCOPE_MIN_HARD_STEM = 4
_SCOPE_MIN_SOFT_STEM = 3

_SCOPE_REFLEXIVE_ENDINGS: tuple[str, ...] = ("ся", "сь")

_SCOPE_HARD_ENDINGS: tuple[str, ...] = (
    # adjective and participle
    "ыми",
    "ими",
    "ого",
    "его",
    "ому",
    "ему",
    "ых",
    "их",
    "ый",
    "ий",
    "ой",
    "ым",
    "им",
    "ом",
    "ем",
    "ей",
    "ою",
    "ею",
    "ая",
    "яя",
    "ое",
    "ее",
    "ые",
    "ие",
    "ую",
    "юю",
    # verb
    "ешься",
    "ишься",
    "аться",
    "иться",
    "ать",
    "ять",
    "еть",
    "ить",
    "ыть",
    "уть",
    "ешь",
    "ишь",
    "ете",
    "ите",
    "ают",
    "яют",
    "уют",
    "ует",
    "ют",
    "ат",
    "ят",
    "ит",
    "ет",
    "ла",
    "ло",
    "ли",
    "на",
    "ны",
    "но",
    "л",
    "н",
    # noun
    "иями",
    "ями",
    "ами",
    "иях",
    "ях",
    "ах",
    "ией",
    "иям",
    "ием",
    "ов",
    "ев",
    "ья",
    "ью",
    "ию",
    "ям",
    "ам",
    "ии",
    "ье",
    "ия",
)

_SCOPE_SOFT_ENDINGS: tuple[str, ...] = (
    "а",
    "е",
    "и",
    "о",
    "у",
    "ы",
    "ь",
    "ю",
    "я",
    "й",
)

_CYRILLIC = re.compile(r"[а-я]")

# Latin words are left exactly as the first cut of this matcher left them —
# their first five characters. Russian endings say nothing about them, and
# this change deliberately does not touch behaviour it has no measurement for.
_SCOPE_LATIN_STEM_LEN = 5


def _fold(text: str) -> str:
    """Lower-case, and read ё as е.

    The two letters are written interchangeably in this backlog — the same
    word appears as «поимённо» (#1170, #1161) and as «поименно» (#1144) — so
    treating them as different letters would call a covered item uncovered
    over a diacritic.
    """
    return (text or "").lower().replace("ё", "е")


def _strip_ending(word: str, endings: tuple[str, ...], floor: int) -> str | None:
    """Longest ending from ``endings`` removed, if ``floor`` characters remain."""
    for ending in sorted(endings, key=len, reverse=True):
        if word.endswith(ending) and len(word) - len(ending) >= floor:
            return word[: -len(ending)]
    return None


def _stem(word: str) -> str:
    """The word with one inflectional ending removed."""
    if not _CYRILLIC.search(word):
        return word[:_SCOPE_LATIN_STEM_LEN]
    stem = _strip_ending(word, _SCOPE_REFLEXIVE_ENDINGS, _SCOPE_MIN_HARD_STEM) or word
    cut = _strip_ending(stem, _SCOPE_HARD_ENDINGS, _SCOPE_MIN_HARD_STEM)
    if cut is None:
        cut = _strip_ending(stem, _SCOPE_SOFT_ENDINGS, _SCOPE_MIN_SOFT_STEM)
    if cut is not None:
        stem = cut
    # «доставленная» and «доставлена» are one word; the doubled н of the long
    # participle is the only thing left between their stems.
    if stem.endswith("нн") and len(stem) > _SCOPE_MIN_SOFT_STEM:
        stem = stem[:-1]
    return stem


# Words too common to carry meaning when matching a scope_in item against the
# acceptance criteria. Short tokens are dropped by length before this set is
# consulted, so only longer filler needs listing. They are written as WORDS
# and stemmed here, so the list stays readable and cannot drift away from the
# stemmer: a hand-written stem would silently stop matching the moment the
# endings table changes.
_SCOPE_STOPWORD_FORMS: frozenset[str] = frozenset(
    {
        "this",
        "that",
        "with",
        "from",
        "into",
        "when",
        "then",
        "given",
        "which",
        "should",
        "must",
        "code",
        "только",
        "должно",
        "должны",
        "который",
        "которая",
        "которые",
        "также",
        "чтобы",
        "этого",
        "этому",
        "этом",
        "этот",
    }
)

_SCOPE_STOPWORDS: frozenset[str] = frozenset(
    _stem(_fold(w)) for w in _SCOPE_STOPWORD_FORMS
)


def _significant_stems(text: str) -> set[str]:
    """Stems of the words in ``text`` that carry meaning."""
    words = re.split(r"[^0-9A-Za-zЀ-ӿ]+", _fold(text))
    stems = set()
    for w in words:
        if len(w) < _SCOPE_MIN_WORD_LEN:
            continue
        stem = _stem(w)
        if stem in _SCOPE_STOPWORDS or w in _SCOPE_STOPWORDS:
            continue
        stems.add(stem)
    return stems


def build_scope_coverage_warnings(inputs: StatementInputs) -> list[Recommendation]:
    """Name scope_in items that no acceptance criterion looks at (#1172).

    An area declared in scope with no criterion aimed at it goes to review
    unchecked exactly where the author himself said the work was needed.

    The match is deliberately SOFT: an item counts as covered when ANY of its
    significant words stems to a word used in ANY Given/When/Then. A stricter
    rule (every word, or per-criterion) would flag items that a criterion does
    cover in other words, and the author would learn to skip the warning. It
    is affordable to be soft here precisely because the charge is zero — this
    names a fact, it does not gate anything.

    An item with no significant words at all (e.g. "a") is left alone: there
    is nothing to match on, and silence is honest where judgement is
    impossible.
    """
    if not inputs.scope_in:
        return []
    covered_by: set[str] = set()
    for row in inputs.ac_rows:
        for clause in ("given", "when_clause", "then_clause"):
            covered_by |= _significant_stems(_row_value(row, clause))

    uncovered: list[str] = []
    for item in inputs.scope_in:
        stems = _significant_stems(item)
        if not stems:
            continue
        if stems & covered_by:
            continue
        uncovered.append(item.strip())
    if not uncovered:
        return []
    listed = "; ".join(f"«{item}»" for item in uncovered)
    return [
        Recommendation(
            field="scope_in",
            severity="low",
            message=(
                f"No acceptance criterion appears to look at these scope "
                f"items: {listed}. You declared the work needed there, so "
                "work will reach review unchecked in exactly the place you "
                "named. Either add a criterion or drop the item from scope. "
                "The match is by wording, so a criterion that covers the item "
                "in different words will still show up here — say so and move "
                "on; nothing is blocked."
            ),
            expected_score_delta=0,
            estimated_minutes=5,
            defect_code="scope_item_without_criterion",
        )
    ]


def build_affected_area_warnings(inputs: StatementInputs) -> list[Recommendation]:
    """Name declared affected_areas absent from the repository tree (#1172).

    DoR counts areas and nothing else (dor.py: ``passed = count > 0``), so a
    typo like "hub/services/steward_dor_apply.py" passes readiness and is only
    caught at submission, when the same area is compared against the diff —
    after the work is done.

    NEVER blocks, and says why in the message: a new file is a legitimate
    case, so this code names a fact instead of judging it. The cost of a false
    positive is one line of text, not a stopped gate.

    Silence when the project declares no working copy (``repo_path`` is None),
    or when the path escapes it: there is no tree to check against, and a
    warning would report the hub's own configuration as the author's mistake.
    """
    if not inputs.affected_areas:
        return []
    root_raw = (inputs.repo_path or "").strip()
    if not root_raw:
        return []
    root = Path(root_raw)
    if not root.is_dir():
        return []
    root = root.resolve()

    missing: list[str] = []
    for area in inputs.affected_areas:
        rel = area.strip().lstrip("/")
        if not rel:
            continue
        candidate = (root / rel).resolve()
        if root not in candidate.parents and candidate != root:
            # Escapes the working copy: not something this code can judge.
            continue
        if not candidate.exists():
            missing.append(area.strip())
    if not missing:
        return []
    listed = ", ".join(missing)
    return [
        Recommendation(
            field="affected_areas",
            severity="low",
            message=(
                f"These affected areas are not in the repository tree: "
                f"{listed}. Readiness only counts areas, so a typo passes here "
                "and is caught at submission, when the area is compared "
                "against the diff — after the work is done. A file you are "
                "about to create is a legitimate case and nothing is blocked: "
                "fix the typo, or confirm the file does not exist yet."
            ),
            expected_score_delta=0,
            estimated_minutes=2,
            defect_code="affected_area_not_in_tree",
        )
    ]


def build_outcome_metric_warnings(inputs: StatementInputs) -> list[Recommendation]:
    """Name an outcome_metric that carries no number (#1172).

    "Improve the quality of statements" is not a metric, but the field is
    filled and DoR is satisfied by that alone. The test is the presence of a
    digit, not a parse of a formula: "from 0 to a noticeable share" counts,
    "improve quality" does not.

    An EMPTY metric is not this code's business — that is a missing field, and
    the DoR check ``has_outcome_hypothesis`` already says so. Reporting it
    twice would put one defect under two names, which is the very thing this
    vocabulary exists to prevent.
    """
    metric = (inputs.outcome_metric or "").strip()
    if not metric:
        return []
    if any(ch.isdigit() for ch in metric):
        return []
    return [
        Recommendation(
            field="outcome_metric",
            severity="low",
            message=(
                f"The outcome metric «{metric}» contains no number, so nobody "
                "can tell later whether it was met. Readiness only checks that "
                "the field is filled. Name a value and a direction, e.g. "
                "'median lead time, 3d -> 1d' or 'statement warnings outside "
                "the vocabulary: 0'."
            ),
            expected_score_delta=0,
            estimated_minutes=5,
            defect_code="outcome_metric_without_number",
        )
    ]


# THE producer list. Every statement defect the hub computes is named here and
# nowhere else — a warning built outside this tuple is a second way to name a
# defect, and the count by code goes silently incomplete (#1172, AC-1).
STATEMENT_DEFECT_PRODUCERS: tuple[
    Callable[[StatementInputs], list[Recommendation]], ...
] = (
    build_ac_quality_warnings,
    build_expectation_source_warnings,
    build_scope_coverage_warnings,
    build_affected_area_warnings,
    build_outcome_metric_warnings,
)


def run_statement_defect_producers(inputs: StatementInputs) -> list[Recommendation]:
    """Run every statement-defect producer over one set of inputs.

    Deliberately NOT named ``build_..._warnings``: that name means "a producer"
    everywhere in this module, and the AC-1 test enumerates the module by it.
    An aggregator answering to the same name would be checked as if it were a
    sixth producer.
    """
    out: list[Recommendation] = []
    for producer in STATEMENT_DEFECT_PRODUCERS:
        out.extend(producer(inputs))
    return out


def _recommendation_for(
    check: DoRCheckItem,
    *,
    is_required: bool,
    config: ReadinessConfig,
) -> Recommendation | None:
    """Build a recommendation for one failed DoR check."""
    template = CHECK_RECOMMENDATIONS.get(check.key)
    if template is None:
        return None
    severity: RecommendationSeverity = "blocking" if is_required else "low"
    if check.key in DOR_ADVISORY_KEYS:
        # Advisory checks cost nothing in readiness (#331), so the promised
        # delta must be zero too — a recommendation claiming +5 that never
        # arrives teaches the author to distrust the number.
        delta = 0
    else:
        delta = config.penalty_required if is_required else config.penalty_optional
    return Recommendation(
        field=template["field"],
        severity=severity,
        message=template["message"],
        expected_score_delta=delta,
        estimated_minutes=template["minutes"],
    )


def build_recommendations(
    dor: DoREvaluation,
    *,
    config: ReadinessConfig = DEFAULT_CONFIG,
) -> list[Recommendation]:
    """Build a sorted recommendation list from a DoR evaluation.

    - Failed REQUIRED checks → severity='blocking', delta = penalty_required.
    - Failed OPTIONAL checks → severity='low', delta = penalty_optional.
    - Passed checks → no recommendation (nothing to suggest).
    - Unknown check keys (shouldn't happen) → silently skipped.

    Sort: blocking → high → medium → low (within a severity, original
    DOR_CHECK_KEYS order is preserved for stable rendering).
    """
    recs: list[Recommendation] = []
    for check in dor.checks:
        if check.passed:
            continue
        if check.key in DOR_ADVISORY_KEYS and check.key not in dor.advisory:
            # This work type is not asked for Discovery — stay quiet (#331).
            continue
        rec = _recommendation_for(
            check, is_required=check.key in dor.required, config=config
        )
        if rec is not None:
            recs.append(rec)
    recs.sort(key=lambda r: SEVERITY_ORDER[r.severity])
    return recs


async def _project_repo_path(db, task_id: int) -> str | None:
    """The project's working copy, or None when it declares none (#337).

    Imported inside the function: orchestration reaches back into services,
    and a module-level import here would close the cycle. Any failure to
    resolve returns None, which every producer reads as "no ground to judge
    on" — never as "nothing found".
    """
    try:
        from hub.services.orchestration import project_git_context

        ctx = await project_git_context(db, task_id)
    except Exception:  # pragma: no cover - defensive: never break readiness
        return None
    value = ctx.get("repo")
    return str(value) if value else None


async def build_for_task(
    db,
    task_id: int,
    *,
    config: ReadinessConfig = DEFAULT_CONFIG,
) -> list[Recommendation]:
    """Async wrapper: load the task, evaluate DoR, build recommendations."""
    dor = await evaluate_dor(db, task_id)
    return build_recommendations(dor, config=config)


async def calculate_readiness_with_recommendations(
    db,
    task_id: int,
    *,
    explain: bool = False,
    config: ReadinessConfig = DEFAULT_CONFIG,
) -> ReadinessReport:
    """End-to-end: ReadinessReport with score, dor checks, risks, and
    populated recommendations — single DB roundtrip per data source.

    Lives here (not in readiness.py) to keep readiness free of any
    knowledge of the recommendation engine. The dependency direction
    stays one-way: recommendations -> readiness/dor.
    """
    dor = await evaluate_dor(db, task_id)
    row = await repo.get_task(db, task_id)
    # 'risks' is a guaranteed column post-migrations (review I10).
    risks_raw = row["risks"] if row is not None else None
    risks = parse_risks_from_row(risks_raw)

    score, components = calculate_score_from_data(dor=dor, risks=risks, config=config)
    recs = build_recommendations(dor, config=config)
    # Non-blocking statement-defect nudges (#6, #1172): every one of them is
    # severity="low" with expected_score_delta=0, and NONE of them touches
    # `score` or `dor.passed` above. Charging here would drop the whole
    # backlog retroactively on the day this ships — the mistake declined in
    # #6 and #331.
    ac_rows = await repo.list_acceptance_criteria(db, task_id)
    recs.extend(
        run_statement_defect_producers(
            StatementInputs(
                ac_rows=list(ac_rows),
                scope_in=deserialize_str_list(row["scope_in"]) if row else [],
                affected_areas=(
                    deserialize_str_list(row["affected_areas"]) if row else []
                ),
                outcome_metric=(row["outcome_metric"] or "") if row else "",
                repo_path=await _project_repo_path(db, task_id),
            )
        )
    )
    recs.sort(key=lambda r: SEVERITY_ORDER[r.severity])

    return ReadinessReport(
        score=score,
        dor_passed=dor.passed,
        dor_checks=dor.checks,
        missing_required=sorted(dor.missing_required),
        risks=risks,
        recommendations=recs,
        explain=[c.to_dict() for c in components] if explain else None,
    )


__all__ = [
    "CHECK_RECOMMENDATIONS",
    "SEVERITY_ORDER",
    "STATEMENT_DEFECT_PRODUCERS",
    "StatementInputs",
    "build_ac_quality_warnings",
    "build_affected_area_warnings",
    "build_expectation_source_warnings",
    "build_for_task",
    "build_outcome_metric_warnings",
    "build_recommendations",
    "build_scope_coverage_warnings",
    "calculate_readiness_with_recommendations",
    "run_statement_defect_producers",
]
