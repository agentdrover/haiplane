from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hub import cli


# Выдуманный sha в ответе prod-state: detect-secrets читает hex-строку как
# высокоэнтропийный секрет, поэтому значение живёт одной именованной константой
# с одной пометкой, а не пометкой на каждой строке, где оно встречается.
DEPLOYED_SHA = "abc123def456"  # pragma: allowlist secret


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def test_api_rejects_non_http_scheme(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "HUB_URL", "file:///etc/passwd")
    monkeypatch.setattr(cli, "HUB_TOKEN", "")
    with pytest.raises(SystemExit) as exc:
        cli._api("GET", "/api/tasks")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "HAIPLANE_HUB_URL" in err
    assert ("OPEN" + "CLAW") not in err, "Wave 5: no legacy name in errors"
    assert "must use http/https" in err


def test_api_uses_validated_base_url_for_requests(monkeypatch) -> None:
    monkeypatch.setattr(cli, "HUB_URL", "https://hub.example.test")
    monkeypatch.setattr(cli, "HUB_TOKEN", "")
    mock_urlopen = MagicMock(return_value=_FakeResponse(b'{"ok": true}'))
    with patch("urllib.request.urlopen", mock_urlopen):
        result = cli._api("GET", "/api/tasks")
    assert result == {"ok": True}
    request = mock_urlopen.call_args.args[0]
    assert request.full_url == "https://hub.example.test/api/tasks"


def test_cmd_list() -> None:
    tasks = [
        {
            "id": 1,
            "title": "Alpha",
            "status": "open",
            "task_type": "task",
            "runtime": "auto",
            "source": "human",
        },
    ]
    mock_api = MagicMock(return_value=tasks)
    args = argparse.Namespace(
        limit=10, status="open", type=None, parent=None, owner=None, reviewer=None
    )
    with (
        patch.object(cli, "_api", mock_api),
        patch("sys.stdout", new=StringIO()) as out,
    ):
        rc = cli.cmd_list(args)
    assert rc == 0
    mock_api.assert_called_once_with("GET", "/api/tasks?limit=10&status=open")
    assert "#1" in out.getvalue()
    assert "Alpha" in out.getvalue()


def test_cli_list_limit_default_stays_twenty() -> None:
    """#1229: CLI default matches MCP (20). Bounds live on REST/MCP, not here."""
    args = cli.build_parser().parse_args(["list"])
    assert args.limit == 20


def test_cmd_create() -> None:
    created = {"id": 99, "title": "New task", "status": "open"}
    mock_api = MagicMock(return_value=created)
    args = argparse.Namespace(
        title="New task",
        description="Desc",
        runtime="vast",
        run=True,
        no_review=True,
        parent=5,
        task_type="task",
        priority="high",
        owner="",
        reviewer="",
        request_id="",
    )
    with (
        patch.object(cli, "_api", mock_api),
        patch("sys.stdout", new=StringIO()) as out,
    ):
        rc = cli.cmd_task(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks",
        {
            "title": "New task",
            "description": "Desc",
            "runtime": "vast",
            "source": "human",
            "run_immediately": True,
            "auto_review": False,
            "task_type": "task",
            "priority": "high",
            "parent_id": 5,
        },
        extra_headers=None,
    )
    assert json.loads(out.getvalue()) == created


def test_cmd_create_with_owner_and_reviewer() -> None:
    created = {"id": 100, "title": "Owned task", "status": "open"}
    mock_api = MagicMock(return_value=created)
    args = argparse.Namespace(
        title="Owned task",
        description="",
        runtime="auto",
        run=False,
        no_review=False,
        parent=None,
        task_type="task",
        priority="medium",
        owner="alice",
        reviewer="bob",
        request_id="",
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_task(args)
    assert rc == 0
    body = mock_api.call_args.args[2]
    assert body["human_owner"] == "alice"
    assert body["human_reviewer"] == "bob"


def test_cmd_start() -> None:
    result = {"id": 3, "status": "running"}
    mock_api = MagicMock(return_value=result)
    args = argparse.Namespace(task_id=3, plan="Step one", runtime="openrouter")
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_start(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/3/start",
        {"plan": "Step one", "runtime": "openrouter"},
    )


def test_cmd_pair_start() -> None:
    result = {"id": 37, "status": "running", "branch": "task-37/pair-start"}
    mock_api = MagicMock(return_value=result)
    args = argparse.Namespace(task_id=37, plan="Plan: pair", agent="composer-analyst")
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_pair_start(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/37/pair-start",
        {"plan": "Plan: pair", "assigned_agent": "composer-analyst"},
    )


def test_cmd_update() -> None:
    upd = {"id": 1, "kind": "status", "content": "Done X"}
    mock_api = MagicMock(return_value=upd)
    args = argparse.Namespace(
        task_id=12, agent="tester", kind="blocker", message="Blocked by CI"
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_update(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/12/updates",
        {"agent": "tester", "kind": "blocker", "content": "Blocked by CI"},
    )


def test_cmd_show() -> None:
    task = {"id": 7, "title": "Show me", "status": "running"}
    mock_api = MagicMock(return_value=task)
    args = argparse.Namespace(task_id=7)
    with (
        patch.object(cli, "_api", mock_api),
        patch("sys.stdout", new=StringIO()) as out,
    ):
        rc = cli.cmd_status(args)
    assert rc == 0
    mock_api.assert_called_once_with("GET", "/api/tasks/7")
    assert json.loads(out.getvalue()) == task


def test_cmd_status_json_includes_worktree_path_key() -> None:
    """AC-6 (#989): oc-hub status dumps GET JSON, including worktree_path."""
    task = {
        "id": 7,
        "title": "Show me",
        "status": "running",
        "worktree_path": "/srv/.ws-worktrees/task-7",
    }
    mock_api = MagicMock(return_value=task)
    args = argparse.Namespace(task_id=7)
    with (
        patch.object(cli, "_api", mock_api),
        patch("sys.stdout", new=StringIO()) as out,
    ):
        rc = cli.cmd_status(args)
    assert rc == 0
    dumped = json.loads(out.getvalue())
    assert dumped["worktree_path"] == "/srv/.ws-worktrees/task-7"


def test_cmd_tree() -> None:
    tree = {
        "id": 1,
        "title": "Root",
        "task_type": "epic",
        "status": "open",
        "progress": {"completed": 1, "total": 4, "percent": 25},
        "children": [
            {
                "id": 2,
                "title": "Child",
                "task_type": "task",
                "status": "open",
                "children": [],
            },
        ],
    }
    mock_api = MagicMock(return_value=tree)
    args = argparse.Namespace(task_id=1)
    with (
        patch.object(cli, "_api", mock_api),
        patch("sys.stdout", new=StringIO()) as out,
    ):
        rc = cli.cmd_tree(args)
    assert rc == 0
    mock_api.assert_called_once_with("GET", "/api/tasks/1/tree")
    text = out.getvalue()
    assert "[epic] #1 Root" in text
    assert "  [task] #2 Child" in text


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


def test_cmd_list_with_owner_filter() -> None:
    tasks = [
        {
            "id": 1,
            "title": "A",
            "status": "open",
            "task_type": "task",
            "runtime": "auto",
            "source": "human",
            "human_owner": "alice",
            "human_reviewer": "bob",
        }
    ]
    mock_api = MagicMock(return_value=tasks)
    args = argparse.Namespace(
        limit=10, status=None, type=None, parent=None, owner="alice", reviewer=None
    )
    with (
        patch.object(cli, "_api", mock_api),
        patch("sys.stdout", new=StringIO()) as out,
    ):
        rc = cli.cmd_list(args)
    assert rc == 0
    mock_api.assert_called_once_with("GET", "/api/tasks?limit=10&human_owner=alice")
    assert "[owner:alice]" in out.getvalue()
    assert "[reviewer:bob]" in out.getvalue()


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


def test_print_http_error_pretty_prints_dor_failed_detail(capsys) -> None:
    body = json.dumps(
        {
            "detail": {
                "error": "dor_failed",
                "task_id": 12,
                "score": 40,
                "missing_required": ["has_problem_statement"],
                "recommendations": [
                    {
                        "field": "problem_statement",
                        "severity": "blocking",
                        "message": "Describe the problem",
                    }
                ],
                "hint": "pass force=true to override the DoR gate",
            }
        }
    )
    cli._print_http_error(422, body)
    err = capsys.readouterr().err
    assert "DoR failed" in err
    assert "score=40" in err
    assert "has_problem_statement" in err
    assert "Describe the problem" in err
    assert "force=true" in err


EXPECTED_TEMPLATE_NAMES = {
    "feature",
    "bug",
    "refactor",
    "chore",
    "docs",
    "spike",
    "incident",
}


def test_list_templates_returns_all_work_types() -> None:
    """All seven WorkType values must ship a YAML template."""
    assert set(cli._list_templates()) == EXPECTED_TEMPLATE_NAMES


def test_cmd_template_list_via_subcommand_arg(capsys) -> None:
    """`oc-hub template list` prints each template name once."""
    args = argparse.Namespace(work_type="list", out=None, force=False, list=False)
    rc = cli.cmd_template(args)
    assert rc == 0
    out = capsys.readouterr().out
    for name in EXPECTED_TEMPLATE_NAMES:
        assert f"- {name}" in out


def test_cmd_template_list_via_flag(capsys) -> None:
    """`oc-hub template <anything> --list` works as a shortcut."""
    args = argparse.Namespace(work_type="bug", out=None, force=False, list=True)
    rc = cli.cmd_template(args)
    assert rc == 0
    assert "feature" in capsys.readouterr().out


def test_cmd_template_show_prints_yaml_to_stdout(capsys) -> None:
    args = argparse.Namespace(work_type="feature", out=None, force=False, list=False)
    rc = cli.cmd_template(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("# Feature")
    assert "work_type: feature" in out
    assert "acceptance_criteria:" in out


def test_cmd_template_unknown_work_type_returns_2(capsys) -> None:
    args = argparse.Namespace(work_type="not_a_type", out=None, force=False, list=False)
    rc = cli.cmd_template(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "Unknown work_type" in err
    assert "feature" in err


def test_cmd_template_writes_file_to_out(tmp_path: Path) -> None:
    out_path = tmp_path / "nested" / "task.yaml"
    args = argparse.Namespace(
        work_type="bug", out=str(out_path), force=False, list=False
    )
    rc = cli.cmd_template(args)
    assert rc == 0
    text = out_path.read_text()
    assert text.startswith("# Bug")
    assert "work_type: bug" in text


def test_cmd_template_refuses_to_overwrite_without_force(
    tmp_path: Path, capsys
) -> None:
    out_path = tmp_path / "task.yaml"
    out_path.write_text("PRE-EXISTING")
    args = argparse.Namespace(
        work_type="chore", out=str(out_path), force=False, list=False
    )
    rc = cli.cmd_template(args)
    assert rc == 1
    assert out_path.read_text() == "PRE-EXISTING"
    assert "Refusing to overwrite" in capsys.readouterr().err


def test_cmd_template_force_overwrites(tmp_path: Path) -> None:
    out_path = tmp_path / "task.yaml"
    out_path.write_text("PRE-EXISTING")
    args = argparse.Namespace(
        work_type="docs", out=str(out_path), force=True, list=False
    )
    rc = cli.cmd_template(args)
    assert rc == 0
    assert out_path.read_text().startswith("# Docs")


@pytest.mark.parametrize("work_type", sorted(EXPECTED_TEMPLATE_NAMES))
def test_template_yaml_parses_into_valid_task_refine(work_type: str) -> None:
    """Each shipped template must round-trip through TaskRefine without error.

    Catches drift between YAML defaults and the Pydantic schema (enum
    typos, renamed fields, list/scalar mistakes) at CI time instead of
    when an Analyst actually loads the template.
    """
    import yaml  # type: ignore[import-untyped]

    from hub.models import TaskRefine

    text = cli._read_template(work_type)
    raw = yaml.safe_load(text)
    assert isinstance(raw, dict)
    # `work_type` field in the YAML must match the file name.
    assert raw.get("work_type") == work_type
    # Strip raw helper keys that are pure placeholders; Pydantic will
    # accept them as strings so this is just a sanity check.
    parsed = TaskRefine(**raw)
    assert parsed.work_type is not None


def test_template_yaml_files_match_dor_required_fields() -> None:
    """For each work_type, the template must populate every DoR-required
    field (or at least leave it as a non-empty placeholder), so a fresh
    `oc-hub refine --from-file` of an unedited template would still pass
    the DoR gate after filling placeholders.

    Specifically: DoR-required scalar fields must be present as keys in
    the YAML (not commented out), and required list fields must be
    non-empty lists.
    """
    import yaml  # type: ignore[import-untyped]

    from hub.services.dor import DOR_REQUIRED_BY_WORK_TYPE

    SCALAR_FIELDS = {
        "has_user_story": "user_story",
        "has_problem_statement": "problem_statement",
        "has_business_value": "business_value",
        "has_size": "size",
        "has_wip_tag": "wip_tag",
    }
    LIST_FIELDS = {
        "has_scope_in": "scope_in",
        "has_validation_commands": "validation_commands",
        "has_acceptance_criteria": "acceptance_criteria",
    }

    for work_type, required in DOR_REQUIRED_BY_WORK_TYPE.items():
        raw = yaml.safe_load(cli._read_template(work_type))
        for check in required:
            if check in SCALAR_FIELDS:
                field = SCALAR_FIELDS[check]
                assert field in raw, (
                    f"template {work_type}.yaml missing DoR-required field {field}"
                )
                assert raw[field], f"template {work_type}.yaml has empty {field}"
            elif check in LIST_FIELDS:
                field = LIST_FIELDS[check]
                assert field in raw, (
                    f"template {work_type}.yaml missing DoR-required list {field}"
                )
                assert isinstance(raw[field], list) and raw[field], (
                    f"template {work_type}.yaml has empty list {field}"
                )


def test_load_payload_file_yaml_without_pyyaml(tmp_path: Path, monkeypatch) -> None:
    """If a YAML file is requested but PyYAML isn't installed, exit cleanly."""
    f = tmp_path / "task.yaml"
    f.write_text("work_type: bug\n")
    import builtins

    real_import = builtins.__import__

    def blocked_import(name: str, *a, **kw):
        if name == "yaml":
            raise ImportError("simulated missing pyyaml")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(SystemExit) as exc:
        cli._load_payload_file(str(f))
    assert exc.value.code == 2


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
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
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


def test_cmd_projects_create() -> None:
    mock_api = MagicMock(return_value={"id": 2, "slug": "calc-kids"})
    args = argparse.Namespace(
        slug="calc-kids",
        name="Calc Kids",
        repo="mrPDA/calc-kids",
        workspace_path="/srv/calc",
        default_branch="develop",
        forge="github",
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_projects_create(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/projects",
        {
            "slug": "calc-kids",
            "name": "Calc Kids",
            "repo": "mrPDA/calc-kids",
            "workspace_path": "/srv/calc",
            "default_branch": "develop",
            # #1114: контракт публикуется трижды — REST, CLI, MCP. Поверхность,
            # где форж задать нельзя, тихо заводит проект на github.
            "forge": "github",
        },
    )


# --- oc-hub dep (#487) --------------------------------------------------------
#
# The CLI holds no rules of its own: it calls the same REST endpoints the MCP
# tools do (#486). Three implementations of one rule drift, and the one nobody
# touches drifts first.


def _dep_args(dep_cmd: str, task_id: int, depends_on: int, json_out: bool = False):
    return argparse.Namespace(
        dep_cmd=dep_cmd, task_id=task_id, depends_on=depends_on, json=json_out
    )


def test_dep_commands_match_the_rest_contract(capsys) -> None:
    # AC-5 (#487): behaviour and exit codes follow REST, including the
    # idempotent answers — "created" and "already existed" are different
    # facts with the same outcome, and the output says which happened.
    calls: list[tuple[str, str]] = []

    def _fake_api(method, path, body=None, **kwargs):
        calls.append((method, path))
        if method == "POST":
            return {"task_id": 830, "depends_on_task_id": 818, "created": True}
        if method == "DELETE":
            return {"task_id": 830, "depends_on_task_id": 818, "removed": False}
        return {
            "blocked_by": [
                {
                    "task_id": 818,
                    "title": "daily digest",
                    "status": "completed",
                    "delivered": False,
                    "reason": "PR #8 не смержен гейтом",
                }
            ],
            "unblocks": [],
        }

    with patch.object(cli, "_api", _fake_api):
        assert cli.cmd_dep(_dep_args("add", 830, 818)) == 0
        assert cli.cmd_dep(_dep_args("rm", 830, 818)) == 0
        assert cli.cmd_dep(_dep_args("list", 830, 0)) == 0

    out = capsys.readouterr().out
    assert "edge created" in out
    assert "was not there" in out
    # Delivery, not status: the blocker is completed and still blocks.
    assert "NOT delivered" in out and "PR #8" in out
    assert calls == [
        ("POST", "/api/tasks/830/dependencies"),
        ("DELETE", "/api/tasks/830/dependencies/818"),
        ("GET", "/api/tasks/830/dependencies"),
    ]


def test_dep_list_says_so_when_there_are_no_edges(capsys) -> None:
    with patch.object(cli, "_api", lambda *a, **k: {"blocked_by": [], "unblocks": []}):
        assert cli.cmd_dep(_dep_args("list", 1, 0)) == 0

    assert "no dependencies" in capsys.readouterr().out


# --- main() dispatch (#851) ---------------------------------------------------
#
# Coverage of build_parser only matters as a side effect of invoking a real
# command. These tests go through main() and assert return codes, operator
# messages, or the API call that would change Hub state.


def _run_main(
    argv: list[str],
    *,
    api_result: Any = None,
    api_side_effect: Any = None,
) -> tuple[int, MagicMock]:
    mock_api = MagicMock()
    if api_side_effect is not None:
        mock_api.side_effect = api_side_effect
    else:
        mock_api.return_value = {} if api_result is None else api_result
    with (
        patch.object(sys, "argv", ["oc-hub", *argv]),
        patch.object(cli, "_api", mock_api),
    ):
        try:
            rc = cli.main()
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    return int(rc), mock_api


def _assert_no_traceback(capsys) -> tuple[str, str]:
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Traceback" not in combined
    return captured.out, captured.err


def test_main_missing_required_flag_exits_2_without_traceback(capsys) -> None:
    rc, api = _run_main(["task"])
    assert rc == 2
    api.assert_not_called()
    _, err = _assert_no_traceback(capsys)
    assert "title" in err.lower() or "required" in err.lower()


def test_main_unknown_command_exits_2_without_traceback(capsys) -> None:
    rc, api = _run_main(["not-a-command"])
    assert rc == 2
    api.assert_not_called()
    _, err = _assert_no_traceback(capsys)
    assert "invalid choice" in err.lower() or "not-a-command" in err


def test_main_refine_with_no_fields_exits_2_without_calling_api(capsys) -> None:
    rc, api = _run_main(["refine", "4"])
    assert rc == 2
    api.assert_not_called()
    _, err = _assert_no_traceback(capsys)
    assert "Nothing to refine" in err


def test_main_review_verdict_rejects_bad_findings_json(capsys) -> None:
    rc, api = _run_main(
        ["review-verdict", "9", "approved", "--findings-json", "{not-json"]
    )
    assert rc == 2
    api.assert_not_called()
    _, err = _assert_no_traceback(capsys)
    assert "invalid --findings-json" in err


def test_main_task_create_sends_request_id_and_prints_created_task(capsys) -> None:
    created = {"id": 44, "title": "Idempotent", "status": "open"}
    rc, api = _run_main(
        [
            "task",
            "--title",
            "Idempotent",
            "--request-id",
            "req-44",
            "--owner",
            "alice",
        ],
        api_result=created,
    )
    assert rc == 0
    method, path, body = api.call_args.args[:3]
    assert method == "POST"
    assert path == "/api/tasks"
    assert body["client_request_id"] == "req-44"
    assert body["human_owner"] == "alice"
    assert api.call_args.kwargs["extra_headers"] == {"X-Client-Request-Id": "req-44"}
    assert json.loads(capsys.readouterr().out) == created


@pytest.mark.parametrize(
    "argv, task_type, extra",
    [
        (
            ["epic", "--title", "E", "--project", "hub", "--owner", "ada"],
            "epic",
            {"project": "hub"},
        ),
        (
            ["feature", "--title", "F", "--parent", "1", "--reviewer", "bob"],
            "feature",
            {"parent_id": 1},
        ),
        (["subtask", "--title", "S", "--parent", "2"], "subtask", {"parent_id": 2}),
    ],
)
def test_main_typed_create_posts_task_type(
    argv: list[str], task_type: str, extra: dict[str, Any], capsys
) -> None:
    created = {"id": 3, "title": argv[2], "status": "open", "task_type": task_type}
    rc, api = _run_main(argv, api_result=created)
    assert rc == 0
    body = api.call_args.args[2]
    assert body["task_type"] == task_type
    assert body["source"] == "human"
    for key, value in extra.items():
        assert body[key] == value
    assert json.loads(capsys.readouterr().out)["task_type"] == task_type


def test_main_claim_and_release_send_session_id(capsys) -> None:
    claimed = {"id": 12, "status": "claimed", "claimed_by": "cursor"}
    rc, api = _run_main(
        ["claim", "12", "--agent", "cursor", "--session-id", "sid-1"],
        api_result=claimed,
    )
    assert rc == 0
    assert api.call_args.args == (
        "POST",
        "/api/tasks/12/claim",
        {"agent": "cursor", "session_id": "sid-1"},
    )
    assert json.loads(capsys.readouterr().out)["status"] == "claimed"

    rc, api = _run_main(
        ["release", "12", "--agent", "cursor", "--session-id", "sid-1"],
        api_result={"id": 12, "status": "open"},
    )
    assert rc == 0
    assert api.call_args.args == (
        "POST",
        "/api/tasks/12/release",
        {"agent": "cursor", "session_id": "sid-1"},
    )


def test_main_pair_start_forwards_branch_and_session() -> None:
    rc, api = _run_main(
        [
            "pair-start",
            "37",
            "--plan",
            "Plan: pair",
            "--agent",
            "cursor",
            "--branch-slug",
            "task-37/cli-coverage",
            "--session-id",
            "sid-37",
        ],
        api_result={"id": 37, "status": "running"},
    )
    assert rc == 0
    assert api.call_args.args == (
        "POST",
        "/api/tasks/37/pair-start",
        {
            "plan": "Plan: pair",
            "assigned_agent": "cursor",
            "branch_slug": "task-37/cli-coverage",
            "session_id": "sid-37",
        },
    )


def test_main_pair_start_forwards_git_mode() -> None:
    rc, api = _run_main(
        [
            "pair-start",
            "41",
            "--plan",
            "Plan: remote",
            "--git-mode",
            "remote",
        ],
        api_result={"id": 41, "status": "running", "git_mode": "remote"},
    )
    assert rc == 0
    assert api.call_args.args == (
        "POST",
        "/api/tasks/41/pair-start",
        {"plan": "Plan: remote", "git_mode": "remote"},
    )


def test_main_reject_archive_withdraw_unarchive_delete(capsys) -> None:
    rc, api = _run_main(
        ["reject", "5", "--comment", "no"],
        api_result={"id": 5, "status": "rejected"},
    )
    assert rc == 0
    assert api.call_args.args == (
        "POST",
        "/api/tasks/5/reject",
        {"comment": "no"},
    )

    rc, api = _run_main(["archive", "5", "--no-cascade"], api_result={"id": 5})
    assert rc == 0
    assert api.call_args.args == ("POST", "/api/tasks/5/archive", {"cascade": False})

    rc, api = _run_main(["withdraw", "5"], api_result={"id": 5, "archived": True})
    assert rc == 0
    assert api.call_args.args[:2] == ("POST", "/api/tasks/5/withdraw")

    rc, api = _run_main(["unarchive", "5"], api_result={"id": 5})
    assert rc == 0
    assert api.call_args.args == ("POST", "/api/tasks/5/unarchive", {"cascade": True})

    rc, api = _run_main(["delete", "5"], api_result=None)
    assert rc == 0
    assert api.call_args.args[:2] == ("DELETE", "/api/tasks/5")
    assert "Task #5 deleted." in capsys.readouterr().out


def test_main_question_answer_propose_and_updates(capsys) -> None:
    rc, api = _run_main(
        ["question", "8", "--message", "blocked?", "--agent", "cursor"],
        api_result={"id": 8, "status": "needs_info"},
    )
    assert rc == 0
    assert api.call_args.args == (
        "POST",
        "/api/tasks/8/question",
        {"agent": "cursor", "question": "blocked?"},
    )

    rc, api = _run_main(
        ["answer", "8", "--message", "go", "--no-resume"],
        api_result={"id": 8, "status": "running"},
    )
    assert rc == 0
    assert api.call_args.args == (
        "POST",
        "/api/tasks/8/answer",
        {"answer": "go", "resume": False},
    )

    rc, api = _run_main(
        [
            "propose",
            "--title",
            "Draft",
            "--agent",
            "cursor",
            "--rationale",
            "need it",
            "--parent",
            "1",
        ],
        api_result={"id": 9, "status": "draft", "source": "agent"},
    )
    assert rc == 0
    body = api.call_args.args[2]
    assert body["source"] == "agent"
    assert body["parent_id"] == 1

    rc, api = _run_main(
        ["updates", "8"],
        api_result=[
            {
                "created_at": "2026-08-22T00:00:00Z",
                "kind": "status",
                "agent": "cursor",
                "content": "working",
            }
        ],
    )
    assert rc == 0
    assert api.call_args.args[:2] == ("GET", "/api/tasks/8/updates")
    assert "working" in capsys.readouterr().out


def test_main_list_filters_and_proposals_and_context(capsys) -> None:
    tasks = [
        {
            "id": 1,
            "title": "Mine",
            "status": "open",
            "task_type": "feature",
            "runtime": "auto",
            "source": "agent",
            "human_owner": "ada",
            "human_reviewer": "bob",
            "parent_id": 10,
            "archived": True,
        }
    ]
    rc, api = _run_main(
        [
            "list",
            "--status",
            "open",
            "--type",
            "feature",
            "--parent",
            "10",
            "--owner",
            "ada",
            "--reviewer",
            "bob",
            "--claimed-by",
            "cursor",
            "--mine",
            "ada",
            "--include-archived",
        ],
        api_result=tasks,
    )
    assert rc == 0
    path = api.call_args.args[1]
    assert path.startswith("/api/tasks?")
    assert "type=feature" in path
    assert "parent_id=10" in path
    assert "human_owner=ada" in path
    assert "claimed_by=cursor" in path
    assert "mine=ada" in path
    assert "include_archived=true" in path
    out = capsys.readouterr().out
    assert "[archived]" in out
    assert "[owner:ada]" in out

    rc, api = _run_main(["proposals", "--status", "pending"], api_result=tasks)
    assert rc == 0
    assert api.call_args.args[1] == "/api/tasks?status=draft&limit=50"
    assert "Mine" in capsys.readouterr().out

    rc, api = _run_main(
        ["context", "1", "--max-chars", "80", "--mode", "summary"],
        api_result={"context_text": "do this next"},
    )
    assert rc == 0
    path = api.call_args.args[1]
    assert path.startswith("/api/tasks/1/context?")
    assert "max_chars=80" in path
    assert "mode=summary" in path
    assert "do this next" in capsys.readouterr().out


def test_main_tree_forwards_caps_and_dep_json_unblocks(capsys) -> None:
    rc, api = _run_main(
        ["tree", "1", "--depth", "2", "--max-nodes", "9", "--mode", "summary"],
        api_result={
            "id": 1,
            "title": "Root",
            "task_type": "epic",
            "status": "open",
            "children": [],
        },
    )
    assert rc == 0
    path = api.call_args.args[1]
    assert "depth=2" in path
    assert "max_nodes=9" in path
    assert "mode=summary" in path

    edges = {
        "blocked_by": [],
        "unblocks": [{"task_id": 3, "title": "Next", "status": "open"}],
    }
    capsys.readouterr()
    rc, api = _run_main(["dep", "list", "1", "--json"], api_result=edges)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == edges

    rc, api = _run_main(["dep", "list", "1"], api_result=edges)
    assert rc == 0
    assert "unblocks   #3 Next" in capsys.readouterr().out


def test_main_outcome_and_projects_and_dashboard(capsys) -> None:
    rc, api = _run_main(
        ["outcome-debt"],
        api_result={"overdue": []},
    )
    assert rc == 0
    assert api.call_args.args[:2] == ("GET", "/api/metrics/outcome-debt")

    rc, api = _run_main(
        [
            "answer-outcome",
            "20",
            "--verdict",
            "moved",
            "--measured-value",
            "12%",
            "--note",
            "ok",
        ],
        api_result={"task_id": 20, "verdict": "moved"},
    )
    assert rc == 0
    assert api.call_args.args == (
        "POST",
        "/api/tasks/20/outcome-answers",
        {"verdict": "moved", "measured_value": "12%", "note": "ok"},
    )

    rc, api = _run_main(
        ["projects", "list", "--include-archived"],
        api_result=[{"slug": "hub"}],
    )
    assert rc == 0
    assert api.call_args.args[:2] == ("GET", "/api/projects?include_archived=true")

    capsys.readouterr()
    rc, api = _run_main(["dashboard"], api_result={"open": 1})
    assert rc == 0
    assert api.call_args.args[:2] == ("GET", "/api/dashboard")
    assert json.loads(capsys.readouterr().out) == {"open": 1}


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


def test_main_whoami_health_prod_state_and_readiness(capsys) -> None:
    who = {
        "username": "cursor",
        "role": "agent",
        "auth_source": "db",
        "api_key_id": 3,
        "principal_id": 4,
        "permissions_count": 2,
        "permissions_summary": ["tasks.read", "tasks.update"],
        "app_version": "0.1.0",
    }
    rc, api = _run_main(["whoami"], api_result=who)
    assert rc == 0
    out = capsys.readouterr().out
    assert "cursor" in out
    assert "API key id: 3" in out

    rc, api = _run_main(["whoami", "--json"], api_result=who)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["username"] == "cursor"

    health = {
        "status": "ok",
        "app_version": "0.1.0",
        "bind_host": "127.0.0.1",
        "bind_port": 8080,
        "auth_required": True,
        "auth_disabled": False,
        "env_tokens_configured": True,
        "vast_enabled": False,
    }
    rc, api = _run_main(["health"], api_result=health)
    assert rc == 0
    assert "Status: ok" in capsys.readouterr().out

    rc, api = _run_main(["health", "--json"], api_result=health)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    prod = {
        "deployed": {"sha": DEPLOYED_SHA, "ref": "main", "at": "2026-08-22"},
        "in_prod": [{"task_id": 1, "title": "A"}],
        "not_in_prod": [],
        "unknown": [],
        "note": "ok",
    }
    rc, api = _run_main(["prod-state", "--limit", "5"], api_result=prod)
    assert rc == 0
    assert api.call_args.args[1] == "/api/prod-state?limit=5"
    assert "Раскатано: abc123def456" in capsys.readouterr().out

    rc, api = _run_main(["prod-state", "--json"], api_result=prod)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["deployed"]["sha"] == DEPLOYED_SHA

    readiness = {
        "score": 40,
        "dor_passed": False,
        "missing_required": ["has_problem_statement"],
        "risks": [{"kind": "security", "severity": "high"}],
        "recommendations": [
            {"severity": "info", "field": "size", "message": "set size"},
        ],
    }
    rc, api = _run_main(["readiness", "3"], api_result=readiness)
    assert rc == 0
    out = capsys.readouterr().out
    assert "score=40" in out
    assert "security:high" in out
    assert "non-blocking suggestions" in out

    blocking = {
        **readiness,
        "recommendations": [
            {"severity": "blocking", "field": "problem_statement", "message": "fill"}
        ],
    }
    rc, api = _run_main(["readiness", "3"], api_result=blocking)
    assert rc == 0
    assert "Blocking recommendations" in capsys.readouterr().out


def test_main_admin_commands(capsys) -> None:
    rc, api = _run_main(["admin", "bootstrap", "--username", "root"])
    assert rc == 1
    api.assert_not_called()
    assert "Password is required" in capsys.readouterr().err

    with patch("getpass.getpass", return_value="secret"):
        rc, api = _run_main(
            [
                "admin",
                "bootstrap",
                "--username",
                "root",
                "--password-prompt",
                "--display-name",
                "Root",
            ],
            api_result={"id": 1},
        )
    assert rc == 0
    assert api.call_args.args[2]["password"] == "secret"  # pragma: allowlist secret
    assert "Admin user 'root' created" in capsys.readouterr().out

    rc, api = _run_main(
        ["admin", "users", "list"],
        api_result=[
            {
                "id": 2,
                "status": "active",
                "username": "ada",
                "display_name": "Ada",
                "roles": ["operator"],
            }
        ],
    )
    assert rc == 0
    assert "ada" in capsys.readouterr().out

    with patch("getpass.getpass", return_value="pw"):
        rc, api = _run_main(
            [
                "admin",
                "users",
                "create",
                "--username",
                "ada",
                "--password-prompt",
                "--role",
                "operator",
            ],
            api_result={"id": 2},
        )
    assert rc == 0
    assert api.call_args.args[2]["password"] == "pw"  # pragma: allowlist secret

    rc, api = _run_main(
        ["admin", "users", "disable", "missing"],
        api_result=[{"id": 2, "username": "ada"}],
    )
    assert rc == 1
    assert "not found" in capsys.readouterr().err

    rc, api = _run_main(
        ["admin", "users", "disable", "ada"],
        api_side_effect=[[{"id": 2, "username": "ada"}], {}],
    )
    assert rc == 0
    assert api.call_args_list[1].args[:2] == ("POST", "/api/admin/principals/2/disable")

    rc, api = _run_main(
        ["admin", "agents", "create", "--name", "bot"],
        api_result={"id": 9},
    )
    assert rc == 0
    assert api.call_args.args[2]["kind"] == "agent"

    rc, api = _run_main(
        ["admin", "keys", "create", "--principal", "missing", "--name", "k"],
        api_result=[],
    )
    assert rc == 1
    assert "not found" in capsys.readouterr().err

    rc, api = _run_main(
        [
            "admin",
            "keys",
            "create",
            "--principal",
            "ada",
            "--name",
            "ci",
            "--expires-days",
            "7",
        ],
        api_side_effect=[
            [{"id": 2, "username": "ada"}],
            {"id": 4, "key_prefix": "och_", "plaintext_key": "secret-key"},
        ],
    )
    assert rc == 0
    assert "secret-key" in capsys.readouterr().out

    rc, api = _run_main(["admin", "keys", "revoke", "4"], api_result={})
    assert rc == 0
    assert api.call_args.args[:2] == ("POST", "/api/admin/api-keys/4/revoke")

    rc, api = _run_main(
        ["admin", "roles", "list"],
        api_result=[
            {
                "slug": "agent",
                "name": "Agent",
                "system": True,
                "permissions": ["tasks.read"],
            }
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "[system]" in out
    assert "tasks.read" in out

    rc, api = _run_main(
        ["admin", "audit", "--limit", "3"],
        api_result=[
            {
                "created_at": "t",
                "actor_username": "ada",
                "action": "disable",
                "target_type": "principal",
                "target_id": "2",
                "summary": "disabled",
            }
        ],
    )
    assert rc == 0
    assert api.call_args.args[1] == "/api/admin/audit?limit=3"
    assert "disabled" in capsys.readouterr().out


def test_api_sends_bearer_token(monkeypatch) -> None:
    monkeypatch.setattr(cli, "HUB_URL", "https://hub.example.test")
    monkeypatch.setattr(cli, "HUB_TOKEN", "tok-1")
    mock_urlopen = MagicMock(return_value=_FakeResponse(b'{"ok": true}'))
    with patch("urllib.request.urlopen", mock_urlopen):
        cli._api(
            "POST",
            "/api/tasks",
            {"title": "x"},
            extra_headers={"X-Client-Request-Id": "req-1"},
        )
    request = mock_urlopen.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer tok-1"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("X-client-request-id") == "req-1"


def test_api_http_error_exits_1_without_traceback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "HUB_URL", "https://hub.example.test")
    monkeypatch.setattr(cli, "HUB_TOKEN", "")

    def _boom(req, timeout=30):
        raise urllib.error.HTTPError(
            req.full_url,
            409,
            "Conflict",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(b'{"reason":"already_claimed"}'),
        )

    with patch("urllib.request.urlopen", _boom), pytest.raises(SystemExit) as exc:
        cli._api("POST", "/api/tasks/1/claim", {"agent": "x"})
    assert exc.value.code == 1
    _, err = _assert_no_traceback(capsys)
    assert "HTTP 409" in err
    assert "already_claimed" in err


def test_api_connection_error_exits_1_without_traceback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "HUB_URL", "https://hub.example.test")
    monkeypatch.setattr(cli, "HUB_TOKEN", "")

    def _boom(req, timeout=30):
        raise urllib.error.URLError("refused")

    with patch("urllib.request.urlopen", _boom), pytest.raises(SystemExit) as exc:
        cli._api("GET", "/api/tasks")
    assert exc.value.code == 1
    _, err = _assert_no_traceback(capsys)
    assert "Connection error" in err


def test_print_http_error_plain_and_generic_json(capsys) -> None:
    cli._print_http_error(500, "plain boom")
    assert "HTTP 500: plain boom" in capsys.readouterr().err
    cli._print_http_error(409, '{"reason":"busy"}')
    assert "HTTP 409" in capsys.readouterr().err


def test_cmd_template_list_when_none_installed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_list_templates", lambda: [])
    args = argparse.Namespace(work_type="list", out=None, force=False, list=False)
    rc = cli.cmd_template(args)
    assert rc == 1
    assert "No templates installed." in capsys.readouterr().err


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


def test_cmd_undelivered_names_observation_and_the_frozen_window() -> None:
    """Находка d7b46ff7: пустой человеческий вывод читается как «чисто».

    ``cmd_undelivered`` берёт тот же ответ API, что и MCP, но обходил только
    два ведра из четырёх. Строка, закрытая наблюдением, и строка, вышедшая за
    окно свипа, до человека не доезжали: после закрытия оператор видел одну
    фразу «No completed task is waiting on an open PR» — то есть «проверено,
    чисто» вместо «закрыто чужим наблюдением, вот кем и по какому коммиту».
    Ровно то правило честности, ради которого MCP печатает третье ведро.

    Мутация по местам применения: снять печать наблюдения ИЛИ печать
    застывших строк — тест падает на своём assert поимённо.
    """
    payload = {
        "undelivered": [],
        "unknown": [
            {
                "task_id": 878,
                "title": "Маховик",
                "reason": "провайдер не ответил",
                "age_hours": 467,
                "still_swept": False,
            }
        ],
        "closed_by_observation": [
            {
                "task_id": 909,
                "title": "Паспорт дефекта",
                "state": "unknown",
                # Своя причина, не совпадающая с причиной строки #878 выше:
                # иначе assert про историю проходил бы за счёт чужого ведра.
                "reason": "репозиторий PR #468 удалён, спросить некого",
                "observed_by": "pda_claude",
                "observed_sha": "19ee3f6faf9f",
                "observed_probe": "git show 19ee3f6faf9f --stat",
                "observed_evidence": "файл на месте, AC-тест зелёный",
            }
        ],
        "sweep_lookback_days": 30,
    }
    mock_api = MagicMock(return_value=payload)
    args = argparse.Namespace(limit=50, json=False)
    out = StringIO()
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=out):
        rc = cli.cmd_undelivered(args)
    assert rc == 0
    text = out.getvalue()

    assert "#909" in text and "pda_claude" in text and "19ee3f6faf9f" in text, (
        "закрытая наблюдением строка обязана быть видна человеку с именем "
        "наблюдавшего и коммитом, иначе закрытие читается как «расхождений нет»"
    )
    assert "19ee3f6faf9f --stat" in text and "AC-тест зелёный" in text
    assert "unknown" in text and "репозиторий PR #468 удалён" in text, (
        "прежний ответ реестра — история, а не стёртое состояние"
    )
    assert "30" in text and "#878" in text, (
        "про строку за краем окна свипа сказано вслух: источники больше не "
        "перепрашиваются"
    )
    assert "архив" not in text.lower()
    # Сердце находки: пустой список открытых PR — это НЕ «проверено, чисто»,
    # пока рядом стоит строка, закрытая чужим наблюдением.
    assert "No completed task is waiting on an open PR." not in text, (
        "фраза «расхождений нет» при закрытой наблюдением строке — штамп: "
        "читатель принимает чужое наблюдение за собственную проверку хаба"
    )


def test_delivery_observe_posts_to_the_observation_endpoint() -> None:
    """Находка ревью Codex, сдача 2, ``hub/app.py:2008-2013``.

    ``undelivered`` советует замёрзшей unknown-строке «записать наблюдение»,
    но CLI не давал команды, которая отправила бы probe/observation/sha на
    ``POST .../observation`` — оператор с одним только CLI не мог
    воспользоваться названным выходом. ``delivery-observe`` — этот вход.
    """
    rc, api = _run_main(
        [
            "delivery-observe",
            "909",
            "--probe",
            "git show 19ee3f6faf9f --stat",
            "--observation",
            "файл на месте, AC-тест зелёный",
            "--sha",
            "19ee3f6faf9f",
        ],
        api_result={"task_id": 909, "closed_state": "unknown"},
    )
    assert rc == 0
    api.assert_called_once_with(
        "POST",
        "/api/delivery/discrepancies/909/observation",
        {
            "probe": "git show 19ee3f6faf9f --stat",
            "observation": "файл на месте, AC-тест зелёный",
            "sha": "19ee3f6faf9f",
        },
    )


def test_cmd_undelivered_frozen_pr_open_is_not_told_to_observe() -> None:
    """Находка ревью #1215, 7be892476f84f99c — та же проверка, что MCP.

    ``cmd_undelivered`` строил единый баннер ``frozen`` из ``(*rows,
    *unknown)`` без разбора состояния: замёрзшей строке с открытым PR
    предлагали «записать наблюдение», хотя эндпоинт отвечает 422
    ``source_still_answers`` — наблюдением закрывают только unknown.
    """
    payload = {
        "undelivered": [
            {
                "task_id": 461,
                "title": "Предпас",
                "pr_number": 461,
                "reason": "PR #461 открыт и не смержен",
                "age_hours": 800,
                "still_swept": False,
            }
        ],
        "unknown": [],
        "closed_by_observation": [],
        "sweep_lookback_days": 30,
    }
    mock_api = MagicMock(return_value=payload)
    args = argparse.Namespace(limit=50, json=False)
    out = StringIO()
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=out):
        rc = cli.cmd_undelivered(args)
    assert rc == 0
    text = out.getvalue()
    assert "#461" in text
    assert "Выход — записать наблюдение" not in text
    assert "источник ещё отвечает" in text


def test_cmd_undelivered_all_clear_stays_locked_when_only_observed_remains() -> None:
    """Находка ревью #1215, d9a42edd0c92224f: вырожденный тест не запирает
    конъюнкт ``not observed``.

    ``test_cmd_undelivered_names_observation_and_the_frozen_window`` выше
    держит ``unknown`` непустым РЯДОМ с ``closed_by_observation`` — то есть
    снятие именно ``and not observed`` из ``if not rows and not unknown and
    not observed:`` (строка вернулась бы к прежнему ``if not rows and not
    unknown:``) остаётся зелёным: ``unknown`` и без того запрещает печатать
    «всё чисто». Настоящая боевая форма после закрытия строки — та, что видна
    у #878/#875/#909: ``undelivered`` и ``unknown`` ОБА пусты, стоит только
    ``closed_by_observation``. Этот тест строит именно её.
    """
    payload = {
        "undelivered": [],
        "unknown": [],
        "closed_by_observation": [
            {
                "task_id": 909,
                "title": "Паспорт дефекта",
                "state": "unknown",
                "reason": "репозиторий PR #468 удалён, спросить некого",
                "observed_by": "pda_claude",
                "observed_sha": "19ee3f6faf9f",
                "observed_probe": "git show 19ee3f6faf9f --stat",
                "observed_evidence": "файл на месте, AC-тест зелёный",
            }
        ],
        "sweep_lookback_days": 30,
    }
    mock_api = MagicMock(return_value=payload)
    args = argparse.Namespace(limit=50, json=False)
    out = StringIO()
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=out):
        rc = cli.cmd_undelivered(args)
    assert rc == 0
    text = out.getvalue()

    assert "#909" in text and "pda_claude" in text
    # Сердце находки: undelivered и unknown оба пусты, но строка закрыта
    # чужим наблюдением, а не хабом — «всё чисто» здесь было бы штампом.
    assert "No completed task is waiting on an open PR." not in text, (
        "unknown пуст, но closed_by_observation — нет: печатать «расхождений "
        "нет» здесь значит выдавать чужое наблюдение за собственный вывод хаба"
    )


def test_cmd_undelivered_still_says_all_clear_when_it_really_is() -> None:
    """Обратная сторона той же находки: молчать, когда сказать нечего.

    Убрать ложное «чисто» легко ценой того, что честное «чисто» исчезнет
    вместе с ним, — и тогда пустой вывод снова придётся толковать. Все четыре
    ведра пусты ровно тогда, когда фраза правдива.
    """
    mock_api = MagicMock(
        return_value={
            "undelivered": [],
            "unknown": [],
            "closed_by_observation": [],
            "sweep_lookback_days": 30,
        }
    )
    args = argparse.Namespace(limit=50, json=False)
    out = StringIO()
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=out):
        rc = cli.cmd_undelivered(args)
    assert rc == 0
    assert "No completed task is waiting on an open PR." in out.getvalue()


def test_delivery_deliver_posts_to_the_registry_deliver_endpoint() -> None:
    """#1333: CLI-вход действия реестра «доставить» — рядом с undelivered."""
    rc, api = _run_main(
        ["delivery-deliver", "1276"],
        api_result={
            "task_id": 1276,
            "delivered": True,
            "state": "delivered",
            "pr_number": 425,
        },
    )
    assert rc == 0
    api.assert_called_once_with("POST", "/api/delivery/discrepancies/1276/deliver", {})


def test_review_economy_prints_the_section_of_practice_metrics(capsys) -> None:
    # #1406: CLI reads the same REST section the page and MCP read.
    econ = {"runs": {"total": 3, "billed": 2, "unbilled": 1}}
    rc, api = _run_main(
        ["review-economy", "--since-days", "30"],
        api_result={"since_days": 30, "review_economy": econ},
    )
    assert rc == 0
    assert api.call_args.args[:2] == ("GET", "/api/metrics/practices?since_days=30")
    assert json.loads(capsys.readouterr().out) == econ


def test_main_release_blocks(capsys) -> None:
    # #1420: `oc-hub release-blocks` reads the cheap route and prints the same
    # «Релиз заблокирован с …» line as prod-state and hub_my_context.
    blocks = {
        "release_blocks": [
            {
                "project": "default",
                "reason": "релизный PR #484 не смержен: ci_fail (checks_failed)",
                "since": "2026-09-25 10:43:47",
                "minutes": 99,
                "ci": {"run_url": "", "failed_checks": ["Ruff and pytest"]},
            }
        ]
    }
    rc, api = _run_main(["release-blocks"], api_result=blocks)
    assert rc == 0
    assert api.call_args.args[1] == "/api/release-blocks"
    out = capsys.readouterr().out
    assert "Релиз заблокирован с 2026-09-25 10:43:47 UTC (default, 99 мин)" in out
    assert "упали: Ruff and pytest" in out

    rc, _ = _run_main(["release-blocks"], api_result={"release_blocks": []})
    assert rc == 0
    assert "Открытых аварий релиза нет" in capsys.readouterr().out


async def test_admin_agents_create_with_watcher_role(client, db, monkeypatch):
    """AC-4 (#1556): --role watcher reaches the API; only a human admin can do it."""
    from hub import config
    from hub.services import admin as admin_svc

    # CLI side: the flag names the role in the request body, default stays agent.
    rc, api = _run_main(
        ["admin", "agents", "create", "--name", "w1", "--role", "watcher"],
        api_result={"id": 9},
    )
    assert rc == 0
    assert api.call_args.args[2]["role"] == "watcher"
    assert api.call_args.args[2]["kind"] == "agent"
    rc, api = _run_main(["admin", "agents", "create", "--name", "w2"], api_result={})
    assert rc == 0 and api.call_args.args[2]["role"] == "agent"
    rc, _ = _run_main(["admin", "agents", "create", "--name", "w3", "--role", "admin"])
    assert rc != 0  # a role that is not agent/watcher is not offered here

    # Server side: the same body, sent by a human admin and by an agent.
    monkeypatch.setattr(
        config, "HUB_TOKENS", {"unused-env-token": config.TokenIdentity("x", "human")}
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    admin = await admin_svc.create_principal(
        db, kind="human", username="boss", role_slug="super_admin"
    )
    admin_key = await admin_svc.create_api_key(db, admin["id"], name="k")
    agent = await admin_svc.create_principal(
        db, kind="agent", username="ordinary", role_slug="agent"
    )
    agent_key = await admin_svc.create_api_key(db, agent["id"], name="k")
    body = {"kind": "agent", "username": "w1", "display_name": "w1", "role": "watcher"}

    denied = await client.post(
        "/api/admin/principals",
        json=body,
        headers={"Authorization": f"Bearer {agent_key['plaintext_key']}"},
    )
    assert denied.status_code == 403, denied.text
    assert not await db.execute_fetchall(
        "SELECT 1 FROM principals WHERE username = 'w1'"
    )

    made = await client.post(
        "/api/admin/principals",
        json=body,
        headers={"Authorization": f"Bearer {admin_key['plaintext_key']}"},
    )
    assert made.status_code in (200, 201), made.text
    assert made.json()["roles"] == ["watcher"]
    role = await db.execute_fetchall(
        "SELECT r.system, GROUP_CONCAT(rp.permission) AS perms FROM roles r "
        "JOIN role_permissions rp ON rp.role_id = r.id WHERE r.slug = 'watcher'"
    )
    assert role[0]["system"] == 1 and role[0]["perms"] == "tasks.read"


# --- hp-hub worktree (#1515) -------------------------------------------------


def _sh(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _clone(tmp_path: Path, name: str, origin: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _sh(repo, "init", "-q", "-b", "develop")
    _sh(repo, "config", "user.email", "t@example.com")
    _sh(repo, "config", "user.name", "Test")
    (repo / "f.txt").write_text("x")
    _sh(repo, "add", ".")
    _sh(repo, "commit", "-q", "-m", "init")
    _sh(repo, "remote", "add", "origin", origin)
    return repo


def _bare_origin(tmp_path: Path, slug: str) -> str:
    """A local bare repo whose path ends in ``owner/name``: a no-network origin."""
    path = tmp_path / "origins" / f"{slug}.git"
    path.mkdir(parents=True)
    _sh(path, "init", "-q", "--bare")
    return str(path)


def _fake_hub(monkeypatch, *, repo_name: str = "agentdrover/Spike_bo") -> str:
    branch = "task-1429/spike-thing"

    def fake_api(method: str, path: str, body: Any = None, **kw: Any) -> Any:
        if path == "/api/tasks/1429":
            return {
                "id": 1429,
                "title": "Spike thing",
                "branch": branch,
                "project": {"id": 5, "slug": "spike"},
            }
        if path.startswith("/api/projects"):
            return [
                {
                    "id": 5,
                    "slug": "spike",
                    "repo": repo_name,
                    "default_branch": "develop",
                }
            ]
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(cli, "_api", fake_api)
    return branch


def _run_worktree(repo: Path) -> int:
    return cli.cmd_worktree(argparse.Namespace(task_id=1429, repo=str(repo)))


def test_worktree_command_creates_then_reuses_rule_path(
    tmp_path, monkeypatch, capsys
) -> None:
    # AC-2 (#1515): the path is built from the ACTUAL clone folder (spike_bo),
    # created on the canonical branch, and reused on the second call.
    branch = _fake_hub(monkeypatch)
    clone = _clone(tmp_path, "spike_bo", _bare_origin(tmp_path, "agentdrover/Spike_bo"))
    expected = tmp_path / ".spike_bo-worktrees" / "task-1429"

    assert _run_worktree(clone) == 0
    first = capsys.readouterr().out.strip()
    assert first == str(expected)
    assert _sh(expected, "branch", "--show-current") == branch

    assert _run_worktree(clone) == 0
    assert capsys.readouterr().out.strip() == first
    assert _sh(expected, "branch", "--show-current") == branch


def test_worktree_command_refuses_dirty_foreign_or_wrong_repo(
    tmp_path, monkeypatch, capsys
) -> None:
    # AC-3 (#1515): refusal names the reason and touches nothing.
    branch = _fake_hub(monkeypatch)
    clone = _clone(tmp_path, "spike_bo", "git@github.com:AgentDrover/spike_bo.git")
    wt = tmp_path / ".spike_bo-worktrees" / "task-1429"
    wt.parent.mkdir()

    # foreign branch, clean: refused, branch unchanged
    _sh(clone, "worktree", "add", "-q", "-b", "other/work", str(wt), "develop")
    assert _run_worktree(clone) == 1
    err = capsys.readouterr().err
    assert "other/work" in err and branch in err
    assert _sh(wt, "branch", "--show-current") == "other/work"

    # foreign branch with uncommitted edits: refused, files intact
    (wt / "f.txt").write_text("edited")
    (wt / "new.txt").write_text("untracked")
    assert _run_worktree(clone) == 1
    err = capsys.readouterr().err
    assert "f.txt" in err
    assert _sh(wt, "branch", "--show-current") == "other/work"
    assert (wt / "f.txt").read_text() == "edited"
    assert (wt / "new.txt").read_text() == "untracked"

    # origin that is not the project repo: refused, no copy created
    other = _clone(tmp_path, "elsewhere", "https://github.com/someone/else.git")
    assert _run_worktree(other) == 1
    err = capsys.readouterr().err
    assert "someone/else" in err and "agentdrover/spike_bo" in err.lower()
    assert not (tmp_path / ".elsewhere-worktrees").exists()


def test_worktree_command_refuses_unregistered_path_missing_repo_and_non_clone(
    tmp_path, monkeypatch, capsys
) -> None:
    # #1515: other refusals leave the disk as they found it.
    _fake_hub(monkeypatch)
    clone = _clone(tmp_path, "spike_bo", "https://github.com/agentdrover/Spike_bo")
    squatter = tmp_path / ".spike_bo-worktrees" / "task-1429"
    squatter.mkdir(parents=True)
    (squatter / "mine.txt").write_text("keep")
    assert _run_worktree(clone) == 1
    assert "не копия клона" in capsys.readouterr().err
    assert (squatter / "mine.txt").read_text() == "keep"

    # project without a repo: nothing to compare the origin with
    _fake_hub(monkeypatch, repo_name="")
    assert _run_worktree(clone) == 1
    assert "не задан repo" in capsys.readouterr().err

    # clone without origin
    _fake_hub(monkeypatch)
    bare = _clone(tmp_path, "noorigin", "https://github.com/x/y")
    _sh(bare, "remote", "remove", "origin")
    assert _run_worktree(bare) == 1
    assert "нет remote origin" in capsys.readouterr().err

    # not a git clone at all
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _run_worktree(plain) == 1
    assert "не git-клон" in capsys.readouterr().err
    assert _run_worktree(tmp_path / "missing") == 1
    assert "не git-клон" in capsys.readouterr().err
    assert not (tmp_path / ".noorigin-worktrees").exists()


def test_worktree_command_from_inside_a_worktree_uses_the_main_clone(
    tmp_path, monkeypatch, capsys
) -> None:
    # #1515: asked from inside a copy, the rule path is still beside the clone.
    _fake_hub(monkeypatch)
    clone = _clone(tmp_path, "spike_bo", _bare_origin(tmp_path, "agentdrover/Spike_bo"))
    assert _run_worktree(clone) == 0
    path = capsys.readouterr().out.strip()
    assert _run_worktree(Path(path)) == 0
    assert capsys.readouterr().out.strip() == path


def test_worktree_command_takes_the_task_branch_from_origin(
    tmp_path, monkeypatch, capsys
) -> None:
    # #1515: a task branch that exists only on origin is checked out with its
    # commits (tracking it), not recreated empty from the base.
    branch = _fake_hub(monkeypatch)
    origin = _bare_origin(tmp_path, "agentdrover/Spike_bo")
    clone = _clone(tmp_path, "spike_bo", origin)
    _sh(clone, "push", "-q", "origin", "develop")
    other = tmp_path / "other_clone"
    _sh(tmp_path, "clone", "-q", origin, str(other))
    _sh(other, "config", "user.email", "t@example.com")
    _sh(other, "config", "user.name", "Test")
    _sh(other, "checkout", "-q", "-b", branch)
    (other / "work.txt").write_text("pushed work")
    _sh(other, "add", ".")
    _sh(other, "commit", "-q", "-m", "task work")
    _sh(other, "push", "-q", "origin", branch)
    pushed = _sh(other, "rev-parse", "HEAD")

    assert _run_worktree(clone) == 0
    wt = Path(capsys.readouterr().out.strip())
    assert _sh(wt, "branch", "--show-current") == branch
    assert _sh(wt, "rev-parse", "HEAD") == pushed
    assert (wt / "work.txt").read_text() == "pushed work"
    assert _sh(wt, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}") == (
        f"origin/{branch}"
    )


def test_worktree_command_names_a_failed_fetch_and_still_creates(
    tmp_path, monkeypatch, capsys
) -> None:
    # #1515: a network failure is a warning, not a refusal.
    branch = _fake_hub(monkeypatch)
    clone = _clone(
        tmp_path, "spike_bo", str(tmp_path / "gone" / "agentdrover" / "Spike_bo")
    )
    assert _run_worktree(clone) == 0
    captured = capsys.readouterr()
    assert f"предупреждение: fetch origin {branch} не удался" in captured.err
    assert "предупреждение: fetch origin develop не удался" in captured.err
    assert _sh(Path(captured.out.strip()), "branch", "--show-current") == branch


def _fetch_failing_git(monkeypatch, stderr: str) -> None:
    """Make every ``git fetch`` of a worktree request fail with ``stderr``."""
    from hub.integrations import git_ops

    real = git_ops._git

    async def fake(*args: str, **kw: Any):
        if args and args[0] == "fetch":
            return 128, "", stderr
        return await real(*args, **kw)

    monkeypatch.setattr(git_ops, "_git", fake)


def test_worktree_absent_branch_gives_no_note_under_a_foreign_locale(
    tmp_path, monkeypatch, capsys
) -> None:
    # #1515: "branch absent on origin" is asked of ls-remote, not read from the
    # text of git's stderr, which is localised. A fetch of the branch must not
    # even be attempted: the only fetch that may fail here is the base's.
    branch = _fake_hub(monkeypatch)
    origin = _bare_origin(tmp_path, "agentdrover/Spike_bo")
    clone = _clone(tmp_path, "spike_bo", origin)
    _sh(clone, "push", "-q", "origin", "develop")
    _fetch_failing_git(monkeypatch, "fatal: не удалось найти удалённую ссылку")
    assert _run_worktree(clone) == 0
    captured = capsys.readouterr()
    assert f"fetch origin {branch}" not in captured.err
    assert "предупреждение" in captured.err  # the base fetch failure only
    assert "fetch origin develop" in captured.err


def test_worktree_real_fetch_failure_of_an_existing_branch_is_named(
    tmp_path, monkeypatch, capsys
) -> None:
    branch = _fake_hub(monkeypatch)
    origin = _bare_origin(tmp_path, "agentdrover/Spike_bo")
    clone = _clone(tmp_path, "spike_bo", origin)
    _sh(clone, "push", "-q", "origin", f"develop:refs/heads/{branch}")
    _fetch_failing_git(monkeypatch, "fatal: boom")
    assert _run_worktree(clone) == 0
    assert f"предупреждение: fetch origin {branch} не удался" in (
        capsys.readouterr().err
    )


def test_create_commands_carry_work_type_and_freeze_rationale(capsys) -> None:
    # #1594: тип работы и обоснование допуска доходят до запроса из task и из
    # типизированных команд; не заданные в тело не попадают (прежнее поведение).
    parser = cli.build_parser()
    for argv, task_type in (
        (["task", "--title", "t"], "task"),
        (["epic", "--title", "t"], "epic"),
        (["feature", "--title", "t", "--parent", "3"], "feature"),
        (["subtask", "--title", "t", "--parent", "3"], "subtask"),
    ):
        plain = MagicMock(return_value={"id": 1})
        with patch.object(cli, "_api", plain), patch("sys.stdout", new=StringIO()):
            args = parser.parse_args(argv)
            assert args.func(args) == 0
        body = plain.call_args.args[2]
        assert "work_type" not in body and "freeze_rationale" not in body, task_type

        framed = MagicMock(return_value={"id": 1})
        with patch.object(cli, "_api", framed), patch("sys.stdout", new=StringIO()):
            args = parser.parse_args(
                [*argv, "--work-type", "bug", "--freeze-rationale", "сбой в проде"]
            )
            assert args.func(args) == 0
        body = framed.call_args.args[2]
        assert body["work_type"] == "bug", task_type
        assert body["freeze_rationale"] == "сбой в проде", task_type

    refine = parser.parse_args(["refine", "9", "--freeze-rationale", "why"])
    assert cli._build_refine_payload(refine)["freeze_rationale"] == "why"


def test_freeze_refusal_is_printed_as_text(capsys) -> None:
    detail = {"error": "freeze_refused", "message": "Заморозка проекта до снятия"}
    cli._print_http_error(422, json.dumps({"detail": detail}))
    assert "HTTP 422: Заморозка проекта до снятия" in capsys.readouterr().err


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


def test_outcome_debt_passes_page_flags() -> None:
    """AC-6. Flags become query parameters; no flags, no query."""
    rc, api = _run_main(["outcome-debt"], api_result={})
    assert rc == 0
    assert api.call_args.args[:2] == ("GET", "/api/metrics/outcome-debt")

    rc, api = _run_main(
        [
            "outcome-debt",
            "--status",
            "overdue",
            "--limit",
            "5",
            "--offset",
            "10",
            "--only-counts",
        ],
        api_result={},
    )
    assert rc == 0
    path = api.call_args.args[1]
    base, _, query = path.partition("?")
    assert base == "/api/metrics/outcome-debt"
    assert sorted(query.split("&")) == [
        "limit=5",
        "offset=10",
        "only_counts=true",
        "status=overdue",
    ]
