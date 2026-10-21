# Спека: неопровергнутая доставка и её цена (v2)

> Статус: **v2 принята владельцем 21.10.2026** (решения в §9). Код хаба этой спекой не меняется.
> Источник идеи: методичка «AI SDLC / AI-PDLC Metrics Scorecard» (окт. 2026), центральная пара
> Verified Success Rate + Cost per Verified Task. Здесь она переведена на сущности хаба.
> Имена таблиц и колонок настоящие, новые помечены **(new)**. Ссылки `file:line` даны по состоянию на 21.10.2026.

## 0. Что изменилось относительно v1

v1 прошла независимое ревью со сверкой с кодом. Главные исправления:

| Было в v1 | Почему неверно | В v2 |
|---|---|---|
| Выборка: задачи, «вышедшие из работы» в окне | задача, застрявшая в `needs_decision`, возвращённая в `open` или заархивированная, не портила метрику — выгодно не заканчивать | когорта по `started_at` с созреванием по времени, исход `stalled` (§4.1) |
| «Брошена» = `failed`/`rejected` после старта | `reject_task` разрешён только из `draft` (`hub/services/lifecycle.py:1636`), `failed` ставит только поллер | явный список исходов неуспеха (§4.2) |
| First-pass: нет перехода `review → running` | pair-ревью возвращает задачу в `running` при любом вердикте, в т.ч. APPROVED (`lifecycle.py:5017`) | первая подача = одна строка `submissions` (§4.5) |
| S1 «merge записан хабом» как сигнал | это определение «доставлена», а не проверка. Строки пишет и бэкфилл задним числом, и merge при уже недействительном одобрении (`note_approval_gone(merged=True)`), а `force_complete`/`decide accept` завершают задачу вовсе без merge | путь доставки `via` (new), исходы `accepted_undelivered` и `merged_unaccepted` (§4.2–4.3) |
| Откаты пишет сборщик стюарда «в той же транзакции» | сборщик записывает откат (в `steward_false_approvals`) только для доставок, одобренных стюардом; откат обычной доставки не пишется нигде | `delivery_reverts` для всех доставок + состояние «не проверено» (§4.4) |
| Сумма токенов ревью из потолка (`review_dispatch.py:5616`) | потолок превращает NULL в 0 и не читает `billed_tokens` — верно для потолка, неверно для метрики | отдельная функция метрики с unknown (§5.1) |
| «Verified Success Rate» | S1–S4 — отсутствие опровержения, а не независимое подтверждение | название «неопровергнутая доставка» + положительный сигнал `outcome_answers` (§4.6) |
| VDR и CVT — 8-й и 9-й ключевые показатели | ключевых уже 7 (`metrics_compare.py:114`), `metrics-ux.md` AC-1 требует 4–6 | замена, а не добавление (§7) |
| Один CVT «по полностью оценённым» | полностью оценены только облачные прогоны — другая популяция | CVT по компонентам с общим знаменателем (§5.3) |
| Решётка `work_type × size × risk_class` | при ~60 задачах за 90 дней почти все ячейки n < 10 | разрезы по одному измерению, интервалы (§2, §7) |

## 1. Зачем

Сейчас `/metrics` отвечает «сколько прошло ревью с первого раза» (`first_pass_acceptance_rate`) и
«сколько стоит находка ревью» (`provider_tokens_per_fixed`). Нет ответов на два главных вопроса:

- **Q1.** Какая доля начатых задач дошла до доставки, которую за окно наблюдения ничто не опровергло:
  ни откат, ни прод-дефект, ни провал AC-тестов? И у какой доли результат ещё и подтверждён положительно?
- **Q2.** Сколько стоит одна такая задача целиком: исполнитель, ревью, стюард, CI, включая брошенные
  и откатанные попытки? И сколько минут человека на неё ушло?

Без Q2 нельзя честно сравнить «сильная модель дорого» и «слабая модель с ретраями».

## 2. Принципы

1. **Единица — листовая задача.** Роллапы (задачи с детьми, `parent_id`) не считаются, как в `cycle_split`.
   `work_type = spike` и задачи `result_kind = 'state'` вне выборки VDR: их результат не доставка кода.
   State-задачи отдаются отдельным счётчиком `out_of_scope`, чтобы их не потерять молча.
2. **Подтверждение не от агента.** Самоотчёт, вердикт LLM-ревьюера, `self_approved` и одобрение стюарда —
   не сигнал. Сигналы — только §4.3–4.6.
3. **Неизвестно ≠ ноль, ≠ успех.** Неизвестная цена — не 0. Непроверенный сигнал — не pass. Везде, где
   данные неполны, отдаётся покрытие (`coverage`) и число неизмеримых по причинам (как `unmeasurable` в
   `defect_clocks`).
4. **Интервал рядом с числом.** Доли — с интервалом Уилсона 95%, медианы и P90 стоимости — с
   бутстреп-интервалом. Сравнение окон в `metrics_compare` заявляет изменение, только когда интервалы
   не пересекаются (вдобавок к `MIN_COMPARE_N`).
5. **Разрезы по одному измерению.** `by_work_type`, `by_size`, `by_risk_class`, `by_implementer_model`
   отдельно, а не решёткой. NULL — своё значение `unknown`, а не выпадение.
6. **Версия определения.** Ответ несёт `definition_version` (как `norms_version` у owner minutes).
   Смена правил сигналов или окон — новая версия. Сравнение окон разных версий не делается.
7. **Только production.** Хаб видит реальные задачи, а не eval. Capability-метрики методички
   (повторяемость, pass^k на эталонном наборе) — вне объёма этой спеки и вне хаба.
8. **Без сводного индекса.** Показатели отдельные, рядом.

## 3. Постоянная история (предусловие для rework, не для VDR)

### 3.1 Что теряется сейчас

`events` живёт `EVENTS_RETENTION_DAYS = 14` дней (`hub/repository.py:2692`). Вердикты ревью по
поколениям живут только там: в `tasks` лежит последний (`review_verdict`, `review_verdict_generation`).
`review_cycle` обнуляется при закрытии окна вердикта (`lifecycle.py:5578`) и после арбитража
(`lifecycle.py:5816`), так что накопительным счётчиком не является. Сколько раундов исправлений было
у задачи за 90 дней, хаб не знает.

VDR (§4) от этого **не зависит**: он строится на `started_at`, `submissions`, `pipeline_merges`,
`completed_exit_seq` и постоянных таблицах. История нужна только для rework (§6).

### 3.2 Решение

Новая таблица (миграция `hub/migrations/steps/m<ts>_<task>_task_history.py`):

```sql
CREATE TABLE IF NOT EXISTS task_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,  -- без FK: см. §5.6 о каскадах
    kind       TEXT    NOT NULL,  -- 'status' | 'review_round' | 'ci_fix_round'
    from_value TEXT    NOT NULL DEFAULT '',
    to_value   TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_task_history_task ON task_history(task_id, id);
CREATE INDEX idx_task_history_time ON task_history(kind, created_at);
```

Пишут её **триггеры**. Прецедент уже есть: `trg_false_approve_*` и
`trg_tasks_claim_cleared_drops_run_proof` (`hub/migrations/part_3.py`), один из них —
`AFTER UPDATE OF status ON tasks` (#1601). `_migrate` выполняет `CREATE TRIGGER … BEGIN … END`
одним оператором, тесты получают схему тем же раннером.

| Триггер | Когда | Пишет |
|---|---|---|
| `trg_task_history_insert` | `AFTER INSERT ON tasks` | `status`, `'' → NEW.status` (задачи создаются сразу в `open`/`running`) |
| `trg_task_history_status` | `AFTER UPDATE OF status ON tasks WHEN OLD.status IS NOT NEW.status` | `status`, `OLD → NEW` |
| `trg_task_history_review_round` | `AFTER UPDATE OF review_cycle ON tasks WHEN NEW.review_cycle > OLD.review_cycle` | `review_round` |
| `trg_task_history_ci_fix` | `AFTER UPDATE OF ci_fix_cycle ON tasks WHEN NEW.ci_fix_cycle > OLD.ci_fix_cycle` | `ci_fix_round` |

Счётчики пишутся по **росту**, поэтому обнуление `review_cycle` историю не стирает. Статус пишут как
минимум три функции: `update_task`, `transition_status_if` (`repository.py:3240`) и
`record_state_decision` (`repository.py:3635`). Триггеру неважно, сколько их и появится ли четвёртая.

Поколение в строку не пишется: `bump_submission_generation` — отдельный UPDATE, его порядок относительно
смены статуса не гарантирован, а «призрачный» бамп (`lifecycle.py:5036`) и откат бампа
(`resubmission_guard.py:152`) делают поколение немонотонным. Источник поколений — `submissions`.

Порядок с `trg_false_approve_reopened`: тот делает `UPDATE tasks` по другой колонке, триггеры истории
на неё не подписаны, рекурсии нет. Тест миграции это проверяет.

Без retention. **Бэкфилл не делается.** Rework отдаёт `history_since` и не считает задачи, начатые
раньше (`start_history_unknown`, как в `cycle_split`).

## 4. Неопровергнутая доставка (UDR) и подтверждённая доставка (CDR)

### 4.1 Когорта

Задача входит в когорту окна, когда **начата** в окне: `started_at` в `[date_from, date_to)`.
Срез по проекту и модели — через `metrics_scope.py`, как у остальных разделов.

Задача **созрела**, когда с `started_at` прошло `UDR_HORIZON_DAYS` (§4.7). Незрелые отдаются счётчиком
`immature` и в доли не входят. Так знаменатель не зависит от того, закончила ли задача работу:
застрявшая задача стареет и становится `stalled`, а не прячется.

Окно по умолчанию сдвинуто назад на горизонт: «окно 90 дней» = задачи, начатые с `today − 90 − H`
по `today − H`. Ответ отдаёт обе даты явно.

### 4.2 Исходы

Каждая созревшая задача получает ровно один исход, по первому подходящему правилу сверху вниз:

| Исход | Правило | Источник |
|---|---|---|
«Доставлена через гейт» ниже значит: у задачи есть строка `pipeline_merges` с `via = 'gate'` (§4.3)
**и** задача в `completed` на момент созревания.

| Исход | Правило | Источник |
|---|---|---|
| `deleted` | строки задачи нет, а строка когорты есть | см. §4.8: когорта фиксируется снимком |
| `abandoned` | `status IN ('failed','rejected')`; или `archived = 1` без доставки; или ручная приёмка без доставки с `disposition = 'abandon'` | `tasks`, `delivery_discrepancies` |
| `accepted_undelivered` | `completed` без строки `pipeline_merges`, есть `delivery_discrepancies` с `state <> 'delivered'` и `disposition IN ('deliver','')` — человек принял (`force_complete`, `decide accept`), а работа не влита | `delivery_discrepancies` (постоянная, `part_2.py:348`) |
| `merged_unaccepted` | строка `pipeline_merges` есть, но задача не `completed`: merge прошёл при уже недействительном одобрении (`note_approval_gone(merged=True)`, `delivery_notes.py:56`) и решение человека не принято | `tasks`, `pipeline_merges` |
| `stalled` | доставки нет и задача не в финальном статусе на момент созревания | `tasks`, `pipeline_merges` |
| `bypassed` | `completed`, но доставка только с `via IN ('backfill','unknown')` | `pipeline_merges.via` (new, §4.3) |
| `refuted` | доставлена через гейт, и сработал хотя бы один сигнал опровержения §4.4 | §4.4 |
| `unchecked` | доставлена через гейт, опровержений нет, но хотя бы один применимый сигнал в состоянии «не проверено» | §4.4 |
| `unrefuted` | доставлена через гейт, все применимые сигналы проверены, опровержений нет | §4.4 |

`completed` без строки `pipeline_merges` и без `delivery_discrepancies` — законная работа без PR
(`orchestration.py:6321`: «Tasks without a PR (config work, docs) are untouched»). Такие задачи идут в
`out_of_scope` с причиной `no_pr`, как state-задачи. Контейнеры (`feature`/`epic` с детьми,
`delivery_state.CONTAINER_TYPES`) — роллапы и в когорту не входят.

Задача, доставленная, потом выведенная из `completed` (`completed_exit_seq > 0`) и брошенная, получает
исход по своему **итоговому** состоянию: если она финально `failed`/`rejected`/`archived` — `abandoned`,
иначе правила выше. Переоткрытие само по себе — опровержение (сигнал R4).

### 4.3 Путь доставки (new)

Колонка `pipeline_merges.via TEXT NOT NULL DEFAULT 'unknown'`. Значение задаёт вызывающий
`record_pipeline_merge` (`hub/repository.py`). Мест вызова четыре:

| Вызов | `via` |
|---|---|
| `hub/services/orchestration.py:4763` (`_record_gate_delivery`, pair-гейт) | `gate` |
| `hub/poller.py:555` (`_record_merge_and_tidy`, headless-гейт, `merge_if_approval_stands`) | `gate` |
| `hub/services/release.py:754` | `release` (строка релиз-PR, в метрике не участвует) |
| `hub/services/merge_ledger_backfill.py:172` | `backfill` |

Доставка по решению человека (`deliver_on_disposition`, `lifecycle.py:6498`) идёт через
`merge_before_completion` — функцию самого гейта, с проверкой одобренного ревью и зелёного CI. Поэтому
она `gate`, а не обход. Обход гейта — это завершение **без** merge (исход `accepted_undelivered`, §4.2).
Чтобы видеть долю ручных доставок, в ответе есть `by_acceptance` по `delivery_discrepancies.accepted_via`.

Старые строки остаются `unknown`. Строка с пустым `merge_sha` (`repository.py:825`) доставкой считается,
но R2 для неё не проверить → `unchecked`.

Если у задачи несколько строк `pipeline_merges` (переоткрытие, повторная доставка), берётся последняя
строка с `via = 'gate'`; если такой нет — задача `bypassed`.

### 4.4 Сигналы опровержения

| # | Опровержение | Источник | «Не проверено», когда |
|---|---|---|---|
| R1 | AC-тест провален на последнем поколении | `ac_test_results`: строка с `submission_generation = tasks.submission_generation`, `source <> 'legacy'`, `status IN ('fail','not_found')` | у AC с `verifiable_by='test'` нет результата на последнем поколении, или есть только `source='legacy'` (по замыслу «unverified, not a pass», `part_3.py:816`) |
| R2 | Доставку откатили в пределах `REVERT_WINDOW_DAYS` | `delivery_reverts` (new, §4.4.1) | `merge_sha` пуст; проект в состоянии «сборщик не проверял» за этот период (§4.4.1) |
| R3 | Прод-дефект, вызванный задачей, в пределах `DEFECT_WINDOW_DAYS` от первого успешного релиза | дефект: `caused_by_task_id = id`, `found_in = 'prod'`; релиз: `MIN(releases.deployed_at)` по `status='success'` и `deployed_sha = pipeline_merges.released_sha`, проекты — через `defect_release.project_scope` (у реальных релизов `project_id` NULL, `defect_release.py:78`) | релиза нет дольше `RELEASE_WAIT_DAYS` → `release_unknown` |
| R4 | Задачу вывели из `completed` после доставки | `tasks.completed_exit_seq > 0`; для задач стюарда — ещё и `steward_false_approvals.source='reopened'` | — |

Применимость:
- R1 применим, только если у задачи есть AC с `verifiable_by='test'`. Иначе задача помечается
  `verifier = review_only` (§4.6).
- R3 применим, только если у проекта есть хотя бы один записанный релиз. Иначе `verifier` получает
  флаг `no_release`.

**Лаг меток.** `caused_by_task_id` ставит человек, с задержкой. Поэтому R3 читает метки «как есть на
момент расчёта», а ответ отдаёт `labels_pending` — число прод-дефектов окна без `caused_by_task_id` и
без `cause_kind`. Прошлое окно в `metrics_compare` пересчитывается с теми же метками, а не берётся из
кэша, — иначе прошлое всегда выглядит хуже, потому что успело накопить метки.

**Связь с CFR.** Существующий `change_failure_rate` (`defect_clocks.py:267`) связывает дефект с
релизом через `tasks.release_id`, R3 — с задачей через `caused_by_task_id`. Это разные вопросы
(«какой деплой сломался» и «какая задача сломала»), оба остаются. Дефект с `release_id`, но без
`caused_by_task_id`, попадает в `labels_pending`.

#### 4.4.1 Хранение откатов (new)

Сейчас откаты ловит фоновый сборщик стюарда (`steward_breaker.py`, `_REVERTS`), но `_trip_revert`
записывает факт (в `steward_false_approvals`, `source='reverted'`) только для доставок с proof стюарда.
Откат обычной доставки не записывается нигде.

```sql
CREATE TABLE IF NOT EXISTS delivery_reverts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL,
    merge_sha   TEXT    NOT NULL,  -- откатанная доставка (pipeline_merges.merge_sha)
    revert_sha  TEXT    NOT NULL,
    detected_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_id, merge_sha)
);
CREATE TABLE IF NOT EXISTS delivery_revert_coverage (
    project_id  INTEGER NOT NULL,
    checked_from TEXT   NOT NULL,  -- с какого момента история проекта просмотрена
    checked_to   TEXT   NOT NULL,  -- до какого
    PRIMARY KEY (project_id, checked_from)
);
```

Сборщик пишет `delivery_reverts` **до** проверки proof стюарда, для любой доставки, найденной
`_reverted_delivery`. Каждый успешный проход расширяет `delivery_revert_coverage`. Проекты без
`workspace_path`, первый тик после сброса курсора и состояние `_note_unchecked` покрытия не дают.
R2 для доставки считается проверенным, только если `[merged_at, merged_at + REVERT_WINDOW_DAYS]`
целиком лежит в покрытии проекта.

### 4.5 Первая подача

Задача **прошла с первой подачи**, если исход `unrefuted` и `COUNT(submissions) = 1`.

`submissions` — постоянная таблица, строка на реальную подачу (`UNIQUE(task_id, generation)`).
«Призрачный» бамп поколения строку не создаёт. Ревью-раунды без новой подачи (замечания исправлены
тем же коммитом) в этой версии не различаются; `task_history.review_round` (§3) позволит уточнить
правило в `definition_version = 2`.

Отличие от существующего `first_pass_acceptance_rate`: тот считает по вердиктам ревью из `events`
(14 дней), этот — по исходу после merge и с брошенными задачами в знаменателе. На странице остаётся
один «first pass» (§7).

### 4.6 Положительное подтверждение

Неопровергнутость — отсутствие плохих новостей. Положительные независимые сигналы, которые хаб уже имеет:

| # | Подтверждение | Источник |
|---|---|---|
| P1 | Гипотеза задачи проверена и сдвинулась | `outcome_answers.verdict = 'moved'` по последнему ответу (`OutcomeVerdict`, `hub/models.py:3872`) |
| P2 | Человек одобрил на гейте вердикта или DoR текущего поколения | `human_gates` (`HUMAN_GATE_EVENT_KINDS`) — **но** события живут 14 дней; для P2 нужен постоянный след (§9, вопрос 3) |

**Confirmed Delivery Rate (CDR)** = доля задач с исходом `unrefuted` и P1. P2 в v2 не используется
до решения по хранению. У задач без `outcome_metric` P1 неприменим — они идут в `unconfirmable`,
и их доля отдаётся отдельно.

### 4.7 Окна наблюдения

Окна не выбираются наугад. Каждое — P80 соответствующей задержки по данным, пересчитывается при
смене `definition_version`, а не на лету:

| Окно | Задержка, по которой берётся P80 | Стартовое значение до данных |
|---|---|---|
| `REVERT_WINDOW_DAYS` | `delivery_reverts.detected_at − pipeline_merges.merged_at` | 14 |
| `DEFECT_WINDOW_DAYS` | `detected_at − MIN(releases.deployed_at)` по дефектам с `caused_by_task_id` | 30 |
| `RELEASE_WAIT_DAYS` | `releases.deployed_at − pipeline_merges.merged_at` | 14 |
| `UDR_HORIZON_DAYS` | P80 `started_at → merged_at` + `RELEASE_WAIT_DAYS` + `DEFECT_WINDOW_DAYS` | 60 |

Ответ отдаёт `windows` с фактическими значениями и n, по которому взят P80.
Раздел `windows_audit` показывает, какая доля реальных откатов и дефектов попала бы за окна.

### 4.8 Снимок когорты

Чтобы `deleted` был измерим, а удаление задачи не меняло прошлые окна: новая таблица
`task_cohort (task_id PRIMARY KEY, started_at, project_id, work_type, size, risk_class,
submission_model, result_kind, is_leaf)`, строка пишется триггером при первом `started_at`
(`AFTER UPDATE OF started_at` и `AFTER INSERT` с непустым `started_at`). Без FK.
Разрезы берутся из снимка на момент старта, а не из текущих значений: переклассификация задачи
задним числом не двигает метрику.

### 4.9 Формулы

```
n          = созревшие задачи когорты (без out_of_scope)
UDR        = unrefuted / n                                  ± Wilson 95%
UDR_full   = то же, только verifier = full (R1 и R3 применимы)
CDR        = unrefuted ∧ P1 / n_confirmable
first_pass = unrefuted ∧ COUNT(submissions) = 1 / n
review_only_share = задачи без тестовых AC / доставленные через гейт   (ниже — лучше)
```

**Главное число — `UDR_full`.** Смешанный UDR рядом, с долей `review_only`. Смешанный UDR как главный
вознаграждал бы задачи без тестовых AC: R1 к ним неприменим, и опровергнуть их труднее (Goodhart).

`unchecked` не входит ни в успех, ни в провал: он в знаменателе, но отдаётся отдельно, и ответ несёт
`udr_if_unchecked_pass` как верхнюю границу.

### 4.10 Защита от подкручивания

- **AC фиксируются на момент подачи.** `submissions` получает `ac_snapshot` (new): `ac_id`,
  `verifiable_by`, `test_ref` на момент подачи. R1 читает AC из снимка последней подачи. Удаление AC или
  смена `verifiable_by` с `test` на другое после первой подачи отдаётся счётчиком `ac_weakened`
  (по разнице снимков) и показывается в проблемных местах.
- **Исправления регрессий.** Задача с `cause_kind = 'regression'` и `caused_by_task_id = X` в
  когорте остаётся (это реальная работа), но в разрезе `by_cause` выделяется, а её стоимость
  дополнительно относится на X (§5.4).

### 4.11 Ответ

```json
"delivery_outcomes": {
  "definition_version": 1, "history_since": "2026-11-01",
  "cohort": {"started_from": "2026-06-23", "started_to": "2026-09-21"},
  "windows": {"revert_days": 14, "defect_days": 30, "release_wait_days": 14, "horizon_days": 60, "n_basis": {...}},
  "n": 63, "immature": 22, "out_of_scope": 5,
  "outcomes": {"unrefuted": 38, "unchecked": 6, "refuted": 7, "bypassed": 1, "accepted_undelivered": 2,
               "merged_unaccepted": 0, "stalled": 4, "abandoned": 5, "deleted": 0},
  "out_of_scope_by": {"state": 2, "no_pr": 2, "spike": 1},
  "refuted_by": {"ac_failed": 1, "reverted": 3, "prod_defect": 2, "reopened": 1},
  "udr_full": {"value": 0.64, "ci95": [0.49, 0.77], "n": 41},
  "udr": {"value": 0.60, "ci95": [0.48, 0.71], "n": 63}, "udr_if_unchecked_pass": 0.70,
  "cdr": {"value": 0.31, "ci95": [...], "n": 29}, "unconfirmable": 34,
  "first_pass": {"value": 0.41, "ci95": [...], "n": 63},
  "review_only_share": 0.35, "ac_weakened": 2, "labels_pending": 3,
  "by_via": {"gate": 51, "backfill": 0, "unknown": 2},
  "by_acceptance": {"force_complete": 2, "decide": 1},
  "by_work_type": [...], "by_size": [...], "by_risk_class": [...], "by_implementer_model": [...], "by_cause": [...]
}
```

Каждый счётчик ведёт к списку задач (Q4 из `metrics-ux.md`).

## 5. Cost per Unrefuted Task (CUT)

### 5.1 Компоненты

Стоимость задачи — по **всем поколениям и всем прогонам**, включая упавшие. Отдельная функция метрики
`task_cost(db, task_ids)` в новом модуле `hub/services/task_cost.py`. Потолок ревью
(`_tokens_already_spent`, `review_dispatch.py:5616`) **не** переиспользуется: он намеренно считает NULL
нулём и не читает `billed_tokens` — у потолка и метрики разные цели.

| Компонент | Источник | Единица | Неизвестно, когда |
|---|---|---|---|
| исполнитель | `executor_runs.cents` (фактические центы облачного прогона) | центы | задача сделана локальной сессией, или `cents IS NULL` у завершённого прогона |
| ревью | `review_dispatches`: `billed_tokens`, иначе `provider_tokens`, по `model` заказа; отчёты без заказа — `machine_reviews.provider_tokens`, иначе `tokens_spent` (самоотчёт, флаг `self_reported`) | токены по модели | ни одного из полей |
| стюард | `steward_judgements.tokens_spent` по `model` | токены по модели | `tokens_spent IS NULL`; причина из `tokens_unknown_reason` |
| CI | `ci_attempt_timings.runner_seconds` | минуты раннера | у задачи есть CI-отчёты, но нет таймингов, или `state <> 'ok'` |

**Минуты человека — не компонент денежной стоимости.** `owner_minute_accruals` — оценка по нормам,
отказы, возвраты и `task_completed` намеренно не считаются (`docs/agent-context/invariants.md`),
начисления есть только с #1874 (19.10.2026). Они отдаются отдельным рядом
`human_minutes_per_unrefuted` с пометкой `estimate` и `norms_version`, задачи до #1874 — `unknown`.

### 5.2 Цены

```python
# hub/config.py
METRIC_PRICES_VERSION = 1
METRIC_PRICES = {
    # центов за 1M токенов, по семье модели (model_family.py); "input"/"output"/"cache_read"
    "tokens": {"claude-opus": {"input": ..., "output": ..., "cache_read": ...}, ...},
    "tokens_unsplit_ratio": {"input": 0.8, "output": 0.2},  # когда классы токенов неизвестны
    "runner_minute_cents": ...,
}
```

- Цена берётся по модели **прогона** (`review_dispatches.model`, `steward_judgements.model`), а не по
  `tasks.submission_model`.
- Классов токенов в хабе сейчас нет: `billed_tokens` и `provider_tokens` — одно число. Пока так, цена
  считается по `tokens_unsplit_ratio`, а ответ несёт `price_basis: "unsplit"`. Это известная ошибка
  в разы для задач с большим кэшем; она названа, а не спрятана.
- Центы исполнителя — фактические, по ценам на момент прогона. Конфиг-цены к ним не применяются.
  Компонент помечен `price_basis: "actual"`.
- Модель вне таблицы цен — компонент `unknown` с причиной `unpriced_model`, а не цена по умолчанию.

### 5.3 Формулы

Знаменатель везде один — `unrefuted` той же когорты §4.1. Числитель — стоимость **всех** созревших задач
когорты, включая неуспешные.

```
CUT_component[c] = Σ cost_c(задачи с известным c) / unrefuted × (n / n_known_c)
CUT              = Σ_c CUT_component[c]
cost_per_task    = распределение cost(task) по задачам: median, P90, бутстреп-интервалы
waste_share      = Σ cost(все исходы, кроме unrefuted и unchecked) / Σ cost   (по известным компонентам)
verification_overhead = Σ (ревью + стюард + CI) / Σ cost
```

`n / n_known_c` — экстраполяция компонента на задачи, где он неизвестен, в предположении, что неизвестные
стоят как известные. Предположение явное: ответ отдаёт `coverage[c] = n_known_c / n` и для каждого
компонента — `CUT_component_lower` (неизвестное как 0). При `coverage < 0.5` компонент в сумму CUT не
входит, а `CUT` получает флаг `partial` со списком выпавших компонентов. Сейчас так будет с исполнителем:
CUT без исполнителя — основное число, это написано словами на странице.

При `unrefuted < MIN_COMPARE_N` CUT не отдаётся (`small_sample`), только сумма и n.

### 5.4 Атрибуция

- **Исправление регрессии.** Стоимость задачи с `cause_kind = 'regression'` и `caused_by_task_id = X`
  дополнительно показывается на X в разрезе `cost_with_fixes` (стоимость задачи + её исправлений).
  В основной CUT не дублируется.
- **Общие расходы** (`unattributed_overhead`): прогоны стюарда без `task_id`, CI релиз-PR, ревью и
  рефайнмент на этапе DoR у задач, не вошедших в когорту, spike, роллапы, заказы ревью по удалённым задачам.
  Отдаются суммой и долей от общего; в CUT не входят.

### 5.5 Разрезы

`by_work_type`, `by_size`, `by_risk_class` — по одному измерению. `by_implementer_model` — только внутри
облачного канала (где известен исполнитель), с явной подписью. `by_reviewer_profile`.

### 5.6 Каскадное удаление

`executor_runs`, `ci_attempt_timings` и `review_dispatches` объявлены с `ON DELETE CASCADE`: удаление задачи
стирает её стоимость, а снимок когорты (§4.8) задачу помнит. Решение v2: задача с исходом `deleted`
идёт в CUT как `unknown` по всем компонентам; число таких задач отдаётся. Менять каскады этой спекой
не предлагается.

### 5.7 Ответ

```json
"delivery_cost": {
  "definition_version": 1, "prices_version": 1, "price_basis": {"executor": "actual", "review": "unsplit", ...},
  "cut_cents": {"value": 290, "ci95": [210, 400], "partial": ["executor"]},
  "components": {
    "executor": {"cents": ..., "coverage": 0.34, "included": false, "lower_bound_per_unrefuted": ...},
    "review":   {"tokens": ..., "cents": ..., "coverage": 0.93, "self_reported_share": 0.08, "included": true},
    "steward":  {"tokens": ..., "cents": ..., "coverage": 0.88, "unknown_reasons": {...}, "included": true},
    "ci":       {"runner_minutes": ..., "cents": ..., "coverage": 0.97, "included": true}
  },
  "cost_per_task_cents": {"median": 120, "p90": 610, "ci95_median": [...]},
  "waste_share": 0.22, "verification_overhead": 0.41,
  "human_minutes_per_unrefuted": {"value": 9.5, "estimate": true, "norms_version": 1, "coverage": 0.6},
  "unattributed_overhead": {"cents": ..., "share": 0.12, "by_kind": {...}},
  "by_work_type": [...], "by_size": [...], "by_implementer_model": [...]
}
```

## 6. Rework

Читает `task_history` (§3) и `submissions` по той же когорте, только для задач, начатых после
`history_since`:

| Показатель | Формула |
|---|---|
| `resubmissions_per_task` | `COUNT(submissions) − 1`: медиана, P90, доля > 1 |
| `review_rounds_per_task` | строки `task_history.kind = 'review_round'`: медиана, P90 |
| `ci_fix_rounds_per_task` | строки `task_history.kind = 'ci_fix_round'`: медиана, P90 |
| `rework_after_delivery` | доля доставленных через гейт с исходом `refuted` |

Возвраты считаются по росту счётчиков, а не по рёбрам статусного графа: ребро `review → running`
бывает и при одобрении (`lifecycle.py:5017`).

## 7. Поверхности и место на странице

По `AGENTS.md` контракт меняется сразу на всех поверхностях:

- REST: три новых раздела в `GET /api/metrics/practices` — `delivery_outcomes`, `delivery_cost`, `rework`.
  Те же параметры среза (`metrics_scope.py`).
- CLI: `oc-hub delivery-outcomes`, `oc-hub delivery-cost` — срезы `/practices`, как `defect-clocks`.
- MCP: строки в текстовой сводке `hub_practice_metrics`. Нового инструмента нет, бюджет каталога
  не трогаем; изменение docstring всё равно проверить `scripts/mcp_catalog_budget.py`.
- Web `/metrics`, первый экран (`INDICATORS`, `hub/services/metrics_compare.py:114`). Сейчас показателей 7,
  `metrics-ux.md` AC-1 требует 4–6. Предложение — 6:

  | Было | Стало |
  |---|---|
  | first_pass (по вердиктам) | **first_pass** по §4.5 |
  | cycle feature, cycle since_start feature | один: since_start feature |
  | precision, provider_tokens_per_fixed | остаются |
  | touches_per_delivered | остаётся |
  | CFR | остаётся |
  | — | **UDR_full** |
  | — | **CUT** уходит в «Разбивки», а не на первый экран, пока `partial` |

  Старый first_pass по вердиктам переезжает в раздел ревью под именем «принято ревью с первого раза».
  Две разные «первые подачи» на первом экране не стоят.
- Новые проблемные места (`rank_problem_spots`): `data_gap`, когда `coverage` компонента < 50%, доля
  `unchecked` > 20% или `history_since` моложе окна; `ac_weakened > 0`; рост доли `accepted_undelivered`.

## 8. Разбиение на задачи

| # | Задача | Зависит | Миграция |
|---|---|---|---|
| T1 | `task_cohort` (снимок при старте, триггеры) | — | да |
| T2 | `pipeline_merges.via` + проставление в 4 местах вызова | — | да |
| T3 | `delivery_reverts` + `delivery_revert_coverage`, запись из сборщика для всех доставок | — | да |
| T4 | `submissions.ac_snapshot` | — | да |
| T5 | `delivery_outcomes` (§4) на всех поверхностях | T1–T4 | нет |
| T6 | `task_history` (триггеры статуса и счётчиков) | — | да |
| T7 | `rework` (§6) | T6 | нет |
| T8 | `task_cost.py`, `METRIC_PRICES`, `delivery_cost` (§5) | T5 | нет |
| T9 | Первый экран `/metrics`: 6 показателей, новые проблемные места | T5, T8 | нет |
| T10 | Пересчёт окон по P80 (§4.7) и `windows_audit` | T3, T5 | нет |
| T11 | Постоянный след человеческих решений: вердикты, одобрения DoR, решения (§9.3); после него — P2 в CDR и уточнение `touches_per_delivered` | — | да |
| T12 | Классы токенов (input/output/cache) у прогонов ревью и стюарда (§9.4); после него — `price_basis: "split"` | — | да |

T1–T4 и T6 — только запись данных, без метрик. Их стоит выкатить первыми и как можно раньше: история
копится только с их выката, а для созревания когорты нужен горизонт (стартово 60 дней).

## 9. Решения владельца (21.10.2026)

1. **Горизонт созревания — ждать.** Предварительного показателя нет: ранние числа смещены к быстрым
   задачам. Первое число UDR — примерно через 60 дней после выката T1–T4.
2. **CUT — в «Разбивках».** На первый экран не выносится, пока покрытие исполнителя ниже 50%
   (флаг `partial`). Пересмотр — когда покрытие станет выше.
3. **Постоянный след человеческих решений — да, отдельной задачей T11.** До неё P2 в CDR не используется.
4. **Классы токенов — да, отдельной задачей T12.** До неё денежная оценка ревью и стюарда идёт с
   `price_basis: "unsplit"` и названной погрешностью.
5. **Ручная приёмка без merge — неуспех.** `accepted_undelivered` остаётся в знаменателе.
   Сознательная отмена (`disposition = 'abandon'`) — `abandoned`.
