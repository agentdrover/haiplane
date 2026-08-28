"""Orchestration helpers: dispatch, review, CI fix, arbiter, Vast cleanup."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import commit_scope, config
from hub import repository as repo
from hub.db import deserialize_str_list, fetchall, get_breadcrumb, log_activity
from hub.integrations import git_ops as git_ops_mod
from hub.integrations.git_ops import (
    WorkspaceBranchMismatchError,
    WorkspaceNotReadyError,
)
from hub.integrations.protocols import CIProbeOutcome
from hub.integrations.registry import plugins
from hub.services import workflow_seed
from hub.services.project_policy import (
    base_branch_of,
    ci_runner_of,
    rearm_clone,
    release_base_of,
)

log = logging.getLogger("hub")


async def project_git_context(
    db: aiosqlite.Connection,
    task_id: int,
) -> dict[str, Any]:
    """Git kwargs from the task's project (#337).

    Empty project fields are omitted so git_ops falls back to env — the
    seeded default project behaves exactly like the pre-project hub.
    """
    row = await repo.resolve_project_for_task(db, task_id)
    if row is None:
        return {}
    d = dict(row)
    ctx: dict[str, Any] = {}
    if (d.get("workspace_path") or "").strip():
        ctx["repo"] = d["workspace_path"].strip()
    if (d.get("repo") or "").strip():
        ctx["gh_repo"] = d["repo"].strip()
    if (d.get("default_branch") or "").strip():
        ctx["base_branch"] = d["default_branch"].strip()
    # No special case for the default project (#604). One existed here —
    # an unconditional empty context, written when default had no real
    # fields and "behave like the pre-project hub" was the only correct
    # answer. Once the owner configured a real repo and workspace (#602),
    # that compatibility silently threw the configuration away, and every
    # consumer of ctx["repo"] — locator resolution, the call-sites section,
    # submission-SHA pinning — stayed blind next to a live clone. The
    # field-wise omission above IS the legacy behaviour: an unconfigured
    # project contributes no keys and git_ops falls back to env.
    return ctx


def machine_review_required(task: dict[str, Any], project_policy: str = "auto") -> bool:
    """Machine-review applicability cascade (#382, #806).

    task override > project policy > auto rules from task metadata.
    Computed at submit/verdict time, not at creation — size and risks are
    refined along the way and the decision must see current values.

    The auto rules, in order: a high or security risk always requires review;
    docs, chore and spike are exempt by their nature; everything else — the
    work types that change code — requires it regardless of declared size.
    """
    import json as _json

    override = (task.get("machine_review_override") or "").strip()
    if override == "require":
        return True
    if override == "skip":
        return False
    policy = (project_policy or "auto").strip()
    if policy == "off":
        return False
    if policy == "always":
        return True
    # auto rules: high/security risk always wins
    try:
        risks = _json.loads(task.get("risks") or "[]")
    except ValueError:
        risks = []
    for risk in risks:
        if isinstance(risk, dict) and (
            risk.get("severity") == "high" or risk.get("kind") == "security"
        ):
            return True
    work_type = (task.get("work_type") or "feature").strip()
    if work_type in ("docs", "chore", "spike"):
        return False
    # Size no longer excuses a change to code (#806). The exemption made sense
    # while a human on the gate read the diff himself — a small change could be
    # eyeballed. The owner does not read code, so "small task" now means
    # "nobody looked at all": #522 was S, touched orchestration.py, mcp_server.py
    # and a template, and reached the human gate with no report and no notice
    # that one was missing. Size is also the author's own estimate, and an
    # exemption from oversight resting on self-assessment is exactly what #582
    # ruled out for risk class.
    return True


# --- Reports that carry no evidence the harness ran (#841) -------------
#
# 60 of the 103 reports in the 90-day window on 2026-08-21 were the same
# shape: raw_count 0, no findings on either side, no tokens, one agent —
# all 60 landed inside 36 minutes on 2026-08-19 (harness v7). #750 named
# that shape at intake and left the row in place on purpose: a stamp is
# still evidence of what a client did. What it did NOT do is change how
# the row reads afterwards, and downstream both the gate and the practice
# metrics counted it as a review that ran and found nothing.
#
# Zero candidates is absence of data, not absence of findings. Evidence is
# taken as WIDELY as the stored columns allow — any one of a claimed
# candidate, an adjudicated finding, a self-counted token, a provider-billed
# token (#828) or more than one agent means something ran. Only a report
# with none of them is "no data", so an honest cheap run that found nothing
# but counted its cost still reads as a review.
#
# The condition is written ONCE. Both the gate below and the two metric
# aggregates use this fragment; a copy would drift, and the whole point is
# that the gate and the number agree on what a review is.
REPORT_HAS_EVIDENCE_SQL = (
    "(raw_count > 0 "
    "OR json_array_length(findings_confirmed) > 0 "
    "OR json_array_length(findings_rejected) > 0 "
    "OR COALESCE(tokens_spent, 0) > 0 "
    "OR COALESCE(provider_tokens, 0) > 0 "
    "OR COALESCE(agent_count, 0) > 1)"
)


def report_has_evidence(review: Any) -> bool:
    """The Python twin of :data:`REPORT_HAS_EVIDENCE_SQL`.

    ``review`` is a ``machine_reviews`` row (or a dict of one). Columns are
    read defensively: ``provider_tokens`` arrived in #828, so a row read
    from an older snapshot may simply not have it, and a missing column
    must read as "no such evidence" rather than raise.
    """

    def col(name: str) -> Any:
        try:
            if isinstance(review, dict):
                return review.get(name)
            return review[name]
        except (KeyError, IndexError):
            return None

    def count(name: str) -> int:
        value = col(name)
        if isinstance(value, str):
            import json as _json

            try:
                parsed = _json.loads(value or "[]")
            except ValueError:
                return 0
            return len(parsed) if isinstance(parsed, list) else 0
        return len(value) if isinstance(value, list) else 0

    return bool(
        (col("raw_count") or 0) > 0
        or count("findings_confirmed") > 0
        or count("findings_rejected") > 0
        or (col("tokens_spent") or 0) > 0
        or (col("provider_tokens") or 0) > 0
        or (col("agent_count") or 0) > 1
    )


async def machine_review_gap(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> str | None:
    """None when policy is satisfied; otherwise a human-readable gap reason."""
    project = await repo.resolve_project_for_task(db, task["id"])
    policy = "auto"
    if project is not None and "machine_review" in project.keys():
        policy = project["machine_review"]
    if not machine_review_required(task, policy):
        return None
    generation = task.get("submission_generation") or 0
    mr = await repo.get_latest_machine_review(db, task["id"])
    if mr is None:
        return "machine-review отсутствует для текущего сабмишена"
    if mr["submission_generation"] != generation:
        return "machine-review устарел (работа пересдана) — прогоните харнесс заново"
    if mr["self_reviewed"] and config.REVIEW_SELF_APPROVE != "allow":
        # #728, and the same substitution as the line below: the requirement is
        # that an INDEPENDENT review ran. This is the queue-passing use the
        # statement quotes — "для аудита слабо, для пропуска в очередь — ок" —
        # and it worked because nothing here asked who wrote the report. Solo
        # mode is the one place that answer is allowed to be "the author".
        return (
            "machine-review подан тем же принципалом, который выполнял "
            "задачу: отчёт о собственной работе не заменяет независимое "
            "ревью — прогоните харнесс под другим принципалом"
        )
    if not report_has_evidence(mr):
        # The requirement is that a review RAN, and this row is the record of
        # one that shows no sign of having run. Saying "satisfied" here is the
        # substitution #750 warned about, one step further down the pipe: in
        # 'warn' it silences the panel line the reviewer reads instead of the
        # diff, and in 'require' it would buy an APPROVED verdict outright.
        return (
            "machine-review без данных: ноль кандидатов, ноль находок, "
            "ни одного посчитанного токена — отчёт есть, исполнения не видно "
            "(#750/#841). Прогоните харнесс заново или выносите вердикт сами"
        )
    return None


# What the gate said the findings turned out to be (#877, on #876's data).
#
# ``precision`` is the share of judged findings that were REAL. A defect the
# owner chose not to fix is still a defect the reviewer found, so ``wont_fix``
# counts as a hit; only ``false_positive`` — "the described defect is not in
# the code" — counts against it. ``resolution_rate`` is narrower and is the
# number BugBot publishes: the share that actually got fixed.
#
# Both are computed ONLY over judged findings. A confirmed finding nobody
# judged is not a miss and not a hit, it is an unanswered question, and it is
# reported beside the rates rather than folded into either (#519, #841).
_REAL = ("fixed", "wont_fix")


async def _disposition_metrics(db: aiosqlite.Connection, since: str) -> dict[str, Any]:
    """Precision and resolution, overall and split by profile and model.

    The window is the REPORT's ``created_at``, never the disposition's: a
    judgement made a week after the run belongs to the run it judges. Keying it
    by when someone clicked would let a report and its verdict fall into
    different windows and divide unrelated things by each other.
    """

    def _rates(counts: dict[str, int]) -> dict[str, Any]:
        judged = sum(counts.values())
        real = sum(counts.get(k, 0) for k in _REAL)
        return {
            # The sample size travels with the rate. Two judged findings can
            # produce a precision of 1.0, and a bare 1.0 invites a decision
            # the sample cannot support.
            "judged": judged,
            "fixed": counts.get("fixed", 0),
            "false_positive": counts.get("false_positive", 0),
            "wont_fix": counts.get("wont_fix", 0),
            "precision": round(real / judged, 3) if judged else None,
            "resolution_rate": (
                round(counts.get("fixed", 0) / judged, 3) if judged else None
            ),
        }

    rows = await fetchall(
        db,
        "SELECT CASE WHEN mr.profile = '' THEN 'не заявлен' ELSE mr.profile END "
        "AS profile, "  # nosec B608 - constant fragment, values stay params
        "CASE WHEN mr.model = '' THEN 'не заявлена' ELSE mr.model END AS model, "
        "d.disposition AS disposition, COUNT(*) AS n "
        "FROM finding_dispositions d "
        "JOIN machine_reviews mr ON mr.id = d.review_id "
        "WHERE mr.created_at >= datetime('now', ?) "
        "GROUP BY profile, model, d.disposition",
        (since,),
    )
    overall: dict[str, int] = {}
    by_profile: dict[str, dict[str, int]] = {}
    by_model: dict[str, dict[str, int]] = {}
    for row in rows:
        r = dict(row)
        n = int(r["n"])
        overall[r["disposition"]] = overall.get(r["disposition"], 0) + n
        p = by_profile.setdefault(r["profile"], {})
        p[r["disposition"]] = p.get(r["disposition"], 0) + n
        m = by_model.setdefault(r["model"], {})
        m[r["disposition"]] = m.get(r["disposition"], 0) + n

    # Coverage of the loop itself: how many reports were judged at all, and how
    # many confirmed findings are still waiting for an answer. Without these a
    # precision of 1.0 over two findings out of ninety reads like a verdict on
    # the harness.
    coverage_rows = await fetchall(
        db,
        "SELECT COUNT(*) AS reports, "  # nosec B608 - constant fragment
        "SUM(CASE WHEN judged.n > 0 THEN 1 ELSE 0 END) AS reports_judged, "
        "COALESCE(SUM(json_array_length(mr.findings_confirmed)), 0) AS confirmed, "
        "COALESCE(SUM(judged.n), 0) AS judged "
        "FROM machine_reviews mr LEFT JOIN ("
        "SELECT review_id, COUNT(*) AS n FROM finding_dispositions "
        "GROUP BY review_id) AS judged ON judged.review_id = mr.id "
        "WHERE mr.created_at >= datetime('now', ?)",
        (since,),
    )
    cov = dict(coverage_rows[0]) if coverage_rows else {}
    reports = int(cov.get("reports") or 0)
    reports_judged = int(cov.get("reports_judged") or 0)
    result = _rates(overall)
    result["reports_judged"] = reports_judged
    # Never a share: "0 of 90 judged" and "90 of 90 judged" are the states a
    # reader needs, and a percentage hides which one this is.
    result["reports_unjudged"] = reports - reports_judged
    result["confirmed_unjudged"] = max(
        int(cov.get("confirmed") or 0) - int(cov.get("judged") or 0), 0
    )
    result["by_profile"] = [
        dict(_rates(counts), profile=name)
        for name, counts in sorted(by_profile.items())
    ]
    result["by_model"] = [
        dict(_rates(counts), model=name) for name, counts in sorted(by_model.items())
    ]
    return result


async def _tokens_per_fixed(
    db: aiosqlite.Connection, since: str, column: str
) -> int | None:
    """Tokens per FIXED finding, or None when nothing supports the division.

    Numerator and denominator come from the same rows — the #516 rule that
    understated the cost per confirmed finding by 38% when they did not. A
    report without a token count contributes neither its tokens nor its
    findings, and a report nobody judged contributes nothing at all.
    """
    if column not in ("tokens_spent", "provider_tokens"):  # pragma: no cover
        raise ValueError(f"unsupported token column: {column}")
    rows = await fetchall(
        db,
        f"SELECT COALESCE(SUM(mr.{column}), 0) AS tokens, "  # nosec B608 - column is checked above against a literal allow-list
        "COALESCE(SUM(fixed.n), 0) AS fixed FROM machine_reviews mr "
        "JOIN (SELECT review_id, COUNT(*) AS n FROM finding_dispositions "
        "WHERE disposition = 'fixed' GROUP BY review_id) AS fixed "
        "ON fixed.review_id = mr.id "
        f"WHERE mr.created_at >= datetime('now', ?) AND mr.{column} IS NOT NULL",
        (since,),
    )
    row = dict(rows[0]) if rows else {}
    fixed = int(row.get("fixed") or 0)
    return round(int(row.get("tokens") or 0) / fixed) if fixed else None


# How many DISTINCT tasks a finding category must appear in before it stops
# being a finding and becomes a property of the codebase (#878). Three, because
# two can be one author's habit inside one week; the number is a judgement, and
# the revisit condition on the task watches whether it was the right one.
RECURRENCE_DEBT_THRESHOLD = 3


async def recurring_categories(
    db: aiosqlite.Connection, since: str
) -> list[dict[str, Any]]:
    """Confirmed-finding categories in the window, with how far they spread.

    ``findings`` counts hits, ``tasks`` counts the DISTINCT tasks they landed
    in. The second is the one the debt is built from: ten hits inside one
    sprawling task say something about that task, three hits across three
    tasks say something about the repository.
    """
    rows = await fetchall(
        db,
        "SELECT COALESCE(json_extract(f.value, '$.category'), '') AS category, "
        "COUNT(*) AS findings, COUNT(DISTINCT mr.task_id) AS tasks "
        "FROM machine_reviews mr, json_each(mr.findings_confirmed) f "
        "WHERE mr.created_at >= datetime('now', ?) "
        "GROUP BY category HAVING category != '' "
        "ORDER BY findings DESC LIMIT 50",
        (since,),
    )
    return [dict(r) | {"recurring": r["tasks"] > 1} for r in rows]


async def build_category_debt(
    db: aiosqlite.Connection, recurring: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Which recurring finding categories owe a deterministic check (#878).

    Takes the already-computed recurrence rows so the caller pays for that
    query once. Returns every category over the threshold — covered and open
    alike: a list that silently drops what was closed cannot show that anything
    ever gets closed, and the habit is the thing being measured.
    """
    covered = {
        dict(row)["category"]: dict(row) for row in await repo.list_category_checks(db)
    }
    debt: list[dict[str, Any]] = []
    for row in recurring:
        if row["tasks"] < RECURRENCE_DEBT_THRESHOLD:
            continue
        check = covered.get(row["category"])
        debt.append(
            {
                "category": row["category"],
                "tasks": row["tasks"],
                "findings": row["findings"],
                "covered": check is not None,
                "check_ref": check["check_ref"] if check else "",
                "recorded_by": check["recorded_by"] if check else "",
            }
        )
    return debt


async def record_category_check(
    db: aiosqlite.Connection,
    *,
    category: str,
    check_ref: str,
    note: str = "",
    recorded_by: str = "",
) -> dict[str, Any]:
    """Close a category by naming the check that now covers it (#878).

    ``check_ref`` must name something real — a test nodeid, a lint rule, a CI
    step. A blank one is refused rather than stored: a category closed by a
    tick is a category nobody covered, and the debt list would shrink while
    the token bill stayed exactly where it was.
    """
    category = (category or "").strip()
    check_ref = (check_ref or "").strip()
    if not category:
        raise ValueError("category is required")
    if not check_ref:
        raise ValueError(
            "check_ref is required: name the test, lint rule or CI step that "
            "covers this category — closing it without one only hides the debt"
        )
    await repo.upsert_category_check(
        db,
        category=category,
        check_ref=check_ref,
        note=note,
        recorded_by=recorded_by,
    )
    await repo.insert_event(
        db,
        kind="category_check_recorded",
        actor=recorded_by or "hub",
        payload={"category": category, "check_ref": check_ref},
    )
    await db.commit()
    return {"category": category, "check_ref": check_ref, "covered": True}


async def practice_metrics(
    db: aiosqlite.Connection, *, since_days: int = 90
) -> dict[str, Any]:
    """Practice economics (#384): machine-review costs, filtration rate,
    harness-version comparison, recurring finding categories, cycle times,
    escaped defects.

    Aggregated on the fly from machine_reviews and task timestamps. Cycle time
    is measured from ``completed_at`` (#517) and from nothing else: a row
    without that stamp has no measurable completion, so it is counted in
    ``no_completion_tasks`` rather than estimated from ``updated_at`` (#810).
    Token and duration fields are optional in reports, so aggregates carry
    ``reports_without_tokens`` instead of pretending coverage is full.

    ``reviews`` counts only the reports that carry evidence a harness ran
    (#841) — see :data:`REPORT_HAS_EVIDENCE_SQL`. The rest are counted as
    ``no_data_reports`` and the row count stays available as
    ``reports_total``, so the change is visible rather than silent: the
    90-day window that read "103 reviews" on 2026-08-21 keeps 103 as
    ``reports_total``, with at least the 60 rows of the v7 batch moving to
    ``no_data_reports``.
    """
    import statistics

    since = f"-{since_days} days"

    totals_rows = await fetchall(
        db,
        # `reviews` counts the reports that show a sign of having run; the
        # stamps are counted beside them, never inside them (#841). Both are
        # reported, so `reports_total` still answers "how many rows landed"
        # and no figure moves silently: the 90-day window read on 2026-08-21
        # held 103 rows, at least 60 of them the v7 batch.
        f"SELECT COUNT(*) AS reports_total, "  # nosec B608 - fragment is a module constant, values stay params
        f"SUM(CASE WHEN {REPORT_HAS_EVIDENCE_SQL} THEN 1 ELSE 0 END) AS reviews, "
        f"SUM(CASE WHEN {REPORT_HAS_EVIDENCE_SQL} THEN 0 ELSE 1 END) "
        "AS no_data_reports, "
        "COALESCE(SUM(raw_count), 0) AS raw_total, "
        "COALESCE(SUM(json_array_length(findings_confirmed)), 0) AS confirmed_total, "
        "COALESCE(SUM(json_array_length(findings_rejected)), 0) AS rejected_total, "
        "COALESCE(SUM(tokens_spent), 0) AS tokens_total, "
        "COALESCE(SUM(duration_ms), 0) AS duration_ms_total, "
        # Counted over the reports that ran: a stamp has no cost to omit, and
        # mixing the two made "73 of 103 reports did not count their tokens"
        # read as a harness discipline problem when 60 of those 73 were rows
        # where nothing ran at all (#841).
        f"SUM(CASE WHEN {REPORT_HAS_EVIDENCE_SQL} AND tokens_spent IS NULL "
        "THEN 1 ELSE 0 END) AS reports_without_tokens, "
        # Findings from the reports that actually reported a cost. The ratio
        # below divides by this, not by every confirmed finding in the window.
        "COALESCE(SUM(CASE WHEN tokens_spent IS NOT NULL "
        "THEN json_array_length(findings_confirmed) ELSE 0 END), 0) "
        "AS confirmed_with_tokens, "
        # #828: the provider's own billing, kept apart from the harness's
        # self-report. Never mixed into the same sum — see below.
        "COALESCE(SUM(provider_tokens), 0) AS provider_tokens_total, "
        "SUM(CASE WHEN provider_tokens IS NOT NULL THEN 1 ELSE 0 END) "
        "AS reports_with_provider, "
        "COALESCE(SUM(CASE WHEN provider_tokens IS NOT NULL "
        "THEN json_array_length(findings_confirmed) ELSE 0 END), 0) "
        "AS confirmed_with_provider "
        "FROM machine_reviews WHERE created_at >= datetime('now', ?)",
        (since,),
    )
    totals = dict(totals_rows[0])
    confirmed = totals["confirmed_total"] or 0
    raw = totals["raw_total"] or 0
    # Cost per finding has to take its numerator and denominator from the same
    # rows. Dividing all tokens by ALL confirmed findings mixed reports that
    # reported a cost with reports that did not, and understated the result by
    # 38% on production — 268846 against an honest 433623. A plausible number
    # is worse than a missing one: nobody double-checks a figure that looks
    # right (#516).
    confirmed_with_tokens = totals["confirmed_with_tokens"] or 0
    totals["tokens_per_confirmed"] = (
        round(totals["tokens_total"] / confirmed_with_tokens)
        if confirmed_with_tokens
        else None
    )
    # Filtration is the share of raw findings that did not survive. Computed
    # against the self-reported raw_count it counted findings that were never
    # adjudicated at all as successfully filtered noise — 75 of 192 on
    # production, 39% of the sample, inflating the rate from 0.573 to 0.740.
    # That flatters the harness in exactly the direction nobody questions.
    #
    # The denominator is now what was actually accounted for. The gap is
    # reported beside it rather than hidden: a rate over an unstated fraction
    # of the findings is the same kind of half-truth (#519).
    rejected = totals["rejected_total"] or 0
    adjudicated = confirmed + rejected
    totals["findings_unaccounted"] = max(raw - adjudicated, 0)
    totals["filtration_rate"] = (
        round(1 - confirmed / adjudicated, 3) if adjudicated else None
    )

    # #828: the cost the PROVIDER billed, computed only over the reports that
    # carry it. The harness's own figure is never substituted for a missing
    # one: on the first live cross-model run the two disagreed by 34x
    # (175 000 reported against 6 013 569 billed, #818), so standing one in
    # for the other would repeat #516 — a plausible number nobody re-checks —
    # with a far larger error. Both stay visible; neither is corrected into
    # the other, because it is not yet established which one is wrong.
    confirmed_with_provider = totals["confirmed_with_provider"] or 0
    totals["provider_tokens_per_confirmed"] = (
        round(totals["provider_tokens_total"] / confirmed_with_provider)
        if confirmed_with_provider
        else None
    )

    # #807: the profile split is what tells whether "review every submission"
    # stayed affordable. Kept beside by_harness rather than folded into it:
    # the harness answers "what ran", the profile answers "how much it was
    # allowed to spend".
    profile_rows = await fetchall(
        db,
        "SELECT CASE WHEN profile = '' THEN 'не заявлен' ELSE profile END "
        "AS profile, COUNT(*) AS reports_total, "  # nosec B608 - constant fragment
        f"SUM(CASE WHEN {REPORT_HAS_EVIDENCE_SQL} THEN 1 ELSE 0 END) AS reviews, "
        f"SUM(CASE WHEN {REPORT_HAS_EVIDENCE_SQL} THEN 0 ELSE 1 END) "
        "AS no_data_reports, "
        "COALESCE(SUM(raw_count), 0) AS raw_total, "
        "COALESCE(SUM(json_array_length(findings_confirmed)), 0) "
        "AS confirmed_total, "
        "COALESCE(SUM(tokens_spent), 0) AS tokens_total, "
        # #893: the provider's bill per RUN, and how many runs it covers.
        # The average alone would invite reading a two-run sample as a rate;
        # the sample size beside it says how much the number is worth.
        "COALESCE(SUM(provider_tokens), 0) AS provider_tokens_total, "
        "SUM(CASE WHEN provider_tokens IS NOT NULL THEN 1 ELSE 0 END) "
        "AS billed_runs, "
        "SUM(CASE WHEN incomplete = 1 THEN 1 ELSE 0 END) AS incomplete_runs "
        "FROM machine_reviews WHERE created_at >= datetime('now', ?) "
        "GROUP BY profile ORDER BY reviews DESC",
        (since,),
    )

    harness_rows = await fetchall(
        db,
        "SELECT harness_skill, harness_version, COUNT(*) AS reports_total, "  # nosec B608 - constant fragment
        f"SUM(CASE WHEN {REPORT_HAS_EVIDENCE_SQL} THEN 1 ELSE 0 END) AS reviews, "
        f"SUM(CASE WHEN {REPORT_HAS_EVIDENCE_SQL} THEN 0 ELSE 1 END) "
        "AS no_data_reports, "
        "COALESCE(SUM(raw_count), 0) AS raw_total, "
        "COALESCE(SUM(json_array_length(findings_confirmed)), 0) AS confirmed_total, "
        "COALESCE(SUM(tokens_spent), 0) AS tokens_total "
        "FROM machine_reviews WHERE created_at >= datetime('now', ?) "
        "GROUP BY harness_skill, harness_version "
        "ORDER BY harness_skill, harness_version",
        (since,),
    )

    recurring = await recurring_categories(db, since)
    # #878: the flywheel. A category the reviewer has found in enough DISTINCT
    # tasks is no longer a finding, it is a property of the codebase, and
    # paying a model to rediscover it every submission is the one cost here
    # that never has to be paid again.
    debt = await build_category_debt(db, recurring)

    # `completed_at` is the only completion clock (#517). Until #810 a row
    # without it was filled in from `updated_at`, and that fallback was
    # defended as a small bias — 0–13.3h against durations of 280–340h. The
    # comparison used the fallback rows as their own yardstick. Split by
    # source on production data (21.08.2026, 518 completed tasks), the two are
    # not the same quantity: measured rows have a median of 0.83h for bug and
    # 1.70h for feature, while the rows filled in from `updated_at` sit at
    # 254h and 550h. So the blended median tracked the share of filled-in rows
    # — bug 35 of 81, feature 55 of 174, refactor 11 of 13 — and reported that
    # bugs take eight times longer than features when measured bugs are in
    # fact the fastest rows in the table.
    #
    # The other half of that argument — dropping them costs three quarters of
    # the sample — expired as the window rolled forward: 46 measured bugs and
    # 119 measured features now stand on their own.
    #
    # Rows without the stamp are counted, never estimated. Their membership in
    # the window is decided by `updated_at` only because nothing else about
    # them is dated: that is a "recent enough to mention" test, never a
    # duration. Every row that HAS a completion is filtered and measured by
    # the same `completed_at` — #518 fixed a version where numerator and
    # window used different clocks, and that fix stays.
    cycle_rows = await fetchall(
        db,
        "SELECT work_type, completed_at IS NULL AS no_completion, "
        "(julianday(completed_at) - julianday(ready_at)) * 24.0 AS hours "
        "FROM tasks WHERE status='completed' AND ready_at IS NOT NULL "
        "AND (completed_at >= datetime('now', ?) "
        "OR (completed_at IS NULL AND updated_at >= datetime('now', ?)))",
        (since, since),
    )
    by_type: dict[str, list[float]] = {}
    no_completion_by_type: dict[str, int] = {}
    unmeasurable_by_type: dict[str, int] = {}
    for r in cycle_rows:
        wt = r["work_type"] or "feature"
        if r["no_completion"]:
            # Checked before the start test below, so a row missing BOTH stamps
            # is counted once, here. The two exclusions overlap almost entirely
            # in today's data: on production every row with a bulk-stamped
            # ready_at also predates completed_at, so unmeasurable_tasks now
            # reads 0 across the board and no_completion_tasks absorbs those
            # rows (chore 16, feature 44, docs 5, refactor 1). That is a change
            # of label, not of exclusion — the #518 test below still guards the
            # case it was written for: a future row that has a completion but a
            # start stamped after it.
            no_completion_by_type[wt] = no_completion_by_type.get(wt, 0) + 1
            continue
        if r["hours"] is None:
            continue
        if r["hours"] <= 0:
            # A non-positive duration is not a fast task, it is a task whose
            # start is unknown (#518). On production every such row carries the
            # same ready_at — a bulk stamp applied to tasks that were already
            # finished — so ready_at records when someone backfilled the
            # column, not when the work became ready. Counting these as zero
            # dragged the feature median from 70h down to 4h.
            #
            # Only equality occurs in the data; negatives were checked for and
            # there are none. The condition stays <= so a clock skew that does
            # produce one is excluded rather than averaged in.
            unmeasurable_by_type[wt] = unmeasurable_by_type.get(wt, 0) + 1
            continue
        by_type.setdefault(wt, []).append(r["hours"])
    # Both exclusions are reported per row rather than folded into the median:
    # a number that silently mixes measured with inferred values reads as fact.
    # Same principle as n_excluded in #518 and findings_unaccounted in #519 —
    # say what is not known instead of estimating it.
    # A work type all of whose rows are excluded still gets a line: saying
    # "5 tasks, no completion stamp, no median" is information, while omitting
    # the row entirely reads as "no work of this type happened" (#518).
    cycle_times = [
        {
            "work_type": wt,
            "tasks": len(by_type.get(wt, [])),
            "no_completion_tasks": no_completion_by_type.get(wt, 0),
            "unmeasurable_tasks": unmeasurable_by_type.get(wt, 0),
            "median_hours": (
                round(statistics.median(by_type[wt]), 2) if by_type.get(wt) else None
            ),
        }
        for wt in sorted(
            set(by_type) | set(unmeasurable_by_type) | set(no_completion_by_type)
        )
    ]

    # #877: what the findings turned out to be. Attached to the profile rows
    # as well, because "lite found three things" and "lite found three things
    # and two of them were real" are different facts about the same run.
    dispositions = await _disposition_metrics(db, since)
    totals["dispositions"] = dispositions
    totals["tokens_per_fixed"] = await _tokens_per_fixed(db, since, "tokens_spent")
    totals["provider_tokens_per_fixed"] = await _tokens_per_fixed(
        db, since, "provider_tokens"
    )
    by_profile_rates = {row["profile"]: row for row in dispositions["by_profile"]}
    profile_dicts = []
    for row in profile_rows:
        entry = dict(row)
        # A profile nobody judged gets the empty shape, not zeros: "no data"
        # and "nothing was real" are opposite readings of the same blank.
        rates = by_profile_rates.get(entry["profile"])
        entry["judged"] = rates["judged"] if rates else 0
        entry["precision"] = rates["precision"] if rates else None
        entry["resolution_rate"] = rates["resolution_rate"] if rates else None
        # #893: the number the profile decision actually rests on — what one
        # run of it bills. None when no run of this profile has a bill yet:
        # dividing by zero billed runs would print 0 and read as "free".
        billed = int(entry.get("billed_runs") or 0)
        entry["provider_tokens_per_run"] = (
            round(int(entry.get("provider_tokens_total") or 0) / billed)
            if billed
            else None
        )
        profile_dicts.append(entry)

    escaped = await _escaped_defect_metrics(db, since)

    human_gates = await _human_gate_metrics(db, since)
    review_outcomes = await _review_outcome_metrics(db, since)

    return {
        "since_days": since_days,
        "machine_reviews": totals,
        "by_harness": [dict(r) for r in harness_rows],
        "by_profile": profile_dicts,
        "by_reviewer_model": dispositions["by_model"],
        "recurring_categories": recurring,
        "category_debt": debt,
        "cycle_times": cycle_times,
        "escaped_defects": escaped,
        "human_gates": human_gates,
        "review_outcomes": review_outcomes,
    }


async def _escaped_defect_metrics(
    db: aiosqlite.Connection, since: str
) -> dict[str, Any]:
    """Bugs filed after their feature was closed — what review let through (#528).

    Recurring categories count what the gate STOPPED. This counts what it
    missed, which is the only side of the ledger that can contradict a
    first-pass acceptance rate of 100% at zero rejections.

    A bug is an escape when the nearest ``feature`` ancestor of its parent
    chain carries a ``completed_at`` and the bug was filed after it. Both
    halves of that test are cheap to get wrong, so both exclusions are
    counted and published beside the result instead of being folded into it:

    * ``bugs_without_feature`` — no feature ancestor at all (parent is an epic
      or nothing). 33 of 103 bugs on production at the time of writing.
    * ``features_without_completion`` — the feature is closed but has no
      completion stamp: ``completed_at`` arrived with #517 and was never
      backfilled, so 53 of 82 closed features cannot answer the question. The
      date is NOT reconstructed from ``updated_at`` — that substitution is the
      defect #810 removed from cycle time, and it would land here as a silent
      wave of fake escapes. Scoped to features that actually have bugs in the
      window: those are the ones where an answer was owed. How many closed
      features lack the stamp overall is a question about data hygiene, not
      about leaks, and mixing the two would put a number on this page that
      nothing here can move.

    With 14 counted escapes against those two buckets, the uncounted currently
    outweighs the counted. Saying so is the point: a bare "14" reads as a
    measurement of quality when it is mostly a measurement of which fields got
    filled in.

    The window applies to the bug's ``created_at`` — when the leak surfaced —
    never to the feature's closure. #518 was a bug about a numerator and a
    window keeping different clocks; this one states its clock.
    """
    rows = await fetchall(
        db,
        # The ancestry walk stops at the first feature, so each bug contributes
        # its NEAREST feature and no other: a bug hanging under a task under a
        # feature is attributed to that feature, not to the epic above it.
        "WITH RECURSIVE ancestry(bug_id, bug_created, node_id, depth) AS ("
        "  SELECT id, created_at, parent_id, 1 FROM tasks"
        "   WHERE work_type = 'bug' AND parent_id IS NOT NULL"
        "     AND created_at >= datetime('now', ?)"
        "  UNION ALL"
        "  SELECT a.bug_id, a.bug_created, t.parent_id, a.depth + 1"
        "    FROM ancestry a JOIN tasks t ON t.id = a.node_id"
        "   WHERE t.task_type != 'feature' AND t.parent_id IS NOT NULL"
        "     AND a.depth < 10"
        ") "
        "SELECT a.bug_id, a.bug_created, f.id AS feature_id, f.title AS title, "
        "f.status AS feature_status, f.completed_at AS feature_completed "
        "FROM ancestry a "
        "JOIN tasks f ON f.id = a.node_id AND f.task_type = 'feature'",
        (since,),
    )
    total_rows = await fetchall(
        db,
        "SELECT COUNT(*) AS bugs FROM tasks "
        "WHERE work_type = 'bug' AND created_at >= datetime('now', ?)",
        (since,),
    )
    bugs_in_window = total_rows[0]["bugs"] or 0

    per_feature: dict[int, dict[str, Any]] = {}
    unstamped: set[int] = set()
    bugs_with_feature: set[int] = set()

    for row in rows:
        bugs_with_feature.add(row["bug_id"])
        if row["feature_completed"] is None:
            # A feature closed without a stamp cannot answer the question, and
            # a feature still open has not let anything escape yet. Only the
            # first case is a gap in the measurement, so only it is counted.
            if row["feature_status"] == "completed":
                unstamped.add(row["feature_id"])
            continue
        if row["bug_created"] <= row["feature_completed"]:
            continue
        entry = per_feature.setdefault(
            row["feature_id"],
            {"feature_id": row["feature_id"], "title": row["title"], "bugs": 0},
        )
        entry["bugs"] += 1

    # The list is the usable part. A total says "leaks happen"; "five bugs
    # escaped #723" is where a post-mortem starts, so the features are named
    # and ordered by how much they leaked.
    features = sorted(per_feature.values(), key=lambda f: (-f["bugs"], f["feature_id"]))
    return {
        "escaped": sum(f["bugs"] for f in features),
        "features": features,
        "bugs_without_feature": bugs_in_window - len(bugs_with_feature),
        "features_without_completion": len(unstamped),
        "bugs_in_window": bugs_in_window,
    }


async def _review_outcome_metrics(
    db: aiosqlite.Connection, since: str
) -> dict[str, Any]:
    """First-pass acceptance and changes-requested rate (#522).

    Two different questions, so two different denominators, and both are
    reported beside their rates rather than left to be reconstructed:

    * ``first_pass_acceptance_rate`` is per TASK — the share of tasks that
      were approved on their first submission. "First time" is pinned to
      ``submission_generation == 1``: the generation is bumped only by a
      resubmission, so a verdict on a later generation is proof that work
      came back. A task whose first submission collected a
      ``changes_requested`` does not become first-pass by being approved
      afterwards on the same generation.
    * ``changes_requested_rate`` is per VERDICT — the proportion the task
      statement names (changes_requested against approved). Mixing the two
      denominators into one number is what makes such a metric unreadable
      three months later.

    Verdicts whose payload carries no usable generation cannot answer the
    first-pass question at all. Their tasks are counted in
    ``tasks_unaccounted`` and kept OUT of the first-pass denominator
    instead of being silently scored as reworked — the #518/#519 rule: say
    what is not known rather than let it flatter or spoil the rate. Those
    verdicts still count toward the changes-requested rate, which does not
    need a generation.

    Self-approved first passes are counted separately: a submission the
    author waved through is not evidence of quality, and folding it into
    the headline number would let the rate rise by removing the reviewer.
    The full self-approval picture is its own metric (#523).
    """
    import json as _json

    rows = await fetchall(
        db,
        "SELECT task_id, payload FROM events "
        "WHERE kind = 'review_verdict_recorded' "
        "AND created_at >= datetime('now', ?)",
        (since,),
    )

    approved = 0
    changes_requested = 0
    per_task: dict[int, dict[str, bool]] = {}

    for row in rows:
        try:
            payload = _json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        verdict = (payload.get("verdict") or "").lower()
        if verdict not in {"approved", "changes_requested"}:
            continue
        if verdict == "approved":
            approved += 1
        else:
            changes_requested += 1

        task_id = row["task_id"]
        if task_id is None:
            continue
        entry = per_task.setdefault(
            task_id,
            {
                "generation_known": False,
                "first_approved": False,
                "first_changes": False,
                "self_approved": False,
            },
        )
        generation = payload.get("submission_generation")
        # bool is an int in Python; a True here would mean generation 1.
        if not isinstance(generation, int) or isinstance(generation, bool):
            continue
        entry["generation_known"] = True
        if generation != 1:
            continue
        if verdict == "approved":
            entry["first_approved"] = True
            entry["self_approved"] = entry["self_approved"] or bool(
                payload.get("self_approved")
            )
        else:
            entry["first_changes"] = True

    measurable = [e for e in per_task.values() if e["generation_known"]]
    first_pass = [
        e for e in measurable if e["first_approved"] and not e["first_changes"]
    ]
    verdicts = approved + changes_requested

    return {
        "tasks": len(measurable),
        "tasks_unaccounted": len(per_task) - len(measurable),
        "first_pass_tasks": len(first_pass),
        "first_pass_acceptance_rate": (
            round(len(first_pass) / len(measurable), 3) if measurable else None
        ),
        "self_approved_first_pass": sum(1 for e in first_pass if e["self_approved"]),
        "verdicts": verdicts,
        "approved": approved,
        "changes_requested": changes_requested,
        "changes_requested_rate": (
            round(changes_requested / verdicts, 3) if verdicts else None
        ),
    }


def _parse_hub_ts(raw: str | None) -> Any:
    """Parse the two timestamp shapes this DB carries (#594): SQLite's
    'YYYY-MM-DD HH:MM:SS' and ISO with 'T'/offset. None on anything else."""
    from datetime import datetime

    if not raw:
        return None
    text = raw.strip().replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


async def _human_gate_metrics(
    db: aiosqlite.Connection, since: str
) -> list[dict[str, Any]]:
    """Override-rate and queue wait per HUMAN gate and project (#737).

    A gate whose override-rate sits at ~0% over the window is a candidate
    for the autopilot (#738); a gate where the human actually changes
    outcomes stays human. Only human decisions count: actor 'hub' (service
    writes, #584) and 'policy' (autopilot, #738) are excluded from both
    the numerator and the denominator. Waits that cannot be measured are
    reported as ``wait_unaccounted`` — never as zeros (the #518 lesson).
    """
    import json as _json
    import statistics

    event_rows = await fetchall(
        db,
        "SELECT e.kind, e.actor, e.payload, e.created_at, e.task_id, t.ready_at "
        "FROM events e "
        "LEFT JOIN tasks t ON t.id = e.task_id "
        "WHERE e.created_at >= datetime('now', ?) AND e.kind IN "
        "('task_approved', 'task_rejected', 'review_verdict_recorded', "
        "'task_decided', 'audit_result') ORDER BY e.created_at ASC",
        (since,),
    )

    # Project attribution (#747): project_id lives on epics only — children
    # inherit it by walking up. A direct tasks.project_id join dropped almost
    # every spike task into 'default' (spike-bo showed 1 decision instead of
    # ~70). Reuse the SAME resolver the git conveyor uses, memoized per call,
    # so 'pending → default' and 'outside any epic → default' stay one rule.
    project_cache: dict[int, str] = {}

    async def project_slug_for(task_id: int | None) -> str:
        if task_id is None:
            return "default"
        if task_id not in project_cache:
            project_row = await repo.resolve_project_for_task(db, task_id)
            project_cache[task_id] = (
                project_row["slug"] if project_row is not None else "default"
            )
        return project_cache[task_id]

    # The submission moment is written by the hub itself in a fixed shape
    # (submit_for_review), which makes it the one submission timestamp that
    # exists for the whole history — there is no dedicated event yet.
    submit_rows = await fetchall(
        db,
        "SELECT task_id, created_at FROM task_updates "
        "WHERE kind = 'status' AND content LIKE ?",
        (f"{repo.SUBMISSION_UPDATE_PREFIX}%",),
    )
    submits: dict[int, list[Any]] = {}
    for row in submit_rows:
        parsed = _parse_hub_ts(row["created_at"])
        if parsed is not None:
            submits.setdefault(row["task_id"], []).append(parsed)

    gates: dict[tuple[str, str], dict[str, Any]] = {}

    def bucket(gate: str, project: str) -> dict[str, Any]:
        return gates.setdefault(
            (gate, project),
            {
                "gate": gate,
                "project": project,
                "approvals": 0,
                "overrides": 0,
                "waits": [],
                "wait_unaccounted": 0,
            },
        )

    def record_wait(entry: dict[str, Any], start: Any, end: Any) -> None:
        if start is None or end is None or end < start:
            entry["wait_unaccounted"] += 1
            return
        entry["waits"].append((end - start).total_seconds() / 3600.0)

    for row in event_rows:
        actor = (row["actor"] or "").strip()
        project_slug = await project_slug_for(row["task_id"])
        decided_at = _parse_hub_ts(row["created_at"])
        kind = row["kind"]
        if kind in {"task_approved", "task_rejected", "task_decided", "audit_result"}:
            # These carry an explicit human/non-human actor.
            if actor != "human":
                continue
        elif actor in {"hub", "policy"}:
            # Verdicts name the reviewer; today every client-driven verdict
            # is the human gate, and the autopilot (#738) will stamp its
            # own actor — excluded here by name.
            continue

        if kind == "task_approved":
            entry = bucket("dor", project_slug)
            entry["approvals"] += 1
            record_wait(entry, _parse_hub_ts(row["ready_at"]), decided_at)
        elif kind == "task_rejected":
            entry = bucket("dor", project_slug)
            entry["overrides"] += 1
            record_wait(entry, _parse_hub_ts(row["ready_at"]), decided_at)
        elif kind == "review_verdict_recorded":
            try:
                payload = _json.loads(row["payload"] or "{}")
            except ValueError:
                payload = {}
            verdict = (payload.get("verdict") or "").lower()
            if verdict not in {"approved", "changes_requested"}:
                continue
            entry = bucket("verdict", project_slug)
            if verdict == "approved":
                entry["approvals"] += 1
            else:
                entry["overrides"] += 1
            candidates = [
                s
                for s in submits.get(row["task_id"], [])
                if decided_at is not None and s <= decided_at
            ]
            record_wait(entry, max(candidates) if candidates else None, decided_at)
        elif kind == "audit_result":
            # The post-hoc side of the autopilot (#739): a spot check that
            # found a problem is the strongest argument to pull the gate
            # back to human — surfaced here as its own gate so the
            # expand-or-roll-back decision reads one table.
            try:
                payload = _json.loads(row["payload"] or "{}")
            except ValueError:
                payload = {}
            audit_result = (payload.get("result") or "").lower()
            if audit_result not in {"ok", "problem"}:
                continue
            entry = bucket("audit", project_slug)
            if audit_result == "ok":
                entry["approvals"] += 1
            else:
                entry["overrides"] += 1
            # A spot check has no queue: nothing waits on it, so no wait
            # sample and no unaccounted bump.
        elif kind == "task_decided":
            try:
                payload = _json.loads(row["payload"] or "{}")
            except ValueError:
                payload = {}
            action = (payload.get("action") or "").lower()
            if action not in {"accept", "rework"}:
                continue
            entry = bucket("decision", project_slug)
            if action == "accept":
                entry["approvals"] += 1
            else:
                entry["overrides"] += 1
            record_wait(entry, _parse_hub_ts(payload.get("entered_at")), decided_at)

    result: list[dict[str, Any]] = []
    for (_, _), entry in sorted(gates.items()):
        decisions = entry["approvals"] + entry["overrides"]
        waits = entry.pop("waits")
        entry["override_rate"] = (
            round(entry["overrides"] / decisions, 3) if decisions else None
        )
        entry["median_wait_hours"] = (
            round(statistics.median(waits), 2) if waits else None
        )
        result.append(entry)
    return result


async def provision_project(
    db: aiosqlite.Connection, project_id: int, *, actor: str = ""
) -> dict[str, str]:
    """Clone/verify a project workspace and record the outcome (#347).

    Never raises for git failures — the outcome lands in
    ``provision_status``/``provision_detail`` so the operator can read
    WHY instead of getting a 500. Missing repo/workspace are provision
    errors too, not validation errors: the button must always answer.

    #476: a successful clone also gets the hub's workflow templates, because
    a repository the hub can clone but cannot deliver from is not provisioned.
    The delivery gate merges only on a green workflow outcome, so a project
    with no ``.github/workflows`` answered ``ci_absent`` and parked approved,
    green work in ``needs_decision``. Seeding is best effort by contract: it
    appends its own sentence to the detail and never changes the status,
    since a clone that worked is still a clone that worked.
    """
    row = await repo.get_project(db, project_id)
    if row is None:
        return {"provision_status": "error", "provision_detail": "project not found"}
    project = dict(row)
    if not (project.get("repo") or "").strip():
        ok, detail = False, "project has no repo configured"
    elif not (project.get("workspace_path") or "").strip():
        ok, detail = False, "project has no workspace_path configured"
    else:
        ok, detail = await plugins.git_ops.clone_repo(
            project["repo"].strip(),
            project["workspace_path"].strip(),
            # #475: one reader for the integration branch. The literal that
            # stood here cloned calc-kids' master workspace with --branch
            # develop the moment the column was blank.
            base_branch_of(project),
        )
        if ok:
            seed = workflow_seed.seed_project_workflows(
                project["workspace_path"].strip(),
                # Same readers as everything else that asks which branches
                # this project uses (#475) — the templates carry placeholders
                # precisely so no branch name is written into a file here.
                base_branch=base_branch_of(project),
                release_branch=release_base_of(project),
                ac_runner=ci_runner_of(project),
            )
            detail = f"{detail}; workflows: {seed.detail}"
            # #887: clone_repo records only the base branch, and only for the
            # clone it just made or verified. Re-arming here through the same
            # writer adds the release branch and makes provisioning a way to
            # repair a clone whose keys drifted — the "existing clone verified"
            # path is the only one that ever runs on a production workspace.
            rearm_clone(row)
    status = "ok" if ok else "error"
    await repo.update_project(
        db, project_id, provision_status=status, provision_detail=detail[:1000]
    )
    await repo.insert_event(
        db,
        kind="project_provisioned",
        project_id=project_id,
        actor=actor or "hub",
        payload={"status": status, "slug": project.get("slug", "")},
    )
    await db.commit()
    await log_activity(
        db,
        "project_provisioned",
        f"Project {project.get('slug', project_id)} provision: {status} — {detail[:200]}",
    )
    return {"provision_status": status, "provision_detail": detail}


def _split_git_kwargs(ctx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(local_git_kwargs, pr_kwargs) — local ops need repo/base, PR needs all."""
    local = {k: v for k, v in ctx.items() if k in ("repo", "base_branch")}
    return local, dict(ctx)


async def dispatch_task(
    db: aiosqlite.Connection,
    task_id: int,
    task: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a task via oc-dev-dispatch, creating a branch if needed."""
    ctx = await project_git_context(db, task_id)
    local_kw, _ = _split_git_kwargs(ctx)
    branch = task.get("branch") or ""
    prepared = True
    refusal = ""
    if not branch:
        # An empty return means "no branch created" — the ordinary state of a
        # hub without git integration. A REFUSAL raises, so an unconfigured hub
        # keeps dispatching while a dirty workspace stops the task (#361).
        try:
            branch = await plugins.git_ops.create_branch(
                task_id, task["title"], **local_kw
            )
        except WorkspaceNotReadyError as exc:
            prepared, refusal = False, str(exc)
        if branch:
            await repo.update_task(db, task_id, branch=branch)
            await db.commit()
    else:
        # A redispatched task skips create_branch entirely, so its dirty-tree
        # refusal never runs for this path — the checkout result is the only
        # signal there is, and it used to be discarded too (#361).
        # A branch is only ever recorded by a working git integration, so an
        # unconfigured hub cannot reach this line with one; if it somehow does,
        # False means "cannot read the workspace" and escalating is exactly what
        # invariant 4 of docs/workspace-safety-policy.md asks for.
        prepared = await plugins.git_ops.checkout(branch, repo=local_kw.get("repo"))

    if not prepared:
        # Dispatching now would put the agent on whatever tree is current, with
        # no branch of its own: its work would land in someone else's checkout,
        # and — because the task row carries no branch — the done-pipeline's
        # "and branch" gate would skip its git tail entirely, leaving that work
        # uncommitted for the next task's `git add -A` to sweep up (#361).
        error = (
            f"git workspace not ready for #{task_id}: "
            + (refusal or f"could not check out {branch!r}")
            + ". Dispatch refused."
        )
        await repo.update_task(
            db, task_id, status="needs_decision", job_id=None, result_text=error
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "blocker",
            f"{error} Обычная причина — незакоммиченные изменения в общем "
            f"рабочем каталоге. Закоммитьте или спрячьте их и решите через "
            f"hub_decide_task.",
        )
        await repo.insert_event(
            db,
            kind="needs_decision",
            task_id=task_id,
            actor="hub",
            payload={"reason": "dispatch_workspace_not_ready", "branch": branch},
        )
        await db.commit()
        log.error("Task #%d → needs_decision: %s", task_id, error)
        return {"error": error}

    updates_rows = await repo.get_task_updates(db, task_id)
    updates = [dict(r) for r in updates_rows] if updates_rows else None

    message = plugins.dispatch.build_enriched_message(
        task["title"],
        task.get("description", ""),
        updates,
        branch=branch,
    )
    runtime = task.get("runtime", "auto")
    result = await plugins.dispatch.submit_task(
        message, runtime=runtime, task_id=task_id
    )
    job_id = result.get("job_id")

    if job_id:
        assigned_agent = (
            result.get("assigned_agent")
            or result.get("agent")
            or task.get("assigned_agent")
            or "developer-agent"
        )
        await repo.update_task(
            db,
            task_id,
            status="running",
            job_id=job_id,
            assigned_agent=assigned_agent,
        )
    else:
        error = result.get("error", "dispatch returned no job_id")
        await repo.update_task(
            db,
            task_id,
            status="open",
            job_id=None,
            result_text=error,
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Developer-agent dispatch unavailable: {error}",
        )
    await db.commit()
    return result


def worktree_per_task_enabled() -> bool:
    """Opt-in git-worktree isolation for pair tasks (#459).

    Default OFF keeps the single-working-tree behavior of #451/#457 unchanged
    in production; set HAIPLANE_WORKTREE_PER_TASK=1 to give each task its own
    worktree so concurrent pair-starts never share a working tree.
    """
    return config.env_get("WORKTREE_PER_TASK") == "1"


def _slug_for_task(task_id: int, task: dict[str, Any], branch_slug: str) -> str:
    """Reuse an existing task branch's slug so its worktree stays stable (#459)."""
    slug = (branch_slug or "").strip()
    if slug:
        return slug
    existing = (task.get("branch") or "").strip()
    prefix = f"task-{task_id}/"
    if existing.startswith(prefix):
        # #884: a branch already doubled by the old code carries the prefix
        # twice; returning the tail verbatim would feed it back in and grow
        # the name on every restart. canonical_task_branch strips repeats,
        # and this path hands it the same shape it expects.
        return existing[len(prefix) :].strip("/")
    return ""


def _pair_push_notifier(db: aiosqlite.Connection):
    """Колбэк для git-слоя (#966): авто-push осиротевшей ветки виден в ленте.

    Молчаливая публикация чужой ветки запрещена constraints задачи — каждая
    такая операция оставляет запись, кто и ради чего её выполнил.
    """

    async def _notify(message: str) -> None:
        await log_activity(db, "pair_branch_auto_pushed", message)

    return _notify


async def prepare_pair_branch(
    db: aiosqlite.Connection,
    task_id: int,
    task: dict[str, Any],
    *,
    branch_slug: str = "",
) -> str:
    """Create or checkout a task branch without dispatching a headless agent."""
    ctx = await project_git_context(db, task_id)
    local_kw, _ = _split_git_kwargs(ctx)
    if worktree_per_task_enabled():
        return await plugins.git_ops.pair_prepare_worktree(
            task_id,
            task["title"],
            branch_slug=_slug_for_task(task_id, task, branch_slug),
            **local_kw,
        )
    branch = (task.get("branch") or "").strip()
    if branch:
        await plugins.git_ops.checkout(branch, repo=local_kw.get("repo"))
        return branch
    return await plugins.git_ops.pair_prepare_branch(
        task_id,
        task["title"],
        branch_slug=branch_slug,
        notify=_pair_push_notifier(db),
        **local_kw,
    )


async def pair_worktree_info(
    db: aiosqlite.Connection,
    task_id: int,
) -> tuple[str, str]:
    """Return (workspace_mode, worktree_path) for a pair task (#530).

    In worktree mode gives the deterministic worktree path so the caller can
    tell the agent where its isolated tree lives; legacy mode returns
    ("legacy", "") and no path.
    """
    if not worktree_per_task_enabled():
        return "legacy", ""
    ctx = await project_git_context(db, task_id)
    local_kw, _ = _split_git_kwargs(ctx)
    path = plugins.git_ops.worktree_path(task_id, local_kw.get("repo"))
    return "worktree", path or ""


async def restore_pair_workspace_base(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Best-effort cleanup after a pair task leaves running (#451/#459).

    Worktree mode (#459): remove the task's worktree. Legacy mode: return the
    single working tree to its base branch. Either way the main clone ends on base.
    Remote git_mode (#975): the hub host never held this workspace — skip.
    """
    row = await repo.get_task(db, task_id)
    if row and (dict(row).get("git_mode") or "hub") == "remote":
        return
    ctx = await project_git_context(db, task_id)
    local_kw, _ = _split_git_kwargs(ctx)
    if worktree_per_task_enabled():
        await plugins.git_ops.pair_remove_worktree(task_id, **local_kw)
        return
    await plugins.git_ops.pair_restore_workspace_base(task_id, **local_kw)


async def switch_pair_workspace_to_task(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Best-effort: make the task branch available for rework after CHANGES_REQUESTED.

    Worktree mode (#459): re-create the task's worktree (removed on submit) so
    fixes land there. Legacy mode (#457): switch the single working tree to the
    task branch instead of the local base restored by #451.
    Remote git_mode (#975): the caller already has the branch in its clone.
    """
    row = await repo.get_task(db, task_id)
    task = dict(row) if row else {}
    if (task.get("git_mode") or "hub") == "remote":
        return
    branch = (task.get("branch") or "").strip()
    if not branch:
        return
    ctx = await project_git_context(db, task_id)
    local_kw, _ = _split_git_kwargs(ctx)
    if worktree_per_task_enabled():
        await plugins.git_ops.pair_prepare_worktree(
            task_id,
            task.get("title") or "",
            branch_slug=_slug_for_task(task_id, task, ""),
            **local_kw,
        )
        return
    await plugins.git_ops.pair_switch_to_task_branch(task_id, branch, **local_kw)


# Statuses whose branch is active-but-unmerged: work in progress or waiting
# for a review verdict. Stacking on top of such a branch is what incident
# #392 produced (#424→#425→#426 on top of unmerged task-392).
STACK_ADVISORY_STATUSES = ["running", "review"]


async def detect_branch_stacking(
    db: aiosqlite.Connection,
    task_id: int,
    branch: str,
) -> dict[str, Any] | None:
    """Advisory branch-stacking detection at submission time (#438).

    Checks — via the project's git repo — whether ``branch`` contains
    commits of ANOTHER unmerged task branch in running/review status.
    Returns ``{"base_task_id", "base_task_branch", "base_task_status",
    "message"}`` for the first stacked base found, or None.

    Advisory by design: a stack can be a deliberate decision, so this never
    blocks and never raises. Graceful degradation: no branch, no plugin
    support, or any git failure silently skips the check.
    """
    branch = (branch or "").strip()
    if not branch:
        return None
    checker = getattr(plugins.git_ops, "branch_contains_unmerged_commits_of", None)
    if checker is None:
        return None

    ctx = await project_git_context(db, task_id)
    base = git_ops_mod._resolve_base(ctx.get("base_branch"))
    repo_path = ctx.get("repo")
    rows = await repo.list_unmerged_branch_tasks(
        db, exclude_task_id=task_id, statuses=STACK_ADVISORY_STATUSES
    )
    for row in rows:
        other = dict(row)
        other_branch = (other.get("branch") or "").strip()
        if not other_branch or other_branch == branch:
            continue
        try:
            stacked = await checker(
                branch, other_branch, base_branch=base, repo=repo_path
            )
        except Exception:  # noqa: BLE001 — advisory only; never break the caller
            log.debug(
                "branch stacking check skipped for #%d (%s vs %s)",
                task_id,
                branch,
                other_branch,
                exc_info=True,
            )
            return None
        if stacked:
            other_id = other["id"]
            other_status = other.get("status") or ""
            message = (
                f"ADVISORY branch stacking: '{branch}' contains unmerged "
                f"commits of task #{other_id} branch '{other_branch}' "
                f"(status: {other_status}). This branch cannot be verified "
                f"against '{base}' on its own and the merge order is "
                f"implicit. Alternatives: wait for task #{other_id} to merge "
                f"into '{base}', rebase, and resubmit — or, if the stack is "
                f"deliberate, merge task #{other_id}'s branch first and "
                f"state the merge order explicitly."
            )
            return {
                "base_task_id": other_id,
                "base_task_branch": other_branch,
                "base_task_status": other_status,
                "message": message,
            }
    return None


def review_approved_for_current_submission(task: dict[str, Any]) -> bool:
    """True only when an APPROVED verdict applies to the latest submission.

    A verdict recorded against an earlier submission generation is stale:
    the work changed since it was approved. A task with no submissions yet
    (generation 0) can never count as approved.
    """
    generation = task.get("submission_generation") or 0
    return (
        generation > 0
        and task.get("review_verdict") == "approved"
        and task.get("review_verdict_generation") == generation
    )


def completion_requires_review(task: dict[str, Any]) -> bool:
    """Universal Review Gate (#306): the single completion-gate predicate.

    Normal completion paths (done reports across API/MCP/poller) must not
    complete a task unless the CURRENT submission carries an APPROVED
    verdict. ``auto_review=False`` is the explicit human-controlled opt-out
    (subtasks default to it); human overrides (decide accept,
    force_complete) bypass this predicate by design and stay audited.
    """
    return bool(task.get("auto_review")) and not review_approved_for_current_submission(
        task
    )


async def _approved_code_check(
    db: aiosqlite.Connection, task: dict[str, Any], pr_num: Any
) -> tuple[str, str]:
    """Is the branch still the code that was approved? (#612)

    Returns ``(refusal, note)``: a non-empty refusal blocks delivery, a
    non-empty note records that the comparison could NOT be made and why.
    Three outcomes, never collapsed into two — the same shape #572 used on the
    verdict path, because "diverged" and "not checked" call for opposite
    responses and merging them would either block on a blinking network or
    stay silent about unreviewed code.
    """
    # Local import: lifecycle imports this module for project_git_context, so a
    # module-level import here closes the cycle. The comparison lives there and
    # is not copied — one implementation for all three paths that need it.
    from hub.services.lifecycle import resolve_branch_tip

    pinned = (task.get("submission_sha") or "").strip()
    if not pinned:
        # Submitted before #572, or the tip could not be read at submission.
        # Delivering is right — a task that predates the mechanism must not be
        # retroactively blocked by it — but silence would read as "verified".
        return "", (
            "Сверка кода с одобрением НЕ проводилась: коммит сдачи не был "
            "закреплён. Доставлено по номеру сдачи, не по коммиту."
        )

    current_tip, tip_reason = await resolve_branch_tip(
        db, task["id"], task.get("branch") or ""
    )
    if not current_tip:
        # The remote blinked. Blocking here would stop the conveyor for every
        # task over a network fault that says nothing about the work.
        return "", (
            f"Сверка кода с одобрением НЕ проводилась: {tip_reason}. "
            f"Одобрен коммит {pinned[:12]}."
        )
    if current_tip != pinned:
        return (
            f"stale_approval: одобрен {pinned[:12]}, ветка на "
            f"{current_tip[:12]} — пересдайте, чтобы ревью увидело текущий код "
            f"(PR #{pr_num})"
        ), ""
    return "", ""


# #951: the two gate refusals that mean "ask again in a minute", not "ask a
# human". Built from the same enum merge_before_completion prints, so the
# prefix contract between the two spots of this file cannot silently drift.
TRANSIENT_GATE_PREFIXES = (
    f"ci_{CIProbeOutcome.pending.value}",
    f"ci_{CIProbeOutcome.unavailable.value}",
)


async def merge_before_completion(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> tuple[bool, str]:
    """Deliver the task's PR before letting it complete (#605).

    Mirrors the headless conveyor's gate (#363): only a green CI merges, a
    refused merge never completes, and the merge commit is asked of the PR
    itself (#534) so the drift guard can tell this delivery from an intruder.
    Returns ``(ok, reason)``; every failure is a reason, never an exception —
    a done report must not 500 because GitHub blinked (AC-5).

    Until this existed the pair flow had NO merge trigger at all: APPROVED
    returned the task to running, report_done completed it, and the PR hung
    unmerged — the state #363 ruled out for headless. Every merge of the
    past week was manual out of necessity, and every one of them now rings
    the drift guard. Found by the first live run of #603.
    """
    task_id = task["id"]
    pr_num = task.get("pr_number")
    if pr_num is None:
        # Вызывающий пускает сюда только задачи с номером PR (#605), но внутри
        # это нигде не было сказано, и проверка типов справедливо считала, что
        # в GitHub может уехать None. Инвариант записан явно: отказ с причиной
        # дешевле падения на первом же вызове с None.
        return False, "no_pr: у задачи нет номера PR — доставлять нечего"
    try:
        # Already delivered — by the headless conveyor, or by an earlier done
        # report that failed after the merge. Merging twice is not extra
        # safety: GitHub refuses the second merge and the refusal would flip
        # a DELIVERED task into needs_decision (#363's exactly-once guard
        # caught precisely this).
        if await repo.pipeline_merge_recorded(db, task_id, pr_num):
            return True, "already delivered"

        # #612: does the branch still stand where the reviewer approved it?
        # #572 bound the verdict to a commit and closed the order "push, then
        # approve". The opposite order — approve, THEN push — reached here
        # untouched: the verdict stays current because its generation never
        # changed, and this gate merges whatever the tip is now. Checked BEFORE
        # the CI probe: there is no point asking about code that will not be
        # delivered, and a refusal then costs one network call instead of two.
        diverged, sha_note = await _approved_code_check(db, task, pr_num)
        if diverged:
            return False, diverged
        if sha_note:
            # Delivered WITHOUT the comparison — the reader must be able to
            # tell that apart from "compared and matched". Written here rather
            # than returned: the gate's (ok, reason) contract feeds a refusal
            # message, and a success has no channel of its own (#605 AC-5).
            await repo.add_task_update(db, task_id, "hub", "alert", sha_note)

        ctx = await project_git_context(db, task_id)
        workspace = ctx.get("repo")
        gh_repo = ctx.get("gh_repo")

        ci = await plugins.git_ops.check_pr_ci(pr_num, repo=workspace, gh_repo=gh_repo)
        if ci.outcome != CIProbeOutcome.passed:
            return False, f"ci_{ci.outcome.value}: {ci.reason}"

        merged = await plugins.git_ops.merge_pr(
            pr_num,
            task_id,
            task.get("title") or "",
            repo=workspace,
            gh_repo=gh_repo,
        )
        if not merged:
            return False, "merge_failed: GitHub refused the merge"

        # The commit THIS pull request produced — never the branch tip,
        # which is whatever landed last (#534, review round 3).
        merge_sha = ""
        try:
            merge_sha = await plugins.git_ops.merge_commit_sha(
                pr_num, repo=workspace, gh_repo=gh_repo
            )
        except Exception:  # noqa: BLE001 - the drift guard flags it once
            log.exception(
                "could not read the merge commit for task #%s; "
                "the drift guard will flag it once",
                task_id,
            )
        proj = await repo.resolve_project_for_task(db, task_id)
        await repo.record_pipeline_merge(
            db,
            pr_number=pr_num,
            merge_sha=merge_sha,
            project_id=(dict(proj)["id"] if proj else None),
            task_id=task_id,
        )

        # Post-merge tidying, not delivery (#552): the work is merged, so a
        # workspace that cannot be returned to base is logged, never fatal.
        try:
            await plugins.git_ops.pull_main(
                repo=workspace, base_branch=ctx.get("base_branch")
            )
            branch = (task.get("branch") or "").strip()
            if branch:
                await plugins.git_ops.delete_branch(
                    branch, repo=workspace, base_branch=ctx.get("base_branch")
                )
        except Exception:  # noqa: BLE001
            log.exception("post-merge tidy failed for task #%s", task_id)
        return True, merge_sha
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.exception("merge gate failed for task #%s", task_id)
        return False, f"merge_gate_error: {exc}"


@dataclass(frozen=True)
class DeliveryPR:
    """Which PR carries this delivery, and whether it can carry it at all (#802).

    ``unusable`` is the state that had no name before: the recorded PR is
    closed, not merged, and no open PR replaces it. Left unnamed, the gate
    walked on and tried to merge a corpse — approved, green work could not be
    delivered by any supported path.
    """

    number: int | None = None
    reason: str = ""
    unusable: bool = False
    # #959: whether the gate managed to establish WHICH PR this is. False only
    # when the recorded number could not be looked up at all. The waiting
    # branch reads it: telling somebody to wait for a green CI is a promise
    # about a PR, and it must not be made when the PR itself is unknown.
    established: bool = True


async def _recorded_pr_state(
    db: aiosqlite.Connection, task: dict[str, Any], pr_number: int
) -> tuple[str, str]:
    """Where the recorded PR stands, and a cause when it cannot be asked (#802).

    Returns ``(state, reason)``. An empty state means "could not look" — never
    "closed": those two lead to opposite decisions, and reading silence as a
    verdict is the mistake #725 catalogued.
    """
    try:
        ctx = await project_git_context(db, task["id"])
        state = await plugins.git_ops.pr_state(
            pr_number, repo=ctx.get("repo"), gh_repo=ctx.get("gh_repo")
        )
    except Exception as exc:  # noqa: BLE001 - a cause, not a failure (AC-4)
        log.warning(
            "PR state lookup failed for #%s (#%s): %s", task["id"], pr_number, exc
        )
        return "", f"состояние PR #{pr_number} не удалось узнать: {exc}"
    if not state:
        return (
            "",
            f"состояние PR #{pr_number} неизвестно — доставка идёт по нему как раньше",
        )
    return state, ""


async def _live_pr_for_branch(
    db: aiosqlite.Connection, task: dict[str, Any], branch: str
) -> tuple[int | None, str]:
    """An open PR for this branch, or a cause. Records nothing by itself."""
    try:
        ctx = await project_git_context(db, task["id"])
        found = await plugins.git_ops.pr_for_branch(
            branch, repo=ctx.get("repo"), gh_repo=ctx.get("gh_repo")
        )
    except Exception as exc:  # noqa: BLE001 - a cause, not a failure (AC-4)
        log.warning("PR search failed for #%s (%s): %s", task["id"], branch, exc)
        return None, f"поиск открытого PR по ветке {branch} не ответил: {exc}"
    if found is None:
        return None, ""
    await repo.update_task(db, task["id"], pr_number=int(found))
    return int(found), ""


async def _confirmed_branch_diff(
    db: aiosqlite.Connection, task: dict[str, Any], branch: str | None
) -> list[str] | None:
    """What the branch verifiably changes, or None when nobody can say (#967).

    The same primitive and the same per-project base the #498 warning reads —
    one rule, not two competing observations. None and [] stay distinct all
    the way down: [] is "looked, the branch changes nothing", None is the
    absence of an answer, and only a non-empty list is positive knowledge.
    """
    name = (branch or "").strip()
    if not name:
        return None
    try:
        ctx = await project_git_context(db, task["id"])
        return await plugins.git_ops.branch_diff_paths(
            name, base_branch=ctx.get("base_branch"), repo=ctx.get("repo")
        )
    except Exception as exc:  # noqa: BLE001 - ignorance is not an accusation (#498)
        log.warning("branch diff for #%s (%s) unavailable: %s", task["id"], name, exc)
        return None


async def ensure_delivery_pr(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    branch: str,
    diff_paths: list[str] | None,
) -> tuple[int | None, str]:
    """Open the PR a commit-carrying branch needs, or say why it could not (#967).

    The headless conveyor has always done this (push, then create_pr); the
    pair flow only ever LOOKED for a PR, so four tasks in one week (#961,
    #963, #965, #966) completed with their work stranded on a branch — the
    PR was opened by hand seconds after the gate had already walked past.

    Called with the diff the caller already resolved: at submission that is
    the #583 resolution feeding the surface check, at done it is
    :func:`_confirmed_branch_diff` — either way ONE observation decides both
    the warning and this action. An empty or unknown diff returns ``(None,
    "")``: nothing is insisted on without positive knowledge (#498).

    Runs against the project clone, not the task's worktree: a worktree
    shares the ref store and objects with the clone, and both operations
    here — push and ``gh pr create`` — act on refs, never on a checkout.
    push is best-effort by design: a branch pushed from another machine has
    no local ref to push, and create_pr against origin still succeeds.

    A filled reason means "commits are confirmed and no PR could be opened"
    — the one caller-facing state that must not complete silently. Never
    raises: a done report must not 500 because GitHub blinked.
    """
    if not branch or not diff_paths:
        return None, ""
    task_id = int(task["id"])
    pushed = False
    try:
        ctx = await project_git_context(db, task_id)
        pushed = await plugins.git_ops.push_branch(branch, repo=ctx.get("repo"))
        if not pushed:
            log.info(
                "ensure_delivery_pr: push refused for #%s (%s) — "
                "the branch may already live on origin",
                task_id,
                branch,
            )
        pr = await plugins.git_ops.create_pr(
            task_id,
            task.get("title") or f"task #{task_id}",
            task.get("description") or "",
            branch,
            repo=ctx.get("repo"),
            gh_repo=ctx.get("gh_repo"),
            base_branch=ctx.get("base_branch"),
        )
    except Exception as exc:  # noqa: BLE001 - a cause, not a failure
        log.warning("ensure_delivery_pr failed for #%s (%s): %s", task_id, branch, exc)
        return None, f"PR для ветки {branch} открыть не удалось: {exc}"
    if not pr:
        return None, (
            f"PR для ветки {branch} открыть не удалось"
            + ("" if pushed else " (push тоже не прошёл)")
        )
    await repo.update_task(db, task_id, pr_number=int(pr))
    return int(pr), ""


async def pr_for_delivery(db: aiosqlite.Connection, task: dict[str, Any]) -> DeliveryPR:
    """The PR that carries this task's branch, looked up at done time (#767).

    Discovery used to happen once, inside ``submit_for_review``. A PR opened
    AFTER the submission therefore never reached ``pr_number`` — and the
    delivery gate below keys on that field, so the task completed with its
    work still sitting in a branch. Observed on #725: APPROVED, green CI, and
    "Task completed" over an open PR #336. Nothing in the flow forbids that
    order; the hub simply stopped looking.

    Returns a :class:`DeliveryPR`. A filled number is recorded on the task
    so the gate and the drift guard see the same PR. An empty number with an
    empty reason means "this task has no branch to carry a PR" — today's
    behaviour for config and docs work, and no network call is made for it.
    A reason is filled only when the lookup itself could not answer, which is
    a cause to report, never an exception to raise (AC-4).
    """
    branch = (task.get("branch") or "").strip()
    recorded = int(task["pr_number"]) if task.get("pr_number") else None
    if recorded is not None:
        # The recorded number is a cached observation, not a fact — the same
        # thing #767 established for an empty field, now for a stale one. A PR
        # can close without its author: a stacked PR dies when its base branch
        # is merged and deleted, which is exactly what stranded #774 with a
        # live branch, a green PR, and a closed number on the task.
        state, note = await _recorded_pr_state(db, task, recorded)
        # #959: "closed" and "absent" are different facts with one consequence
        # — the recorded number cannot carry this delivery, so look for the one
        # that can. They are NOT merged into a single state: the feed has to
        # say which of the two happened, or the next reader debugging a stuck
        # task learns nothing from the line that explained it.
        if state in ("closed", "absent"):
            gone = (
                f"записанный PR #{recorded} закрыт и не слит"
                if state == "closed"
                else f"записанного PR #{recorded} нет в репозитории проекта"
            )
            replacement, find_note = (
                await _live_pr_for_branch(db, task, branch) if branch else (None, "")
            )
            if replacement and replacement != recorded:
                return DeliveryPR(
                    replacement,
                    f"{gone} — доставка идёт открытым PR #{replacement} той же ветки",
                )
            return DeliveryPR(
                recorded,
                find_note
                or (
                    f"{gone}, а открытого PR у ветки "
                    f"{branch or '(ветки нет)'} не нашлось"
                ),
                unusable=True,
            )
        # merged, open, or unknown: the number stands. "Merged" must never be
        # replaced — a second merge is not extra safety, and #605 already had
        # to guard that. "Unknown" keeps today's behaviour with a named cause,
        # and carries that it is unknown so the waiting branch does not promise
        # a green CI for a PR nobody could reach (#959).
        return DeliveryPR(recorded, note, established=bool(state))
    if not branch:
        return DeliveryPR()
    try:
        ctx = await project_git_context(db, task["id"])
        found = await plugins.git_ops.pr_for_branch(
            branch, repo=ctx.get("repo"), gh_repo=ctx.get("gh_repo")
        )
    except Exception as exc:  # noqa: BLE001 - a cause, not a failure (AC-4)
        log.warning(
            "PR lookup at done failed for #%s (%s): %s", task["id"], branch, exc
        )
        return DeliveryPR(reason=f"поиск PR по ветке {branch} не ответил: {exc}")
    if found is None:
        return DeliveryPR(
            reason=f"открытый PR для ветки {branch} не найден — доставка не проверялась"
        )
    await repo.update_task(db, task["id"], pr_number=found)
    return DeliveryPR(int(found))


async def transition_after_agent_done(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    *,
    has_done: bool,
    exit_code: int | None = None,
    result_text: str | None = None,
) -> str:
    """Post-done lifecycle shared by headless poller and pair mode."""
    task_id = task["id"]
    branch = task.get("branch")

    if has_done and not completion_requires_review(task):
        # Delivery gate (#605): a task that owns a PR completes only once
        # that PR is merged — "completed" must mean delivered, the invariant
        # #363 established for headless and the pair flow never had. Tasks
        # without a PR (config work, docs) are untouched. force_complete and
        # decide-accept are human overrides and bypass this by design.
        #
        # #767: an empty pr_number is no longer taken as "nothing to deliver".
        # It has two meanings — "this work needs no PR" and "a PR exists and
        # the hub never learned its number" — and the second one completed
        # tasks over unmerged branches. The lookup runs here, at done time,
        # instead of only at submission.
        pr_note = ""
        # Bound before the branch: the refusal below reads it, and a name that
        # exists only on one path is a trap for the next edit.
        delivery_pr = DeliveryPR()
        if not task.get("job_id"):
            delivery_pr = await pr_for_delivery(db, task)
            if delivery_pr.number:
                task = {**task, "pr_number": delivery_pr.number}
            pr_note = delivery_pr.reason
            if not task.get("pr_number"):
                # #967: the lookup finding nothing is no longer the end of the
                # question. When git positively confirms commits on the branch,
                # completing without a PR knowingly strands them — the state
                # #961/#963/#965/#966 all reached in one week, each fixed by a
                # human opening the PR after the fact. The hub opens it here
                # and hands it to the same gate below. Without that knowledge
                # (no branch, empty diff, git silent) today's path stands
                # untouched — #498's rule that ignorance is not an accusation.
                diff = await _confirmed_branch_diff(db, task, branch)
                created, create_reason = await ensure_delivery_pr(
                    db, task, (branch or "").strip(), diff
                )
                if created:
                    task = {**task, "pr_number": created}
                    delivery_pr = DeliveryPR(created)
                    # The lookup's "PR not found" note would now read as "the
                    # delivery went unchecked" over a gate that IS checking it.
                    pr_note = ""
                    await repo.add_task_update(
                        db,
                        task_id,
                        "hub",
                        "status",
                        f"PR #{created} открыт хабом для доставки ветки "
                        f"{branch}: на ней {len(diff or [])} изменённых "
                        "файл(ов), а открытого PR не было (#967).",
                    )
                elif create_reason:
                    await repo.update_task(db, task_id, status="needs_decision")
                    await repo.add_task_update(
                        db,
                        task_id,
                        "hub",
                        "alert",
                        # #952: names only actions needs_decision accepts.
                        f"Done report NOT completed: ветка {branch} меняет "
                        f"{len(diff or [])} файл(ов), а {create_reason}. "
                        "Решение за человеком (hub_decide_task): rework "
                        "вернёт задачу в running — откройте PR руками или "
                        "почините доступ к GitHub и пересдайте done; accept "
                        "завершит задачу БЕЗ доставки ветки.",
                    )
                    await repo.insert_event(
                        db,
                        kind="needs_decision",
                        task_id=task_id,
                        actor=task.get("assigned_agent") or "agent",
                        payload={
                            "reason": "delivery_pr_missing",
                            "detail": create_reason,
                        },
                    )
                    log.info(
                        "Task #%d → needs_decision: commits confirmed on %s "
                        "and no PR could be opened (%s)",
                        task_id,
                        branch,
                        create_reason,
                    )
                    return "needs_decision"
        if pr_note:
            # The gate could not look. Completion still follows today's rule,
            # but the reader is told which check did not run — an absent line
            # here would read as "there was nothing to deliver" (AC-4).
            await repo.add_task_update(db, task_id, "hub", "alert", pr_note)
        if task.get("pr_number") and not task.get("job_id"):
            if delivery_pr.unusable:
                # Nothing to merge: the recorded PR is closed and nothing
                # replaces it. Refusing here is the decision the merge gate
                # would reach anyway, taken before touching GitHub.
                ok, detail = False, delivery_pr.reason
            else:
                ok, detail = await merge_before_completion(db, task)
            if not ok:
                # #951: a temporary state is not a decision. CI still running
                # (or unreadable this minute) resolves itself — the poller in
                # the same situation just retries next pass, and on 25.08.2026
                # the done-flow's needs_decision here cost a human rework for
                # a CI that went green four minutes later (#949). The task
                # stays in running: the existing stale watches and the #418
                # deadline backstop keep it from waiting forever. Terminal
                # refusals — a red CI, a merge GitHub refused, a closed PR —
                # still call a human below: those need an actual decision.
                if detail.startswith(TRANSIENT_GATE_PREFIXES):
                    # #959: waiting is still right, but the reason has to be
                    # the true one. When the PR itself could not be read, "wait
                    # for a green CI" names a check that never ran and points
                    # at a PR the gate never established — the shape of hint
                    # #952 removed from the terminal branch, here in the
                    # patient one.
                    cause = (
                        "Это временное состояние, решение человека не требуется: "
                        "отчитайтесь о готовности снова, когда CI станет зелёным."
                        if delivery_pr.established
                        else "Состояние самого PR прочитать не удалось, поэтому "
                        "про CI тут сказать нечего: отчитайтесь о готовности "
                        "снова, когда PR станет доступен. Если он недоступен "
                        "не временно — это вопрос к человеку."
                    )
                    await repo.add_task_update(
                        db,
                        task_id,
                        "hub",
                        "alert",
                        f"Доставка отложена: PR #{task['pr_number']} — "
                        f"{detail}. {cause}",
                    )
                    log.info(
                        "Task #%d stays running: merge gate waiting on CI (%s)",
                        task_id,
                        detail,
                    )
                    return "running"
                await repo.update_task(db, task_id, status="needs_decision")
                await repo.add_task_update(
                    db,
                    task_id,
                    "hub",
                    "alert",
                    # #952: this line is written TOGETHER with the transition
                    # to needs_decision, and is read AFTER it — so it may only
                    # name actions that status accepts. "Report done again"
                    # is not one of them: the hub itself refuses it there
                    # (human_decision_required), which on 25.08.2026 sent an
                    # agent down a dead end the hint had pointed to (#949).
                    f"Done report NOT completed: PR #{task['pr_number']} is not "
                    f"delivered — {detail}. Решение за человеком "
                    "(hub_decide_task): rework вернёт задачу в running — "
                    "устраните причину и пересдайте done; accept завершит "
                    "задачу БЕЗ доставки PR.",
                )
                await repo.insert_event(
                    db,
                    kind="needs_decision",
                    task_id=task_id,
                    actor=task.get("assigned_agent") or "agent",
                    payload={"reason": "merge_gate", "detail": detail},
                )
                log.info(
                    "Task #%d → needs_decision: merge gate refused (%s)",
                    task_id,
                    detail,
                )
                return "needs_decision"
            # #812: delivery succeeded, so the release range grew. Opening or
            # refreshing the release PR is best-effort and never blocks the
            # done report: the report answers about this task, and a release
            # that could not be prepared is a reason in the log, not a failure
            # of the work that is already in develop.
            from hub.services.release import open_release_for_task

            try:
                await open_release_for_task(db, task_id)
            except Exception as exc:  # noqa: BLE001 - a cause, not a failure
                log.warning("release PR not prepared for #%s: %s", task_id, exc)

        # Review gate satisfied: either an explicit auto_review opt-out or
        # the current submission already has an APPROVED verdict. Complete
        # WITHOUT bumping the generation — no new work is being submitted,
        # and a bump would invalidate the very approval that authorizes
        # this completion (#306).
        await repo.update_task(
            db,
            task_id,
            status="completed",
            exit_code=exit_code,
            result_text=result_text,
        )
        await repo.insert_event(
            db,
            kind="task_completed",
            task_id=task_id,
            actor=task.get("assigned_agent") or "agent",
            payload={"via": "report_done"},
        )
        log.info("Task #%d → completed after done report", task_id)
        return "completed"

    if has_done:
        # Unreviewed done report = a work submission (#305): bumping the
        # generation invalidates any APPROVED verdict from earlier work.
        await repo.bump_submission_generation(db, task_id)

    if (
        task.get("auto_review")
        and not review_budget_exhausted(task.get("review_cycle", 0))
        and has_done
        and branch
    ):
        ctx = await project_git_context(db, task_id)
        workspace = ctx.get("repo")
        # #991: the CI gate asks for a PR where it means "is there anything to
        # deliver". For a task whose work is not code — a policy turned on, a
        # mechanism watched, a decision recorded — the branch exists (pair_start
        # always makes one) and carries nothing the base does not have. There is
        # no PR to open, so the poller retried and escalated: #927 sat in
        # needs_decision with "Cannot create PR: no commits on branch or push
        # failed" while its work was finished and evidenced by three live checks.
        #
        # Ask about substance instead, with the method the release path already
        # uses (#968). Three answers, and only one skips: content that does not
        # differ means the CI gate has no subject. "Could not compare" keeps the
        # old path — ignorance must not close a task quietly (#725). The REVIEW
        # gate is untouched either way: skipping it here would let anything
        # uncommitted complete itself, which is a worse defect than the one
        # being fixed.
        # TWO sources decide this, not one. The tail below starts with
        # auto_commit, which exists precisely because the work may still be
        # sitting UNCOMMITTED in the working tree — asking only about commits
        # would call that work "nothing to deliver" and skip the commit that
        # would have delivered it. So: uncommitted changes count as work, and
        # only when the tree is clean does the branch-vs-base comparison get
        # to answer.
        # The base default belongs to git_ops (_resolve_base, #362 I4) — an
        # empty base is passed through, never recomputed here.
        nothing_to_deliver = False
        dirty_now = await plugins.git_ops.dirty_paths(repo=workspace)
        if not dirty_now:
            base_ref = (ctx.get("base_branch") or "").strip()
            differs = await plugins.git_ops.content_differs(
                base_ref, branch, repo=workspace, gh_repo=ctx.get("gh_repo")
            )
            nothing_to_deliver = differs is False
        if nothing_to_deliver:
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "status",
                f"Доставлять нечего: ветка {branch} не отличается по "
                "содержимому от базовой ветки проекта, PR открывать не из "
                "чего. "
                "Гейт CI пропущен как беспредметный — ревью задача проходит "
                "обычным порядком (#991). Если работа должна была быть в "
                "коде, значит она не закоммичена.",
            )
            log.info(
                "Task #%d: nothing to deliver on %s — CI gate skipped",
                task_id,
                branch,
            )
        # Worktree mode (#459): a PAIR task's branch is checked out in its own
        # worktree while the main clone stays on base; targeting the main clone
        # would silently fail checkout and let squash_branch reset the base
        # branch. Only redirect for pair tasks (no job_id) whose worktree
        # actually exists — headless dispatch tasks (job_id set) build their
        # branch in the main clone and never create a worktree, so redirecting
        # them at a nonexistent path would crash the poller's done-pipeline.
        git_repo = workspace
        if worktree_per_task_enabled() and not task.get("job_id"):
            wt = plugins.git_ops.worktree_path(task_id, workspace)
            if wt and os.path.isdir(wt):
                git_repo = wt
        # #361 I1: the result of this checkout was never inspected, and the
        # comment above already names the consequence — squash_branch resetting
        # the wrong branch. checkout() has always returned a bool; nobody read
        # it. Every step below rewrites history or pushes, so a failed checkout
        # must stop the tail rather than run it against whatever branch is
        # current. Silence here is what docs/workspace-safety-policy.md
        # invariant 4 forbids.
        if not await plugins.git_ops.checkout(branch, repo=git_repo):
            await repo.update_task(
                db,
                task_id,
                status="needs_decision",
                exit_code=exit_code,
                result_text=result_text,
            )
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "blocker",
                f"Не удалось перейти на ветку {branch!r} в {git_repo} — "
                "git-хвост done-конвейера (commit, squash, push, PR) не "
                "выполнялся, чтобы не тронуть чужую ветку. Проверьте состояние "
                "рабочего каталога и решите через hub_decide_task.",
            )
            await repo.insert_event(
                db,
                kind="needs_decision",
                task_id=task_id,
                actor="hub",
                payload={"reason": "done_checkout_failed", "branch": branch},
            )
            log.error(
                "Task #%d → needs_decision: cannot check out %r in %s",
                task_id,
                branch,
                git_repo,
            )
            return "needs_decision"
        # Commit-scope gate (#361 AC-1). auto_commit stages the whole tree, and
        # the tree was only proven clean at branch creation — a headless task
        # then shares the main clone for its entire run, so an edit made by
        # anyone else in that window is dirty here and looks exactly like the
        # task's own work. affected_areas is the only attribution the hub has.
        # It is a weak one (an agent may legitimately touch more than the task
        # predicted), which is why 'require' escalates to a human rather than
        # dropping files, and why the default is 'warn'.
        scope_mode = (config.COMMIT_SCOPE_GATE or "warn").strip().lower()
        if scope_mode != "off":
            areas = deserialize_str_list(task.get("affected_areas"))
            dirty = await plugins.git_ops.dirty_paths(repo=git_repo)
            if not areas:
                # No declared scope means the check could not run. Say so —
                # silence here would read as "checked and clean" (#537).
                if dirty:
                    await repo.add_task_update(
                        db,
                        task_id,
                        "hub",
                        "status",
                        "Проверка области коммита не выполнялась: у задачи не "
                        f"объявлены affected_areas. В коммит уйдут {len(dirty)} "
                        "файлов без сверки с областью задачи.",
                    )
            else:
                foreign = commit_scope.foreign_paths(dirty, areas)
                if foreign and scope_mode == "require":
                    listed = ", ".join(foreign[:10])
                    error = (
                        f"В рабочем каталоге {git_repo} есть изменения вне "
                        f"объявленной области задачи #{task_id}: {listed}. "
                        "git-хвост остановлен, чтобы чужая работа не ушла в PR "
                        "задачи."
                    )
                    await repo.update_task(
                        db,
                        task_id,
                        status="needs_decision",
                        exit_code=exit_code,
                        result_text=error,
                    )
                    await repo.add_task_update(
                        db,
                        task_id,
                        "hub",
                        "blocker",
                        f"{error} Объявленная область: {', '.join(areas)}. "
                        "Решите через hub_decide_task: расширить область, "
                        "убрать чужие правки или закоммитить как есть.",
                    )
                    await repo.insert_event(
                        db,
                        kind="needs_decision",
                        task_id=task_id,
                        actor="hub",
                        payload={
                            "reason": "commit_scope_violation",
                            "branch": branch,
                            "foreign": foreign[:20],
                        },
                    )
                    log.error(
                        "Task #%d → needs_decision: %d file(s) outside scope",
                        task_id,
                        len(foreign),
                    )
                    return "needs_decision"
                if foreign:
                    await repo.add_task_update(
                        db,
                        task_id,
                        "hub",
                        "status",
                        f"В коммит задачи уходят {len(foreign)} файлов вне "
                        f"объявленной области: {', '.join(foreign[:10])}. "
                        "Режим проверки — warn, коммит выполнен. Включите "
                        "HAIPLANE_COMMIT_SCOPE=require, чтобы останавливать.",
                    )
        # expected_branch is defence in depth: the checkout above may report
        # success while HEAD ends up elsewhere. Its refusal RAISES rather than
        # returning False, because False also means "nothing to commit" and
        # collapsing the two left the guard inert — squash and push ran anyway.
        try:
            await plugins.git_ops.auto_commit(
                task_id,
                title=task.get("title", ""),
                repo=git_repo,
                expected_branch=branch,
            )
        except WorkspaceBranchMismatchError as exc:
            await repo.update_task(
                db,
                task_id,
                status="needs_decision",
                exit_code=exit_code,
                result_text=str(exc),
            )
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "blocker",
                f"{exc} — git-хвост done-конвейера остановлен, чтобы не "
                f"переписать историю чужой ветки. Решите через hub_decide_task.",
            )
            await repo.insert_event(
                db,
                kind="needs_decision",
                task_id=task_id,
                actor="hub",
                payload={"reason": "done_branch_mismatch", "branch": branch},
            )
            log.error("Task #%d → needs_decision: %s", task_id, exc)
            return "needs_decision"
        # Fall through to the review gate when there is nothing to deliver:
        # commit, squash, push and PR all have no subject, and the task
        # still owes a verdict — it just owes no pull request (#991).
        if not nothing_to_deliver:
            squashed = await plugins.git_ops.squash_branch(
                task_id,
                task.get("title", ""),
                branch,
                repo=git_repo,
                base_branch=ctx.get("base_branch"),
            )
            await plugins.git_ops.push_branch(branch, repo=git_repo, force=squashed)
            if not task.get("pr_number"):
                pr_num = await plugins.git_ops.create_pr(
                    task_id,
                    task["title"],
                    task.get("description", ""),
                    branch,
                    repo=git_repo,
                    gh_repo=ctx.get("gh_repo"),
                    base_branch=ctx.get("base_branch"),
                )
                if pr_num:
                    await repo.update_task(db, task_id, pr_number=pr_num)
                    task["pr_number"] = pr_num
            await repo.update_task(
                db,
                task_id,
                status="ci_check",
                exit_code=exit_code,
                result_text=result_text,
            )
            log.info("Task #%d → ci_check after done report", task_id)
            return "ci_check"

    if has_done and review_budget_exhausted(task.get("review_cycle", 0)):
        # Review cycle limit reached without approval: escalate to the human
        # Decision Gate instead of looping through review forever (#306).
        await repo.update_task(
            db,
            task_id,
            status="needs_decision",
            exit_code=exit_code,
            result_text=result_text,
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Review cycle limit reached ({task.get('review_cycle', 0)}/"
            f"{config.MAX_REVIEW_CYCLES}) without APPROVED review. "
            "Human decision required (hub_decide_task).",
        )
        await repo.insert_event(
            db,
            kind="needs_decision",
            task_id=task_id,
            actor="hub",
            payload={"reason": "review_cycle_limit"},
        )
        log.info("Task #%d → needs_decision (review cycle limit)", task_id)
        return "needs_decision"

    if has_done:
        # Universal Review Gate (#306): a done report on an unreviewed task
        # is a submission for review, not a completion. Route to
        # client-driven review (no review_job_id) and tell the agent how to
        # obtain the verdict.
        task_row = await repo.get_task(db, task_id)
        generation_num = (
            dict(task_row).get("submission_generation", 0) if task_row else 0
        )
        await repo.update_task(
            db,
            task_id,
            status="review",
            review_job_id=None,
            exit_code=exit_code,
            result_text=result_text,
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "status",
            f"Universal Review Gate: done report routed to review "
            f"(submission #{generation_num}). Obtain an APPROVED verdict via "
            "hub_submit_review (reviewer: hub_get_review_brief), then report "
            "done again.",
        )
        log.info("Task #%d → review after done report (review gate)", task_id)
        return "review"

    await repo.update_task(
        db,
        task_id,
        status="pending_report",
        exit_code=exit_code,
        result_text=result_text,
    )
    log.info("Task #%d → pending_report after done report", task_id)
    return "pending_report"


def review_budget_exhausted(review_cycle: int, max_cycles: int | None = None) -> bool:
    """Whether the review fix budget is spent (#423) — one source of truth.

    ``review_cycle`` is the number of developer fix iterations already
    dispatched. The budget is exhausted — the next CHANGES_REQUESTED escalates
    (to arbiter / needs_decision) instead of dispatching another fix — once that
    count reaches ``max_cycles``. Pair and headless share this, so at MAX=3 both
    run fixes 1, 2 and 3 and escalate the 4th. ``MAX <= 0`` is exhausted
    immediately. No flow may compare review_cycle to MAX_REVIEW_CYCLES itself.
    """
    if max_cycles is None:
        max_cycles = config.MAX_REVIEW_CYCLES
    return review_cycle >= max_cycles


async def dispatch_review(
    db: aiosqlite.Connection,
    task: dict[str, Any],
) -> None:
    """Dispatch a code-review job for a completed task."""
    task_id = task["id"]
    review_cycle = task.get("review_cycle", 0)
    breadcrumb = await get_breadcrumb_str(db, task_id)
    message = plugins.dispatch.build_review_message(
        task_id=task_id,
        title=task["title"],
        description=task.get("description", ""),
        review_cycle=review_cycle,
        max_cycles=config.MAX_REVIEW_CYCLES,
        branch=task.get("branch", ""),
        pr_number=task.get("pr_number"),
        breadcrumb=breadcrumb,
    )
    result = await plugins.dispatch.submit_task(
        message,
        runtime=config.REVIEW_RUNTIME,
        agent=config.REVIEW_AGENT,
        task_id=task_id,
    )
    review_job_id = result.get("job_id")
    if review_job_id:
        await repo.update_task(
            db, task_id, status="review", review_job_id=review_job_id
        )
        log.info(
            "Poll: task #%d → review (job=%s, agent=%s, cycle=%d)",
            task_id,
            review_job_id,
            config.REVIEW_AGENT,
            review_cycle + 1,
        )
    else:
        log.warning(
            "Poll: failed to dispatch review for task #%d: %s",
            task_id,
            result.get("error"),
        )
        # Universal Review Gate (#309): failure to dispatch a reviewer must
        # never complete the task — escalate to the human Decision Gate.
        await repo.update_task(db, task_id, status="needs_decision")
        await repo.insert_event(
            db,
            kind="needs_decision",
            task_id=task_id,
            actor="hub",
            payload={"reason": "review_dispatch_failed"},
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Reviewer dispatch failed: {result.get('error', 'no job_id')}. "
            "Universal Review Gate: manual decision required (hub_decide_task).",
        )
    await db.commit()


async def dispatch_fix(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    review_comments: str,
) -> None:
    """Dispatch a fix job back to the developer agent."""
    task_id = task["id"]
    review_cycle = task.get("review_cycle", 0) + 1
    message = plugins.dispatch.build_fix_message(
        task_id=task_id,
        title=task["title"],
        description=task.get("description", ""),
        review_comments=review_comments,
        review_cycle=review_cycle,
        max_cycles=config.MAX_REVIEW_CYCLES,
        branch=task.get("branch", ""),
    )
    runtime = task.get("runtime", "auto")
    result = await plugins.dispatch.submit_task(
        message, runtime=runtime, task_id=task_id
    )
    job_id = result.get("job_id")
    if job_id:
        await repo.update_task(
            db,
            task_id,
            status="fix_requested",
            job_id=job_id,
            review_cycle=review_cycle,
        )
        log.info(
            "Poll: task #%d → fix_requested (job=%s, cycle=%d/%d)",
            task_id,
            job_id,
            review_cycle,
            config.MAX_REVIEW_CYCLES,
        )
    else:
        log.warning(
            "Poll: failed to dispatch fix for task #%d: %s",
            task_id,
            result.get("error"),
        )
        # Universal Review Gate (#309): CHANGES_REQUESTED work must not
        # silently complete when the fix dispatch fails.
        await repo.update_task(
            db,
            task_id,
            status="needs_decision",
            review_cycle=review_cycle,
        )
        await repo.insert_event(
            db,
            kind="needs_decision",
            task_id=task_id,
            actor="hub",
            payload={"reason": "fix_dispatch_failed"},
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Fix dispatch failed after CHANGES_REQUESTED: "
            f"{result.get('error', 'no job_id')}. Manual decision required.",
        )
    await db.commit()


async def dispatch_arbiter(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    updates_list: list[dict[str, Any]],
) -> None:
    """Dispatch an arbiter (Claude Sonnet) when review cycle limit is reached.

    At-most-once per submission generation (#421): a conditional claim persists
    a ``dispatching`` marker BEFORE the external submit, so a repeat poll or a
    restart finds it and never dispatches a second paid job. The marker moves to
    ``running`` with the job id on success; a crash between submit and job id
    leaves ``dispatching`` for the poller's ambiguity watchdog to resolve.
    """
    task_id = task["id"]
    generation = task.get("submission_generation") or 0

    claimed = await repo.claim_arbiter_dispatch(db, task_id, generation)
    if not claimed:
        await db.commit()
        log.info(
            "Poll: task #%d arbiter already claimed for generation %d, skipping",
            task_id,
            generation,
        )
        return
    # The marker must be durable before the external side effect.
    await db.commit()

    review_cycle = task.get("review_cycle", 0)
    review_history = [
        u
        for u in updates_list
        if u.get("kind") in ("review", "done", "status", "alert")
    ]

    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Review cycle limit reached ({review_cycle}/{config.MAX_REVIEW_CYCLES}). "
        "Dispatching arbiter for independent assessment.",
    )
    await db.commit()
    await log_activity(
        db,
        "review_cycle_limit",
        f"Task #{task_id}: review cycle limit ({review_cycle}/{config.MAX_REVIEW_CYCLES}), dispatching arbiter",
    )

    message = plugins.dispatch.build_arbiter_message(
        task_id=task_id,
        title=task["title"],
        description=task.get("description", ""),
        review_history=review_history,
        review_cycle=review_cycle,
        max_cycles=config.MAX_REVIEW_CYCLES,
        branch=task.get("branch", ""),
    )
    result = await plugins.dispatch.submit_task(
        message,
        runtime=config.ARBITER_RUNTIME,
        agent=config.ARBITER_AGENT,
        task_id=task_id,
    )
    arbiter_job_id = result.get("job_id")
    if arbiter_job_id:
        await repo.mark_arbiter_running(db, task_id, arbiter_job_id)
        await repo.update_task(
            db, task_id, status="review", review_job_id=arbiter_job_id
        )
        log.info("Poll: task #%d → arbiter review (job=%s)", task_id, arbiter_job_id)
    else:
        # A definite submit failure (the call returned, no job id): finish the
        # marker and escalate. Do not leave it dispatching — that is only for
        # the crash window where the call never returned.
        log.warning(
            "Poll: failed to dispatch arbiter for task #%d: %s",
            task_id,
            result.get("error"),
        )
        await repo.update_task(
            db, task_id, status="needs_decision", arbiter_state="finished"
        )
        await repo.insert_event(
            db,
            kind="needs_decision",
            task_id=task_id,
            actor="hub",
            payload={"reason": "arbiter_dispatch_failed"},
        )
    await db.commit()


async def dispatch_ci_fix(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ci_failures: dict[str, Any],
) -> None:
    """Dispatch developer to fix CI failures."""
    task_id = task["id"]
    ci_fix_cycle = task.get("ci_fix_cycle", 0)
    message = plugins.dispatch.build_ci_fix_message(
        task_id=task_id,
        title=task["title"],
        description=task.get("description", ""),
        ci_failures=ci_failures,
        ci_fix_cycle=ci_fix_cycle,
        max_cycles=config.MAX_CI_FIX_CYCLES,
        branch=task.get("branch", ""),
    )
    runtime = task.get("runtime", "auto")
    result = await plugins.dispatch.submit_task(
        message, runtime=runtime, task_id=task_id
    )
    job_id = result.get("job_id")
    if job_id:
        branch = task.get("branch")
        if branch:
            await plugins.git_ops.checkout(branch)
        await repo.update_task(
            db,
            task_id,
            status="running",
            job_id=job_id,
            ci_fix_cycle=ci_fix_cycle + 1,
        )
        log.info(
            "Poll: task #%d → running (CI fix, cycle=%d/%d)",
            task_id,
            ci_fix_cycle + 1,
            config.MAX_CI_FIX_CYCLES,
        )
    else:
        log.warning(
            "Poll: failed to dispatch CI fix for task #%d: %s",
            task_id,
            result.get("error"),
        )
        await repo.update_task(db, task_id, status="needs_decision")
        await repo.insert_event(
            db,
            kind="needs_decision",
            task_id=task_id,
            actor="hub",
            payload={"reason": "ci_fix_dispatch_failed"},
        )
    await db.commit()


def extract_review_verdict(
    task_id: int,
    review_job_id: str,
    db_updates: list[dict[str, Any]],
) -> str | None:
    """Return 'approved' or 'changes_requested' from task_updates or full dispatch log.

    Search order:
    1. task_updates with kind='review' — scan all lines for verdict
    2. Full dispatch log — scan all lines for the LAST occurrence of a verdict keyword
    """
    for u in reversed(db_updates):
        if u.get("kind") == "review":
            text = u.get("content", "").strip()
            verdict = scan_text_for_verdict(text)
            if verdict:
                return verdict

    full_log = plugins.dispatch.job_log_full(review_job_id)
    if full_log:
        verdict = scan_text_for_verdict(full_log)
        if verdict:
            log.info(
                "Poll: task #%d verdict '%s' extracted from dispatch log",
                task_id,
                verdict,
            )
            return verdict

    return None


def scan_text_for_verdict(text: str) -> str | None:
    """Scan text for the last occurrence of APPROVED or CHANGES_REQUESTED."""
    last_verdict: str | None = None
    for line in text.split("\n"):
        line_lower = line.strip().lower()
        if "changes_requested" in line_lower:
            last_verdict = "changes_requested"
        elif (
            line_lower.rstrip().endswith("approved") or line_lower.strip() == "approved"
        ):
            last_verdict = "approved"
    return last_verdict


async def maybe_destroy_vast(
    db: aiosqlite.Connection,
    task: dict[str, Any],
) -> None:
    """Destroy Vast instance if no active Vast tasks remain."""
    if not config.VAST_ENABLED:
        return
    if await plugins.vast.has_active_vast_tasks(db):
        return
    status = await plugins.vast.vast_status()
    if not status.get("managed"):
        return
    log.info("No active Vast tasks remaining, destroying instance")
    await plugins.vast.vast_down()
    await log_activity(
        db,
        "vast_shutdown",
        f"Vast instance destroyed after task #{task['id']} finished",
    )


async def get_breadcrumb_str(
    db: aiosqlite.Connection,
    task_id: int,
) -> str:
    """Build a human-readable breadcrumb string for dispatch messages."""
    crumbs = await get_breadcrumb(db, task_id)
    if len(crumbs) <= 1:
        return ""
    return " > ".join(
        f"{c['task_type'].capitalize()}: {c['title']} (#{c['id']})" for c in crumbs
    )
