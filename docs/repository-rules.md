# Правила ведения репозитория

## Назначение

Этот репозиторий содержит standalone OpenClaw Hub: FastAPI backend, Web UI, CLI, MCP tools, интеграции, документацию для агентов и регрессионные тесты.

Репозиторий должен оставаться воспроизводимым: новый разработчик или агент должен иметь возможность поднять окружение, понять архитектуру, внести узкое изменение и проверить его без устных договоренностей.

## Ветки

- Основная ветка: `main`.
- Рабочие ветки для задач: `task-<hub-task-id>/<short-slug>`.
- Для исправлений без задачи: `chore/<short-slug>` или `fix/<short-slug>`, но предпочтительно сначала создать задачу в хабе.
- Одна исполняемая задача - одна ветка.
- Не смешивать независимые изменения в одной ветке.

## Коммиты

Коммиты должны быть небольшими и объяснять поведение, а не только измененные файлы.

Рекомендуемый формат:

```text
<type>: <short summary>
```

Допустимые `type`:

- `feat`: новая возможность;
- `fix`: исправление ошибки;
- `docs`: документация;
- `test`: тесты;
- `refactor`: рефакторинг без изменения поведения;
- `chore`: обслуживание, конфигурация, инфраструктура;
- `ci`: CI/CD.

Примеры:

```text
docs: add human agent workflow implementation plan
fix: pass force flag through mcp approve tool
test: cover pending report force complete api
```

## Что хранить в git

Хранить:

- исходный код `hub/`;
- тесты `tests/`;
- документацию `docs/`;
- агентские роли `agents/`;
- project skills `skills/`;
- Cursor/MCP project config, если он нужен команде;
- `pyproject.toml`;
- `uv.lock`, чтобы зависимости были воспроизводимыми.

Не хранить:

- `.venv/`;
- `.ruff_cache/`, `.pytest_cache/`, `__pycache__/`;
- локальные SQLite базы `*.db`;
- coverage/htmlcov artifacts;
- `.DS_Store`;
- секреты, токены, private keys, `.env` с реальными значениями.

## Зависимости

- Добавлять зависимости только через `uv add <package>`.
- Dev-зависимости добавлять через dependency group, а не вручную редактировать lock без причины.
- После изменения зависимостей коммитить и `pyproject.toml`, и `uv.lock`.

## Перед началом изменения

1. Прочитать `docs/agent-context/system-map.md`.
2. Найти область изменения в `docs/agent-context/change-map.md`.
3. Если изменение касается lifecycle, schema, DoR, MCP, CLI или integrations, прочитать `docs/agent-context/invariants.md`.
4. Для выбора тестов открыть `docs/agent-context/testing-playbook.md`.
5. Если задача относится к human + AI-agent workflow, открыть:
   - `docs/software-development-workflow.md`;
   - `docs/software-development-workflow-implementation-plan.md`.

## Контрактные изменения

Если меняются request/response модели, статусы, поля задачи или API behavior, нельзя менять только один слой.

Проверить и при необходимости обновить:

- `hub/models.py`;
- `hub/app.py`;
- `hub/cli.py`;
- `hub/mcp_server.py`;
- `hub/web.py` и templates;
- `hub/repository.py`;
- `hub/db.py`, если меняется persisted schema;
- affected tests.

MCP и CLI должны оставаться поведенчески согласованными с REST API.

## Миграции

- Любое новое persisted поле или изменение схемы вносить через `hub/db.py`.
- Добавлять тесты свежей схемы и migration path.
- Не трактовать отсутствующие structured columns как пустые silently, если это скрывает ошибку миграции.

## Тестирование

Использовать только:

```bash
uv run pytest
```

Не использовать:

```bash
python -m pytest
```

Базовая проверка перед крупным завершением:

```bash
uv run ruff check hub tests
uv run ruff format hub tests
uv run pytest -q
```

Для узких изменений сначала запускать focused suite из `docs/agent-context/testing-playbook.md`, затем расширять при необходимости.

## Работа агентов

Агенты обязаны:

- работать из корня репозитория;
- держать изменения узкими;
- не переписывать чужие изменения;
- перед кодом читать контекст задачи и архитектурные документы;
- фиксировать вопросы, блокеры и результаты через OpenClaw Hub;
- не расширять scope молча, а создавать draft proposal для новой работы;
- в done report указывать changed files, behavior change и validation commands.

## Pull request / review checklist

Перед merge проверить:

- задача или причина изменения понятна;
- acceptance criteria покрыты;
- validation commands выполнены или явно указано, почему не выполнены;
- API/CLI/MCP/Web не разошлись в контрактах;
- миграции добавлены для persisted schema changes;
- тесты добавлены или обновлены для поведения;
- docs обновлены, если меняется workflow или публичный контракт;
- нет секретов и локальных артефактов.

## Релизы

Пока нет отдельного release process. До его появления:

- все production-relevant изменения проходят через PR/review;
- release notes собирать из merged commits и task done reports;
- перед релизом запускать полный `uv run pytest -q` и ruff check.
