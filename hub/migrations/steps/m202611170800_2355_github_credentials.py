"""Encrypted credential slots and separate app/install/project permission planes."""

MIGRATIONS = [
    (
        "github_credential_slots_2355",
        """CREATE TABLE IF NOT EXISTS github_credential_slots (
        slot TEXT PRIMARY KEY, kind TEXT NOT NULL,
        ciphertext BLOB NOT NULL, expires_at REAL,
        version INTEGER NOT NULL DEFAULT 1,
        state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','refreshing','blocked')),
        lease_id TEXT, lease_until REAL, failure_code TEXT NOT NULL DEFAULT ''
    )""",
    ),
    (
        "github_app_credential_configs_2355",
        """CREATE TABLE IF NOT EXISTS github_app_credential_configs (
        app_id INTEGER PRIMARY KEY CHECK(app_id>0), client_id TEXT NOT NULL,
        permissions TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled'))
    )""",
    ),
    (
        "github_user_credential_apps_2355",
        """CREATE TABLE IF NOT EXISTS github_user_credential_apps (
        account_link_id INTEGER NOT NULL REFERENCES github_account_links(id),
        app_id INTEGER NOT NULL REFERENCES github_app_credential_configs(app_id),
        PRIMARY KEY(account_link_id,app_id)
    )""",
    ),
    (
        "github_project_credential_grants_2355",
        """CREATE TABLE IF NOT EXISTS github_project_credential_grants (
        project_id INTEGER PRIMARY KEY REFERENCES project_repository_bindings(project_id),
        app_id INTEGER NOT NULL REFERENCES github_app_credential_configs(app_id),
        installation_permissions TEXT NOT NULL, project_permissions TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended'))
    )""",
    ),
]
