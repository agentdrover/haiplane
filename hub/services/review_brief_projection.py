"""Compact MCP projection of the review brief, with paged sections (#1682).

The REST brief is unchanged: median ~37k characters, maximum 207k, and a client
that cannot take that much saves the answer to a file and shows the agent 0-2 KB.
This module builds what ``hub_get_review_brief`` and ``hp-hub review-brief`` hand
out instead: a projection of at most ``MAX_CHARS`` characters, measured on the
FINAL ``CallToolResult`` with the SDK serializer (not on the Python dict), whose
decision-critical part is never cut, and sections the reader pages through.

The projection is a pure function of the brief dict: it never mutates it, and
nothing here is reachable from REST, ``steward-evidence``, the reviewer prompts
or ``packet_hash``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from mcp.types import CallToolResult

from hub.services import evidence_states
from hub.services.review_brief_fields import MANDATORY, TEXT, fields_of
from hub.mcp_structured import structured_echo_result, structured_error_result

logger = logging.getLogger(__name__)

MAX_CHARS = 12_000
EXCEEDED_NOTE = "превышен потолок"
FINDINGS_SHOWN = 10
TITLE_CAP = 200
AC_SHOWN = 20
SYMBOLS_SHOWN = 10
ADDRESSES_SHOWN = 5
STATEMENT_CAP = 1_500
CAVEAT_CAP = 600

LIST_TAG = "_list"
SECTIONS = (
    "call_sites",
    "acceptance_criteria",
    "statement",
    "machine_review",
    "verdict_findings",
    "full",
)
# Only prose excerpts are cut. Identifiers and locators (file, line, id, uid,
# linked task, scope, sha, ...) are carried whole: a cut path reads as another path.
PROSE_FIELDS = frozenset({"title", "message"})
_CHANGED = "раздел изменился (ветка сдвинулась или пересдача) — начните с offset=0"
# Heuristic states first: a reader with a short budget meets the sites that
# nobody touched before the ones that are fine.
_STATE_ORDER = ("untouched_sites", "only_tests", "no_callers")
# Every TEXT field of the classification, most useful first. A field missing
# here would have a class but no place in the answer; a test compares the two.
_STATEMENT_FIELDS = (
    "description",
    "technical_hints",
    "scope_growth",
    "latest_submission_summary",
    "scope_in",
    "scope_out",
    "validation_commands",
    "outcome_metric",
    "outcome_indicator",
    "outcome_deadline",
    "outcome_revisit_condition",
    "redesign_rationale",
)
_BLOCK_ORDER = ("acceptance_criteria", "call_sites", *_STATEMENT_FIELDS)


def result_chars(result: CallToolResult) -> int:
    """Characters of the result as the SDK puts it on the wire."""
    return len(result.model_dump_json(by_alias=True, exclude_none=True))


_FINALIZE: ContextVar[Callable[[CallToolResult], CallToolResult] | None] = ContextVar(
    "review_brief_finalize", default=None
)


@contextmanager
def finalizing(wrap: Callable[[CallToolResult], CallToolResult]) -> Iterator[None]:
    """Fit against the result AFTER the transport funnel has wrapped it.

    The funnel (InstrumentedFastMCP.call_tool) adds ``unknown_arguments`` to
    both halves of the result once the tool returned. The budget is measured
    on that final shape, so the caller passes the same wrapper here.
    """
    token = _FINALIZE.set(wrap)
    try:
        yield
    finally:
        _FINALIZE.reset(token)


def _size(result: CallToolResult) -> int:
    wrap = _FINALIZE.get()
    return result_chars(wrap(result) if wrap else result)


def mode_of(section: str) -> str:
    return "full" if section == "full" else ("section" if section else "projection")


def log_issue(
    result: CallToolResult, task_id: Any, mode: str, section: str, offset: Any
) -> int:
    """Write the size of EVERY issued answer to the log, with its mode (#1682)."""
    size = result_chars(result)
    exceeded = size > MAX_CHARS
    logger.log(
        logging.WARNING if exceeded and mode != "full" else logging.INFO,
        "review_brief_mcp task=%s mode=%s section=%s offset=%s chars=%s limit=%s",
        task_id,
        mode,
        section or "-",
        offset,
        size,
        MAX_CHARS,
    )
    return size


def _cap(value: Any, cap: int) -> str:
    text = str(value or "")
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _identity(brief: dict[str, Any]) -> dict[str, Any]:
    return {
        "submission_generation": brief.get("submission_generation", 0),
        "submission_sha": brief.get("submission_sha", ""),
        "current_branch_tip": brief.get("current_branch_tip", ""),
    }


# --- the part that is never cut ------------------------------------------------
#
# Driven by FIELD_CLASS (hub/services/review_brief_fields.py): every MANDATORY
# entry of the table is carried, so a field the table calls decision data
# cannot be forgotten here, and a new brief field cannot appear without a class.


def _rows(items: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    """The MANDATORY children of ``prefix`` for each element; free text is capped."""
    names = fields_of(MANDATORY, prefix)
    return [
        {
            n: _cap(it.get(n), TITLE_CAP) if n in PROSE_FIELDS else it.get(n)
            for n in names
        }
        for it in items
    ]


def _finding_digest(
    items: list[dict[str, Any]], prefix: str, section: str
) -> dict[str, Any]:
    """Up to FINDINGS_SHOWN rows of the listed children, the rest by count.

    ``section`` is the section that really holds the omitted items.
    """
    shown = _rows(items[:FINDINGS_SHOWN], prefix)
    omitted = len(items) - len(shown)
    return {
        "total": len(items),
        "shown": len(shown),
        "omitted": omitted,
        "hint": f"section={section}" if omitted else "",
        "items": shown,
    }


#: Lists of a machine review carried as one row per entry (decision values, no
#: free text), and lists carried as a digest of at most FINDINGS_SHOWN rows.
_ROW_LISTS = ("dispositions", "observation_closures", "deferred_repeats")
_DIGEST_LISTS = ("findings_confirmed", "unresolved")


def _machine_review_part(mr: dict[str, Any] | None) -> dict[str, Any] | None:
    """None stays None: no report is not a clean report.

    Every list is carried as ``<name>_count``; defect lists also as a digest and
    decision lists as rows. Scalars (incomplete=None, self_reviewed,
    principal_id, ...) as they are.
    """
    if not isinstance(mr, dict):
        return None
    part: dict[str, Any] = {}
    for name in fields_of(MANDATORY, "machine_review"):
        value = mr.get(name)
        if isinstance(value, list):
            part[f"{name}_count"] = len(value)
        else:
            part[name] = value
    # #1933: непрочитанные тесты AC названы поимённо - счёт «потеряно измерений»
    # не скажет, что именно из доказательств AC осталось без чтения.
    part["ac_tests_unread"] = list(mr.get("ac_tests_unread") or [])
    for name in _DIGEST_LISTS:
        part[name] = _finding_digest(
            mr.get(name) or [], f"machine_review.{name}", "machine_review"
        )
    for name in _ROW_LISTS:
        part[name] = _rows(mr.get(name) or [], f"machine_review.{name}")
    return part


def _latest_review_part(review: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(review, dict):
        return None
    part = {
        name: review.get(name)
        for name in fields_of(MANDATORY, "latest_review")
        if name != "findings"
    }
    findings = review.get("findings") or []
    part["findings_count"] = len(findings)
    part["findings"] = _finding_digest(
        findings, "latest_review.findings", "verdict_findings"
    )
    return part


def _review_report_part(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(report, dict):
        return None
    part = {
        name: report.get(name)
        for name in fields_of(MANDATORY, "review_report")
        if name != "machine_review"
    }
    inner = report.get("machine_review")
    # The report's own machine review is the same record as machine_review in
    # the usual case; its id makes any difference between the two visible.
    part["machine_review_id"] = inner.get("id") if isinstance(inner, dict) else None
    return part


def _base_merge_part(merge: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(merge, dict):
        return None
    return {
        "state": merge.get("state"),
        "reason": merge.get("reason"),
        "files_count": len(merge.get("files") or []),
    }


def _diff_base_part(base: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(base, dict):
        return None
    return {"state": base.get("state"), "reason": base.get("reason")}


def _ac_results_part(brief: dict[str, Any]) -> dict[str, Any]:
    """Current negative AC results by id - they survive a truncated AC block.

    Everything that is not a current pass is listed: fail and not_found, and
    any status this code does not know. A stale result is counted, not hidden.
    """
    results = brief.get("ac_test_results") or []
    current = [r for r in results if r.get("is_current")]
    return {
        "total": len(results),
        "current": len(current),
        "stale": len(results) - len(current),
        "negative": [
            {"ac_id": r.get("ac_id"), "status": r.get("status")}
            for r in current
            if r.get("status") != "pass"
        ],
    }


def _locators_part(brief: dict[str, Any]) -> dict[str, Any]:
    rows = brief.get("locator_resolution") or []
    part = {
        "total": len(rows),
        "rows": _rows(rows, "locator_resolution"),
        "unresolved": [
            {"ac_id": r.get("ac_id"), "status": r.get("status")}
            for r in rows
            if r.get("status") != "resolvable"
        ],
    }
    # #2026: AC not verified by a test — a locator does not apply, nothing more.
    summary = evidence_states.manual_ac_summary(brief.get("acceptance_criteria") or [])
    if summary:
        # The note is said once, in the text line (``_checks_lines``).
        part["not_applicable"] = {k: v for k, v in summary.items() if k != "note"}
    return part


def _verdict_route_part(route: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(route, dict):
        return None
    return {k: route.get(k) for k in ("code", "decider", "final", "line")}


def _freshness_part(block: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(block, dict):
        return None
    return {
        "state": block.get("state"),
        "reason": block.get("reason"),
        "deliveries_count": len(block.get("deliveries") or []),
        "omitted": block.get("omitted"),
    }


def _ci_evidence_part(brief: dict[str, Any]) -> dict[str, Any]:
    """Compact form (#2026): a missing block is ``state, expectation, run``.

    The long explanation of the state stays in the full brief (section=full):
    the mandatory part is never cut, so it must stay short.
    """
    out: dict[str, Any] = {}
    for name, block in (brief.get("ci_evidence") or {}).items():
        if not isinstance(block, dict) or block.get("state") == "received":
            out[name] = block
            continue
        short = {k: block[k] for k in ("state", "expectation", "run") if block.get(k)}
        # A block of an old shape (no expectation) keeps its reason: nothing else says why.
        out[name] = short if block.get("expectation") else block
    return out


#: Top-level MANDATORY fields that are not carried as they are.
_REDUCERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "project": lambda b: (b.get("project") or {}).get("slug") or None,
    "ac_test_results": _ac_results_part,
    "locator_resolution": _locators_part,
    "ci_evidence": _ci_evidence_part,
    "base_merge": lambda b: _base_merge_part(b.get("base_merge")),
    "diff_base": lambda b: _diff_base_part(b.get("diff_base")),
    "verdict_route": lambda b: _verdict_route_part(b.get("verdict_route")),
    "statement_freshness": lambda b: _freshness_part(b.get("statement_freshness")),
    "review_circle": lambda b: (
        b.get("review_circle") if (b.get("review_circle") or {}).get("laps") else None
    ),
    "path_notices": lambda b: (
        b.get("path_notices")
        if (b.get("path_notices") or {}).get("state") not in (None, "none")
        else None
    ),
    "diff_command": lambda b: b.get("diff_command") or None,
    "stacking_warning": lambda b: b.get("stacking_warning") or None,
    "state_review": lambda b: b.get("state_review") or None,
    "catalogue_rules": lambda b: b.get("catalogue_rules") or [],
}


def cause_task_line(block: dict[str, Any] | None) -> str:
    """Строка блока «задача-причина» для текста брифа (#1858); "" — блока нет.

    Одна функция на оба текста (компактный и section=full): читатель текста не
    должен гадать, «причины нет» это или «поле не показали».
    """
    if not block:
        return ""
    if block.get("state") != "known":
        return str(block.get("note") or "Задача-причина: нет данных")
    areas = ", ".join(block.get("affected_areas") or []) or "-"
    return (
        f"Задача-причина #{block.get('task_id')}: {block.get('title')} "
        f"({block.get('link')}), {block.get('areas_note')}: {areas}; "
        f"cause_kind={block.get('cause_kind')}"
    )


def mandatory_part(brief: dict[str, Any]) -> dict[str, Any]:
    """Decision-critical fields. Whatever the budget, none of them is cut."""
    part: dict[str, Any] = {"projection": True}
    for name in fields_of(MANDATORY):
        reduce = _REDUCERS.get(name)
        part[name] = reduce(brief) if reduce else brief.get(name)
    part["latest_review"] = _latest_review_part(brief.get("latest_review"))
    part["review_report"] = _review_report_part(brief.get("review_report"))
    part["machine_review"] = _machine_review_part(brief.get("machine_review"))
    return part


# --- blocks that give way to the budget ---------------------------------------


def _count_block(
    items: list[Any], shown: int, section: str, cut_inside: bool = False
) -> dict[str, Any]:
    """Counts of a block; the hint also appears when a SHOWN element was cut."""
    more = shown < len(items) or cut_inside
    return {
        "shown": shown,
        "omitted": len(items) - shown,
        "total": len(items),
        "truncated": cut_inside,
        "hint": f"section={section}" if more else "",
    }


def _ac_block(brief: dict[str, Any], level: int) -> dict[str, Any]:
    acs = brief.get("acceptance_criteria") or []
    items = []
    for ac in acs[:level]:
        row = {"id": ac.get("id"), "then": _cap(ac.get("then"), TITLE_CAP)}
        if len(str(ac.get("then") or "")) > TITLE_CAP:
            row["truncated"] = True
        items.append(row)
    cut = any(r.get("truncated") for r in items)
    block = _count_block(acs, level, "acceptance_criteria", cut)
    block["items"] = items
    return block


def _symbol_row(
    entry: dict[str, Any], outcomes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    addresses = list(dict.fromkeys(entry.get("untouched") or []))
    shown = addresses[:ADDRESSES_SHOWN]
    row: dict[str, Any] = {
        "symbol": entry.get("symbol"),
        "defined_in": entry.get("defined_in"),
        "state": entry.get("state"),
        "total_sites": entry.get("total_sites", 0),
        "untouched_total": len(addresses),
        "shown": len(shown),
        "omitted": len(addresses) - len(shown),
        "untouched": shown,
        "truncated": len(addresses) > len(shown),
    }
    outcome = outcomes.get(str(entry.get("symbol")))
    if outcome:
        row["only_tests_outcome"] = outcome.get("outcome")
        row["only_tests_call_path"] = outcome.get("call_path")
    return row


def _ordered_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def rank(entry: dict[str, Any]) -> int:
        state = entry.get("state")
        return _STATE_ORDER.index(state) if state in _STATE_ORDER else len(_STATE_ORDER)

    return sorted(entries, key=rank)


def _call_sites_block(brief: dict[str, Any], level: int) -> dict[str, Any]:
    section = brief.get("call_sites") or {}
    entries = _ordered_entries(section.get("entries") or [])
    outcomes = {str(o.get("symbol")): o for o in section.get("only_tests") or []}
    by_state: dict[str, int] = {}
    for entry in entries:
        by_state[str(entry.get("state"))] = by_state.get(str(entry.get("state")), 0) + 1
    caveats = {
        key: _cap(section.get(key), CAVEAT_CAP)
        for key in (
            "status",
            "reason",
            "summary",
            "note",
            "only_tests_state",
            "only_tests_summary",
        )
        if section.get(key)
    }
    rows = [_symbol_row(e, outcomes) for e in entries[:level]]
    cut = any(r["truncated"] for r in rows) or any(
        len(str(section.get(k) or "")) > CAVEAT_CAP for k in caveats
    )
    block = _count_block(entries, level, "call_sites", cut)
    block["by_state"] = by_state
    block["caveats"] = caveats
    block["items"] = rows
    return block


def _statement_texts(brief: dict[str, Any]) -> dict[str, str]:
    texts = {}
    for name in _STATEMENT_FIELDS:
        value = brief.get(name)
        text = (
            "\n".join(str(x) for x in value)
            if isinstance(value, list)
            else str(value or "")
        )
        if text:
            texts[name] = text
    return texts


def _text_block(text: str, level: int) -> dict[str, Any]:
    """Counts always; the text and the ``truncated`` marker only when they say something."""
    clipped = text[:level]
    cut = len(clipped) < len(text)
    block: dict[str, Any] = {
        "shown": len(clipped),
        "omitted": len(text) - len(clipped),
        "total": len(text),
        "hint": "section=statement" if cut else "",
    }
    if clipped:
        block["text"] = clipped
    if cut:
        block["truncated"] = True
    return block


def _blocks(brief: dict[str, Any], levels: dict[str, int]) -> dict[str, Any]:
    blocks: dict[str, Any] = {
        "acceptance_criteria": _ac_block(brief, levels["acceptance_criteria"]),
        "call_sites": _call_sites_block(brief, levels["call_sites"]),
    }
    statement = {
        name: _text_block(text, levels[name])
        for name, text in _statement_texts(brief).items()
    }
    if statement:
        blocks["statement"] = statement
    return blocks


def _max_levels(brief: dict[str, Any]) -> dict[str, int]:
    entries = (brief.get("call_sites") or {}).get("entries") or []
    maxes = {
        "acceptance_criteria": min(
            AC_SHOWN, len(brief.get("acceptance_criteria") or [])
        ),
        "call_sites": min(SYMBOLS_SHOWN, len(entries)),
    }
    texts = _statement_texts(brief)
    for name in _STATEMENT_FIELDS:
        maxes[name] = min(STATEMENT_CAP, len(texts.get(name, "")))
    return maxes


# --- text of the projection ----------------------------------------------------


def _verdict_lines(proj: dict[str, Any]) -> list[str]:
    lines = []
    review = proj["latest_review"]
    if review:
        from hub.models import latest_review_freshness

        fresh = latest_review_freshness(
            bool(review["is_current"]), bool(review["closed_by_decision"])
        )
        solo = (
            " [SELF-APPROVED: solo mode, not independent]"
            if review["self_approved"]
            else ""
        )
        if review["closed_by_decision"]:
            fresh += " [closed by the rework decision]"
        lines.append(
            f"Latest verdict: {str(review['verdict'] or '?').upper()} for "
            f"submission #{review['submission_generation']} ({fresh}){solo}; "
            f"{review['findings_count']} finding(s)"
        )
    gen = proj["current_generation_review"]
    if gen:
        lines.append(f"Current-generation review: {gen.get('headline') or gen}")
    if review:
        lines.extend(_digest_lines("Findings of that verdict", review["findings"]))
    report = proj["review_report"]
    if report:
        lines.append(
            f"Review report: {report['state']} (machine review #"
            f"{report['machine_review_id']}), diff {report['diff_files']} files/"
            f"{report['diff_lines']} lines {report['diff_note'] or ''}".rstrip()
        )
    if proj["review_in_flight"]:
        lines.append(f"Review in flight: {proj['review_in_flight'].get('headline')}")
    route = proj["verdict_route"]
    if route and route.get("line"):
        lines.append(f"Verdict route: {route['line']}")
    return lines


_ROW_EXTRAS = ("id", "scope", "linked_task_id", "file", "line", "finding_uid")


def _row_text(row: dict[str, Any]) -> str:
    title = row.get("title") or row.get("message") or ""
    extras = " ".join(
        f"{k}={row[k]}" for k in _ROW_EXTRAS if row.get(k) not in (None, "")
    )
    return f"[{row.get('severity')}] {title}" + (f" ({extras})" if extras else "")


def _digest_lines(label: str, digest: dict[str, Any]) -> list[str]:
    lines = [f"{label}: {digest['total']}"]
    lines.extend(f"  - {_row_text(f)}" for f in digest["items"])
    if digest["omitted"]:
        lines.append(f"  … and {digest['omitted']} more ({digest['hint']})")
    return lines


def _decision_lines(mr: dict[str, Any]) -> list[str]:
    """What was decided about each finding, by uid (the free text stays in full)."""
    lines = [
        f"  disposition {r['finding_uid']}: {r['disposition']} (by {r['decided_by']})"
        for r in mr["dispositions"]
    ]
    lines.extend(
        f"  closed by live check {r['live_check_id']}: {r['finding_uid']} (hand "
        f"{r['hand'] or '-'})"
        for r in mr["observation_closures"]
    )
    lines.extend(
        f"  deferred repeat {r['finding_uid']} -> task #{r['linked_task_id']}"
        for r in mr["deferred_repeats"]
    )
    return lines


def _boundary_lines(proj: dict[str, Any]) -> list[str]:
    """Review boundaries: what the verdict is judged against."""
    lines = []
    if proj["constraints"]:
        lines.append("Constraints: " + "; ".join(proj["constraints"]))
    if proj["out_of_scope_for_review"]:
        lines.append(
            "Out of scope for review: " + "; ".join(proj["out_of_scope_for_review"])
        )
    if proj["review_checklist"]:
        lines.append("Review checklist:")
        lines.extend(f"  - {item}" for item in proj["review_checklist"])
    return lines


def _machine_lines(mr: dict[str, Any] | None) -> list[str]:
    if mr is None:
        return ["Machine review: none"]
    incomplete = {None: "unknown (never stated)", True: "YES", False: "no"}[
        mr["incomplete"]
    ]
    head = (
        f"Machine review #{mr['id']}: outcome={mr['outcome']}, profile="
        f"{mr['profile'] or '-'}, model={mr['model'] or '-'}, current="
        f"{mr['is_current']}, incomplete={incomplete}"
    )
    if mr["incomplete_reason"]:
        head += f" ({mr['incomplete_reason']})"
    lines = [
        head,
        f"  confirmed {mr['findings_confirmed_count']}, rejected "
        f"{mr['findings_rejected_count']}, unresolved {mr['unresolved_count']}, "
        f"lost dimensions {mr['lost_dimensions_count']}, dispositions "
        f"{mr['dispositions_count']}, deferred repeats {mr['deferred_repeats_count']}"
        f", closures {mr['observation_closures_count']}",
    ]
    if mr["ac_tests_unread"]:
        lines.append(
            "  AC TESTS NOT READ by the reviewer: " + "; ".join(mr["ac_tests_unread"])
        )
    if mr["self_reviewed"]:
        lines.append("  SELF-REVIEWED: the author reviewed their own work")
    elif mr["principal_id"] is None:
        lines.append("  independence NOT established (no reviewer principal recorded)")
    if mr["pin_verified"]:
        lines.append("  pin verified: the hub matched the report to its submission")
    else:
        lines.append(
            "  pin NOT verified: the report does not prove which submission it read"
        )
    if mr["second_door_channel"] or mr["second_door_reason"]:
        lines.append(
            f"  channel: {mr['second_door_channel']} ({mr['second_door_reason']})"
        )
    if mr["ci_integrity_verdict"] or mr["ci_integrity_explanation"]:
        lines.append(
            f"  CI integrity answer: {mr['ci_integrity_verdict']} "
            f"{mr['ci_integrity_explanation']}".rstrip()
        )
    lines.extend(_decision_lines(mr))
    if mr["findings_confirmed"]["total"]:
        lines.extend(_digest_lines("Confirmed findings", mr["findings_confirmed"]))
    if mr["unresolved"]["total"]:
        lines.extend(_digest_lines("Unresolved findings", mr["unresolved"]))
    return lines


def _checks_lines(proj: dict[str, Any]) -> list[str]:
    """Outcomes of the mechanical checks - failures and unknowns are named."""
    lines = []
    pre = proj["prepass"] or {}
    mark = " FAILED" if pre.get("state") == "failed" or pre.get("failed") else ""
    lines.append(
        f"Pre-pass [{pre.get('state', '?')}]{mark}: {pre.get('reason', '')}"
        + (f" | failed: {', '.join(pre['failed'])}" if pre.get("failed") else "")
        + (f" | skipped: {', '.join(pre['skipped'])}" if pre.get("skipped") else "")
    )
    val = proj["validation"] or {}
    lines.append(
        f"Validation [{val.get('state', '?')}] verified={val.get('verified')}"
        f"{' NEEDS HUMAN COMPARE' if val.get('needs_human_compare') else ''}: "
        f"{val.get('headline', '')}"
    )
    acr = proj["ac_test_results"]
    bad = ", ".join(f"{r['ac_id']}={r['status']}" for r in acr["negative"])
    lines.append(
        f"AC test results: {acr['current']} current, {acr['stale']} stale"
        + (f" | NOT PASSING: {bad}" if bad else "")
    )
    loc = proj["locator_resolution"]
    if loc["unresolved"]:
        lines.append(
            "AC locators not resolved: "
            + ", ".join(f"{r['ac_id']}={r['status']}" for r in loc["unresolved"])
        )
    if loc.get("not_applicable"):
        na = loc["not_applicable"]
        more = f" +{na['more']} more" if na["more"] else ""
        lines.append(
            f"AC locators not applicable ({evidence_states.MANUAL_AC_NOTE}): "
            f"{', '.join(na['acs'])}{more}"
        )
    for name in ("mutations", "baseline"):
        block = (proj["ci_evidence"] or {}).get(name) or {}
        if block.get("expectation") and block.get("state") != "received":
            # One short format, no repeat of the reason (#2026): the full text is in section=full.
            lines.append(
                f"CI {name} [{block.get('state')}] expectation={block['expectation']}"
                + (f" | {block['run']}" if block.get("run") else "")
            )
            continue
        lines.append(
            f"CI {name} [{block.get('state', '?')}]: {block.get('reason', '')}"
        )
    live = proj["live_check"] or {}
    lines.append(
        f"Live check [{live.get('state', '?')}]"
        f"{' SHA MISMATCH' if live.get('sha_mismatch') else ''}: {live.get('reason', '')}"
    )
    fresh = proj["statement_freshness"]
    if fresh:
        lines.append(f"Statement freshness [{fresh['state']}]: {fresh['reason']}")
    return lines


def _evidence_lines(proj: dict[str, Any]) -> list[str]:
    lines = []
    coverage = proj["evidence_coverage"] or {}
    lines.append(
        f"Evidence coverage [{coverage.get('state', '?')}]: "
        f"{coverage.get('headline', '')}"
    )
    lines.extend(
        f"  - {m.get('check', '?')}: {m.get('reason', '')}"
        for m in coverage.get("checks_missing") or []
    )
    ci = proj["ci_run_report"] or {}
    lines.append(
        f"CI report [{ci.get('state', '?')}]: {ci.get('reason', '')} | trust="
        f"{ci.get('trust') or '-'} {ci.get('trust_reason') or ''}".rstrip()
    )
    if ci.get("provenance") or ci.get("provenance_note"):
        lines.append(
            f"  provenance: {json.dumps(ci.get('provenance'), ensure_ascii=False)}"
            f" {ci.get('provenance_note') or ''}".rstrip()
        )
    return lines + _integrity_lines(proj["ci_integrity"])


def _integrity_lines(block: dict[str, Any] | None) -> list[str]:
    if not isinstance(block, dict):
        return []
    lines = [f"CI integrity: {block.get('gap') or block.get('state')}"]
    lines.extend(
        f"  {s.get('path')}:{s.get('line')} [{s.get('rule')}]"
        for s in block.get("signs") or []
    )
    lines.append(f"  Question: {block.get('question', '')}")
    if block.get("answer"):
        lines.append(f"  Answer: {block['answer']}")
    if block.get("note"):
        lines.append(f"  Note: {block['note']}")
    return lines


def _diff_lines(proj: dict[str, Any]) -> list[str]:
    base = proj["diff_base"] or {}
    unverified = base.get("state") != "resolved"
    if proj["diff_command"]:
        lines = [f"Diff: {proj['diff_command']}"]
        if unverified:
            lines.append(
                f"  base NOT verified [{base.get('state')}]: {base.get('reason') or ''}"
            )
        return lines
    if unverified and (base.get("reason") or base.get("state")):
        return [
            f"Diff: NOT AVAILABLE [{base.get('state')}] — {base.get('reason') or ''}"
        ]
    return []


def _state_review_lines(state: dict[str, Any]) -> list[str]:
    """What a state task accepts: the whole block, as the full text prints it."""
    generation = state.get("generation", 0)
    lines = [
        f"{state.get('label', 'результат: состояние')} — поколение {generation}",
        str(state.get("headline") or ""),
        f"Rollback: {state.get('rollback') or 'не назван'}",
        "Evidence of the current generation (the author's word, not run by the hub):",
    ]
    for ev in state.get("evidence") or []:
        lines.append(
            f"  {ev.get('ac_id', '?')} | действие: {ev.get('action', '')} | "
            f"наблюдение: {ev.get('observed', '')} | над чем: {ev.get('target', '')}"
            f" | когда: {ev.get('observed_at', '')} | автор: {ev.get('author', '')}"
        )
    lines.extend(
        f"  {a} | записи этого поколения нет" for a in state.get("missing_ac") or []
    )
    na = state.get("not_applicable") or []
    if na:
        lines.append(
            "Проверки кода — не применимо: "
            + "; ".join(f"{c.get('check', '?')} ({c.get('reason', '')})" for c in na)
        )
    lines.append(
        f"Verdict: only a human, with expected_generation={state.get('expected_generation', generation)}."
    )
    return [line for line in lines if line]


def _rules_lines(rules: list[dict[str, Any]]) -> list[str]:
    """Catalogue rules of the area (#920); nothing when there are none."""
    if not rules:
        return []
    lines = [
        "Rules from the catalogue (category_checks) for this area — classes "
        "already paid for; check they did not come back:"
    ]
    for rule in rules:
        defects = rule.get("source_defects") or []
        source = (
            ", ".join(f"#{d['task_id']} {d.get('title', '')}".rstrip() for d in defects)
            if defects
            else "no defect recorded (rule from the recurrence debt, #878)"
        )
        lines.append(
            f"  - {rule['category']} → check: {rule['check_ref']} "
            f"| defect: {source} | area: {', '.join(rule.get('matched_areas') or [])}"
        )
    return lines


def _verdict_command_line(proj: dict[str, Any]) -> str:
    """The generation a commit-task verdict must name (#1706), and the revision."""
    generation = proj["submission_generation"]
    return (
        f"Verdict: pass expected_generation={generation} (CLI: --expected-generation "
        f"{generation}); a verdict that stands is never replaced silently, only by "
        f"a human with supersedes_revision={proj['review_verdict_revision']}."
    )


def _head_lines(proj: dict[str, Any]) -> list[str]:
    lines = [
        f"Review brief for task #{proj['task_id']}: {proj['title']} "
        "[projection; whole brief: section=full]",
        f"Status: {proj['status']} | submission #{proj['submission_generation']} "
        f"| review cycle {proj['review_cycle']}",
    ]
    if not proj["state_review"]:  # a state task has no branch, diff or CI
        lines.append(
            f"Branch: {proj['branch'] or '-'} | submitted "
            f"{proj['submission_sha'] or '-'} | tip "
            f"{proj['current_branch_tip'] or '-'} | sha_check="
            f"{proj['sha_check']} ({proj['sha_check_reason'] or '-'})"
        )
    if proj["project"]:
        lines.insert(1, f"Project: {proj['project']}")
    merge = proj["base_merge"]
    if merge and merge["state"] in ("conflicting", "unknown"):
        lines.append(f"Base merge [{merge['state']}]: {merge['reason']}")
    if proj["state_review"]:
        lines.extend(_state_review_lines(proj["state_review"]))
    else:
        lines.append(_verdict_command_line(proj))
        lines.extend(_diff_lines(proj))
    lines.extend(_rules_lines(proj["catalogue_rules"]))
    lines.extend(_boundary_lines(proj))
    if proj["review_circle"]:
        lines.append(
            f"Review circle: {proj['review_circle'].get('laps')} laps — "
            + "; ".join(proj["review_circle"].get("breakdown") or [])
        )
    cause_line = cause_task_line(proj.get("cause_task"))
    if cause_line:
        lines.append(cause_line)
    if proj["stacking_warning"]:
        lines.append(str(proj["stacking_warning"]))
    if proj["path_notices"] and proj["path_notices"].get("text"):
        lines.append(str(proj["path_notices"]["text"]))
    return lines


def _block_head(name: str, block: dict[str, Any]) -> str:
    line = f"{name}: {block['shown']}/{block['total']} shown"
    if block["omitted"]:
        line += f", {block['omitted']} omitted"
    if block.get("truncated"):
        line += ", shown elements cut"
    if block["hint"]:
        line += f" ({block['hint']})"
    return line


def _block_lines(blocks: dict[str, Any]) -> list[str]:
    lines = ["", _block_head("Acceptance criteria", blocks["acceptance_criteria"])]
    lines.extend(
        f"  {ac['id']}: {ac['then']}" for ac in blocks["acceptance_criteria"]["items"]
    )
    calls = blocks["call_sites"]
    lines.append(_block_head("Call sites (symbols)", calls))
    lines.append(f"  by state: {json.dumps(calls['by_state'], ensure_ascii=False)}")
    lines.extend(f"  {k}: {v}" for k, v in calls["caveats"].items())
    for row in calls["items"]:
        lines.append(
            f"  {row['symbol']} [{row['state']}] sites {row['total_sites']}, "
            f"untouched {row['untouched_total']} (shown {row['shown']}, omitted "
            f"{row['omitted']})"
        )
        lines.extend(f"    - {address}" for address in row["untouched"])
    for name, block in (blocks.get("statement") or {}).items():
        lines.append(_block_head(f"Statement/{name}", block))
        if block.get("text"):
            lines.append(block["text"])
    return lines


def _projection_summary(proj: dict[str, Any], exceeded: bool) -> str:
    warning = proj["self_review_warning"]
    lines: list[str] = []
    if warning:
        lines.append(
            f"WARNING [{warning.get('reason', 'self_review_forbidden')}]: "
            f"{warning.get('message', '')}\n{warning.get('hint', '')}\n"
        )
    lines.extend(_head_lines(proj))
    lines.extend(_verdict_lines(proj))
    lines.extend(_machine_lines(proj["machine_review"]))
    if proj["profile_downgrade"]:
        lines.append(f"PROFILE DOWNGRADED: {proj['profile_downgrade'].get('headline')}")
    lines.extend(_checks_lines(proj))
    lines.extend(_evidence_lines(proj))
    lines.extend(_block_lines(proj["blocks"]))
    if exceeded:
        lines.append(
            f"\n[{EXCEEDED_NOTE}: {MAX_CHARS}] the mandatory part alone is bigger"
        )
    lines.append(
        "\nAgent: hub_submit_review takes changes_requested only (one scope="
        "in_scope finding); a positive outcome is a review report. APPROVED: "
        "human or hub policy."
    )
    return "\n".join(lines)


def projection_payload(
    brief: dict[str, Any], levels: dict[str, int], exceeded: bool = False
) -> tuple[str, dict[str, Any]]:
    """The text and the structured projection of the brief at the given levels."""
    proj = {**mandatory_part(brief), "blocks": _blocks(brief, levels)}
    if exceeded:
        proj["bounds"] = {"limit": MAX_CHARS, "note": EXCEEDED_NOTE}
    return _projection_summary(proj, exceeded), proj


def _projection_result(
    brief: dict[str, Any], levels: dict[str, int], exceeded: bool = False
) -> CallToolResult:
    summary, proj = projection_payload(brief, levels, exceeded)
    return structured_echo_result(summary, brief=proj)


def fit_levels(brief: dict[str, Any]) -> tuple[dict[str, int], bool]:
    """Largest levels, in priority order, whose FINAL result fits the budget."""
    levels = {name: 0 for name in _BLOCK_ORDER}
    if _size(_projection_result(brief, levels)) > MAX_CHARS:
        return levels, True
    maxes = _max_levels(brief)
    for name in _BLOCK_ORDER:
        low, high = 0, maxes[name]
        while low < high:
            mid = (low + high + 1) // 2
            fits = _size(_projection_result(brief, {**levels, name: mid}))
            low, high = (mid, high) if fits <= MAX_CHARS else (low, mid - 1)
        levels[name] = low
    return levels, False


def project_brief(brief: dict[str, Any]) -> CallToolResult:
    levels, exceeded = fit_levels(brief)
    return _projection_result(brief, levels, exceeded)


# --- sections ------------------------------------------------------------------


@dataclass
class _Section:
    unit: str
    items: list[Any] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    text: str | None = None

    @property
    def total(self) -> int:
        return len(self.text) if self.text is not None else len(self.items)

    def body(self) -> dict[str, Any]:
        if self.text is not None:
            return {"text": self.text}
        return {"meta": self.meta, "items": self.items}


def _statement_section(brief: dict[str, Any]) -> _Section:
    parts = []
    for name in fields_of(TEXT):
        value = brief.get(name)
        if not value:
            continue
        body = value if isinstance(value, str) else "\n".join(f"- {v}" for v in value)
        parts.append(f"## {name}\n{body}\n")
    return _Section("chars", text="\n".join(parts))


META_CAP = 500


def _move_bulky(data: dict[str, Any], items: list[Any], tag: str) -> dict[str, Any]:
    """Keep small scalars as page metadata; everything else becomes elements.

    A list contributes one element per entry, a bulky scalar one element. So no
    page is made large by its metadata, only by a single element of its own.
    """
    meta: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, list) and value:
            items.extend({tag: key, "value": v} for v in value)
        elif len(json.dumps(value, ensure_ascii=False, default=str)) > META_CAP:
            items.append({tag: key, "value": value})
        else:
            meta[key] = value
    return meta


def _machine_section(brief: dict[str, Any]) -> _Section:
    section = _Section("findings")
    section.meta = _move_bulky(
        dict(brief.get("machine_review") or {}), section.items, "kind"
    )
    return section


def _verdict_findings_section(brief: dict[str, Any]) -> _Section:
    """The findings of the latest verdict, whole; the verdict's scalars as meta."""
    review = dict(brief.get("latest_review") or {})
    items = list(review.pop("findings", None) or [])
    return _Section("findings", items=items, meta=review)


def _call_sites_section(brief: dict[str, Any]) -> _Section:
    """Symbols first; the other lists and bulky texts follow as tagged elements."""
    data = dict(brief.get("call_sites") or {})
    items = list(data.pop("entries", None) or [])
    meta = _move_bulky(data, items, LIST_TAG)
    return _Section("symbols", items=items, meta=meta)


def _load_section(brief: dict[str, Any], name: str) -> _Section:
    if name == "acceptance_criteria":
        return _Section("acceptance_criteria", items=list(brief.get(name) or []))
    if name == "statement":
        return _statement_section(brief)
    if name == "machine_review":
        return _machine_section(brief)
    if name == "verdict_findings":
        return _verdict_findings_section(brief)
    return _call_sites_section(brief)


def fingerprint(brief: dict[str, Any], name: str, section: _Section) -> str:
    """sha256 of the whole section together with the identity of the submission."""
    blob = json.dumps(
        {"section": name, "identity": _identity(brief), "body": section.body()},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _error(reason: str, message: str, **extra: Any) -> CallToolResult:
    return structured_error_result({"reason": reason, "message": message, **extra})


def _page(
    brief: dict[str, Any],
    name: str,
    section: _Section,
    fp: str,
    offset: int,
    count: int,
) -> tuple[str, dict[str, Any]]:
    end = offset + count
    page: dict[str, Any] = {
        "section": name,
        "unit": section.unit,
        "offset": offset,
        "total": section.total,
        "fingerprint": fp,
        **_identity(brief),
    }
    if brief.get("self_review_warning"):
        page["self_review_warning"] = brief["self_review_warning"]
    if section.text is not None:
        content = section.text[offset:end]
        page["text"] = content
    else:
        if offset == 0:
            page["meta"] = section.meta
        page["items"] = section.items[offset:end]
        content = json.dumps(page["items"], ensure_ascii=False)
    page["shown"] = count
    page["next_offset"] = end if end < section.total else None
    head = (
        f"Review brief #{brief.get('task_id')}, section {name} ({section.unit} "
        f"{offset}..{end} of {section.total}), next_offset={page['next_offset']}, "
        f"fingerprint={fp}"
    )
    return f"{head}\n{content}", page


def _page_result(
    brief: dict[str, Any],
    name: str,
    section: _Section,
    fp: str,
    offset: int,
    count: int,
) -> CallToolResult:
    summary, page = _page(brief, name, section, fp, offset, count)
    return structured_echo_result(summary, page=page)


def _element_alone_over(section: _Section, offset: int) -> bool:
    """Does the first element of the page exceed the ceiling by itself?"""
    if section.text is not None:
        return False
    first = section.items[offset : offset + 1]
    return len(json.dumps(first, ensure_ascii=False)) * 2 > MAX_CHARS


def _fit_page(
    brief: dict[str, Any], name: str, section: _Section, fp: str, offset: int
) -> CallToolResult:
    remaining = section.total - offset
    low, high = min(1, remaining), remaining
    while low < high:
        mid = (low + high + 1) // 2
        size = _size(_page_result(brief, name, section, fp, offset, mid))
        low, high = (mid, high) if size <= MAX_CHARS else (low, mid - 1)
    result = _page_result(brief, name, section, fp, offset, low)
    if _size(result) <= MAX_CHARS:
        return result
    # The exception is for ONE element that alone exceeds the ceiling; a page
    # over it for any other reason (large scalar metadata) is named as such.
    summary, page = _page(brief, name, section, fp, offset, low)
    reason = "element" if _element_alone_over(section, offset) else "metadata"
    page["bounds"] = {"limit": MAX_CHARS, "note": EXCEEDED_NOTE, "reason": reason}
    return structured_echo_result(f"[{EXCEEDED_NOTE}] {summary}", page=page)


def section_result(
    brief: dict[str, Any], name: str, offset: int, expect: str
) -> CallToolResult:
    section = _load_section(brief, name)
    fp = fingerprint(brief, name, section)
    if expect and expect != fp:
        return _error(
            "section_changed",
            _CHANGED,
            section=name,
            fingerprint=fp,
            expected=expect,
            **_identity(brief),
        )
    if offset < 0 or offset > section.total or (offset == section.total > 0):
        return _error(
            "bad_offset",
            f"offset {offset} is outside the section ({section.total} "
            f"{section.unit}); the last page has next_offset=null",
            section=name,
            total=section.total,
            fingerprint=fp,
        )
    return _fit_page(brief, name, section, fp, offset)


def brief_result(
    brief: dict[str, Any], section: str = "", offset: int = 0, expect: str = ""
) -> CallToolResult:
    """The MCP answer of hub_get_review_brief for everything but section=full."""
    if not section:
        return project_brief(brief)
    if section == "full":
        raise ValueError("section=full is the unprojected answer; the caller builds it")
    if section not in SECTIONS:
        return _error(
            "unknown_section",
            f"unknown section {section!r}",
            allowed=list(SECTIONS),
        )
    return section_result(brief, section, offset, expect)
