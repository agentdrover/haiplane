"""Стартовый пакет проекта (#1631): первое сообщение агенту без credentials.

Ответ строится ТОЛЬКО из перечисленных ниже полей: запрос не читается (ни
bearer, ни cookie), ``auth`` и ``config`` не сериализуются, ``workspace_path``
проекта не отдаётся, а у файлов правил наружу идёт одно состояние — причина
чтения может нести путь сервера. В конфиге MCP токен — плейсхолдер.

Текст рендерится здесь, один раз, из той же модели; CLI печатает его как есть.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

import aiosqlite

from hub import config
from hub import repository as repo
from hub.hub_instance import hub_base_url
from hub.models import (
    AgentBootstrap,
    BootstrapMcpConfig,
    BootstrapRulesFile,
    BootstrapSkill,
)
from hub.services import project_policy, working_rules
from hub.services.effective_policy import effective_policy
from hub.workflow_reference import HUMAN_ONLY_TOOLS, HUMAN_ROUTES

MCP_SERVER_NAME = "haiplane-hub"
TOKEN_PLACEHOLDER = "<ТОКЕН АГЕНТА>"  # nosec B105 - плейсхолдер, не секрет
REVIEW_RULES_FILE = ".hub/REVIEW_RULES.md"
CI_LINE = "push → зелёный CI на sha → сдача"

_FIRST_CALLS = (
    "hub_whoami — принципал и роль",
    'hub_my_context(project="{slug}", mode="full") — карта переходов и гейты',
    'hub_session_register(session_id="<id вашей сессии>") — до claim',
    'hub_list_tasks(project="{slug}") — задачи проекта',
    "hub_my_context(task_id=<id задачи>) — контекст выбранной задачи",
    'hub_claim_task(task_id=<id>, agent="<имя из hub_whoami>", session_id="<тот же id>")'
    " — agent обязателен",
    'hub_pair_start(task_id=<id>, assigned_agent="<то же имя, что в claim>", '
    'session_id="<тот же id>", git_mode="remote") — assigned_agent должен '
    "совпасть с держателем claim",
)


#: Ключи политики со свободным текстом или структурой (команды, пути, заметки):
#: наружу идёт только факт «настроен / не настроен», содержимое не публикуется.
_FREE_TEXT_KEYS = frozenset(
    {"ci_runner", "path_notices", "release_artifacts", "freeze", "risk_map"}
)
_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
URL_WITHHELD_NOTE = (
    "URL хаба не опубликован: он содержит учётные данные, параметры или "
    "нестандартную схему. Возьмите URL у владельца."
)


def _safe_hub_url(raw: str) -> str:
    """URL хаба для публикации или пустая строка; исходная строка не возвращается."""
    try:
        parts = urlsplit(raw)
        _ = parts.port  # неверный порт бросает ValueError
    except ValueError:
        return ""
    bad = (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or ";" in parts.path
    )
    if bad:
        return ""
    return raw.rstrip("/")


def _safe_value(row: dict[str, Any]) -> str:
    value = row.get("value")
    if row["key"] in _FREE_TEXT_KEYS:
        return "настроен" if value else "не настроен"
    if isinstance(value, bool) or value is None:
        return "-" if value is None else ("true" if value else "false")
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and _TOKEN.match(value):
        return value
    return "задан (значение не публикуется)"


def safe_policy_brief(data: dict[str, Any]) -> list[str]:
    """Brief политики для пакета: только безопасные значения (не format_policy_brief)."""
    shown = [r for r in data["keys"] if r["source"] in ("project", "derived")]
    parts = [f"{r['key']} = {_safe_value(r)} [{r['source']}]" for r in shown]
    steward = data["steward"]
    mode = [str(steward.get(k, "")) for k in ("requested", "effective")]
    mode = [m if _TOKEN.match(m) else "?" for m in mode]
    return [
        f"Policy of project {data['slug']}: "
        + ("; ".join(parts) if parts else "all keys at defaults"),
        f"Steward: requested {mode[0]}, effective {mode[1]}",
    ]


def _submit_order(ci_mode: str) -> list[str]:
    steps = [
        "работа в ветке task-<id>/<slug>, по одной задаче за раз",
        'push полным refspec "${B}:refs/heads/${B}", без force',
    ]
    if ci_mode in (project_policy.RED_TEST_WARN, project_policy.RED_TEST_REQUIRE):
        steps.append(CI_LINE)
    steps.append(
        "hub_submit_for_review — изменение сданного кода: новый коммит, пуш и "
        "новая сдача (новое поколение, прежний вердикт не текущий, #1054); "
        "после changes_requested исправить, запушить и сдать новый sha; повтор "
        "из review с тем же sha сохраняет поколение, статус и текущесть "
        "вердикта; finding_outcomes, accept_areas и решение о заказе ревью "
        "могут обновиться (#1265); при ошибке транспорта сначала "
        "hub_task_status; после APPROVED хаб вливает сам"
    )
    return steps


def _human_only() -> list[str]:
    return [
        f"{HUMAN_ROUTES[tool]} — передать человеку"
        for tool in HUMAN_ONLY_TOOLS
        if tool in HUMAN_ROUTES
    ]


async def _active_skills(db: aiosqlite.Connection) -> list[BootstrapSkill]:
    rows = await repo.list_skills(db)
    return [
        BootstrapSkill(name=str(r["name"]), version=int(r["version"]))
        for r in rows
        if r["status"] == "active"
    ]


async def _rules_file(project: Any, path: str) -> BootstrapRulesFile:
    raw = await working_rules.read_project_base_file(project, path)
    state = str(raw.get("state") or "unreadable")
    if state not in ("present", "missing", "unreadable"):
        state = "unreadable"
    return BootstrapRulesFile(path=path, state=state)


def _state_note(f: BootstrapRulesFile) -> str:
    if f.state == "unreadable":
        return "unreadable (прочитать не удалось; это не значит, что файла нет)"
    return f.state


def render_agent_bootstrap(b: AgentBootstrap) -> str:
    """Единый серверный текст пакета: шесть разделов в фиксированном порядке."""
    where = f"репозиторий {b.repo or '—'}, базовая ветка {b.base_branch or '—'}"
    lines = [
        f"# Стартовый пакет проекта {b.project_slug} ({b.project_name})",
        "",
        "Задачами управляет Haiplane Hub"
        + (f" ({b.mcp.url.removesuffix('/mcp')})" if b.mcp.url else "")
        + ". Токен агента выдаёт владелец; в этом тексте его нет и быть не должно.",
        "",
        "## 1. Конфиг MCP",
        "```json",
        "{",
        '  "mcpServers": {',
        f'    "{b.mcp.server_name}": {{',
        f'      "type": "{b.mcp.transport}",',
        f'      "url": "{b.mcp.url or "<URL ХАБА>"}",',
        f'      "headers": {{"Authorization": "{b.mcp.authorization}"}}',
        "    }",
        "  }",
        "}",
        "```",
        *([b.mcp.url_note] if b.mcp.url_note else []),
        f"Вместо {TOKEN_PLACEHOLDER} подставьте токен локально; в чат и в логи его не печатайте.",
        "",
        "## 2. Первые вызовы",
    ]
    lines += [f"{i}. {c}" for i, c in enumerate(b.first_calls, 1)]
    lines += ["", "## 3. Активные навыки хаба"]
    lines += (
        [f"- {s.name} v{s.version}" for s in b.skills]
        if b.skills
        else ["- активных навыков нет"]
    )
    lines += ["", "## 4. Политика проекта", *b.policy_brief]
    lines += ["", f"## 5. Файлы правил ({where})"]
    lines += [f"- {f.path}: {_state_note(f)}" for f in b.rules_files]
    lines += ["", "## 6. Порядок сдачи и человеческие гейты"]
    lines += [f"{i}. {s}" for i, s in enumerate(b.submit_order, 1)]
    lines += ["", "Только человек (агент не вызывает — передать человеку):"]
    lines += [f"- {h}" for h in b.human_only]
    lines += [
        "",
        "Ошибка транспорта MCP не значит, что запись не прошла: сначала "
        "hub_task_status, потом повтор.",
    ]
    return "\n".join(lines)


async def build_agent_bootstrap(
    db: aiosqlite.Connection, project: Any
) -> AgentBootstrap:
    """Собрать пакет проекта из разрешённых источников; ничего не пишет."""
    slug = str(project["slug"])
    hub_url = _safe_hub_url(hub_base_url())
    ci_mode = project_policy.ci_before_submit_of(project_policy.gate_policy_of(project))
    bootstrap = AgentBootstrap(
        project_slug=slug,
        project_name=str(project["name"] or slug),
        repo=str(project["repo"] or "").strip(),
        base_branch=(
            str(project["default_branch"] or "").strip() or config.PAIR_BASE_BRANCH
        ),
        mcp=BootstrapMcpConfig(
            server_name=MCP_SERVER_NAME,
            transport="streamable-http",
            url=f"{hub_url}/mcp" if hub_url else "",
            url_note="" if hub_url else URL_WITHHELD_NOTE,
            authorization=f"Bearer {TOKEN_PLACEHOLDER}",
        ),
        first_calls=[c.format(slug=slug) if "{slug}" in c else c for c in _FIRST_CALLS],
        skills=await _active_skills(db),
        policy_brief=safe_policy_brief(await effective_policy(db, project)),
        rules_files=[
            await _rules_file(project, working_rules.AGENT_RULES_FILE),
            await _rules_file(project, REVIEW_RULES_FILE),
        ],
        ci_before_submit=ci_mode,
        submit_order=_submit_order(ci_mode),
        human_only=_human_only(),
        text="",
    )
    bootstrap.text = render_agent_bootstrap(bootstrap)
    return bootstrap
