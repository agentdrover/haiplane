"""Active test skills for HTTP and browser fixtures that launch a slot."""

import aiosqlite

from hub import repository as repo
from hub.db import seed_default_skills
from hub.services.executor_skill_bundle import SKILL_NAMES


async def seed_slot_skills(db: aiosqlite.Connection) -> None:
    await seed_default_skills(db)
    for name in SKILL_NAMES[1:]:
        if await repo.get_active_skill(db, name) is not None:
            continue
        await repo.create_skill_version(
            db,
            name=name,
            kind="skill",
            content=f"Instructions for {name}",
            status="active",
        )
