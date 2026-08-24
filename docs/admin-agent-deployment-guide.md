# Инструкция для ИИ-агента администратора: развёртывание Haiplane Hub на внешнем сервере

Документ описывает пошаговый протокол, по которому ИИ-агент с ролью администратора
разворачивает сервис `haiplane-hub` на внешнем Linux-сервере (Ubuntu 22.04/24.04
или Debian 12). Все шаги предполагают неинтерактивный запуск под учётной записью
с правами `sudo`.

Если сервер уже развёрнут и нужно только обновить текущий `agenthai.ru`
используйте короткий runbook для агентов:
[`docs/agent-deploy-runbook.md`](agent-deploy-runbook.md).

> Принципы работы агента:
>
> 1. **Идемпотентность.** Каждый шаг проверяет текущее состояние перед изменением.
> 2. **Подтверждение деструктивных операций.** Удаление БД, перезапись конфигов,
>   `rm -rf`, изменение DNS, ротация секретов — только после явного подтверждения
>    оператора.
> 3. **Аудит.** Все выполненные команды и их вывод сохраняются в журнал
>   `/var/log/haiplane-hub/deploy.log`.
> 4. **Секреты не печатать в чат.** Токены, пароли, приватные ключи — только в
>   переменные окружения и файлы с правами `0600`.

---

## 0. Входные параметры

Перед началом агент должен запросить у оператора и зафиксировать:


| Параметр                 | Пример                                        | Обязательность                      |
| ------------------------ | --------------------------------------------- | ----------------------------------- |
| `DEPLOY_HOST`            | `hub.example.com`                             | да                                  |
| `DEPLOY_USER`            | `deploy` (sudoer)                             | да                                  |
| `SSH_KEY_PATH`           | `~/.ssh/id_ed25519_hub`                       | да                                  |
| `DOMAIN`                 | `hub.example.com`                             | да (для TLS)                        |
| `ADMIN_EMAIL`            | `ops@example.com`                             | да (для Let's Encrypt)              |
| `GITHUB_REPO`            | `mrPDA/haiplane-hub`                          | да                                  |
| `HAIPLANE_HUB_REPO`      | `owner/managed-repo`                          | опционально                         |
| `GH_TOKEN`               | `ghp_…`                                       | если используется GitHub-интеграция |
| `INITIAL_ADMIN_LOGIN`    | `admin`                                       | да                                  |
| `INITIAL_ADMIN_PASSWORD` | сгенерировать через `openssl rand -base64 24` | да                                  |


Если хотя бы один обязательный параметр отсутствует — агент **останавливается**
и запрашивает значение у оператора.

---

## 1. Проверка доступа к серверу

```bash
ssh -i "$SSH_KEY_PATH" -o BatchMode=yes -o ConnectTimeout=10 \
    "$DEPLOY_USER@$DEPLOY_HOST" 'echo ok && uname -a && id'
```

Критерии успеха:

- Код возврата `0`.
- Вывод содержит `ok`.
- `id` показывает членство в группе `sudo` либо `wheel`.

При ошибке — сообщить оператору и не продолжать.

---

## 2. Базовая подготовка ОС

Все команды ниже выполняются на удалённом хосте через SSH.

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3.11-dev \
    git curl ca-certificates ufw nginx \
    build-essential pkg-config
```

Проверить версию Python:

```bash
python3.11 --version   # должна быть >= 3.11
```

Если в дистрибутиве нет `python3.11`, использовать
[deadsnakes PPA](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa) или
собрать через `pyenv`. Решение фиксируется в логе.

---

## 3. Сервисный пользователь и каталоги

```bash
# Системный пользователь без shell — запускать сервис под ним
sudo useradd --system --create-home --home-dir /var/lib/haiplane-hub \
    --shell /usr/sbin/nologin haiplane || true

# Каталоги
sudo install -d -o haiplane -g haiplane -m 0750 /etc/haiplane-hub
sudo install -d -o haiplane -g haiplane -m 0750 /var/lib/haiplane-hub
sudo install -d -o haiplane -g haiplane -m 0750 /var/log/haiplane-hub
sudo install -d -o haiplane -g haiplane -m 0750 /opt/haiplane-hub
```

---

## 4. Установка кода

```bash
sudo -u haiplane git clone https://github.com/${GITHUB_REPO}.git /opt/haiplane-hub/src
cd /opt/haiplane-hub/src
LATEST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "main")
sudo -u haiplane git checkout "$LATEST_TAG"
```

Создать виртуальное окружение и установить пакет:

```bash
sudo -u haiplane python3.11 -m venv /opt/haiplane-hub/venv
sudo -u haiplane /opt/haiplane-hub/venv/bin/pip install --upgrade pip wheel
sudo -u haiplane /opt/haiplane-hub/venv/bin/pip install -e /opt/haiplane-hub/src
```

Проверка:

```bash
sudo -u haiplane /opt/haiplane-hub/venv/bin/haiplane-hub --help || true
sudo -u haiplane /opt/haiplane-hub/venv/bin/oc-hub --help
```

---

## 5. Конфигурация (переменные окружения)

Создать файл `/etc/haiplane-hub/haiplane-hub.env` (права `0640`,
владелец `root:haiplane`):

```dotenv
# --- сетевые параметры ---
HAIPLANE_HUB_HOST=127.0.0.1
HAIPLANE_HUB_PORT=8080
# Публичный URL за reverse proxy — echo в MCP (instance/base_url, #174).
HAIPLANE_HUB_URL=https://__DOMAIN__

# --- хранилище ---
HAIPLANE_HUB_HOME=/var/lib/haiplane-hub
HAIPLANE_HUB_DB=/var/lib/haiplane-hub/hub.db
HAIPLANE_TRANSCRIPTS_DIR=/var/lib/haiplane-hub/transcripts

# --- аутентификация ---
# Cookie защищён, потому что сервис стоит за HTTPS-прокси
HAIPLANE_HUB_COOKIE_SECURE=1
# Одноразовый bootstrap-токен для создания первого администратора через UI/CLI.
# После создания админа — закомментировать строку и перезапустить сервис.
HAIPLANE_HUB_BOOTSTRAP_ADMIN_TOKEN=__REPLACE_ME__

# --- интеграции (опционально) ---
HAIPLANE_HUB_REPO=__OWNER__/__REPO__
GH_BIN=/usr/bin/gh
# GH_TOKEN кладётся в /etc/haiplane-hub/secrets.env (см. ниже)
```

Секреты вынести в отдельный файл `/etc/haiplane-hub/secrets.env`
(права `0600`, владелец `root:haiplane`):

```dotenv
GH_TOKEN=ghp_xxx
```

Сгенерировать bootstrap-токен:

```bash
BOOTSTRAP=$(openssl rand -hex 32)
sudo sed -i "s|__REPLACE_ME__|$BOOTSTRAP|" /etc/haiplane-hub/haiplane-hub.env
sudo chown root:haiplane /etc/haiplane-hub/*.env
sudo chmod 0640 /etc/haiplane-hub/haiplane-hub.env
sudo chmod 0600 /etc/haiplane-hub/secrets.env
```

Значение `BOOTSTRAP` передать оператору **один раз** и не сохранять в логах.

---

## 6. systemd unit

Файл `/etc/systemd/system/haiplane-hub.service`:

```ini
[Unit]
Description=Haiplane Hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=haiplane
Group=haiplane
WorkingDirectory=/opt/haiplane-hub/src
EnvironmentFile=/etc/haiplane-hub/haiplane-hub.env
EnvironmentFile=-/etc/haiplane-hub/secrets.env
ExecStart=/opt/haiplane-hub/venv/bin/haiplane-hub
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/haiplane-hub/service.log
StandardError=append:/var/log/haiplane-hub/service.err

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/haiplane-hub /var/log/haiplane-hub
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=true
LockPersonality=true
MemoryDenyWriteExecute=true

[Install]
WantedBy=multi-user.target
```

Запустить:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now haiplane-hub
sudo systemctl status haiplane-hub --no-pager
```

Проверка локального доступа:

```bash
curl -fsS http://127.0.0.1:8080/healthz || curl -fsS http://127.0.0.1:8080/
```

---

## 7. Reverse proxy (nginx + TLS)

Конфиг `/etc/nginx/sites-available/haiplane-hub`:

```nginx
server {
    listen 80;
    server_name __DOMAIN__;
    location /.well-known/acme-challenge/ { root /var/www/letsencrypt; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name __DOMAIN__;

    ssl_certificate     /etc/letsencrypt/live/__DOMAIN__/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/__DOMAIN__/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;

    client_max_body_size 25m;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
        proxy_read_timeout 300s;
        proxy_buffering    off;
    }
}
```

Подставить домен и активировать:

```bash
sudo sed -i "s|__DOMAIN__|$DOMAIN|g" /etc/nginx/sites-available/haiplane-hub
sudo ln -sf /etc/nginx/sites-available/haiplane-hub /etc/nginx/sites-enabled/
sudo install -d /var/www/letsencrypt
sudo nginx -t && sudo systemctl reload nginx
```

Получить TLS-сертификат:

```bash
sudo apt-get install -y certbot
sudo certbot certonly --webroot -w /var/www/letsencrypt \
    -d "$DOMAIN" -m "$ADMIN_EMAIL" --agree-tos --non-interactive
sudo systemctl reload nginx
```

Проверить автообновление:

```bash
sudo systemctl list-timers | grep certbot
```

---

## 8. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw --force enable
sudo ufw status verbose
```

Порт `8080` **не открывать** наружу — сервис слушает только `127.0.0.1`.

---

## 9. Создание первого администратора

После старта сервиса агент создаёт администратора через bootstrap-токен.

```bash
# С локального хоста (на сервере). Токен из env: заголовок Authorization: Bearer
# (не X-Bootstrap-Token). В теле JSON поле username, не login. Пароль должен
# удовлетворять правилам сложности в hub/models.py (в т.ч. спецсимвол).
curl -fsS -X POST "https://${DOMAIN}/api/admin/bootstrap" \
    -H "Authorization: Bearer ${BOOTSTRAP}" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"${INITIAL_ADMIN_LOGIN}\",\"password\":\"${INITIAL_ADMIN_PASSWORD}\"}"
```

> Если эндпойнт отличается — агент сверяется с `hub/services/admin.py` и
> `docs/admin-section-design.md`, затем уточняет вызов.

После успешного ответа:

1. Закомментировать `HAIPLANE_HUB_BOOTSTRAP_ADMIN_TOKEN` в
  `/etc/haiplane-hub/haiplane-hub.env`.
2. Перезапустить сервис: `sudo systemctl restart haiplane-hub`.
3. Передать оператору учётные данные администратора по защищённому каналу
  (1Password / Bitwarden / Vaultwarden) — **не в чат**.

---

## 10. Резервное копирование

База — SQLite, поэтому корректное копирование — через `.backup`:

```bash
sudo install -d -o haiplane -g haiplane -m 0750 /var/backups/haiplane-hub
sudo tee /etc/cron.daily/haiplane-hub-backup >/dev/null <<'EOF'
#!/bin/sh
set -e
TS=$(date -u +%Y%m%dT%H%M%SZ)
DEST="/var/backups/haiplane-hub/hub-$TS.db"
sudo -u haiplane /opt/haiplane-hub/venv/bin/python -c \
    "import sqlite3,sys; \
     src=sqlite3.connect('/var/lib/haiplane-hub/hub.db'); \
     dst=sqlite3.connect(sys.argv[1]); \
     src.backup(dst); dst.close(); src.close()" "$DEST"
gzip -9 "$DEST"
find /var/backups/haiplane-hub -name 'hub-*.db.gz' -mtime +14 -delete
EOF
sudo chmod 0755 /etc/cron.daily/haiplane-hub-backup
```

Один раз вручную проверить корректность бэкапа и восстановления на тестовом
пути.

---

## 11. Smoke-тесты

Агент обязан выполнить и приложить вывод:

```bash
# 1. Сервис жив
systemctl is-active haiplane-hub
# 2. Порт слушается только локально
sudo ss -tlnp | grep ':8080'
# 3. HTTPS отвечает
curl -fsSI "https://$DOMAIN/" | head -5
# 4. TLS валидный
echo | openssl s_client -servername "$DOMAIN" -connect "$DOMAIN":443 2>/dev/null \
    | openssl x509 -noout -dates -subject
# 5. Логи без критики за последние 5 минут
sudo journalctl -u haiplane-hub --since '5 min ago' --no-pager | tail -50
```

Если хоть одна проверка упала — фиксируем причину в логе деплоя и
останавливаемся.

---

## 12. Обновление до новой версии

Стандартный цикл обновления (подтверждение оператора **обязательно**, потому
что задействует БД и рестарт сервиса):

```bash
cd /opt/haiplane-hub/src
sudo -u haiplane git fetch --tags
NEW_TAG=<запросить у оператора>
sudo -u haiplane git checkout "$NEW_TAG"
sudo -u haiplane /opt/haiplane-hub/venv/bin/pip install -e .
# Бэкап перед рестартом
sudo /etc/cron.daily/haiplane-hub-backup
sudo systemctl restart haiplane-hub
sudo journalctl -u haiplane-hub -f --since '1 min ago'
```

При ошибке миграции — откатить `git checkout <предыдущий тег>` и восстановить
БД из последнего архива в `/var/backups/haiplane-hub`.

---

## 13. Чеклист завершения деплоя

Агент отчитывается оператору в виде заполненного чеклиста:

- SSH-доступ проверен.
- Системные пакеты установлены, Python ≥ 3.11.
- Пользователь `haiplane` и каталоги созданы с правильными правами.
- Код выкачен на тег `<TAG>`, venv собран.
- Файлы `/etc/haiplane-hub/*.env` созданы, права `0640/0600`.
- systemd-юнит установлен, сервис активен, рестартует автоматически.
- nginx + Let's Encrypt настроены, HTTPS отвечает 200/301.
- UFW активен, наружу открыты только 22/80/443.
- Первый администратор создан, bootstrap-токен отозван.
- Cron-бэкап работает, тестовое восстановление выполнено.
- Smoke-тесты пройдены, логи без ошибок.
- Учётные данные переданы оператору через защищённый канал.

---

## 14. Что НЕЛЬЗЯ делать без явной команды оператора

- Удалять `/var/lib/haiplane-hub/hub.db` или каталог бэкапов.
- Запускать `git reset --hard`, `git clean -fdx` в `/opt/haiplane-hub/src`.
- Открывать порт `8080` наружу или менять `HAIPLANE_HUB_HOST` на `0.0.0.0`
без TLS-прокси.
- Отключать аутентификацию (`HAIPLANE_HUB_AUTH_DISABLED=1`,
`HAIPLANE_HUB_ALLOW_UNAUTHENTICATED_NETWORK=1`).
- Менять DNS-записи или ротацию TLS-сертификата.
- Передавать секреты в чат или в публичные репозитории.

При сомнении — остановиться и запросить подтверждение.