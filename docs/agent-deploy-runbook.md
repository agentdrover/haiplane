# Runbook для ИИ-агентов: деплой OpenClaw Hub на текущий сервер

Этот документ — короткая инструкция для агентов, которые продолжают работу над
уже развёрнутым OpenClaw Hub. Он не заменяет полный первичный гайд
`docs/admin-agent-deployment-guide.md`, а фиксирует фактический сервер,
проверки доступа, безопасное обращение с ключами и стандартный путь деплоя.

> Основной путь деплоя теперь автоматический: merge в `main` запускает CD в
> `.github/workflows/ci.yml` (job `deploy`), который выполняет ровно раздел 4
> этого runbook через `deploy/remote-deploy.sh`. Подробности и список секретов —
> в `deploy/CD.md`. Ручной деплой ниже — это fallback и инструмент для отката.

## 1. Что уже известно

Текущий production-like сервер:

- HTTP UI: `http://agenthai.ru:8080/`
- IP: `194.113.34.33`
- SSH user: `user1`
- systemd service: `openclaw-hub`
- runtime user на сервере: `openclaw`
- исходники сервиса: `/opt/openclaw-hub/src`
- виртуальное окружение: `/opt/openclaw-hub/venv`
- staging-каталог для rsync: `/home/user1/openclaw-hub-src-staging`
- основной env-файл: `/etc/openclaw-hub/openclaw-hub.env`
- опциональный secrets env-файл: `/etc/openclaw-hub/secrets.env`
- логи: `/var/log/openclaw-hub/`

Важно: в репозитории нет приватных SSH-ключей, токенов, паролей или `.env` с
реальными значениями. Не добавляйте их в git.

## 2. Правила работы с доступами

Никогда не печатайте секреты в чат и не вставляйте их в markdown-файлы.

Разрешено проверять только наличие доступа и наличие файлов:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=15 user1@194.113.34.33 'echo ssh_ok'
ssh user1@194.113.34.33 'test -f ~/openclaw-hub-credentials.txt && echo credentials_file_present'
ssh user1@194.113.34.33 'test -f ~/openclaw-hub-ip-access.txt && echo ip_access_file_present'
```

Не делайте так без явного запроса пользователя:

```bash
# НЕ выполнять по умолчанию: выведет секреты в лог/чат
ssh user1@194.113.34.33 'cat ~/openclaw-hub-credentials.txt'
ssh user1@194.113.34.33 'cat ~/openclaw-hub-ip-access.txt'
sudo cat /etc/openclaw-hub/secrets.env
```

Если нужно убедиться, что токены есть в env-файлах, проверяйте без вывода
значений:

```bash
ssh user1@194.113.34.33 '
  sudo test -s /etc/openclaw-hub/openclaw-hub.env && echo openclaw_env_present
  sudo test -s /etc/openclaw-hub/secrets.env && echo secrets_env_present || true
  sudo grep -q "^OPENCLAW_HUB_TOKENS=" /etc/openclaw-hub/openclaw-hub.env \
    && echo hub_tokens_configured || echo hub_tokens_missing
'
```

Если SSH не работает из текущей среды, не генерируйте и не меняйте ключи
самостоятельно. Сначала сообщите пользователю: нужен доступ к приватному ключу
или настроенный SSH agent. Публичный ключ сам по себе не даёт возможность
подключиться.

## 3. Быстрая проверка перед деплоем

Перед деплоем агент должен убедиться, что:

- SSH доступ работает.
- Серверный каталог `/opt/openclaw-hub/src` существует.
- Локальные тесты для затронутой области прошли.
- В рабочем дереве нет неожиданных секретов.

Команда проверки сервера:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=15 user1@194.113.34.33 '
  echo ssh_ok
  sudo test -d /opt/openclaw-hub/src && echo src_ok
  sudo test -f /etc/openclaw-hub/openclaw-hub.env && echo env_ok
  systemctl is-active openclaw-hub
'
```

Локальная проверка зависит от изменения. Для UI-правок обычно достаточно:

```bash
uv run pytest -q tests/test_web.py
uv run ruff check hub tests
```

Для широких backend-изменений используйте:

```bash
uv run pytest -q
uv run ruff check hub tests
```

## 4. Стандартный деплой текущей рабочей копии

Деплой выполняется через staging-каталог пользователя `user1`, затем `sudo rsync`
в `/opt/openclaw-hub/src`. Это текущий рабочий процесс для этого сервера. Та же
логика серверной части версионируется в `deploy/remote-deploy.sh` и используется
авто-деплоем; при желании после `rsync` в staging можно запустить именно её:
`ssh user1@194.113.34.33 'bash -s' < deploy/remote-deploy.sh`.

Запускать из корня локального репозитория:

```bash
rsync -az --delete \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '*.pyc' \
  --exclude '.git' \
  /Users/denispukinov/openclaw-hub-standalone/ \
  user1@194.113.34.33:~/openclaw-hub-src-staging/

ssh user1@194.113.34.33 'bash -s' <<'REMOTE'
set -euo pipefail
STAGING="$HOME/openclaw-hub-src-staging"
DEST=/opt/openclaw-hub/src

sudo rsync -a --delete "$STAGING/" "$DEST/"
sudo chown -R openclaw:openclaw "$DEST"
sudo -u openclaw /opt/openclaw-hub/venv/bin/pip install -e "$DEST" -q
sudo systemctl restart openclaw-hub
sleep 2
sudo systemctl is-active openclaw-hub
curl -sf -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/healthz
REMOTE
```

Критерии успеха:

- `systemctl is-active openclaw-hub` возвращает `active`.
- `/healthz` возвращает `200`.

## 5. Проверка после деплоя

Минимальная проверка:

```bash
ssh user1@194.113.34.33 '
  echo "service=$(systemctl is-active openclaw-hub)"
  curl -sf http://127.0.0.1:8080/healthz
'
```

Если сервис не стартует:

```bash
ssh user1@194.113.34.33 '
  sudo journalctl -u openclaw-hub -n 80 --no-pager
  echo "--- service.err ---"
  sudo tail -80 /var/log/openclaw-hub/service.err 2>/dev/null || true
'
```

Не меняйте конфигурацию, токены, firewall или systemd hardening наугад. Сначала
сформулируйте причину отказа по логам.

## 6. Где лежат важные файлы на сервере

```text
/opt/openclaw-hub/src/                 код приложения
/opt/openclaw-hub/venv/                Python venv
/etc/openclaw-hub/openclaw-hub.env     несекретная и частично чувствительная конфигурация
/etc/openclaw-hub/secrets.env          секреты интеграций, если есть
/var/lib/openclaw-hub/hub.db           SQLite база
/var/log/openclaw-hub/service.log      stdout сервиса
/var/log/openclaw-hub/service.err      stderr сервиса
/home/user1/openclaw-hub-credentials.txt  админские доступы, не читать без запроса
/home/user1/openclaw-hub-ip-access.txt    API/MCP токен и заметки по IP-доступу, не читать без запроса
```

## 7. Когда использовать полный deployment guide

Используйте `docs/admin-agent-deployment-guide.md`, если нужно:

- поднять новый сервер с нуля;
- перенести сервис на другой домен;
- настраивать nginx/TLS;
- создавать первого администратора;
- менять модель хранения секретов;
- менять systemd unit или hardening.

Для обычных правок кода и UI используйте этот runbook.

## 9. Локальный hub на ноутбуке (pair / dev)

Для проверки задач до merge в `main`:

```bash
# один раз: .env.local из deploy/local-hub.env.example
./deploy/run-local-hub.sh
# UI: http://127.0.0.1:8080/
```

Типичный `.env.local` (файл в `.gitignore`):

```bash
OPENCLAW_HUB_DB=/absolute/path/.local/state/hub.db
OPENCLAW_HUB_TOKENS=name:token:human,agent-name:token:agent
OPENCLAW_WORKSPACE_REPO=/absolute/path/to/workspace-clone
OPENCLAW_HUB_REPO=org/repo-name
```

### `OPENCLAW_WORKSPACE_REPO` и pair mode

| Переменная | Назначение |
|------------|------------|
| `OPENCLAW_WORKSPACE_REPO` | Корень git, где git ops plugin выполняет `create_branch` / `checkout` при `hub_start_task` и **`hub_pair_start`** |
| `OPENCLAW_HUB_REPO` | Имя GitHub-репозитория для PR/CI интеграций (metadata) |

**Production (agenthai.ru):** `OPENCLAW_WORKSPACE_REPO` обычно указывает на server clone (`/opt/openclaw-hub/src` или продуктовый repo на сервере). Pair-start создаёт branch **там**.

**Local dev:** часто `OPENCLAW_WORKSPACE_REPO` = тот же каталог, что открыт в Cursor. Тогда pair-start **переключает и чистит этот clone** — см. [Pair mode: git policy](../software-development-workflow.md#pair-mode-git-policy).

Ожидания для pair path B:

1. Hub DB и lifecycle — локально или на agenthai; git push — в общий `origin`.
2. Перед `hub_pair_start` — commit или stash в workspace repo.
3. После pair-start сверить `tasks.branch` в UI/API с `git branch --show-current`.
4. Push и PR — с машины, где написан код; server hub не заменяет push с ноутбука.
5. Не копировать production `hub.db` на laptop без понимания, что approve/running state общий snapshot, а git remote один.

Подробнее: [software-development-workflow.md](../software-development-workflow.md#pair-mode-git-policy), [task-workflow.html](task-workflow.html#pair-git-policy).

## 10. Чеклист для следующего агента

1. Прочитать задачу пользователя.
2. Проверить область изменения и тесты.
3. Не читать и не выводить секреты.
4. Сделать локальные изменения.
5. Прогнать релевантные тесты и `ruff`.
6. Выполнить стандартный деплой из раздела 4.
7. Проверить `active` и `200`.
8. В финальном ответе указать, что проверено, без публикации токенов и паролей.
