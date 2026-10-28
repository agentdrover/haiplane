# Testing Playbook

Pick the smallest useful validation set first, then widen only if the diff crosses boundaries.

## Locally: `make check-fast`; the full suite is CI's (#2000)

`make check-fast` is `make check` with the targeted tests instead of the whole
suite: lint, format, mypy, the budgets, layers, migration steps and the security
scans, then `scripts/changed_tests.py` runs

- the `tests/test_*.py` files you changed or added;
- the tree guards of `docs/agent-context/tree-guards.json` (tests that check a
  property of the whole repository: a new long file, a leftover name, a broken
  link, a hub-wide rule);
- the test files that mention the path of a changed non-Python file (a template,
  a JSON ledger, a doc).

Up to 8 files it runs in-process (`-n 0`: every xdist worker imports
`tests/conftest.py` again), more on `-n 4`. It is a convenience, not evidence: it
does not follow imports or symbols (measured on 68 red CI runs: the author's own
tests catch 28% of the failing files, plus the guards 75-82%, and names or the
change-map add 7 points for ten times the selection). A test it misses is found
by CI at the price of one cycle; dispatch CI before submitting, as always.

A clone that ran `make hooks` also runs it before every push of a `task-*`,
`fix/*`, `chore/*`, `docs/*` or `ci/*` branch (`haiplane.prePushCheck`; the result
is remembered per tree, `HAIPLANE_SKIP_PREPUSH=1` skips it once). The hub never
sets that key in the clones it prepares on its own host. A new test that walks the
repository tree goes into the ledger (`tests/test_tree_guards.py` says when).

## Baseline Commands

- Lint: `uv run ruff check hub tests`
- Format: `uv run ruff format hub tests`
- Full suite: `uv run pytest -q`

## By Change Type

| Change Type | First Tests |
|---|---|
| REST API route or error behavior | `uv run pytest tests/test_api.py -q` |
| Context / breadcrumb / child-view behavior | `uv run pytest tests/test_api_context.py -q` |
| Refine payloads or structured updates | `uv run pytest tests/test_api_refine.py -q` |
| Approval gate or DoR failures | `uv run pytest tests/test_api_approve_gate.py -q` |
| Models or enum validation | `uv run pytest tests/test_models.py -q` |
| Repository or SQL behavior | `uv run pytest tests/test_repository.py tests/test_repository_structured.py -q` |
| DB migration behavior | `uv run pytest tests/test_db_migrations.py -q` |
| Lifecycle and service logic | `uv run pytest tests/test_services.py -q` |
| DoR / readiness / recommendations | `uv run pytest tests/test_dor.py tests/test_readiness.py tests/test_recommendations.py -q` |
| MCP tools | `uv run pytest tests/test_mcp_server.py -q` |
| CLI | `uv run pytest tests/test_cli*.py -q` |
| Web dashboard and templates | `uv run pytest tests/test_web*.py -q` |
| Poller or orchestration flows | `uv run pytest tests/test_poller.py -q` |
| Auth | `uv run pytest tests/test_auth.py -q` |

## Expansion Rules

- If a change touches `hub/models.py`, run model tests plus the nearest consumer tests.
- If a change touches `hub/db.py` or `hub/migrations/`, run migration tests and repository tests.
- If a change touches `hub/services/lifecycle.py`, include API approve or service tests even if the diff looked internal.
- If a change touches `hub/mcp_server.py`, run MCP tests and the API tests that back the same operation.

## Regression Discipline

- Bug fixes should normally ship with a regression test.
- Schema changes should be validated against both fresh-state and migration scenarios.
- Contract changes should be tested at the narrowest public surface that exposes them.

## Universal Review Gate Changes

Any change that touches review-gate behavior (`completion_requires_review`,
`transition_after_agent_done`, `submit_for_review`, `record_review_verdict`,
poller review handling, `refresh_task`) must run the focused gate suites
before the full run:

```bash
uv run pytest tests/test_services.py tests/test_api.py tests/test_mcp_server.py tests/test_poller.py tests/test_cli*.py -q
```

Key regression anchors: stale-approval invalidation, gate routing of done
reports (review / ci_check / needs_decision at cycle limit), MCP envelope
projection (`awaiting=review`, `actor_hint`), poller convergence on
`transition_after_agent_done`, and the audited `force_complete` override.

## Web tests moved by theme (#1677)

`tests/test_web.py` was split by meaning into `tests/test_web*.py` (tasks and
inbox, `_queue`, `_projects`, `_policy`, `_review_verdict`, `_review_evidence`,
`_admin`, `_ui`); shared helpers and the autouse fixture
`_form_carries_policy_version` live in `tests/web_support.py`. Test bodies are
unchanged, but a test's nodeid changed with its file: old nodeids in closed
tasks (`tests/test_web.py::name`) are not rewritten (variant 1); find a test by
name with `grep -rn "def <name>" tests/test_web*.py`. Each file stays under
2000 lines and 200 000 bytes so the AC locator can read it (256 KB limit).

## База теста — копия шаблона (#1958)

Фикстура `db` отдаёт копию базы, собранной один раз на воркер
(`tests/template_db_support.py`): подготовка стоила ~0,5–1,4 с на тест против
~0,05–0,1 с самого теста. Новый seed или шаг подготовки базы добавляется в
`apply_build` (одна функция и для шаблона, и для полного пути), а не в
фикстуру. Тест, подменяющий `hub.db._MIGRATIONS`, `_SCHEMA` или функции
сборки до запроса `db`, получает полный путь автоматически. Сходство шаблона с
прежней сборкой держит `tests/test_template_db.py` (эталон там записан
отдельно): при расхождении краснеет он.

## Бюджет сложности (#1066, #1693)

- `uv run python scripts/complexity_budget.py` красит CI при исключении `unclaimed`, у которого живой замер от 90% своего `max`, и при `removed_by` не по формату (`task:#N`, `permanent — <причина>`, `unclaimed — <причина>`). Лечение: назначить задачу или объявить постоянным с причиной.
- Сверить `measured` без пополнения запаса: `--refresh-measured`. `--update` пересчитывает потолки и остаётся решением человека.

## Тесты не зависят от настроек машины (#1851)

- Тест не опирается на личность git, системный rsync, права не-root или версию Python машины: зависимость убирают в тесте или фикстуре (env, шим, проходимый каталог), assert не ослабляют. Облачный контейнер claude.ai/code (root, без rsync, с `GIT_CONFIG_COUNT`) — штатная среда, `make check` в нём обязан быть зелёным.
- Пропуск — только когда возможности в окружении нет и обойти это нельзя; предикат живёт в `tests/env_caps.py`, возвращает причину и в CI (`GITHUB_ACTIONS=true`) — `None`. Сторож `tests/test_environment_policy.py::test_every_env_cap_runs_in_ci` проверяет каждый публичный предикат модуля.

## Корпус промахов ревью (#1864)

Проверка ревьюера и судьи стюарда на дефектах, которые они однажды пропустили, а не на ощущениях. Корпус — `tests/fixtures/review_eval/cases.json`, счёт — `scripts/review_eval.py` (оффлайн, база только на чтение, сети нет, вывод побайтово одинаков на одной базе и корпусе).

**Как добавить случай после промаха**
- Одна сдача — один случай: `task_id`, `submission_generation`, полный `submission_sha` (40 hex; скрипт сверяет его с таблицей сдач, расхождение — `sha_mismatch`, в счёт не идёт), `expectation` = `defect` или `clean`.
- Истина — только вердикт человека (`truth.kind=human_review`, `update_id` записи `kind=review`) или прод-дефект (`truth.kind=prod_defect`, `task_id` и `caused_by_task_id`). Находки автора (`finding_outcomes`) истиной не считаются.
- Дефектная: `defect_class` из словаря `defect_classes` корпуса и `locators` — файл и диапазон строк; мест одного класса может быть несколько. В локаторы идут только находки в объёме задачи. Нет места в коде на момент сдачи — локатор пуст, допустимо только для прод-дефекта (машинное ревью по такому случаю не считается, судья и советник считаются).
- Чистая: вердикт `APPROVED`, запись о доставке (`delivery`), `checked_at` и `window_days` (не меньше 14; меньшее валидатор отвергает, bool тоже). Пока с доставки прошло меньше окна, случай `provisional`; окно считается по полям корпуса, а не по часам. Чистая берётся только без прод-дефекта и возврата.
- Секретов и кода исполнителей в корпусе нет: ссылки, метаданные, одна строка заметки. `tests/test_review_eval.py::test_corpus_schema_and_size` красится на неполном случае, коротком sha, нехватке случаев (дефектных ≥ 8, чистых ≥ 5) и на признаках секрета.

**Как читать счёт** (`uv run python scripts/review_eval.py [--db путь]`)
- Машинное ревью: «обнаружил» — в любом несамом отчёте поколения (перенос `carried_from_review_id` без дублей) есть подтверждённая находка с файлом локатора и строкой в окне ±20. Нераспознанная (`unresolved`) или находка с файлом без строки — только «насторожился». Ложная тревога — подтверждённая находка на чистой. Отчёты `self_reviewed=true` не входят.
- Судья: «удержал» — не approve. Знаменатель — суждения по существу (удержал + одобрил); процедурная эскалация (`PROCEDURAL_ESCALATES`) и «суждения нет» выведены отдельными строками, без суждений по существу пишется «нет суждений по существу», а не 0/1.
- Советник: только там, где судья нового контура одобрил. `object` по существу на дефектной — «поймал», `concur` — «пропустил», `object` на чистой — ложная тревога; `object`, превращённый из `concur` процедурно, а также `missing`, `timeout`, `pending`, `refused`, `not_applicable` — вне счёта и выведены отдельно.
- У каждой доли названы знаменатель и покрытие корпуса. «Нет данных» — не ноль: случай без отчёта или суждения в знаменатель не попадает и показан отдельно.
- Чистых пока почти все `provisional`: ложные тревоги по ним — предварительные.

**План платного перепрогона:** `--plan-cap N` печатает цели без дублей по `(task, generation, sha)` (случаи с `sha_mismatch` и без сдачи в план не входят и места под потолком не занимают) и цену: `billed_tokens` (выгрузка), иначе `provider_tokens` с пометкой «нижняя граница», иначе «нет данных» (в сумму не входит, не ноль). Скрипт ничего не запускает: перепрогон заказывает человек, прочитавший число.
