"""Project passport projection. A passport is descriptive and grants no rights."""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hub.agent_passport_contract import (
    AgentPassport,
    PassportDeclaration,
    PassportFact,
    PassportRole,
    PassportRuntime,
)
from hub.db import fetchall
from hub.services import effective_policy, policy_change, working_rules

DECLARATION_PATH = ".hub/AGENT_PASSPORT.json"
PILOT_SOURCE = "Hub · пилот #2340"
POLICY_SOURCE = "effective policy"
RUNTIME_SOURCE = "приёмка постоянной роли"
ROADMAP_ORDER_SOURCE = "role_orders.started_at"
OBSERVATION_WINDOW = timedelta(days=1)
PURPOSE = {
    "executor": "Исполнение задач проекта",
    "roadmap": "Планирование и уточнение дорожной карты",
    "infrastructure": "Наблюдение за инфраструктурой и отчёт в Hub",
}


class RoleDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["executor", "roadmap", "infrastructure"]
    runtime: Literal["codex"] = "codex"
    events: Literal["task_assignment", "hub_events", "infra_alerts"] | None = None
    memory: Literal["hub_task", "hub_roadmap", "infra_checkpoint"] | None = None


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"]
    roles: list[RoleDeclaration] = Field(max_length=len(PURPOSE))
    review_by: date | None = None


_PILOT_ROLES = {
    "default": [
        RoleDeclaration(role="executor", events="task_assignment", memory="hub_task"),
        RoleDeclaration(role="roadmap", events="hub_events", memory="hub_roadmap"),
    ],
    "home-infra": [
        RoleDeclaration(
            role="infrastructure", events="infra_alerts", memory="infra_checkpoint"
        )
    ],
}

_PIPELINE_FIELDS = {
    "dor": "DoR",
    "review": "Ревью",
    "verdict": "Вердикт",
    "ci_before_submit": "CI перед сдачей",
    "release": "Выкат",
    "submission_contract": "Контракт сдачи",
    "completion": "Критерий завершения",
}
_PERMISSION_FIELDS = {
    "executor_launch": "Запуск исполнителя",
    "executor_push_rights_task": "Ссылка на основание допуска на push",
    "authorization": "Проверка разрешений",
}
_BUDGET_FIELDS = {
    "executor_task_cents_ceiling": "Потолок затрат, центы",
    "executor_task_token_ceiling": "Потолок токенов",
    "review_limit": "Лимит ревью",
    "deep_daily_cap": "Суточный лимит deep review",
}
_POLICY_FIELDS = _PIPELINE_FIELDS | _PERMISSION_FIELDS | _BUDGET_FIELDS


def _parse_declaration(raw: dict[str, Any]) -> Declaration | None:
    if raw.get("truncated") or not isinstance(raw.get("content"), str):
        return None
    try:
        declaration = Declaration.model_validate_json(raw["content"])
    except ValidationError:
        return None
    if len(declaration.roles) != len({r.role for r in declaration.roles}):
        return None
    return declaration


async def _read_declaration(
    project: Any, today: date
) -> tuple[PassportDeclaration, list[RoleDeclaration], str]:
    raw = await working_rules.read_project_base_file(
        project, DECLARATION_PATH, limit_chars=4096
    )
    sha = raw.get("sha")
    revision = (
        sha if isinstance(sha, str) and re.fullmatch("[0-9a-f]{40}", sha) else None
    )
    result = PassportDeclaration(
        state="unreadable", source=DECLARATION_PATH, revision=revision
    )
    if raw.get("state") == "present":
        declaration = _parse_declaration(raw)
        if declaration is None:
            result.state = "invalid"
            return result, [], result.source
        result.state = (
            "stale"
            if declaration.review_by and declaration.review_by < today
            else "present"
        )
        return result, declaration.roles, result.source
    if raw.get("state") == "missing":
        result.state = "missing"
    return result, _PILOT_ROLES.get(project["slug"], []), PILOT_SOURCE


def _declaration_fact(value: str | None, source: str, *, stale: bool) -> PassportFact:
    state = ("stale" if stale else "declared") if value is not None else "unknown"
    return PassportFact(state=state, value=value, source=source)


async def _roadmap_observation(
    db: aiosqlite.Connection, project_id: int, now: datetime
) -> PassportFact:
    # A start from the authenticated host proves role activity, not the model.
    rows = await fetchall(
        db,
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', MAX(started_at)) AS started "
        "FROM role_orders WHERE project_id=? AND role='roadmap' "
        "AND datetime(started_at) <= datetime(?)",
        (project_id, now.isoformat()),
    )
    checked = rows[0]["started"]
    if not checked:
        return PassportFact(source=ROADMAP_ORDER_SOURCE)
    started = datetime.fromisoformat(checked)
    return PassportFact(
        state="stale" if now - started > OBSERVATION_WINDOW else "observed",
        value="зарегистрирован старт роли, семейство runtime unknown",
        source=ROADMAP_ORDER_SOURCE,
        checked_at=checked,
    )


def _role_view(
    role: RoleDeclaration, source: str, observation: PassportFact, *, stale: bool
) -> PassportRole:
    return PassportRole(
        role=role.role,
        purpose=PURPOSE[role.role],
        status="planned" if role.role == "infrastructure" else "declared",
        source=source,
        runtime=PassportRuntime(
            declared=_declaration_fact(role.runtime, source, stale=stale),
            observed=observation,
            verified=PassportFact(source=RUNTIME_SOURCE),
        ),
        events=_declaration_fact(role.events, source, stale=stale),
        memory=_declaration_fact(role.memory, source, stale=stale),
    )


def _policy_fact(row: dict[str, Any], observed_at: str) -> PassportFact:
    value = row["value"]
    if row["key"] == "executor_push_rights_task" and value is not None:
        value = "configured"
    source = f"{POLICY_SOURCE} · {row['source']} · {row['reader']}"
    return PassportFact(
        source=source,
        state="observed" if value is not None else "unknown",
        value=value,
        checked_at=observed_at,
    )


def _policy_facts(project: Any, observed_at: str) -> dict[str, PassportFact]:
    # Publish only registered, typed gate values. Raw policy can contain commands,
    # paths, provider configuration and extension keys.
    facts = {
        row["key"]: _policy_fact(row, observed_at)
        for row in effective_policy.key_rows(project)
        if row["key"] in _POLICY_FIELDS
    }
    facts["completion"] = PassportFact(
        state="observed",
        value="критерии приёмки задачи, проверки, сдача и действующие гейты",
        source="рабочие правила Hub",
        checked_at=observed_at,
    )
    facts["authorization"] = PassportFact(
        state="observed",
        value="права принципала и проектные допуски проверяются для каждого действия",
        source="действующая авторизация Hub",
        checked_at=observed_at,
    )
    return facts


def _line(label: str, fact: PassportFact) -> str:
    value = str(fact.value) if fact.value is not None else "unknown"
    checked = f" · {fact.checked_at}" if fact.checked_at else ""
    return f"- {label} = {value} [{fact.state} · {fact.source}{checked}]"


def render_agent_passport(view: AgentPassport) -> str:
    d = view.declaration
    revision = f" @ {d.revision}" if d.revision else ""
    lines = [
        f"Паспорт агента · {view.project} · схема {view.schema_version}",
        f"Декларация: {d.state} [{d.source}{revision}]",
        f"Версия effective policy: {view.policy_version}",
        "Перед действием проверьте действующую политику и разрешения.",
    ]
    for facts in (view.pipeline, view.permissions, view.budget):
        lines.extend(_line(_POLICY_FIELDS[key], fact) for key, fact in facts.items())
    if not view.roles:
        lines.append("Постоянные роли: unknown")
    for role in view.roles:
        lines.append(f"{role.role} · {role.purpose} [{role.status} · {role.source}]")
        lines.extend(
            [
                _line("Codex runtime, заявлен", role.runtime.declared),
                _line("Codex runtime, наблюдался", role.runtime.observed),
                _line("Codex runtime, проверен", role.runtime.verified),
                _line("События / таймеры", role.events),
                _line("Память / checkpoint", role.memory),
            ]
        )
    return "\n".join(lines)


async def build_agent_passport(db: aiosqlite.Connection, project: Any) -> AgentPassport:
    now = datetime.now(timezone.utc)
    declaration, roles, source = await _read_declaration(project, now.date())
    facts = _policy_facts(project, now.isoformat(timespec="seconds"))
    observations = (
        {"roadmap": await _roadmap_observation(db, int(project["id"]), now)}
        if any(r.role == "roadmap" for r in roles)
        else {}
    )
    view = AgentPassport(
        project=project["slug"],
        policy_version=policy_change.policy_version(project),
        declaration=declaration,
        pipeline={key: facts[key] for key in _PIPELINE_FIELDS},
        permissions={key: facts[key] for key in _PERMISSION_FIELDS},
        budget={key: facts[key] for key in _BUDGET_FIELDS},
        roles=[
            _role_view(
                r,
                source,
                observations.get(r.role) or PassportFact(source=RUNTIME_SOURCE),
                stale=declaration.state == "stale",
            )
            for r in roles
        ],
    )
    view.text = render_agent_passport(view)
    return view
