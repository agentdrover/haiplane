"""Чтение истории базовой ветки для предохранителя стюарда (#1865).

Две функции, обе принимают исполнитель git ``run(*args) -> (rc, out, err)``
(в git_ops это ``_git`` с зафиксированным репозиторием), поэтому модуль не
знает ни о репозитории, ни о подпроцессах. Ответ ``None`` значит «спросить не
удалось» - это не то же самое, что «коммитов нет» (#725).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

GitRun = Callable[..., Awaitable[tuple[int, str, str]]]

_REC = "\x1e"
_FLD = "\x1f"
# merge-base --is-ancestor: 1 - не предок, 128 - объекта нет в клоне. И то и
# другое значит, что история под курсором переписана или недоступна.
_NOT_ANCESTOR = (1, 128)
#: Строка отката в теле коммита; её переносит squash и ищет предохранитель.
REVERT_LINE = re.compile(r"^This reverts commit [0-9a-f]{7,40}.*$", re.MULTILINE)
#: Коммитов второго родителя merge-коммита, которые читаются за раз.
SECOND_PARENT_LIMIT = 100


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


async def revert_lines(run: GitRun, rng: str) -> str:
    """Строки «This reverts commit …» из тел коммитов диапазона (пусто - нет/не прочли).

    Для squash: сжатый коммит получает тему «feat(task): slug (#id)», и без
    переноса строка отката терялась бы (#1865).
    """
    rc, out, _ = await run("log", "--format=%B", rng)
    if rc != 0:
        return ""
    return "\n".join(dict.fromkeys(REVERT_LINE.findall(out)))


async def _second_parent_message(run: GitRun, parents: list[str]) -> str | None:
    """Тела коммитов второго родителя merge-коммита (``merge-base..parent2``).

    Откат, влитый через PR merge-коммитом, лежит на втором родителе, а не в
    первой линии. Не больше ``SECOND_PARENT_LIMIT`` коммитов; None - не прочли.
    """
    rc, base, _ = await run("merge-base", parents[0], parents[1])
    if rc != 0 or not base.strip():
        return None
    rc, out, _ = await run(
        "log",
        f"-n{SECOND_PARENT_LIMIT}",
        "--format=%B",
        f"{base.strip()}..{parents[1]}",
    )
    return out if rc == 0 else None


async def commit_facts(
    run: GitRun, shas: list[str]
) -> dict[str, dict[str, Any]] | None:
    """``{sha: {"parents", "message", "second_parent_message"}}``; None - не прочли.

    Полное сообщение (``%B``), а не тема: строка «This reverts commit» живёт в
    теле, и ``first_parent_log`` её не отдаёт. Для merge-коммита первой линии
    ``second_parent_message`` несёт тела его ветки (иначе пусто).
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
            parents = parts[1].split()
            second = ""
            if len(parents) > 1:
                got = await _second_parent_message(run, parents)
                if got is None:
                    return None
                second = got
            facts[parts[0].strip()] = {
                "parents": parents,
                "message": parts[2],
                "second_parent_message": second,
            }
    return facts
