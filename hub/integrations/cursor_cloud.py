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
from typing import Any

import httpx

from hub import config

log = logging.getLogger(__name__)

_TIMEOUT = 30.0


def is_configured() -> bool:
    return bool((config.CURSOR_API_KEY or "").strip())


async def _request(
    method: str, path: str, json_body: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """One guarded round-trip; every failure mode collapses to None."""
    if not is_configured():
        return None
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
            return None
        body = resp.json()
        if not isinstance(body, dict):
            log.warning("cursor cloud %s %s -> non-object body", method, path)
            return None
        return body
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("cursor cloud %s %s failed: %s", method, path, exc)
        return None


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
    body: dict[str, Any] = {
        "prompt": {"text": prompt_text},
        "model": {"id": model_id},
        "repos": [{"url": repo_url, "startingRef": starting_ref}],
        "autoCreatePR": False,
        "workOnCurrentBranch": False,
        "mcpServers": [
            {
                "name": "openclaw-hub",
                "type": "http",
                "url": hub_mcp_url,
                "headers": {"Authorization": f"Bearer {reviewer_token}"},
            }
        ],
    }
    return await _request("POST", "/v1/agents", body)


async def get_run(agent_id: str, run_id: str) -> dict[str, Any] | None:
    return await _request("GET", f"/v1/agents/{agent_id}/runs/{run_id}")


async def get_usage(agent_id: str, run_id: str | None = None) -> dict[str, Any] | None:
    path = f"/v1/agents/{agent_id}/usage"
    if run_id:
        path += f"?runId={run_id}"
    return await _request("GET", path)


async def list_models() -> dict[str, Any] | None:
    return await _request("GET", "/v1/models")
