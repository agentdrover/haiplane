"""GitHub.com provider: fixed HTTPS hosts, narrow tokens and safe failures."""

from datetime import datetime, timezone
import json
import time
from collections.abc import Callable

import httpx
import jwt

from hub.integrations.github_credentials import (
    CredentialContext,
    CredentialError,
    OwnerProof,
    ResolvedGrant,
    Secret,
    TokenResponse,
)


class GithubTokenProvider:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.transport, self.clock = transport, clock

    @staticmethod
    def _positive_id(data: dict, key: str) -> int:
        value = data.get(key)
        if type(value) is not int or value <= 0:
            raise CredentialError("invalid_provider_response")
        return value

    async def _request(
        self,
        method: str,
        path: str,
        token: Secret | None = None,
        *,
        form: dict[str, str] | None = None,
        body: dict | None = None,
    ) -> dict:
        url = (
            "https://github.com/login/oauth/access_token"
            if form is not None
            else "https://api.github.com" + path
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        if form is not None:
            headers["Accept"] = "application/json"
        if token is not None:
            headers["Authorization"] = "Bearer " + token.reveal()
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=15,
            ) as client:
                response = await client.request(
                    method, url, headers=headers, data=form, json=body
                )
            if response.status_code in (401, 403, 404):
                raise CredentialError("provider_refused")
            if response.status_code != 200 and response.status_code != 201:
                raise CredentialError("provider_unknown")
            if len(response.content) > 2_000_000:
                raise CredentialError("invalid_provider_response")
            data = response.json()
            if not isinstance(data, dict) or "error" in data:
                raise CredentialError("provider_refused")
            return data
        except CredentialError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise CredentialError("provider_unavailable") from None

    @staticmethod
    def _payload(secret: Secret) -> dict:
        try:
            data = json.loads(secret.reveal())
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (TypeError, ValueError):
            raise CredentialError("credential_unreadable") from None

    def _app_jwt(self, app_id: int, secret: Secret) -> Secret:
        try:
            payload = self._payload(secret)
            encoded = jwt.encode(
                {
                    "iat": int(self.clock()) - 60,
                    "exp": int(self.clock()) + 540,
                    "iss": str(app_id),
                },
                payload["private_key"],
                algorithm="RS256",
            )
            return Secret(encoded)
        except Exception:
            raise CredentialError("credential_unreadable") from None

    async def refresh_user(
        self, app_id: int, app_secret: Secret, old: Secret
    ) -> tuple[Secret, float]:
        app, user = self._payload(app_secret), self._payload(old)
        try:
            form = {
                "client_id": app["client_id"],
                "client_secret": app["client_secret"],
                "grant_type": "refresh_token",
                "refresh_token": user["refresh_token"],
            }
            data = await self._request("POST", "", form=form)
            access, refresh = data["access_token"], data["refresh_token"]
            expires = int(data["expires_in"])
            if (
                not isinstance(access, str)
                or not isinstance(refresh, str)
                or not access
                or not refresh
                or expires <= 30
            ):
                raise ValueError
            return Secret(
                json.dumps({"access_token": access, "refresh_token": refresh})
            ), self.clock() + expires
        except (KeyError, ValueError, TypeError):
            raise CredentialError("invalid_provider_response") from None

    async def verify_owner(
        self,
        context: CredentialContext,
        grant: ResolvedGrant,
        user_secret: Secret,
        app_secret: Secret,
    ) -> OwnerProof:
        try:
            user_token = Secret(self._payload(user_secret)["access_token"])
            actor = await self._request("GET", "/user", user_token)
            repo = await self._request("GET", "/repos/" + grant.full_name, user_token)
            installation = await self._request(
                "GET",
                "/repos/" + grant.full_name + "/installation",
                self._app_jwt(grant.app_id, app_secret),
            )
            permissions = tuple(
                sorted(
                    (k, v)
                    for k, v in installation["permissions"].items()
                    if isinstance(k, str) and v in ("read", "write")
                )
            )
            visible = False
            for page in range(1, 101):
                data = await self._request(
                    "GET",
                    f"/user/installations/{grant.installation_id}/repositories?per_page=100&page={page}",
                    user_token,
                )
                repos = data["repositories"]
                if not isinstance(repos, list):
                    raise ValueError
                if any(r.get("id") == context.repository_id for r in repos):
                    visible = True
                    break
                if len(repos) < 100:
                    break
            else:
                raise CredentialError("provider_unknown")
            return OwnerProof(
                self._positive_id(actor, "id"),
                self._positive_id(repo, "id"),
                self._positive_id(installation, "id"),
                repo.get("permissions", {}).get("admin") is True and visible,
                permissions,
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            raise CredentialError("invalid_provider_response") from None

    async def mint(
        self, context: CredentialContext, grant: ResolvedGrant, app_secret: Secret
    ) -> TokenResponse:
        data = await self._request(
            "POST",
            f"/app/installations/{grant.installation_id}/access_tokens",
            self._app_jwt(grant.app_id, app_secret),
            body={
                "repository_ids": [context.repository_id],
                "permissions": dict(context.permissions),
            },
        )
        try:
            expiry = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                raise ValueError
            return TokenResponse(
                Secret(data["token"]), expiry.astimezone(timezone.utc).timestamp()
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            raise CredentialError("invalid_provider_response") from None
