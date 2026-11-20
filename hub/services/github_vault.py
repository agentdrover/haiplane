"""Encrypted slots with DB-coordinated rotation and single-flight renewal."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
import math
from pathlib import Path
import time
from uuid import uuid4

from hub import db as database
from hub.integrations.github_credentials import CredentialError, Secret
from hub.integrations.github_keyring import GithubKeyring


Renew = Callable[[Secret | None], Awaitable[tuple[Secret, float]]]


class GithubVault:
    def __init__(
        self,
        dsn: str,
        key_path: Path,
        *,
        clock: Callable[[], float] = time.time,
        timeout: float = 30,
    ):
        self.dsn, self.key_path, self.clock, self.timeout = (
            dsn,
            key_path,
            clock,
            timeout,
        )

    @asynccontextmanager
    async def _transaction(self):
        db = await database.connect(self.dsn)
        try:
            async with database.write_transaction(db):
                yield db
        finally:
            await db.close()

    async def bootstrap(self) -> None:
        """Installer-only: never replace a missing key of a nonempty vault."""
        async with self._transaction() as db:
            try:
                ring = GithubKeyring.load(self.key_path)
                await self._verify_rows(db, ring)
                return
            except CredentialError as error:
                if str(error) != "key_missing":
                    raise
            rows = await database.fetchall(
                db, "SELECT 1 FROM github_credential_slots LIMIT 1"
            )
            if rows:
                raise CredentialError("key_missing")
            try:
                GithubKeyring.create(self.key_path)
            except CredentialError as error:
                if str(error) != "key_already_exists":
                    raise
                GithubKeyring.load(self.key_path)

    async def check_key(self) -> None:
        async with self._transaction() as db:
            await self._verify_rows(db, GithubKeyring.load(self.key_path))

    @staticmethod
    async def _verify_rows(db, ring: GithubKeyring) -> None:
        for row in await database.fetchall(
            db,
            "SELECT slot,kind,ciphertext FROM github_credential_slots WHERE length(ciphertext)>0",
        ):
            ring.open(row["slot"], row["kind"], bytes(row["ciphertext"]))

    async def put(
        self, slot: str, kind: str, secret: Secret, *, expires_at: float | None = None
    ) -> None:
        if not slot or not kind or len(slot) > 256 or len(kind) > 40:
            raise CredentialError("invalid_slot")
        if expires_at is not None and not math.isfinite(expires_at):
            raise CredentialError("invalid_provider_response")
        async with self._transaction() as db:
            ring = GithubKeyring.load(self.key_path)
            await db.execute(
                "INSERT INTO github_credential_slots(slot,kind,ciphertext,expires_at) VALUES (?,?,?,?) "
                "ON CONFLICT(slot) DO UPDATE SET kind=excluded.kind,ciphertext=excluded.ciphertext,expires_at=excluded.expires_at,version=github_credential_slots.version+1,state='active',lease_id=NULL,lease_until=NULL,failure_code=''",
                (slot, kind, ring.seal(slot, kind, secret), expires_at),
            )

    async def get(self, slot: str, kind: str) -> Secret:
        return (await self.material(slot, kind))[0]

    async def material(self, slot: str, kind: str) -> tuple[Secret, int]:
        """Read the plaintext and its version from the same locked snapshot."""
        async with self._transaction() as db:
            ring = GithubKeyring.load(self.key_path)
            rows = await database.fetchall(
                db,
                "SELECT * FROM github_credential_slots WHERE slot=? AND kind=?",
                (slot, kind),
            )
            if not rows or rows[0]["state"] != "active" or not rows[0]["ciphertext"]:
                raise CredentialError("credential_unavailable")
            if (
                rows[0]["expires_at"] is not None
                and rows[0]["expires_at"] <= self.clock() + 30
            ):
                raise CredentialError("credential_unavailable")
            return ring.open(slot, kind, bytes(rows[0]["ciphertext"])), int(
                rows[0]["version"]
            )

    async def _reserve(self, slot: str, kind: str, lease: str):
        async with self._transaction() as db:
            ring = GithubKeyring.load(self.key_path)
            await db.execute(
                "INSERT OR IGNORE INTO github_credential_slots(slot,kind,ciphertext,expires_at) VALUES (?,?,?,0)",
                (slot, kind, b""),
            )
            row = (
                await database.fetchall(
                    db, "SELECT * FROM github_credential_slots WHERE slot=?", (slot,)
                )
            )[0]
            if row["kind"] != kind:
                raise CredentialError("refresh_reauthorization_required")
            if row["state"] == "blocked":
                if kind != "installation":
                    raise CredentialError("refresh_reauthorization_required")
                if row["lease_until"] is None or row["lease_until"] > self.clock():
                    raise CredentialError(row["failure_code"] or "refresh_pending")
                # Only installation minting is safe to retry; OAuth refresh
                # can consume a single-use input and requires reauthorization.
                await db.execute(
                    "UPDATE github_credential_slots SET state='active',failure_code='' WHERE slot=?",
                    (slot,),
                )
            if row["state"] == "refreshing":
                if row["lease_until"] is None or row["lease_until"] <= self.clock():
                    # A refresh token may already have been consumed externally.
                    await db.execute(
                        "UPDATE github_credential_slots SET state='blocked',failure_code='provider_unknown',lease_until=? WHERE slot=?",
                        (self.clock() + 5, slot),
                    )
                    return "blocked", None, 0.0
                return "wait", None, 0.0
            old = (
                ring.open(slot, kind, bytes(row["ciphertext"]))
                if row["ciphertext"]
                else None
            )
            if old is not None and (
                row["expires_at"] is None or row["expires_at"] > self.clock() + 30
            ):
                return "ready", old, row["expires_at"] or float("inf")
            await db.execute(
                "UPDATE github_credential_slots SET state='refreshing',lease_id=?,lease_until=? WHERE slot=?",
                (lease, self.clock() + self.timeout + 10, slot),
            )
            return "renew", old, 0.0

    async def refresh(self, slot: str, kind: str, renew: Renew) -> tuple[Secret, float]:
        lease = uuid4().hex
        for _ in range(1500):
            state, old, expires = await self._reserve(slot, kind, lease)
            if state == "ready":
                if old is None or expires <= self.clock() + 30:
                    raise CredentialError("credential_expired")
                return old, expires
            if state == "blocked":
                raise CredentialError(
                    "provider_unknown"
                    if kind == "installation"
                    else "refresh_reauthorization_required"
                )
            if state == "renew":
                token, expires = await self._renew(slot, kind, lease, old, renew)
                if expires <= self.clock() + 30:
                    raise CredentialError("credential_expired")
                return token, expires
            await asyncio.sleep(0.02)
        raise CredentialError("refresh_pending")

    async def _renew(
        self, slot: str, kind: str, lease: str, old: Secret | None, renew: Renew
    ) -> tuple[Secret, float]:
        try:
            secret, expires = await asyncio.wait_for(renew(old), timeout=self.timeout)
            if (
                not isinstance(secret, Secret)
                or not math.isfinite(expires)
                or expires <= self.clock() + 30
            ):
                raise CredentialError("invalid_provider_response")
            async with self._transaction() as db:
                ring = GithubKeyring.load(self.key_path)
                if expires <= self.clock() + 30:
                    raise CredentialError("credential_expired")
                cur = await db.execute(
                    "UPDATE github_credential_slots SET ciphertext=?,expires_at=?,version=version+1,state='active',lease_id=NULL,lease_until=NULL,failure_code='' WHERE slot=? AND kind=? AND state='refreshing' AND lease_id=? AND lease_until>?",
                    (
                        ring.seal(slot, kind, secret),
                        expires,
                        slot,
                        kind,
                        lease,
                        self.clock(),
                    ),
                )
                if cur.rowcount != 1:
                    raise CredentialError("refresh_superseded")
            return secret, expires
        except BaseException as error:
            cleanup_failed = False
            try:
                await self._block_refresh(
                    slot,
                    lease,
                    str(error)
                    if isinstance(error, CredentialError)
                    else "refresh_outcome_unknown",
                )
            except Exception:
                cleanup_failed = True
            if not isinstance(error, Exception):
                raise
            reason = (
                "refresh_outcome_unknown"
                if cleanup_failed
                else (
                    str(error)
                    if isinstance(error, CredentialError)
                    else "refresh_outcome_unknown"
                )
            )
            raise CredentialError(reason) from None

    async def _block_refresh(self, slot: str, lease: str, reason: str) -> None:
        async with self._transaction() as db:
            await db.execute(
                "UPDATE github_credential_slots SET state='blocked',expires_at=CASE WHEN kind='installation' THEN 0 ELSE expires_at END,lease_id=NULL,lease_until=?,failure_code=? WHERE slot=? AND lease_id=?",
                (self.clock() + 5, str(CredentialError(reason)), slot, lease),
            )

    async def rotate(self) -> None:
        async with self._transaction() as db:
            ring = GithubKeyring.load(self.key_path).next_key()
            ring.write()
            for row in await database.fetchall(
                db, "SELECT * FROM github_credential_slots WHERE length(ciphertext)>0"
            ):
                secret = ring.open(row["slot"], row["kind"], bytes(row["ciphertext"]))
                await db.execute(
                    "UPDATE github_credential_slots SET ciphertext=? WHERE slot=?",
                    (ring.seal(row["slot"], row["kind"], secret), row["slot"]),
                )
        # Check again under the DB lock: another process may have rotated since
        # the first commit. Never overwrite its newer keyring when retiring keys.
        async with self._transaction() as db:
            current = GithubKeyring.load(self.key_path)
            if current.active == ring.active:
                rows = await database.fetchall(
                    db,
                    "SELECT ciphertext FROM github_credential_slots WHERE length(ciphertext)>0",
                )
                import json

                if all(json.loads(bytes(r[0]))[0] == ring.active for r in rows):
                    GithubKeyring(
                        self.key_path,
                        ring.active,
                        {ring.active: ring.keys[ring.active]},
                    ).write()
