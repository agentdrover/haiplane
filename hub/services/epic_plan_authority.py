"""Trusted preflight and hard spending grants for the bounded pilot."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from hub import config


class WorkBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    phase: Literal["executor", "review"]
    project: Literal["receipt-split-calculator"]
    plan_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_id: int = Field(gt=0)
    generation: int = Field(gt=0)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    host: Literal["k8", "cloud", "local"]
    model: str = Field(min_length=1, max_length=128)
    principal_id: int = Field(gt=0)
    actor_principal_id: int = Field(gt=0)
    attempt: int = Field(gt=0, le=2)


class SpendingGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    binding: WorkBinding
    reference: str = Field(pattern=r"^[A-Za-z0-9_.-]{12,128}$")
    ceiling_cents: int = Field(gt=0)
    expires_at: AwareDatetime
    workspace_ready: bool
    hard_cap_enforced: bool
    idempotent_reference: bool
    source_ci_verified: bool
    independent_review_ready: bool
    owner_checkpoint_ready: bool

    def covers(self, binding: WorkBinding) -> bool:
        return (
            self.binding == binding
            and self.expires_at > datetime.now(UTC)
            and self.workspace_ready
            and self.hard_cap_enforced
            and self.idempotent_reference
            and self.source_ci_verified
            and self.independent_review_ready
            and self.owner_checkpoint_ready
        )


class ChargeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    reference: str
    binding: WorkBinding
    charged_cents: int = Field(ge=0, le=1_000_000_000)


def _endpoint() -> tuple[str, str] | None:
    root = (config.env_get("EPIC_PLAN_COST_AUTHORITY_URL", "") or "").rstrip("/")
    credential = config.env_get("EPIC_PLAN_COST_AUTHORITY_TOKEN", "") or ""
    url = urlsplit(root)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or not credential
    ):
        return None
    return root, credential


def configured_source_fingerprint() -> str:
    try:
        config = _endpoint()
    except ValueError:
        return ""
    return sha256(config[0].encode()).hexdigest() if config else ""


async def _call(route: str, data: dict) -> bytes | None:
    # This server-owned origin issues read-only proofs. No URL or credential
    # comes from a plan, task, host, reviewer, callback or user request.
    try:
        endpoint = _endpoint()
        if endpoint is None:
            return None
        url, token = endpoint
        async with (
            asyncio.timeout(5),
            httpx.AsyncClient(follow_redirects=False, trust_env=False) as client,
        ):
            response = await client.post(
                f"{url}/{route}",
                json=data,
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            return response.content
    except (ValueError, httpx.HTTPError, TimeoutError):
        return None


async def get_grant(binding: WorkBinding) -> SpendingGrant | None:
    data = await _call("grant", binding.model_dump())
    if data is None:
        return None
    try:
        grant = SpendingGrant.model_validate_json(data)
    except ValidationError:
        return None
    return grant if grant.covers(binding) else None


async def get_charge(binding: WorkBinding, reference: str) -> ChargeReceipt | None:
    data = await _call(
        "charge", {"binding": binding.model_dump(), "reference": reference}
    )
    if data is None:
        return None
    try:
        receipt = ChargeReceipt.model_validate_json(data)
    except ValidationError:
        return None
    if receipt.reference != reference or receipt.binding != binding:
        return None
    return receipt
