# Workspace Safety Policy

Эта политика фиксирует инварианты, при которых несколько людей и AI-агентов могут безопасно работать в одном workspace repo через OpenClaw Hub. Цель — исключить тихое перетирание чужой работы, размывание scope и ложное завершение задач при конфликтах в git/CI.

Связанные документы:
- [software-development-workflow.md](software-development-workflow.md) — общий жизненный цикл задачи и [Pair mode: git policy](software-development-workflow.md#pair-mode-git-policy).
- [task-workflow.html](task-workflow.html#pair-git-policy) — краткая схема path B и git-сценарии.
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

## Pair mode (path B) — дополнительные правила git

Применяется, когда задача стартовала через `hub_pair_start` (running **без** `job_id`), а код пишется в Cursor на локальном clone.

### P1. Commit before pair-start на shared workspace

Если `OPENCLAW_WORKSPACE_REPO` совпадает с каталогом, открытым в Cursor, коммитьте или прячьте изменения до `hub_pair_start` — иначе старт задачи будет отклонён.

**Историческая справка (#361).** Раньше `create_branch` на грязном дереве выполнял `git checkout .` + `git clean -fd`, то есть молча уничтожал незакоммиченные правки, и этот документ описывал такое поведение как текущее с обходом «коммить заранее». Теперь оно невозможно: `create_branch` отказывается стартовать на грязном дереве и возвращает пустую ветку, а done-конвейер при неудачном checkout уводит задачу в `needs_decision` вместо продолжения. Отказ вместо разрушения — это инвариант 4 выше, применённый к git-операциям.

### P2. Имя branch в хабе ≠ автоматически текущая ветка IDE

Hub записывает `task-<id>/<slug>` в поле задачи и может checkout эту ветку в workspace repo. Ветка разработчика в Cursor (например `task-37/pair-start`) может отличаться от slug из title. Разрешение — явное: checkout branch из задачи, merge/cherry-pick, или update в хабе после согласования с human owner. **Не** делать force-push чужих веток.

### P3. CI/review только после push

Pair mode не запускает headless dispatch. GitHub CI и reviewer agent ориентируются на **remote** branch. До `git push` задача может быть `running` в хабе, но CI не увидит код — это ожидаемо.

### P4. Server workspace vs local clone

Если hub на сервере, а Cursor на ноутбуке — два clone. Invariant «один branch на задачу» сохраняется по **имени** на remote; локально создайте matching branch и push. Не редактируйте server clone вручную без runbook и human gate.

Подробные шаги: [software-development-workflow.md § Pair mode: git policy](software-development-workflow.md#pair-mode-git-policy).

## Worktree-per-task (opt-in, #459)

По умолчанию Hub держит один workspace на проект и переключает ветки в нём
(`git checkout`), поэтому две задачи не могут держать разные ветки
одновременно (#451/#457 лишь возвращают дерево на base между задачами).

При `OPENCLAW_WORKTREE_PER_TASK=1` включается изоляция через `git worktree`:

- pair-start создаёт для задачи **отдельное рабочее дерево** по детерминированному
  пути (`.<repo>-worktrees/task-<id>`, sibling основного клона), а основной клон
  (`_default`) **всегда остаётся на base branch** и не переключается.
- Две задачи получают независимые worktree с общим `.git` — параллельный
  pair-start не сводит их в одну ветку и не перетирает работу друг друга.
- Worktree убирается (`git worktree remove` + `prune`) при уходе задачи из
  `running` (submit / done / release); при CHANGES_REQUESTED дерево создаётся
  заново для доработки.
- Инварианты сохраняются: **грязный worktree не удаляется** (изменения не
  теряются), stale-регистрация чистится `prune` перед созданием, ветка задачи
  от base отклоняется при `base ahead of origin` (см. #457).

Флаг по умолчанию выключен — боевое поведение не меняется, пока опс не включит
его осознанно. Мульти-репозиторные workspace остаются вне объёма.

## Commit-scope gate (#361)

`create_branch` отказывается стартовать на грязном дереве, но это **точечная
проверка на момент создания ветки**, а не гарантия на весь прогон задачи.
Worktree-изоляция выше её не закрывает: она применяется только к pair-задачам,
а headless-задачи (с `job_id`) весь прогон живут в основном клоне. Всё, что
записано в этот клон, пока агент работает, на момент коммита выглядит ровно как
работа задачи — git не различает авторов.

Единственная атрибуция, которая у хаба есть, — объявленные задачей
`affected_areas`. Перед `auto_commit` done-конвейер сверяет с ними грязные пути
(`OPENCLAW_COMMIT_SCOPE`):

- `warn` (по умолчанию) — файлы вне области перечисляются в апдейте задачи,
  коммит выполняется;
- `require` — git-хвост останавливается, задача уходит в `needs_decision` по
  инварианту 4; решение принимает человек;
- `off` — проверка выключена.

Атрибуция слабая в обе стороны: агент может законно тронуть больше, чем задача
предсказала, поэтому `require` **эскалирует, а не отбрасывает файлы**. И если
`affected_areas` не объявлены, проверка не выполняется — это говорится вслух
отдельным апдейтом, потому что молчание читалось бы как «проверено и чисто».
Полное решение — распространить worktree-изоляцию на headless-задачи.

## Аудит

Любое нарушение политики должно быть видимым постфактум:
- human override — через `hub_decide_task` или force-gate (оставляет audit-update и запись в activity log);
- touching foreign branch без override — эскалация human-owner-ом, факт фиксируется в notes.
