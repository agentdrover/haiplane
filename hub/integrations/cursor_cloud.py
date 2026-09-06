"""Thin client for the Cursor Cloud Agents API v1 (#756).

Spec: https://cursor.com/docs/cloud-agent/api/endpoints — PUBLIC BETA, so
the whole module lives by one contract: every method returns ``dict |
None`` and ``None`` means "could not" — no key, network trouble, non-2xx,
unparsable body, changed schema. One ``log.warning`` per failure, never an
exception across the boundary. Consumers (the review dispatcher, #757) are
required to live with ``None``: a broken beta must break nothing except
the automation it powers.

Nobody calls this module until #757 lands — shipping it changes no hub
behavior, the same shadow-step pattern as #581 and #743.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from hub import brand, config

log = logging.getLogger(__name__)

_TIMEOUT = 30.0


def is_configured() -> bool:
    return bool((config.CURSOR_API_KEY or "").strip())


@dataclass(frozen=True)
class Refusal:
    """Почему провайдер не принял запрос, в разбираемом виде (#1182).

    Модуль по контракту схлопывает любой провал в ``None``, и это верно
    для потребителей, которым важен только факт: сломанная бета не должна
    ломать ничего, кроме автоматики, которую она питает. Но один
    потребитель — выбор судьи — обязан различать два разных провала.
    «Эта модель здесь недоступна» и «связь оборвалась» требуют разных
    решений: первое означает взять другую модель, второе — попробовать ту
    же ещё раз. Схлопнутые в одно, они дают судью, зависящего от качества
    сети, а такое суждение невоспроизводимо.

    Поэтому деталь отказа не заменяет ``None``, а сопровождает его: старый
    контракт цел, а тот, кому нужно, спрашивает отдельно.
    """

    #: HTTP-код, если ответ вообще был; 0 — до ответа не дошло.
    status: int = 0
    #: ``error.code`` из тела, если провайдер его назвал.
    code: str = ""
    #: Короткий фрагмент ответа — для человека в журнале, не для решений.
    detail: str = ""

    @property
    def is_transport(self) -> bool:
        """Сеть, таймаут, нечитаемый ответ — то, что стоит повторить как есть."""
        return self.status == 0 or self.status // 100 == 5


async def _attempt(
    method: str, path: str, json_body: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, Refusal | None]:
    """Один защищённый заход: ``(тело, отказ)``, и ровно одно из них не None.

    Здесь живёт вся работа; ``_request`` — вид на неё для тех, кому хватает
    факта провала.
    """
    if not is_configured():
        return None, Refusal(detail="CURSOR_API_KEY не задан")
    url = f"{(config.CURSOR_API_URL or 'https://api.cursor.com').rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {config.CURSOR_API_KEY.strip()}"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(method, url, json=json_body, headers=headers)
        if resp.status_code // 100 != 2:
            log.warning(
                "cursor cloud %s %s -> HTTP %s: %s",
                method,
                path,
                resp.status_code,
                resp.text[:300],
            )
            return None, Refusal(
                status=resp.status_code,
                code=_error_code(resp),
                detail=resp.text[:300],
            )
        body = resp.json()
        if not isinstance(body, dict):
            log.warning("cursor cloud %s %s -> non-object body", method, path)
            return None, Refusal(status=resp.status_code, detail="тело не объект")
        return body, None
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("cursor cloud %s %s failed: %s", method, path, exc)
        return None, Refusal(detail=str(exc)[:300])


def _error_code(resp: httpx.Response) -> str:
    """``error.code`` из тела ответа, или пустая строка.

    Тело беты может оказаться чем угодно, поэтому разбор целиком защищён:
    имя, которого мы не смогли прочитать, — это отсутствие имени, а не
    повод для исключения через границу, которая обещала их не пропускать.
    """
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 - см. контракт модуля
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    code = error.get("code")
    return code.strip() if isinstance(code, str) else ""


async def _request(
    method: str, path: str, json_body: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """One guarded round-trip; every failure mode collapses to None.

    Контракт не изменился: потребителям, которым важен только факт провала,
    по-прежнему возвращается ``None``. Различать причины умеет ``_attempt``,
    и спрашивает его ровно тот, кому это нужно (#1182).
    """
    body, _ = await _attempt(method, path, json_body)
    return body


async def create_review_agent(
    *,
    repo_url: str,
    starting_ref: str,
    model_id: str,
    prompt_text: str,
    hub_mcp_url: str,
    reviewer_token: str,
) -> dict[str, Any] | None:
    """Queue a cloud agent that reviews ``starting_ref`` of ``repo_url``.

    The agent gets the hub's own MCP inline, authenticated as the REVIEWER
    principal — the report comes back through our contract
    (hub_get_review_brief / hub_submit_machine_review), not through git:
    ``autoCreatePR=false`` and ``workOnCurrentBranch=false`` keep any
    accidental commits on a throwaway cursor/ branch.
    """
    created, _ = await create_agent_attempt(
        repo_url=repo_url,
        starting_ref=starting_ref,
        model_id=model_id,
        prompt_text=prompt_text,
        hub_mcp_url=hub_mcp_url,
        reviewer_token=reviewer_token,
    )
    return created


async def create_agent_attempt(
    *,
    repo_url: str,
    starting_ref: str,
    model_id: str,
    prompt_text: str,
    hub_mcp_url: str,
    reviewer_token: str,
) -> tuple[dict[str, Any] | None, Refusal | None]:
    """То же, что :func:`create_review_agent`, но с причиной отказа (#1182).

    Нужна одному потребителю — выбору судьи, который обязан отличить
    «эта модель недоступна» от «связь оборвалась». Тело запроса собирается
    здесь, а ``create_review_agent`` остаётся видом на неё: два способа
    собрать один и тот же запрос разошлись бы, и разошёлся бы тот, который
    реже читают.
    """
    body: dict[str, Any] = {
        "prompt": {"text": prompt_text},
        "model": {"id": model_id},
        "repos": [{"url": repo_url, "startingRef": starting_ref}],
        "autoCreatePR": False,
        "workOnCurrentBranch": False,
        "mcpServers": [
            {
                "name": brand.MCP_SERVER_NAME,
                "type": "http",
                "url": hub_mcp_url,
                "headers": {"Authorization": f"Bearer {reviewer_token}"},
            }
        ],
    }
    return await _attempt("POST", "/v1/agents", body)


async def get_run(agent_id: str, run_id: str) -> dict[str, Any] | None:
    return await _request("GET", f"/v1/agents/{agent_id}/runs/{run_id}")


async def get_usage(agent_id: str, run_id: str | None = None) -> dict[str, Any] | None:
    path = f"/v1/agents/{agent_id}/usage"
    if run_id:
        path += f"?runId={run_id}"
    return await _request("GET", path)


async def list_models() -> dict[str, Any] | None:
    return await _request("GET", "/v1/models")
