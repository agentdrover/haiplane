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
from typing import Any

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


def event_of(stored: Mapping[str, Any] | None) -> str:
    """The event kind the stored report of the current attempt names, or ''."""
    return str(_provenance(stored).get("event") or "")


def run_id_of(stored: Mapping[str, Any] | None) -> str:
    return str(_provenance(stored).get("run_id") or "")


def expectation_of(event: str) -> str:
    if event in _EXPECTED:
        return MISSING_UNEXPECTEDLY
    if event in _NOT_EXPECTED:
        return NOT_EXPECTED
    return ORIGIN_UNCONFIRMED


def _where(event: str, run_id: str) -> str:
    return f"прогон {run_id or '?'} ({event or 'событие неизвестно'})"


def missing_block(label: str, pinned: str, event: str = "", run_id: str = ""):
    """The block for a ``label`` result the current attempt carries none of."""
    from hub.models import CIEvidenceBlock

    kind = expectation_of(event)
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


def unconfirmed_block(reason: str):
    """A block whose origin the hub cannot confirm (no commit, unconfirmed report, odd shape)."""
    from hub.models import CIEvidenceBlock

    return CIEvidenceBlock(expectation=ORIGIN_UNCONFIRMED, reason=reason)


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
