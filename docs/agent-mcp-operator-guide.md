# Haiplane Hub — MCP operator guide

Единая инструкция для оператора и агента: как подключить Hub через MCP
(streamable HTTP или stdio), проверить соединение и устранить типовые ошибки.

> **Не дублирует** полный серверный деплой — см.
> [`docs/admin-agent-deployment-guide.md`](admin-agent-deployment-guide.md) и
> [`docs/agent-deploy-runbook.md`](agent-deploy-runbook.md).
> Сеть через Tailscale: [`deploy/TAILSCALE.md`](../deploy/TAILSCALE.md).

---

## Оглавление

1. [Что нужно перед стартом](#1-что-нужно-перед-старта)
2. [Два транспорта: stdio и streamable HTTP](#2-два-транспорта-stdio-и-streamable-http)
3. [Быстрый старт с нуля (streamable)](#3-быстрый-старт-с-нуля-streamable)
4. [Подключение Cursor](#4-подключение-cursor)
4a. [Cloud / iOS: чат без MCP](#4a-cloud--ios-чат-без-mcp)
5. [Проверка curl](#5-проверка-curl)
6. [Диагностика identity и health](#6-диагностика-identity-и-health)
7. [Troubleshooting](#7-troubleshooting)
8. [Staging vs production](#8-staging-vs-production)
9. [См. также](#9-см-также)

---

## 1. Что нужно перед стартом

| Что | Зачем |
|---|---|
| Рабочий Hub (`haiplane-hub` или systemd) | REST + MCP на одном процессе |
| Bearer-токен | `HAIPLANE_HUB_TOKENS`, DB API key или `HAIPLANE_HUB_TOKEN` (stdio) |
| Доступ к URL Hub | localhost, SSH-туннель, Tailscale или reverse proxy |
| Cursor ≥ поддержки streamable HTTP MCP | для удалённого инстанса |

**Канонический MCP endpoint (streamable HTTP):**

```text
https://<host>/mcp
```

или для локальной разработки:

```text
http://127.0.0.1:8080/mcp
```

> **Частая ошибка:** путь `/mcp/mcp` — это устаревший вложенный mount.
> Сейчас он возвращает **404**. Используйте только **`/mcp`**.

Обязательные заголовки для streamable HTTP:

| Заголовок | Значение |
|---|---|
| `Authorization` | `Bearer <TOKEN>` (не печатайте токен в чат/логи) |
| `Accept` | `application/json, text/event-stream` |
| `Content-Type` | `application/json` (на POST с JSON-RPC телом) |

Hub также подмешивает недостающие части `Accept` через middleware, но клиент
лучше настроить явно — так проще отлаживать.

---

## 2. Два транспорта: stdio и streamable HTTP

| | **stdio** | **streamable HTTP** |
|---|---|---|
| Когда | локальная разработка, Pi рядом с Hub | Cursor на ноутбуке → удалённый/staging/prod Hub |
| Cursor config | `"command": "uv", "args": ["run", "haiplane-hub-mcp"]` | `"type": "streamable-http", "url": "…/mcp"` |
| Токен | env `HAIPLANE_HUB_TOKEN` | заголовок `Authorization: Bearer …` |
| URL Hub | env `HAIPLANE_HUB_URL` (REST backend для subprocess) | URL в `mcp.json` |
| Сессия MCP | управляет subprocess | `initialize` → заголовок `Mcp-Session-Id` на follow-up |

**stdio** запускает `haiplane-hub-mcp`, который проксирует вызовы инструментов
в REST API Hub по `HAIPLANE_HUB_URL`. Токен берётся из `HAIPLANE_HUB_TOKEN`.

**streamable HTTP** — клиент (Cursor) говорит напрямую с `/mcp` того же
uvicorn-процесса, что и Web UI. Auth — Bearer на каждый запрос; после
`initialize` нужен `Mcp-Session-Id` для `tools/list` и `tools/call`.

Пример stdio (локально) — см. [`.cursor/mcp.json.example`](../.cursor/mcp.json.example).

---

## 3. Быстрый старт с нуля (streamable)

Пошагово для нового оператора (локальный Hub; для staging замените host).

### 3.1 Поднять Hub

```bash
cd haiplane
uv sync
export HAIPLANE_HUB_TOKENS="dev:YOUR_TOKEN_HERE:agent"
export HAIPLANE_HUB_HOST=127.0.0.1
export HAIPLANE_HUB_PORT=8080
uv run haiplane-hub
```

Проверка liveness (без auth):

```bash
curl -fsS http://127.0.0.1:8080/healthz
# → ok
```

### 3.2 Проверить REST auth

```bash
export HUB=http://127.0.0.1:8080
export TOKEN=YOUR_TOKEN_HERE

curl -fsS -H "Authorization: Bearer $TOKEN" "$HUB/api/tasks?limit=1"
```

Ожидается JSON-массив (200). Без токена — 401.

### 3.3 Initialize MCP (JSON-RPC)

```bash
curl -sS -D /tmp/mcp-headers.txt \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1.0"}}}' \
  "$HUB/mcp"
```

Критерии успеха:

- HTTP **200**
- в теле есть `serverInfo` с именем `haiplane-hub`
- в заголовках ответа есть **`Mcp-Session-Id`**

Сохраните session id:

```bash
SESSION=$(grep -i '^mcp-session-id:' /tmp/mcp-headers.txt | cut -d: -f2- | tr -d ' \r\n')
echo "session=$SESSION"
```

### 3.4 tools/list

```bash
curl -sS \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: $SESSION" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  "$HUB/mcp"
```

В списке должны быть `hub_project_status`, `hub_create_task`, и т.д.

Если шаги 3.3–3.4 проходят — MCP на этом инстансе **рабочий** (AC-1 для staging:
повторите с URL staging и выданным оператором токеном).

---

## 4. Подключение Cursor

Скопируйте шаблон из [`.cursor/mcp.json.example`](../.cursor/mcp.json.example)
или добавьте сервер:

```json
{
  "mcpServers": {
    "haiplane-hub": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8080/mcp",
      "headers": {
        "Authorization": "Bearer ${env:HAIPLANE_HUB_MCP_TOKEN}",
        "Accept": "application/json, text/event-stream"
      }
    }
  }
}
```

Рекомендации:

1. Токен храните в env (`HAIPLANE_HUB_MCP_TOKEN`), не в git.
2. URL заканчивается на **`/mcp`**, не `/mcp/mcp`.
3. После смены `mcp.json` перезагрузите MCP в Cursor (Reload Window).
4. Для production за reverse proxy используйте `https://<domain>/mcp` и
   убедитесь, что `Host` входит в `HAIPLANE_HUB_ALLOWED_HOSTS` на сервере.

**Локальный stdio-вариант** (Hub на той же машине):

```json
{
  "mcpServers": {
    "haiplane-hub-local": {
      "command": "uv",
      "args": ["run", "haiplane-hub-mcp"],
      "env": {
        "HAIPLANE_HUB_URL": "http://127.0.0.1:8080",
        "HAIPLANE_HUB_TOKEN": "<same token as HAIPLANE_HUB_TOKENS value>"
      }
    }
  }
}
```

---

## 4a. Cloud / iOS: чат без MCP

Cursor for iOS и `cursor.com/agents` не дают добавить свой MCP: облачная сессия
не видит `.cursor/mcp.json`, и агент в таком чате без личности получает 401,
либо `403 agent_create_forbidden`. Для этого случая есть **chat-pair** —
одноразовый код, который обменивается на короткую сессию с правами только на
постановку и уточнение задач (#961).

**Никогда не вставляйте в чат ноутбучный `HAIPLANE_HUB_MCP_TOKEN`**: он живёт
днями и останется в транскрипте Cursor навсегда. В чат идёт только код с кнопки.

### Как оператору

1. Откройте хаб в браузере (в том числе на телефоне) → **«Подключить чат»**
   (`/chat-pair`) → кнопка **«Получить код»**.
2. Код вида `AH-7K2M9QRS` живёт ~5 минут, работает один раз, и запрос нового
   кода сжигает предыдущий.
3. В чате Cursor напишите агенту: `хаб: AH-7K2M9QRS`.
4. Закончили — кнопка **«Отозвать чат-сессии»** на той же странице. Вход в
   браузере и API-ключи она не трогает.

### Как агенту в облачном чате

```bash
# 1. Обменять код на сессию (публичный маршрут, Authorization не нужен)
curl -sS -X POST https://<hub>/api/auth/chat-pair/redeem \
  -H 'Content-Type: application/json' \
  -d '{"code":"AH-7K2M9QRS"}'
# → { "token": "...", "expires_at": "...", "username": "...",
#     "role": "human", "base_url": "...", "permissions": [...] }

# 2. Проверить, что это действительно chat-pair сессия
curl -sS https://<hub>/api/whoami -H "Authorization: Bearer $TOKEN"
# → auth_source=chat_pair, permissions: tasks.read/create/refine/update

# 3. Поставить задачу
curl -sS -X POST https://<hub>/api/tasks -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"title":"...","description":"..."}'
```

Правила для агента:

1. **Токен не эхом.** Не печатать его в чат, в task updates, в описание PR.
2. Задача создаётся в `open`. `run_immediately` и `auto_review=false` этот канал
   отклоняет с 422 `chat_pair_run_forbidden` — запуск исполнения делается из
   хаба, а не из чужого транскрипта.
3. Всё, кроме постановки и уточнения задачи, закрыто: approve, review-verdict,
   pair-start, проекты, треды, `/api/admin/*` и сам `/mcp` отвечают 403
   `chat_pair_gate_forbidden`. Это не баг канала, а его граница.
4. Повторный код — 401 `chat_pair_invalid`; неизвестный и просроченный отвечают
   ровно так же.

Настройки на сервере (drop-in, все с разумными дефолтами):
`HAIPLANE_CHAT_PAIR_CODE_SECONDS` (300), `HAIPLANE_CHAT_PAIR_TTL_SECONDS` (7200),
`HAIPLANE_CHAT_PAIR_REDEEM_MAX` / `HAIPLANE_CHAT_PAIR_REDEEM_WINDOW_SECONDS`
(10 / 300). В open mode (хаб без принципалов и `HAIPLANE_HUB_TOKENS`) канал
отвечает 503 `chat_pair_auth_required`: без auth нет и личности, которую мог бы
нести код.

---

## 5. Проверка curl

Минимальный набор после деплоя:

```bash
# 1. Liveness
curl -fsS "$HUB/healthz"

# 2. Public health snapshot (bind/auth/vast flags, без секретов)
curl -fsS "$HUB/health" | jq .

# 3. Identity под вашим токеном
curl -fsS -H "Authorization: Bearer $TOKEN" "$HUB/api/whoami" | jq .

# 4. MCP initialize (см. раздел 3.3)
```

---

## 6. Диагностика identity и health

| Surface | Команда / tool | Что показывает |
|---|---|---|
| REST | `GET /api/whoami` | username, role, permissions, auth source (`env` / `db`) |
| REST | `GET /health` | bind host/port, auth_required, vast_enabled |
| CLI | `oc-hub whoami` / `oc-hub health` | то же через CLI |
| MCP | `hub_whoami` / `hub_health` | то же для агента в Cursor |

Если `hub_whoami` показывает не ту роль — сверьте токен в заголовке с
`HAIPLANE_HUB_TOKENS` или с выданным DB API key (источник `db`, id ключа без секрета).

---

## 7. Troubleshooting

| Симптом | HTTP | Вероятная причина | Что делать |
|---|---|---|---|
| **401 Unauthorized** | 401 | Нет или неверный `Authorization: Bearer` | Выдайте токен из `HAIPLANE_HUB_TOKENS` / admin API keys; для stdio — `HAIPLANE_HUB_TOKEN`; не используйте пустой Bearer |
| **421 Misdirected Request** / rebinding | 421 | Неверный `Host` / DNS rebinding protection у клиента | Используйте hostname из `HAIPLANE_HUB_ALLOWED_HOSTS`; для Tailscale — MagicDNS из [`deploy/TAILSCALE.md`](../deploy/TAILSCALE.md); Hub отключает MCP-layer rebinding, но nginx/proxy должен проксировать правильный Host |
| **406 Not Acceptable** | 406 | В `Accept` нет `application/json` и/или `text/event-stream` | Добавьте `Accept: application/json, text/event-stream` в Cursor headers и curl |
| **Missing session** / tools fail после initialize | 400/ошибка MCP | Вызов `tools/list` или `tools/call` без `Mcp-Session-Id` | Сначала `initialize`, возьмите `Mcp-Session-Id` из ответа, передайте во все follow-up запросы |
| **404 на `/mcp/mcp`** | 404 | Устаревший путь | Используйте **`/mcp`** |
| Connection refused | — | Hub не слушает / неверный туннель | `curl /healthz` на loopback; для prod — SSH `-L 8080:127.0.0.1:8080` (см. [`docs/agent-onboarding.md`](agent-onboarding.md)) |
| 403 human_only_gate в MCP | 403 | Agent-токен на human-only tool | Используйте human/admin токен или попросите человека (`hub_force_complete_task`, `hub_decide_task`, …) |

### 401 — подробнее

- REST `/api/*` и `/mcp` требуют auth, когда настроен `HAIPLANE_HUB_TOKENS`
  и не включён `HAIPLANE_HUB_AUTH_DISABLED`.
- `/healthz` и `/health` публичны; 401 на них не ожидается.
- Cookie-сессия Web UI и Bearer — разные пути; MCP использует **только Bearer**.

### 406 — подробнее

MCP streamable HTTP negotiation требует JSON **и** SSE в Accept. Cursor иногда
шлёт неполный Accept на GET; Hub middleware это исправляет для путей `/mcp/*`,
но явный заголовок в конфиге надёжнее.

### Missing session — подробнее

Типичная последовательность:

1. `POST /mcp` + `initialize` → сохранить `Mcp-Session-Id`
2. `POST /mcp` + `notifications/initialized` (если требует клиент)
3. `POST /mcp` + `tools/list` / `tools/call` **с тем же** `Mcp-Session-Id`

Без шага 1 клиент пишет «Missing session» или аналог.

---

## 8. Staging vs production

| | Локально | Staging / production |
|---|---|---|
| URL | `http://127.0.0.1:8080/mcp` | `https://<domain>/mcp` или туннель на `:8080` |
| База | своя `hub.db` | **отдельная** от локальной |
| Деплой кода | ветка разработчика | merge в `main` → auto-deploy ([`deploy/CD.md`](../deploy/CD.md)) |
| Токен | свой dev-токен | файл на сервере / выданный оператором (**не** коммитить) |

Перед работой агент должен понимать, **к какому инстансу** подключён MCP —
задачи и статусы между инстансами не синхронизируются.

---

## 9. См. также

- [`docs/agent-onboarding.md`](agent-onboarding.md) — жизненный цикл задач и MCP tools
- [`docs/cursor-agent-rules.md`](cursor-agent-rules.md) — правила агента
- [`docs/admin-agent-deployment-guide.md`](admin-agent-deployment-guide.md) — полный деплой сервера
- [`docs/agent-deploy-runbook.md`](agent-deploy-runbook.md) — runbook agenthai / обновления
- [`deploy/TAILSCALE.md`](../deploy/TAILSCALE.md) — tailnet + MCP для команды
- [`.cursor/mcp.json.example`](../.cursor/mcp.json.example) — готовые фрагменты конфига
