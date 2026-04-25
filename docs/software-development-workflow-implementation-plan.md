# План внедрения процесса human + AI-agent разработки

## Цель

Превратить описанный в `docs/software-development-workflow.md` процесс в рабочий Cursor + OpenClaw Hub workflow. План разбит на этапы так, чтобы сначала закрыть быстрые контрактные разрывы без миграций, а затем перейти к более крупным изменениям модели данных и UI.

## Этап 1. Минимальный Cursor workflow без миграций

### 1.1. MCP parity для approval ✅ done (2026-04-24)

**Итог**

- `hub/mcp_server.py`: `hub_approve_task(..., force=False)` с обновлённым docstring, `force` прокидывается в `POST /api/tasks/{id}/approve`.
- `tests/test_mcp_server.py::test_hub_approve_task_passes_force` покрывает контракт.
- Примечание в `docs/software-development-workflow.md` обновлено: force approve доступен единым контрактом через MCP / REST / CLI / Web UI, является audited human override.



**Проблема**

API, CLI и Web поддерживают `force=true` для `approve`, но MCP `hub_approve_task` пока не принимает `force`. Поэтому из Cursor нельзя выполнить контролируемый force approve через тот же инструмент, которым агент утверждает задачу.

**Изменения**

- В `hub/mcp_server.py`:
  - добавить параметр `force: bool = False` в `hub_approve_task`;
  - передавать `"force": force` в тело `POST /api/tasks/{task_id}/approve`;
  - обновить docstring инструмента.
- В `tests/test_mcp_server.py`:
  - добавить проверку, что `hub_approve_task(..., force=True)` передает `force` в API.

**Валидация**

```bash
uv run pytest tests/test_mcp_server.py -q
```

### 1.2. MCP tool для force-complete pending report ✅ done (2026-04-24)

**Итог**

- REST endpoint `POST /api/tasks/{task_id}/force-complete` в `hub/app.py`, сервис `services.force_complete_task` в `hub/services/lifecycle.py`, MCP tool `hub_force_complete_task(task_id, comment="")` в `hub/mcp_server.py`, CLI `hub force-complete` в `hub/cli.py`.
- Web UI parity закрыта: `POST /tasks/{id}/web-force-complete` теперь принимает комментарий через htmx `hx-prompt` (`HX-Prompt` header) или form field `comment`, кнопка в `hub/templates/partials/inbox.html` переведена на `hx-prompt`.
- Тесты: `test_services.py`, `test_api.py`, `test_mcp_server.py`, `test_cli.py`, `test_web.py` (`test_web_force_complete_records_hx_prompt`, `test_web_force_complete_falls_back_to_form_comment`).


**Проблема**

Web UI умеет явно закрывать `pending_report` через force-complete, а Cursor MCP нет. Для процесса контроля результатов это оставляет дыру: человек в Cursor видит слабый/отсутствующий отчет, но не может явно принять ответственность через MCP.

**Изменения**

- В `hub/mcp_server.py`:
  - добавить `hub_force_complete_task(task_id: int)`;
  - инструмент должен вызывать существующий API/Web-backed service route, если REST route уже есть;
  - если REST route отсутствует, добавить REST endpoint вместо вызова web route из MCP.
- Если добавляется REST endpoint:
  - `hub/app.py`: `POST /api/tasks/{task_id}/force-complete`;
  - использовать существующий `services.force_complete_task`;
  - тесты в `tests/test_api.py` или `tests/test_services.py`;
  - MCP test в `tests/test_mcp_server.py`.

**Валидация**

```bash
uv run pytest tests/test_mcp_server.py tests/test_api.py tests/test_services.py -q
```

### 1.3. Cursor agent rules ✅ done (2026-04-24)

**Итог**

- `docs/cursor-agent-rules.md` — 8 обязательных правил + раздел human gates + minimum MCP tools.
- `docs/templates/cursor/openclaw-hub.mdc` — installable Cursor rule со `alwaysApply: true`.



**Проблема**

Процесс описан в общем workflow-документе, но агентам Cursor нужен короткий, исполняемый набор правил.

**Изменения**

- Добавить `docs/cursor-agent-rules.md`.
- Зафиксировать правила:
  - перед работой вызвать `hub_my_context(task_id)`;
  - если задача не готова, не писать код, а предложить refine/AC/risk;
  - перед стартом оставить `Plan:`;
  - вопросы через `hub_ask_question`;
  - блокеры через `hub_task_update(..., kind="blocker")`;
  - завершение через `hub_report_done`;
  - новые найденные работы только через draft proposal;
  - не закрывать задачу при failed CI, unresolved blocker или review changes.
- Добавить шаблон `.cursor/rules/openclaw-hub.mdc` или `docs/templates/cursor/openclaw-hub.mdc`, если не хочется навязывать Cursor-файлы в корне.

**Валидация**

Документальная проверка. Тесты не нужны.

### 1.4. Уточнить role prompts ✅ done (2026-04-24)

**Итог**

Все четыре `agents/*.md` получили секцию `Hub Lifecycle Duties`:
- `architect-analyst.md` — refine / AC / risks / readiness, force=true как осознанный human override.
- `python-senior-developer.md` — `hub_my_context`, `Plan:`, done report с changed files / behavior / validation.
- `testing-agent.md` — validation commands возвращаются в update/report, failed validation → blocker.
- `code-reviewer.md` — проверка diff vs acceptance criteria / validation / contract surfaces; `hub_decide_task` остаётся human gate.



**Проблема**

`agents/*.md` уже описывают роли, но не закрепляют обязательные MCP-действия и lifecycle-gates.

**Изменения**

- `agents/architect-analyst.md`:
  - добавить обязанность доводить draft до passing DoR;
  - явно использовать refine, AC, risks, readiness.
- `agents/python-senior-developer.md`:
  - перед работой читать `hub_my_context`;
  - перед стартом оставлять `Plan:`;
  - done report должен включать changed files, behavior, validation.
- `agents/testing-agent.md`:
  - validation commands должны возвращаться в task update/done report;
  - failed validation должен становиться blocker или review finding.
- `agents/code-reviewer.md`:
  - проверять не только diff, но и соответствие acceptance criteria, validation и contract surfaces.

**Валидация**

Документальная проверка. Тесты не нужны.

### 1.5. README entrypoint ✅ done (2026-04-24)

**Итог**

В `README.md` добавлен раздел `Cursor + Hub Workflow` со ссылками на `docs/software-development-workflow.md`, `docs/software-development-workflow-implementation-plan.md`, `docs/cursor-agent-rules.md`, шаблон `docs/templates/cursor/openclaw-hub.mdc` и минимальный набор MCP tools.



**Проблема**

В README уже есть ссылка на общий workflow, но нет короткого entrypoint для команды, которая стартует работу из Cursor.

**Изменения**

- В `README.md` добавить короткий раздел `Cursor + Hub workflow`:
  - как запустить hub;
  - какой документ читать;
  - какие MCP tools являются обязательными;
  - где лежат Cursor rules.

**Валидация**

Документальная проверка. Тесты не нужны.

## Этап 2. Улучшение контроля команды

### 2.1. Human owner / reviewer fields ✅ done (2026-04-25)

**Итог**

- `human_owner` и `human_reviewer` добавлены в task schema, REST API, refine payload, repository, migrations и `TaskView`.
- CLI и MCP умеют задавать поля при create/propose, уточнять через refine и фильтровать список задач.
- Web UI показывает owner/reviewer в списке и detail view, поддерживает фильтры и создание задач с этими полями.
- Тесты покрывают модели, миграции, repository, REST API, refine, CLI, MCP и Web UI.

**Проблема**

Для team queue и agent swarm нужна явная ответственность человека: кто владелец задачи, кто принимает результат.

**Изменения**

- Добавить поля, например:
  - `human_owner: str`;
  - `human_reviewer: str`.
- Затронутые файлы:
  - `hub/models.py`;
  - `hub/db.py` migrations;
  - `hub/repository.py`;
  - `hub/app.py`;
  - `hub/cli.py`;
  - `hub/mcp_server.py`;
  - `hub/web.py` и templates;
  - тесты.

**Контрактное правило**

Это schema/API contract change. Нужно обновлять API, CLI, MCP и тесты в одном проходе.

**Валидация**

```bash
uv run pytest tests/test_models.py tests/test_repository_structured.py tests/test_db_migrations.py -q
uv run pytest tests/test_api.py tests/test_cli.py tests/test_mcp_server.py tests/test_web.py -q
```

### 2.2. Decision capture flow ✅ done (2026-04-25)

**Итог**

- `TaskDecide` расширен параметрами `decision_summary: str` и `record_decision: bool`.
- `lifecycle.decide_task` записывает `decision_summary` в task update log (kind=`decision`) независимо от notes.
- При `record_decision=True` summary сохраняется через `NotesPlugin.save_decision` (если доступен); noop adapter возвращает None, flow не ломается.
- `NotesPlugin` protocol расширен методом `save_decision`; noop и concrete (`notes.py`) реализации добавлены.
- MCP `hub_decide_task` принимает `decision_summary` и `record_decision`; docstring описывает Decision Gate.
- CLI `oc-hub decide` принимает `--summary` и `--record-decision`.
- Web UI для `needs_decision` содержит textarea для `decision_summary` и checkbox `record_decision`.
- Template `task_detail.html` рендерит `decision` kind в updates thread.
- Тесты: `test_services.py` (5 новых), `test_api.py` (4 новых), `test_mcp_server.py` (2 новых), `test_cli.py` (3 новых), `test_web.py` (2 новых), `test_notes_integration.py` (payload контракта `notes_decision_save`).

**Проблема**

После `needs_decision` человек принимает решение, но процесс сохранения причины и контекста в notes пока не является явной частью UI/MCP.

**Изменения**

- `hub/models.py`: `TaskDecide` + `decision_summary`, `record_decision`.
- `hub/services/lifecycle.py`: `decide_task` пишет decision update и опционально вызывает `plugins.notes.save_decision`.
- `hub/integrations/protocols.py`: `NotesPlugin.save_decision`.
- `hub/integrations/noop.py`: `NoopNotes.save_decision` → `None`.
- `hub/integrations/notes.py`: `NotesIntegration.save_decision` → `notes_decision_save`.
- `hub/mcp_server.py`: `hub_decide_task` + новые параметры и docstring.
- `hub/cli.py`: `cmd_decide` + `--summary`, `--record-decision`; parser обновлён.
- `hub/web.py`: `web_decide_task` принимает `decision_summary`, `record_decision`.
- `hub/templates/task_detail.html`: форма с textarea для summary, checkbox; рендер `decision` kind.
- `tests/test_notes_integration.py`: concrete notes adapter отправляет обязательные поля `decision` и `why`.
- Тесты: `tests/test_services.py`, `tests/test_api.py`, `tests/test_mcp_server.py`, `tests/test_cli.py`, `tests/test_web.py`, `tests/test_notes_integration.py`.

**Валидация**

```bash
uv run pytest tests/test_mcp_server.py tests/test_api.py tests/test_web.py -q
uv run pytest tests/test_cli.py tests/test_services.py -q
uv run pytest -q
```

### 2.3. Workspace safety policy ✅ done (2026-04-24)

**Итог**

- `docs/workspace-safety-policy.md` — 4 инварианта: один branch на задачу, неприкосновенность чужого branch, out-of-scope только через draft proposal, конфликт branch/PR/CI → `needs_decision`. Секции по ролям и аудиту.
- Ссылки: `docs/software-development-workflow.md` (секция «Branch, PR и CI»), `docs/cursor-agent-rules.md` (вступление), `docs/templates/cursor/openclaw-hub.mdc` (правило branch isolation), `README.md` (секция «Cursor + Hub Workflow»).

**Проблема**

Для нескольких агентов нужна формальная branch isolation policy, чтобы они не мешали друг другу в workspace repo.

**Изменения**

- Добавить `docs/workspace-safety-policy.md`.
- Зафиксировать:
  - одна исполняемая задача - один branch;
  - agent не меняет чужой branch без явной human decision;
  - новая работа за пределами scope оформляется draft proposal;
  - конфликт branch/PR/CI переводит задачу в `needs_decision`.
- Добавить ссылки из workflow и Cursor rules.

**Валидация**

Документальная проверка. Тесты не нужны.

## Этап 3. Надежность параллельных агентов

### 3.1. Dedicated risk endpoint ✅ done (2026-04-25)

**Итог**

- Добавлен REST endpoint `POST /api/tasks/{task_id}/risks`, который принимает один `TaskRisk` и возвращает обновленный `TaskView`.
- `hub.repository.append_task_risk` делает атомарный SQL append через `json_insert(... '$[#]' ...)`, без client-side read-modify-write.
- `hub.services.refinement.add_risk` оборачивает append в SAVEPOINT и возвращает 404 для неизвестной задачи.
- MCP `hub_add_risk` и CLI `oc-hub risk add` переведены на dedicated endpoint.
- Тесты покрывают API success/404/422, CLI/MCP endpoint contract и repository append без замены существующих рисков.

**Проблема**

MCP `hub_add_risk` сейчас делает read-modify-write через `/refine`. При параллельных агентах возможна потеря обновления: last writer wins.

**Изменения**

- Добавить endpoint:
  - `POST /api/tasks/{task_id}/risks`;
  - append одного риска в транзакции.
- Добавить service/repository helper для атомарного append.
- Обновить `hub_add_risk`, чтобы он использовал новый endpoint.
- При необходимости добавить CLI команду `risk add`, если текущая тоже использует read-modify-write.

**Затронутые файлы**

- `hub/models.py`;
- `hub/app.py`;
- `hub/repository.py`;
- `hub/cli.py`;
- `hub/mcp_server.py`;
- `tests/test_api_refine.py` или новый тестовый блок;
- `tests/test_mcp_server.py`;
- `tests/test_cli.py`.

**Валидация**

```bash
uv run pytest tests/test_api_refine.py tests/test_mcp_server.py tests/test_cli.py -q
```

### 3.2. Review checklist field ✅ done (2026-04-25)

**Итог**

- Поле `review_checklist: list[str]` добавлено в `TaskCreate`, `TaskRefine` (PATCH-семантика: omit оставляет значение, `[]` явно очищает) и `TaskView` в `hub/models.py`.
- `hub/db.py`: миграция `add_review_checklist_column` (TEXT NOT NULL DEFAULT '[]'), запись в `LIST_STR_COLUMNS` и `STRUCTURED_TASK_FIELDS`. Repository / lifecycle / refinement service не требовали явных правок — они автоматически подбирают новое поле через `structured_fields_to_db` / `structured_fields_from_row`.
- CLI: `oc-hub refine --review-check "..."` (repeatable) через `_REFINE_LIST_FIELDS`.
- MCP: `hub_refine_task(..., review_checklist=...)` форвардит поле только если оно не None.
- Web: `hub/templates/task_detail.html` рендерит секцию `Review Checklist` только при непустом списке. UI создания задач не расширялся (минимальный паттерн).
- DoR не менялся, `out_of_scope_for_review` остаётся независимым полем.
- Тесты: `tests/test_models.py` (defaults / max_length / PATCH semantics), `tests/test_db_migrations.py` (column + default), `tests/test_repository_structured.py` (round-trip + replace/omit/clear), `tests/test_api_refine.py`, `tests/test_cli.py`, `tests/test_mcp_server.py`, `tests/test_web.py` (рендер при наличии и отсутствие при пустом списке). Полный `uv run pytest -q` — 390 passed.

**Проблема**

Acceptance criteria отвечают на вопрос "как понять, что задача выполнена", а reviewer checklist отвечает на вопрос "что дополнительно проверить в diff". Сейчас это смешивается через описание, hints и `out_of_scope_for_review`.

**Изменения**

- Добавить поле вроде `review_checklist: list[str]`.
- Обновить DoR только если поле становится required для некоторых `work_type`.
- Протащить через models/db/repository/API/CLI/MCP/Web.

**Контрактное правило**

Это schema/API change. Делать отдельной задачей с миграцией и тестами.

**Валидация**

```bash
uv run pytest tests/test_models.py tests/test_repository_structured.py tests/test_db_migrations.py -q
uv run pytest tests/test_api_refine.py tests/test_cli.py tests/test_mcp_server.py tests/test_web.py -q
```

## Рекомендуемый порядок запуска доработки

1. Этап 1.1: MCP `force` parity.
2. Этап 1.2: MCP/API force-complete для `pending_report`.
3. Этап 1.3-1.5: Cursor rules, role prompts, README.
4. Этап 2.3: workspace safety policy.
5. Этап 2.1: owner/reviewer fields.
6. Этап 2.2: decision capture.
7. Этап 3.1: dedicated risk endpoint.
8. Этап 3.2: review checklist.

## Definition of Done для всего плана

- Cursor agent может вести задачу через MCP от context до done report.
- Человек может из Cursor или Web пройти approval, answer, decision и force-complete gates.
- Агентские роли содержат конкретные MCP/lifecycle обязанности.
- Команда видит единый процесс в README и docs.
- Контрактные изменения покрыты тестами на API, CLI, MCP, DB/Web там, где они затронуты.
