"""HTTP-level checks: no live GitHub account or real credential is used."""

from datetime import datetime, timezone
import json
from urllib.parse import parse_qs

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
import httpx
import jwt
import pytest

from hub.integrations.github_credentials import (
    CredentialContext,
    CredentialError,
    ResolvedGrant,
    Secret,
)
from hub.integrations.github_token_provider import GithubTokenProvider


@pytest.fixture(scope="module")
def private_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def fixtures(private_key):
    _, pem = private_key
    app = Secret(
        json.dumps(
            {
                "private_key": pem,
                "client_secret": "synthetic-client-secret",  # pragma: allowlist secret
                "client_id": "client1",
            }
        )
    )
    ctx = CredentialContext(
        1, 1, 201, 1, "read", 1, (("contents", "read"), ("metadata", "read"))
    )
    grant = ResolvedGrant(
        5, 50, 1, 101, "org/repo", "client1", ctx.permissions, (1,), "app:5", "user:5:1"
    )
    user = Secret(
        json.dumps(
            {
                "access_token": "synthetic-user-token",
                "refresh_token": "synthetic-refresh-token",
            }
        )
    )
    return app, ctx, grant, user


async def test_real_provider_pins_hosts_repo_permissions_and_refresh_body(
    private_key, caplog
):
    app, ctx, grant, user = fixtures(private_key)
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "github.com":
            assert request.url.path == "/login/oauth/access_token"
            assert not request.url.query
            form = parse_qs(request.content.decode())
            assert form["client_secret"] == ["synthetic-client-secret"]
            assert form["refresh_token"] == ["synthetic-refresh-token"]
            assert request.headers["accept"] == "application/json"
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-user-new",
                    "refresh_token": "synthetic-refresh-new",
                    "expires_in": 3600,
                },
            )
        assert request.url.host == "api.github.com"
        if request.url.path == "/user":
            assert request.headers["authorization"] == "Bearer synthetic-user-token"
            return httpx.Response(200, json={"id": 101})
        if request.url.path == "/repos/org/repo":
            return httpx.Response(200, json={"id": 201, "permissions": {"admin": True}})
        if request.url.path == "/repos/org/repo/installation":
            encoded = request.headers["authorization"].split()[1]
            decoded = jwt.decode(
                encoded,
                private_key[0].public_key(),
                algorithms=["RS256"],
                options={"verify_exp": False, "verify_iat": False},
            )
            assert decoded["iss"] == "5" and decoded["exp"] - decoded["iat"] <= 600
            return httpx.Response(
                200, json={"id": 50, "permissions": dict(ctx.permissions)}
            )
        if request.url.path == "/user/installations/50/repositories":
            return httpx.Response(200, json={"repositories": [{"id": 201}]})
        assert request.url.path == "/app/installations/50/access_tokens"
        assert json.loads(request.content) == {
            "repository_ids": [201],
            "permissions": dict(ctx.permissions),
        }
        return httpx.Response(
            201,
            json={
                "token": "synthetic-installation-long-variable-token",
                "expires_at": datetime.fromtimestamp(4600, timezone.utc).isoformat(),
            },
        )

    provider = GithubTokenProvider(
        transport=httpx.MockTransport(handler), clock=lambda: 1000.0
    )
    proof = await provider.verify_owner(ctx, grant, user, app)
    assert proof.admin and proof.repository_id == 201 and proof.github_user_id == 101
    response = await provider.mint(ctx, grant, app)
    assert response.expires_at == 4600
    assert response.token.reveal() == "synthetic-installation-long-variable-token"
    refreshed, expiry = await provider.refresh_user(5, app, user)
    assert (
        json.loads(refreshed.reveal())["refresh_token"] == "synthetic-refresh-new"
        and expiry == 4600
    )
    projection = caplog.text + " ".join(str(r.url) for r in requests)
    for secret in (
        "synthetic-user-token",
        "synthetic-client-secret",
        "synthetic-refresh-token",
        "synthetic-installation-long-variable-token",
    ):
        assert secret not in projection


@pytest.mark.parametrize(
    "status,reason",
    [
        (302, "provider_unknown"),
        (401, "provider_refused"),
        (403, "provider_refused"),
        (404, "provider_refused"),
        (503, "provider_unknown"),
    ],
)
async def test_provider_refuses_redirects_and_never_echoes_error_body(
    private_key, status, reason
):
    app, ctx, grant, _ = fixtures(private_key)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://attacker.invalid/capture"},
            text="synthetic-secret-in-error",
        )

    provider = GithubTokenProvider(
        transport=httpx.MockTransport(handler), clock=lambda: 1000.0
    )
    with pytest.raises(CredentialError) as error:
        await provider.mint(ctx, grant, app)
    assert str(error.value) == reason
    assert len(requests) == 1
    assert "synthetic-secret" not in str(error.value)


async def test_owner_proof_requires_user_visible_installation_repository(private_key):
    app, ctx, grant, user = fixtures(private_key)

    def handler(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 101})
        if request.url.path == "/repos/org/repo":
            return httpx.Response(200, json={"id": 201, "permissions": {"admin": True}})
        if request.url.path.endswith("/installation"):
            return httpx.Response(
                200, json={"id": 50, "permissions": dict(ctx.permissions)}
            )
        return httpx.Response(200, json={"repositories": []})

    provider = GithubTokenProvider(
        transport=httpx.MockTransport(handler), clock=lambda: 1000.0
    )
    assert not (await provider.verify_owner(ctx, grant, user, app)).admin
