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
- SSH-доступ: значения `DEPLOY_HOST` / `DEPLOY_USER` — в секретах CI и у оператора; в репозитории они не публикуются
- systemd service: `haiplane-hub`
- runtime user на сервере: `haiplane`
- исходники сервиса: `/opt/haiplane-hub/src`
- виртуальное окружение: `/opt/haiplane-hub/venv`
- staging-каталог для rsync: `/home/user1/haiplane-hub-src-staging`
- основной env-файл: `/etc/haiplane-hub/haiplane-hub.env`
- опциональный secrets env-файл: `/etc/haiplane-hub/secrets.env`
- логи: `/var/log/haiplane-hub/`

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
sudo cat /etc/haiplane-hub/secrets.env
```

Если нужно убедиться, что токены есть в env-файлах, проверяйте без вывода
значений:

```bash
ssh <DEPLOY_USER>@<DEPLOY_HOST> '
  sudo test -s /etc/haiplane-hub/haiplane-hub.env && echo haiplane_env_present
  sudo test -s /etc/haiplane-hub/secrets.env && echo secrets_env_present || true
  sudo grep -q "^HAIPLANE_HUB_TOKENS=" /etc/haiplane-hub/haiplane-hub.env \
    && echo hub_tokens_configured || echo hub_tokens_missing
  sudo grep -q "^HAIPLANE_HUB_URL=https://agenthai.ru$" /etc/haiplane-hub/haiplane-hub.env \
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
- Серверный каталог `/opt/haiplane-hub/src` существует.
- Локальные тесты для затронутой области прошли.
- В рабочем дереве нет неожиданных секретов.

Команда проверки сервера:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=15 <DEPLOY_USER>@<DEPLOY_HOST> '
  echo ssh_ok
  sudo test -d /opt/haiplane-hub/src && echo src_ok
  sudo test -f /etc/haiplane-hub/haiplane-hub.env && echo env_ok
  systemctl is-active haiplane-hub
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
в `/opt/haiplane-hub/src`. Это текущий рабочий процесс для этого сервера. Та же
логика серверной части версионируется в `deploy/remote-deploy.sh` и используется
авто-деплоем; при желании после `rsync` в staging можно запустить именно её:
`ssh <DEPLOY_USER>@<DEPLOY_HOST> 'bash -s' < deploy/remote-deploy.sh`.

> **Правка `deploy/remote-deploy.sh` требует ручного шага на сервере.** SSH-ключ
> CI (`haiplane-hub-ci-deploy`) с 14.08.2026 ограничен форсированной командой
> `/usr/local/sbin/haiplane-ci-deploy-guard`: она пропускает только `rsync` на
> приём в `~/haiplane-hub-src-staging/` и `bash -s`, причём выполняет не
> присланный скрипт, а закреплённую копию `/usr/local/sbin/haiplane-remote-deploy.sh`
> — и только если sha256 совпал. Смысл в том, что утечка секрета `DEPLOY_SSH_KEY`
> больше не даёт произвольную команду на сервере: до этого ключ был обычным
> шеллом под `user1`, у которого `NOPASSWD:ALL`.
>
> Поэтому изменение `deploy/remote-deploy.sh` в репозитории **уронит деплой**,
> пока копию на сервере не обновит человек:
>
> ```bash
> ssh <DEPLOY_USER>@<DEPLOY_HOST> 'sudo tee /usr/local/sbin/haiplane-remote-deploy.sh >/dev/null' < deploy/remote-deploy.sh
> ssh <DEPLOY_USER>@<DEPLOY_HOST> 'sudo chmod 0755 /usr/local/sbin/haiplane-remote-deploy.sh'
> ```
>
> Падение будет громким, а не тихим: job упадёт красным, а в логе будут оба
> sha256 и эта же команда. Отказы guard пишет в syslog тегом `haiplane-ci-guard`
> (`journalctl -t haiplane-ci-guard`).
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
  /Users/<user>/haiplane/ \
  <DEPLOY_USER>@<DEPLOY_HOST>:~/haiplane-hub-src-staging/

ssh <DEPLOY_USER>@<DEPLOY_HOST> 'bash -s' <<'REMOTE'
set -euo pipefail
STAGING="$HOME/haiplane-hub-src-staging"
DEST=/opt/haiplane-hub/src

sudo rsync -a --delete "$STAGING/" "$DEST/"
sudo chown -R haiplane:haiplane "$DEST"
sudo -u haiplane /opt/haiplane-hub/venv/bin/pip install -e "$DEST" -q
sudo systemctl restart haiplane-hub
sleep 2
sudo systemctl is-active haiplane-hub
curl -sf -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/healthz
REMOTE
```

Критерии успеха:

- `systemctl is-active haiplane-hub` возвращает `active`.
- `/healthz` возвращает `200`.

## 5. Проверка после деплоя

Минимальная проверка:

```bash
ssh <DEPLOY_USER>@<DEPLOY_HOST> '
  echo "service=$(systemctl is-active haiplane-hub)"
  curl -sf http://127.0.0.1:8080/healthz
'
```

Если сервис не стартует:

```bash
ssh <DEPLOY_USER>@<DEPLOY_HOST> '
  sudo journalctl -u haiplane-hub -n 80 --no-pager
  echo "--- service.err ---"
  sudo tail -80 /var/log/haiplane-hub/service.err 2>/dev/null || true
'
```

Не меняйте конфигурацию, токены, firewall или systemd hardening наугад. Сначала
сформулируйте причину отказа по логам.

## 6. Где лежат важные файлы на сервере

```text
/opt/haiplane-hub/src/                 код приложения
/opt/haiplane-hub/venv/                Python venv
/usr/local/sbin/haiplane-ci-deploy-guard      форсированная команда для ключа CI (см. раздел 4)
/usr/local/sbin/haiplane-remote-deploy.sh     закреплённая копия deploy/remote-deploy.sh
/etc/haiplane-hub/haiplane-hub.env     несекретная и частично чувствительная конфигурация
/etc/haiplane-hub/secrets.env          секреты интеграций, если есть
/var/lib/haiplane-hub/hub.db           SQLite база
/var/log/haiplane-hub/service.log      stdout сервиса
/var/log/haiplane-hub/service.err      stderr сервиса
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
HAIPLANE_HUB_REPO=org/repo-name
```

### Reviewer-токен (Universal Review Gate, #432)

Кроме токена агента-исполнителя, провижинь **отдельную reviewer-идентичность**
(`cursor-reviewer` в примере выше): роль `agent`, имя отличается от
исполнителя, значение токена уникально. Без неё вердикт ревью от исполнителя
блокируется gate'ом (`self_review_forbidden`) и цикл ревью стопорится.

- Генерация секрета: `openssl rand -hex 32` (в чат/git не выводить).
- На production токен добавляется в `HAIPLANE_HUB_TOKENS` в
  `/etc/haiplane-hub/haiplane-hub.env` (проверять наличие через `grep -q`,
  как в разделе 2, без вывода значений) + restart `haiplane-hub`.
- Reviewer-сессия (агент/subagent, который вызывает `hub_submit_review`)
  запускается с `HAIPLANE_HUB_TOKEN=<reviewer-token>`; исполнитель — со своим
  токеном. Детали: `docs/agent-onboarding.md`, раздел про reviewer-идентичность.

### `HAIPLANE_WORKSPACE_REPO` и pair mode

| Переменная | Назначение |
|------------|------------|
| `HAIPLANE_WORKSPACE_REPO` | Корень git, где git ops plugin выполняет `create_branch` / `checkout` при `hub_start_task` и **`hub_pair_start`** |
| `HAIPLANE_HUB_REPO` | Имя GitHub-репозитория для PR/CI интеграций (metadata) |
| `HAIPLANE_WORKTREE_PER_TASK` | `1` включает изоляцию pair-задач через `git worktree` (#459): каждая задача получает своё дерево `.<repo>-worktrees/task-<id>`, основной клон остаётся на base. По умолчанию выкл — поведение как раньше. Требует git ≥ 2.15 и место под несколько деревьев. Подробнее: [workspace-safety-policy.md](workspace-safety-policy.md#worktree-per-task-opt-in-459) |

**Production (agenthai.ru):** `HAIPLANE_WORKSPACE_REPO` обычно указывает на server clone (`/opt/haiplane-hub/src` или продуктовый repo на сервере). Pair-start создаёт branch **там**. В `/etc/haiplane-hub/haiplane-hub.env` обязателен `HAIPLANE_HUB_URL=https://agenthai.ru` — без него MCP echo `instance: local` и `base_url: http://127.0.0.1:8080` (#174, #452).

**Local dev:** часто `HAIPLANE_WORKSPACE_REPO` = тот же каталог, что открыт в Cursor. Тогда pair-start **переключает и чистит этот clone** — см. [Pair mode: git policy](../software-development-workflow.md#pair-mode-git-policy).

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

Сервисный пользователь `haiplane` на agenthai должен уметь `git fetch origin` в
workspace хаба (`/var/lib/haiplane-hub/workspaces/_default`). Если доступа нет,
`pair_prepare_branch` тихо делает `pull --ff-only` с `check=False` и создаёт
pair-ветки от **устаревшего** develop.

**Настройка (человек/админ, один раз):**

1. Сгенерировать read-only deploy key (без passphrase) от имени `haiplane`:

   ```sh
   sudo -u haiplane ssh-keygen -t ed25519 -N '' \
     -f /home/haiplane/.ssh/id_ed25519 -C 'haiplane@agenthai deploy'
   sudo -u haiplane cat /home/haiplane/.ssh/id_ed25519.pub
   ```

2. Добавить публичный ключ в GitHub: репозиторий `agentdrover/haiplane`
   → Settings → Deploy keys → Add deploy key → **Allow write access ВЫКЛ**
   (read-only достаточно; push идёт с машины разработчика).

3. Прописать хост в `~haiplane/.ssh/config` (или задать `GIT_SSH_COMMAND` в
   unit-файле сервиса):

   ```
   Host github.com
     IdentityFile /home/haiplane/.ssh/id_ed25519
     IdentitiesOnly yes
   ```

4. Убедиться, что `origin` использует ssh, а не https:
   `sudo -u haiplane git -C /var/lib/haiplane-hub/workspaces/_default remote set-url origin git@github.com:agentdrover/haiplane.git`

**Проверка (AC-1):**

```sh
ssh agenthai "sudo -n -u haiplane git -C /var/lib/haiplane-hub/workspaces/_default fetch origin --prune \
  && sudo -n -u haiplane git -C /var/lib/haiplane-hub/workspaces/_default remote -v"
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
