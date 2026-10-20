"""Чтение истории базовой ветки для предохранителя стюарда (#1865).

Две функции, обе принимают исполнитель git ``run(*args) -> (rc, out, err)``
(в git_ops это ``_git`` с зафиксированным репозиторием), поэтому модуль не
знает ни о репозитории, ни о подпроцессах. Ответ ``None`` значит «спросить не
удалось» - это не то же самое, что «коммитов нет» (#725).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

GitRun = Callable[..., Awaitable[tuple[int, str, str]]]

_REC = "\x1e"
_FLD = "\x1f"
# merge-base --is-ancestor: 1 - не предок, 128 - объекта нет в клоне. И то и
# другое значит, что история под курсором переписана или недоступна.
_NOT_ANCESTOR = (1, 128)


async def first_parent_after(
    run: GitRun, base: str, since: str, limit: int
) -> dict[str, Any] | None:
    """Коммиты первой линии ``origin/<base>`` после ``since``, СТАРЫЕ первыми.

    ``{"head": sha, "reset": bool, "shas": [...]}``. ``reset`` - ``since`` не
    предок головы (историю переписали): вызывающий не судит ничего и ставит
    курсор на ``head``. Берутся самые старые ``limit`` коммитов: курсор
    переходит на последний разобранный, остальное - на следующем тике.
    """
    rc, head, _ = await run("rev-parse", "--verify", f"origin/{base}^{{commit}}")
    if rc != 0 or not head.strip():
        return None
    head = head.strip()
    if since:
        rc, _, _ = await run("merge-base", "--is-ancestor", since, head)
        if rc in _NOT_ANCESTOR:
            return {"head": head, "reset": True, "shas": []}
        if rc != 0:
            return None
    rc, out, _ = await run(
        "rev-list", "--first-parent", "--reverse", f"{since}..{head}" if since else head
    )
    if rc != 0:
        return None
    return {"head": head, "reset": False, "shas": out.split()[: max(1, int(limit))]}


async def commit_facts(
    run: GitRun, shas: list[str]
) -> dict[str, dict[str, Any]] | None:
    """``{sha: {"parents": [...], "message": полный текст}}``; None - не прочли.

    Полное сообщение (``%B``), а не тема: строка «This reverts commit» живёт в
    теле, и ``first_parent_log`` её не отдаёт.
    """
    if not shas:
        return {}
    rc, out, _ = await run(
        "log", "--no-walk=unsorted", f"--format=%H{_FLD}%P{_FLD}%B{_REC}", *shas, "--"
    )
    if rc != 0:
        return None
    facts: dict[str, dict[str, Any]] = {}
    for record in out.split(_REC):
        parts = record.strip("\n").split(_FLD, 2)
        if len(parts) == 3 and parts[0].strip():
            facts[parts[0].strip()] = {
                "parents": parts[1].split(),
                "message": parts[2],
            }
    return facts
