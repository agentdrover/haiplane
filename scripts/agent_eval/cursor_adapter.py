"""Адаптер текущего провайдера (Cursor Cloud Agents) для runner-а (#1222).

Идёт тем же швом, что и стюард: ``cursor_cloud.create_agent_attempt``. Отличия
от рабочего пути намеренные: агенту НЕ передаётся ни MCP хаба, ни токен
(``hub_mcp_url`` и ``reviewer_token`` пусты) — доступа к production у прогона
нет вообще.

Этот модуль в тестах не вызывается: ответы провайдера здесь не проверены
живьём. Читаются те же поля, что у ревью: ``agent.id``, ``run.id``,
``run.status``, ``run.result`` (#1036) и ``/usage``. Если поле иное, прогон
честно станет ``infrastructure_error``/``incomplete``, но не ``passed``.
"""

from __future__ import annotations

import asyncio
import time

from agent_eval.runner import ProviderReply, ProviderRequest
from hub.integrations import cursor_cloud

_TERMINAL = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}


class CursorTransport:
    live = True
    provider = "cursor"

    def __init__(
        self,
        *,
        repo_url: str,
        starting_ref: str = "HEAD",
        poll_interval_s: float = 5.0,
        poll_limit_s: float = 600.0,
    ) -> None:
        self.params: dict = {}
        self._repo_url = repo_url
        self._ref = starting_ref
        self._interval = poll_interval_s
        self._limit = poll_limit_s

    async def complete(self, request: ProviderRequest) -> ProviderReply:
        created, denial = await cursor_cloud.create_agent_attempt(
            repo_url=self._repo_url,
            starting_ref=self._ref,
            model_id=request.model,
            prompt_text=request.prompt,
            name=request.session_id,
        )
        agent_id = ((created or {}).get("agent") or {}).get("id") or ""
        run_id = ((created or {}).get("run") or {}).get("id") or ""
        if not agent_id or not run_id:
            # Повторять можно только то, что точно не создало агента.
            safe = bool(
                denial and denial.is_transport and not denial.leaves_create_unknown
            )
            status = denial.status if denial else 0
            code = denial.code if denial else ""
            return ProviderReply(
                error=f"provider_refused: status={status} code={code}", retryable=safe
            )
        return await self._await_result(agent_id, run_id)

    async def _await_result(self, agent_id: str, run_id: str) -> ProviderReply:
        deadline = time.monotonic() + self._limit
        run = None
        while time.monotonic() < deadline:
            run = await cursor_cloud.get_run(agent_id, run_id)
            if str((run or {}).get("status") or "").upper() in _TERMINAL:
                break
            await asyncio.sleep(self._interval)
        else:
            await cursor_cloud.cancel_run(agent_id, run_id)
            return ProviderReply(error="timeout: прогон провайдера не завершился")
        usage = await self._usage(agent_id, run_id)
        text = (run or {}).get("result")
        if str(run.get("status")).upper() != "FINISHED" or not isinstance(text, str):
            return ProviderReply(
                error=f"run_not_finished: status={run.get('status')}", usage=usage
            )
        return ProviderReply(text=text, usage=usage)

    async def _usage(self, agent_id: str, run_id: str) -> dict | None:
        total, _ = cursor_cloud.usage_totals(
            await cursor_cloud.get_usage(agent_id, run_id)
        )
        # Провайдер называет сумму; разбивки на вход/выход в контракте нет.
        return None if total is None else {"input_tokens": total, "output_tokens": 0}
