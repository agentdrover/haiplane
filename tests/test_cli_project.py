"""hp-hub list и proposals называют проект каждой задачи (#1672)."""

from __future__ import annotations

import argparse
from io import StringIO
from unittest.mock import MagicMock, patch

from hub import cli


def _task(task_id: int, title: str, slug: str) -> dict:
    return {
        "id": task_id,
        "title": title,
        "status": "draft",
        "task_type": "task",
        "runtime": "auto",
        "source": "agent",
        "project": {"id": task_id, "slug": slug},
    }


def test_list_prints_the_project_of_each_task() -> None:
    """AC-6 (#1672): строка каждой задачи в list и proposals содержит slug."""
    tasks = [_task(1, "Alpha", "default"), _task(2, "Beta", "it-grade-dashboard")]
    args = argparse.Namespace(limit=10, status="draft", type=None, parent=None)

    for command in (cli.cmd_list, cli.cmd_proposals):
        with (
            patch.object(cli, "_api", MagicMock(return_value=tasks)),
            patch("sys.stdout", new=StringIO()) as out,
        ):
            assert command(args) == 0

        lines = out.getvalue().splitlines()
        assert len(lines) == 2, command.__name__
        assert "[default]" in lines[0], (command.__name__, lines[0])
        assert "[it-grade-dashboard]" in lines[1], (command.__name__, lines[1])
