"""Запуск облачного исполнителя из очереди F1 по политике проекта (#1412, F2.4).

Последний шаг F2 (#1365): хаб сам заказывает исполнителя Cursor на задачу,
которую назвала очередь (#1274), и записывает строку прогона (#1410); дальше
прогон держит F2.3 (#1411) — опрос, потолок, отмена, снятие по сдаче.

Режим — ключ политики проекта ``executor_launch``:

* ``off`` (по умолчанию и для всего нечитаемого, #835) — запуска нет;
* ``manual`` — запуск нажимает человек (REST и кнопка, только человек или
  админ). От ЕГО имени хаб выписывает одноразовый код implementer на задачу
  и вкладывает его в промпт: сегодня код выдаёт человек, и точка доверия F4
  (#1368) не сдвигается — агентского пути «выпиши себе код» нет.
* ``auto`` в этой задаче не принимается записью политики: без выдачи кода
  диспетчером (F4) запускать некому (решение владельца 26.09.2026).

Отказ всегда называет причину и НЕ зовёт провайдера: вне GitHub (облако до
GitVerse не достаёт), off, нет наблюдения F2.1 (токен прогона не должен
пушить в develop и main — #1409), модель исполнителя одного семейства с
ревьюером или стюардом (#758, #1008), очередь никого не назвала, по задаче
уже идёт прогон.

Промпт здесь минимальный: задача, база, код и указание обменять его первым
шагом. Скилл и дисциплину исполнителя даёт F3 (#1366).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.services import chat_pair, orchestrator_queue, project_policy
from hub.services.executor_dispatch import OUTCOME_RUNNING
from hub.services.model_family import same_family

log = logging.getLogger(__name__)

LAUNCH_KEY = "executor_launch"
PUSH_RIGHTS_TASK_KEY = "executor_push_rights_task"
LAUNCH_OFF = "off"
LAUNCH_MANUAL = "manual"
#: Что принимает запись политики сейчас. ``auto`` — после F4 и тени F6.
LAUNCH_MODES: tuple[str, ...] = (LAUNCH_OFF, LAUNCH_MANUAL)

AGENT_KIND = "executor"

REASON_OFF = "запуск исполнителя выключен политикой проекта"
REASON_NOT_GITHUB = "проект не на GitHub"
REASON_NO_OBSERVATION = "нет наблюдения прав токена"
REASON_FAMILY = "семейство модели исполнителя не отличается"
REASON_NO_MODEL = "модель исполнителя не задана"
REASON_NO_CANDIDATE = "очередь не назвала задачу"
REASON_ALREADY_RUNNING = "по задаче уже идёт прогон исполнителя"
REASON_NO_ACTING_AGENT = "нет агента chat-pair"
REASON_CREATE_REFUSED = "провайдер отказал в создании агента"
REASON_CREATE_EXHAUSTED = "создание агента не удалось за все попытки"
REASON_ANSWER_LOST = "ответ на создание агента не дошёл"

#: Отказы, которые стоит повторить с паузой (F0 и 22–24.09): лимит частоты
#: и исчерпанный лимит аккаунта, который Cursor отдаёт то 429, то кодом.
_RETRY_CODES = frozenset({"rate_limit_exceeded", "usage_limit_exceeded"})


@dataclass
class LaunchResult:
    launched: bool
    reason: str = ""
    task_id: int | None = None
    agent_id: str = ""
    run_id: str = ""
    row_id: int | None = None


def launch_mode_of(policy: dict) -> str:
    """``manual`` только когда так и записано; всё остальное — ``off``."""
    if isinstance(policy, dict) and policy.get(LAUNCH_KEY) == LAUNCH_MANUAL:
        return LAUNCH_MANUAL
    return LAUNCH_OFF


def _refused(reason: str, task_id: int | None = None) -> LaunchResult:
    return LaunchResult(False, reason, task_id)


async def _observation_missing(db: aiosqlite.Connection, policy: dict) -> str:
    """Причина, если наблюдения F2.1 нет; пусто — есть.

    Наблюдение — живая проверка (``live_checks``) с исходом done на задаче,
    которую владелец назвал в политике. Политику правит только человек, так
    что снять это условие агент сам не может.
    """
    witness = policy.get(PUSH_RIGHTS_TASK_KEY)
    if isinstance(witness, bool) or not isinstance(witness, int):
        return (
            f"{REASON_NO_OBSERVATION}: в политике проекта не названа задача "
            f"наблюдения ({PUSH_RIGHTS_TASK_KEY})"
        )
    checks = [dict(c) for c in await repo.list_live_checks(db, witness)]
    if not any(c.get("outcome") == "done" for c in checks):
        return (
            f"{REASON_NO_OBSERVATION}: на #{witness} нет живой проверки отказа "
            "пуша в develop и main"
        )
    return ""


def _family_refusal(model: str) -> str:
    """Причина, если модель исполнителя не отделена от ревьюера и стюарда."""
    from hub.services.review_dispatch import pick_review_model

    if not model:
        return f"{REASON_NO_MODEL} (EXECUTOR_MODEL)"
    others = {
        "ревьюер": pick_review_model(model),
        "стюард": (config.STEWARD_MODEL or "").strip(),
    }
    for role, other in others.items():
        # None — «не знаем» (#1008): незнание не доказывает разнесения.
        if same_family(model, other) is not False:
            return f"{REASON_FAMILY}: исполнитель {model}, {role} {other or '—'}"
    return ""


async def _preflight(db: aiosqlite.Connection, project: Any) -> str:
    """Отказ до очереди и до провайдера; пусто — можно выбирать задачу."""
    policy = project_policy.gate_policy_of(project)
    if launch_mode_of(policy) != LAUNCH_MANUAL:
        return REASON_OFF
    forge = project_policy.forge_of(project)
    if forge != "github":
        return f"{REASON_NOT_GITHUB} ({forge}): облако Cursor достаёт только до GitHub"
    missing = await _observation_missing(db, policy)
    if missing:
        return missing
    return _family_refusal((config.EXECUTOR_MODEL or "").strip())


async def _candidate(db: aiosqlite.Connection, project: Any) -> tuple[int | None, str]:
    answer = await orchestrator_queue.next_task(db, project)
    task_id = answer.get("next_task_id")
    if task_id is None:
        return None, f"{REASON_NO_CANDIDATE}: {answer.get('summary') or ''}".strip()
    for row in await repo.list_executor_runs(db, int(task_id)):
        r = dict(row)
        if r["outcome"] == OUTCOME_RUNNING:
            return None, f"{REASON_ALREADY_RUNNING} #{task_id} ({r['agent_id']})"
    return int(task_id), ""


def _prompt(task: dict[str, Any], base: str, code: str, hub_url: str) -> str:
    """Минимальный промпт F2.4; F3 (#1366) заменит его скиллом."""
    return (
        f"Ты исполнитель задачи #{task['id']} хаба Haiplane: {task['title']}.\n"
        f"ПЕРВЫМ шагом, до любой другой работы, обменяй одноразовый код "
        f"implementer на сессию: POST {hub_url}/api/auth/chat-pair/redeem, "
        f"код {code}. Он живёт {config.CHAT_PAIR_CODE_SECONDS} с.\n"
        f"Дальше по HTTP хаба: claim и pair-start задачи #{task['id']}, работа в "
        f"ветке с каноническим именем от базы {base}, одна сдача "
        "submit-review. Дисциплину исполнителя возьми из скилла "
        "executor-pair-discipline (GET /api/skills/executor-pair-discipline), "
        "если он есть. После сдачи заверши прогон."
    )


def _should_retry(refusal: cursor_cloud.Refusal | None) -> bool:
    return refusal is not None and (
        refusal.status == 429 or refusal.code in _RETRY_CODES
    )


async def _create(
    task_id: int, generation: int, order: dict[str, Any]
) -> tuple[str, str, str]:
    """Заказать агента с повторами: ``(agent_id, run_id, причина отказа)``.

    Повтор — только на отказ, который НЕ создал агента (429, лимит). Обрыв
    ответа — сначала сверка по имени: наивный повтор покупал бы второго
    (#1199); «не смогли спросить» — отказ, а не повтор.
    """
    limit = max(1, config.EXECUTOR_LAUNCH_MAX_ATTEMPTS)
    last = ""
    for attempt in range(1, limit + 1):
        marker = cursor_cloud.agent_marker(AGENT_KIND, task_id, generation, attempt)
        created, refusal = await cursor_cloud.create_agent_attempt(name=marker, **order)
        agent = (created or {}).get("agent") or {}
        run = (created or {}).get("run") or {}
        if agent.get("id"):
            return (
                str(agent["id"]),
                str(run.get("id") or agent.get("latestRunId") or ""),
                "",
            )
        if refusal is not None and refusal.is_transport:
            seen = await cursor_cloud.find_agent_by_name(marker)
            if seen.agent_id:
                return seen.agent_id, seen.run_id, ""
            return "", "", f"{REASON_ANSWER_LOST}: {refusal.detail}"
        last = _refusal_text(refusal)
        if not _should_retry(refusal):
            return "", "", f"{REASON_CREATE_REFUSED}: {last}"
        if attempt < limit:
            await asyncio.sleep(config.EXECUTOR_LAUNCH_PAUSE_S)
    return "", "", f"{REASON_CREATE_EXHAUSTED}: {limit} попыток, последний отказ {last}"


def _refusal_text(refusal: cursor_cloud.Refusal | None) -> str:
    if refusal is None:
        return "пустой ответ"
    code = f" {refusal.code}" if refusal.code else ""
    return f"HTTP {refusal.status or 'без ответа'}{code}"


async def launch_executor(
    db: aiosqlite.Connection, project: Any, *, issuer_principal_id: int
) -> LaunchResult:
    """Запустить исполнителя на задачу из очереди — по нажатию человека."""
    from hub.services.review_dispatch import instance_base_url

    refusal = await _preflight(db, project)
    if refusal:
        return _refused(refusal)
    task_id, why = await _candidate(db, project)
    if task_id is None:
        return _refused(why)
    if await chat_pair.get_acting_agent(db) is None:
        return _refused(f"{REASON_NO_ACTING_AGENT} (CHAT_PAIR_AGENT)", task_id)
    task = dict(await repo.get_task(db, task_id))
    generation = int(task.get("submission_generation") or 0) + 1
    code, _ttl = await chat_pair.issue_code(
        db, issuer_principal_id, kind="implementer", bound_task_id=task_id
    )
    await db.commit()
    base = project_policy.base_branch_of(project)
    model = config.EXECUTOR_MODEL.strip()
    order = {
        "repo_url": f"https://github.com/{project['repo']}",
        "starting_ref": base,
        "model_id": model,
        "prompt_text": _prompt(task, base, code, instance_base_url().rstrip("/")),
    }
    agent_id, run_id, failed = await _create(task_id, generation, order)
    if failed:
        await repo.add_task_update(
            db, task_id, "hub", "alert", f"Исполнитель НЕ запущен: {failed}."
        )
        await db.commit()
        return _refused(failed, task_id)
    row_id = await repo.create_executor_run(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id=agent_id,
        run_id=run_id,
        model=model,
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        f"Исполнитель запущен хабом по нажатию человека (#1412): агент "
        f"{agent_id}, модель {model}, сдача {generation}, от базы {base}.",
    )
    await db.commit()
    return LaunchResult(True, "", task_id, agent_id, run_id, row_id)
