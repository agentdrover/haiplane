#!/usr/bin/env python3
"""Installer entry: create a fresh key, never replace a lost recovery key."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

# Like the other installer scripts, this also runs from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub import config, db as database  # noqa: E402
from hub.integrations.github_credentials import CredentialError  # noqa: E402
from hub.services.github_vault import GithubVault  # noqa: E402


async def prepare(
    database_path: Path, key_path: Path, *, rotate: bool = False
) -> dict[str, str]:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    db = await database.connect(str(database_path))
    try:
        await database.bootstrap(db)
    finally:
        await db.close()
    vault = GithubVault(str(database_path), key_path)
    try:
        if rotate:
            await vault.rotate()
        else:
            await vault.bootstrap()
        return {"credential_storage": "ready"}
    except CredentialError as error:
        # Hub history/UI must still start after a DB-only restore; the broker
        # refuses GitHub access until the operator restores the separate key.
        return {
            "credential_storage": "recovery_required",
            "reason": str(error),
            "action": "restore_operator_key",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=config.HUB_DB_PATH)
    parser.add_argument(
        "--key-file",
        type=Path,
        default=Path(
            config.env_get(
                "GITHUB_CREDENTIAL_KEY_FILE",
                str(
                    config.HOME
                    / ".config"
                    / "haiplane-hub"
                    / "github"
                    / "credentials.keys"
                ),
            )
        ),
    )
    parser.add_argument("--rotate", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(prepare(args.database, args.key_file, rotate=args.rotate))
    print(json.dumps(result))
    return 2 if args.rotate and result["credential_storage"] != "ready" else 0


if __name__ == "__main__":
    raise SystemExit(main())
