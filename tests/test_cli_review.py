"""CLI: сдача на ревью, вердикты, исходы находок и судья."""

from __future__ import annotations

import argparse
import json
from io import StringIO
from unittest.mock import MagicMock, patch

from hub import cli
from tests.cli_support import _assert_no_traceback, _run_main


def test_cmd_submit_review() -> None:
    result = {"id": 42, "status": "review", "submission_generation": 1}
    mock_api = MagicMock(return_value=result)
    args = argparse.Namespace(task_id=42, agent="dev", summary="first pass")
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_submit_review(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/42/submit-review",
        {"agent": "dev", "summary": "first pass"},
    )


def test_cmd_submit_review_prints_path_notices_as_returned() -> None:
    """#1589: CLI печатает ответ хаба как есть — поле path_notices доходит."""
    notices = {"generation": 1, "state": "matched", "text": "обновить копию"}
    result = {"id": 42, "status": "review", "path_notices": notices}
    out = StringIO()
    args = argparse.Namespace(task_id=42, agent="dev", summary="")
    with (
        patch.object(cli, "_api", MagicMock(return_value=result)),
        patch("sys.stdout", new=out),
    ):
        assert cli.cmd_submit_review(args) == 0
    assert json.loads(out.getvalue())["path_notices"] == notices


def test_cmd_review_brief() -> None:
    mock_api = MagicMock(return_value={"task_id": 42, "title": "T"})
    args = argparse.Namespace(task_id=42)
    with (
        patch.object(cli, "_api", mock_api),
        patch.object(cli, "_verdict_block", MagicMock(return_value="блок")),
        patch("sys.stdout", new=StringIO()),
        patch("sys.stderr", new=StringIO()),
    ):
        rc = cli.cmd_review_brief(args)
    assert rc == 0
    mock_api.assert_called_once_with("GET", "/api/tasks/42/review-brief")


def test_cmd_review_verdict_with_findings() -> None:
    result = {"id": 42, "status": "running"}
    mock_api = MagicMock(return_value=result)
    findings = [{"id": 1, "severity": "high", "message": "Fix it"}]
    args = argparse.Namespace(
        task_id=42,
        verdict="changes_requested",
        comments="see findings",
        agent="reviewer",
        findings_json=json.dumps(findings),
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_review_verdict(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/42/review-verdict",
        {
            "verdict": "changes_requested",
            "comments": "see findings",
            "agent": "reviewer",
            "findings": findings,
        },
    )


def test_cmd_review_verdict_forwards_auto_draft_flag() -> None:
    # #436: --create-tasks-for-out-of-scope passes through to the REST body.
    result = {"id": 42, "status": "running"}
    mock_api = MagicMock(return_value=result)
    findings = [
        {"id": 1, "severity": "low", "message": "Elsewhere", "scope": "out_of_scope"},
        {"id": 2, "severity": "high", "message": "Fix it"},
    ]
    args = argparse.Namespace(
        task_id=42,
        verdict="changes_requested",
        comments="",
        agent="reviewer",
        findings_json=json.dumps(findings),
        create_tasks_for_out_of_scope=True,
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_review_verdict(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/42/review-verdict",
        {
            "verdict": "changes_requested",
            "agent": "reviewer",
            "findings": findings,
            "create_tasks_for_out_of_scope": True,
        },
    )


def test_cmd_review_verdict_rejects_bad_findings_json(capsys) -> None:
    args = argparse.Namespace(
        task_id=42,
        verdict="approved",
        comments="",
        agent="",
        findings_json="{not json",
    )
    mock_api = MagicMock()
    with patch.object(cli, "_api", mock_api):
        rc = cli.cmd_review_verdict(args)
    assert rc == 2
    assert "invalid --findings-json" in capsys.readouterr().err
    mock_api.assert_not_called()


def test_cmd_steward_judgement() -> None:
    mock_api = MagicMock(return_value={"id": 1, "verdict": "approve"})
    args = argparse.Namespace(
        task_id=42,
        generation=1,
        kind="verdict",
        verdict="approve",
        confidence="high",
        escalate_reason="",
        model="gpt-5.3-codex",
        grounds_json='[{"source":"ci_pinned_sha"}]',
        closures_json="",
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_steward_judgement(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/42/steward-judgement",
        {
            "generation": 1,
            "kind": "verdict",
            "verdict": "approve",
            "confidence": "high",
            "model": "gpt-5.3-codex",
            "grounds": [{"source": "ci_pinned_sha"}],
        },
    )


def test_main_review_verdict_rejects_bad_findings_json(capsys) -> None:
    rc, api = _run_main(
        ["review-verdict", "9", "approved", "--findings-json", "{not-json"]
    )
    assert rc == 2
    api.assert_not_called()
    _, err = _assert_no_traceback(capsys)
    assert "invalid --findings-json" in err


# --- исходы находок в отчёте о готовности (#1155) ----------------------------
def test_done_refuses_broken_finding_outcomes_json(capsys) -> None:
    """AC-4: неразбираемый JSON — отказ с ненулевым кодом и без запроса.

    Отправить отчёт без исходов и вернуть 0 значило бы записать готовность,
    потеряв ответ автора: молча потерянный ответ неотличим от неответа, а
    узнал бы автор об этом только от гейта на следующей сдаче.
    """
    rc, api = _run_main(
        [
            "update",
            "42",
            "--kind",
            "done",
            "--message",
            "готово",
            "--finding-outcomes",
            '[{"finding_uid": "abc", ',
        ]
    )

    assert rc == 2
    # Именно not_called: сдача без исходов не должна уезжать вовсе.
    api.assert_not_called()
    _, err = _assert_no_traceback(capsys)
    assert "--finding-outcomes is not valid JSON" in err


def test_done_sends_valid_finding_outcomes(capsys) -> None:
    """И разобранные исходы доезжают до тела запроса, а не только не падают.

    Без этой половины отказ выше зелен и тогда, когда поле не отправляется
    никогда.
    """
    rc, api = _run_main(
        [
            "update",
            "42",
            "--kind",
            "done",
            "--message",
            "готово",
            "--finding-outcomes",
            '[{"finding_uid": "abc", "outcome": "fixed"}]',
        ]
    )

    assert rc == 0
    body = api.call_args.args[2]
    assert body["kind"] == "done"
    assert body["finding_outcomes"] == [{"finding_uid": "abc", "outcome": "fixed"}]


def test_submit_review_still_refuses_broken_finding_outcomes_json(capsys) -> None:
    """Общий разбор не сменил поведение сдачи: тот же код и тот же текст."""
    rc, api = _run_main(
        ["submit-review", "42", "--finding-outcomes", "{не json"],
    )

    assert rc == 2
    api.assert_not_called()
    _, err = _assert_no_traceback(capsys)
    assert "--finding-outcomes is not valid JSON" in err


def test_the_parser_knows_the_advisor_kind_and_its_verdicts() -> None:
    """CLI принимает kind=advisor и ответы concur/object — тот же словарь, что у хаба."""
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "steward-judgement",
            "7",
            "--generation",
            "1",
            "--kind",
            "advisor",
            "--verdict",
            "concur",
        ]
    )
    assert (args.kind, args.verdict) == ("advisor", "concur")
    args = parser.parse_args(["steward-false-approve-clear", "7", "--note", "ok"])
    assert args.func is cli.cmd_steward_false_approve_clear and args.note == "ok"
