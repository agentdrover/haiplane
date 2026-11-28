"""Races, loss, corrupted material and actor isolation at the live boundary."""

import asyncio
from dataclasses import replace, FrozenInstanceError
import traceback

import pytest

from hub.config import TokenIdentity
from hub.integrations.github_credentials import CredentialError, Secret, OwnerProof
from hub.integrations.github_keyring import GithubKeyring
from hub.services.github_vault import GithubVault
from hub.services.project_access import Delegation
from datetime import datetime, timedelta, timezone
from tests.test_github_credentials import setup


@pytest.mark.parametrize("purpose", ["read", "git_write"])
@pytest.mark.parametrize(
    "override",
    [
        {"protected_path_policy": ["mutable"]},
        {"expected_sha": ["mutable"]},
        {"refs": (["refs/heads/mutable"],)},
        {"permissions": (("contents", ["read"]),)},
    ],
)
def test_operation_context_rejects_mutable_fields_for_every_purpose(purpose, override):
    from hub.integrations.github_credentials import CredentialContext

    values = dict(
        project_id=1,
        binding_id=1,
        repository_id=201,
        actor_id=1,
        purpose=purpose,
        auth_epoch=1,
        permissions=(("contents", "read"),),
        refs=("refs/heads/feature",),
        expected_sha="a" * 40,
        protected_path_policy="deny-protected",
    )
    values.update(override)
    with pytest.raises(CredentialError, match="invalid_context"):
        CredentialContext(**values)


async def test_token_expiring_during_final_owner_proof_is_not_published(
    db, db_dsn, tmp_path, monkeypatch
):
    vault, service, provider, contexts, identities, _ = await setup(
        db, db_dsn, tmp_path
    )
    clock = [1000.0]
    vault.clock = service.clock = lambda: clock[0]
    verify = provider.verify_owner
    calls = []

    async def verify_after_wait(*args):
        calls.append(True)
        proof = await verify(*args)
        if len(calls) == 2:
            clock[0] = 4601.0
        return proof

    monkeypatch.setattr(provider, "verify_owner", verify_after_wait)
    with pytest.raises(CredentialError, match="credential_expired"):
        await service.credential(identities[0], contexts[0])
    assert len(calls) == 2


async def test_token_expiring_while_waiting_for_publication_lock_is_blocked(
    db, db_dsn, tmp_path, monkeypatch
):
    vault, _, _, _, _, _ = await setup(db, db_dsn, tmp_path)
    clock = [1000.0]
    vault.clock = lambda: clock[0]
    original = GithubKeyring.load
    calls = []

    def advance_at_publication(path):
        calls.append(True)
        ring = original(path)
        if len(calls) == 2:
            clock[0] = 4990.0
        return ring

    async def renew(_old):
        return Secret("synthetic-installation"), 5000.0

    monkeypatch.setattr(GithubKeyring, "load", advance_at_publication)
    with pytest.raises(CredentialError, match="credential_expired"):
        await vault.refresh("expiring", "installation", renew)


async def test_first_bootstrap_persists_new_key_directory_parents(
    db, db_dsn, tmp_path, monkeypatch
):
    import os

    original = os.fsync
    synced = []

    def record(fd):
        synced.append(os.fstat(fd).st_ino)
        original(fd)

    monkeypatch.setattr(os, "fsync", record)
    path = tmp_path / "fresh-parent" / "keys" / "credential.keys"
    await GithubVault(db_dsn, path).bootstrap()
    assert tmp_path.stat().st_ino in synced
    assert path.parent.parent.stat().st_ino in synced
    assert path.parent.stat().st_ino in synced

    def fail(fd):
        if os.fstat(fd).st_ino == tmp_path.stat().st_ino:
            raise OSError("injected parent fsync failure")
        original(fd)

    monkeypatch.setattr(os, "fsync", fail)
    failed = tmp_path / "failed-parent" / "keys" / "credential.keys"
    with pytest.raises(CredentialError, match="key_unreadable"):
        await GithubVault(db_dsn, failed).bootstrap()
    assert not failed.exists()
    # The failed mkdir remains. A retry must sync its original parent too,
    # rather than trusting existence as evidence of durability.
    assert failed.parent.parent.exists()
    synced.clear()
    monkeypatch.setattr(os, "fsync", record)
    await GithubVault(db_dsn, failed).bootstrap()
    assert failed.exists()
    assert tmp_path.stat().st_ino in synced
    assert failed.parent.parent.stat().st_ino in synced
    assert failed.parent.stat().st_ino in synced


async def test_cached_token_requires_live_owner_and_exact_repo_proof(
    db, db_dsn, tmp_path, monkeypatch
):
    _, service, provider, contexts, identities, _ = await setup(db, db_dsn, tmp_path)
    ctx = contexts[0]
    await service.credential(identities[0], ctx)
    for proof in (
        OwnerProof(999, ctx.repository_id, 50, True, ctx.permissions),
        OwnerProof(101, 999, 50, True, ctx.permissions),
        OwnerProof(101, ctx.repository_id, 999, True, ctx.permissions),
        OwnerProof(101, ctx.repository_id, 50, False, ctx.permissions),
        OwnerProof(101, ctx.repository_id, 50, True, (("metadata", "read"),)),
    ):

        async def verify(*_args):
            return proof

        monkeypatch.setattr(provider, "verify_owner", verify)
        with pytest.raises(CredentialError, match="delegation_denied"):
            await service.credential(identities[0], ctx)
    assert len(provider.minted) == 1


async def test_actor_task_purpose_and_epoch_partition_the_cache(db, db_dsn, tmp_path):
    _, service, provider, contexts, identities, _ = await setup(db, db_dsn, tmp_path)
    ctx = contexts[0]
    task = await db.execute(
        "INSERT INTO tasks(title,project_id) VALUES ('scoped',?)", (ctx.project_id,)
    )
    await db.commit()
    for context in (
        ctx,
        replace(ctx, purpose="clone"),
        replace(ctx, task_id=task.lastrowid),
    ):
        await service.credential(identities[0], context)
    assert len(provider.minted) == 3
    actor = TokenIdentity(
        "pair",
        "agent",
        principal_id=ctx.actor_id,
        permissions=frozenset({"tasks.read"}),
        chat_pair_kind="implementer",
        chat_pair_task_id=task.lastrowid,
    )
    bound = replace(ctx, task_id=task.lastrowid)
    with pytest.raises(CredentialError, match="access_denied"):
        await service.credential(actor, bound)
    delegation = Delegation(
        ctx.actor_id,
        ctx.project_id,
        task.lastrowid,
        1,
        frozenset({"tasks.read"}),
        datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert (await service.credential(actor, bound, delegation)).context == bound
    with pytest.raises(CredentialError):
        replace(ctx, permissions=list(ctx.permissions))
    with pytest.raises(CredentialError):
        replace(ctx, purpose="clone", permissions=(("contents", "write"),))
    with pytest.raises(FrozenInstanceError):
        ctx.project_id = 999


async def test_revocation_while_minting_cannot_publish_a_lease(
    db, db_dsn, tmp_path, monkeypatch
):
    _, service, provider, contexts, identities, _ = await setup(db, db_dsn, tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()
    original = provider.mint

    async def mint(*args):
        started.set()
        await finish.wait()
        return await original(*args)

    monkeypatch.setattr(provider, "mint", mint)
    pending = asyncio.create_task(service.credential(identities[0], contexts[0]))
    await started.wait()
    await db.execute(
        "UPDATE principals SET status='disabled' WHERE id=?", (contexts[0].actor_id,)
    )
    await db.commit()
    finish.set()
    with pytest.raises(CredentialError, match="access_denied"):
        await pending


async def test_ciphertext_cannot_be_swapped_between_slots(db, db_dsn, tmp_path):
    vault, _, _, _, _, _ = await setup(db, db_dsn, tmp_path)
    await vault.put("other", "app", Secret("synthetic-other"))
    await db.execute(
        "UPDATE github_credential_slots SET ciphertext=(SELECT ciphertext FROM github_credential_slots WHERE slot='app:1') WHERE slot='other'"
    )
    await db.commit()
    with pytest.raises(CredentialError, match="credential_unreadable"):
        await vault.get("other", "app")


async def test_key_permissions_symlinks_and_wrong_keys_fail_closed(
    db, db_dsn, tmp_path
):
    vault, service, _, contexts, identities, _ = await setup(db, db_dsn, tmp_path)
    vault.key_path.chmod(0o644)
    with pytest.raises(CredentialError, match="key_unreadable"):
        await vault.check_key()
    vault.key_path.chmod(0o600)
    saved = vault.key_path.with_suffix(".saved")
    vault.key_path.rename(saved)
    vault.key_path.symlink_to(saved)
    with pytest.raises(CredentialError, match="key_unreadable"):
        await vault.check_key()
    vault.key_path.unlink()
    GithubKeyring.create(vault.key_path)
    with pytest.raises(CredentialError, match="credential_unreadable"):
        await vault.bootstrap()
    assert (await service.status(identities[0], contexts[0].project_id))[
        "status"
    ] == "recovery_required"
    vault.key_path.unlink()
    saved.rename(vault.key_path)
    await vault.check_key()


async def test_rotation_crash_retains_old_keys_until_ciphertext_commit(
    db, db_dsn, tmp_path, monkeypatch
):
    vault, _, _, _, _, _ = await setup(db, db_dsn, tmp_path)
    original = (await vault.get("app:1", "app")).reveal()

    def fail(*_args):
        raise RuntimeError("injected rotation interruption")

    with monkeypatch.context() as patch:
        patch.setattr(GithubKeyring, "seal", fail)
        with pytest.raises(RuntimeError):
            await vault.rotate()
    assert (await vault.get("app:1", "app")).reveal() == original
    assert len(GithubKeyring.load(vault.key_path).keys) == 2
    second = GithubVault(db_dsn, vault.key_path, clock=lambda: 1000.0)
    await asyncio.gather(vault.rotate(), second.rotate())
    assert (await second.get("app:1", "app")).reveal() == original
    assert len(GithubKeyring.load(vault.key_path).keys) == 1


async def test_cancelled_refresh_is_not_replayed_by_another_instance(
    db, db_dsn, tmp_path
):
    vault, _, _, _, _, _ = await setup(db, db_dsn, tmp_path)
    await vault.put("expired", "oauth", Secret("synthetic-consumable"), expires_at=0)
    started = asyncio.Event()
    calls = []

    async def renew(_old):
        calls.append(True)
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(vault.refresh("expired", "oauth", renew))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    second = GithubVault(db_dsn, vault.key_path, clock=lambda: 1000.0)
    with pytest.raises(CredentialError, match="refresh_reauthorization_required"):
        await second.refresh("expired", "oauth", renew)
    assert len(calls) == 1


async def test_unknown_refresh_and_failed_cleanup_do_not_leak_exception_context(
    db, db_dsn, tmp_path, monkeypatch
):
    vault, service, provider, contexts, identities, _ = await setup(
        db, db_dsn, tmp_path
    )
    provider.failure = True
    with pytest.raises(CredentialError) as error:
        await service.credential(identities[0], contexts[0])
    assert error.value.__context__ is None
    assert "synthetic-user" not in "".join(traceback.format_exception(error.value))

    async def fail(_old):
        raise RuntimeError("synthetic-secret-in-provider-error")

    async def cleanup(*_args):
        raise RuntimeError("synthetic-secret-in-cleanup-error")

    monkeypatch.setattr(vault, "_block_refresh", cleanup)
    with pytest.raises(CredentialError) as unknown:
        await vault.refresh("indeterminate", "oauth", fail)
    assert str(unknown.value) == "refresh_outcome_unknown"
    assert "synthetic-secret" not in "".join(traceback.format_exception(unknown.value))


async def test_bootstrap_installer_is_idempotent_and_recovery_keeps_hub_usable(
    tmp_path,
):
    from scripts.github_credentials_bootstrap import prepare

    path = tmp_path / "database" / "hub.db"
    key = tmp_path / "keys" / "credentials.keys"
    assert await prepare(path, key) == {"credential_storage": "ready"}
    before = key.read_bytes()
    assert await prepare(path, key) == {"credential_storage": "ready"}
    assert key.read_bytes() == before
    vault = GithubVault(str(path), key)
    await vault.put("material", "app", Secret("synthetic-private-material"))
    key.rename(key.with_suffix(".saved"))
    state = await prepare(path, key)
    assert state["credential_storage"] == "recovery_required"
    assert state["reason"] == "key_missing"
    assert not key.exists()


async def test_installation_mint_failure_can_retry_but_oauth_refresh_cannot(
    db, db_dsn, tmp_path, monkeypatch
):
    vault, service, provider, contexts, identities, _ = await setup(
        db, db_dsn, tmp_path
    )
    clock = [1000.0]
    vault.clock = service.clock = lambda: clock[0]
    original = provider.mint
    calls = []

    async def mint(*args):
        calls.append(True)
        if len(calls) == 1:
            raise CredentialError("provider_unknown")
        return await original(*args)

    monkeypatch.setattr(provider, "mint", mint)
    with pytest.raises(CredentialError, match="provider_unknown"):
        await service.credential(identities[0], contexts[0])
    with pytest.raises(CredentialError, match="provider_unknown"):
        await service.credential(identities[0], contexts[0])
    assert len(calls) == 1
    clock[0] += 6
    assert (await service.credential(identities[0], contexts[0])).context == contexts[0]
    assert len(calls) == 2


async def test_expired_refresh_lease_requires_verified_reauthorization(
    db, db_dsn, tmp_path
):
    vault, _, _, _, _, _ = await setup(db, db_dsn, tmp_path)
    await vault.put("crashed", "oauth", Secret("synthetic-old-pair"), expires_at=0)
    await db.execute(
        "UPDATE github_credential_slots SET state='refreshing',lease_id='dead-worker',lease_until=999 WHERE slot='crashed'"
    )
    await db.commit()
    calls = []

    async def renew(_old):
        calls.append(True)
        return Secret("synthetic-new-pair"), 5000.0

    for instance in (vault, GithubVault(db_dsn, vault.key_path, clock=lambda: 1000)):
        with pytest.raises(CredentialError, match="refresh_reauthorization_required"):
            await instance.refresh("crashed", "oauth", renew)
    assert not calls
    await vault.put(
        "crashed", "oauth", Secret("synthetic-verified-pair"), expires_at=5000
    )
    assert (await vault.refresh("crashed", "oauth", renew))[
        0
    ].reveal() == "synthetic-verified-pair"
    assert not calls
