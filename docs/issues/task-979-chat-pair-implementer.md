# SDD: Chat-pair kind=implementer — агентская сессия на одну open-задачу

> Реализовано в хабе: T-kind **#980**, T-ui **#981**, T-docs **#982**,
> T-remote **#975**, T-session **#977**. Этот документ — полная SDD
> (T-kind-spec **#979**): схема, контракт start, allowlist один-в-один,
> закрытые маршруты, AC с локаторами на уже зелёные тесты.
>
> Outline пакета: [`chat-pair-implementer-path.md`](chat-pair-implementer-path.md).
> Intake **не** этот канал: [`task-961-chat-pair.md`](task-961-chat-pair.md).
>
> **Не реализация.** Рантайм и правки allowlist вне скоупа #979.

---

## Мета

| Поле | Значение |
|---|---|
| `work_type` | `docs` (спека); рантайм уже `feature` #980 |
| `class_of_service` | `standard` |
| `size` | `M` |
| `task_type` | `task` |
| `agent_fit` | `sdd_native` |

---

## User story

Как оператор, я хочу выдать одноразовый код с карточки **уже одобренной**
open-задачи, чтобы облачный агент в Cursor без MCP мог claim / pair-start
(`git_mode=remote`) / submit-for-review **только этой** задачи, а approve и
review-verdict оставались человеком.

---

## Problem statement

#961 даёт `role=human` и `tasks.create` — постановка, не исполнение.
Флип того канала в агента ломает create, revoke и git на хосте хаба
(ревью подхода A). Нужен **соседний** `kind` на той же машинерии кодов:
`role=agent`, без create, с `bound_task_id`, отдельным allowlist.

---

## Scope in

- Схема `chat_pair_codes` / `chat_pair_sessions`: `kind`, `bound_task_id`,
  `acting_principal_id`.
- Контракт `POST /api/auth/chat-pair/start` с `{kind, task_id}` и CSRF POST
  `/tasks/{id}/web-implementer-start`.
- Allowlist implementer один-в-один с кодом (`CHAT_PAIR_IMPLEMENTER_ALLOWLIST`).
- Таблица закрытых маршрутов (не widening intake).
- Constraints #961: презентационная `role=human` — **только intake**.
- AC с `test_ref` на `tests/test_chat_pair.py` / `tests/test_web.py` /
  `tests/test_docs_links.py`.

## Scope out

- Рантайм-код и изменение allowlist (#980 уже выкатил).
- TTL/renew (#983 — не стартовать до живого прогона на 2h).
- Чек-лист Cloud VM в `docs/software-development-workflow.md` (T-remote).
- Переписывание operator-guide §4a (T-docs #982).
- CSRF-ротация cookie на карточке (#990, draft).

---

## Constraints

- Sibling `kind`, не флип intake. `CHAT_PAIR_ALLOWLIST` **не меняется**.
- `role` intake = `"human"` презентационно; `role` implementer = `"agent"`.
  Гейты смотрят `auth_source` + `chat_pair_kind` + allowlist, не `role`.
- `CHAT_PAIR_IMPLEMENTER_PERMS` = `{tasks.read, tasks.update, tasks.agent_report}`.
  Не `_AGENT_DEFAULT_PERMS` (там `tasks.create`). Нет `tasks.human_gate`.
- Выдача только из `status=open`. Иначе 409 `chat_pair_task_not_open`.
- Нет/неактивен `HAIPLANE_CHAT_PAIR_AGENT` (дефолт `cloud`) → 503
  `chat_pair_agent_missing` на выдаче. Redeem неизвестного кода — 401
  `chat_pair_invalid` (не оракул 503).
- `{task_id}` в пути сверяется с `bound_task_id` как int; нечисловой
  сегмент → 403 `chat_pair_gate_forbidden`, не 500.
- Burn неиспользованных кодов: `(principal_id, kind, bound_task_id)`.
- Revoke scoped by `kind` (и `bound_task_id` для implementer-сессии /
  когда оператор передаёт `task_id`). Intake-кнопка `/chat-pair` гасит
  только intake.
- `/mcp` закрыт. Self-review-verdict закрыт.
- Код и token не в логи / updates / audit plaintext.

---

## Схема

```text
chat_pair_codes
  principal_id          -- issuer (как intake)
  kind                  -- 'intake' | 'implementer', DEFAULT 'intake'
  bound_task_id         -- NULL intake; NOT NULL implementer
  code_hash, expires_at, redeemed_at, created_at

chat_pair_sessions
  principal_id          -- issuer (audit, revoke)
  acting_principal_id   -- NULL intake; cloud-принципал для implementer
  kind, bound_task_id   -- как у кода
  token_hash, expires_at, revoked_at, created_at
```

`TokenIdentity`: `chat_pair_kind`, `chat_pair_task_id`. Whoami **не**
экспортирует эти поля; `kind` / `bound_task_id` — в ответе redeem.

---

## Контракт start

Intake (без тела) без изменений.

```text
POST /api/auth/chat-pair/start
Authorization: Bearer <human> | cookie+CSRF
Content-Type: application/json
{ "kind": "implementer", "task_id": N }

200  { "code": "AH-…", "expires_in_sec": 300 }
409  chat_pair_task_not_open     # status != open
422  task_id required
404  task not found
503  chat_pair_agent_missing     # нет активного cloud
403  human_only_gate             # агентский токен
```

Канон тот же URL, что intake; тело отличает kind. Карточка:
`POST /tasks/{id}/web-implementer-start` (CSRF), код один раз на странице
с номером задачи и TTL.

Redeem тот же публичный `POST /api/auth/chat-pair/redeem`. Implementer:

```text
role=agent, kind=implementer, bound_task_id=N
permissions = sorted(CHAT_PAIR_IMPLEMENTER_PERMS)
```

Intake-поля `role`/`permissions` байт-совместимы со старыми ассертами.

---

## Allowlist implementer (один-в-один с `hub/auth.py`)

```text
GET    /api/whoami
GET    /api/diagnostics/identity
GET    /api/tasks/{task_id}
GET    /api/tasks/{task_id}/tree
GET    /api/tasks/{task_id}/context
GET    /api/tasks/{task_id}/readiness
GET    /api/tasks/{task_id}/review-brief
GET    /api/tasks/{task_id}/acceptance_criteria
GET    /api/tasks/{task_id}/updates
POST   /api/tasks/{task_id}/updates
POST   /api/tasks/{task_id}/question
POST   /api/tasks/{task_id}/claim
POST   /api/tasks/{task_id}/pair-start
POST   /api/tasks/{task_id}/submit-review
POST   /api/tasks/{task_id}/declare-wait
POST   /api/sessions/register
POST   /api/sessions/{session_id}/heartbeat
POST   /api/auth/chat-pair/redeem
POST   /api/auth/chat-pair/revoke
```

`{task_id}` — named group; равенство `bound_task_id`. `{session_id}` —
анонимный сегмент (свой UUID).

---

## Закрытые маршруты (не в implementer allowlist)

| Метод+путь | Почему закрыт |
|---|---|
| `GET /api/tasks` | список всех задач |
| `POST /api/tasks` | create; нет в perms |
| `POST …/refine`, `PUT/DELETE …/acceptance_criteria`, `POST …/risks` | intake-authoring |
| `POST …/review-verdict`, `…/approve`, `…/decide`, `…/force-complete` | человеческие гейты |
| `POST …/release` | только `claimed`, трогает git хоста |
| `GET/POST /mcp`, `/run-validation`, `/run-ac-tests` | хост хаба / MCP |
| projects, skills, `/api/admin/*` | не этот канал |
| любой другой `{task_id}` | 403 `chat_pair_gate_forbidden` |

Intake-маршруты create/refine **остаются** на `CHAT_PAIR_ALLOWLIST`.

---

## Acceptance criteria

`expectation_source: requirement`. Локаторы — существующие тесты #980/#981
(#979 не пишет рантайм).

### AC-1 — чужой `{task_id}` закрыт

- **Given** implementer-сессия, bound к задаче N
- **When** `GET /api/tasks/{M}` где M ≠ N
- **Then** 403 `chat_pair_gate_forbidden`; `GET /api/tasks/{N}` → 200
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_implementer_cannot_reach_another_task`

### AC-2 — код только из `open`

- **Given** задача не `open` (running после pair-start)
- **When** `POST /api/auth/chat-pair/start` `{kind:implementer, task_id}`
- **Then** 409 `chat_pair_task_not_open`; строка кода не создаётся
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_implementer_code_not_issued_unless_task_is_open`

### AC-3 — revoke по kind

- **Given** живые intake и implementer сессии одного issuer
- **When** implementer `POST /api/auth/chat-pair/revoke`
- **Then** implementer whoami → 401; intake whoami → 200, `role=human`
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_implementer_self_revoke_leaves_intake_alive`

### AC-4 — повторный redeem неотличим

- **Given** уже потраченный implementer-код
- **When** второй redeem
- **Then** 401 `chat_pair_invalid`; поля `token` нет
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_implementer_redeem_spent_code_is_indistinguishable`

### AC-5 — нет cloud → 503 на выдаче, 401 на угадывании

- **Given** принципал `HAIPLANE_CHAT_PAIR_AGENT` неактивен
- **When** start implementer; затем redeem случайной строки
- **Then** start 503 `chat_pair_agent_missing`; redeem 401 `chat_pair_invalid`
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_implementer_start_without_cloud_is_503_guessed_redeem_401`

### AC-6 — intake рядом не ломается

- **Given** живая implementer-сессия на задаче N
- **When** intake start+redeem того же issuer
- **Then** whoami `role=human`, `permissions_summary == CHAT_PAIR_PERMS`;
  `POST /api/tasks` создаёт задачу
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_intake_start_redeem_unchanged_alongside_implementer`

### AC-7 — кнопка на open-карточке

- **Given** human cookie+CSRF, задача `open`
- **When** `POST /tasks/{id}/web-implementer-start`
- **Then** HTML с `AH-` и номером задачи; код `kind=implementer`,
  `bound_task_id=id`. Без CSRF — не 200 с новым кодом
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_open_task_card_issues_implementer_code`

### AC-8 — running карточку не выдаёт

- **Given** задача `running`
- **When** web-implementer-start
- **Then** 409, нового кода нет; кнопка в HTML open-ветки не рендерится
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_running_task_card_does_not_issue_implementer_code`

### AC-9 — `/chat-pair` остаётся intake

- **Given** страница `/chat-pair`
- **When** GET HTML
- **Then** копирайт про постановку/уточнение; слова implementer нет
- **verifiable_by:** `test`
- **test_ref:** `tests/test_chat_pair.py::test_chat_pair_page_copy_stays_intake`

### AC-10 — operator-guide 4a ≠ 4b

- **Given** `docs/agent-mcp-operator-guide.md`
- **When** читать 4a и 4b
- **Then** 4a — intake; 4b — кнопка «Передать в облачный чат» и
  `git_mode=remote`
- **verifiable_by:** `test`
- **test_ref:** `tests/test_docs_links.py::test_operator_guide_separates_intake_and_implementer_pairing`

---

## Risks

| Риск | Митигация |
|---|---|
| Спека разъедется с allowlist | AC-1 + этот документ копирует кортеж из `hub/auth.py`; правка списка — решение в задаче, не «заодно» |
| Читатель примет `role` за гейт | Constraints здесь и changelog в #961: human = intake-only |
| TTL 2h молча оставляет running | #983, не этот документ |

---

## Validation

```bash
uv run pytest -q tests/test_chat_pair.py tests/test_docs_links.py
```

Рантайм в #979 не меняется: тесты уже зелёные на `develop`.

---

## Files

- `docs/issues/task-979-chat-pair-implementer.md` (этот файл)
- `docs/issues/task-961-chat-pair.md` — Constraints + changelog: role=human intake-only
- `docs/agent-context/change-map.md` — ссылка на эту SDD
- `docs/issues/chat-pair-implementer-path.md` — outline, не дублировать AC

---

## YAML (исторический контракт T-kind, уже в хабе как #980)

```yaml
work_type: feature
size: L
scope_in:
  - kind=implementer on same code machinery as #961
  - start body {kind, task_id}; issue only from open
  - sibling allowlist; bound_task_id gate
  - CHAT_PAIR_IMPLEMENTER_PERMS without tasks.create
scope_out:
  - flipping intake role to agent
  - widening CHAT_PAIR_ALLOWLIST
  - TTL renew
affected_areas:
  - hub/services/chat_pair.py
  - hub/auth.py
  - hub/app.py
  - hub/db.py
  - hub/models.py
  - hub/config.py
  - hub/actionable_errors.py
```
