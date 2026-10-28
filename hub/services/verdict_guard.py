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
    parts: dict[str, Any] = {
        "verdict": body.verdict.value,
        "comments": body.comments.strip(),
        "findings": findings,
    }
    if body.pair_miss is not None or body.pair_miss_note:
        # #2139: пометка меняет смысл вердикта; без неё повтор с пометкой
        # считался бы тем же вердиктом и отвечал успехом, ничего не записав.
        # Только при наличии пометки: отпечатки прежних вердиктов не меняются.
        parts["pair_miss"] = [body.pair_miss, body.pair_miss_note.strip()]
    packed = json.dumps(
        parts,
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


def ensure_approval_is_human(
    body: TaskReviewVerdict,
    *,
    agent_caller: bool,
    human_caller: bool,
    self_approved: bool,
) -> None:
    """APPROVED принимает человек или внутренняя автоматика, не внешний агент (#1872).

    19.10.2026 #1867 закрыта APPROVED агента при карточке «вердикт: человек»:
    запись вердикта маршрут не читает, а принятый APPROVED удовлетворяет
    review-гейт. Правило не зависит ни от маршрута, ни от статуса: оба флага
    приходят из аутентифицированной личности (REST и web), а не из тела запроса.

    Внутренние писатели (auto_verdict, steward_applied) ``agent_caller`` не
    передают и сюда не попадают. Credential с ``tasks.human_gate`` считается
    человеком. Исключение — solo mode, и только когда сервер сам вычислил
    ``self_approved`` (ensure_reviewer_independence), а не по флагу конфигурации.
    Отказ ничего не пишет: проверка стоит до чтения вердикта и до записи.
    """
    if body.verdict.value != "approved":
        return
    if not agent_caller or human_caller or self_approved:
        return
    raise _refusal(
        409,
        "approval_reserved_for_human",
        "APPROVED принимает только человек или автоматика хаба по политике "
        "проекта; внешний агент approved не записывает. Ничего не записано.",
        "вердикт за человеком: сдайте отчёт ревью (hub_submit_machine_review) "
        "или CHANGES_REQUESTED; одобрит владелец.",
    )


def ensure_pair_miss_mark(body: TaskReviewVerdict, *, human_caller: bool) -> None:
    """Пометка «не ошибка пары» (#2139): только человек, только false, только с заметкой.

    Агент (и любой вызов без личности человека) получает 403 и ничего не
    записывает: поле не отбрасывается молча, иначе агент не узнал бы, что
    пометка не легла. Человек без заметки, с pair_miss=true, с пометкой на
    approved или с заметкой без пометки - 422. Ничего не пишет.
    """
    if body.pair_miss is None and not body.pair_miss_note:
        return
    if not human_caller:
        raise _refusal(
            403,
            "pair_miss_human_only",
            "Пометку pair_miss ставит только человек; вердикт агента её не "
            "несёт. Ничего не записано.",
            "уберите pair_miss и pair_miss_note из вердикта.",
        )
    problem = ""
    if body.pair_miss is not False:
        problem = (
            "pair_miss принимает только false (по умолчанию возврат - ошибка пары)"
        )
    elif body.verdict.value != "changes_requested":
        problem = "пометка относится к возврату (changes_requested)"
    elif not body.pair_miss_note.strip():
        problem = "pair_miss=false требует непустой pair_miss_note"
    if problem:
        raise _refusal(
            422,
            "pair_miss_invalid",
            f"{problem}. Ничего не записано.",
            "pair_miss=false вместе с pair_miss_note: почему это не ошибка пары.",
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
    if supersedes is not None and expected_generation is None:
        # Ревизия растёт по задаче, а не по сдаче: без названного поколения
        # устаревшая замена легла бы на пересданную работу (Codex P2).
        raise _refusal(
            422,
            "verdict_replacement_needs_generation",
            "замена вердикта требует expected_generation: ревизия не привязана "
            "к сдаче, и без поколения замена могла бы лечь на пересданную работу.",
            "Прочитайте бриф и повторите с --expected-generation <N> "
            "--supersedes-revision <R>.",
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
