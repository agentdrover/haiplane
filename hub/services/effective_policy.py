"""Действующая политика проекта одной сводкой (#1457).

Поведение проекта задают ключи ``gate_policy``, настройки сервера и замок #743,
и прочитать их разом можно было только из кода и БД. Эта сводка — не вторая
копия правил: у неё нет своих умолчаний и толкований. Значение каждого ключа
она спрашивает у ТОГО ЖЕ читателя, которым пользуется решатель, а источник
выводит из одного факта: есть ли ключ в сохранённой политике проекта.

Полнота держится перечнем, а не памятью: ``REGISTRY`` обязан покрывать каждый
ключ из ``models.GATE_POLICY_KEYS`` и каждый ``*_KEY`` из ``project_policy``;
``unsummarised_keys`` называет расхождение, и тест роняется на нём.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import config, models
from hub import repository as repo
from hub.services import (
    auto_approve,
    executor_dispatch,
    executor_launch,
    executor_slots,
    project_policy,
    review_dispatch,
    steward_dispatch,
    steward_shadow,
)

POLICY_CHANGED_EVENT = "project_gate_policy_changed"


@dataclass(frozen=True)
class PolicyEntry:
    """Как прочитать один ключ: читатель, его имя для человека, серверный фон."""

    read: Callable[[dict], Any]
    reader: str
    #: Атрибут ``config``, из которого читатель берёт умолчание, когда ключа в
    #: проекте нет; тогда источник — сервер, а не умолчание кода.
    server_attr: str = ""
    #: Если действующее значение выведено из СОСЕДНЕГО ключа, а не прочитано
    #: из своего, возвращает «ключ=значение» этого соседа; иначе пусто.
    derived_from: Callable[[dict], str] | None = None


def _review_derived_from(policy: dict) -> str:
    """review включается делегированным вердиктом, а не собственным ключом."""
    if project_policy.review_dispatch_enabled(policy) and (
        policy.get("review") != project_policy.REVIEW_DISPATCH
    ):
        return f"verdict={policy.get('verdict')}"
    return ""


def _ceiling_name(policy: dict) -> str | None:
    ceiling = auto_approve.project_ceiling_of(policy)
    return ceiling.value if ceiling is not None else None


REGISTRY: dict[str, PolicyEntry] = {
    "dor": PolicyEntry(
        lambda p: project_policy.gate_value_of(p, "dor"),
        "project_policy.gate_value_of",
    ),
    "verdict": PolicyEntry(
        lambda p: project_policy.gate_value_of(p, "verdict"),
        "project_policy.gate_value_of",
    ),
    "review": PolicyEntry(
        lambda p: (
            project_policy.REVIEW_DISPATCH
            if project_policy.review_dispatch_enabled(p)
            else project_policy.REVIEW_OFF
        ),
        "project_policy.review_dispatch_enabled",
        derived_from=_review_derived_from,
    ),
    "risk_map": PolicyEntry(project_policy.risk_map_of, "project_policy.risk_map_of"),
    "dor_max_class": PolicyEntry(_ceiling_name, "auto_approve.project_ceiling_of"),
    "release": PolicyEntry(
        lambda p: (
            project_policy.RELEASE_AUTO
            if project_policy.release_auto_enabled(p)
            else project_policy.RELEASE_MANUAL
        ),
        "project_policy.release_auto_enabled",
    ),
    "ci_runner": PolicyEntry(
        lambda p: project_policy.ci_runner_from(p) or None,
        "project_policy.ci_runner_from",
    ),
    "steward_shadow": PolicyEntry(
        steward_dispatch.steward_shadow_of, "steward_dispatch.steward_shadow_of"
    ),
    "review_limit": PolicyEntry(
        project_policy.review_limit_of, "project_policy.review_limit_of"
    ),
    "review_limit_mode": PolicyEntry(
        project_policy.review_limit_mode_of, "project_policy.review_limit_mode_of"
    ),
    "orchestrator_queue": PolicyEntry(
        project_policy.queue_mode_of, "project_policy.queue_mode_of"
    ),
    "wip_limit": PolicyEntry(
        project_policy.wip_limit_of, "project_policy.wip_limit_of"
    ),
    "deep_daily_cap": PolicyEntry(
        review_dispatch.deep_daily_cap_of,
        "review_dispatch.deep_daily_cap_of",
        "REVIEW_DEEP_DAILY_CAP",
    ),
    "small_delta_lines": PolicyEntry(
        review_dispatch.small_delta_lines_of,
        "review_dispatch.small_delta_lines_of",
        "REVIEW_SMALL_DELTA_LINES",
    ),
    "executor_launch": PolicyEntry(
        executor_launch.launch_mode_of, "executor_launch.launch_mode_of"
    ),
    "executor_push_rights_task": PolicyEntry(
        executor_launch.push_rights_task_of, "executor_launch.push_rights_task_of"
    ),
    "executor_task_cents_ceiling": PolicyEntry(
        executor_dispatch.task_cents_ceiling_of,
        "executor_dispatch.task_cents_ceiling_of",
        "EXECUTOR_TASK_CENTS_CEILING",
    ),
    "executor_task_token_ceiling": PolicyEntry(
        executor_dispatch.task_token_ceiling_of,
        "executor_dispatch.task_token_ceiling_of",
        "EXECUTOR_TASK_TOKEN_CEILING",
    ),
    "circle_deep_stop": PolicyEntry(
        review_dispatch.circle_deep_stop_of,
        "review_dispatch.circle_deep_stop_of",
        "REVIEW_CIRCLE_DEEP_STOP",
    ),
    "submission_contract": PolicyEntry(
        project_policy.submission_contract_of, "project_policy.submission_contract_of"
    ),
    "claim_area_check": PolicyEntry(
        project_policy.claim_area_check_of, "project_policy.claim_area_check_of"
    ),
    "bug_red_test": PolicyEntry(
        project_policy.bug_red_test_of, "project_policy.bug_red_test_of"
    ),
    "statement_paths": PolicyEntry(
        project_policy.statement_paths_of, "project_policy.statement_paths_of"
    ),
    "deep_reviewer": PolicyEntry(
        project_policy.deep_reviewer_of, "project_policy.deep_reviewer_of"
    ),
    "merge_is_delivery": PolicyEntry(
        project_policy.merge_is_delivery_of, "project_policy.merge_is_delivery_of"
    ),
    "slot_dead_minutes": PolicyEntry(
        executor_slots.dead_minutes_of,
        "executor_slots.dead_minutes_of",
        "EXECUTOR_SLOT_DEAD_MINUTES",
    ),
}


def _rule_keys() -> set[str]:
    """Ключи, которые знают правила: принимаемые записью и читаемые в project_policy."""
    keys = set(models.GATE_POLICY_KEYS)
    for name in dir(project_policy):
        value = getattr(project_policy, name)
        if name.endswith("_KEY") and isinstance(value, str):
            keys.add(value)
    # Ключи ветки (release_base) живут в default_branch_policy, не в gate_policy.
    return keys - set(project_policy.DEFAULT_BRANCH_POLICY_KEYS)


def unsummarised_keys() -> list[str]:
    """Расхождение правил и сводки: ключи без записи и записи без правила."""
    rules = _rule_keys()
    return sorted((rules - set(REGISTRY)) | (set(REGISTRY) - rules))


def assert_summary_complete() -> None:
    """Падает, называя ключи, по которым сводка и правила разошлись."""
    drift = unsummarised_keys()
    if drift:
        raise AssertionError(f"ключи политики и сводка разошлись: {drift}")


def _server_backed(entry: PolicyEntry) -> bool:
    if not entry.server_attr:
        return False
    return bool(str(getattr(config, entry.server_attr, "") or "").strip())


def _key_row(key: str, entry: PolicyEntry, policy: dict) -> dict[str, Any]:
    stored = key in policy
    derived = entry.derived_from(policy) if entry.derived_from else ""
    if derived:
        source = "derived"
    elif stored:
        source = "project"
    else:
        source = "server" if _server_backed(entry) else "default"
    row: dict[str, Any] = {
        "key": key,
        "value": entry.read(policy),
        "source": source,
        "default": entry.read({}),
        "reader": entry.reader,
    }
    if stored:
        row["stored"] = policy[key]
    if derived:
        row["derived_from"] = derived
    return row


async def _steward_block(db: aiosqlite.Connection) -> dict[str, Any]:
    return await steward_shadow.mode_report(db)


def _server_block() -> dict[str, Any]:
    """Белый список настроек сервера; для секретов только «задан / не задан»."""
    return {
        "executor_model": (config.EXECUTOR_MODEL or "").strip(),
        "steward_model": config.STEWARD_MODEL,
        "reviewer_model": {
            "override": (config.CURSOR_REVIEW_MODEL or "").strip(),
            "default_pick": review_dispatch.pick_review_model(""),
        },
        "secrets_configured": {
            "steward_hub_token": bool(config.STEWARD_HUB_TOKEN),
            "cursor_reviewer_hub_token": bool(config.CURSOR_REVIEWER_HUB_TOKEN),
            "local_reviewer_hub_token": bool(config.LOCAL_REVIEWER_HUB_TOKEN),
        },
    }


def _locks_block(slug: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "#743",
            "applies": project_policy.gate_lock_applies(slug),
            "gates": list(project_policy.GATE_LOCK_GATES),
            "refused_values": sorted(project_policy.DELEGATED_VERDICTS),
            "meaning": (
                "проект default (сам хаб) не принимает делегирование ни на "
                "одном из этих гейтов; политика default всегда human"
            ),
        }
    ]


async def _last_change(
    db: aiosqlite.Connection, project_id: int
) -> dict[str, Any] | None:
    row = await repo.last_project_event(db, project_id, POLICY_CHANGED_EVENT)
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "at": row["created_at"],
        "actor": row["actor"],
        "by": payload.get("by") or "",
        "changed": list(payload.get("changed") or []),
        "removed": list(payload.get("removed") or []),
    }


async def effective_policy(db: aiosqlite.Connection, project: Any) -> dict[str, Any]:
    """Действующая политика проекта: ключи, сервер, стюард, замки, последняя правка."""
    policy = project_policy.gate_policy_of(project)
    return {
        "slug": project["slug"],
        "keys": [_key_row(key, entry, policy) for key, entry in REGISTRY.items()],
        "unknown_keys": {k: v for k, v in policy.items() if k not in REGISTRY},
        "steward": await _steward_block(db),
        "server": _server_block(),
        "locks": _locks_block(project["slug"]),
        "last_change": await _last_change(db, project["id"]),
    }


def _show(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return f"{len(value)} rule(s)"
    return str(value)


def _source_label(row: dict[str, Any]) -> str:
    if row["source"] == "derived":
        return f"derived from {row['derived_from']}"
    return row["source"]


def _stored_note(row: dict[str, Any]) -> str:
    """Сохранённое значение рядом, если оно расходится с действующим."""
    if "stored" in row and row["stored"] != row["value"]:
        return f" (stored {_show(row['stored'])})"
    return ""


def format_effective_policy(data: dict[str, Any]) -> list[str]:
    """Строки сводки — общие для CLI и MCP, как у занятости слотов."""
    lines = [f"Effective policy of project {data['slug']}"]
    for row in data["keys"]:
        lines.append(
            f"  {row['key']} = {_show(row['value'])} [{_source_label(row)}]"
            + _stored_note(row)
        )
    for key, value in (data.get("unknown_keys") or {}).items():
        lines.append(f"  {key} = {_show(value)} [unknown key]")
    steward = data["steward"]
    lines.append(
        f"Steward: requested {steward['requested']}, effective {steward['effective']}"
    )
    for refusal in steward["act_refusals"]:
        lines.append(f"  act refused: {refusal['code']} — {refusal['detail']}")
    server = data["server"]
    reviewer = server["reviewer_model"]
    lines.append(
        f"Server: executor_model {server['executor_model'] or 'not set'}, "
        f"steward_model {server['steward_model']}, reviewer_model "
        f"{reviewer['override'] or reviewer['default_pick']}"
        f"{' (override)' if reviewer['override'] else ' (default pick)'}"
    )
    secrets = server["secrets_configured"]
    lines.append(
        "Secrets: "
        + ", ".join(f"{k} {'set' if v else 'not set'}" for k, v in secrets.items())
    )
    for lock in data["locks"]:
        state = "applies" if lock["applies"] else "does not apply"
        lines.append(
            f"Lock {lock['id']}: {state} (gates {', '.join(lock['gates'])}; "
            f"refuses {', '.join(lock['refused_values'])})"
        )
    change = data.get("last_change")
    if change:
        who = change["by"] or change["actor"]
        touched = ", ".join([*change["changed"], *(f"-{k}" for k in change["removed"])])
        lines.append(f"Last change: {change['at']} by {who} ({touched or 'no keys'})")
    else:
        lines.append("Last change: none recorded")
    return lines


def format_policy_brief(data: dict[str, Any]) -> list[str]:
    """Короткий блок для hub_my_context: только то, что отличается от умолчаний."""
    shown = [r for r in data["keys"] if r["source"] in ("project", "derived")]
    parts = [
        f"{r['key']} = {_show(r['value'])} [{_source_label(r)}]" + _stored_note(r)
        for r in shown
    ]
    steward = data["steward"]
    lines = [
        f"Policy of project {data['slug']}: "
        + ("; ".join(parts) if parts else "all keys at defaults")
    ]
    lines.append(
        f"Steward: requested {steward['requested']}, effective {steward['effective']}"
        " — full view: hub_effective_policy"
    )
    return lines
