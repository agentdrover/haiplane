"""Пустой машинный отчёт и самостоятельный approve стюарда (#2082).

Отчёт без единого кандидата - «нет данных», а не «нет находок». Решение по нему
одно на автопилот и стюарда: ``auto_verdict.empty_report_decision``, чистая
функция. Здесь только то, чего автопилоту не нужно:

* ДОБЫЧА доказательства биллинга (сеть) - до транзакции записи вердикта, один
  раз на применение; доказательство привязано к задаче, поколению и id отчёта;
* ЧИСТАЯ перепроверка внутри транзакции: актуален ли тот же отчёт и привязано
  ли доказательство именно к нему. Сети там нет;
* чтение без сети для readiness и маршрута вердикта: непроверенный биллинг
  называется условием, а не выдаётся за доказанный.

Правило ОДНО на все режимы, включая двойную чистоту (#1986): признаки исполнения
(пин, число агентов) работу ревьюера не доказывают. Отчёты пары без кандидатов
режим отвергает сам (``steward_double_clean._unclean``), а последний отчёт
сдачи проходит это правило, как любой другой.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.services import auto_verdict
from hub.services.auto_verdict import EmptyReportDecision
from hub.services.refusal_reason import REASON_TEXTS, CodedRefusal

log = logging.getLogger(__name__)

CODE_NO_DATA = auto_verdict.CODE_NO_DATA
CODE_ABOVE_CEILING = auto_verdict.CODE_ABOVE_CEILING


@dataclass(frozen=True)
class EmptyProof:
    """Что спросили у биллинга про ОДИН отчёт одной сдачи; ``usage`` None - не доказано."""

    task_id: int
    generation: int
    report_id: int
    usage: int | None


@dataclass
class _Scope:
    """Область одного применения: принадлежит Task, которая её открыла."""

    owner: asyncio.Task[Any] | None
    proofs: dict[int, EmptyProof]


_SCOPE: ContextVar[_Scope | None] = ContextVar("steward_empty_scope", default=None)


def _own() -> dict[int, EmptyProof] | None:
    """Доказательства ЭТОГО применения; дочерняя Task унаследованную область не видит."""
    scope = _SCOPE.get()
    if scope is None or scope.owner is not asyncio.current_task():
        return None
    return scope.proofs


@asynccontextmanager
async def proof_scope() -> AsyncIterator[None]:
    """Одно применение стюарда: доказательство не переживает его и Task его открывшей.

    Вложенный вход той же Task делит область внешнего: ранняя проверка и
    применение одного суждения спрашивают провайдера один раз. Контекст
    копируется в дочернюю Task вместе с областью, но владелец у неё другой - она
    открывает свою и доказательство заново.
    """
    if _own() is not None:
        yield
        return
    token = _SCOPE.set(_Scope(asyncio.current_task(), {}))
    try:
        yield
    finally:
        _SCOPE.reset(token)


async def _scene(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(задача, пустой отчёт ЭТОЙ сдачи) или None, если пустого отчёта нет."""
    row = await repo.get_latest_machine_review(db, task_id)
    if row is None:
        return None
    review = dict(row)
    if (review.get("submission_generation") or 0) != generation:
        return None
    if (review.get("raw_count") or 0) >= 1:
        return None
    task = dict(await repo.get_task(db, task_id) or {"id": task_id})
    return task, review


def _bound_usage(proof: EmptyProof | None, review: dict[str, Any]) -> int | None:
    """Объём работы из доказательства, только если оно о ЭТОМ отчёте."""
    if proof is None:
        return None
    same = (
        proof.task_id == review.get("task_id")
        and proof.generation == (review.get("submission_generation") or 0)
        and proof.report_id == review.get("id")
    )
    return proof.usage if same else None


def refusal_of(
    decision: EmptyReportDecision, task: dict[str, Any]
) -> tuple[str, str] | None:
    """Отказ привратника по решению: фиксированный текст кода и класс, без чужих слов."""
    if not decision.code:
        return None
    return (
        decision.code,
        f"{REASON_TEXTS.get(decision.code, decision.code)} (raw_count=0, класс сдачи "
        f"{task.get('risk_class') or 'не вычислен'})",
    )


async def _run_is_of_this_report(
    db: aiosqlite.Connection, task_id: int, generation: int, review: dict[str, Any]
) -> bool:
    """Завершённый запуск, чей биллинг будет спрошен, породил ИМЕННО этот отчёт.

    Предикат автопилота берёт последний завершённый запуск поколения; для
    доказательства стюарда этого мало: биллинг запуска A не доказывает работу,
    результатом которой стал отчёт B. Нет своего запуска - нет доказательства.
    """
    from hub.services.review_dispatch import dispatch_for_report

    owner = await dispatch_for_report(db, task_id, generation, review)
    settled = await repo.get_settled_review_dispatch(db, task_id, generation)
    return (
        owner is not None
        and settled is not None
        and int(owner["id"]) == int(settled["id"])
    )


async def _prove(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    review: dict[str, Any],
) -> EmptyProof:
    """Доказательство биллинга для этого отчёта: из области применения или от провайдера.

    Сеть, поэтому звать ТОЛЬКО до транзакции записи. Сбой провайдера - «не
    доказано» (отсутствие данных), а не исключение и не «доказано».
    """
    known = _own()
    cached = (known or {}).get(task_id)
    if (
        cached is not None
        and cached.report_id == review.get("id")
        and cached.generation == generation
    ):
        return cached
    usage = None
    if await _run_is_of_this_report(db, task_id, generation, review):
        try:
            usage = await auto_verdict._proven_empty_usage(
                db, task_id, generation, review
            )
        except Exception as exc:  # noqa: BLE001 - провайдер недоступен: не доказано
            log.warning("empty-report billing proof failed for #%s: %s", task_id, exc)
    proof = EmptyProof(task_id, generation, int(review["id"]), usage)
    if known is not None:
        known[task_id] = proof
    return proof


async def early_refusal(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> tuple[str, str] | None:
    """Ранняя проверка: пустой отчёт без доказанной работы. Сеть допустима - до транзакции."""
    scene = await _scene(db, task_id, generation)
    if scene is None:
        return None
    task, review = scene
    proof = await _prove(db, task_id, generation, review)
    decision = auto_verdict.empty_report_decision(
        task, review, _bound_usage(proof, review), checked=True
    )
    return refusal_of(decision, task)


async def require(db: aiosqlite.Connection, task_id: int, generation: int) -> None:
    """Прямое применение: отказ ``CodedRefusal``, если отчёт пуст и работа не доказана."""
    refused = await early_refusal(db, task_id, generation)
    if refused is not None:
        raise CodedRefusal(409, f"{refused[0]}: {refused[1]}", code=refused[0])


async def write_refusal(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> tuple[str, str] | None:
    """Перепроверка в транзакции записи: ЧИСТОЕ чтение, сети нет.

    Актуальный отчёт читается заново; доказательство засчитывается, только если
    оно привязано к его id. Другой пустой отчёт, пришедший после ранней
    проверки, доказательства не имеет.
    """
    scene = await _scene(db, task_id, generation)
    if scene is None:
        return None
    task, review = scene
    proof = (_own() or {}).get(task_id)
    decision = auto_verdict.empty_report_decision(
        task, review, _bound_usage(proof, review), checked=True
    )
    return refusal_of(decision, task)


async def stored_reading(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> tuple[EmptyReportDecision, dict[str, Any]] | None:
    """Чтение без сети для readiness и маршрута: решение «биллинг не проверен».

    ``None`` - отчёта без кандидатов у этой сдачи нет. Непустое ``code`` -
    известный запрет (класс выше потолка); пустое с ``pending`` - условие.
    """
    scene = await _scene(db, task_id, generation)
    if scene is None:
        return None
    task, review = scene
    return auto_verdict.empty_report_decision(task, review, None, checked=False), task
