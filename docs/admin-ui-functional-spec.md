# Admin Section — Functional Specification

## Current State (as-is)

All 6 admin pages are **read-only tables** with no interactive actions.
All operations (create, edit, disable, revoke) are available only via REST API or CLI.

## Target State (to-be)

Full CRUD operations from the browser with HTMX-driven interactions,
confirm dialogs, flash messages, and filters.

---

## Pages

### 1. `/admin` — Summary (Dashboard)

**Current**: 5 stat cards + warnings + recent audit table.

**Add**:

- Stat card **Active Sessions** — count of non-expired browser sessions.
- Stat card **Expiring Keys (7d)** — API keys expiring within 7 days.
- **Quick Actions** block — buttons:
  - "Create User" → `/admin/users`
  - "Create Agent" → `/admin/agents`
  - "Create API Key" → `/admin/keys`
- Warning when `locked_users > 0` with link to filtered users view.

---

### 2. `/admin/users` — Human Users

**Current**: read-only table (ID, Username, Display Name, Email, Roles, Status, Last Seen).

**Add**:

#### Create User form (above table)

| Field        | Type     | Validation                          |
|--------------|----------|-------------------------------------|
| Username     | text     | required, 1-100 chars, `[a-zA-Z0-9_\-\.]+` |
| Display Name | text     | optional, max 200                   |
| Email        | text     | optional, max 320                   |
| Password     | password | required, min 8, letter+digit+special |
| Role         | select   | from available system roles         |

Submit → `POST /api/admin/principals` + `POST .../password`.
After creation — flash message "User created".

#### Row actions (Actions column)

| Action         | Condition                        | API call                  |
|----------------|----------------------------------|---------------------------|
| Disable        | status=active, not last admin    | `POST .../disable`        |
| Enable         | status=disabled                  | `POST .../enable`         |
| Unlock         | status=locked                    | `POST .../enable`         |
| Reset Password | always                           | `POST .../password`       |
| Edit Roles     | always                           | `PUT .../roles`           |
| Edit Profile   | always                           | `PATCH .../`              |

- **Reset Password** — modal with new password field.
- **Edit Roles** — modal with role checkboxes.
- **Edit Profile** — modal with display_name, email, notes fields.

#### Filters (above table)

- Status: All / Active / Disabled / Locked
- Search by username / display_name

---

### 3. `/admin/agents` — AI Agents

**Current**: read-only table, same structure as Users.

**Add**:

#### Create Agent form

| Field        | Type | Validation                    |
|--------------|------|-------------------------------|
| Username     | text | required, agent identifier    |
| Display Name | text | optional                      |
| Notes        | text | optional                      |

Role automatically set to `agent`.
Submit → `POST /api/admin/principals` with `kind=agent`.

#### Row actions

| Action         | Condition       | API call                          |
|----------------|-----------------|-----------------------------------|
| Create API Key | always          | `POST .../api-keys`               |
| Disable        | status=active   | `POST .../disable`                |
| Enable         | status=disabled | `POST .../enable`                 |
| View Keys      | always          | navigate to `/admin/keys?owner=ID`|

- **Create API Key** — modal: Name, Expires (days).
  After creation — modal with plaintext key, "Copy" button,
  warning: "This key will not be shown again."

Info block: "AI agents authenticate via API keys, not passwords."

---

### 4. `/admin/roles` — Roles & Permissions

**Current**: read-only table (Slug, Name, Description, System, Permissions).

**Add**:

#### Permission badges (grouped by category)

- `admin.*` — red badges
- `tasks.*` — blue badges
- `integrations.*` — green badges

#### Permission matrix (alternative view)

Table: roles as columns, permissions as rows, checkmarks at intersections.
Gives a quick visual answer to "who can do what".

#### Info message

"System roles cannot be modified. Custom roles will be available in a future release."

---

### 5. `/admin/keys` — API Keys

**Current**: read-only table (ID, Owner, Name, Prefix, Expires, Last Used, Revoked).

**Add**:

#### Create Key form

| Field   | Type   | Validation                      |
|---------|--------|---------------------------------|
| Owner   | select | all active principals           |
| Name    | text   | required, key description       |
| Expires | number | optional, days (0 = no expiry)  |

Submit → `POST /api/admin/principals/{id}/api-keys`.
Result: modal with plaintext key, "Copy to clipboard" button,
warning: "Save this key now — it will not be shown again."

#### Row actions

| Action | Condition    | API call                         |
|--------|-------------|----------------------------------|
| Revoke | not revoked | `POST /api/admin/api-keys/{id}/revoke` |

Confirm dialog before revoke.

#### Filters

- Status: Active / Revoked / Expired / All
- Owner (principal)

#### Visual indicators

- Keys expiring within 7 days → yellow badge "Expiring soon"
- Keys not used for 30+ days → gray badge "Unused"

---

### 6. `/admin/audit` — Audit Log

**Current**: read-only table (Time, Actor, Action, Target, Summary).

**Add**:

#### Filters

- Actor (select from principals)
- Action type (create_principal, disable_principal, revoke_api_key,
  set_password, set_roles, bootstrap, etc.)
- Date range (from — to)
- Text search in summary

#### Pagination

Prev / Next buttons or infinite scroll via HTMX `hx-trigger="revealed"`.
Current hard limit: 100 entries.

#### Detail expansion

Click on a row → expandable block showing full `detail` field (if present).

---

## UX Decisions (cross-cutting)

### Confirmations

All destructive actions (Disable, Revoke, Reset Password) require a
confirm dialog. Wording example:
"Are you sure you want to disable user **{username}**?"

### Flash messages

- Success → green toast at top: "User created", "API key revoked", "Password updated".
- Error → red toast with API error text.

### Navigation badges

Sub-nav items show counts: Users (3), Agents (2), Keys (5).

### HTMX

All forms and actions use HTMX (`hx-post`, `hx-swap`) without full page reload.
Consistent with the pattern already used in the main dashboard.

---

## Out of Scope

- Self-registration (users created by admin only)
- Bulk operations (mass disable, mass revoke)
- Audit log export (CSV/JSON)
- Custom roles editor (all roles are system-defined)
- Two-factor authentication
- User self-service profile page ("My account")

---

## Implementation Priority

| Priority | Page    | Feature                                              |
|----------|---------|------------------------------------------------------|
| P0       | Users   | Create user + Disable/Enable + Reset password        |
| P0       | Agents  | Create agent + Create API Key (show plaintext once)  |
| P0       | Keys    | Revoke key via UI                                    |
| P1       | Users   | Edit roles                                           |
| P1       | Keys    | Create key form + filters                            |
| P1       | Audit   | Filters + pagination                                 |
| P1       | Summary | Quick Actions + expiring keys counter                |
| P2       | Roles   | Permission matrix                                    |
| P2       | All     | Flash messages, nav counter badges                   |
