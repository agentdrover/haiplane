"""Private-project metadata foundation. Legacy projects remain unassigned.

Circular deferred keys require the owner membership and matching connection
to exist at commit, including when ownership changes. No credentials here.
"""

MIGRATIONS = [
    (
        "private_project_security_2350",
        """CREATE TABLE IF NOT EXISTS private_project_security (
            project_id INTEGER PRIMARY KEY REFERENCES projects(id),
            owner_id INTEGER NOT NULL REFERENCES principals(id),
            owner_role TEXT NOT NULL DEFAULT 'owner' CHECK(owner_role='owner'),
            auth_epoch INTEGER NOT NULL DEFAULT 1 CHECK(auth_epoch > 0),
            UNIQUE(project_id, owner_id),
            FOREIGN KEY(project_id, owner_id, owner_role)
                REFERENCES project_memberships(project_id, principal_id, role)
                DEFERRABLE INITIALLY DEFERRED
        )""",
    ),
    (
        "private_project_memberships_2350",
        """CREATE TABLE IF NOT EXISTS project_memberships (
            project_id INTEGER NOT NULL REFERENCES private_project_security(project_id)
                DEFERRABLE INITIALLY DEFERRED,
            principal_id INTEGER NOT NULL REFERENCES principals(id),
            role TEXT NOT NULL CHECK(role IN ('owner','member','viewer')),
            PRIMARY KEY(project_id, principal_id),
            UNIQUE(project_id, principal_id, role)
        )""",
    ),
    (
        "private_project_one_owner_2350",
        "CREATE UNIQUE INDEX IF NOT EXISTS private_project_one_owner "
        "ON project_memberships(project_id) WHERE role='owner'",
    ),
    (
        "private_project_invitations_2350",
        """CREATE TABLE IF NOT EXISTS project_invitations (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES private_project_security(project_id),
            recipient_id INTEGER NOT NULL REFERENCES principals(id),
            role TEXT NOT NULL CHECK(role IN ('member','viewer')),
            token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash)=64),
            expires_at TEXT NOT NULL,
            auth_epoch INTEGER NOT NULL CHECK(auth_epoch > 0),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','accepted','revoked','expired'))
        )""",
    ),
    (
        "private_project_one_invitation_2350",
        "CREATE UNIQUE INDEX IF NOT EXISTS private_project_one_invitation "
        "ON project_invitations(project_id, recipient_id) WHERE status='pending'",
    ),
    (
        "private_project_account_links_2350",
        """CREATE TABLE IF NOT EXISTS github_account_links (
            id INTEGER PRIMARY KEY,
            principal_id INTEGER NOT NULL UNIQUE REFERENCES principals(id),
            github_user_id INTEGER NOT NULL UNIQUE CHECK(github_user_id > 0),
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disconnected')),
            UNIQUE(id, principal_id)
        )""",
    ),
    (
        "private_project_bindings_2350",
        """CREATE TABLE IF NOT EXISTS project_repository_bindings (
            project_id INTEGER PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            account_link_id INTEGER NOT NULL,
            repository_id INTEGER NOT NULL CHECK(repository_id > 0),
            installation_id INTEGER NOT NULL CHECK(installation_id > 0),
            full_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','disconnected')),
            UNIQUE(owner_id, repository_id),
            FOREIGN KEY(project_id, owner_id)
                REFERENCES private_project_security(project_id, owner_id)
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(account_link_id, owner_id)
                REFERENCES github_account_links(id, principal_id)
                DEFERRABLE INITIALLY DEFERRED
        )""",
    ),
]
