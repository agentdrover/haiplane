# Runbook для ИИ-агентов: деплой Haiplane Hub на уже развёрнутый сервер

Этот документ — короткая инструкция для агентов, которые продолжают работу над
уже развёрнутым Haiplane Hub. Он не заменяет полный первичный гайд
`docs/admin-agent-deployment-guide.md`, а фиксирует проверки доступа, безопасное
обращение с ключами и стандартный путь деплоя.

**Это шаблон: подставьте значения своей инсталляции.** Все конкретные имена
хостов, пользователей, доменов и каталогов вынесены в плейсхолдеры (таблица в
разделе 1). В репозитории они намеренно не публикуются: реальные значения живут
в секретах CI и у оператора. Порядок шагов, команды проверки и предупреждения
применимы к любой инсталляции с такой же схемой (systemd + venv + rsync).

> Основной путь деплоя — автоматический: merge в `main` запускает CD в
> `.github/workflows/ci.yml` (job `deploy`), который выполняет ровно раздел 4
> этого runbook через `deploy/remote-deploy.sh`. Подробности и список секретов —
> в `deploy/CD.md`. Ручной деплой ниже — это fallback и инструмент для отката.

## 1. Плейсхолдеры и раскладка инсталляции

| Плейсхолдер | Что это | Пример |
|-------------|---------|--------|
| `<DEPLOY_HOST>` | хост сервера для SSH | `hub.example.com` |
| `<DEPLOY_USER>` | sudo-пользователь, под которым идёт деплой | `deploy` |
| `<HUB_URL>` | публичный URL сервиса за reverse proxy | `https://hub.example.com` |
| `<SERVICE>` | имя systemd-юнита и каталогов установки | `haiplane-hub` |
| `<RUNTIME_USER>` | системный пользователь, под которым работает сервис | `haiplane` |
| `<STAGING_DIR>` | staging-каталог для rsync в домашней директории `<DEPLOY_USER>` | `~/hub-src-staging` |
| `<REPO_SLUG>` | `owner/repo` управляемого репозитория на GitHub | `owner/repo` |
| `<LOCAL_REPO>` | путь к локальному клону, из которого деплоите | `/path/to/clone` |

Типовая раскладка на сервере (её создаёт первичный гайд):

- HTTP UI: `<HUB_URL>` (сервис слушает `127.0.0.1:8080` за proxy)
- systemd service: `<SERVICE>`
- runtime user на сервере: `<RUNTIME_USER>`
- исходники сервиса: `/opt/<SERVICE>/src`
- виртуальное окружение: `/opt/<SERVICE>/venv`
- staging-каталог для rsync: `<STAGING_DIR>`
- основной env-файл: `/etc/<SERVICE>/<SERVICE>.env`
- опциональный secrets env-файл: `/etc/<SERVICE>/secrets.env`
- логи: `/var/log/<SERVICE>/`

Важно: в репозитории нет приватных SSH-ключей, токенов, паролей или `.env` с
реальными значениями. Не добавляйте их в git.

## 2. Правила работы с доступами

Никогда не печатайте секреты в чат и не вставляйте их в markdown-файлы.

Разрешено проверять только наличие доступа и наличие файлов:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=15 <DEPLOY_USER>@<DEPLOY_HOST> 'echo ssh_ok'
```

Не делайте так без явного запроса пользователя:

```bash
# НЕ выполнять по умолчанию: выведет секреты в лог/чат
sudo cat /etc/<SERVICE>/secrets.env
```

Если нужно убедиться, что токены есть в env-файлах, проверяйте без вывода
значений:

```bash
ssh <DEPLOY_USER>@<DEPLOY_HOST> '
  sudo test -s /etc/<SERVICE>/<SERVICE>.env && echo hub_env_present
  sudo test -s /etc/<SERVICE>/secrets.env && echo secrets_env_present || true
  sudo grep -q "^HAIPLANE_HUB_TOKENS=" /etc/<SERVICE>/<SERVICE>.env \
    && echo hub_tokens_configured || echo hub_tokens_missing
  sudo grep -q "^HAIPLANE_HUB_URL=<HUB_URL>$" /etc/<SERVICE>/<SERVICE>.env \
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
- Серверный каталог `/opt/<SERVICE>/src` существует.
- Локальные тесты для затронутой области прошли.
- В рабочем дереве нет неожиданных секретов.

Команда проверки сервера:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=15 <DEPLOY_USER>@<DEPLOY_HOST> '
  echo ssh_ok
  sudo test -d /opt/<SERVICE>/src && echo src_ok
  sudo test -f /etc/<SERVICE>/<SERVICE>.env && echo env_ok
  systemctl is-active <SERVICE>
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

Деплой выполняется через staging-каталог пользователя `<DEPLOY_USER>`, затем
`sudo rsync` в `/opt/<SERVICE>/src`. Та же логика серверной части
версионируется в `deploy/remote-deploy.sh` и используется авто-деплоем; при
желании после `rsync` в staging можно запустить именно её:
`ssh <DEPLOY_USER>@<DEPLOY_HOST> 'bash -s' < deploy/remote-deploy.sh`.

> **Если ключ CI ограничен forced command, правка `deploy/remote-deploy.sh`
> требует ручного шага на сервере.** Рекомендуемая (и применённая в нашей
> инсталляции) схема: SSH-ключ CI привязан к обёртке
> `/usr/local/sbin/<SERVICE>-ci-deploy-guard`, которая пропускает только `rsync`
> на приём в `<STAGING_DIR>` и `bash -s`, причём выполняет не присланный скрипт,
> а закреплённую копию `/usr/local/sbin/<SERVICE>-remote-deploy.sh` — и только
> если sha256 совпал. Смысл в том, что утечка секрета `DEPLOY_SSH_KEY` не даёт
> произвольную команду на сервере: без обёртки ключ — обычный шелл под
> `<DEPLOY_USER>`, у которого обычно `NOPASSWD:ALL`.
>
> При такой схеме изменение `deploy/remote-deploy.sh` в репозитории **уронит
> деплой**, пока копию на сервере не обновит человек:
>
> ```bash
> ssh <DEPLOY_USER>@<DEPLOY_HOST> 'sudo tee /usr/local/sbin/<SERVICE>-remote-deploy.sh >/dev/null' < deploy/remote-deploy.sh
> ssh <DEPLOY_USER>@<DEPLOY_HOST> 'sudo chmod 0755 /usr/local/sbin/<SERVICE>-remote-deploy.sh'
> ```
>
> Падение будет громким, а не тихим: job упадёт красным, а в логе будут оба
> sha256 и эта же команда. Отказы guard пишет в syslog тегом
> `<SERVICE>-ci-guard` (`journalctl -t <SERVICE>-ci-guard`).
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
  <LOCAL_REPO>/ \
  <DEPLOY_USER>@<DEPLOY_HOST>:<STAGING_DIR>/

ssh <DEPLOY_USER>@<DEPLOY_HOST> 'bash -s' <<'REMOTE'
set -euo pipefail
STAGING="<STAGING_DIR>"
DEST=/opt/<SERVICE>/src

sudo rsync -a --delete "$STAGING/" "$DEST/"
sudo chown -R <RUNTIME_USER>:<RUNTIME_USER> "$DEST"
sudo -u <RUNTIME_USER> /opt/<SERVICE>/venv/bin/pip install -e "$DEST" -q
sudo systemctl restart <SERVICE>
sleep 2
sudo systemctl is-active <SERVICE>
curl -sf -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/healthz
REMOTE
```

Критерии успеха:

- `systemctl is-active <SERVICE>` возвращает `active`.
- `/healthz` возвращает `200`.

## 5. Проверка после деплоя

Минимальная проверка:

```bash
ssh <DEPLOY_USER>@<DEPLOY_HOST> '
  echo "service=$(systemctl is-active <SERVICE>)"
  curl -sf http://127.0.0.1:8080/healthz
'
```

Если сервис не стартует:

```bash
ssh <DEPLOY_USER>@<DEPLOY_HOST> '
  sudo journalctl -u <SERVICE> -n 80 --no-pager
  echo "--- service.err ---"
  sudo tail -80 /var/log/<SERVICE>/service.err 2>/dev/null || true
'
```

Не меняйте конфигурацию, токены, firewall или systemd hardening наугад. Сначала
сформулируйте причину отказа по логам.

## 6. Где лежат важные файлы на сервере

```text
/opt/<SERVICE>/src/                            код приложения
/opt/<SERVICE>/venv/                           Python venv
/usr/local/sbin/<SERVICE>-ci-deploy-guard      форсированная команда для ключа CI (см. раздел 4)
/usr/local/sbin/<SERVICE>-remote-deploy.sh     закреплённая копия deploy/remote-deploy.sh
/etc/<SERVICE>/<SERVICE>.env                   несекретная и частично чувствительная конфигурация
/etc/<SERVICE>/secrets.env                     секреты интеграций, если есть
/var/lib/<SERVICE>/hub.db                      SQLite база
/var/log/<SERVICE>/service.log                 stdout сервиса
/var/log/<SERVICE>/service.err                 stderr сервиса
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
HAIPLANE_HUB_DB=/absolute/path/.local/state/hub.db
HAIPLANE_HUB_TOKENS=name:token:human,cursor:token:agent,cursor-reviewer:token2:agent
HAIPLANE_WORKSPACE_REPO=/absolute/path/to/workspace-clone
HAIPLANE_HUB_REPO=<REPO_SLUG>
```

### Reviewer-токен (Universal Review Gate, #432)

Кроме токена агента-исполнителя, провижинь **отдельную reviewer-идентичность**
(`cursor-reviewer` в примере выше): роль `agent`, имя отличается от
исполнителя, значение токена уникально. Без неё вердикт ревью от исполнителя
блокируется gate'ом (`self_review_forbidden`) и цикл ревью стопорится.

- Генерация секрета: `openssl rand -hex 32` (в чат/git не выводить).
- На сервере токен добавляется в `HAIPLANE_HUB_TOKENS` в
  `/etc/<SERVICE>/<SERVICE>.env` (проверять наличие через `grep -q`,
  как в разделе 2, без вывода значений) + restart `<SERVICE>`.
- Reviewer-сессия (агент/subagent, который вызывает `hub_submit_review`)
  запускается с `HAIPLANE_HUB_TOKEN=<reviewer-token>`; исполнитель — со своим
  токеном. Детали: `docs/agent-onboarding.md`, раздел про reviewer-идентичность.

### `HAIPLANE_WORKSPACE_REPO` и pair mode

| Переменная | Назначение |
|------------|------------|
| `HAIPLANE_WORKSPACE_REPO` | Корень git, где git ops plugin выполняет `create_branch` / `checkout` при `hub_start_task` и **`hub_pair_start`** |
| `HAIPLANE_HUB_REPO` | Имя GitHub-репозитория для PR/CI интеграций (metadata) |
| `HAIPLANE_WORKTREE_PER_TASK` | `1` включает изоляцию pair-задач через `git worktree` (#459): каждая задача получает своё дерево `.<repo>-worktrees/task-<id>`, основной клон остаётся на base. По умолчанию выкл — поведение как раньше. Требует git ≥ 2.15 и место под несколько деревьев. Подробнее: [workspace-safety-policy.md](workspace-safety-policy.md#worktree-per-task-opt-in-459) |

**Серверная инсталляция:** `HAIPLANE_WORKSPACE_REPO` обычно указывает на server
clone (`/opt/<SERVICE>/src` или продуктовый repo на сервере). Pair-start создаёт
branch **там**. В `/etc/<SERVICE>/<SERVICE>.env` обязателен
`HAIPLANE_HUB_URL=<HUB_URL>` — без него MCP echo `instance: local` и
`base_url: http://127.0.0.1:8080` (#174, #452).

**Local dev:** часто `HAIPLANE_WORKSPACE_REPO` = тот же каталог, что открыт в Cursor. Тогда pair-start **переключает и чистит этот clone** — см. [Pair mode: git policy](software-development-workflow.md#pair-mode-git-policy).

Ожидания для pair path B:

1. Hub DB и lifecycle — локально или на сервере; git push — в общий `origin`.
2. Перед `hub_pair_start` — commit или stash в workspace repo.
3. После pair-start Hub может автоматически переключить workspace с **чистой,
   запушенной** ветки другой задачи (`task-N/*`) на base branch (#451); грязное
   дерево или незапушенная чужая ветка по-прежнему дают 422 с путём workspace и hint.
4. После `hub_submit_for_review`, `hub_report_done` или `hub_release_task` Hub
   best-effort возвращает workspace на base branch, если он на ветке этой задачи и чистый.
5. Push и PR — с машины, где написан код; server hub не заменяет push с ноутбука.
6. Не копировать серверную `hub.db` на laptop без понимания, что approve/running state общий snapshot, а git remote один.

Подробнее: [software-development-workflow.md](software-development-workflow.md#pair-mode-git-policy), [task-workflow.html](task-workflow.html#pair-git-policy).

### Git-доступ сервисного пользователя (deploy key, #455)

Сервисный пользователь `<RUNTIME_USER>` на сервере должен уметь `git fetch origin`
в workspace хаба (`/var/lib/<SERVICE>/workspaces/_default`). Если доступа нет,
`pair_prepare_branch` тихо делает `pull --ff-only` с `check=False` и создаёт
pair-ветки от **устаревшего** develop.

**Настройка (человек/админ, один раз):**

1. Сгенерировать read-only deploy key (без passphrase) от имени `<RUNTIME_USER>`:

   ```sh
   sudo -u <RUNTIME_USER> ssh-keygen -t ed25519 -N '' \
     -f /home/<RUNTIME_USER>/.ssh/id_ed25519 -C '<SERVICE> deploy key'
   sudo -u <RUNTIME_USER> cat /home/<RUNTIME_USER>/.ssh/id_ed25519.pub
   ```

2. Добавить публичный ключ в GitHub: репозиторий `<REPO_SLUG>`
   → Settings → Deploy keys → Add deploy key → **Allow write access ВЫКЛ**
   (read-only достаточно; push идёт с машины разработчика).

3. Прописать хост в `~<RUNTIME_USER>/.ssh/config` (или задать `GIT_SSH_COMMAND` в
   unit-файле сервиса):

   ```
   Host github.com
     IdentityFile /home/<RUNTIME_USER>/.ssh/id_ed25519
     IdentitiesOnly yes
   ```

4. Убедиться, что `origin` использует ssh, а не https:
   `sudo -u <RUNTIME_USER> git -C /var/lib/<SERVICE>/workspaces/_default remote set-url origin git@github.com:<REPO_SLUG>.git`

**Проверка (AC-1):**

```sh
ssh <DEPLOY_USER>@<DEPLOY_HOST> "sudo -n -u <RUNTIME_USER> git -C /var/lib/<SERVICE>/workspaces/_default fetch origin --prune \
  && sudo -n -u <RUNTIME_USER> git -C /var/lib/<SERVICE>/workspaces/_default remote -v"
```

**Health-check в хабе (#455):** при `HAIPLANE_WORKSPACE_HEALTHCHECK=1` хаб на
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
