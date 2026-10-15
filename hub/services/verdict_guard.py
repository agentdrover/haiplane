"""Стоящий вердикт commit-задачи не заменяется молча (#1706).

15.10.2026 по #1699 (поколение 1) CHANGES_REQUESTED в 14:19 и устаревший
APPROVED на то же поколение в 14:22: хаб принял второй, гейт влил PR. Запись
вердикта перезаписывала стоящий без условия, а ``expected_generation`` из тела
до неё на commit-пути не доходил.

Правило одно, для ВСЕХ писателей (REST, CLI, MCP, web, стюард, автопилот):

* вердикта на живое поколение ещё нет — первая запись;
* стоит тот же по существу (вид, находки, комментарий) — повтор: 200 без
  единой записи (кроме автоматики: ей разрешена только первая запись);
* стоит другой — 409 с названной ревизией; заменить его может только
  аутентифицированный человек, назвавший ``supersedes_revision`` = текущей;
* окно одобрения, закрытое решением человека (#1286), заменой не открывается.

Здесь стоят проверки ДО записи (дешёвый и понятный отказ, повтор без черновиков
follow-up). Решает атомарность условие самого UPDATE
(``hub.repository_verdict``): между этой проверкой и записью может вклиниться
другой писатель, и тогда запись откажет сама.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastapi import HTTPException

from hub import repository as repo
from hub.mcp_envelope import enrich_error_payload
from hub.models import TaskReviewVerdict


class Outcome(Enum):
    first = "first"  # на живое поколение вердикта нет
    repeat = "repeat"  # тот же запрос: ничего не писать, ответить 200
    supersede = "supersede"  # человек заменяет названную ревизию


@dataclass(frozen=True)
class Standing:
    """Что стоит на живом поколении: вердикт, ревизия, кто и когда."""

    verdict: str
    revision: int
    generation: int
    author: str
    at: str


def verdict_digest(body: TaskReviewVerdict) -> str:
    """Отпечаток запроса: вид, находки и комментарий; имя автора не входит."""
    findings = sorted(
        (f.model_dump(exclude_none=True, mode="json") for f in body.findings),
        key=lambda f: json.dumps(f, sort_keys=True, ensure_ascii=False),
    )
    packed = json.dumps(
        {
            "verdict": body.verdict.value,
            "comments": body.comments.strip(),
            "findings": findings,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(packed.encode()).hexdigest()


def standing_on(task: dict[str, Any]) -> bool:
    """Стоит ли на живом поколении вердикт."""
    generation = int(task.get("submission_generation") or 0)
    return bool(
        (task.get("review_verdict") or "").strip()
        and task.get("review_verdict_generation") == generation
    )


async def _standing(db: Any, task: dict[str, Any]) -> Standing:
    rows = await repo.get_task_updates(db, int(task["id"]))
    last = next(
        (dict(r) for r in reversed(rows) if dict(r).get("kind") == "review"), {}
    )
    return Standing(
        verdict=str(task.get("review_verdict") or ""),
        revision=int(task.get("review_verdict_revision") or 0),
        generation=int(task.get("review_verdict_generation") or 0),
        author=str(last.get("agent") or "неизвестен"),
        at=str(last.get("created_at") or "неизвестно"),
    )


def _refusal(
    status_code: int, reason: str, message: str, hint: str, **extra: Any
) -> HTTPException:
    payload = {
        "reason": reason,
        "actor_hint": "human" if status_code == 409 else "agent",
        "message": message,
        "hint": hint,
        **extra,
    }
    return HTTPException(status_code, detail=enrich_error_payload(payload))


def _generation_moved(expected: int, live: int) -> HTTPException:
    return _refusal(
        409,
        "verdict_generation_moved",
        f"вердикт о сдаче {expected} не записан: живое поколение — {live}. "
        "Ничего не записано.",
        "Прочитайте бриф (oc-hub review-brief <id> / hub_get_review_brief) и "
        f"повторите вердикт с expected_generation={live}.",
        current_generation=live,
    )


def _standing_refusal(st: Standing) -> HTTPException:
    return _refusal(
        409,
        "verdict_already_stands",
        f"на сдачу {st.generation} уже стоит {st.verdict} (ревизия {st.revision}, "
        f"{st.at}, {st.author}); замена — с supersedes_revision={st.revision}.",
        "Замена — только человеком: oc-hub review-verdict <id> <вердикт> "
        f"--expected-generation {st.generation} --supersedes-revision "
        f"{st.revision}. Повтор того же вердикта ничего не меняет.",
        current_revision=st.revision,
    )


async def decide(
    db: Any,
    task: dict[str, Any],
    body: TaskReviewVerdict,
    *,
    expected_generation: int | None,
    automated: bool,
    human_caller: bool,
) -> tuple[Outcome, Standing | None]:
    """Допустима ли запись; отказ — HTTPException, ничего не записано.

    ``expected_generation`` — то, о чём судили (вызывающий называет его сам;
    без него вердикт принимается как прежде, но ответ предупреждает).
    ``automated`` — стюард и автопилот: им разрешена только первая запись.
    """
    live = int(task.get("submission_generation") or 0)
    if expected_generation is not None and expected_generation != live:
        raise _generation_moved(expected_generation, live)
    supersedes = body.supersedes_revision
    if supersedes is not None and not human_caller:
        raise _refusal(
            403,
            "verdict_replacement_is_human_only",
            "стоящий вердикт заменяет только аутентифицированный человек: "
            "supersedes_revision агенту и автоматике не принимается.",
            "Если вердикт стоит ошибочно, скажите об этом человеку.",
        )
    if not standing_on(task):
        return Outcome.first, None
    st = await _standing(db, task)
    if task.get("review_verdict_closed_generation") == live:
        raise _refusal(
            409,
            "verdict_window_closed",
            f"одобрение сдачи {live} закрыто решением человека: нужна новая "
            "сдача, замена вердикта окно не открывает.",
            "Автору — hub_submit_for_review после правок, затем новый вердикт.",
            current_revision=st.revision,
        )
    same = (task.get("review_verdict_digest") or "") == verdict_digest(
        body
    ) and st.verdict == body.verdict.value
    if same and not automated:
        return Outcome.repeat, st
    if supersedes is None or automated:
        raise _standing_refusal(st)
    if supersedes != st.revision:
        raise _refusal(
            409,
            "verdict_revision_stale",
            f"ревизия {supersedes} устарела: сейчас стоит {st.verdict} "
            f"(ревизия {st.revision}, {st.at}, {st.author}). Ничего не записано.",
            f"Прочитайте карточку и, если замена всё ещё нужна, повторите с "
            f"supersedes_revision={st.revision}.",
            current_revision=st.revision,
        )
    return Outcome.supersede, st


def not_written_refusal(task: dict[str, Any], expected: int | None) -> HTTPException:
    """Запись не состоялась в самой транзакции: между проверкой и ею вклинился другой."""
    named = "не названо" if expected is None else str(expected)
    return _refusal(
        409,
        "verdict_not_written",
        f"вердикт не записан (поколение {named}): пока шла проверка, сдачу "
        "пересдали, на неё записали другой вердикт или стоящий заменили.",
        "Прочитайте карточку заново и повторите вердикт, если он нужен.",
    )


def replaced_line(old: Standing, new_verdict: str, new_revision: int) -> str:
    """Строка ленты о замене."""
    return (
        f"вердикт заменён: было {old.verdict} (ревизия {old.revision}), "
        f"стало {new_verdict} (ревизия {new_revision})"
    )
