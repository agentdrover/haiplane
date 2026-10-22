"""GitHub: перезапуск упавших job прогона и имена job попытки (#1956).

Вынесено из ``github.py`` (потолок размера файла). Функции получают то, что
нужно от форжа, параметром: запуск ``gh`` и постраничное чтение.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from hub.integrations.protocols import CIRunRequestOutcome, CIRunRequestResult

#: Платный вызов, но ответ нужен быстро: он не держит проход доставки.
RERUN_TIMEOUT = 30
#: Ответы GitHub «нельзя»: нет права actions:write у токена гейта, прогон не
#: перезапускаем (идёт, старше срока, пересоздан) или его нет. Это решение
#: форжа, повтор его не изменит.
_REFUSED = ("http 403", "http 404", "http 409", "http 422")

Gh = Callable[..., Awaitable[tuple[int, str, str]]]
Pages = Callable[..., Awaitable[list[Any] | None]]


async def rerun_failed_jobs(
    gh: Gh, run_id: str, *, repo: str | None, gh_repo: str
) -> CIRunRequestResult:
    """POST repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs.

    Исходы три: ``requested`` (201), ``declined`` (форж сказал «нельзя») и
    ``unavailable`` (вызов не дошёл или ответ неизвестен: таймаут, сеть, 5xx).
    Повторять вызов нельзя ни в одном случае: после ``unavailable`` запрос мог
    дойти, и хаб закрывает бронь, а не спрашивает ещё раз. Классификации job
    нет: перезапускаются все упавшие.
    """
    if not str(run_id).isdigit():
        return CIRunRequestResult(CIRunRequestOutcome.declined, "run_id_invalid")
    rc, _out, err = await gh(
        "api",
        "--method",
        "POST",
        f"repos/{gh_repo}/actions/runs/{run_id}/rerun-failed-jobs",
        repo=repo,
        check=False,
        timeout=RERUN_TIMEOUT,
    )
    if rc == 0:
        return CIRunRequestResult(CIRunRequestOutcome.requested, "rerun_requested")
    detail = (err or "").strip()[:200] or None
    if any(code in (detail or "").lower() for code in _REFUSED):
        return CIRunRequestResult(
            CIRunRequestOutcome.declined, "rerun_refused", details=detail
        )
    return CIRunRequestResult(
        CIRunRequestOutcome.unavailable, "rerun_call_failed", details=detail
    )


async def attempt_jobs(
    pages: Pages, run_id: str, attempt: str, *, repo: str | None, gh_repo: str
) -> list[dict[str, Any]] | None:
    """``[{"name", "conclusion", "failed_steps"}]``; ``[]`` - job нет; None - не прочиталось.

    Список длиннее прочитанного тоже None: неполный список выдал бы неполную
    инструкцию.
    """
    if not (str(run_id).isdigit() and str(attempt).isdigit()):
        return None
    jobs = await pages(
        f"repos/{gh_repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
        "jobs",
        repo=repo,
    )
    if jobs is None:
        return None
    found: list[dict[str, Any]] = []
    for job in jobs:
        steps = job.get("steps") if isinstance(job.get("steps"), list) else []
        found.append(
            {
                "name": str(job.get("name") or ""),
                "conclusion": str(job.get("conclusion") or "").lower(),
                "failed_steps": [
                    str(st.get("name") or "")
                    for st in steps
                    if isinstance(st, dict)
                    and str(st.get("conclusion") or "").lower() == "failure"
                ],
            }
        )
    return found
