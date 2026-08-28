"""Seeded skills stay current without overwriting a person (#1028, #380).

``hub_get_skill`` serves the ACTIVE version, so a library still teaching the
previous contract keeps teaching it however many drafts pile up beside it. That
is not a cosmetic gap: the write path started refusing reports without a
locator (#1007) while the active skill still described the old shape, so every
harness following it walked into a 422 on the whole report.
"""

from __future__ import annotations

import asyncio

import aiosqlite

from hub.db import MACHINE_REVIEW_CYCLE_SKILL, seed_default_skills
from hub.db import fetchall


async def _versions(db: aiosqlite.Connection, name: str) -> list[dict]:
    return [
        dict(r)
        for r in await fetchall(
            db,
            "SELECT version, content, status, created_by FROM skills "
            "WHERE name=? ORDER BY version ASC",
            (name,),
        )
    ]


async def _install_old_version(
    db: aiosqlite.Connection, name: str, *, created_by: str
) -> None:
    """Put a stale ACTIVE version in the library, as an upgrade would find it."""
    await db.execute("DELETE FROM skills WHERE name=?", (name,))
    await db.execute(
        "INSERT INTO skills (name, kind, version, content, tags, status, created_by) "
        "VALUES (?, 'skill', 1, 'старый текст без locator', '[]', 'active', ?)",
        (name, created_by),
    )
    await db.commit()


async def test_unedited_seed_skill_is_promoted(db: aiosqlite.Connection):
    # AC-3: the hub replaces its OWN previous word — the version it seeded and
    # nobody touched — so hub_get_skill serves the contract the write path
    # actually enforces.
    await _install_old_version(db, "machine-review-cycle", created_by="seed")
    await seed_default_skills(db)

    versions = await _versions(db, "machine-review-cycle")
    assert len(versions) == 2
    assert versions[0]["status"] == "superseded"
    active = [v for v in versions if v["status"] == "active"]
    assert len(active) == 1
    assert active[0]["content"] == MACHINE_REVIEW_CYCLE_SKILL
    assert "locator" in active[0]["content"], "the served text teaches the contract"


async def test_operator_edit_is_never_overwritten(db: aiosqlite.Connection):
    # AC-4: a version a person wrote stays active. The shipped text waits beside
    # it as a draft — the automaton does not get to overrule a human (#380).
    await _install_old_version(db, "machine-review-cycle", created_by="denis")
    await seed_default_skills(db)

    versions = await _versions(db, "machine-review-cycle")
    assert len(versions) == 2
    active = [v for v in versions if v["status"] == "active"]
    assert len(active) == 1
    assert active[0]["created_by"] == "denis"
    assert active[0]["content"] == "старый текст без locator"
    drafts = [v for v in versions if v["status"] == "draft"]
    assert drafts and drafts[0]["content"] == MACHINE_REVIEW_CYCLE_SKILL


async def test_second_start_adds_nothing(db: aiosqlite.Connection):
    # Idempotence: get_db seeds on every connection, and a hub restarted twice
    # must not grow a version per restart.
    await seed_default_skills(db)
    before = await _versions(db, "machine-review-cycle")
    await seed_default_skills(db)
    await seed_default_skills(db)
    assert await _versions(db, "machine-review-cycle") == before


async def test_seeding_is_safe_on_a_parallel_start(db: aiosqlite.Connection):
    # AC-5: two connections seeding at once compute the same next version. One
    # loses the UNIQUE(name, version) race — and losing is fine, because the
    # winner wrote the same text. What must not happen is a connection dying
    # over it.
    await _install_old_version(db, "machine-review-cycle", created_by="seed")
    await asyncio.gather(
        seed_default_skills(db),
        seed_default_skills(db),
        seed_default_skills(db),
    )
    versions = await _versions(db, "machine-review-cycle")
    assert len(versions) == 2, "three racing seeders, one new version"
    assert [v for v in versions if v["status"] == "active"][0][
        "content"
    ] == MACHINE_REVIEW_CYCLE_SKILL
