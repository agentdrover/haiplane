# Путь разработки ПО через OpenClaw Hub

Implementation roadmap: `docs/software-development-workflow-implementation-plan.md`.

## Цель

OpenClaw Hub должен быть общей контрольной плоскостью для работы людей и AI-агентов из Cursor. Люди формулируют намерение, утверждают готовность задач, отвечают на вопросы, принимают спорные решения и контролируют результат. Агенты помогают разбирать требования, готовить задачи, писать код, проверять изменения и возвращать отчеты в хаб.

Хаб не заменяет git, CI и ревью. Он связывает их в один наблюдаемый жизненный цикл: от идеи до PR, проверки, доработок и завершения.

## Рабочая модель

У проекта есть две разные среды:

- **Hub repo**: этот репозиторий, где живет сервер хаба, Web UI, REST API, CLI и MCP.
- **Workspace repo**: рабочий git-репозиторий продукта, с которым будут работать агенты. Он задается через `OPENCLAW_WORKSPACE_REPO`.

Все люди и агенты должны видеть один и тот же хаб. Cursor подключается к MCP-поверхности хаба, а хаб уже обращается к REST API, базе, dispatch, git, GitHub, notes и transcripts через плагины.

## Роли

**Человек-заказчик**

Формулирует цель, приоритеты и ограничения. Принимает результат в продуктовых терминах.

**Человек-техлид**

Утверждает готовность задачи, контролирует риски, решает спорные ситуации, принимает или отправляет на доработку результаты после арбитража.

**Architect Analyst agent**

Превращает сырую идею в готовую задачу: выбирает `work_type`, уточняет `scope_in`, `scope_out`, acceptance criteria, validation commands, risks и добивается прохождения Definition of Ready.

**Developer agent**

Берет утвержденную задачу, читает контекст через хаб, пишет план, выполняет изменение в workspace repo, отправляет статусные обновления и `done`-отчет.

**Testing agent**

Проверяет поведение, дописывает или запускает тесты, фиксирует команды в задаче и сообщает регрессионные риски.

**Code Reviewer agent**

Проверяет diff и контрактные риски: API, CLI, MCP, схему, миграции, жизненный цикл, тесты.

## Единица работы

Базовая единица - task или subtask. Epic и feature задают структуру, но не должны автоматически уходить агенту в работу.

Иерархия:

```text
epic -> feature -> task -> subtask
```

Каждая исполняемая задача должна содержать:

- `work_type`;
- краткое описание проблемы или пользовательской ценности;
- явный `scope_in` и при необходимости `scope_out`;
- acceptance criteria в формате Given/When/Then;
- validation commands;
- размер (`size`) и при необходимости `wip_tag`;
- риски и mitigation, если есть неопределенность;
- ограничения для ревью, если reviewer должен что-то игнорировать.

## Основной жизненный цикл

```mermaid
flowchart TD
    A["Идея или запрос"] --> B["Draft task"]
    B --> C["Refine: DoR поля, AC, risks"]
    C --> D{"Definition of Ready passed?"}
    D -- "нет" --> C
    D -- "да" --> E["Human approve"]
    E --> F["Open task"]
    F --> G["Plan required"]
    G --> H["Dispatch to developer agent"]
    H --> I{"Agent needs info?"}
    I -- "да" --> J["needs_info: human answer"]
    J --> H
    I -- "нет" --> K["done report"]
    K --> L{"Auto review enabled?"}
    L -- "нет" --> M["pending_report or completed"]
    L -- "да" --> N["CI check"]
    N -- "fail" --> O["CI fix cycle"]
    O --> H
    N -- "pass" --> P["Reviewer agent"]
    P -- "changes requested" --> Q["fix_requested"]
    Q --> H
    P -- "approved" --> R["PR merge / completed"]
    P -- "unclear or cycle limit" --> S["needs_decision"]
    S --> T{"Human decision"}
    T -- "accept" --> R
    T -- "rework" --> Q
```

## Сценарий 1: человек поручает работу агенту

1. Человек создает feature/task в Web UI, CLI или Cursor через MCP.
2. Analyst agent уточняет задачу через `hub_refine_task`, `hub_add_acceptance_criterion`, `hub_add_risk`.
3. Человек смотрит `hub_get_readiness` или readiness в UI.
4. Если DoR проходит, человек утверждает задачу.
5. Перед стартом должен быть план: `hub_start_task(..., plan="...")` или отдельный update с `Plan:`.
6. Developer agent берет контекст через `hub_my_context`, работает в workspace repo и пишет updates.
7. Если агенту не хватает данных, он вызывает `hub_ask_question`; человек отвечает через UI или `hub_answer_question`.
8. Агент отправляет `hub_report_done` с тем, что изменено и как проверено.
9. Хаб прогоняет CI/review/fix cycles. Человек вмешивается только при `needs_info`, `needs_decision`, stale, failed или force-complete.

## Сценарий 2: агент предлагает работу человеку

1. Агент видит проблему, недостающий тест, долг или уточнение и создает draft через `hub_propose_task`.
2. Draft не исполняется автоматически.
3. Analyst agent или человек доводит draft до готовности.
4. Человек выбирает одно из действий:
   - approve: задача становится `open`;
   - approve + run: задача сразу стартует после утверждения;
   - reject: предложение отклоняется с комментарием;
   - force approve: допускается только как осознанное исключение через API/CLI/Web, хаб пишет audit update.

## Сценарий 3: человек контролирует результат агента

Контроль должен быть по состояниям, а не через ручной мониторинг чатов.

В Inbox должны попадать:

- `draft`: агент предложил задачу, нужен approve/reject;
- `needs_info`: агент задал вопрос;
- `pending_report`: работа завершилась, но нет нормального done-отчета;
- `needs_decision`: review/CI/arbiter не смогли автоматически закрыть задачу;
- stale running tasks: агент долго не обновлял статус.

Для приемки человек проверяет:

- есть ли done report;
- выполнены ли acceptance criteria;
- совпадают ли фактические validation commands с заявленными;
- есть ли PR и прошел ли CI;
- не исчерпаны ли review/CI fix cycles;
- есть ли force overrides или alerts в update log.

## Контракт Cursor + Hub

В Cursor агентам нужно дать правило: хаб является источником правды по задачам и статусам.

Минимальный набор MCP-инструментов для ежедневной работы:

- `hub_project_status` - обзор очереди, вопросов, ревью, решений и PR;
- `hub_list_tasks` / `hub_task_status` - поиск и детализация задач;
- `hub_my_context` - контекст задачи перед работой;
- `hub_refine_task` - уточнение структурных полей;
- `hub_add_acceptance_criterion` / `hub_replace_acceptance_criteria` - критерии приемки;
- `hub_add_risk` - фиксация рисков;
- `hub_get_readiness` - проверка DoR;
- `hub_approve_task` / `hub_reject_task` - человеческий gate;
- `hub_start_task` - запуск только после плана;
- `hub_task_update`, `hub_ask_question`, `hub_report_done` - отчетность агента;
- `hub_decide_task` - человеческое решение после арбитража.

Агентам не стоит обходить хаб, если действие влияет на состояние задачи. Код можно менять в workspace repo, но намерение, вопросы, блокеры, done report и решения должны возвращаться в хаб.

Важно: текущий MCP `hub_approve_task` не принимает `force=true`. Для force approve сейчас нужно использовать API, CLI или Web UI, либо добавить `force` в MCP-контракт отдельным изменением.

## Definition of Ready как главный входной gate

До старта задачи хаб должен отвечать на вопрос: "может ли разработчик-агент выполнить работу без дополнительного интервью?".

Поэтому `draft -> open` проходит через DoR:

- если required checks не пройдены, approve возвращает 422;
- force approve разрешен, но должен быть редким и аудируемым;
- readiness score полезен для улучшения задачи, но сам по себе не заменяет missing required checks;
- разные `work_type` имеют разные DoR-профили, поэтому bug, chore, docs, spike и incident не надо насильно доводить до feature-уровня детализации.

## Human gates

Обязательные точки человеческого контроля:

1. **Approval gate**: перевод `draft -> open`.
2. **Start gate**: наличие плана перед dispatch.
3. **Question gate**: ответ на `needs_info`.
4. **Decision gate**: `needs_decision` после blocker, review ambiguity, CI/review cycle limit или arbiter.
5. **Force gate**: любой `force=true` должен иметь комментарий и оставлять audit trail.
6. **Completion gate for weak reports**: `pending_report` нельзя считать завершенным без понятного отчета или явного force-complete.

## Branch, PR и CI

Для каждой исполняемой задачи хаб создает или переиспользует branch вида `task-<id>/<slug>` через git ops plugin.

Рекомендуемый порядок:

1. Developer agent работает только в branch задачи.
2. После done хаб auto-commit, squash, push и создает PR, если это возможно.
3. CI проверяется до reviewer agent.
4. Если CI падает, задача возвращается в developer agent через CI fix cycle.
5. Если CI проходит, запускается reviewer agent.
6. После approval и успешного CI хаб может merge PR и завершить задачу.

Если branch/PR/CI не удалось создать или интерпретировать, задача должна уходить в `needs_decision`, а не молча считаться выполненной.

## Режимы работы команды

**Solo with agents**

Один человек ведет задачи и использует агентов как исполнителей. Достаточно Web UI + Cursor MCP + CLI для редких операций.

**Pair human + agent**

Человек держит Cursor-сессию открытой, агент работает по одной задаче, все вопросы и отчеты фиксируются в хабе. Это режим для рискованных изменений.

**Team queue**

Несколько людей работают с одной очередью. Важны явные owners, статусы, Inbox и запрет на запуск задач без DoR/плана.

**Agent swarm**

Несколько агентов работают параллельно только над независимыми tasks/subtasks. Общий epic/feature используется для прогресса и группировки, но не как исполняемая задача.

## Операционный ритм

Ежедневно:

- открыть `hub_project_status` или dashboard;
- разобрать `draft`, `needs_info`, `needs_decision`, `pending_report`, stale;
- не запускать новые задачи, пока старые блокеры не разобраны.

Перед стартом задачи:

- проверить readiness;
- убедиться, что acceptance criteria проверяемы;
- добавить план;
- выбрать runtime.

После завершения:

- прочитать done report;
- проверить validation commands;
- посмотреть PR/CI/review;
- принять, отправить на rework или зафиксировать решение.

После спорных решений:

- сохранить короткое архитектурное или процессное решение в notes, чтобы `hub_list_decisions` возвращал контекст будущим агентам.

## Минимальные правила для агентов в Cursor

1. Перед работой вызвать `hub_my_context(task_id)`.
2. Если задача не готова, не писать код, а предложить refine/AC/risk.
3. Перед dispatch или началом работы оставить `Plan:`.
4. Все блокеры фиксировать через `hub_task_update(..., kind="blocker")` или `hub_ask_question`.
5. В done report указывать changed files, behavior change и validation commands.
6. Не закрывать задачу напрямую, если есть неразрешенный blocker, failed CI или review changes.
7. Для новых найденных работ создавать draft proposal, а не расширять текущий scope молча.

## Где текущий хаб уже поддерживает процесс

- REST API является канонической поверхностью.
- MCP-инструменты уже зеркалируют API для Cursor и удаленных агентов.
- `draft -> open` защищен DoR и atomic approve.
- `start` требует план.
- `needs_info`, `needs_decision`, `pending_report`, stale и drafts агрегируются в Inbox/dashboard.
- Poller умеет переводить задачи через running, ci_check, review, fix_requested, needs_decision и completed.
- Интеграции optional: без dispatch/git/GitHub/notes/transcripts хаб остается пригодным для CRUD и ручного контроля.

## Рекомендуемые следующие улучшения

1. **Cursor setup guide**: добавить короткую инструкцию подключения MCP в Cursor и базовые agent rules.
2. **Role prompts**: расширить `agents/*.md` конкретными правилами использования MCP tools.
3. **Ownership field**: если команда растет, добавить явного human owner/reviewer у задачи.
4. **Review checklist field**: отделить acceptance criteria от reviewer-specific checklist.
5. **MCP force approve parity**: добавить параметр `force` в `hub_approve_task`, чтобы Cursor не уступал CLI/Web в контролируемых override-сценариях.
6. **Concurrent risk handling**: заменить read-modify-write в `hub_add_risk` отдельным API endpoint, если агенты часто добавляют риски параллельно.
7. **Workspace safety policy**: формализовать правило branch isolation для нескольких агентов.
8. **Decision capture flow**: сделать сохранение решений из `needs_decision` более явной частью UI/MCP.
