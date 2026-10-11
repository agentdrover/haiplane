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
    policy_change,
    project_policy,
    review_dispatch,
    steward_dispatch,
    steward_exit,
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
    #: Читаемый вид значения, когда общий «N rule(s)» ничего не говорит (#1594).
    show: Callable[[Any], str] | None = None


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
    "ci_before_submit": PolicyEntry(
        project_policy.ci_before_submit_of, "project_policy.ci_before_submit_of"
    ),
    "statement_paths": PolicyEntry(
        project_policy.statement_paths_of, "project_policy.statement_paths_of"
    ),
    "deep_reviewer": PolicyEntry(
        project_policy.deep_reviewer_of, "project_policy.deep_reviewer_of"
    ),
    "local_review_fallback": PolicyEntry(
        project_policy.local_review_fallback_of,
        "project_policy.local_review_fallback_of",
    ),
    "merge_is_delivery": PolicyEntry(
        project_policy.merge_is_delivery_of, "project_policy.merge_is_delivery_of"
    ),
    "path_notices": PolicyEntry(
        project_policy.path_notices_of, "project_policy.path_notices_of"
    ),
    "release_artifacts": PolicyEntry(
        project_policy.release_artifacts_of, "project_policy.release_artifacts_of"
    ),
    "freeze": PolicyEntry(
        project_policy.freeze_view_of,
        "project_policy.freeze_of",
        show=project_policy.freeze_show,
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
    if entry.show is not None:
        row["shown"] = entry.show(row["value"])
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


async def _key_changed_at(
    db: aiosqlite.Connection, project_id: int, key: str
) -> str | None:
    """Время последней правки ключа: по ленте правок политики, новейшая первой.

    Лента событий живёт ограниченно (``EVENTS_RETENTION_DAYS``): старше — None,
    то есть «записи нет», а не «правки не было».
    """
    for row in await repo.list_project_events(db, project_id, POLICY_CHANGED_EVENT):
        try:
            payload = json.loads(row["payload"] or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        touched = [*(payload.get("changed") or []), *(payload.get("removed") or [])]
        if key in touched:
            return row["created_at"]
    return None


async def _locks_block(
    db: aiosqlite.Connection, project: Any, policy: dict[str, Any]
) -> list[dict[str, Any]]:
    """Замок #743: разрешённые и запрещённые пары, решение владельца отдельно
    от того, что на проекте стоит на деле и с какого времени (#1602)."""
    slug = project["slug"]
    allowed = project_policy.gate_lock_allowed_pairs()
    return [
        {
            "id": "#743",
            "applies": project_policy.gate_lock_applies(slug),
            "gates": list(project_policy.GATE_LOCK_GATES),
            "allowed": allowed,
            "allowed_note": {
                pair: project_policy.GATE_LOCK_OWNER_DECISION for pair in allowed
            },
            "refused": project_policy.gate_lock_refused_pairs(),
            "actual": {
                gate: project_policy.gate_value_of(policy, gate)
                for gate in project_policy.GATE_LOCK_GATES
            }
            | {
                "changed_at": await _key_changed_at(db, int(project["id"]), "verdict"),
            },
            "meaning": (
                "проект default (сам хаб) не принимает делегирование на "
                "этих гейтах, кроме разрешённых пар; автопилот на default "
                "вердикт не ставит никогда"
            ),
        }
    ]


async def _steward_verdicts_block(
    db: aiosqlite.Connection, slug: str
) -> dict[str, Any]:
    """Фактические вердикты стюарда на проекте (#1602); теневые не считаются."""
    window = steward_exit.STEWARD_VERDICT_WINDOW_DAYS
    counts = await steward_exit.actual_steward_verdicts(db, window)
    return {"count": counts.get(slug, 0), "window_days": window}


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
        # #1593: «было → стало» и id записи расписания, если правку исполнил хаб.
        "changes": payload.get("changes") or {},
        "schedule_id": payload.get("schedule_id"),
        "late": bool(payload.get("late")),
    }


def _scheduled_block(
    policy: dict[str, Any], rows: list[Any]
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Ожидающие правки (#1593): список и разбивка по ключам реестра.

    Значение «после» считает тот же читатель, что и сводка, на политике, к
    которой правки применены по порядку (at, id): две правки одного ключа
    показывают цепочку, а не две независимые подмены.
    """
    pending = [policy_change.view_row(r) for r in rows]
    running = dict(policy)
    by_key: dict[str, list[dict[str, Any]]] = {}
    for item in pending:
        for key, value in item["patch"].items():
            if value is None:
                running.pop(key, None)
            else:
                running[key] = value
            entry = REGISTRY.get(key)
            if entry is None:
                continue
            planned = entry.read(running)
            by_key.setdefault(key, []).append(
                {
                    "id": item["id"],
                    "at": item["at"],
                    "value": planned,
                    "shown": entry.show(planned) if entry.show else None,
                    "note": item["note"],
                }
            )
    return pending, by_key


def key_rows(project: Any) -> list[dict[str, Any]]:
    """Строки ключей реестра для проекта: значение, источник, умолчание (#1638).

    Единственный расчёт строк; без обращений к базе. Им пользуются и сводка
    ``effective_policy``, и форма проекта, которой нужны только ключи без
    расписания, истории и стюарда.
    """
    policy = project_policy.gate_policy_of(project)
    return [_key_row(key, entry, policy) for key, entry in REGISTRY.items()]


async def effective_policy(db: aiosqlite.Connection, project: Any) -> dict[str, Any]:
    """Действующая политика проекта: ключи, сервер, стюард, замки, последняя правка."""
    policy = project_policy.gate_policy_of(project)
    pending, by_key = _scheduled_block(
        policy,
        await repo.list_scheduled_policy_changes(
            db, int(project["id"]), state="pending"
        ),
    )
    keys = key_rows(project)
    for row in keys:
        if row["key"] in by_key:
            row["scheduled"] = by_key[row["key"]]
    refused = await repo.list_scheduled_policy_changes(
        db, int(project["id"]), state="refused"
    )
    return {
        "slug": project["slug"],
        "policy_version": policy_change.policy_version(project),
        "scheduled": pending,
        "scheduled_refused": [policy_change.view_row(r) for r in refused[-3:]],
        "keys": keys,
        "unknown_keys": {k: v for k, v in policy.items() if k not in REGISTRY},
        "steward": await _steward_block(db),
        "server": _server_block(),
        "locks": await _locks_block(db, project, policy),
        "steward_verdicts": await _steward_verdicts_block(db, project["slug"]),
        "last_change": await _last_change(db, project["id"]),
    }


def _show(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return f"{len(value)} rule(s)"
    return str(value)


def _source_label(row: dict[str, Any]) -> str:
    if row["source"] == "derived":
        return f"derived from {row['derived_from']}"
    return row["source"]


def _row_value(row: dict[str, Any]) -> str:
    """Значение ключа для человека: свой вид ключа, иначе общий."""
    return row["shown"] if "shown" in row else _show(row["value"])


def _stored_note(row: dict[str, Any]) -> str:
    """Сохранённое значение рядом, если оно расходится с действующим."""
    if "shown" in row:
        return ""
    if "stored" in row and row["stored"] != row["value"]:
        return f" (stored {_show(row['stored'])})"
    return ""


def format_effective_policy(data: dict[str, Any]) -> list[str]:
    """Строки сводки — общие для CLI и MCP, как у занятости слотов."""
    lines = [f"Effective policy of project {data['slug']}"]
    for row in data["keys"]:
        lines.append(
            f"  {row['key']} = {_row_value(row)} [{_source_label(row)}]"
            + _stored_note(row)
        )
        for plan in row.get("scheduled") or []:
            lines.append(
                f"    → {plan.get('shown') or _show(plan['value'])} с {plan['at']} "
                f"(отложенная правка #{plan['id']})"
            )
    for key, value in (data.get("unknown_keys") or {}).items():
        lines.append(f"  {key} = {_show(value)} [unknown key]")
    steward = data["steward"]
    lines.append(
        f"Steward: requested {steward['requested']}, effective {steward['effective']}"
    )
    for refusal in steward["act_refusals"]:
        lines.append(f"  act refused: {refusal['code']} — {refusal['detail']}")
    contour = steward.get("contour")
    if contour:
        # #1601: счётчики пары судья+советник — те же, что в practice_metrics.
        lines.append(
            f"  pairs {contour['pairs']} (concur {contour['concur']}, object "
            f"{contour['object']}, timeout {contour['timeout']}), false_approve "
            f"{contour['false_approve']}"
            + "".join(
                f" #{item['task_id']} [{item['source']}]"
                for item in contour["false_approve_tasks"]
            )
            + f", procedural escalations {contour['procedural_escalations']}"
        )
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
            f"allows {', '.join(lock['allowed'])}; "
            f"refuses {', '.join(lock['refused'])})"
        )
        if lock["applies"]:
            for pair, note in lock["allowed_note"].items():
                lines.append(f"  {pair} allowed: {note}")
            actual = lock["actual"]
            lines.append(
                "  actual: "
                + ", ".join(f"{g}={actual[g]}" for g in lock["gates"])
                + f"; verdict changed at {actual['changed_at'] or 'not recorded'}"
            )
    verdicts = data["steward_verdicts"]
    lines.append(
        f"Steward verdicts: {verdicts['count']} in {verdicts['window_days']} days "
        "(recorded verdicts only; shadow and DoR judgements are not counted)"
    )
    change = data.get("last_change")
    if change:
        who = change["by"] or change["actor"]
        touched = ", ".join([*change["changed"], *(f"-{k}" for k in change["removed"])])
        lines.append(f"Last change: {change['at']} by {who} ({touched or 'no keys'})")
        if change.get("schedule_id") is not None:
            lines.append(
                f"  executed from schedule #{change['schedule_id']}"
                + (" (late)" if change.get("late") else "")
                + ": "
                + (policy_change.render_changes(change["changes"]) or "no changes")
            )
    else:
        lines.append("Last change: none recorded")
    for item in data.get("scheduled") or []:
        lines.append(
            f"Scheduled #{item['id']} at {item['at']}: "
            + json.dumps(item["patch"], ensure_ascii=False, sort_keys=True)
            + (f" — {item['note']}" if item["note"] else "")
        )
    for item in data.get("scheduled_refused") or []:
        lines.append(
            f"Scheduled #{item['id']} REFUSED at {item['executed_at']}: "
            f"{item['result'].get('reason', '')}"
        )
    return lines


def format_policy_brief(data: dict[str, Any]) -> list[str]:
    """Короткий блок для hub_my_context: только то, что отличается от умолчаний."""
    shown = [r for r in data["keys"] if r["source"] in ("project", "derived")]
    parts = [
        f"{r['key']} = {_row_value(r)} [{_source_label(r)}]" + _stored_note(r)
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
