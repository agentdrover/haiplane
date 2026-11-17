"""Local AES-GCM keyring, separate from SQLite and DB-only backups."""

import base64
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hub.integrations.github_credentials import CredentialError, Secret


class GithubKeyring:
    def __init__(self, path: Path, active: str, keys: dict[str, bytes]):
        self.path, self.active, self.keys = path, active, keys

    @staticmethod
    def _open_directory(path: Path, create: bool) -> int:
        try:
            return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            if not create:
                raise
        parent = GithubKeyring._open_directory(path.parent, create=True)
        try:
            try:
                os.mkdir(path.name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                # Another bootstrap may have created this directory.
                os.fsync(parent)
            else:
                # Persist every new ancestor entry, not only the key file.
                os.fsync(parent)
            fd = os.open(
                path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            try:
                os.fsync(fd)
            except BaseException:
                os.close(fd)
                raise
            return fd
        finally:
            os.close(parent)

    @staticmethod
    def _directory(path: Path, create: bool = False) -> int:
        try:
            fd = GithubKeyring._open_directory(path.parent, create)
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                os.close(fd)
                raise CredentialError("key_unreadable")
            return fd
        except FileNotFoundError:
            raise CredentialError("key_missing") from None
        except OSError:
            raise CredentialError("key_unreadable") from None

    @classmethod
    def load(cls, path: Path) -> "GithubKeyring":
        directory = cls._directory(path)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1
                ):
                    raise CredentialError("key_unreadable")
                data = json.loads(stream.read(65537))
            keys = {
                k: base64.b64decode(v, validate=True) for k, v in data["keys"].items()
            }
            active = data["active"]
            if (
                data["format"] != 1
                or active not in keys
                or len(keys) > 16
                or any(len(v) != 32 for v in keys.values())
            ):
                raise ValueError
            return cls(path, active, keys)
        except FileNotFoundError:
            raise CredentialError("key_missing") from None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise CredentialError("key_unreadable") from None
        finally:
            os.close(directory)

    @classmethod
    def create(cls, path: Path) -> "GithubKeyring":
        active = uuid4().hex
        ring = cls(path, active, {active: AESGCM.generate_key(bit_length=256)})
        ring.write(exclusive=True)
        return ring

    def write(self, *, exclusive: bool = False) -> None:
        directory = self._directory(self.path, create=True)
        target = self.path.name if exclusive else ".github-key-" + uuid4().hex
        try:
            fd = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            payload = json.dumps(
                {
                    "format": 1,
                    "active": self.active,
                    "keys": {
                        k: base64.b64encode(v).decode() for k, v in self.keys.items()
                    },
                },
                separators=(",", ":"),
            ).encode()
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if not exclusive:
                os.replace(
                    target, self.path.name, src_dir_fd=directory, dst_dir_fd=directory
                )
            os.fsync(directory)
        except FileExistsError:
            raise CredentialError("key_already_exists") from None
        except OSError:
            raise CredentialError("key_unreadable") from None
        finally:
            if not exclusive:
                try:
                    os.unlink(target, dir_fd=directory)
                except FileNotFoundError:
                    pass
            os.close(directory)

    def next_key(self) -> "GithubKeyring":
        if len(self.keys) >= 16:
            raise CredentialError("key_rotation_recovery_required")
        active = uuid4().hex
        return GithubKeyring(
            self.path,
            active,
            {**self.keys, active: AESGCM.generate_key(bit_length=256)},
        )

    @staticmethod
    def _aad(slot: str, kind: str) -> bytes:
        return json.dumps(
            ["github-credential-v1", slot, kind], separators=(",", ":")
        ).encode()

    def seal(self, slot: str, kind: str, secret: Secret) -> bytes:
        nonce = os.urandom(12)
        encrypted = AESGCM(self.keys[self.active]).encrypt(
            nonce, secret.reveal().encode(), self._aad(slot, kind)
        )
        return json.dumps(
            [self.active, base64.b64encode(nonce + encrypted).decode()],
            separators=(",", ":"),
        ).encode()

    def open(self, slot: str, kind: str, ciphertext: bytes) -> Secret:
        from cryptography.exceptions import InvalidTag

        try:
            kid, encoded = json.loads(ciphertext)
            data = base64.b64decode(encoded, validate=True)
            raw = AESGCM(self.keys[kid]).decrypt(
                data[:12], data[12:], self._aad(slot, kind)
            )
            return Secret(raw.decode())
        except (InvalidTag, ValueError, KeyError, TypeError, UnicodeError):
            raise CredentialError("credential_unreadable") from None
