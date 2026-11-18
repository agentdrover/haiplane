"""Read-only schema shared by the passport and project bootstrap."""

from typing import Literal

from pydantic import BaseModel


class PassportFact(BaseModel):
    state: Literal["declared", "observed", "verified", "unknown", "stale"] = "unknown"
    value: str | int | None = None
    source: str
    checked_at: str | None = None


class PassportRuntime(BaseModel):
    declared: PassportFact
    observed: PassportFact
    verified: PassportFact


class PassportRole(BaseModel):
    role: Literal["executor", "roadmap", "infrastructure"]
    purpose: str
    status: Literal["declared", "planned"]
    source: str
    runtime: PassportRuntime
    events: PassportFact
    memory: PassportFact


class PassportDeclaration(BaseModel):
    state: Literal["present", "missing", "unreadable", "invalid", "stale"]
    source: str
    revision: str | None = None


class AgentPassport(BaseModel):
    schema_version: Literal["1"] = "1"
    project: str
    policy_version: str
    declaration: PassportDeclaration
    pipeline: dict[str, PassportFact]
    permissions: dict[str, PassportFact]
    budget: dict[str, PassportFact]
    roles: list[PassportRole]
    text: str = ""
