# Правила ведения репозитория

## Назначение

Этот репозиторий содержит standalone OpenClaw Hub: FastAPI backend, Web UI, CLI, MCP tools, интеграции, документацию для агентов и регрессионные тесты.

Репозиторий должен оставаться воспроизводимым: новый разработчик или агент должен иметь возможность поднять окружение, понять архитектуру, внести узкое изменение и проверить его без устных договоренностей.

## Ветки

- Основная стабильная ветка: `main`.
- Интеграционная ветка разработки: `develop`.
- Рабочие ветки для задач: `task-<hub-task-id>/<short-slug>`, создаются от `develop` и вливаются обратно в `develop`.
- Для исправлений без задачи: `chore/<short-slug>` или `fix/<short-slug>`, но предпочтительно сначала создать задачу в хабе.
- Одна исполняемая задача - одна ветка.
- Не смешивать независимые изменения в одной ветке.
- `main` защищается на GitHub: изменения должны попадать туда из `develop` после проверки, кроме явно подтвержденных emergency-изменений владельцем репозитория.
- Повседневная работа агентов ведется в `develop` или короткоживущих task/fix/chore ветках, не напрямую в `main`.
- Ветки агентов должны быть короткоживущими: после merge удалить remote branch и закрыть связанную задачу/отчет.

## Жизненный цикл ветки задачи

- Ветка задачи создается от актуального `develop` (`git fetch origin` перед созданием).
- Доработка после `changes_requested` выполняется в **той же ветке** той же задачи:
  исправить findings по номерам и снова отправить работу через
  `hub_submit_for_review`. Ревью видит только новый сабмишен (новый submission
  generation) — push коммитов в ветку сам по себе повторное ревью не запускает.
- **Запрещено** базировать новую task-ветку на невлитой ветке, находящейся на
  ревью. Ревью задачи A не видит ветку задачи B, а стек веток ломает порядок
  merge (инцидент #392: fix-ветки #424→#425→#426 были основаны на невлитой
  ветке task-392, находившейся на ревью).
- Исключение: осознанный стек веток допустим только с явным порядком merge,
  зафиксированным в task update каждой затронутой задачи.
- Полный review-цикл и submission generations описаны в
  `docs/software-development-workflow.md` (Universal Review Gate).

## Цикл ревью и маршрутизация находок

Каждая находка ревью имеет scope (`in_scope`|`out_of_scope`, см. #435)
и закрывается ровно одним из двух маршрутов:

- **in-scope** → вердикт `changes_requested`, задача возвращается в
  `running`, фикс в той же ветке той же задачи, resubmit через
  `hub_submit_for_review` (см. «Жизненный цикл ветки задачи» выше).
  `changes_requested` требует хотя бы одну in-scope находку.
- **out-of-scope** → отдельная задача + ссылка `linked_task_id` в находке
  вердикта. Такие находки не блокируют вердикт: если все находки
  out-of-scope — вердикт `approved` с рекомендациями.

Маршруты не смешивать: **запрещено заводить параллельные задачи на
in-scope находки** — они закрываются только через resubmit той же задачи
(инцидент #392: параллельные fix-задачи на in-scope находки вместо
доработки сломали порядок merge).

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

Правила для LLM-агентов:

- Не создавать commit без прямого запроса пользователя.
- Перед commit показать `git status`, diff и выбранный список файлов.
- Не добавлять чужие незакоммиченные изменения, секреты, локальные базы, cache artifacts.
- Не использовать `--amend`, `rebase`, force-push или skip hooks без явного подтверждения пользователя.

## Push / Pull Request

- Remote по умолчанию: `origin` на приватный GitHub-репозиторий.
- Первый push новой ветки: `git push -u origin HEAD`.
- Push в `main` не является обычным рабочим процессом; предпочтительный путь - PR из рабочей ветки.
- PR должен использовать `.github/pull_request_template.md`, указывать задачу/intent, LLM work log, validation commands и риски.
- Перед merge проверить GitHub CI и review checklist ниже.
- Локальная защита push хранится в `.githooks/pre-push`. **Вооружается шагом установки:** `make setup` ставит окружение и активирует хук вместе. Вручную: `git config core.hooksPath .githooks`. Проверить: `make doctor` — ненулевой код, если push не будет проверен.
- Копирование хука в `.git/hooks` (как советовала прежняя редакция) тоже работает, но копия перестаёт соответствовать политике при первом же её изменении, и никто об этом не узнаёт. `doctor` такой клон распознаёт и предлагает перейти на `core.hooksPath`.
- Emergency push в `main` допускается только владельцем и только явно: `ALLOW_MAIN_PUSH=1 git push origin main`.

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
- в done report указывать changed files, behavior change и validation commands;
- **Universal Review Gate**: нормальное завершение задачи возможно только при
  APPROVED-ревью для текущего сабмишена (`hub_submit_for_review` →
  `hub_get_review_brief` → `hub_submit_review`). Done report без актуального
  одобрения не завершает задачу, а отправляет её в `review`. Исключения:
  `auto_review=false` (явный опт-аут при создании) и audited human overrides
  (`hub_decide_task` accept, `hub_force_complete_task`). Merge в `main`
  остаётся релизом — гейт дополняет, а не заменяет PR/CI-процесс.

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

## CI/CD и авто-деплой

- CI и CD описаны в `.github/workflows/ci.yml`.
- На каждый `pull_request` и `push` в `main` **и в `develop`** запускается job
  `test`: `ruff check`, `ruff format --check`, `pytest`, `pip-audit`, `bandit`,
  `detect-secrets`.
- **`mergeStateStatus=CLEAN` не означает «CI прошёл».** Это ответ GitHub
  «обязательных проверок нет» — на приватном репо без branch protection он
  выглядит одинаково и когда проверки прошли, и когда их не запускали вовсе.
  Перед merge смотреть `gh pr checks <N>`, а не `mergeStateStatus`.
  Инцидент 28.07.2026: стек #505–#510 слит в `develop` при `CLEAN` на всех
  шести PR — CI не запускался ни разу (триггеры были только на `main`), и в
  `develop` уехали три дефекта, найденные лишь релизным PR (#543).
- **Любой merge в `main` автоматически выкатывается на сервер.** Job `deploy`
  стартует только после успешного `test` и только на `push` в `main`
  (на pull_request деплой не идёт).
- Деплой повторяет ручной процесс из `docs/agent-deploy-runbook.md` раздел 4:
  `rsync` в staging → `deploy/remote-deploy.sh` (промоут в `/opt/openclaw-hub/src`,
  `pip install -e`, restart systemd, проверка `/healthz`).
- Логика деплоя версионируется в `deploy/remote-deploy.sh`; её правят там, а не в YAML.
- Доступы деплоя — это GitHub Actions secrets `DEPLOY_HOST`, `DEPLOY_USER`,
  `DEPLOY_SSH_KEY` (см. `deploy/CD.md`). Секреты не хранятся в git.
- Практический вывод для агентов: merge в `main` — это релиз в прод. Сначала
  merge в `develop` и проверка, и только потом продвижение в `main`. Не вливать
  непроверенный код прямо в `main`.

## Релизы

Отдельного версионируемого release process пока нет, релиз = merge в `main`
(авто-деплой выше). До появления формального процесса:

- все production-relevant изменения проходят через PR/review и проходят CI;
- release notes собирать из merged commits и task done reports;
- перед merge в `main` запускать полный `uv run pytest -q` и ruff check;
- ручной деплой и откат — по `docs/agent-deploy-runbook.md` раздел 4 (тот же
  `deploy/remote-deploy.sh`).
