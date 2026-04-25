from __future__ import annotations

import argparse
import json
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hub import cli


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


def test_cmd_refine_with_owner_and_reviewer() -> None:
    args = _refine_args(human_owner="alice", human_reviewer="bob")
    mock_api = MagicMock(return_value={})
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_refine(args)
    assert rc == 0
    payload = mock_api.call_args.args[2]
    assert payload["human_owner"] == "alice"
    assert payload["human_reviewer"] == "bob"


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
