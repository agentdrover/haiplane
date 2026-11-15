"""``oc-hub spec reindex <project_id>`` (#2223).

Вынесено из ``hub/cli.py``: у того бюджет размера (#1669). Команда одна и та же,
что у REST и MCP: ``POST /api/projects/{id}/spec/reindex``. Отказ (файл не
разобран, дубль id) печатает ответ хаба с именами файлов и выходит с кодом 1 в
``hub.cli._api``; прежний индекс при этом цел.
"""

from __future__ import annotations

import argparse
from typing import Any


def _api(*args: Any, **kwargs: Any) -> Any:
    # Лениво: hub.cli собирает парсеры при импорте и импортирует этот модуль.
    from hub.cli import _api as api

    return api(*args, **kwargs)


def cmd_spec_reindex(args: argparse.Namespace) -> int:
    """Перечитать спецификацию проекта по ветке по умолчанию и показать итог."""
    result = _api("POST", f"/api/projects/{int(args.project_id)}/spec/reindex")
    print(
        f"Индекс спецификации: {result['commit'][:12]} ({result['ref']}), "
        f"файлов {result['files']}, требований {result['requirements']}"
    )
    for item in result.get("skipped") or []:
        print(f"  пропущен: {item['file']}: {item['reason']}")
    return 0


def add_parsers(sub: Any) -> None:
    parser = sub.add_parser(
        "spec",
        help="Behaviour spec index (#2223): oc-hub spec reindex <project_id>",
    )
    inner = parser.add_subparsers(dest="spec_cmd", required=True)
    reindex = inner.add_parser(
        "reindex",
        help="Re-read specs/ from the project's default-branch ref into the index",
    )
    reindex.add_argument("project_id", type=int, help="Project id (hub_list_projects)")
    reindex.set_defaults(func=cmd_spec_reindex)
