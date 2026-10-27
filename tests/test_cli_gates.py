"""CLI: approve, decide, return-to-work, force-complete и approve-batch."""

from __future__ import annotations

import argparse
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from hub import cli


def test_cmd_approve_passes_force_flag() -> None:
    args = argparse.Namespace(
        task_id=5, comment="hot fix", run=True, runtime="vast", force=True
    )
    mock_api = MagicMock(return_value={"id": 5, "status": "open"})
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_approve(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/5/approve",
        {"comment": "hot fix", "run": True, "force": True, "runtime": "vast"},
    )


def test_cmd_decide_accept_with_summary() -> None:
    result = {"id": 10, "status": "completed"}
    mock_api = MagicMock(return_value=result)
    args = argparse.Namespace(
        task_id=10,
        accept=True,
        rework=False,
        message="",
        summary="Accepted after review.",
        record_decision=True,
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_decide(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/10/decide",
        {
            "action": "accept",
            "instructions": "",
            "decision_summary": "Accepted after review.",
            "record_decision": True,
            "confirm_no_job": False,
        },
    )


def test_cmd_decide_rework_with_summary() -> None:
    result = {"id": 11, "status": "fix_requested"}
    mock_api = MagicMock(return_value=result)
    args = argparse.Namespace(
        task_id=11,
        accept=False,
        rework=True,
        message="Fix the bug.",
        summary="Edge case in auth.",
        record_decision=False,
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_decide(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/11/decide",
        {
            "action": "rework",
            "instructions": "Fix the bug.",
            "decision_summary": "Edge case in auth.",
            "record_decision": False,
            "confirm_no_job": False,
        },
    )


def test_return_to_work_command_calls_the_rest_action() -> None:
    # AC-4 (#1356): CLI — обёртка над тем же REST-действием, что и кнопка.
    result = {"id": 1241, "status": "open"}
    mock_api = MagicMock(return_value=result)
    args = argparse.Namespace(task_id=1241, reason="Держатель молчит с 17.09")
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_return_to_work(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/1241/return-to-work",
        {"reason": "Держатель молчит с 17.09"},
    )

    parser = cli.build_parser()
    parsed = parser.parse_args(["return-to-work", "1241", "--reason", "пропал"])
    assert parsed.func is cli.cmd_return_to_work
    assert parsed.task_id == 1241 and parsed.reason == "пропал"
    with pytest.raises(SystemExit):
        parser.parse_args(["return-to-work", "1241"])  # причина обязательна

    # Флаг брошенного job доезжает до того же REST-действия.
    mock_api.reset_mock()
    parsed = parser.parse_args(
        ["return-to-work", "1241", "--reason", "умер", "--abandon-active-job"]
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        assert cli.cmd_return_to_work(parsed) == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/1241/return-to-work",
        {"reason": "умер", "abandon_active_job": True},
    )


def test_cmd_decide_without_summary() -> None:
    result = {"id": 12, "status": "completed"}
    mock_api = MagicMock(return_value=result)
    args = argparse.Namespace(
        task_id=12,
        accept=True,
        rework=False,
        message="",
        summary="",
        record_decision=False,
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_decide(args)
    assert rc == 0
    body = mock_api.call_args.args[2]
    assert body["decision_summary"] == ""
    assert body["record_decision"] is False


def test_cmd_force_complete_passes_message() -> None:
    args = argparse.Namespace(task_id=9, message="reviewed manually")
    mock_api = MagicMock(return_value={"id": 9, "status": "completed"})
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_force_complete(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST", "/api/tasks/9/force-complete", {"comment": "reviewed manually"}
    )


def test_cmd_force_complete_default_empty_message() -> None:
    args = argparse.Namespace(task_id=9, message="")
    mock_api = MagicMock(return_value={"id": 9, "status": "completed"})
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_force_complete(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST", "/api/tasks/9/force-complete", {"comment": ""}
    )


def test_cmd_approve_batch() -> None:
    result = {"approved": [1, 2], "skipped": [{"task_id": 3, "reason": "dor_failed"}]}
    mock_api = MagicMock(return_value=result)
    args = argparse.Namespace(
        task_ids=[1, 2, 3],
        min_readiness=80,
        no_require_dor=False,
        allow_high_risks=False,
        comment="batch",
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_approve_batch(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/batch-approve",
        {
            "task_ids": [1, 2, 3],
            "require_dor_passed": True,
            "exclude_high_risks": True,
            "min_readiness": 80,
            "comment": "batch",
        },
    )


def test_cmd_steward_false_approve_clear() -> None:
    """Снятие ошибочного одобрения пары (#1601): один вызов REST, тело с заметкой."""
    mock_api = MagicMock(return_value={"task_id": 42, "cleared": 1})
    args = argparse.Namespace(task_id=42, note="разобрано")
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_steward_false_approve_clear(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/42/steward-false-approve/clear",
        {"note": "разобрано"},
    )
