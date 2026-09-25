#!/usr/bin/env python3
"""Разовая проба: может ли облачный прогон Cursor пушить в develop и main (#1409).

F2.1 эпика #1272. Прежде чем хаб впервые запустит облачного исполнителя,
владелец хочет ВИДЕТЬ отказ пуша в базовые ветки из самого прогона. Скрипт
заказывает ровно одного агента тем же клиентом, что и ревью
(``hub.integrations.cursor_cloud``), даёт ему узкий промпт, дожидается
ответа, печатает его и снимает прогон.

    python scripts/probe_executor_push_rights.py            # план, сеть не трогается
    python scripts/probe_executor_push_rights.py --live     # настоящий платный запуск

По умолчанию — ``--dry-run``: печатается тело запроса и параметры опроса и
отмены, ни одного сетевого вызова. Деньги тратит только ``--live``, и
запускает его владелец (вариант А из F0, docs/specs/
orchestrator-executor-environment.md, раздел «Проба прав пуша»).

Ключ Cursor берётся так же, как у хаба: из окружения процесса через
``hub.config.CURSOR_API_KEY``. Скрипт его не печатает ни в каком режиме —
о нём сообщается только «задан» или «не задан».
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from hub import config
from hub.integrations import cursor_cloud

TASK_ID = 1409
REPO_URL = "https://github.com/agentdrover/haiplane"
STARTING_REF = "develop"
#: Из SUBSCRIPTION_LAUNCHABLE_MODELS; на ней же шли оба прогона F0.
MODEL_ID = "claude-sonnet-5"
CONTROL_BRANCH = f"task-{TASK_ID}/push-probe"
MARKER = cursor_cloud.agent_marker("probe", TASK_ID, 0, 1)

BEGIN = "BEGIN_PROBE"
END = "END_PROBE"

POLL_INTERVAL_S = 20.0
POLL_DEADLINE_S = 15 * 60.0
#: F0: пять 429 подряд, успех с шестой после паузы. Потолок с запасом.
CANCEL_MAX_ATTEMPTS = 10
CANCEL_PAUSE_S = 30.0

TERMINAL = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}

PROMPT = f"""Это проба прав пуша, а не задача. Выполни строго шаги ниже и больше ничего.
Не меняй файлы, не открывай PR, не запускай тесты, не ставь зависимости.

1. git checkout -b {CONTROL_BRANCH}
2. git commit --allow-empty -m "probe: push rights (#{TASK_ID})"
3. Контроль: git push origin HEAD:refs/heads/{CONTROL_BRANCH} 2>&1; echo "rc=$?"
4. git push --dry-run origin HEAD:refs/heads/develop 2>&1; echo "rc=$?"
5. git push --dry-run origin HEAD:refs/heads/main 2>&1; echo "rc=$?"

Шаги 4 и 5 выполни, даже если шаг 3 не прошёл.
ЗАПРЕЩЕНО: любой git push в develop или main без --dry-run, --force,
--delete, любые другие push, чем перечисленные.

Последним сообщением выведи ровно один блок и заверши работу:

{BEGIN}
=== control: git push origin HEAD:refs/heads/{CONTROL_BRANCH}
<весь вывод шага 3 дословно>
rc=<код>
=== develop: git push --dry-run origin HEAD:refs/heads/develop
<весь вывод шага 4 дословно>
rc=<код>
=== main: git push --dry-run origin HEAD:refs/heads/main
<весь вывод шага 5 дословно>
rc=<код>
{END}

Вывод копируй ДОСЛОВНО: без сокращений, пересказа и пояснений внутри блока.
Единственное исключение: если в выводе окажется токен или пароль, замени его
на ***.
"""

_SECRET_PATTERNS = (
    re.compile(r"(://)[^/@\s]+@"),
    re.compile(r"\b(?:ghs|ghp|gho|ghu|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
)


class Provider(Protocol):
    async def create(
        self, body: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, cursor_cloud.Refusal | None]: ...

    async def find(self, name: str) -> cursor_cloud.Reconciliation: ...

    async def get_run(self, agent_id: str, run_id: str) -> dict[str, Any] | None: ...

    async def cancel(
        self, agent_id: str, run_id: str
    ) -> tuple[dict[str, Any] | None, cursor_cloud.Refusal | None]: ...

    async def usage(self, agent_id: str, run_id: str) -> dict[str, Any] | None: ...


class CursorProvider:
    """Настоящий провайдер: те же вызовы и та же авторизация, что у хаба.

    ``_attempt`` приватен, но взят нарочно: публичный ``create_agent_attempt``
    вшивает MCP хаба с токеном ревьюера, а пробе он не нужен и не должен
    попадать в облако. Разовому скрипту честнее позвать тот же защищённый
    заход, чем добавлять в хаб функцию под один запуск.
    """

    async def create(
        self, body: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, cursor_cloud.Refusal | None]:
        return await cursor_cloud._attempt(
            "POST", "/v1/agents", body, timeout=cursor_cloud._CREATE_TIMEOUT
        )

    async def find(self, name: str) -> cursor_cloud.Reconciliation:
        return await cursor_cloud.find_agent_by_name(name)

    async def get_run(self, agent_id: str, run_id: str) -> dict[str, Any] | None:
        return await cursor_cloud.get_run(agent_id, run_id)

    async def cancel(
        self, agent_id: str, run_id: str
    ) -> tuple[dict[str, Any] | None, cursor_cloud.Refusal | None]:
        return await cursor_cloud._attempt(
            "POST", f"/v1/agents/{agent_id}/runs/{run_id}/cancel"
        )

    async def usage(self, agent_id: str, run_id: str) -> dict[str, Any] | None:
        return await cursor_cloud.get_usage(agent_id, run_id)


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


@dataclass
class Timing:
    poll_interval: float = POLL_INTERVAL_S
    poll_deadline: float = POLL_DEADLINE_S
    cancel_attempts: int = CANCEL_MAX_ATTEMPTS
    cancel_pause: float = CANCEL_PAUSE_S


def request_body() -> dict[str, Any]:
    return {
        "prompt": {"text": PROMPT},
        "model": {"id": MODEL_ID},
        "repos": [{"url": REPO_URL, "startingRef": STARTING_REF}],
        "autoCreatePR": False,
        "workOnCurrentBranch": False,
        "name": MARKER,
    }


def redact(text: str) -> str:
    """Токены GitHub и креды в URL — в ``***``; всё остальное дословно."""
    text = _SECRET_PATTERNS[0].sub(r"\1***@", text)
    for pattern in _SECRET_PATTERNS[1:]:
        text = pattern.sub("***", text)
    return text


def extract_probe_block(text: str | None) -> str | None:
    """Последний блок между маркерами, без самих маркеров; None — блока нет.

    Последний, как у разбора отчёта ревью: модель может процитировать формат
    по дороге, и пример не должен заслонить настоящий ответ.
    """
    if not text:
        return None
    blocks = re.findall(
        rf"^\s*{BEGIN}\s*$\n(.*?)^\s*{END}\s*$", text, flags=re.DOTALL | re.MULTILINE
    )
    if not blocks:
        return None
    return str(blocks[-1]).rstrip("\n")


def _status(run: dict[str, Any] | None) -> str:
    return str((run or {}).get("status") or "").upper()


def _key_state() -> str:
    return "задан" if cursor_cloud.is_configured() else "НЕ задан"


def print_plan(timing: Timing) -> None:
    print("РЕЖИМ: --dry-run, сеть не трогается, агент не создаётся.")
    print(f"Ключ Cursor в окружении: {_key_state()} (значение не печатается).")
    print(f"API: {config.CURSOR_API_URL}")
    print("Сделал бы: POST /v1/agents с телом:")
    print(json.dumps(request_body(), ensure_ascii=False, indent=2))
    print(
        f"Затем опрос GET /v1/agents/{{id}}/runs/{{runId}} каждые "
        f"{timing.poll_interval:.0f} с до FINISHED или {timing.poll_deadline:.0f} с;"
    )
    print(f"разбор текста рана между {BEGIN} и {END};")
    print(
        "отмена POST /v1/agents/{id}/runs/{runId}/cancel, если прогон не "
        f"завершён: до {timing.cancel_attempts} попыток с паузой "
        f"{timing.cancel_pause:.0f} с до подтверждённого CANCELLED;"
    )
    print("usage: GET /v1/agents/{id}/usage?runId={runId}.")
    print("Настоящий запуск: --live.")


async def create_agent(provider: Provider) -> tuple[str, str]:
    created, refusal = await provider.create(request_body())
    if created is not None:
        agent = created.get("agent") or {}
        run = created.get("run") or {}
        return str(agent.get("id") or ""), str(
            run.get("id") or agent.get("latestRunId") or ""
        )
    refusal = refusal or cursor_cloud.Refusal(detail="пустой ответ")
    print(
        f"Создание отказано: HTTP {refusal.status} code={refusal.code or '-'} "
        f"{refusal.detail[:200]}"
    )
    if not refusal.is_transport:
        return "", ""
    # Обрыв на создании уже оставлял оплаченного агента без идентификатора
    # (#1199). Ищем своего по метке, прежде чем признать провал.
    found = await provider.find(MARKER)
    if found.agent_id:
        print(f"Агент найден по метке {MARKER} после обрыва.")
        return found.agent_id, found.run_id
    if not found.asked:
        print(f"Список агентов не прочитан: проверьте вручную метку {MARKER}.")
    return "", ""


async def poll_run(
    provider: Provider,
    agent_id: str,
    run_id: str,
    timing: Timing,
    sleep: Sleep,
    clock: Clock,
) -> dict[str, Any] | None:
    started = clock()
    last_status = ""
    run: dict[str, Any] | None = None
    while True:
        fresh = await provider.get_run(agent_id, run_id)
        if fresh is not None:
            run = fresh
        status = _status(run)
        if status != last_status:
            print(f"[{clock() - started:5.0f} с] прогон: {status or 'нет ответа'}")
            last_status = status
        if status in TERMINAL or clock() - started >= timing.poll_deadline:
            return run
        await sleep(timing.poll_interval)


async def cancel_run(
    provider: Provider, agent_id: str, run_id: str, timing: Timing, sleep: Sleep
) -> str:
    """Снять прогон; вернуть его конечный статус или '' — не подтверждено.

    F0: ``cancel`` пять раз подряд отвечал 429 и взял с шестой. Поэтому
    успехом считается не 2xx на отмене, а статус прогона, прочитанный после.
    """
    for attempt in range(1, timing.cancel_attempts + 1):
        status = _status(await provider.get_run(agent_id, run_id))
        if status in TERMINAL:
            return status
        _, refusal = await provider.cancel(agent_id, run_id)
        if refusal is None:
            print(f"Отмена, попытка {attempt}: принята, проверяю статус.")
        else:
            print(
                f"Отмена, попытка {attempt}: HTTP {refusal.status} "
                f"code={refusal.code or '-'}"
            )
        await sleep(timing.cancel_pause)
    status = _status(await provider.get_run(agent_id, run_id))
    return status if status in TERMINAL else ""


def print_usage(usage: dict[str, Any] | None) -> None:
    if usage is None:
        print("usage: провайдер не ответил.")
        return
    total = (usage.get("totalUsage") or {}).get("totalTokens")
    cents = usage.get("chargedCents")
    if cents is None:
        cents = (usage.get("totalUsage") or {}).get("chargedCents")
    print(f"usage: totalTokens={total} chargedCents={cents}")
    print(json.dumps(usage, ensure_ascii=False, sort_keys=True))


def print_result(run: dict[str, Any] | None) -> bool:
    text = str((run or {}).get("result") or "")
    block = extract_probe_block(text)
    if block is None:
        print(f"Блока {BEGIN}/{END} в тексте рана нет. Текст как есть:")
        print(redact(text) if text else "<пусто>")
        return False
    print(BEGIN)
    print(redact(block))
    print(END)
    return True


async def live(provider: Provider, timing: Timing, sleep: Sleep, clock: Clock) -> int:
    print(
        f"РЕЖИМ: --live. Ключ Cursor в окружении: {_key_state()} (значение не печатается)."
    )
    if not cursor_cloud.is_configured():
        print("Ключа нет в окружении процесса — запуск невозможен.")
        return 2
    agent_id, run_id = await create_agent(provider)
    if not agent_id or not run_id:
        print(
            f"Агент не создан или без прогона: agent={agent_id or '-'} run={run_id or '-'}"
        )
        return 2
    print(f"Агент {agent_id}, прогон {run_id}, метка {MARKER}.")
    run = await poll_run(provider, agent_id, run_id, timing, sleep, clock)
    got_block = print_result(run)
    final = _status(run)
    if final not in TERMINAL:
        final = await cancel_run(provider, agent_id, run_id, timing, sleep)
        if not final:
            print(
                f"ОТМЕНА НЕ ПОДТВЕРЖДЕНА после {timing.cancel_attempts} попыток: "
                f"снимите прогон вручную (agent={agent_id} run={run_id})."
            )
    print(f"Конечный статус прогона: {final or 'не подтверждён'}.")
    print_usage(await provider.usage(agent_id, run_id))
    if not final:
        return 3
    return 0 if got_block else 1


def main(
    argv: list[str] | None = None,
    *,
    provider: Provider | None = None,
    timing: Timing | None = None,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="live", action="store_false", default=False)
    mode.add_argument("--live", dest="live", action="store_true")
    args = parser.parse_args(argv)
    timing = timing or Timing()
    if not args.live:
        print_plan(timing)
        return 0
    return asyncio.run(live(provider or CursorProvider(), timing, sleep, clock))


if __name__ == "__main__":
    sys.exit(main())
