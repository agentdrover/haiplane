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
