"""Internal credential contracts. No browser/API payload contains a Secret."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
import hashlib
import json
import re


class CredentialError(RuntimeError):
    """Only fixed reason codes may cross the credential boundary."""

    REASONS = frozenset(
        {
            "access_changed",
            "access_denied",
            "connection_unavailable",
            "context_mismatch",
            "credential_unavailable",
            "credential_expired",
            "credential_unreadable",
            "delegation_denied",
            "invalid_app",
            "invalid_context",
            "invalid_permissions",
            "invalid_provider_response",
            "invalid_repository_name",
            "invalid_secret",
            "invalid_slot",
            "key_already_exists",
            "key_missing",
            "key_rotation_recovery_required",
            "key_unreadable",
            "permission_denied",
            "permission_expansion_refused",
            "provider_unavailable",
            "provider_refused",
            "provider_unknown",
            "purpose_scope_exceeded",
            "reauthorization_required",
            "refresh_pending",
            "refresh_reauthorization_required",
            "refresh_superseded",
            "refresh_unavailable",
            "refresh_outcome_unknown",
            "write_policy_required",
        }
    )

    def __init__(self, reason: str):
        super().__init__(reason if reason in self.REASONS else "credential_unavailable")


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str):
        if not isinstance(value, str) or not value or len(value) > 1_048_576:
            raise CredentialError("invalid_secret")
        self._value = value

    def reveal(self) -> str:
        """Trusted provider/process boundary only; never use in a projection."""
        return self._value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__


PermissionSet = tuple[tuple[str, str], ...]
LEVEL = {"read": 1, "write": 2}
PURPOSES = {
    "read": {"contents": "read", "metadata": "read"},
    "clone": {"contents": "read", "metadata": "read"},
    "git_write": {"contents": "write", "metadata": "read"},
    "pull_request": {"pull_requests": "write", "contents": "read", "metadata": "read"},
    "ci_read": {
        "actions": "read",
        "checks": "read",
        "statuses": "read",
        "metadata": "read",
    },
    "ci_write": {"actions": "write", "metadata": "read"},
    "workflow_write": {"contents": "write", "workflows": "write", "metadata": "read"},
}


def permissions_of(value: dict[str, str]) -> PermissionSet:
    if not value or any(
        k not in {p for v in PURPOSES.values() for p in v} or v not in LEVEL
        for k, v in value.items()
    ):
        raise CredentialError("invalid_permissions")
    return tuple(sorted(value.items()))


def permits(ceiling: PermissionSet, requested: PermissionSet) -> bool:
    levels = dict(ceiling)
    return all(
        LEVEL.get(levels.get(k, ""), 0) >= LEVEL.get(v, 99) for k, v in requested
    )


@dataclass(frozen=True)
class CredentialContext:
    project_id: int
    binding_id: int
    repository_id: int
    actor_id: int
    purpose: str
    auth_epoch: int
    permissions: PermissionSet
    task_id: int | None = None
    refs: tuple[str, ...] = ()
    expected_sha: str = ""
    protected_path_policy: str = ""

    def __post_init__(self):
        if (
            type(self.permissions) is not tuple
            or any(
                type(p) is not tuple
                or len(p) != 2
                or any(type(v) is not str for v in p)
                for p in self.permissions
            )
            or type(self.refs) is not tuple
            or any(type(ref) is not str for ref in self.refs)
            or type(self.purpose) is not str
            or type(self.expected_sha) is not str
            or type(self.protected_path_policy) is not str
        ):
            raise CredentialError("invalid_context")
        if self.task_id is not None and (
            type(self.task_id) is not int or self.task_id <= 0
        ):
            raise CredentialError("invalid_context")
        if any(
            type(v) is not int or v <= 0
            for v in (
                self.project_id,
                self.binding_id,
                self.repository_id,
                self.actor_id,
                self.auth_epoch,
            )
        ):
            raise CredentialError("invalid_context")
        if self.purpose not in PURPOSES or self.permissions != permissions_of(
            dict(self.permissions)
        ):
            raise CredentialError("invalid_context")
        if not permits(tuple(PURPOSES[self.purpose].items()), self.permissions):
            raise CredentialError("purpose_scope_exceeded")
        if self.is_write and (
            not self.refs
            or any(
                not isinstance(ref, str)
                or not ref.startswith("refs/")
                or ".." in ref
                or any(c in ref for c in ("\n", "\r", " "))
                for ref in self.refs
            )
            or not re.fullmatch(r"[a-fA-F0-9]{40}", self.expected_sha)
            or not self.protected_path_policy
        ):
            raise CredentialError("write_policy_required")

    @property
    def is_write(self) -> bool:
        return any(v == "write" for _, v in self.permissions)

    def cache_key(self, versions: tuple[int, ...]) -> str:
        values = (
            self.project_id,
            self.binding_id,
            self.repository_id,
            self.actor_id,
            self.task_id,
            self.purpose,
            self.auth_epoch,
            self.permissions,
            self.refs,
            self.expected_sha,
            self.protected_path_policy,
            versions,
        )
        return hashlib.sha256(
            json.dumps(values, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class ResolvedGrant:
    app_id: int
    installation_id: int
    account_link_id: int
    github_user_id: int
    full_name: str
    client_id: str
    installation_permissions: PermissionSet
    versions: tuple[int, ...]
    app_slot: str
    user_slot: str


@dataclass(frozen=True)
class OwnerProof:
    github_user_id: int
    repository_id: int
    installation_id: int
    admin: bool
    installation_permissions: PermissionSet


@dataclass(frozen=True)
class TokenResponse:
    token: Secret
    expires_at: float


@dataclass(frozen=True)
class CredentialLease:
    context: CredentialContext
    token: Secret
    expires_at: float


class CredentialProvider(Protocol):
    async def refresh_user(
        self, app_id: int, app_secret: Secret, old: Secret
    ) -> tuple[Secret, float]: ...
    async def verify_owner(
        self,
        context: CredentialContext,
        grant: ResolvedGrant,
        user_secret: Secret,
        app_secret: Secret,
    ) -> OwnerProof: ...
    async def mint(
        self, context: CredentialContext, grant: ResolvedGrant, app_secret: Secret
    ) -> TokenResponse: ...


def command_environment(
    lease: CredentialLease,
    context: CredentialContext,
    runtime: Path,
    inherited: dict[str, str],
) -> dict[str, str]:
    """For a trusted gh subprocess only, never for code supplied by a project.

    Execution/sandbox and purpose-to-command dispatch are separate boundaries.
    The caller must reauthorize before executing; a lease is not an ACL bypass.
    """
    if lease.context != context or not runtime.is_absolute():
        raise CredentialError("context_mismatch")
    env = {
        k: v
        for k, v in inherited.items()
        if k in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ"}
    }
    env.update(
        HOME=str(runtime),
        GH_CONFIG_DIR=str(runtime / "gh"),
        GH_TOKEN=lease.token.reveal(),
        GH_HOST="github.com",
        GH_PROMPT_DISABLED="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
    )
    return env


def credential_origin(full_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name) or any(
        part in {".", ".."} for part in full_name.split("/")
    ):
        raise CredentialError("invalid_repository_name")
    return f"https://github.com/{full_name}.git"
