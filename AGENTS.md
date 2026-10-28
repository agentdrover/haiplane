# Haiplane Hub Standalone

## General Rules

- Work from this repository root only.
- Keep changes narrow and intentional.
- Follow repository workflow rules in `docs/repository-rules.md`.
- **Merge into `main` auto-deploys to the production-like server** via
  `.github/workflows/ci.yml` (job `deploy`, runs after tests pass on push to
  `main`). Treat any merge to `main` as a release. See `deploy/CD.md`.
- Use `uv run pytest`; do not use `python -m pytest`.
- Use `uv add <package>` for dependencies.
- If a change touches API contracts, update `hub/cli.py`, `hub/mcp_server.py`, and affected tests in the same pass.
- If a change touches persisted task data or schema, a migration is a NEW FILE `hub/migrations/steps/m<YYYYMMDDHHMM>_<task>_<what>.py` (`MIGRATIONS` list; `part_1..3` are frozen; a merged step file is never edited; `hub/db.py` only applies them). Tests of the feature go in a thematic test file, not in `test_db_migrations.py`.
- Функция сверх бюджета сложности красит CI (`scripts/complexity_budget.py`,
  #1066). Пороги — в `pyproject.toml`, ledger исключений — в
  `docs/agent-context/complexity-budget.json`. Разбейте функцию; если это
  невозможно, внесите запись через `--update` и впишите `removed_by` руками —
  `task:#NNNN`, `permanent` с причиной или честное `unclaimed`. Запись
  снимается удалением строки: если нарушения больше нет, проверка сама
  потребует её убрать.
  `unclaimed` с живым замером от 90% своего `max` тоже красит CI (#1693):
  назначьте `task:#N` или `permanent — <причина>`; формат `removed_by`
  проверяется. Замеры обновляет `--refresh-measured` (меняет только `measured`,
  `max` не трогает); `--update` пересчитывает и потолки — это решение человека.
- Новый assert, сравнивающий настенное время с порогом меньше 1 с, красит CI
  (`scripts/wallclock_asserts.py`, #1989): на загруженной машине такой тест
  падает без дефекта. Подмените часы (monkeypatch) или проверяйте счётчик, а не
  секунды. Старые места — в `docs/agent-context/wallclock-asserts.json`;
  `--update` добавляет новое место с пустым `removed_by` (впишите руками
  `task:#NNNN`, `permanent: <причина>` или `unclaimed: <причина>`). Запись
  снимается удалением строки, когда места больше нет.
- Файл .py выше порога размера красит CI (`scripts/file_size_budget.py`, #1669).
  Пороги по деревьям — в `pyproject.toml` (`[tool.haiplane.file_size]`), ledger —
  в `docs/agent-context/file-size-budget.json`. Новый файл выше порога или рост
  файла из ledger выше потолка: разбейте файл; если нельзя, `--update` и впишите
  `removed_by` руками (`task:#NNNN`, `permanent` с причиной или `unclaimed`).
  Запись снимается удалением строки, когда файл вернулся под порог.
  Дерево tests (#1932): порог 800 = `REVIEW_FILE_LINE_CAP`, ledger только
  сокращается — CI сверяет его с базой: новая запись `tests/…` или поднятый
  потолок красят CI, `--update` запись tests не поднимает. Тесты задачи — в
  новом файле до 800 строк. База не прочитана (ссылка, событие push, pyproject
  или ledger базы) — rc 2, в том числе у `--update`; `--update` без доступа к
  базе возможен только с `--no-base`.
  Предупреждения «вырос сверх замера» (файл из ledger) и «у порога» (файл вне
  ledger, `near_pct` в `pyproject.toml`) CI не красят (#1676). Тест или код не
  ужимается ради порога — не ужимать, а делить по темам: новый тест кладите в
  тематический файл (существующий или новый `tests/test_<область>_<тема>.py`),
  новый код — в отдельный модуль. «У порога» — повод разделить файл в той задаче,
  которая его вызвала, или завести задачу на разделение.
- Новый импорт в запрещённом направлении слоёв красит CI (`scripts/layer_rules.py`,
  #1852). Таблица слоёв, запреты и ledger текущих исключений — в
  `docs/agent-context/layer-rules.json`. Уберите импорт; если нельзя, `--update`
  допишет ребро с плейсхолдером, а `removed_by` впишите руками: `task:#NNNN`,
  `permanent — <причина>` или `unclaimed — <причина>`. Запись снимается
  удалением строки, когда ребра больше нет.
- If a change touches MCP tool docstrings or signatures, it changes the published
  `tools/list`: run `uv run python scripts/mcp_catalog_budget.py` (CI runs it too).
  If the headroom is not enough, follow `working_headroom_note` in
  `docs/agent-context/mcp-catalog-budget.json` — the one refill policy (#1241);
  do not raise the budget with `--update`.
- Prefer focused validation:
  - `make check-fast` before a push (#2000): all the static checks of
    `make check` plus the tests the diff makes worth running (your own, the
    tree guards, readers of the files you changed). The full suite is CI's job
    (dispatch it before submitting); a miss costs one CI cycle.
  - `uv run ruff check hub tests`
  - `uv run ruff format hub tests`
  - `uv run pytest -q`
  - `uv run python scripts/surface_parity.py` before submitting: names the
    contract surfaces (REST/CLI/MCP, metric and page, schema and storage) that
    usually change together and are missing from this diff. Warning only —
    always exits 0, so either change them too or say in the submission why they
    do not apply (#833).

## Fast Start For Agents

- Read `docs/agent-context/system-map.md` before exploring the codebase.
- Use `docs/agent-context/change-map.md` to decide which files and tests to touch.
- Read `docs/agent-context/invariants.md` before changing lifecycle, schema, DoR, or integrations.
- Read `docs/agent-context/testing-playbook.md` before choosing validation scope.
- If the task is mostly onboarding, architecture, or impact analysis, use `skills/project-context/`.

## Repo Map

- `hub/app.py`: FastAPI app and API routes
- `hub/web.py`: dashboard and HTMX routes
- `hub/services/`: lifecycle, readiness, orchestration, refinement
- `hub/db.py`: schema, migration runner, serialization helpers
- `hub/migrations/`: the migration list (`_MIGRATIONS`) and its SQL constants
- `hub/repository.py`: SQL access layer
- `hub/models.py`: contracts and enums
- `hub/cli.py`: `oc-hub`
- `hub/mcp_server.py`: MCP surface for agents
- `hub/cli_templates/work_types/`: task templates
- `tests/`: regression coverage

## Agent Roles

- `agents/architect-analyst.md`
- `agents/python-senior-developer.md`
- `agents/testing-agent.md`
- `agents/code-reviewer.md`
- `agents/devsecops.md`

## Project Skills

- `skills/project-context/`
- `skills/hub-development/`
- `skills/hub-testing/`
- `skills/hub-task-prep/`
- `skills/hub-submit-task/`: правила сдачи задачи на ревью
- `skills/hub-review-report-reading/`: правила чтения отчёта ревью
