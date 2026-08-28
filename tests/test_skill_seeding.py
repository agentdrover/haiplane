"""Seeded skills stay current without overwriting a person (#1028, #380).

``hub_get_skill`` serves the ACTIVE version, so a library still teaching the
previous contract keeps teaching it however many drafts pile up beside it. That
is not a cosmetic gap: on 2026-08-28 production refused a report without a
``locator`` (#1007) while the active ``machine-review-cycle`` was still v1 from
July, teaching the shape that gets the 422 — every harness that honestly read
the library walked into it.
"""

from __future__ import annotations

import asyncio

import aiosqlite

from hub.db import (
    _SCHEMA,
    MACHINE_REVIEW_CYCLE_SKILL,
    _migrate,
    fetchall,
    seed_default_skills,
)
from hub.repository import activate_skill_version, get_active_skill

OLD_TEXT = "старый текст без locator"


async def _versions(db: aiosqlite.Connection, name: str) -> list[dict]:
    return [
        dict(r)
        for r in await fetchall(
            db,
            "SELECT version, content, status, created_by, activated_by FROM skills "
            "WHERE name=? ORDER BY version ASC",
            (name,),
        )
    ]


async def _install(
    db: aiosqlite.Connection,
    name: str,
    rows: list[tuple[int, str, str, str, str]],
) -> None:
    """Put an exact population in the library, as an upgrade would find it.

    Each row is (version, content, status, created_by, activated_by). The
    library is emptied first so the test states the whole population rather
    than adding to whatever the fixture happened to seed.
    """
    await db.execute("DELETE FROM skills WHERE name=?", (name,))
    for version, content, status, created_by, activated_by in rows:
        await db.execute(
            "INSERT INTO skills (name, kind, version, content, tags, status, "
            "created_by, activated_by) VALUES (?, 'skill', ?, ?, '[]', ?, ?, ?)",
            (name, version, content, status, created_by, activated_by),
        )
    await db.commit()


async def _served(db: aiosqlite.Connection, name: str) -> str | None:
    """What ``hub_get_skill`` would return — the only answer that matters."""
    row = await get_active_skill(db, name)
    return None if row is None else str(row["content"])


async def test_unedited_seed_skill_is_promoted(db: aiosqlite.Connection):
    """AC-3, on the row an upgrade ACTUALLY finds.

    ``activated_by`` arrives by ``ALTER TABLE ... DEFAULT ''``, so every row
    that predates the column — which today is every row in production — carries
    an EMPTY value, not ``'seed'``. A suite that only ever sets ``'seed'``
    leaves the one input the library really holds untested: narrowing the
    predicate to ``activated_by == 'seed'`` keeps such a suite green while the
    hub goes on serving the July contract and the write path goes on answering
    422 (#1035).
    """
    await _install(db, "machine-review-cycle", [(1, OLD_TEXT, "active", "seed", "")])
    await seed_default_skills(db)

    assert await _served(db, "machine-review-cycle") == MACHINE_REVIEW_CYCLE_SKILL


async def test_the_hubs_own_published_version_is_replaced(db: aiosqlite.Connection):
    """The other half: a row the seed itself published, on a later upgrade."""
    await _install(
        db, "machine-review-cycle", [(1, OLD_TEXT, "active", "seed", "seed")]
    )
    await seed_default_skills(db)

    assert await _served(db, "machine-review-cycle") == MACHINE_REVIEW_CYCLE_SKILL


async def test_only_one_version_stays_live(db: aiosqlite.Connection):
    """A revert of the shipped text has to take effect, so it must be served.

    ``get_active_skill`` serves the HIGHEST active version. If each upgrade
    left its predecessor active, reverting the constant would re-activate an
    older version while the reverted-away text kept winning on version number —
    permanently. The hub's own previous word steps back to a draft; a person's
    never does.
    """
    await _install(db, "machine-review-cycle", [(1, OLD_TEXT, "active", "seed", "")])
    await seed_default_skills(db)

    live = [
        v
        for v in await _versions(db, "machine-review-cycle")
        if v["status"] == "active"
    ]
    assert len(live) == 1, f"exactly one live version, got {len(live)}"
    assert live[0]["content"] == MACHINE_REVIEW_CYCLE_SKILL


async def test_shipped_text_waiting_as_a_draft_is_activated(db: aiosqlite.Connection):
    """The state #1007 leaves behind: right text, wrong status.

    This is the population the previous fix created and the one a check of the
    form "is the shipped text present anywhere" reads as done. It is not done:
    the library still SERVES the July contract, so the harness still collects a
    422 on the whole report. Promotion has to look at the active row, not at
    the set of rows.
    """
    await _install(
        db,
        "machine-review-cycle",
        [
            (1, OLD_TEXT, "active", "seed", "seed"),
            (2, MACHINE_REVIEW_CYCLE_SKILL, "draft", "seed", ""),
        ],
    )
    await seed_default_skills(db)

    assert await _served(db, "machine-review-cycle") == MACHINE_REVIEW_CYCLE_SKILL
    versions = await _versions(db, "machine-review-cycle")
    assert len(versions) == 2, "the waiting draft is promoted, not duplicated"


async def test_no_active_version_at_all_is_published(db: aiosqlite.Connection):
    """Nobody published anything, so nobody is being overruled.

    A library holding only drafts answers 404 to ``hub_get_skill``. Treating
    that as "an operator is mid-edit" and filing yet another draft leaves the
    contract unreadable for as long as the hub runs.
    """
    await _install(db, "machine-review-cycle", [(1, OLD_TEXT, "draft", "denis", "")])
    assert await _served(db, "machine-review-cycle") is None

    await seed_default_skills(db)
    assert await _served(db, "machine-review-cycle") == MACHINE_REVIEW_CYCLE_SKILL


async def test_operator_edit_is_never_overwritten(db: aiosqlite.Connection):
    # AC-4: a version a person published stays active. The shipped text waits
    # beside it as a draft — the automaton does not overrule a human (#380).
    await _install(
        db, "machine-review-cycle", [(1, OLD_TEXT, "active", "denis", "denis")]
    )
    await seed_default_skills(db)

    assert await _served(db, "machine-review-cycle") == OLD_TEXT
    drafts = [
        v for v in await _versions(db, "machine-review-cycle") if v["status"] == "draft"
    ]
    assert drafts and drafts[0]["content"] == MACHINE_REVIEW_CYCLE_SKILL


async def test_human_activation_of_a_seeded_draft_is_respected(
    db: aiosqlite.Connection,
):
    """Authorship is not the same act as publication (#380).

    Activation is a human gate, and it leaves ``created_by='seed'`` on a row a
    person is now standing behind. A seed keyed on authorship alone would read
    that row as its own and replace a decision it never made.
    """
    await _install(db, "machine-review-cycle", [(1, OLD_TEXT, "draft", "seed", "")])
    # Through the real activation path, not by writing the column by hand: the
    # column only protects a person if the code that publishes actually fills
    # it in, and a test that stubs the value cannot tell whether it does.
    await activate_skill_version(db, "machine-review-cycle", 1, activated_by="denis")
    await db.commit()

    await seed_default_skills(db)

    assert await _served(db, "machine-review-cycle") == OLD_TEXT, (
        "the person who published this version keeps it published"
    )


async def test_second_start_adds_nothing(db: aiosqlite.Connection):
    # Idempotence: get_db seeds on every connection, and a hub restarted twice
    # must not grow a version per restart.
    await seed_default_skills(db)
    before = await _versions(db, "machine-review-cycle")
    await seed_default_skills(db)
    await seed_default_skills(db)
    assert await _versions(db, "machine-review-cycle") == before


async def test_seeding_is_safe_on_a_parallel_start(tmp_path):
    """AC-5: two WORKERS, not two coroutines sharing one connection.

    ``get_db`` seeds per connection, so the race that matters is between
    processes holding separate handles to the same file. Coroutines on a single
    ``aiosqlite`` connection serialise behind its own lock and never contend
    for ``UNIQUE(name, version)`` at all — a test built that way reports green
    without executing the branch it exists to cover.
    """
    path = tmp_path / "race.db"
    connections = []
    for _ in range(3):
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.executescript(_SCHEMA)
        await _migrate(conn)
        connections.append(conn)
    try:
        await _install(
            connections[0],
            "machine-review-cycle",
            [(1, OLD_TEXT, "active", "seed", "seed")],
        )
        results = await asyncio.gather(
            *(seed_default_skills(c) for c in connections),
            return_exceptions=True,
        )
        raised = [r for r in results if isinstance(r, BaseException)]
        assert not raised, f"a seeder died on a benign race: {raised}"

        versions = await _versions(connections[0], "machine-review-cycle")
        assert len(versions) == 2, "three racing seeders, one new version"
        assert (
            await _served(connections[0], "machine-review-cycle")
            == MACHINE_REVIEW_CYCLE_SKILL
        )
        # The loser must not leave its transaction aborted: the connection has
        # to keep working for the statements that come after it.
        assert await _served(connections[-1], "multi-agent-review") is not None
    finally:
        for conn in connections:
            await conn.close()
