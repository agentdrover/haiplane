# Continuous Deployment: auto-deploy on merge to `main`

Каждый push в `main` (то есть любой merge PR в `main`) автоматически выкатывает код
на текущий production-like сервер. Деплой выполняется GitHub Actions и повторяет
ручной процесс из `docs/agent-deploy-runbook.md` раздел 4.

## Как это работает

Workflow: `.github/workflows/ci.yml`, job `deploy`.

1. Job `test` гоняет `ruff check`, `ruff format --check` и `pytest`.
2. Job `deploy` стартует только если:
   - `test` прошёл (`needs: test`);
   - событие — это `push` в `main` (на `pull_request` деплой не запускается).
3. Шаги деплоя:
   - кладёт приватный SSH-ключ из секрета в раннер;
   - `rsync` рабочего дерева в `~/openclaw-hub-src-staging` на сервере;
   - `ssh ... 'bash -s' < deploy/remote-deploy.sh` — промоут staging в
     `/opt/openclaw-hub/src`, `pip install -e`, `systemctl restart openclaw-hub`,
     проверка `systemctl is-active` и `GET /healthz`.

`concurrency.group: deploy-production` гарантирует, что два деплоя не пойдут
параллельно. Деплой привязан к GitHub Environment `production` — на него можно
повесить required reviewers / wait timer в настройках репозитория.

Логика самого деплоя живёт в `deploy/remote-deploy.sh`, чтобы её можно было
ревьюить и менять без правки YAML.

## Обязательные GitHub secrets

Настроить в **Settings → Secrets and variables → Actions** (репозиторий или
Environment `production`):

| Secret | Назначение | Пример |
|--------|------------|--------|
| `DEPLOY_HOST` | хост/IP сервера | `194.113.34.33` |
| `DEPLOY_USER` | SSH-пользователь с passwordless sudo для шагов деплоя | `user1` |
| `DEPLOY_SSH_KEY` | приватный SSH-ключ целиком (OpenSSH/PEM), без пароля | содержимое файла приватного ключа целиком, включая строки `BEGIN`/`END` |

Требования к ключу/доступу:

- публичная часть ключа добавлена в `~/.ssh/authorized_keys` пользователя `DEPLOY_USER`;
- `DEPLOY_USER` может без пароля выполнять `sudo rsync`, `sudo chown`,
  `sudo -u openclaw ... pip`, `sudo systemctl restart openclaw-hub`,
  `sudo journalctl -u openclaw-hub`;
- ключ хранится только в секретах GitHub. В git его класть нельзя.

## Что НЕ коммитим

Приватные ключи, токены, реальные `.env`. Секреты живут только в GitHub Actions
secrets и в `/etc/openclaw-hub/*.env` на сервере (см. runbook).

## Ручной деплой / откат

Авто-деплой не отменяет ручной путь. Если нужно выкатить или откатить вручную,
используйте `docs/agent-deploy-runbook.md` раздел 4 (тот же `remote-deploy.sh`
можно запустить руками после `rsync` в staging). Откат — задеплоить предыдущий
коммит `main` (revert PR или checkout нужного тега и ручной деплой).

## Проверка после авто-деплоя

```bash
ssh "$DEPLOY_USER@$DEPLOY_HOST" '
  echo "service=$(systemctl is-active openclaw-hub)"
  curl -sf http://127.0.0.1:8080/healthz
'
```

## Health-гейт деплоя: опрос, а не пауза (#547)

`deploy/remote-deploy.sh` после рестарта **опрашивает** `/healthz` с бюджетом
`HEALTH_BUDGET_SECONDS` (по умолчанию 60 с, интервал 1 с), а не спит фиксированные
две секунды. Успех — по первому 200.

Причина: юнит объявлен `Type=simple`, поэтому systemd считает его активным в момент
старта процесса, а uvicorn в этот момент ещё может не принимать соединения.
Наблюдение 28.07.2026 (run 30399012885): выкат прошёл полностью, `service=active`,
`healthz=000`, джоба красная. Ложно-красный деплой опаснее шумного — он приучает
читать красное как «опять гонка», и следующий настоящий сбой будет пропущен.

Три исхода различимы в логе джобы: сервис не поднялся / поднялся, но не отвечает /
отвечает. На обеих ветках отказа печатается `journalctl -u openclaw-hub -n 40`.
Раньше лог тянулся только когда сервис лежал — то есть ровно тогда, когда причина и
так очевидна.

**Время до первого 200 печатается при успехе намеренно.** Увеличенный бюджет
маскирует деградацию старта: приложение начнёт подниматься сорок секунд вместо двух,
а гейт этого не заметит. Растущее число в логе заметно глазом, молчаливый успех — нет.

### Почему не `Type=notify`

Это убрало бы гонку в корне, а не отодвигало её бюджетом, и остаётся правильным
долгосрочным решением. Но своими силами systemd этого не сделает: `Type=notify`
требует, чтобы **само приложение** отправило `READY=1` в `$NOTIFY_SOCKET` после
того, как ASGI-сервер начал слушать. uvicorn такого из коробки не умеет — нужна
доработка точки входа `openclaw-hub` (вызов sd_notify в хуке готовности FastAPI).

Это изменение приложения, а не деплоя, поэтому в #547 не вошло. Возвращаться к
вопросу имеет смысл, если время до первого 200 начнёт расти — тогда бюджет перестанет
быть страховкой и станет маскировкой.

## Rollout note: Universal Review Gate (2026-07)

С релиза эпика #300 (задачи #305–#311) хаб **сервером** требует ревью перед
завершением: `hub_report_done` остаётся совместимой точкой входа, но в
pair/client-флоу его результат изменился — без APPROVED-вердикта для текущего
сабмишена задача уходит в `review` (client-driven) или `ci_check`, а не в
`completed`. Клиенты, ожидавшие немедленного `completed`, должны читать
envelope ответа:

```json
{
  "status": "review",
  "awaiting": "review",
  "actor_hint": "agent",
  "next_action": "Obtain a review verdict: reviewer runs hub_get_review_brief and hub_submit_review; after APPROVED, report done again."
}
```

Откат этого поведения — только откатом релиза (см. выше): конфиг-флага нет,
гейт — инвариант жизненного цикла. Явный опт-аут на уровне задачи —
`auto_review=false`; human overrides (`hub_decide_task` accept,
`hub_force_complete_task`) работают и аудируются.

## Git-доступ workspace (провижининг проектов, #347/#348)

Хаб клонирует workspace проектов сам (`POST /api/projects/{id}/provision`,
кнопка **Provision** на `/projects`, MCP `hub_provision_project`). Для этого
пользователю, под которым работает сервис хаба, нужен git-доступ к репозиторию
проекта. **Публичные репозитории читаются анонимно по https — настройка не
нужна** (#377: короткая форма `owner/repo` сначала пробует https, затем ssh).
Для приватных — один из двух вариантов:

1. **Deploy key (рекомендуется для приватных репо):** ssh-ключ
   `~/.ssh/id_ed25519` пользователя сервиса добавлен как Deploy key
   репозитория (read-only достаточно). `git_ops` подхватывает его
   автоматически через `GIT_SSH_COMMAND`.
2. **gh auth:** `gh auth login` под пользователем сервиса (тогда repo можно
   указывать как `owner/repo`, https-доступ пойдёт через gh-креды).

Проверочная команда (под пользователем сервиса):

```bash
sudo -u <svc-user> git ls-remote git@github.com:<owner>/<repo>.git HEAD
```

Именно её эквивалент хаб выполняет перед клоном: если доступа нет, провижининг
вернёт `provision_status=error` с текстом ошибки ls-remote — это штатная
диагностика, а не падение.

Онбординг проекта за минуту: создать проект на `/projects` (repo +
workspace path) → провижининг стартует автоматически (или кнопкой
Provision) → привязать эпик (`project` при создании эпика, #346) — задачи
проекта едут по его репозиторию и веткам.

### OPENCLAW_WORKSPACE_REPO (#378)

Нужен ТОЛЬКО для pair-конвейера задач default-проекта (подготовка веток на
сервере). Провижининг проектов и git-операции проектных workspace работают
без него. Если переменная не указывает на git-клон, pair_start для
default-задач вернёт читаемую 422-ошибку с подсказкой.
