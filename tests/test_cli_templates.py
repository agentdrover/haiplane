"""CLI: шаблоны work-type и загрузка payload-файлов."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hub import cli

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


def test_cmd_template_list_when_none_installed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_list_templates", lambda: [])
    args = argparse.Namespace(work_type="list", out=None, force=False, list=False)
    rc = cli.cmd_template(args)
    assert rc == 1
    assert "No templates installed." in capsys.readouterr().err
