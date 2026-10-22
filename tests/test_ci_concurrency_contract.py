"""#1923: only an older pull_request synchronize run of a pull request is cancelled.

Kept apart from tests/test_ci_workflow_contract.py: that file sits at its ceiling in
the ledger of test sizes (#1932: the ledger of tests only shrinks), so the new
contract lives in a file of its own and reads the same workflow through the same
helpers.
"""

from __future__ import annotations

from tests.test_ci_workflow_contract import _Condition, _doc, _text, _tokenize


def _concurrency_group(expression: str, **env: str) -> str:
    """Evaluate the workflow-level group: ``COND && format(A, X) || format(B, Y)``.

    The strict evaluator of the job conditions has no ``format()``; this accepts
    exactly the one shape the workflow uses and refuses any other, so a changed
    expression fails here instead of being guessed.
    """
    import re

    text = expression.strip()
    assert text.startswith("${{") and text.endswith("}}"), expression
    match = re.fullmatch(
        r"(?P<cond>.+?) && format\('(?P<a>[^']*)', (?P<x>[\w.]+)\)"
        r" \|\| format\('(?P<b>[^']*)', (?P<y>[\w.]+)\)",
        text[3:-2].strip(),
    )
    assert match, f"the group expression changed its shape: {expression!r}"
    values = {
        "github.event.pull_request.number": str(env.get("pr", "0")),
        "github.run_id": str(env.get("run_id", "0")),
    }
    holds = _Condition(
        _tokenize(match["cond"]),
        {
            "github.event_name": env["event"],
            "github.event.action": env.get("action", ""),
        },
        {"success()": True, "failure()": False, "cancelled()": False, "always()": True},
    ).parse()
    if holds:
        return match["a"].replace("{0}", values[match["x"]])
    return match["b"].replace("{0}", values[match["y"]])


def test_cancel_in_progress_only_for_pr_synchronize() -> None:
    """#1923 AC-1: only a pull_request synchronize run is cancelled, and only by one.

    The delivery gate reads every run on a sha and one cancelled run is `failed`
    for good, so a dispatch, a push to develop/main, an opened/reopened run and the
    deploy must have a group of their own (their run id) and never be cancelled.
    """
    doc = _doc()
    concurrency = doc["concurrency"]
    group, cancel = concurrency["group"], concurrency["cancel-in-progress"]

    def cancelled(event: str, action: str = "") -> bool:
        text = cancel.strip()
        assert text.startswith("${{") and text.endswith("}}"), cancel
        return bool(
            _Condition(
                _tokenize(text[3:-2]),
                {"github.event_name": event, "github.event.action": action},
                {
                    "success()": True,
                    "failure()": False,
                    "cancelled()": False,
                    "always()": True,
                },
            ).parse()
        )

    runs = [
        ("pull_request", "synchronize", True),
        ("pull_request", "opened", False),
        ("pull_request", "reopened", False),
        ("workflow_dispatch", "", False),
        ("push", "", False),
    ]
    for event, action, expected in runs:
        assert cancelled(event, action) is expected, (event, action)

    # the group of a synchronize run is its pull request: two pushes meet;
    # every other run is alone in a group named by its own run id
    first = _concurrency_group(
        group, event="pull_request", action="synchronize", pr="7", run_id="100"
    )
    second = _concurrency_group(
        group, event="pull_request", action="synchronize", pr="7", run_id="101"
    )
    other_pr = _concurrency_group(
        group, event="pull_request", action="synchronize", pr="8", run_id="102"
    )
    assert first == second != other_pr
    alone = {
        _concurrency_group(group, event=event, action=action, pr="7", run_id=rid)
        for rid, (event, action, _) in enumerate(runs[1:], start=200)
    }
    assert len(alone) == len(runs) - 1, "a run that is not a synchronize shares a group"
    assert first not in alone
    # a re-run of the SAME run keeps its group (it is the same run id)
    assert _concurrency_group(group, event="push", run_id="5") == _concurrency_group(
        group, event="push", run_id="5"
    )
    # the deploy keeps its own group and never cancels
    deploy = doc["jobs"]["deploy"]["concurrency"]
    assert deploy == {"group": "deploy-production", "cancel-in-progress": False}
    # nothing else in the file cancels anything
    assert _text().count("cancel-in-progress") == 2, "the workflow and the deploy only"
