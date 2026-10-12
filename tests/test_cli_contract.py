"""CLI: refine, ac, risk, readiness и subtasks — правка контракта задачи."""

from __future__ import annotations

import argparse
import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hub import cli
from tests.cli_support import _assert_no_traceback, _run_main


# ---------------------------------------------------------------------------
# Structured task form (#42)
# ---------------------------------------------------------------------------
def _refine_args(**overrides) -> argparse.Namespace:
    """Build a refine Namespace with all CLI fields defaulted to None.

    cmd_refine reads attributes via getattr(..., None), so we mirror the
    parser's shape to keep the test focused on payload assembly.
    """
    base = {
        "task_id": 42,
        "from_file": None,
        "work_type": None,
        "class_of_service": None,
        "size": None,
        "wip_tag": None,
        "due_date": None,
        "user_story": None,
        "problem": None,
        "value": None,
        "tech_hints": None,
        "scope_in": None,
        "scope_out": None,
        "affected_area": None,
        "validation": None,
        "constraint": None,
        "assumption": None,
        "out_of_scope_review": None,
        "review_check": None,
        "human_owner": None,
        "human_reviewer": None,
        "title": None,
        "clear_acs": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cmd_refine_only_includes_provided_fields() -> None:
    """PATCH semantics: omitted CLI flags must be omitted from the body
    so the server doesn't clobber the existing value."""
    args = _refine_args(
        work_type="bug",
        problem="login fails",
        scope_in=["auth", "session"],
        validation=["uv run pytest -q"],
    )
    mock_api = MagicMock(return_value={"updated_columns": ["work_type"]})
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_refine(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/42/refine",
        {
            "work_type": "bug",
            "problem_statement": "login fails",
            "scope_in": ["auth", "session"],
            "validation_commands": ["uv run pytest -q"],
        },
    )


def test_cmd_refine_bulk_posts_items_from_file(tmp_path: Path) -> None:
    f = tmp_path / "bulk.json"
    f.write_text(
        json.dumps(
            {
                "items": [
                    {"task_id": 1, "problem_statement": "ps"},
                    {"task_id": 2, "user_story": "us"},
                ]
            }
        )
    )
    args = argparse.Namespace(from_file=str(f))
    mock_api = MagicMock(return_value={"results": []})
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_refine_bulk(args)
    assert rc == 0
    method, path, payload = mock_api.call_args.args
    assert method == "POST"
    assert path == "/api/tasks/refine-bulk"
    assert payload["items"][0]["task_id"] == 1


def test_cmd_refine_bulk_requires_items_array(tmp_path: Path) -> None:
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({"nope": 1}))
    args = argparse.Namespace(from_file=str(f))
    mock_api = MagicMock()
    with patch.object(cli, "_api", mock_api), patch("sys.stderr", new=StringIO()):
        rc = cli.cmd_refine_bulk(args)
    assert rc == 2
    mock_api.assert_not_called()


def test_cmd_refine_with_owner_and_reviewer() -> None:
    args = _refine_args(human_owner="alice", human_reviewer="bob")
    mock_api = MagicMock(return_value={})
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_refine(args)
    assert rc == 0
    payload = mock_api.call_args.args[2]
    assert payload["human_owner"] == "alice"
    assert payload["human_reviewer"] == "bob"


def test_cmd_refine_with_title() -> None:
    args = _refine_args(title="Renamed task")
    mock_api = MagicMock(return_value={"updated_columns": ["title"]})
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_refine(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/42/refine",
        {"title": "Renamed task"},
    )


def test_cmd_refine_review_check_repeatable_flag() -> None:
    """--review-check is repeatable and lands as review_checklist list."""
    args = _refine_args(review_check=["check migration", "verify rollback"])
    mock_api = MagicMock(return_value={"updated_columns": ["review_checklist"]})
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_refine(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/42/refine",
        {"review_checklist": ["check migration", "verify rollback"]},
    )


def test_cmd_refine_clear_acs_sends_empty_list() -> None:
    args = _refine_args(clear_acs=True)
    mock_api = MagicMock(return_value={"ac_count": 0})
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_refine(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST", "/api/tasks/42/refine", {"acceptance_criteria": []}
    )


def test_cmd_refine_empty_payload_returns_2_without_calling_api() -> None:
    args = _refine_args()
    mock_api = MagicMock()
    with (
        patch.object(cli, "_api", mock_api),
        patch("sys.stderr", new=StringIO()) as err,
    ):
        rc = cli.cmd_refine(args)
    assert rc == 2
    mock_api.assert_not_called()
    assert "Nothing to refine" in err.getvalue()


def test_cmd_refine_from_file_overlay_with_cli_override(tmp_path: Path) -> None:
    """--from-file is the base layer; explicit CLI flags win."""
    f = tmp_path / "task.json"
    f.write_text(
        json.dumps({"work_type": "feature", "size": "M", "user_story": "from file"})
    )
    args = _refine_args(
        from_file=str(f),
        work_type="bug",  # overrides feature
        scope_in=["x"],
    )
    mock_api = MagicMock(return_value={})
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_refine(args)
    assert rc == 0
    payload = mock_api.call_args.args[2]
    assert payload["work_type"] == "bug"
    assert payload["size"] == "M"
    assert payload["user_story"] == "from file"
    assert payload["scope_in"] == ["x"]


def test_cmd_ac_add_sends_full_body() -> None:
    args = argparse.Namespace(
        task_id=7,
        id="AC-1",
        given="g",
        when="w",
        then="t",
        by="test",
        test_ref="tests/x.py",
    )
    mock_api = MagicMock(return_value={"id": "AC-1"})
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_ac_add(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/7/acceptance_criteria",
        {
            "id": "AC-1",
            "given": "g",
            "when": "w",
            "then": "t",
            "verifiable_by": "test",
            "test_ref": "tests/x.py",
        },
    )


def test_cmd_ac_upsert_puts_by_id() -> None:
    args = argparse.Namespace(
        task_id=7,
        id="AC-1",
        given="g",
        when="w",
        then="t",
        by="manual",
        test_ref="",
    )
    mock_api = MagicMock(return_value={"id": "AC-1"})
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_ac_upsert(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "PUT",
        "/api/tasks/7/acceptance_criteria/AC-1",
        {
            "id": "AC-1",
            "given": "g",
            "when": "w",
            "then": "t",
            "verifiable_by": "manual",
        },
    )


def test_cmd_ac_delete_url_encodes_id() -> None:
    """ac_id can contain spaces or slashes; the URL must be percent-encoded."""
    args = argparse.Namespace(task_id=7, id="AC 1/v2")
    mock_api = MagicMock(return_value=None)
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_ac_delete(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "DELETE", "/api/tasks/7/acceptance_criteria/AC%201%2Fv2"
    )


def test_cmd_ac_replace_loads_array_from_file(tmp_path: Path) -> None:
    f = tmp_path / "acs.json"
    items = [
        {"id": "AC-1", "given": "g", "when": "w", "then": "t", "verifiable_by": "test"},
        {
            "id": "AC-2",
            "given": "g2",
            "when": "w2",
            "then": "t2",
            "verifiable_by": "manual",
        },
    ]
    f.write_text(json.dumps(items))
    args = argparse.Namespace(task_id=7, from_file=str(f))
    mock_api = MagicMock(return_value=items)
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_ac_replace(args)
    assert rc == 0
    mock_api.assert_called_once_with("PUT", "/api/tasks/7/acceptance_criteria", items)


def test_cmd_ac_replace_rejects_non_array(tmp_path: Path) -> None:
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({"id": "AC-1"}))
    args = argparse.Namespace(task_id=7, from_file=str(f))
    mock_api = MagicMock()
    with (
        patch.object(cli, "_api", mock_api),
        patch("sys.stderr", new=StringIO()) as err,
    ):
        rc = cli.cmd_ac_replace(args)
    assert rc == 2
    mock_api.assert_not_called()
    assert "must contain a JSON/YAML array" in err.getvalue()


def test_cmd_risk_add_uses_dedicated_endpoint() -> None:
    args = argparse.Namespace(
        task_id=7,
        kind="performance",
        severity="medium",
        description="slow loop",
        mitigation="add index",
    )

    with (
        patch.object(cli, "_api", return_value={"id": 7, "risks": []}) as mock_api,
        patch("sys.stdout", new=StringIO()),
    ):
        rc = cli.cmd_risk_add(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/7/risks",
        {
            "kind": "performance",
            "severity": "medium",
            "description": "slow loop",
            "mitigation": "add index",
        },
    )


def test_cmd_readiness_human_summary_includes_score_and_missing() -> None:
    args = argparse.Namespace(task_id=12, explain=False, json=False)
    payload = {
        "score": 65,
        "dor_passed": False,
        "missing_required": ["has_problem_statement", "has_validation"],
        "risks": [],
        "recommendations": [
            {
                "field": "problem_statement",
                "severity": "blocking",
                "message": "Add a problem",
            },
            {"field": "size", "severity": "warning", "message": "Set size"},
        ],
    }
    mock_api = MagicMock(return_value=payload)
    with (
        patch.object(cli, "_api", mock_api),
        patch("sys.stdout", new=StringIO()) as out,
    ):
        rc = cli.cmd_readiness(args)
    assert rc == 0
    mock_api.assert_called_once_with("GET", "/api/tasks/12/readiness")
    text = out.getvalue()
    assert "score=65" in text
    assert "dor_passed=no" in text
    assert "has_problem_statement" in text
    assert "Add a problem" in text


def test_cmd_readiness_explain_passes_query_param() -> None:
    args = argparse.Namespace(task_id=12, explain=True, json=True)
    mock_api = MagicMock(return_value={"score": 100, "dor_passed": True})
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        cli.cmd_readiness(args)
    mock_api.assert_called_once_with("GET", "/api/tasks/12/readiness?explain=true")


def test_cmd_readiness_tree_lists_nodes() -> None:
    args = argparse.Namespace(task_id=46, include_root=False, json=False)
    payload = {
        "root_id": 46,
        "total": 2,
        "ready": 1,
        "not_ready": 1,
        "nodes": [
            {
                "id": 47,
                "title": "Ready",
                "status": "draft",
                "score": 100,
                "dor_passed": True,
                "missing_required": [],
            },
            {
                "id": 48,
                "title": "Not ready",
                "status": "draft",
                "score": 40,
                "dor_passed": False,
                "missing_required": ["has_problem_statement"],
            },
        ],
    }
    mock_api = MagicMock(return_value=payload)
    with (
        patch.object(cli, "_api", mock_api),
        patch("sys.stdout", new=StringIO()) as out,
    ):
        rc = cli.cmd_readiness_tree(args)
    assert rc == 0
    mock_api.assert_called_once_with("GET", "/api/tasks/46/readiness-tree")
    text = out.getvalue()
    assert "1/2 ready" in text
    assert "1 not ready" in text
    assert "#48" in text
    assert "has_problem_statement" in text


def test_cmd_readiness_tree_include_root_query() -> None:
    args = argparse.Namespace(task_id=46, include_root=True, json=True)
    mock_api = MagicMock(
        return_value={
            "root_id": 46,
            "total": 0,
            "ready": 0,
            "not_ready": 0,
            "nodes": [],
        }
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        cli.cmd_readiness_tree(args)
    mock_api.assert_called_once_with(
        "GET", "/api/tasks/46/readiness-tree?include_root=true"
    )


def test_main_refine_with_no_fields_exits_2_without_calling_api(capsys) -> None:
    rc, api = _run_main(["refine", "4"])
    assert rc == 2
    api.assert_not_called()
    _, err = _assert_no_traceback(capsys)
    assert "Nothing to refine" in err


def test_main_subtasks_bulk_and_ac_list_and_risk_list(tmp_path: Path, capsys) -> None:
    payload_path = tmp_path / "kids.json"
    payload_path.write_text(json.dumps({"items": [{"title": "Child"}]}))
    created = [
        {
            "id": 21,
            "title": "Child",
            "status": "draft",
            "task_type": "subtask",
            "runtime": "auto",
            "source": "agent",
        }
    ]
    rc, api = _run_main(
        [
            "subtasks-bulk",
            "7",
            "--from-file",
            str(payload_path),
            "--task-type",
            "subtask",
            "--source",
            "agent",
            "--agent",
            "cursor",
        ],
        api_result=created,
    )
    assert rc == 0
    method, path, body = api.call_args.args[:3]
    assert method == "POST"
    assert path == "/api/tasks/7/subtasks"
    assert body["task_type"] == "subtask"
    assert body["source"] == "agent"
    assert body["agent"] == "cursor"
    assert "Child" in capsys.readouterr().out

    rc, api = _run_main(["ac", "list", "7"], api_result=[])
    assert rc == 0
    assert "(no acceptance criteria)" in capsys.readouterr().out

    acs = [
        {
            "id": "AC-1",
            "verifiable_by": "log_check",
            "given": "g",
            "when": "w",
            "then": "t",
            "test_ref": "tests/test_cli.py",
        }
    ]
    rc, api = _run_main(["ac", "list", "7"], api_result=acs)
    assert rc == 0
    out = capsys.readouterr().out
    assert "AC-1" in out
    assert "tests/test_cli.py" in out

    rc, api = _run_main(["ac", "list", "7", "--json"], api_result=acs)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == acs

    rc, api = _run_main(["risk", "list", "7"], api_result={"risks": []})
    assert rc == 0
    assert "(no risks)" in capsys.readouterr().out

    risks = [
        {
            "kind": "security",
            "severity": "high",
            "description": "token leak",
            "mitigation": "rotate",
        }
    ]
    rc, api = _run_main(["risk", "list", "7"], api_result={"risks": risks})
    assert rc == 0
    out = capsys.readouterr().out
    assert "token leak" in out
    assert "rotate" in out

    rc, api = _run_main(["risk", "list", "7", "--json"], api_result={"risks": risks})
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == risks


def test_main_subtasks_bulk_rejects_non_object_file(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"title": "x"}]))
    rc, api = _run_main(["subtasks-bulk", "7", "--from-file", str(bad)])
    assert rc == 2
    api.assert_not_called()
    _, err = _assert_no_traceback(capsys)
    assert "JSON/YAML object" in err


def test_main_subtasks_bulk_defaults_task_type_and_source(tmp_path: Path) -> None:
    payload_path = tmp_path / "kids.json"
    payload_path.write_text(json.dumps({"items": [{"title": "Child"}]}))
    rc, api = _run_main(
        ["subtasks-bulk", "7", "--from-file", str(payload_path)],
        api_result=[],
    )
    assert rc == 0
    body = api.call_args.args[2]
    assert body["task_type"] == "subtask"
    assert body["source"] == "agent"


def test_main_subtasks_bulk_rejects_missing_items(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"title": "x"}))
    rc, api = _run_main(["subtasks-bulk", "7", "--from-file", str(bad)])
    assert rc == 2
    api.assert_not_called()
    assert "items array" in capsys.readouterr().err


def test_main_refine_from_file_rejects_array(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"work_type": "bug"}]))
    with pytest.raises(SystemExit) as exc:
        with patch.object(
            sys, "argv", ["oc-hub", "refine", "1", "--from-file", str(bad)]
        ):
            cli.main()
    assert exc.value.code == 2
    _, err = _assert_no_traceback(capsys)
    assert "JSON/YAML object" in err


def test_main_refine_bulk_rejects_non_object(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([]))
    rc, api = _run_main(["refine-bulk", "--from-file", str(bad)])
    assert rc == 2
    api.assert_not_called()
    assert "JSON/YAML object" in capsys.readouterr().err


def test_main_ac_upsert_with_test_ref() -> None:
    rc, api = _run_main(
        [
            "ac",
            "upsert",
            "7",
            "--id",
            "AC-2",
            "--given",
            "g",
            "--when",
            "w",
            "--then",
            "t",
            "--by",
            "test",
            "--test-ref",
            "tests/test_cli.py",
        ],
        api_result={"id": "AC-2"},
    )
    assert rc == 0
    body = api.call_args.args[2]
    assert body["test_ref"] == "tests/test_cli.py"
