# Runbook для ИИ-агентов: деплой Haiplane Hub на текущий сервер

Этот документ — короткая инструкция для агентов, которые продолжают работу над
уже развёрнутым Haiplane Hub. Он не заменяет полный первичный гайд
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
  sudo grep -q "^OPENCLAW_HUB_URL=https://agenthai.ru$" /etc/openclaw-hub/openclaw-hub.env \
    && echo hub_public_url_ok || echo hub_public_url_missing
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

> **Правка `deploy/remote-deploy.sh` требует ручного шага на сервере.** SSH-ключ
> CI (`openclaw-hub-ci-deploy`) с 14.08.2026 ограничен форсированной командой
> `/usr/local/sbin/openclaw-ci-deploy-guard`: она пропускает только `rsync` на
> приём в `~/openclaw-hub-src-staging/` и `bash -s`, причём выполняет не
> присланный скрипт, а закреплённую копию `/usr/local/sbin/openclaw-remote-deploy.sh`
> — и только если sha256 совпал. Смысл в том, что утечка секрета `DEPLOY_SSH_KEY`
> больше не даёт произвольную команду на сервере: до этого ключ был обычным
> шеллом под `user1`, у которого `NOPASSWD:ALL`.
>
> Поэтому изменение `deploy/remote-deploy.sh` в репозитории **уронит деплой**,
> пока копию на сервере не обновит человек:
>
> ```bash
> ssh user1@194.113.34.33 'sudo tee /usr/local/sbin/openclaw-remote-deploy.sh >/dev/null' < deploy/remote-deploy.sh
> ssh user1@194.113.34.33 'sudo chmod 0755 /usr/local/sbin/openclaw-remote-deploy.sh'
> ```
>
> Падение будет громким, а не тихим: job упадёт красным, а в логе будут оба
> sha256 и эта же команда. Отказы guard пишет в syslog тегом `openclaw-ci-guard`
> (`journalctl -t openclaw-ci-guard`).
>
> Ручной деплой ниже ограничения не касается: он идёт под личным ключом
> администратора, а форсированная команда висит только на ключе CI.

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
/usr/local/sbin/openclaw-ci-deploy-guard      форсированная команда для ключа CI (см. раздел 4)
/usr/local/sbin/openclaw-remote-deploy.sh     закреплённая копия deploy/remote-deploy.sh
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
OPENCLAW_HUB_TOKENS=name:token:human,cursor:token:agent,cursor-reviewer:token2:agent
OPENCLAW_WORKSPACE_REPO=/absolute/path/to/workspace-clone
OPENCLAW_HUB_REPO=org/repo-name
```

### Reviewer-токен (Universal Review Gate, #432)

Кроме токена агента-исполнителя, провижинь **отдельную reviewer-идентичность**
(`cursor-reviewer` в примере выше): роль `agent`, имя отличается от
исполнителя, значение токена уникально. Без неё вердикт ревью от исполнителя
блокируется gate'ом (`self_review_forbidden`) и цикл ревью стопорится.

- Генерация секрета: `openssl rand -hex 32` (в чат/git не выводить).
- На production токен добавляется в `OPENCLAW_HUB_TOKENS` в
  `/etc/openclaw-hub/openclaw-hub.env` (проверять наличие через `grep -q`,
  как в разделе 2, без вывода значений) + restart `openclaw-hub`.
- Reviewer-сессия (агент/subagent, который вызывает `hub_submit_review`)
  запускается с `OPENCLAW_HUB_TOKEN=<reviewer-token>`; исполнитель — со своим
  токеном. Детали: `docs/agent-onboarding.md`, раздел про reviewer-идентичность.

### `OPENCLAW_WORKSPACE_REPO` и pair mode

| Переменная | Назначение |
|------------|------------|
| `OPENCLAW_WORKSPACE_REPO` | Корень git, где git ops plugin выполняет `create_branch` / `checkout` при `hub_start_task` и **`hub_pair_start`** |
| `OPENCLAW_HUB_REPO` | Имя GitHub-репозитория для PR/CI интеграций (metadata) |
| `OPENCLAW_WORKTREE_PER_TASK` | `1` включает изоляцию pair-задач через `git worktree` (#459): каждая задача получает своё дерево `.<repo>-worktrees/task-<id>`, основной клон остаётся на base. По умолчанию выкл — поведение как раньше. Требует git ≥ 2.15 и место под несколько деревьев. Подробнее: [workspace-safety-policy.md](workspace-safety-policy.md#worktree-per-task-opt-in-459) |

**Production (agenthai.ru):** `OPENCLAW_WORKSPACE_REPO` обычно указывает на server clone (`/opt/openclaw-hub/src` или продуктовый repo на сервере). Pair-start создаёт branch **там**. В `/etc/openclaw-hub/openclaw-hub.env` обязателен `OPENCLAW_HUB_URL=https://agenthai.ru` — без него MCP echo `instance: local` и `base_url: http://127.0.0.1:8080` (#174, #452).

**Local dev:** часто `OPENCLAW_WORKSPACE_REPO` = тот же каталог, что открыт в Cursor. Тогда pair-start **переключает и чистит этот clone** — см. [Pair mode: git policy](../software-development-workflow.md#pair-mode-git-policy).

Ожидания для pair path B:

1. Hub DB и lifecycle — локально или на agenthai; git push — в общий `origin`.
2. Перед `hub_pair_start` — commit или stash в workspace repo.
3. После pair-start Hub может автоматически переключить workspace с **чистой,
   запушенной** ветки другой задачи (`task-N/*`) на base branch (#451); грязное
   дерево или незапушенная чужая ветка по-прежнему дают 422 с путём workspace и hint.
4. После `hub_submit_for_review`, `hub_report_done` или `hub_release_task` Hub
   best-effort возвращает workspace на base branch, если он на ветке этой задачи и чистый.
5. Push и PR — с машины, где написан код; server hub не заменяет push с ноутбука.
6. Не копировать production `hub.db` на laptop без понимания, что approve/running state общий snapshot, а git remote один.

Подробнее: [software-development-workflow.md](../software-development-workflow.md#pair-mode-git-policy), [task-workflow.html](task-workflow.html#pair-git-policy).

### Git-доступ сервисного пользователя (deploy key, #455)

Сервисный пользователь `openclaw` на agenthai должен уметь `git fetch origin` в
workspace хаба (`/var/lib/openclaw-hub/workspaces/_default`). Если доступа нет,
`pair_prepare_branch` тихо делает `pull --ff-only` с `check=False` и создаёт
pair-ветки от **устаревшего** develop.

**Настройка (человек/админ, один раз):**

1. Сгенерировать read-only deploy key (без passphrase) от имени `openclaw`:

   ```sh
   sudo -u openclaw ssh-keygen -t ed25519 -N '' \
     -f /home/openclaw/.ssh/id_ed25519 -C 'openclaw@agenthai deploy'
   sudo -u openclaw cat /home/openclaw/.ssh/id_ed25519.pub
   ```

2. Добавить публичный ключ в GitHub: репозиторий `mrPDA/openclaw-hub-standalone`
   → Settings → Deploy keys → Add deploy key → **Allow write access ВЫКЛ**
   (read-only достаточно; push идёт с машины разработчика).

3. Прописать хост в `~openclaw/.ssh/config` (или задать `GIT_SSH_COMMAND` в
   unit-файле сервиса):

   ```
   Host github.com
     IdentityFile /home/openclaw/.ssh/id_ed25519
     IdentitiesOnly yes
   ```

4. Убедиться, что `origin` использует ssh, а не https:
   `sudo -u openclaw git -C /var/lib/openclaw-hub/workspaces/_default remote set-url origin git@github.com:mrPDA/openclaw-hub-standalone.git`

**Проверка (AC-1):**

```sh
ssh agenthai "sudo -n -u openclaw git -C /var/lib/openclaw-hub/workspaces/_default fetch origin --prune \
  && sudo -n -u openclaw git -C /var/lib/openclaw-hub/workspaces/_default remote -v"
```

**Health-check в хабе (#455):** при `OPENCLAW_WORKSPACE_HEALTHCHECK=1` хаб на
старте пробует `git ls-remote origin` в default workspace и пишет `WARNING` в
лог, если origin недоступен (вместо тихого устаревания базы). Диагностика также
доступна в `hub_admin_my_identity` (ветка workspace) и `GET
/api/diagnostics/identity`. Метод `git_ops.origin_reachable(repo)` — переиспользуемая
проверка. Секрет ключа в лог/чат не выводить.

## 10. Чеклист для следующего агента

1. Прочитать задачу пользователя.
2. Проверить область изменения и тесты.
3. Не читать и не выводить секреты.
4. Сделать локальные изменения.
5. Прогнать релевантные тесты и `ruff`.
6. Выполнить стандартный деплой из раздела 4.
7. Проверить `active` и `200`.
8. В финальном ответе указать, что проверено, без публикации токенов и паролей.
