# Агентский каталог MCP (#1624)

Каждый агент на каждом ходу платит за весь `tools/list`. Поэтому каталог
делится на два вида, а бюджет меряет тот, который платит агент.

## Два вида

| Вид | Кто получает | Что внутри |
|---|---|---|
| `agent` | агентский токен, watcher, steward, open mode, stdio, код без запроса | всё, кроме шести human-only гейтов |
| `full` | человек: роль `human`, `admin`, `super_admin` или право `tasks.human_gate` (кроме watcher и steward) | всё |

Кто человек, решает `TokenIdentity.is_human` из защищённой ветки
`hub/auth.py` (после ограничений ролей). Флаг кладётся в контекст MCP
(`hub/mcp_internal_auth.py`, `identity_is_human()`); роль для этого не годится:
кастомная роль с `tasks.human_gate` — человек, watcher с тем же правом — нет.

Решение принимается на каждом запросе: `InstrumentedFastMCP.list_tools`
(`hub/mcp_server.py`) строит свежий список из `list_tools_for(view)`; менеджер
инструментов и кэш SDK не меняются.

## Скрытые инструменты (`AGENT_HIDDEN_TOOLS`, `hub/workflow_reference.py`)

| Инструмент | Путь человека |
|---|---|
| `hub_approve_task` | `POST /api/tasks/{id}/approve` |
| `hub_reject_task` | `POST /api/tasks/{id}/reject` |
| `hub_decide_task` | `POST /api/tasks/{id}/decide` |
| `hub_force_complete_task` | `POST /api/tasks/{id}/force-complete` |
| `hub_answer_question` | `POST /api/tasks/{id}/answer` |
| `hub_start_task` | `POST /api/tasks/{id}/start` |

`hub_create_project` тоже human-only, но в этот список не входит.

Вызов скрытого инструмента не-человеком отказан внутри измеряемого пути
(`InstrumentedFastMCP.call_tool`, до инструмента): `isError=true`, JSON-конверт
`reason=human_only_gate`, `actor_hint=human`, `next_action` с маршрутом REST,
одна запись телеметрии, REST не вызывается. REST остаётся последней защитой (403).

## Удалено

`hub_approve_proposal`, `hub_reject_proposal` (REST `/api/proposals` остаётся) и
`hub_submit_steward_judgement` (стюард в `/mcp` не допускается; суждение уходит
`POST /api/tasks/{id}/steward-judgement`, команда — в блоке доступа промта
стюарда, `hub/services/steward_shadow.py`).

## Бюджет

- `scripts/mcp_catalog_budget.py` и проверка CI меряют вид `agent`.
- `/api/metrics/mcp-usage` и страница `/metrics/agent-api` (usage) читают вид
  `full`: `unused_tools` видит скрытые инструменты тоже.
- `/api/metrics/mcp-catalog`: верхний уровень — вид `agent` (контракт прежний),
  плюс `view` и `views.{agent,full}` с числами обоих видов.
- `WORKING_FREEZE` описаний снижен до 35600 строкой `WORKING_FREEZE_HISTORY`
  (потолки `budgets` не тронуты, `--update` не запускался).

## Тексты для агента

Тексты, которые код хаба формирует для агента, не называют скрытые и
удалённые инструменты: вместо имени стоит путь человека (REST/UI/`oc-hub`).
Страж: `tests/test_mcp_envelope.py::test_agent_facing_texts_name_no_hidden_tools`
(реальные генераторы плюс разбор строковых литералов `hub/`; исключения заданы
по файлам явно).
