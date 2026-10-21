# Спека: подтверждённая доставка и её цена

> Статус: **черновик дизайна, на обсуждении у владельца.** Код хаба этой спекой не меняется.
> Источник идеи: методичка «AI SDLC / AI-PDLC Metrics Scorecard» (окт. 2026), центральная пара
> Verified Success Rate + Cost per Verified Task. Здесь она переведена на сущности хаба.
> Имена таблиц и колонок настоящие; новые помечены **(new)**.

## 1. Зачем

Сейчас `/metrics` отвечает «сколько прошло с первого раза» (`first_pass_acceptance_rate`) и
«сколько стоит находка ревью» (`provider_tokens_per_fixed`). Нет ответов на два главных вопроса:

- **Q1.** Какая доля взятых в работу задач дошла до результата, который подтвердил не агент и не
  ревьюер-LLM, а независимый сигнал: merge, AC-тесты, отсутствие отката и прод-дефекта?
- **Q2.** Сколько стоит одна такая задача целиком: исполнитель, ревью, стюард, CI, минуты человека,
  включая брошенные и откатанные попытки?

Без Q2 нельзя честно сравнить «сильная модель дорого» и «слабая модель с ретраями».
Без Q1 первая подача ничего не говорит о том, что было после merge.

Объём спеки:

| § | Что | Новое хранение |
|---|---|---|
| 3 | История переходов статусов | `task_transitions` (new) |
| 4 | Verified Delivery Rate (VDR) и first-pass VDR | `delivery_reverts` (new) |
| 5 | Cost per Verified Task (CVT) | таблица цен в конфиге (new), без миграции |
| 6 | Rework: возвраты в работу и поколения подачи | нет, читает §3 |
| 7 | Поверхности и разрезы | — |

## 2. Принципы (из методички, применительно к хабу)

1. **Единица — листовая задача, а не промпт и не прогон.** Роллапы (задачи с детьми) не считаются,
   как в `cycle_split`. `work_type = spike` вне выборки: его результат не доставка.
2. **Подтверждение не от агента.** Самоотчёт «готово», вердикт LLM-ревьюера и `self_approved` —
   не сигнал успеха. Сигналы — только §4.2.
3. **Неизвестно ≠ ноль.** Неизвестная цена компонента не ноль, задача без созревшего окна наблюдения
   не успех. Везде, где данные неполны, отдаётся покрытие (`coverage`) и число неизмеримых по причинам —
   тот же приём, что `unmeasurable` в `defect_clocks`.
4. **Разрез обязателен.** Каждое число есть в разрезе `work_type × size × risk_class`; смесь задач
   разной сложности вводит в заблуждение.
5. **Распределение, а не только среднее.** Для стоимости и rework — медиана, P90, n.
6. **Без сводного индекса.** VDR и CVT — два отдельных показателя рядом с существующими шестью.

## 3. История переходов статусов (предусловие)

### 3.1 Проблема

`events` живёт `EVENTS_RETENTION_DAYS = 14` дней (`hub/repository.py:2692`), а `tasks.status_entered_at`
хранит только текущий статус. За окно 90 дней нельзя узнать, сколько раз задача возвращалась в
`fix_requested`/`running`, сколько ждала в `review` и была ли она когда-то `running` до `failed`.
`task_updates` — свободный текст, разбирать его ненадёжно.

### 3.2 Решение

Новая таблица (миграция `hub/migrations/steps/m<ts>_<task>_task_transitions.py`):

```sql
CREATE TABLE IF NOT EXISTS task_transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,          -- без FK: история переживает удаление
    from_status TEXT    NOT NULL,
    to_status   TEXT    NOT NULL,
    generation  INTEGER NOT NULL DEFAULT 0, -- tasks.submission_generation на момент перехода
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_task_transitions_task ON task_transitions(task_id, id);
CREATE INDEX idx_task_transitions_time ON task_transitions(created_at);
```

Запись — **SQLite-триггером** `AFTER UPDATE OF status ON tasks WHEN OLD.status IS NOT NEW.status`.
Статус сейчас пишется в двух местах (`update_task` и `transition_status` в `hub/repository.py`),
но триггер не зависит от того, появится ли третье: колонку невозможно обойти — тот же довод, что у
штампа `completed_at` в примитиве (#517). Актор и причина в таблицу не пишутся: они есть в `events`
на 14 дней, а метрикам §4–6 нужны только факт, время и поколение.

Без retention: строк порядка «задачи × 6–10», это меньше, чем `task_updates`.

**Бэкфилл не делается.** Метрики, читающие переходы, отдают `history_since` — дату первой строки
`task_transitions` — и не считают задачи, начатые раньше (`start_history_unknown`, как в `cycle_split`).

## 4. Verified Delivery Rate

### 4.1 Выборка (знаменатель)

Задача попадает в выборку окна, когда она **вышла из работы** в окне:

| Исход | Условие | Момент выхода |
|---|---|---|
| доставлена | есть строка `pipeline_merges` с её `task_id` | `MIN(pipeline_merges.merged_at)` |
| брошена | `status IN ('failed','rejected')` и `started_at IS NOT NULL` | последний переход в этот статус (§3) |

Задачи, отклонённые до старта (`rejected` на DoR, `started_at IS NULL`), не в выборке: агент их не
делал. Задачи в работе — тоже нет.

### 4.2 Сигналы подтверждения

Доставленная задача **подтверждена**, если выполнены все сигналы, применимые к ней:

| # | Сигнал | Источник | Когда не применим |
|---|---|---|---|
| S1 | Merge записан хабом, а не найден дрейфом | `pipeline_merges` (гейт доставки уже требует зелёный CI и одобрение текущего поколения) | — |
| S2 | Все AC с `verifiable_by='test'` прошли на последнем поколении | `ac_test_results.status = 'pass'` и `submission_generation = tasks.submission_generation`; логика «текущего» результата — `current_ac_test_results` (`hub/services/ac_tests.py`) | у задачи нет AC с тестом → `verifier = review_only` |
| S3 | Доставку не откатили за окно наблюдения | `delivery_reverts` (new, §4.4) по `pipeline_merges.merge_sha` | — |
| S4 | Нет прод-дефекта, вызванного задачей, за окно наблюдения после релиза | `tasks` дефекта: `caused_by_task_id = id`, `found_in = 'prod'`, `detected_at` ≤ `releases.deployed_at + W`; релиз задачи — через `pipeline_merges.released_sha` = `releases.deployed_sha` (как в `defect_release.py`) | проект без записанных релизов → `release_unknown` |

S2 и S4 не применимы к части задач. Такая задача не исключается: она идёт в разрез `verifier`
(`full` — применимы все сигналы, `review_only` — без S2, `no_release` — без S4), чтобы VDR задач
без тестов не смешивался с VDR задач с тестами.

### 4.3 Созревание

S3 и S4 требуют окна наблюдения `VERIFIED_WINDOW_DAYS` (предлагаю 14, в `hub/config.py`).
Задача, доставленная меньше W дней назад, — **наблюдается**: в VDR не входит ни в числитель, ни в
знаменатель и отдаётся отдельным счётчиком `observing`, как `observing_total` в `outcome_debt`.
Иначе свежая неделя всегда выглядит лучше старой.

Для S4 отсчёт идёт от `deployed_at` релиза; пока релиза нет, задача тоже наблюдается
(или `no_release`, если у проекта релизов нет совсем).

### 4.4 Хранение откатов (new)

Откат доставки сейчас ловит фоновый сборщик стюарда (`steward_breaker.py`, `_REVERTS`), но
результат живёт только событием `steward_breaker_tripped` (14 дней). Нужна таблица:

```sql
CREATE TABLE IF NOT EXISTS delivery_reverts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL,
    merge_sha   TEXT    NOT NULL,  -- откатанная доставка (pipeline_merges.merge_sha)
    revert_sha  TEXT    NOT NULL,
    detected_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_id, merge_sha)
);
```

Пишет её тот же сборщик в той же транзакции, где сейчас trip. Ключ — sha доставки: тот же довод,
что в `_trip_revert` (откат может прийти несколькими коммитами).

### 4.5 Формулы

```
VDR            = verified / (verified + not_verified + abandoned)        по созревшим задачам
first_pass_VDR = verified_first_pass / (тот же знаменатель)
```

- `verified` — доставлена, все применимые S1–S4 выполнены.
- `not_verified` — доставлена, но провален S2, S3 или S4. Отдаётся с разбивкой по первому
  проваленному сигналу (`ac_failed`, `reverted`, `prod_defect`).
- `abandoned` — брошена (§4.1).
- `verified_first_pass` — verified, и при этом `MAX(submissions.generation) = 1` и в `task_transitions`
  нет перехода в `fix_requested` или из `review` обратно в `running`.

Отличие от существующего `first_pass_acceptance_rate`: тот считает по вердиктам ревью, этот — по
результату после merge, и в его знаменателе есть брошенные задачи.

### 4.6 Ответ

```json
"verified_delivery": {
  "window_days": 90, "verified_window_days": 14, "history_since": "2026-11-01",
  "vdr": 0.71, "first_pass_vdr": 0.48, "n": 63,
  "verified": 45, "verified_first_pass": 30,
  "not_verified": {"total": 9, "ac_failed": 2, "reverted": 3, "prod_defect": 4},
  "abandoned": 9, "observing": 11,
  "by_verifier": {"full": {...}, "review_only": {...}, "no_release": {...}},
  "by_class": [{"work_type": "feature", "size": "M", "risk_class": "R2", "vdr": ..., "n": ...}],
  "unmeasurable": {"start_history_unknown": 4}
}
```

Каждый счётчик ведёт к списку задач (Q4 из `metrics-ux.md`).

## 5. Cost per Verified Task

### 5.1 Компоненты стоимости задачи

Стоимость считается по **всем поколениям и всем прогонам** задачи, включая упавшие:

| Компонент | Источник | Единица | Неизвестно, когда |
|---|---|---|---|
| исполнитель | `executor_runs.cents`, `executor_runs.tokens` | центы | задача сделана не облачным исполнителем (локальная сессия) или прогон без цены |
| ревью | `review_dispatches`: `COALESCE(billed_tokens, provider_tokens)`; отчёты без заказа — `machine_reviews.provider_tokens`, затем `tokens_spent` | токены | у прогона нет ни счёта, ни provider_tokens |
| стюард | `steward_judgements.tokens_spent` | токены | `tokens_spent IS NULL` |
| CI | `ci_attempt_timings.runner_seconds` | минуты раннера | у задачи нет ни одного тайминга при наличии CI-отчётов |
| человек | `owner_minute_accruals.seconds` | минуты (оценка по нормам, `NORMS_VERSION`) | никогда: нет начислений = 0 касаний |

Сумма токенов ревью по задаче **не пишется заново**: её уже считает потолок токенов ревью
(`hub/services/review_dispatch.py`, ~5636). Его нужно вынести в общую функцию и вызывать из обоих мест —
одно правило, одно число.

### 5.2 Приведение к одной валюте

Токены, минуты раннера и минуты человека несопоставимы без цен. Новая таблица цен в `hub/config.py`:

```python
METRIC_PRICES_VERSION = 1
METRIC_PRICES = {
    "tokens_per_cent": {"claude-opus": ..., "claude-sonnet": ..., "default": ...},  # по model_family
    "runner_minute_cents": ...,
    "owner_hour_cents": ...,  # 0 по умолчанию — человек не в деньгах, пока владелец не решит
}
```

Версия цен отдаётся в ответе, как `norms_version` у owner minutes. Смена цен меняет историю целиком, это
явно. Компоненты всегда отдаются ещё и **в своих единицах**, чтобы спор о ценах не прятал данные.

### 5.3 Формулы

```
cost(task)  = Σ компонентов, в центах
CVT         = Σ cost(все задачи выборки §4.1, включая брошенные и неподтверждённые) / verified
cost_per_attempt = Σ cost / n
waste_share = Σ cost(not_verified + abandoned) / Σ cost
verification_overhead = Σ (ревью + стюард + CI + человек) / Σ cost
```

CVT делит **полную** стоимость на число подтверждённых. Так брошенная задача дорожает результат, как
требует методичка («retries и rework входят в цену»).

### 5.4 Неполные данные

Задача **полностью оценена**, если известны все применимые компоненты. Отдаётся:

- `cvt` — только по полностью оценённым задачам выборки, с их `n`;
- `cvt_lower_bound` — по всем задачам, неизвестное как 0. Подписан как нижняя граница, не как оценка;
- `coverage` по каждому компоненту: доля задач, где он известен.

Если покрытие исполнителя ниже 50% (сейчас так: цена есть только у облачных прогонов), страница
показывает CVT без исполнителя как основной и пишет это словами. Сравнивать модели исполнителя по CVT
можно только внутри облачного канала.

### 5.5 Разрезы

`by_class` (`work_type × size × risk_class`), `by_implementer_model` (`tasks.submission_model`,
семья через `model_family.py`), `by_reviewer_profile`. Ниже `MIN_COMPARE_N = 10` — `small_sample`.

### 5.6 Ответ

```json
"verified_cost": {
  "prices_version": 1, "norms_version": 1,
  "cvt_cents": 412, "cvt_n": 31, "cvt_lower_bound_cents": 280,
  "cost_per_attempt_cents": {"median": 190, "p90": 760},
  "waste_share": 0.22, "verification_overhead": 0.41,
  "components": {
    "executor":  {"cents": ..., "coverage": 0.34},
    "review":    {"tokens": ..., "cents": ..., "coverage": 0.93},
    "steward":   {"tokens": ..., "cents": ..., "coverage": 0.88},
    "ci":        {"runner_minutes": ..., "cents": ..., "coverage": 0.97},
    "human":     {"minutes": ..., "cents": ..., "estimate": true}
  },
  "by_class": [...], "by_implementer_model": [...]
}
```

## 6. Rework

Читает `task_transitions` (§3) и `submissions`, по той же выборке §4.1:

| Показатель | Формула |
|---|---|
| `returns_per_task` | переходов `review → fix_requested`, `review → running`, `ci_check → running` на задачу: медиана, P90 |
| `generations_per_task` | `MAX(submissions.generation)`: медиана, P90, доля > 2 |
| `ci_fix_share` | доля задач с `tasks.ci_fix_cycle > 0` на момент выхода из работы |
| `rework_after_merge` | доля доставленных, по которым создан дефект `cause_kind = regression` или откат (§4.4) |

Это заменитель Human Rework Rate методички: «существенной человеческой переделки» хаб не видит, а
возвраты и поколения видит.

## 7. Поверхности и место на странице

По `AGENTS.md` контракт меняется сразу на всех поверхностях:

- REST: два новых раздела в `GET /api/metrics/practices` (`verified_delivery`, `verified_cost`, `rework`).
  Те же параметры среза: окно, проект, модель (`metrics_scope.py`).
- CLI: `oc-hub verified-delivery`, `oc-hub verified-cost` — срезы `/practices`, как `defect-clocks`.
- MCP: строка в текстовой сводке `hub_practice_metrics`. Новый инструмент не нужен,
  бюджет каталога не трогаем.
- Web `/metrics`: VDR и CVT становятся ключевыми показателями 7 и 8 в `INDICATORS`
  (`hub/services/metrics_compare.py`): `first_pass_vdr` — выше лучше, `cvt_cents` — ниже лучше.
  Сравнение с прошлым окном и проблемные места работают без доработок. Новый вид проблемного места:
  `data_gap`, когда `coverage` исполнителя < 50% или `history_since` моложе окна.

## 8. Разбиение на задачи

| # | Задача | Зависит | Миграция |
|---|---|---|---|
| T1 | `task_transitions` + триггер + `history_since` | — | да |
| T2 | `delivery_reverts`, запись из сборщика стюарда | — | да |
| T3 | Вынести сумму токенов ревью по задаче в общую функцию | — | нет |
| T4 | `verified_delivery` (§4) на всех поверхностях | T1, T2 | нет |
| T5 | `METRIC_PRICES` + `verified_cost` (§5) | T3, T4 | нет |
| T6 | `rework` (§6) | T1 | нет |
| T7 | Показатели 7–8 на `/metrics`, `data_gap` | T4, T5 | нет |

T1 и T2 стоит выкатить первыми и как можно раньше: метрики начнут иметь историю только с их выката.

## 9. Открытые вопросы владельцу

1. **W = 14 дней** для S3/S4 — подходит? Больше W — честнее, но позже число.
2. **Минуты человека в деньгах:** по умолчанию `owner_hour_cents = 0` (видны отдельно в минутах).
   Включать ли их в CVT?
3. **Цены токенов:** брать из конфига или из Cursor-экспорта (`cursor_usage_events`) там, где он есть?
4. **Брошенная задача, переоткрытая новой задачей** (`caused_by_task_id`): считать одну попытку или две?
   Предлагаю две, это соответствует единице «задача».
5. **`review_only` в главном числе:** показывать VDR по всем задачам или только по `verifier = full`?
   Предлагаю по всем, с разрезом рядом и предупреждением, если доля `review_only` > 50%.
