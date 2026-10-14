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
from dataclasses import dataclass, field
from typing import Any

from mcp.types import CallToolResult

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

SECTIONS = ("call_sites", "acceptance_criteria", "statement", "machine_review", "full")
_CHANGED = "раздел изменился (ветка сдвинулась или пересдача) — начните с offset=0"
# Heuristic states first: a reader with a short budget meets the sites that
# nobody touched before the ones that are fine.
_STATE_ORDER = ("untouched_sites", "only_tests", "no_callers")
_STATEMENT_FIELDS = (
    "description",
    "technical_hints",
    "scope_growth",
    "latest_submission_summary",
)
_BLOCK_ORDER = ("acceptance_criteria", "call_sites", *_STATEMENT_FIELDS)


def result_chars(result: CallToolResult) -> int:
    """Characters of the result as the SDK puts it on the wire."""
    return len(result.model_dump_json(by_alias=True, exclude_none=True))


def log_issue(
    result: CallToolResult, task_id: int, mode: str, section: str, offset: int
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


def _finding_digest(items: list[dict[str, Any]]) -> dict[str, Any]:
    shown = [
        {
            "severity": f.get("severity"),
            "title": _cap(f.get("title"), TITLE_CAP),
            **({"uid": f["finding_uid"]} if f.get("finding_uid") else {}),
        }
        for f in items[:FINDINGS_SHOWN]
    ]
    return {
        "total": len(items),
        "shown": len(shown),
        "omitted": len(items) - len(shown),
        "items": shown,
    }


def _machine_review_part(mr: dict[str, Any] | None) -> dict[str, Any] | None:
    """None stays None: no report is not a clean report."""
    if not isinstance(mr, dict):
        return None
    confirmed = mr.get("findings_confirmed") or []
    unresolved = mr.get("unresolved") or []
    return {
        "id": mr.get("id"),
        "is_current": mr.get("is_current"),
        "outcome": mr.get("outcome"),
        "profile": mr.get("profile"),
        # None = "never stated", passed as it is, never turned into complete.
        "incomplete": mr.get("incomplete"),
        "incomplete_reason": mr.get("incomplete_reason"),
        "findings_confirmed_count": len(confirmed),
        "findings_rejected_count": len(mr.get("findings_rejected") or []),
        "unresolved_count": len(unresolved),
        "lost_dimensions_count": len(mr.get("lost_dimensions") or []),
        "findings_confirmed": _finding_digest(confirmed),
        "unresolved": _finding_digest(unresolved),
    }


def _latest_review_part(review: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(review, dict):
        return None
    return {
        "verdict": review.get("verdict"),
        "is_current": review.get("is_current"),
        "closed_by_decision": review.get("closed_by_decision"),
        "self_approved": review.get("self_approved"),
        "submission_generation": review.get("submission_generation"),
        "findings_count": len(review.get("findings") or []),
    }


def _state_review_part(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    return {
        key: state.get(key)
        for key in (
            "label",
            "generation",
            "headline",
            "missing_ac",
            "expected_generation",
        )
    }


def _base_merge_part(merge: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(merge, dict):
        return None
    return {
        "state": merge.get("state"),
        "reason": merge.get("reason"),
        "files_count": len(merge.get("files") or []),
    }


def _cautions(brief: dict[str, Any]) -> dict[str, Any]:
    """Warnings the full text put near the top; short, so they are not cut either."""
    notices = brief.get("path_notices")
    circle = brief.get("review_circle") or {}
    return {
        # #1589: manual server steps for the paths this generation touched.
        "path_notices": (
            notices
            if isinstance(notices, dict) and notices.get("state") != "none"
            else None
        ),
        "review_circle": circle if circle.get("laps") else None,
        "stacking_warning": brief.get("stacking_warning") or None,
    }


def mandatory_part(brief: dict[str, Any]) -> dict[str, Any]:
    """Decision-critical fields. Whatever the budget, none of them is cut."""
    project = brief.get("project")
    report = brief.get("review_report")
    return {
        "projection": True,
        "task_id": brief.get("task_id"),
        "title": brief.get("title"),
        "status": brief.get("status"),
        "project": project.get("slug") if isinstance(project, dict) else None,
        "review_cycle": brief.get("review_cycle", 0),
        "result_kind": brief.get("result_kind"),
        **_identity(brief),
        "branch": brief.get("branch"),
        "sha_check": brief.get("sha_check"),
        "sha_check_reason": brief.get("sha_check_reason"),
        "self_review_warning": brief.get("self_review_warning"),
        "latest_review": _latest_review_part(brief.get("latest_review")),
        "current_generation_review": brief.get("current_generation_review"),
        "review_report": (
            {"state": report.get("state")} if isinstance(report, dict) else None
        ),
        "machine_review": _machine_review_part(brief.get("machine_review")),
        "evidence_coverage": brief.get("evidence_coverage"),
        # #1687: how far the CI report is trusted, with the reason.
        "ci_run_report": brief.get("ci_run_report"),
        # #1688: signs, the question, the answer and the owner's note, whole.
        "ci_integrity": brief.get("ci_integrity"),
        "base_merge": _base_merge_part(brief.get("base_merge")),
        "state_review": _state_review_part(brief.get("state_review")),
        **_cautions(brief),
    }


# --- blocks that give way to the budget ---------------------------------------


def _count_block(items: list[Any], shown: int, section: str) -> dict[str, Any]:
    return {
        "shown": shown,
        "omitted": len(items) - shown,
        "total": len(items),
        "hint": f"section={section}" if shown < len(items) else "",
    }


def _ac_block(brief: dict[str, Any], level: int) -> dict[str, Any]:
    acs = brief.get("acceptance_criteria") or []
    block = _count_block(acs, level, "acceptance_criteria")
    block["items"] = [
        {"id": ac.get("id"), "then": _cap(ac.get("then"), TITLE_CAP)}
        for ac in acs[:level]
    ]
    return block


def _symbol_row(
    entry: dict[str, Any], outcomes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    addresses = list(dict.fromkeys(entry.get("untouched") or []))
    shown = addresses[:ADDRESSES_SHOWN]
    row = {
        "symbol": entry.get("symbol"),
        "defined_in": entry.get("defined_in"),
        "state": entry.get("state"),
        "total_sites": entry.get("total_sites", 0),
        "untouched_total": len(addresses),
        "shown": len(shown),
        "omitted": len(addresses) - len(shown),
        "untouched": shown,
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
    block = _count_block(entries, level, "call_sites")
    block["by_state"] = by_state
    block["caveats"] = {
        key: _cap(section.get(key), CAVEAT_CAP)
        for key in ("status", "reason", "summary", "note", "only_tests_state")
        if section.get(key)
    }
    if section.get("only_tests_summary"):
        block["caveats"]["only_tests_summary"] = _cap(
            section["only_tests_summary"], CAVEAT_CAP
        )
    block["items"] = [_symbol_row(e, outcomes) for e in entries[:level]]
    return block


def _statement_texts(brief: dict[str, Any]) -> dict[str, str]:
    texts = {
        "description": str(brief.get("description") or ""),
        "technical_hints": str(brief.get("technical_hints") or ""),
        "scope_growth": "\n".join(str(x) for x in brief.get("scope_growth") or []),
        "latest_submission_summary": str(brief.get("latest_submission_summary") or ""),
    }
    return {name: text for name, text in texts.items() if text}


def _text_block(text: str, level: int) -> dict[str, Any]:
    clipped = text[:level]
    return {
        "text": clipped,
        "shown": len(clipped),
        "omitted": len(text) - len(clipped),
        "total": len(text),
        "hint": "section=statement" if level < len(text) else "",
    }


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
        lines.append(
            f"Latest verdict: {str(review['verdict'] or '?').upper()} for "
            f"submission #{review['submission_generation']} ({fresh}); "
            f"{review['findings_count']} finding(s)"
        )
    gen = proj["current_generation_review"]
    if gen:
        lines.append(f"Current-generation review: {gen.get('headline') or gen}")
    report = proj["review_report"]
    if report:
        lines.append(f"Review report: {report['state']}")
    return lines


def _digest_lines(label: str, digest: dict[str, Any]) -> list[str]:
    lines = [f"{label}: {digest['total']}"]
    lines.extend(f"  - [{f['severity']}] {f['title']}" for f in digest["items"])
    if digest["omitted"]:
        lines.append(f"  … and {digest['omitted']} more (section=machine_review)")
    return lines


def _machine_lines(mr: dict[str, Any] | None) -> list[str]:
    if mr is None:
        return ["Machine review: none"]
    incomplete = {None: "unknown (never stated)", True: "YES", False: "no"}[
        mr["incomplete"]
    ]
    head = (
        f"Machine review #{mr['id']}: outcome={mr['outcome']}, profile="
        f"{mr['profile'] or '-'}, current={mr['is_current']}, incomplete="
        f"{incomplete}"
    )
    if mr["incomplete_reason"]:
        head += f" ({mr['incomplete_reason']})"
    lines = [
        head,
        f"  confirmed {mr['findings_confirmed_count']}, rejected "
        f"{mr['findings_rejected_count']}, unresolved {mr['unresolved_count']}, "
        f"lost dimensions {mr['lost_dimensions_count']}",
    ]
    if mr["findings_confirmed"]["total"]:
        lines.extend(_digest_lines("Confirmed findings", mr["findings_confirmed"]))
    if mr["unresolved"]["total"]:
        lines.extend(_digest_lines("Unresolved findings", mr["unresolved"]))
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


def _head_lines(proj: dict[str, Any]) -> list[str]:
    lines = [
        f"Review brief for task #{proj['task_id']}: {proj['title']} "
        "[projection; whole brief: section=full]",
        f"Status: {proj['status']} | submission #{proj['submission_generation']} "
        f"| review cycle {proj['review_cycle']}",
        f"Branch: {proj['branch'] or '-'} | submitted {proj['submission_sha'] or '-'}"
        f" | tip {proj['current_branch_tip'] or '-'} | sha_check="
        f"{proj['sha_check']} ({proj['sha_check_reason'] or '-'})",
    ]
    if proj["project"]:
        lines.insert(1, f"Project: {proj['project']}")
    merge = proj["base_merge"]
    if merge and merge["state"] in ("conflicting", "unknown"):
        lines.append(f"Base merge [{merge['state']}]: {merge['reason']}")
    if proj["state_review"]:
        lines.append(f"State task: {proj['state_review']['headline']}")
    if proj["review_circle"]:
        lines.append(
            f"Review circle: {proj['review_circle'].get('laps')} laps — "
            + "; ".join(proj["review_circle"].get("breakdown") or [])
        )
    if proj["stacking_warning"]:
        lines.append(str(proj["stacking_warning"]))
    if proj["path_notices"] and proj["path_notices"].get("text"):
        lines.append(str(proj["path_notices"]["text"]))
    return lines


def _block_head(name: str, block: dict[str, Any]) -> str:
    line = f"{name}: {block['shown']}/{block['total']} shown"
    if block["omitted"]:
        line += f", {block['omitted']} omitted ({block['hint']})"
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
    lines.extend(_evidence_lines(proj))
    lines.extend(_block_lines(proj["blocks"]))
    if exceeded:
        lines.append(
            f"\n[{EXCEEDED_NOTE}: {MAX_CHARS}] the mandatory part alone is bigger"
        )
    lines.append(
        "\nSubmit the verdict with hub_submit_review (verdict=approved|"
        "changes_requested, findings for changes_requested; changes_requested "
        "needs at least one scope=in_scope finding)."
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
    if result_chars(_projection_result(brief, levels)) > MAX_CHARS:
        return levels, True
    maxes = _max_levels(brief)
    for name in _BLOCK_ORDER:
        low, high = 0, maxes[name]
        while low < high:
            mid = (low + high + 1) // 2
            fits = result_chars(_projection_result(brief, {**levels, name: mid}))
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
    for name in (
        "description",
        "scope_in",
        "scope_out",
        "out_of_scope_for_review",
        "review_checklist",
        "validation_commands",
        "constraints",
        "technical_hints",
        "scope_growth",
        "latest_submission_summary",
    ):
        value = brief.get(name)
        if not value:
            continue
        body = value if isinstance(value, str) else "\n".join(f"- {v}" for v in value)
        parts.append(f"## {name}\n{body}\n")
    return _Section("chars", text="\n".join(parts))


def _machine_section(brief: dict[str, Any]) -> _Section:
    report = brief.get("machine_review") or {}
    section = _Section("findings")
    for key, value in report.items():
        if isinstance(value, list):
            section.items.extend({"kind": key, "value": item} for item in value)
        else:
            section.meta[key] = value
    return section


def _call_sites_section(brief: dict[str, Any]) -> _Section:
    data = dict(brief.get("call_sites") or {})
    entries = data.pop("entries", None) or []
    return _Section("symbols", items=list(entries), meta=data)


def _load_section(brief: dict[str, Any], name: str) -> _Section:
    if name == "acceptance_criteria":
        return _Section("acceptance_criteria", items=list(brief.get(name) or []))
    if name == "statement":
        return _statement_section(brief)
    if name == "machine_review":
        return _machine_section(brief)
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


def _fit_page(
    brief: dict[str, Any], name: str, section: _Section, fp: str, offset: int
) -> CallToolResult:
    remaining = section.total - offset
    low, high = min(1, remaining), remaining
    while low < high:
        mid = (low + high + 1) // 2
        size = result_chars(_page_result(brief, name, section, fp, offset, mid))
        low, high = (mid, high) if size <= MAX_CHARS else (low, mid - 1)
    result = _page_result(brief, name, section, fp, offset, low)
    if result_chars(result) <= MAX_CHARS:
        return result
    # One element bigger than the ceiling goes out alone, and says so.
    summary, page = _page(brief, name, section, fp, offset, low)
    page["bounds"] = {"limit": MAX_CHARS, "note": EXCEEDED_NOTE}
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
