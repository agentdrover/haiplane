"""Exact words for what the review packet lacks or was told not to expect (#2026).

The brief used to write one line for every absent check result ("не получено ...
это не результат") and one for the author's word. A judge or an advisor read that
as a hole in the evidence even where the step was never meant to run, and could
not tell it from a real hole. Each absent mutations/baseline block now carries
one of four states next to the fact, with the event kind and the run:

* ``not_expected``          the event is one the step skips (push, synchronize);
* ``missing_unexpectedly``  the event is one the step runs on (workflow_dispatch,
                            PR opened/reopened) and no result came;
* ``other_attempt``         another verified attempt of the same commit counted
                            it (#1936, ``ci_evidence_other``);
* ``origin_unconfirmed``    the hub cannot say: no report, an unconfirmed report,
                            a block in an unrecognised shape, or a pull_request
                            run whose action is not recorded (GitHub's run event
                            carries no action; ``ci_trust`` requires them equal).

Only the brief says this. ``CIEvidenceBlock.state`` stays ``received`` /
``not_received`` and the gates keep reading the current attempt's projection.
The wording never turns an absence into "выполнено" or "провал".
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

RECEIVED = "received"
NOT_EXPECTED = "not_expected"
MISSING_UNEXPECTEDLY = "missing_unexpectedly"
OTHER_ATTEMPT = "other_attempt"
ORIGIN_UNCONFIRMED = "origin_unconfirmed"

# ci.yml, step ``evidence`` of job treedup: the events that run the steps.
_EXPECTED = frozenset(
    {"workflow_dispatch", "pull_request.opened", "pull_request.reopened"}
)
_NOT_EXPECTED = frozenset({"push", "pull_request.synchronize"})

_NOT_A_RESULT = "это не результат и не отметка «выполнено»"


def _provenance(stored: Mapping[str, Any] | None) -> dict[str, Any]:
    try:
        data = json.loads((stored or {}).get("provenance") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


#: The workflow whose ``evidence`` step the rules above describe.
HUB_WORKFLOW = ".github/workflows/ci.yml"
#: The project whose repository is the hub's own (the one that workflow belongs to).
HUB_PROJECT = "default"


class Origin(NamedTuple):
    """Where an absent block's attempt came from: its event and run, and whether
    the event rules above apply to that project's CI at all."""

    event: str = ""
    run_id: str = ""
    rules_known: bool = True


def origin_of(stored: Mapping[str, Any] | None, project_slug: str) -> Origin:
    """The origin read from the stored report of the current attempt.

    The rules describe the hub's own ``ci.yml``; a satellite's workflow (its own
    path, or no evidence step at all) is not covered, and nothing is claimed.
    """
    prov = _provenance(stored)
    # The documented ``@<ref>`` suffix of a run's path is ignored, as in ci_contract.
    path = str(prov.get("workflow_path") or "").split("@", 1)[0]
    known = project_slug == HUB_PROJECT and path == HUB_WORKFLOW
    return Origin(str(prov.get("event") or ""), str(prov.get("run_id") or ""), known)


def expectation_of(origin: Origin) -> str:
    if not origin.rules_known:
        return ORIGIN_UNCONFIRMED
    if origin.event in _EXPECTED:
        return MISSING_UNEXPECTEDLY
    if origin.event in _NOT_EXPECTED:
        return NOT_EXPECTED
    return ORIGIN_UNCONFIRMED


def _where(event: str, run_id: str) -> str:
    return f"прогон {run_id or '?'} ({event or 'событие неизвестно'})"


def missing_block(label: str, pinned: str, origin: Origin = Origin()):
    """The block for a ``label`` result the current attempt carries none of."""
    from hub.models import CIEvidenceBlock

    event, run_id = origin.event, origin.run_id
    kind = expectation_of(origin)
    head = f"не получено: {label} для коммита {pinned[:12]}"
    where = _where(event, run_id)
    if kind == MISSING_UNEXPECTEDLY:
        why = (
            f"{where}: шаг на этом событии считает {label}, результата нет — "
            f"отсутствует вопреки ожиданию; {_NOT_A_RESULT}"
        )
    elif kind == NOT_EXPECTED:
        why = (
            f"{where}: шаг на этом событии {label} не считает (ci.yml, шаг evidence) — "
            f"не ожидался; {_NOT_A_RESULT}"
        )
    elif event and not origin.rules_known:
        why = (
            f"{where}: правила шага известны для CI самого хаба ({HUB_WORKFLOW}), "
            f"для этого CI — нет; ожидался ли шаг, не подтверждено; {_NOT_A_RESULT}"
        )
    elif event:
        why = (
            f"{where}: шаг считает {label} на dispatch и opened/reopened и "
            "пропускает на synchronize; действие события хаб не хранит — "
            f"ожидался ли шаг, не подтверждено; {_NOT_A_RESULT}"
        )
    else:
        why = (
            "отчёта CI о прогоне этого коммита нет — ожидался ли шаг, "
            f"не подтверждено; {_NOT_A_RESULT}"
        )
    return CIEvidenceBlock(
        run=where if event or run_id else "", expectation=kind, reason=f"{head}: {why}"
    )


def unconfirmed_block(reason: str, origin: Origin = Origin()):
    """A block whose origin the hub cannot confirm (no commit, unconfirmed report, odd shape).

    The event and the run, when the stored report names them, stay beside the
    reason — said to be unconfirmed, not dropped.
    """
    from hub.models import CIEvidenceBlock

    where = _where(origin.event, origin.run_id) if origin.event or origin.run_id else ""
    return CIEvidenceBlock(
        expectation=ORIGIN_UNCONFIRMED,
        run=f"{where} (не подтверждено)" if where else "",
        reason=reason,
    )


# The states the CI step scripts write (mutation_changed.py, red_test_baseline.py).
_STEP_STATES = {
    "мутации": frozenset(
        {"ran", "baseline_red", "no_changed_functions", "no_tests", "error"}
    ),
    "baseline": frozenset({"ran", "no_tests", "error"}),
}


def evidence_block(label: str, raw: str | None, pinned: str, origin=None):
    """One stored evidence block as the brief reads it (#1606).

    ``origin`` is the event and run of the current attempt's report: it says
    whether an empty block was to be expected (#2026, ``evidence_states``).
    """
    from hub.models import CIEvidenceBlock

    try:
        data = json.loads(raw or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict) or not data:
        return missing_block(label, pinned, origin or Origin())
    if data.get("state") not in _STEP_STATES[label]:
        # Provenance alone does not make an object the step's report.
        return unconfirmed_block(
            f"не получено: {label} для коммита {pinned[:12]} пришли в "
            "нераспознанном виде (нет известного state) — результатом не считаются",
            origin or Origin(),
        )
    prov = data.get("provenance")
    prov = prov if isinstance(prov, dict) and prov else {}
    if prov:
        run = (
            f"прогон {prov.get('run_id') or '?'} "
            f"({prov.get('event') or 'событие неизвестно'}), "
            f"{prov.get('at') or 'время неизвестно'}"
        )
        if prov.get("run_url"):
            run += f", {prov['run_url']}"
    else:
        run = "прогон неизвестен"
    adopted = data.get("adopted_from_run")
    if adopted:
        # #1919: the provenance above is the run that REPORTED; the numbers were
        # counted by the dispatch run on the same head. The file says so itself,
        # so this is the run's own word, not something the hub verified.
        run += f"; усыновлено из dispatch-прогона {adopted} (со слов прогона)"
    return CIEvidenceBlock(
        state="received",
        expectation=RECEIVED,
        result=str(data.get("state") or ""),
        run=run,
        provenance=prov,
        reason=str(data.get("reason") or ""),
    )


#: Said once per brief, not per AC: the MCP brief has a character budget.
MANUAL_AC_NOTE = (
    "тестовый локатор неприменим к этим AC; это не отметка об их выполнении"
)
_SUMMARY_CAP = 10


def manual_ac_summary(acs: Iterable[Any]) -> dict[str, Any] | None:
    """The compact form for the brief: ids with their kind, the note once, a cap."""
    notes = manual_ac_notes(acs)
    if not notes:
        return None
    shown = [f"{n['ac_id']}={n['verifiable_by']}" for n in notes[:_SUMMARY_CAP]]
    return {"acs": shown, "more": len(notes) - len(shown), "note": MANUAL_AC_NOTE}


def manual_ac_notes(acs: Iterable[Any]) -> list[dict[str, str]]:
    """Per AC that is not verified by a test: only that a test locator does not apply.

    No "выполнено" and no "не выполнено": a locator is about a named test, and
    these criteria name none.
    """
    out: list[dict[str, str]] = []
    for ac in acs:
        data = ac if isinstance(ac, Mapping) else ac.model_dump(mode="json")
        kind = str(data.get("verifiable_by") or "")
        if kind and kind != "test":
            out.append(
                {
                    "ac_id": str(data.get("id") or ""),
                    "verifiable_by": kind,
                    "note": (
                        f"тестовый локатор неприменим (verifiable_by={kind}); "
                        "это не отметка о выполнении AC"
                    ),
                }
            )
    return out


def downgrade_rules(reasons: Iterable[str]) -> list[str]:
    """The rule each reason of a deep -> lite downgrade comes from."""
    from hub.services.review_dispatch import CIRCLE_STOP_REASON, DEEP_CAP_REASON_MARK

    rules: list[str] = []
    for mark, rule in ((DEEP_CAP_REASON_MARK, "#1414"), (CIRCLE_STOP_REASON, "#1432")):
        if any(mark in r for r in reasons) and rule not in rules:
            rules.append(rule)
    return rules
