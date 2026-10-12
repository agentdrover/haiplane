"""CLI: команды задачи (list/create/start/update/show/tree), dep и dispatch main()."""

from __future__ import annotations

import argparse
import json
from io import StringIO
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hub import cli
from tests.cli_support import _assert_no_traceback, _run_main

# Выдуманный sha в ответе prod-state: detect-secrets читает hex-строку как
# высокоэнтропийный секрет, поэтому значение живёт одной именованной константой
# с одной пометкой, а не пометкой на каждой строке, где оно встречается.
DEPLOYED_SHA = "abc123def456"  # pragma: allowlist secret


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
