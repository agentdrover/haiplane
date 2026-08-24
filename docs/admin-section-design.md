# Дизайн раздела администрирования

## Контекст

Текущая auth-модель уже сдвинулась от single-user режима к multi-user MVP:

- токены задаются через `HAIPLANE_HUB_TOKENS`;
- формат токена поддерживает роль: `name:token[:role]`;
- роли: `human`, `agent`, `admin`;
- `AuthMiddleware` кладет `request.state.identity`;
- human-only операции защищаются через `require_human_or_admin`;
- есть `require_admin`, но полноценного admin UI/API пока нет;
- browser login хранит bearer token в cookie;
- пользователи, агенты, пароли, ключи, роли и аудит пока не живут в БД.

Следующий шаг - вынести управление идентичностями из env-only конфигурации в DB-backed admin section, оставив env-токены как bootstrap/fallback.

## Цели

Раздел администрирования должен позволять администраторам:

- управлять администраторами и пользователями;
- управлять AI-агентами как отдельными identity, а не как строковыми `agent` именами;
- назначать роли и разрешения;
- выпускать, ротировать и отзывать API/MCP ключи;
- управлять паролями людей без хранения plaintext;
- видеть audit trail по чувствительным действиям;
- безопасно отключать доступ без перезапуска хаба.

## Не цели MVP

- OAuth/OIDC/SAML;
- multi-tenant organization model;
- fine-grained policy language;
- внешний secrets manager как обязательная зависимость;
- self-service registration.

Эти возможности можно добавить позже, если DB-backed auth и audit уже спроектированы правильно.

## Основные сущности

### Principal

Единая identity для человека, AI-агента или service account.

Поля:

- `id`;
- `kind`: `human | agent | service`;
- `username`: стабильный slug;
- `display_name`;
- `email`;
- `status`: `active | disabled | locked`;
- `created_at`, `updated_at`;
- `created_by`;
- `last_seen_at`;
- `notes`.

Правило: tasks могут продолжать хранить `human_owner`, `human_reviewer`, `assigned_agent` как строки для обратной совместимости, но UI должен предлагать значения из `principals`.

### Role

Роль - набор permissions.

Системные роли:

- `super_admin`: полный доступ, bootstrap-only, нельзя удалить;
- `admin`: управление пользователями, агентами, ключами, настройками;
- `operator`: human gates, запуск задач, decision gate, force-complete;
- `developer`: создание и ведение задач без admin-доступа;
- `viewer`: read-only dashboard/tasks;
- `agent`: agent-scoped workflow: propose, update, question, report done;
- `reviewer_agent`: agent + review/report permissions;
- `security_admin`: доступ к audit/security settings без полного управления задачами.

### Permission

Permissions должны быть строковыми и стабильными.

Минимальный набор:

- `admin.read`;
- `admin.users.write`;
- `admin.agents.write`;
- `admin.roles.write`;
- `admin.credentials.write`;
- `admin.audit.read`;
- `tasks.read`;
- `tasks.create`;
- `tasks.refine`;
- `tasks.update`;
- `tasks.human_gate`;
- `tasks.agent_report`;
- `tasks.decision`;
- `integrations.vast.manage`;
- `system.settings.write`.

В коде удобно начать со статического registry permissions в `hub/authz.py`, а в БД хранить `role_permissions`.

### Credential

Credentials делятся на пароли, API keys и browser sessions.

#### Password credential

Для human principals.

Поля:

- `principal_id`;
- `password_hash`;
- `hash_algorithm`: например `argon2id`;
- `password_changed_at`;
- `must_rotate`;
- `failed_attempts`;
- `locked_until`;
- `last_login_at`.

Правила:

- plaintext password никогда не хранится;
- password reset создает one-time reset token;
- reset token хранится только как hash;
- при смене пароля можно отзывать все browser sessions.

#### API key

Для CLI, MCP, AI agents и service accounts.

Поля:

- `id`;
- `principal_id`;
- `name`;
- `key_prefix`: первые 8-12 символов для отображения;
- `key_hash`;
- `scopes` или role binding snapshot;
- `expires_at`;
- `last_used_at`;
- `revoked_at`;
- `created_at`;
- `created_by`.

Правила:

- plaintext key показывается один раз при создании;
- в БД хранится только hash;
- key можно отозвать без удаления principal;
- agent keys должны иметь короткий срок жизни по умолчанию;
- ключи для Cursor/MCP должны иметь минимальную роль `agent`, если им не нужны human gates.

#### Browser session

MVP сейчас кладет bearer token в cookie. Production-grade admin section должен заменить это на opaque session id.

Поля:

- `id`;
- `principal_id`;
- `session_hash`;
- `created_at`;
- `last_seen_at`;
- `expires_at`;
- `revoked_at`;
- `ip_hash`;
- `user_agent`.

Cookie хранит только opaque session token, а не API key.

## Предлагаемая схема БД

Новые таблицы:

```sql
principals(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  username TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  notes TEXT NOT NULL DEFAULT '',
  created_by INTEGER REFERENCES principals(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen_at TEXT
);

roles(
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  system INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

principal_roles(
  principal_id INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  granted_by INTEGER REFERENCES principals(id),
  granted_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (principal_id, role_id)
);

role_permissions(
  role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  permission TEXT NOT NULL,
  PRIMARY KEY (role_id, permission)
);

password_credentials(
  principal_id INTEGER PRIMARY KEY REFERENCES principals(id) ON DELETE CASCADE,
  password_hash TEXT NOT NULL,
  hash_algorithm TEXT NOT NULL DEFAULT 'argon2id',
  password_changed_at TEXT NOT NULL DEFAULT (datetime('now')),
  must_rotate INTEGER NOT NULL DEFAULT 0,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  last_login_at TEXT
);

api_keys(
  id INTEGER PRIMARY KEY,
  principal_id INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  key_prefix TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,
  scopes TEXT NOT NULL DEFAULT '[]',
  expires_at TEXT,
  last_used_at TEXT,
  revoked_at TEXT,
  created_by INTEGER REFERENCES principals(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

browser_sessions(
  id INTEGER PRIMARY KEY,
  principal_id INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  session_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  last_seen_at TEXT,
  revoked_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  ip_hash TEXT NOT NULL DEFAULT '',
  user_agent TEXT NOT NULL DEFAULT ''
);

admin_audit_log(
  id INTEGER PRIMARY KEY,
  actor_principal_id INTEGER REFERENCES principals(id),
  action TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL,
  detail TEXT,
  ip_hash TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Индексы:

- `idx_principals_kind_status`;
- `idx_api_keys_principal_id`;
- `idx_api_keys_prefix`;
- `idx_browser_sessions_principal_id`;
- `idx_admin_audit_actor`;
- `idx_admin_audit_target`.

## Bootstrap

Нельзя зависеть от уже существующего admin user, если БД пустая.

Правила:

1. Если нет ни одного active `super_admin` / `admin`, хаб находится в `admin_bootstrap_required`.
2. Bootstrap допускается только:
   - через `HAIPLANE_HUB_BOOTSTRAP_ADMIN_TOKEN`; или
   - через локальный CLI `oc-hub admin bootstrap`, выполняемый на машине сервера.
3. Bootstrap создает первого `super_admin`, password credential и optional API key.
4. После успешного bootstrap токен считается использованным; его нельзя оставлять постоянным credential.

Env tokens из `HAIPLANE_HUB_TOKENS`:

- сохранить как compatibility fallback на один переходный релиз;
- помечать в UI как `external/env credential`;
- не позволять управлять ими из UI;
- рекомендовать миграцию в DB credentials.

## Auth flow

### API / MCP / CLI

1. Проверить `Authorization: Bearer <key>`.
2. Hash key, найти active `api_keys`.
3. Проверить principal status.
4. Собрать roles + permissions.
5. Положить в `request.state.identity`.
6. Обновить `last_used_at` асинхронно или best-effort.

Fallback: если key не найден в DB, проверить `HAIPLANE_HUB_TOKENS` (env-токены).

### Browser

1. Login принимает username + password.
2. Проверяет password credential.
3. Создает opaque browser session.
4. Cookie хранит session token.
5. Middleware ищет session в `browser_sessions`.

Login по bearer token можно временно оставить как legacy path, но admin design должен считать его deprecated.

## Разделы UI

Все страницы под `/admin/*` доступны только permission `admin.read`, write actions требуют конкретных permissions.

### `/admin`

Сводка:

- количество active/disabled users;
- количество active agents;
- ключи, истекающие в ближайшие 7/30 дней;
- locked users;
- последние admin audit events;
- предупреждения: env tokens enabled, auth disabled, non-secure cookie.

### `/admin/users`

Список human principals:

- username, display name, email;
- roles;
- status;
- last seen;
- actions: create, disable, lock/unlock, reset password, rotate keys.

### `/admin/users/{id}`

Карточка пользователя:

- профиль;
- роли;
- API keys;
- browser sessions;
- activity/audit;
- task ownership links: owned/reviewed tasks.

### `/admin/agents`

Список AI agents:

- agent slug;
- role/scopes;
- active keys;
- last used;
- assigned/running tasks;
- transcript link, если доступен;
- actions: create key, revoke key, disable agent.

### `/admin/roles`

Управление ролями:

- системные роли read-only или partially locked;
- custom roles;
- permission matrix.

### `/admin/keys`

Глобальный вид ключей:

- owner principal;
- prefix;
- name;
- scopes;
- expires;
- last used;
- revoked;
- actions: revoke, rotate.

Plaintext key никогда не показывать после создания.

### `/admin/audit`

Фильтруемый audit:

- actor;
- action;
- target;
- date range;
- result.

Audit должен покрывать:

- создание/изменение/отключение principal;
- назначение/снятие role;
- создание/revoke API key;
- password reset;
- session revoke;
- bootstrap;
- auth setting changes.

## REST API

Минимальные endpoints:

```text
GET    /api/admin/summary
GET    /api/admin/principals
POST   /api/admin/principals
GET    /api/admin/principals/{id}
PATCH  /api/admin/principals/{id}
POST   /api/admin/principals/{id}/disable
POST   /api/admin/principals/{id}/enable

GET    /api/admin/roles
POST   /api/admin/roles
PATCH  /api/admin/roles/{id}
PUT    /api/admin/roles/{id}/permissions
PUT    /api/admin/principals/{id}/roles

POST   /api/admin/principals/{id}/password
POST   /api/admin/principals/{id}/password-reset

GET    /api/admin/api-keys
POST   /api/admin/principals/{id}/api-keys
POST   /api/admin/api-keys/{id}/revoke

GET    /api/admin/sessions
POST   /api/admin/sessions/{id}/revoke

GET    /api/admin/audit
```

Все write endpoints:

- требуют admin permission;
- пишут `admin_audit_log`;
- возвращают 403 до проверки существования target, если actor не имеет прав.

## CLI

CLI нужен для bootstrap и аварийного управления.

Команды:

```bash
oc-hub admin bootstrap --username alice --password-prompt
oc-hub admin users list
oc-hub admin users create --username bob --email bob@example.com --role operator
oc-hub admin users disable bob
oc-hub admin agents create --name cursor-dev
oc-hub admin keys create --principal cursor-dev --name "Cursor MCP" --expires-days 30
oc-hub admin keys revoke <key-id>
oc-hub admin roles list
oc-hub admin audit --limit 50
```

CLI должен поддерживать `HAIPLANE_HUB_TOKEN` и не печатать plaintext secret повторно.

## MCP

Администрирование через MCP опасно: агент с admin key может сам повышать доступ.

MVP:

- не добавлять write admin MCP tools;
- можно добавить read-only `hub_admin_my_identity` для диагностики current identity/permissions;
- создание/ротация ключей - через Web UI или CLI.

Если позже нужны MCP admin tools, они должны требовать отдельную permission вроде `admin.mcp.write` и быть выключены по умолчанию.

## Интеграция с задачами

Admin identities должны улучшить существующие поля:

- `human_owner`: выбирать из active human principals;
- `human_reviewer`: выбирать из active human principals;
- `assigned_agent`: выбирать из active agent principals;
- agent token username должен совпадать с `assigned_agent` или иметь permission на shared agent pool.

Это можно вводить постепенно: сначала UI suggestions, затем strict validation.

## Безопасность

Обязательные правила:

- Hash passwords через Argon2id или эквивалентный password hashing API.
- Hash API keys и session tokens; plaintext показывать только один раз.
- Использовать constant-time compare для hashes.
- Cookie: `HttpOnly`, `SameSite=Lax`, `Secure` при TLS.
- Lockout/rate-limit для password login.
- Short-lived agent keys by default.
- Admin audit immutable на уровне приложения: нет update/delete endpoints.
- Нельзя удалить последнего active admin/super_admin.
- Нельзя снять admin role с самого себя, если это последний admin.

## Файлы для реализации

Ожидаемый impact:

- `hub/models.py`: admin request/response models, enums;
- `hub/db.py`: migrations для admin tables;
- `hub/repository.py`: SQL helpers;
- `hub/services/admin.py`: business logic, audit, bootstrap;
- `hub/auth.py`: DB-backed identity resolution, permission helpers;
- `hub/config.py`: bootstrap/env compatibility settings;
- `hub/app.py`: `/api/admin/*`;
- `hub/web.py`: `/admin/*`;
- `hub/templates/admin/*.html`;
- `hub/static/style.css`: restrained admin UI;
- `hub/cli.py`: `oc-hub admin ...`;
- `tests/test_admin_api.py`;
- `tests/test_admin_services.py`;
- `tests/test_auth.py`;
- `tests/test_cli.py`;
- `tests/test_web.py`;
- `tests/test_db_migrations.py`.

## Поэтапная реализация

### Этап A. DB-backed identity foundation

- Добавить таблицы `principals`, `roles`, `principal_roles`, `role_permissions`, `admin_audit_log`.
- Seed системных ролей.
- Добавить `hub/services/admin.py`.
- Bootstrap первого admin.
- Auth пока продолжает принимать env tokens.

Валидация:

```bash
uv run pytest tests/test_db_migrations.py tests/test_auth.py -q
```

### Этап B. API keys and permission checks

- Добавить `api_keys`.
- Перевести bearer auth на DB keys + env fallback.
- Добавить permission-based dependencies: `require_permission("...")`.
- Покрыть role boundaries тестами.

Валидация:

```bash
uv run pytest tests/test_auth.py tests/test_admin_api.py -q
```

### Этап C. Browser users and passwords

- Добавить password credentials и browser sessions.
- Заменить token-cookie login на username/password login.
- Добавить password reset / must rotate.
- Оставить legacy token login временно, если нужно для миграции.

Валидация:

```bash
uv run pytest tests/test_auth.py tests/test_web.py -q
```

### Этап D. Admin UI

- Реализовать `/admin`, users, agents, roles, keys, audit.
- Добавить sidebar link только для admin.
- Все write actions через POST/HTMX с audit.

Валидация:

```bash
uv run pytest tests/test_web.py tests/test_admin_api.py -q
```

### Этап E. CLI bootstrap and emergency admin

- Добавить `oc-hub admin bootstrap`.
- Добавить users/agents/keys/audit read/write команды.
- Не добавлять MCP write tools.

Валидация:

```bash
uv run pytest tests/test_cli.py tests/test_admin_api.py -q
```

## Definition of Done

- Первый admin создается без ручной правки БД.
- Admin может создать human user, agent identity и API key.
- Agent key не может выполнять human/admin gates.
- Human/operator может выполнять human gates, но не admin writes.
- Admin может отозвать key/session без рестарта хаба.
- Login не хранит bearer token в cookie.
- Все admin writes отражаются в `admin_audit_log`.
- Последнего admin нельзя отключить или лишить роли.
- Env-token auth documented as compatibility mode, not primary admin model.
