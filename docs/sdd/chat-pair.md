# SDD: Chat-pair — постановка задач из Cursor без MCP

> Спека для задачи хаба (`work_type: feature`).  
> Не реализация. Ветка/коммит не входят в этот документ.  
> Контекст: Cursor web/agents не даёт добавить свой MCP; оператор ставит
> задачи из чата приложения (iOS / cloud) так же, как с ноутбука.  
> **Rev. 4** — правки по третьему ревью (G1–G5): запрет `run_immediately`
> в create для chat-pair, точные метод+путь в allowlist, механика матчинга
> в middleware, явный отказ `/mcp`, синхронизация YAML.  
> Rev. 3 — второе ревью (F1′–F5′): allowlist маршрутов вместо точечных
> запретов, презентационная роль, требования к фикстурам, общая обвязка
> ошибок, self-revoke.  
> Rev. 2 — первый круг ревью (F1–F9): узкие права сессии, отдельный
> rate limit, revoke-ручка, reaper, open-mode, CSRF, точные AC.

---

## Мета

| Поле | Значение |
|---|---|
| `work_type` | `feature` |
| `class_of_service` | `standard` |
| `size` | `M` |
| `wip_tag` | `feature_work` |
| `task_type` | `task` (исполняемая; epic/feature не автостарт) |
| `redesign_decision` | `redesign` |
| `agent_fit` | `sdd_native` |

`redesign_rationale`: не чинить отсутствие MCP в Cursor Agents. Новый канал
личности: одноразовый код в чате → короткая сессия с **узкими** правами →
REST. MCP не расширяем. Сессия не наследует admin/human-gate права
принципала — иначе код в транскрипте Cursor стоит человеческого гейта.

Ключевое решение rev. 3: доступ сессии описан **положительным allowlist**
маршрутов, а не запретами в отдельных гейтах. Идентичность в хабе бинарная
(human ⟂ agent), chat-pair — третье состояние, и любая ветка вида
`if identity.is_agent: … else: <человеческая ветка>` по умолчанию отдаёт
ему человеческую. Перечислять такие ветки — гарантированно пропустить
следующую.

---

## User story

Как оператор в приложении Cursor (телефон или облачный чат), я хочу вставить
короткий код из хаба и сразу поставить задачу, чтобы не нужен был свой MCP
в web/agents и не нужно было везти ноут.

---

## Problem statement

На ноутбуке постановка идёт через Hub MCP (`hub_create_task` + human-токен).
В Cursor for iOS / `cursor.com/agents` свой MCP добавить нельзя. Облачная
сессия не видит `.cursor/mcp.json`. Без личности агент не может вызвать
`POST /api/tasks`: либо 401, либо агентский токен даёт
`403 agent_create_forbidden`.

Вставка постоянного `HAIPLANE_HUB_MCP_TOKEN` в чат оставляет секрет в
транскрипте Cursor навсегда. Текущие API-ключи живут днями
(`expires_days >= 1`) — для чата это слишком долго.

Полная human/admin-сессия из такого кода недопустима: тот же принципал на
проде часто `admin`. Одним `auth_source` с узким permission set проблема не
закрывается: хаб решает «человек или агент» не в одном месте, а десятком
веток `is_agent` / `is_human` — и часть из них вообще без гейта. Так,
`POST /api/tasks/{id}/review-verdict` не имеет зависимости-гейта, а
`ensure_reviewer_independence(is_agent=False)` читает не-агента как человека,
то есть сессия смогла бы ставить APPROVED в обход Universal Review Gate
(#318/#432). Поэтому нужен и отдельный `auth_source`, и deny-by-default
allowlist маршрутов.

---

## Business value

Оператор ставит задачу в прод-хаб с телефона за один прогон Cursor, без
ноута и без кастомного MCP. Секрет в чате — одноразовый код на минуты.
Атрибуция задачи — тот же human-принципал, что выдал код; **права
сессии — только постановка и чтение/уточнение**, не approve / decide /
force-complete / admin.

---

## Discovery

| Поле | Значение |
|---|---|
| `outcome_metric` | С телефона создана задача в `open` от принципала P, без MCP и без human-gate прав у сессии |
| `outcome_indicator` | AC-4, AC-7, AC-7b, AC-7c зелёные; повторный redeem → 401 (AC-5) |
| `outcome_deadline` | Первый живой прогон после merge в `develop` |
| `outcome_revisit_condition` | Cursor начнёт принимать custom MCP на web/agents — тогда chat-pair остаётся запасным каналом, не удаляем молча |
| `redesign_decision` | `redesign` |
| `agent_fit` | `sdd_native` |

---

## Scope in

- Выдача одноразового кода залогиненному human (Bearer **или** cookie+CSRF).
- Публичный redeem кода на сессионный Bearer (`auth_source=chat_pair`).
- Сессия 1–2 часа; `principal_id` = кто выдал код; **фиксированный узкий
  permission set** (не роль/права принципала).
- **Allowlist маршрутов** для `auth_source=chat_pair`: всё, чего нет в
  списке, — 403 `chat_pair_gate_forbidden`, включая маршруты, которых
  сегодня не существует.
- Повторный redeem / чужой / просроченный код → одна и та же 401.
- **Отдельный** rate limiter на redeem по IP (не `login_limiter`).
- Повторный start тем же человеком сжигает неиспользованный предыдущий код.
- Revoke всех chat-pair сессий принципала (REST + кнопка рядом с кодом);
  сессия может погасить сама себя.
- Reaper: чистка просроченных/сожжённых кодов и сессий в `_session_reaper`.
- Отказ start/redeem в open mode (`HUB_TOKENS` пуст / `HUB_AUTH_DISABLED`).
- Кнопка в Web UI (Safari): код + таймер + отзыв.
- Документация в `docs/agent-mcp-operator-guide.md`: блок Cloud / iOS.
- Аудит: start, redeem, revoke — без plaintext кода и токена.

---

## Scope out

- Добавление MCP в каталог Cursor / inline `mcpServers` в Cloud API.
- `RuntimeChoice.cursor_cloud`, implementer dispatch, путь C.
- Изменение `hub_create_task` / снятие `agent_create_forbidden`.
- Новые MCP tools (`hub_chat_pair_*`). MCP catalog не растёт.
- CLI-команда pairing (REST + Web достаточно).
- Reserve branch, `adopt_branch`, merge с телефона, git-хвост облачного агента.
- OAuth / magic link на email.
- Подписка на `bc-…` и склейка с карточкой задачи.
- Растягивание `api_keys.expires_days` до минут.

---

## Affected areas

- `hub/auth.py` — public redeem; resolve `chat_pair`; отказ open mode;
  **allowlist-проверка в `AuthMiddleware`**; отдельный `chat_pair_limiter`
- `hub/app.py` — start, redeem, revoke
- `hub/models.py` — views/bodies chat-pair
- `hub/db.py` — `chat_pair_codes`, `chat_pair_sessions`
- `hub/services/chat_pair.py` (новый)
- `hub/config.py` — TTL кода/сессии, порог rate limit
- `hub/poller.py` — reaper для chat-pair таблиц
- `hub/web.py` + `hub/templates/` — форма кода / отзыв
- `docs/agent-mcp-operator-guide.md`
- `docs/agent-context/change-map.md`
- `hub/mcp_envelope.py` / `hub/actionable_errors.py` — новые `reason`
  проходят общую обвязку (`enrich_error_payload`, `compute_next_action`)
- `tests/test_chat_pair.py`
- `tests/test_db_migrations.py`
- `tests/test_auth.py` (публичный путь; allowlist-отказ для chat_pair)

---

## Constraints

- Код и сессионный токен в логи, task updates, audit `detail` и ответы
  агента в чат не пишутся. В БД только hash.
- `POST /api/auth/chat-pair/redeem` — публичный путь. Start — только
  аутентифицированный human, не open mode.
- Start: **либо** `Authorization: Bearer` human, **либо** cookie-сессия
  **с валидным CSRF**. Cookie без CSRF → 403. Третьего нет.
- Агентский токен не выдаёт код: 403, `detail.reason=human_only_gate`.
- Chat-pair сессия ходит **только** по allowlist маршрутов; всё остальное —
  403 `chat_pair_gate_forbidden`. Deny-by-default, а не список запретов.
- `role` у chat-pair — **презентационное** поле (`"human"` для whoami и
  атрибуции). Ни один гейт не имеет права принимать решение по нему:
  основание — `auth_source` и allowlist. `role="admin"` в сессии не
  появляется никогда, иначе `is_admin` пройдёт по роли.
- Permission set сессии фиксирован (см. Technical hints). Не копировать
  `permissions` / `role` admin-принципала.
- Новые `reason` (`chat_pair_gate_forbidden`, `chat_pair_invalid`,
  `chat_pair_rate_limited`, `chat_pair_auth_required`,
  `chat_pair_run_forbidden`) проходят через `enrich_error_payload` и
  получают строку в `compute_next_action`.
- `POST /api/tasks` от chat-pair: `run_immediately=true` и
  `auto_review=false` → 422 `chat_pair_run_forbidden` (см. Technical hints).
- `/mcp` для chat-pair закрыт явно (403), не как побочный эффект.
- Не класть сессионный токен в `job_id` и не вызывать Cloud Agents API.
- Контракт REST каноничен. MCP/CLI pairing не дублируют. В сабмишене к
  `surface_parity.py`: «pairing — auth-канал, не доменный контракт;
  MCP tools/list не меняется».
- Rate limit redeem — **отдельный** инстанс лимитера, не `login_limiter`
  (общий бакет по IP иначе блокирует `/login` оператору).

---

## Assumptions

- `https://agenthai.ru` доступен с VM Cursor cloud (ревьюер уже ходит на
  `/mcp`).
- Оператор может открыть Web UI хаба в Safari на том же телефоне
  (cookie-сессия).
- Облачный агент умеет `curl` / `httpx` на публичный HTTPS.
- Человек не вставляет в чат ноутбучный `HAIPLANE_HUB_MCP_TOKEN`.
- На проде auth включён (есть principals / `HUB_TOKENS`); open mode — только
  локальная совместимость, chat-pair там отключён явно.

---

## Technical hints

Три эндпоинта, отдельные таблицы, не `api_keys`.

```text
POST /api/auth/chat-pair/start      # human Bearer | cookie+CSRF → { code, expires_in_sec }
POST /api/auth/chat-pair/redeem     # public { code } → { token, expires_at, username, role, base_url, permissions }
POST /api/auth/chat-pair/revoke     # human Bearer | cookie+CSRF → revoke all chat-pair sessions of caller
```

### Код

- Алфавит: Crockford base32 без `I L O U` (и без путаницы 0/O, 1/I).
- Длина payload: **ровно 8** символов алфавита (~40 бит).
- Отображение: `AH-XXXXYYYY` (префикс `AH-` + 8 символов; дефис только для
  чтения). В БД и в hash — нормализованная форма: uppercase, без дефисов,
  без префикса → 8 символов.
- Redeem принимает любой из видов (`AH-7K2M9Q`, `ah-7k2m9q`, `7K2M9Q`) после
  одной функции `normalize_pair_code`.
- TTL: `HAIPLANE_CHAT_PAIR_CODE_SECONDS` (default 300). В ответе start
  `expires_in_sec` = **текущее** значение конфига, не хардкод 300.

### Сессия

- Таблица `chat_pair_sessions`: `token_hash`, `principal_id`, `expires_at`,
  `revoked_at`, `created_at`.
- TTL: `HAIPLANE_CHAT_PAIR_TTL_SECONDS` (default 7200).
- `TokenIdentity`:
  - `principal_id` = issuer
  - `username` = username принципала (атрибуция)
  - `role` = `"human"` — **презентационно**, не основание для решений
    (см. Constraints). Роль форсится, потому что `is_admin` возвращает True
    по `role in ("admin","super_admin")`, а `has_permission` для
    `super_admin` вообще короткозамыкается на True.
  - `auth_source` = `"chat_pair"`
  - `permissions` = `_CHAT_PAIR_PERMS` (фиксированный frozenset):

```text
tasks.read
tasks.create
tasks.refine
tasks.update
```

  Явно **нет**: `tasks.human_gate`, `tasks.decision`, `tasks.archive`,
  `tasks.delete`, `admin.*`, `integrations.vast.manage`, `tasks.agent_report`.

- Create task: `is_agent` = false → `_reject_agent_authored_source`
  пропускает; задача `open` с атрибуцией `principal_id`.

### Allowlist маршрутов (F1′)

Одна проверка в `AuthMiddleware` **после** резолва идентичности: если
`auth_source == "chat_pair"` и `(method, path)` не совпал с шаблоном из
списка — 403 `chat_pair_gate_forbidden`.

**Механика матчинга (G3).** `AuthMiddleware.dispatch` выполняется до
роутинга: в нём есть только сырой `request.url.path`, шаблон роута FastAPI
ещё не известен. Поэтому allowlist объявляется списком пар
`(method, "/api/tasks/{task_id}/refine")` и на старте компилируется в
якорённые регексы (`{param}` → `[^/]+`, `^…$`); middleware сверяет
`(request.method, path)` с компилятом. Префиксное сравнение строк
запрещено: префикс `/api/tasks/` пустил бы и `approve`, и `decide`.

Разрешено ровно это (метод + путь, пути сверены с `hub/app.py`;
`acceptance_criteria` — с подчёркиванием):

```text
GET    /api/whoami
GET    /api/diagnostics/identity
GET    /api/tasks
GET    /api/tasks/{task_id}
GET    /api/tasks/{task_id}/tree
GET    /api/tasks/{task_id}/context
GET    /api/tasks/{task_id}/readiness
POST   /api/tasks
POST   /api/tasks/{task_id}/refine
GET    /api/tasks/{task_id}/acceptance_criteria
POST   /api/tasks/{task_id}/acceptance_criteria
PUT    /api/tasks/{task_id}/acceptance_criteria
PUT    /api/tasks/{task_id}/acceptance_criteria/{ac_id}
DELETE /api/tasks/{task_id}/acceptance_criteria/{ac_id}
POST   /api/tasks/{task_id}/risks
POST   /api/auth/chat-pair/revoke
```

Replace (`PUT` списком) и `DELETE` включены сознательно: это тот же
авторинг AC, которым канал и занимается; ничего за пределами черновика
задачи они не трогают.

**Ограничение внутри разрешённого маршрута (G1).** `POST /api/tasks`
разрешён, но `TaskCreate` несёт `run_immediately`, а `create_task` для
не-агентов диспатчит немедленно (`hub/services/lifecycle.py`:
`if normalized.run_immediately and source != agent → dispatch_task`).
Для `auth_source=chat_pair`:

- `run_immediately=true` → **422** `chat_pair_run_forbidden`. Отклонение,
  не молчаливое обнуление: тихая деградация прочитается как «запустилось».
- `auto_review=false` → тоже **422** (тот же reason). Опт-аут ревью —
  решение, которое не должно приниматься с канала, живущего в чужом
  транскрипте. Дефолт `auto_review=true` проходит как есть.

Всё остальное закрыто по умолчанию, в том числе то, что каждый по
отдельности пропустил бы chat-pair как «человека»:

| Маршрут | Что пропустило бы без allowlist |
|---|---|
| `POST /api/tasks/{id}/review-verdict` | гейта нет вообще; `ensure_reviewer_independence(is_agent=False)` читает сессию как человека → APPROVED в обход Universal Review Gate (#318/#432) |
| `POST /api/tasks/{id}/approve`, `/reject`, `/decide`, `/force-complete`, `/batch-approve` | `require_human_or_admin` смотрит только на `is_agent` |
| `POST /api/projects`, `POST /api/skills` | `is_agent` False → `active` вместо `pending`/`draft` (`hub/app.py:409,685`) |
| `POST /api/tasks/{id}/pair-start`, `/claim` | `caller_is_agent=False` снимает требование `session_id` (#852) |
| `GET` треды сообщений | `is_human=not is_agent` открывает чужие треды (`hub/app.py:2031`) |
| `POST /api/tasks/{id}/updates` | `tasks.update` есть, а `kind=done` заходит в lifecycle. Сознательно НЕ в списке: цель канала — поставить задачу, не вести её |
| `/api/admin/*` | `require_admin` |

Веб-маршруты (`hub/web.py`) закрыты тем же правилом: chat-pair — API-канал,
UI ходит под cookie.

`/mcp` тоже вне списка — и это решение, а не следствие (G4): утёкший
chat-pair токен, воткнутый в MCP-клиент, не должен получить каталог
инструментов хаба. 403 на `/mcp` проверяется в AC-7d.

Расширение списка — отдельное решение с обоснованием в задаче, не правка
по ходу реализации.

### Rate limit

- Новый инстанс: `chat_pair_limiter = LoginRateLimiter(max_attempts=10,
  window_seconds=300)` в `hub/auth.py` (или рядом в `chat_pair.py`).
- **Не** писать в `login_limiter`.
- Порог конфигурируем: `HAIPLANE_CHAT_PAIR_REDEEM_MAX` /
  `HAIPLANE_CHAT_PAIR_REDEEM_WINDOW_SECONDS` (defaults 10 / 300).
- При блокировке redeem → **HTTP 429**, `detail.reason=chat_pair_rate_limited`.
  Это решение API (веб-логин исторически делает 303 — не копировать).

### Open mode

- Если `_is_open_mode()`: start и redeem → **503**,
  `detail.reason=chat_pair_auth_required`. Код не создаётся.
- Требование к тестам (F3′): `_is_open_mode()` истинен при пустом
  `HUB_TOKENS`, поэтому chat-pair тесты поднимают хаб с принципалами или
  `HAIPLANE_HUB_TOKENS`. Иначе все AC кроме AC-13 получат 503 вместо своих
  кодов, а сам AC-13 пройдёт вхолостую. Open mode — отдельная фикстура.

### CSRF / start

- Bearer human → CSRF не нужен.
- Cookie session → обязателен валидный CSRF (header или form field, как
  остальные web POST). Без CSRF → 403, код не создаётся (иначе чужой
  сайт сжигает предыдущий код оператора).

### Revoke

- `POST /api/auth/chat-pair/revoke`: помечает `revoked_at` на всех
  не-revoked сессиях caller `principal_id`.
- Принимают: cookie+CSRF, human Bearer **и сам chat-pair токен** (F5′) —
  «закончил в метро, погасил канал» не должно требовать ноутбука. Это
  единственный маршрут в allowlist, который сессия вызывает про себя.
- Не трогает `browser_sessions` и `api_keys`.

### Reaper

- В `_session_reaper` (`hub/poller.py`): DELETE из `chat_pair_codes` где
  `expires_at < now` OR `redeemed_at IS NOT NULL` старше N часов; DELETE
  из `chat_pair_sessions` где `expires_at < now` OR `revoked_at IS NOT NULL`.
- Тот же часовой цикл, что browser sessions.

### Whoami

- `auth_source` публично = `chat_pair` (через `_public_auth_source`, без
  сворачивания в другое имя).
- `permissions_summary` = узкий set; `api_key_id` = null.

### Агент после redeem

1. `GET /api/whoami` — проверить `auth_source=chat_pair`.
2. `POST /api/tasks` — создать задачу.
3. Токен в чат / updates / PR description не писать.

Паттерны: `hub/services/admin.py` (`hash_api_key`, `create_browser_session`,
`revoke_browser_session`), `hub/auth.py` (`_PUBLIC_PATHS`,
`LoginRateLimiter`, `require_human_or_admin`), `hub/poller.py`
(`_session_reaper`), `tests/test_auth.py`.

---

## Acceptance criteria

Каждый `verifiable_by: test` обязан иметь локатор. Тесты пишутся в
`tests/test_chat_pair.py` до кода (TDD). `expectation_source: requirement`.

### AC-1 — start только для human

- **Given** запрос с агентским Bearer
- **When** `POST /api/auth/chat-pair/start`
- **Then** HTTP 403, `detail.reason=human_only_gate`; строка кода в БД не
  создаётся
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_start_rejects_agent_token`

### AC-2 — start выдаёт код, в БД только hash

- **Given** залогиненный human (Bearer) и
  `HAIPLANE_CHAT_PAIR_CODE_SECONDS=300`
- **When** `POST /api/auth/chat-pair/start`
- **Then** 200; `expires_in_sec` равен текущему конфигу (300); `code` после
  `normalize_pair_code` — ровно 8 символов алфавита; в таблице нет plaintext;
  `code_hash` совпадает с hash нормализованного кода
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_start_returns_code_stores_hash_only`

### AC-2b — start с cookie требует CSRF

- **Given** human cookie-сессия без CSRF (или с неверным CSRF)
- **When** `POST /api/auth/chat-pair/start`
- **Then** 403; код не создаётся
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_start_cookie_requires_csrf`

### AC-3 — повторный start сжигает предыдущий код

- **Given** human уже получил код A, он не redeem
- **When** тот же human делает второй start и получает код B
- **Then** redeem(A) → 401 `chat_pair_invalid`; redeem(B) → 200
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_second_start_burns_unused_code`

### AC-4 — redeem один раз даёт узкую сессию того же principal

- **Given** валидный неиспользованный код, выданный принципалу P (в т.ч. если
  P — admin)
- **When** `POST /api/auth/chat-pair/redeem` с этим кодом без Authorization
  (допустимы формы `AH-…` и без префикса)
- **Then** 200, поля `token`, `expires_at`, `username`, `role=human`,
  `base_url`, `permissions` = ровно
  `{tasks.read, tasks.create, tasks.refine, tasks.update}`;
  `GET /api/whoami` с token: тот же `principal_id`, что у P,
  `auth_source=chat_pair`, в `permissions_summary` нет `tasks.human_gate`,
  `tasks.decision`, `admin.read`
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_redeem_issues_session_for_same_principal`

### AC-5 — повтор / чужой / просроченный код неотличимы

- **Given** уже redeem-нутый код, неизвестная строка и код с истекшим TTL
- **When** каждый из трёх уходит в redeem
- **Then** все три: HTTP 401, один и тот же `detail.reason=chat_pair_invalid`;
  новый token не выдаётся
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_redeem_invalid_cases_are_indistinguishable`

### AC-6 — отдельный rate limit redeem → 429

- **Given** с одного IP больше
  `HAIPLANE_CHAT_PAIR_REDEEM_MAX` неуспешных redeem за окно
- **When** следующий redeem
- **Then** HTTP 429, `detail.reason=chat_pair_rate_limited`; тот же IP всё
  ещё может открыть `/login` (login_limiter не затронут); после снятия
  окна валидный не истёкший код всё ещё redeem-ится
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_redeem_rate_limited_per_ip_separate_from_login`

### AC-7 — сессия ставит задачу как human (open)

- **Given** token после успешного redeem
- **When** `POST /api/tasks` с этим Bearer, `title` непустой, **без**
  `client_request_id`
- **Then** HTTP 200, задача `status=open`, `source` human (не draft агента);
  с `client_request_id` на первом создании — 201, на повторе того же ключа —
  200 и тот же `id`
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_chat_pair_token_creates_open_task`

### AC-7b — сессия не проходит human gates и admin

- **Given** token после redeem от **admin**-принципала
- **When** `POST /api/tasks/{id}/approve` и `GET /api/admin/summary`
- **Then** оба: HTTP 403, `detail.reason=chat_pair_gate_forbidden`
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_chat_pair_token_forbidden_on_human_gate_and_admin`

### AC-7c — allowlist закрывает ветки, читающие сессию как человека

- **Given** token после redeem
- **When** по очереди: `POST /api/tasks/{id}/review-verdict` с verdict
  `approved`; `POST /api/tasks/{id}/pair-start`; `POST /api/projects`;
  `POST /api/tasks/{id}/updates`
- **Then** каждый: HTTP 403, `detail.reason=chat_pair_gate_forbidden`;
  вердикт в БД не записан, статус задачи не изменился, проект не создан
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_chat_pair_allowlist_blocks_human_branch_routes`

### AC-7d — deny-by-default для неизвестного маршрута и /mcp

- **Given** token после redeem
- **When** запрос на любой аутентифицированный маршрут вне allowlist,
  который не перечислен в AC-7b/AC-7c (напр. `GET /api/tasks/{id}/log`),
  и MCP `initialize` на `/mcp` с этим же Bearer
- **Then** оба: HTTP 403, `detail.reason=chat_pair_gate_forbidden`;
  каталог MCP-инструментов не выдан
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_chat_pair_denies_unlisted_route`

### AC-7e — allowlist пропускает свои маршруты

- **Given** token после redeem
- **When** `GET /api/whoami`, `GET /api/tasks`, `GET /api/tasks/{id}`,
  `POST /api/tasks/{id}/refine`, `GET /api/tasks/{id}/readiness`
- **Then** ни один не 403; refine меняет только переданные поля
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_chat_pair_allowlist_permits_task_authoring`

### AC-7f — create не запускает исполнение и не выключает ревью

- **Given** token после redeem
- **When** `POST /api/tasks` с `run_immediately=true`; затем отдельный
  запрос с `auto_review=false`
- **Then** оба: HTTP 422, `detail.reason=chat_pair_run_forbidden`; задача
  не создана, `dispatch_task` не вызван (нет job, нет ветки); тот же
  payload без этих полей → задача создаётся в `open`
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_chat_pair_create_rejects_run_and_review_optout`

### AC-8 — TTL сессии

- **Given** redeem при `HAIPLANE_CHAT_PAIR_TTL_SECONDS=1`
- **When** после истечения TTL `GET /api/whoami` с тем token
- **Then** 401; `POST /api/tasks` тоже 401
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_chat_pair_session_expires`

### AC-9 — отзыв чат-сессий

- **Given** живой chat-pair token принципала P и живые cookie-сессия + API
  key того же P
- **When** P вызывает `POST /api/auth/chat-pair/revoke` (Bearer human ноута
  или cookie+CSRF)
- **Then** chat-pair token → 401; cookie whoami и API-key whoami → 200
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_revoke_chat_pair_does_not_kill_other_auth`

### AC-9b — сессия гасит сама себя

- **Given** живой chat-pair token
- **When** этим же token вызывается `POST /api/auth/chat-pair/revoke`
- **Then** 200; следующий запрос тем же token → 401
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_chat_pair_token_can_revoke_itself`

### AC-10 — секрет не в аудите

- **Given** успешные start, redeem и revoke
- **When** читаем `audit` / `activity_log` этих действий
- **Then** нет plaintext `code` и нет `token`; есть `principal_id` и
  `auth_source=chat_pair` (где применимо)
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_audit_omits_code_and_token`

### AC-11 — Web UI выдаёт тот же start

- **Given** human в Web UI с валидным CSRF
- **When** отправляет форму «Подключить чат Cursor»
- **Then** страница показывает код того же формата, что API start; без CSRF
  — отказ; агентский токен форму не открывает (403 / redirect login)
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_web_pair_form_issues_code`

### AC-12 — миграция свежей и старой БД

- **Given** пустая БД и БД до этой миграции
- **When** прогон миграций
- **Then** таблицы `chat_pair_codes` и `chat_pair_sessions` существуют;
  старые тесты auth/admin зелёные
- **verifiable_by:** `test`
- **test_ref:** `tests/test_db_migrations.py::test_chat_pair_tables_exist`

### AC-13 — open mode отключён

- **Given** отдельная фикстура: хаб в open mode (`HUB_TOKENS` пуст или
  `HAIPLANE_HUB_AUTH_DISABLED`), при этом остальные chat-pair тесты идут на
  фикстуре с настроенными принципалами/токенами
- **When** start или redeem
- **Then** HTTP 503, `detail.reason=chat_pair_auth_required`; код не создаётся
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_open_mode_refuses_chat_pair`

### AC-14 — reaper чистит просроченное

- **Given** просроченный код, redeemed-код старше порога и revoked/expired
  session в БД
- **When** отрабатывает `_session_reaper` (или вызываемая из него чистка)
- **Then** эти строки удалены; живой код и живая сессия остаются
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_reaper_purges_expired_pair_rows`

### AC-15 — ошибки проходят общую обвязку

- **Given** отказы `chat_pair_gate_forbidden`, `chat_pair_invalid`,
  `chat_pair_rate_limited`, `chat_pair_auth_required`
- **When** читаем тело ответа
- **Then** каждое прошло `enrich_error_payload` (есть поля конверта) и имеет
  непустой `next_action` из `compute_next_action` — не голый `reason`
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_chat_pair_errors_are_actionable`

---

## Risks

| kind | severity | description | mitigation |
|---|---|---|---|
| `security` | `high` | Код в транскрипте Cursor → кража сессии | TTL кода 5 мин, burn-after-read, одинаковая 401, отдельный rate limit, сессия ≤ 2 ч, revoke, hash в БД, **узкие permissions** |
| `security` | `high` | Утечка кода даёт human-gate/admin | Allowlist deny-by-default в `AuthMiddleware`; AC-7b, AC-7c, AC-7d |
| `security` | `high` | Сессия читается как человек там, где гейта нет (`review-verdict`, `pair-start`, проекты, треды) | Тот же allowlist: маршрут закрыт до того, как ветка `is_agent` вообще исполнится; AC-7c |
| `security` | `high` | Разрешённый `POST /api/tasks` несёт `run_immediately` → headless dispatch с утёкшего кода | 422 `chat_pair_run_forbidden` на `run_immediately`/`auto_review=false`; AC-7f |
| `security` | `high` | Агент печатает token в чат | Token только в JSON redeem; guide запрещает эхо |
| `security` | `medium` | Перебор кодов на публичном redeem | ~40 бит + 5 мин + отдельный limiter → 429 |
| `security` | `medium` | CSRF-less cookie start сжигает чужой код | Start: Bearer или cookie+CSRF only (AC-2b) |
| `breaking_change` | `low` | Проверка в `AuthMiddleware` лежит на пути каждого запроса | Ветка активна только при `auth_source=chat_pair`; для остальных источников поведение не меняется; регрессия — `tests/test_auth.py` |
| `ambiguous_requirements` | `medium` | Allowlist окажется тесен (не хватит маршрута для нормальной постановки) | Расширение — решение с обоснованием в задаче; AC-7e фиксирует минимальный рабочий набор |
| `external_dependency` | `low` | Egress Cursor cloud до `agenthai.ru` | Уже есть у review dispatch |
| `unknown_unknowns` | `medium` | Cursor сохранит тело redeem в tool-log | Короткий TTL; revoke; не admin-токен в канале |
| `ambiguous_requirements` | `low` | Путают с MCP-токеном ноута | Scope out + guide: в чат только код с кнопки |

Класс риска: **R2** (короткая сессия с узкими правами). `kind=security`
на любой severity → deep review — ожидаемо.

---

## Validation commands

```text
uv run ruff check hub tests
uv run ruff format --check hub tests
uv run pytest -q tests/test_chat_pair.py tests/test_db_migrations.py tests/test_auth.py
uv run python scripts/surface_parity.py
```

Полный `uv run pytest -q` перед сдачей.

MCP catalog budget не гонять: tools/list не меняется.

В тексте сабмишена к `surface_parity`: pairing — auth-канал (REST only);
CLI/MCP surfaces не применимы.

---

## Review checklist

- Публичный только redeem; start — human + (Bearer | cookie+CSRF).
- Агент не получает код (`human_only_gate`).
- Allowlist — deny-by-default; компилированные регексы из пар метод+путь,
  не префиксы строк; пути сверены с `hub/app.py` (подчёркивание в
  `acceptance_criteria`).
- `review-verdict`, `pair-start`, `claim`, `projects`, `skills`, `updates`,
  треды, web-маршруты и `/mcp` закрыты (AC-7c, AC-7d).
- Create: `run_immediately` / `auto_review=false` → 422 (AC-7f); dispatch
  из create недостижим для chat-pair.
- Ни один гейт не решает по `role` chat-pair сессии.
- Permissions фиксированы; admin-принципал не протекает в сессию.
- Rate limit redeem отделён от `login_limiter`; ответ 429.
- AC-5: три инвалидных случая неотличимы.
- AC-7: `open`, не `draft`; статусы 200/201 как у idempotency.
- AC-9/9b: revoke-ручка есть; cookie и API keys живы; сессия гасит себя.
- AC-13: open mode → 503; фикстуры остальных тестов не в open mode.
- AC-14: reaper чистит таблицы.
- AC-15: новые reason проходят `enrich_error_payload`.
- AC-10: секреты не в audit.
- Нет правок `hub/mcp_server.py` tool surface.
- Миграция в `hub/db.py`.

`out_of_scope_for_review`:

- UX копирования кода в буфер (достаточно показать код).
- Красота мобильной вёрстки сверх читаемого кода и таймера.

---

## Files (expected)

| Слой | Файлы |
|---|---|
| Контракт | `hub/models.py` — `ChatPairStartView`, `ChatPairRedeem`, `ChatPairRedeemed` |
| Схема | `hub/db.py` — `chat_pair_codes`, `chat_pair_sessions` |
| Сервис | `hub/services/chat_pair.py` |
| Auth | `hub/auth.py` — public path, resolve, limiters, allowlist в middleware |
| Ошибки | `hub/actionable_errors.py`, `hub/mcp_envelope.py` — новые reason |
| REST | `hub/app.py` — start, redeem, revoke |
| Poller | `hub/poller.py` — reaper |
| Web | `hub/web.py`, шаблон pairing |
| Конфиг | `hub/config.py` — TTL, redeem max/window, `_CHAT_PAIR_PERMS` |
| Docs | `docs/agent-mcp-operator-guide.md`, `docs/agent-context/change-map.md` |
| Тесты | `tests/test_chat_pair.py`, миграции, точечно `tests/test_auth.py` |

После появления id задачи в хабе: переложить этот файл в
`docs/issues/task-<id>-chat-pair.md` (как `docs/issues/task-237-…`).

---

## Test plan

Автотесты = все AC (1…15, включая суффиксные 2b, 7b–7f, 9b).

Ручной прогон после CI (не блокирует DoR):

1. Safari → хаб → «Подключить чат» → код.
2. Облачный чат Cursor: `хаб: <code>`.
3. Агент: redeem, `whoami` (узкие permissions), `POST /api/tasks`.
4. В хабе задача `open`.
5. Агент пробует approve и review-verdict — оба 403
   `chat_pair_gate_forbidden`.
6. Повтор кода — 401.
7. «Отозвать чаты» в UI — старый token мёртв; Safari-логин жив.

---

## YAML для refine в хаб

```yaml
work_type: feature
class_of_service: standard
size: M
wip_tag: feature_work
redesign_decision: redesign
redesign_rationale: >
  Cursor Agents не принимает custom MCP. Новый канал: одноразовый код →
  короткая сессия с узкими правами → REST. Доступ описан положительным
  allowlist маршрутов: идентичность в хабе бинарная, и любая ветка
  "не агент → значит человек" иначе отдаёт сессии человеческий путь.
agent_fit: sdd_native
user_story: |
  As an operator in the Cursor app, I want to paste a short hub pairing
  code and create a task, so I do not need custom MCP on web/agents or a laptop.
problem_statement: |
  Phone/cloud Cursor cannot attach Hub MCP. A long-lived token in chat
  survives in the transcript. A full human/admin session from that channel
  would expose approve/decide/admin. Existing API keys expire in days.
business_value: |
  Create an open hub task from a Cursor cloud/phone chat without MCP,
  attributing the issuer principal while limiting the session to
  create/read/refine/update only.
outcome_metric: Task created in open from a chat-pair session without gate rights
outcome_indicator: AC-4, AC-7, AC-7b, AC-7c pass; second redeem is 401
scope_in:
  - one-time pairing code for a logged-in human (Bearer or cookie+CSRF)
  - public redeem to a short-lived Bearer with fixed narrow permissions
  - deny-by-default route allowlist for auth_source=chat_pair
  - separate redeem rate limiter (429), revoke endpoint (incl. self), reaper
  - open-mode refusal, web button, docs
scope_out:
  - Cursor dashboard MCP
  - cloud implementer dispatch
  - new MCP tools
  - CLI pairing
  - git/branch/phone merge
affected_areas:
  - hub/auth.py
  - hub/app.py
  - hub/services/chat_pair.py
  - hub/db.py
  - hub/poller.py
  - hub/web.py
  - hub/actionable_errors.py
  - hub/mcp_envelope.py
  - tests/test_chat_pair.py
validation_commands:
  - uv run ruff check hub tests
  - uv run ruff format --check hub tests
  - uv run pytest -q tests/test_chat_pair.py tests/test_db_migrations.py tests/test_auth.py
  - uv run python scripts/surface_parity.py
constraints:
  - secrets never in logs or audit plaintext
  - redeem public; start human-only with CSRF when cookie
  - chat_pair access is a deny-by-default route allowlist (compiled method+path regexes)
  - create rejects run_immediately and auto_review=false with 422
  - /mcp is explicitly closed for chat_pair
  - no gate may decide on the chat_pair role; it is presentational
  - chat_pair permissions fixed; never inherit admin
  - redeem limiter must not be login_limiter
  - do not extend api_keys expires_days for this
assumptions:
  - Cursor cloud VMs can reach https://agenthai.ru
  - operator can open Hub Web UI on the phone
  - production auth is enabled (not open mode)
  - chat-pair tests run with principals configured, not in open mode
```

---

## Changelog (rev. 4)

| Finding | Что сделано |
|---|---|
| G1 | `POST /api/tasks` от chat-pair отклоняет `run_immediately=true` и `auto_review=false` (422 `chat_pair_run_forbidden`) — dispatch из create недостижим; AC-7f |
| G2 | Allowlist переписан точными парами метод+путь; `acceptance_criteria` с подчёркиванием, все четыре метода включены осознанно |
| G3 | Механика матчинга: компилированные из шаблонов регексы на старте, middleware сверяет (method, path); префиксы запрещены |
| G4 | `/mcp` закрыт явно, включён в AC-7d |
| G5 | `hub/mcp_envelope.py` добавлен в YAML `affected_areas` |

## Changelog (rev. 3)

| Finding | Что сделано |
|---|---|
| F1′ | Allowlist маршрутов deny-by-default в `AuthMiddleware` вместо патча `require_human_or_admin`/`require_admin`; таблица закрытых веток (`review-verdict`, `pair-start`, projects, skills, updates, треды, web); AC-7c, AC-7d, AC-7e |
| F2′ | `role` объявлена презентационной; запрет решать по ней вынесен в Constraints |
| F3′ | Требование к фикстурам: chat-pair тесты не в open mode; AC-13 на отдельной фикстуре |
| F4′ | Новые reason проходят `enrich_error_payload` / `compute_next_action`; AC-15; `hub/actionable_errors.py` в affected_areas |
| F5′ | Сессия может отозвать сама себя; AC-9b |

## Changelog (rev. 2)

| Finding | Что сделано |
|---|---|
| F1 | Узкий `_CHAT_PAIR_PERMS`; gate refuse `chat_pair_gate_forbidden`; AC-7b |
| F2 | Отдельный `chat_pair_limiter`; 429 как решение API; AC-6 уточнён |
| F3 | `POST /api/auth/chat-pair/revoke` в scope и Files; AC-9 реализуем |
| F4 | Reaper в `hub/poller.py`; AC-14; poller в affected_areas |
| F5 | Open mode → 503 `chat_pair_auth_required`; AC-13 |
| F6 | Start: Bearer \| cookie+CSRF; AC-2b |
| F7 | AC-1: только `human_only_gate` |
| F8 | 8 символов + нормализация; `expires_in_sec` из конфига |
| F9 | AC-7: 200 без idempotency key; 201/200 с `client_request_id` |

---

## Зависимости

Нет. Не базировать на невлитой ветке ревью.

Не блокирует и не заменяется путём C (хаб → Cloud API).
