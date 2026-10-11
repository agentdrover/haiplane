"""Адаптер текущего провайдера (Cursor Cloud Agents) для runner-а (#1222).

Идёт тем же швом, что и стюард: ``cursor_cloud.create_agent_attempt``. Отличия
от рабочего пути намеренные:

* агенту НЕ передаётся ни MCP хаба, ни токен — доступа к production нет;
* репозиторий только из явного allowlist eval-репозиториев; продовые
  ``agentdrover/*`` отклоняются всегда (облачный агент может запушить ветку);
* повтор допустим только после ДОКАЗАННОГО отказа до создания агента;
* облачный прогон, не дошедший до конца (таймаут, отмена), останавливается
  ровно одним ``cancel_run`` с подтверждением; agent_id/run_id остаются в
  ``refs`` для ручного восстановления.

В тестах модуль гоняется только на подменённом ``cursor_cloud``. Ответы
провайдера живьём не проверены: читаются те же поля, что у ревью
(``agent.id``, ``run.id``, ``run.status``, ``run.result``, ``/usage``).
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, replace
from typing import Any

from agent_eval.runner import ProviderReply, ProviderRequest
from hub.integrations import cursor_cloud

_TERMINAL = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}
ALLOWED_REPOS_ENV = "HAIPLANE_EVAL_ALLOWED_REPOS"
#: Владельцы продовых репозиториев: отклоняются даже из allowlist.
FORBIDDEN_OWNERS = frozenset({"agentdrover"})
#: Доля срока runner-а, которую адаптер тратит сам; остаток — на отмену.
_WAIT_SHARE = 0.8
#: Статусы 4xx, которые НЕ доказывают отсутствие агента.
_UNPROVEN_4XX = frozenset({404, 408, 409})
#: Исключения httpx, после которых запрос заведомо не ушёл.
_BEFORE_SEND = ("ConnectError", "ConnectTimeout")

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")


def repo_slug(value: str) -> str:
    """``owner/name`` из ``owner/name`` или https://github.com/owner/name(.git)."""
    text = (value or "").strip()
    text = re.sub(r"^https://github\.com/", "", text).removesuffix(".git").strip("/")
    if not _SLUG.match(text):
        raise ValueError("репозиторий должен быть owner/name на GitHub")
    return text.lower()


def load_allowed_repos(explicit: list[str] | None = None) -> frozenset[str]:
    raw = explicit if explicit else os.environ.get(ALLOWED_REPOS_ENV, "").split(",")
    return frozenset(repo_slug(item) for item in raw if item.strip())


def check_repo(repo_url: str, allowlist: frozenset[str]) -> str:
    """Slug разрешённого eval-репозитория или ValueError."""
    if not allowlist:
        raise ValueError(
            f"allowlist eval-репозиториев пуст: задайте {ALLOWED_REPOS_ENV} или "
            "--allowed-repo"
        )
    slug = repo_slug(repo_url)
    if slug.split("/", 1)[0] in FORBIDDEN_OWNERS:
        raise ValueError(f"{slug}: продовый репозиторий, eval в него не ходит")
    if slug not in allowlist:
        raise ValueError(f"{slug} нет в allowlist eval-репозиториев")
    return slug


def refusal_proven(denial: cursor_cloud.Refusal | None) -> bool:
    """Отказ, после которого агента заведомо нет (4xx кроме 404/408/409, ConnectError)."""
    if denial is None:
        return False
    if denial.status:
        return 400 <= denial.status < 500 and denial.status not in _UNPROVEN_4XX
    return denial.detail.startswith(_BEFORE_SEND)


@dataclass
class _Live:
    agent_id: str = ""
    run_id: str = ""
    finished: bool = False
    cancelled: bool = False

    def refs(self) -> dict[str, str]:
        return {"agent_id": self.agent_id, "run_id": self.run_id}


class CursorTransport:
    live = True
    provider = "cursor"

    def __init__(
        self,
        *,
        repo_url: str,
        allowed_repos: list[str] | None = None,
        starting_ref: str = "HEAD",
        poll_interval_s: float = 5.0,
        poll_limit_s: float = 600.0,
    ) -> None:
        self.params: dict[str, Any] = {}
        #: session_id -> идентификаторы у провайдера (для восстановления).
        self.refs: dict[str, dict[str, str]] = {}
        self._slug = check_repo(repo_url, load_allowed_repos(allowed_repos))
        self._ref = starting_ref
        self._interval = poll_interval_s
        self._limit = poll_limit_s

    async def complete(self, request: ProviderRequest) -> ProviderReply:
        live = _Live()
        try:
            reply = await self._drive(request, live)
        except BaseException:
            # Отмена runner-ом или сбой: облачный прогон не бросаем.
            await self._stop(live)
            raise
        if live.agent_id and not live.finished:
            confirmed = await self._stop(live)
            note = "" if confirmed else "; cancel_unconfirmed"
            reply = replace(reply, error=(reply.error or "") + note)
        return reply

    async def _drive(self, request: ProviderRequest, live: _Live) -> ProviderReply:
        created, denial = await cursor_cloud.create_agent_attempt(
            repo_url=f"https://github.com/{self._slug}",
            starting_ref=self._ref,
            model_id=request.model,
            prompt_text=request.prompt,
            name=request.session_id,
        )
        live.agent_id = ((created or {}).get("agent") or {}).get("id") or ""
        live.run_id = ((created or {}).get("run") or {}).get("id") or ""
        if not (live.agent_id and live.run_id):
            proven = refusal_proven(denial)
            status = denial.status if denial else 0
            code = denial.code if denial else ""
            return ProviderReply(
                error=f"provider_refused: status={status} code={code}",
                retryable=proven,
                no_charge=proven,
            )
        self.refs[request.session_id] = live.refs()
        wait = min(self._limit, request.timeout_s * _WAIT_SHARE)
        return await self._await_result(live, wait)

    async def _await_result(self, live: _Live, wait_s: float) -> ProviderReply:
        deadline = time.monotonic() + wait_s
        run: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            run = await cursor_cloud.get_run(live.agent_id, live.run_id)
            if str((run or {}).get("status") or "").upper() in _TERMINAL:
                live.finished = True
                break
            await asyncio.sleep(self._interval)
        else:
            return ProviderReply(
                error="timeout: прогон провайдера не завершился", refs=live.refs()
            )
        usage = await self._usage(live)
        text = (run or {}).get("result")
        status = str((run or {}).get("status")).upper()
        if status != "FINISHED" or not isinstance(text, str):
            return ProviderReply(
                error=f"run_not_finished: status={status}",
                usage=usage,
                refs=live.refs(),
            )
        return ProviderReply(text=text, usage=usage, refs=live.refs())

    async def _stop(self, live: _Live) -> bool:
        """Одна просьба об отмене и перечитывание: True — остановка подтверждена."""
        if not live.agent_id or not live.run_id or live.cancelled or live.finished:
            return live.finished
        live.cancelled = True
        await cursor_cloud.cancel_run(live.agent_id, live.run_id)
        for _ in range(3):
            run = await cursor_cloud.get_run(live.agent_id, live.run_id)
            if str((run or {}).get("status") or "").upper() in _TERMINAL:
                live.finished = True
                return True
            await asyncio.sleep(self._interval)
        return False

    async def _usage(self, live: _Live) -> dict | None:
        total, _ = cursor_cloud.usage_totals(
            await cursor_cloud.get_usage(live.agent_id, live.run_id)
        )
        # Провайдер называет сумму; разбивки на вход/выход в контракте нет.
        return None if total is None else {"input_tokens": total, "output_tokens": 0}
