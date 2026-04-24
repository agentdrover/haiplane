# Workspace Safety Policy

Эта политика фиксирует инварианты, при которых несколько людей и AI-агентов могут безопасно работать в одном workspace repo через OpenClaw Hub. Цель — исключить тихое перетирание чужой работы, размывание scope и ложное завершение задач при конфликтах в git/CI.

Связанные документы:
- [software-development-workflow.md](software-development-workflow.md) — общий жизненный цикл задачи.
- [cursor-agent-rules.md](cursor-agent-rules.md) — короткий checklist для Cursor-агентов.

## Scope

Политика применяется ко всем исполняемым задачам в статусах `dispatched`, `running`, `pending_report`, `ci_check`, `review`, `fix_requested`. Она не ограничивает read-only операции (чтение кода, анализ, предложение draft proposal).

## Инварианты

### 1. Один branch на одну исполняемую задачу

Для каждой задачи в работе используется один branch вида `task-<id>/<slug>`, который создаётся и переиспользуется через git ops plugin (см. раздел «Branch, PR и CI» в [software-development-workflow.md](software-development-workflow.md)).

- Developer agent и testing agent по задаче `#N` пишут только в branch `task-N/...`.
- Переиспользование branch между задачами запрещено.
- Если исторический branch задачи утерян или renamed человеком, задача переводится в `needs_decision` — новый branch не создаётся автоматически.

### 2. Чужой branch неприкосновенен без human decision

Agent не делает commit, push, rebase, force-push, merge или branch delete в branch чужой задачи.

- Если для прогресса по задаче `#A` нужен код, который живёт в branch задачи `#B`, корректный путь — дождаться merge `#B` в базовый branch или оформить явное решение человеком через `hub_decide_task` / `needs_decision`.
- Исключение — явная human decision, зафиксированная через `hub_decide_task`, с указанием reason в audit trail.

### 3. Out-of-scope работа — только draft proposal

Если в ходе работы обнаружена необходимость в изменении, выходящем за `scope_in` текущей задачи, agent **не** расширяет её молча.

- Корректный путь: `hub_propose_task(...)` как draft, ссылка на находку в `hub_task_update(..., kind="status")` текущей задачи.
- Расширение текущей задачи допустимо только после явного human approve draft-а и обновления acceptance criteria / validation через `hub_refine_task`.

### 4. Конфликт branch / PR / CI → `needs_decision`

Если автоматика не может достоверно интерпретировать состояние git/PR/CI, задача переводится в `needs_decision`, а не завершается тихо.

Конкретные триггеры:
- merge conflict, который нельзя разрешить тривиальным rebase;
- PR закрыт или изменён человеком вручную вне потока хаба;
- CI упал повторно после fix cycle (лимит попыток исчерпан);
- reviewer agent и developer agent не могут сойтись после цикла `fix_requested` → review;
- branch / PR / workflow run отсутствуют там, где ожидались.

В `needs_decision` обязательны:
- фиксированная причина в `hub_task_update(..., kind="blocker")` или audit-комментарии решения;
- ссылки на соответствующий PR / CI run / конфликтующий branch.

## Как это применяется ролями

- **Developer agent**: работает только в branch своей задачи, не трогает чужие branches, предлагает out-of-scope работу через `hub_propose_task`.
- **Testing agent**: валидация и дополнительные тесты — в том же branch задачи; failed validation → `hub_task_update(..., kind="blocker")`.
- **Code Reviewer agent**: при неразрешимой неоднозначности оставляет задачу в `needs_decision`, не вызывает `hub_decide_task` сам.
- **Architect Analyst agent**: при обнаружении scope creep оформляет новый draft через `hub_propose_task`, не расширяет текущую задачу.
- **Человек**: единственный, кто может (a) авторизовать касание чужого branch через `hub_decide_task`, (b) принять решение в `needs_decision`, (c) выполнить `hub_approve_task(..., force=true)` или `hub_force_complete_task`.

## Аудит

Любое нарушение политики должно быть видимым постфактум:
- human override — через `hub_decide_task` или force-gate (оставляет audit-update и запись в activity log);
- touching foreign branch без override — эскалация human-owner-ом, факт фиксируется в notes.
