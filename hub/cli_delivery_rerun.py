"""CLI: история автоперезапуска CI на свежей базе одной строкой (#1956).

Объект ``delivery_rerun`` собрал хаб (``services/delivery_rerun.card_view``);
CLI его не пересчитывает, а печатает строку в stderr рядом с JSON карточки.
"""

from __future__ import annotations

import sys
from typing import Any


def print_line(result: Any) -> None:
    view = result.get("delivery_rerun") if isinstance(result, dict) else None
    if not isinstance(view, dict) or not view.get("state"):
        return
    outcome = f" / {view['outcome']}" if view.get("outcome") else ""
    jobs = view.get("failed_jobs")
    named = f"; упали: {', '.join(jobs)}" if jobs else ""
    print(
        f"Перезапуск CI: {view['state']}{outcome}; run {view.get('run_id')}, "
        f"после попытки {view.get('attempt_before')}{named}",
        file=sys.stderr,
    )
