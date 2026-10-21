"""Сверка заявленного expected_sha с головой ветки, которую хаб наблюдал (#1937)."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from hub import repository as repo
from hub.actionable_errors import head_moved_detail, head_unverified_detail
from hub.db import write_transaction
from hub.expected_sha import normalize_expected_sha


def refuse_for_state_task(body: Any) -> None:
    """У задачи-состояния нет коммита: молчаливое игнорирование выдало бы
    несверенную сдачу за сверенную."""
    if body.expected_sha:
        raise HTTPException(
            422,
            detail="expected_sha — поле только для commit-задач: у задачи-состояния нет коммита",
        )


def head_refusal(state: Any) -> tuple[HTTPException, str] | None:
    """Отказ по expected_sha и текст alert к нему — вычисление без записи.

    ``None`` — заявления нет или голова совпала. Два разных отказа:
    ``head_moved`` — голова наблюдена и другая; ``head_unverified`` — наблюдения
    нет или оно не sha. Заявление не закрепляется никогда: сравнивается с
    наблюдением (``state.submission_sha`` от ``resolve_branch_tip``).
    """
    expected = state.body.expected_sha
    if not expected:
        return None
    observed = state.submission_sha
    try:
        normalized = normalize_expected_sha(observed)
    except ValueError:
        normalized = None
    if normalized == expected:
        return None
    if normalized is None:
        cause = state.sha_reason or "наблюдение головы не вернуло sha коммита"
        detail = head_unverified_detail(
            task_id=state.task_id,
            expected_sha=expected,
            observed_sha=observed,
            cause=cause,
        )
        text = (
            f"Сдача отказана (head_unverified): заявлен {expected}, голову ветки "
            f"хаб не наблюдал ({cause}); наблюдено: {observed or '—'}."
        )
    else:
        detail = head_moved_detail(
            task_id=state.task_id, expected_sha=expected, observed_sha=normalized
        )
        text = (
            f"Сдача отказана (head_moved): заявлен {expected}, голова ветки у "
            f"хаба {normalized}."
        )
    return HTTPException(409, detail=detail), text


async def refuse_unless_head_is_expected(state: Any) -> None:
    """409, если голова ветки не та, что заявил клиент.

    Единственная запись — alert с обоими sha в своей короткой транзакции.
    Пробная сдача (``state.dry_run``, #1975) отказывает тем же 409, но alert не
    пишет. Headless-путь тело не передаёт — сверки там нет.
    """
    refusal = head_refusal(state)
    if refusal is None:
        return
    error, text = refusal
    if not getattr(state, "dry_run", False):
        async with write_transaction(state.db):
            await repo.add_task_update(state.db, state.task_id, "hub", "alert", text)
    raise error
