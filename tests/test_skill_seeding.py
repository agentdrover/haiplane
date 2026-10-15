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
from pathlib import Path

import aiosqlite

from hub.db import (
    _SCHEMA,
    EXECUTOR_PAIR_DISCIPLINE_SKILL,
    MACHINE_REVIEW_CYCLE_SKILL,
    _migrate,
    fetchall,
    seed_default_skills,
)
import hub.db as db_module
from hub.repository import activate_skill_version, get_active_skill

OLD_TEXT = "старый текст без locator"

#: The rule itself, not a pointer to it: a text that keeps "see hub-submit-task"
#: and drops these has stopped teaching anything (Codex on 97c8e2a).
RESUBMISSION_CLAIMS = (
    "новая сдача (новое поколение, прежний вердикт не текущий, #1054)",
    "повтор из review с тем же sha сохраняет поколение, статус и текущесть вердикта",
    "finding_outcomes, accept_areas и решение о заказе ревью могут обновиться (#1265)",
)


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


async def test_a_revert_is_served_even_though_its_version_is_lower(
    db: aiosqlite.Connection,
):
    """The step the demote actually exists for: promote LOW while HIGH is live.

    One upgrade proves nothing here — the version it inserts is the highest, so
    it would be served with or without a demote. The failure appears on the
    second step: reverting the constant re-activates an EARLIER version while
    the reverted-away text keeps winning on version number, and
    ``get_active_skill`` serves the highest active one. Without the demote the
    library goes on serving text the code no longer contains, for good.
    """
    await _install(
        db,
        "machine-review-cycle",
        [
            (1, MACHINE_REVIEW_CYCLE_SKILL, "draft", "seed", ""),
            (2, "текст, который откатили", "active", "seed", "seed"),
        ],
    )
    await seed_default_skills(db)

    assert await _served(db, "machine-review-cycle") == MACHINE_REVIEW_CYCLE_SKILL
    versions = {
        v["version"]: v["status"] for v in await _versions(db, "machine-review-cycle")
    }
    assert versions == {1: "active", 2: "draft"}, (
        "the reverted-away version steps down, or it keeps winning on number"
    )


async def test_the_demote_never_touches_what_a_person_published(
    db: aiosqlite.Connection,
):
    """The demote runs over a MIXED library, and only our own rows may move.

    Every other test lands in a branch where the statement either does not run
    or has nothing of a person's to hit. This one puts a human-published
    version and a stale seed version side by side at different numbers, which
    is the only population where the WHERE clause has to do any work.
    """
    await _install(
        db,
        "machine-review-cycle",
        [
            (1, "версия человека", "active", "denis", "denis"),
            (2, "прежнее слово сида", "active", "seed", "seed"),
        ],
    )
    await seed_default_skills(db)

    by_version = {v["version"]: v for v in await _versions(db, "machine-review-cycle")}
    assert by_version[1]["status"] == "active", "a person's version is never demoted"
    assert by_version[1]["activated_by"] == "denis"
    assert by_version[2]["status"] == "draft", "our own previous word steps down"
    assert await _served(db, "machine-review-cycle") == MACHINE_REVIEW_CYCLE_SKILL


async def test_promoting_what_a_person_published_keeps_their_signature(
    db: aiosqlite.Connection,
):
    """Agreeing with a person is not the same act as replacing them.

    When the shipped text is already in the library on a row a HUMAN activated,
    the seed activates that row — and must leave their name on it. Stamping
    'seed' would erase the only record that a person ever spoke here, and the
    next upgrade would read the row as the hub's own and replace a decision it
    never made. The library serves the right text either way, so nothing but
    this assertion can tell the two apart.
    """
    await _install(
        db,
        "machine-review-cycle",
        [
            (1, MACHINE_REVIEW_CYCLE_SKILL, "active", "seed", "denis"),
            (2, "прежнее слово сида", "active", "seed", "seed"),
        ],
    )
    await seed_default_skills(db)

    by_version = {v["version"]: v for v in await _versions(db, "machine-review-cycle")}
    assert await _served(db, "machine-review-cycle") == MACHINE_REVIEW_CYCLE_SKILL
    assert by_version[1]["activated_by"] == "denis", (
        "the person who published this text keeps their name on it"
    )


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


async def test_a_lost_version_number_never_leaves_the_library_unreadable(
    db: aiosqlite.Connection, monkeypatch
):
    """Losing the INSERT must not take the only active version down with it.

    ``ON CONFLICT DO NOTHING`` makes losing cheap, and the demote runs whether
    we won or not — so a seeder that lost its version number to somebody ELSE's
    row would step the library's only active version down and put nothing in
    its place. ``hub_get_skill`` then answers 404 for as long as nobody
    intervenes, which is a worse outcome than the stale text this whole
    function exists to replace.

    Reachable: an agent proposing a version through ``POST /api/skills`` files
    a DRAFT, and it commits between this seeder's SELECT and its INSERT. The
    row that won the version number is neither ours nor active, so nothing else
    puts the library back.
    """
    await _install(db, "machine-review-cycle", [(1, OLD_TEXT, "active", "seed", "")])

    real_insert = db_module._insert_seed_skill

    async def _lose_the_race(conn, name, kind, content, tags, version, status):
        if name != "machine-review-cycle":
            return await real_insert(conn, name, kind, content, tags, version, status)
        # Somebody else took this version number a moment ago; our INSERT hits
        # UNIQUE(name, version) and does nothing.
        await conn.execute(
            "INSERT INTO skills (name, kind, version, content, tags, status, "
            "created_by, activated_by) VALUES (?, ?, ?, ?, ?, 'draft', 'agent', '')",
            (name, kind, version, "версия, предложенная агентом", tags),
        )

    monkeypatch.setattr(db_module, "_insert_seed_skill", _lose_the_race)
    await seed_default_skills(db)

    assert await _served(db, "machine-review-cycle") == OLD_TEXT, (
        "the library keeps serving the stale text until a seeder actually "
        "replaces it — it never goes dark"
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


async def test_executor_discipline_seed_teaches_resubmission_and_waits_as_draft(
    db: aiosqlite.Connection,
):
    """The seeded discipline matches lifecycle.submit_for_review (#1054, #1265).

    A fix to submitted code is a new commit, a push and a NEW submission; a
    feed entry does not move the pinned sha. And the text lands beside, never
    over, an active version that a person stands behind: both a version a
    stranger wrote and a seeded draft a person activated.
    """
    text = EXECUTOR_PAIR_DISCIPLINE_SKILL
    flat = " ".join(text.split())  # a phrase wrapped over two lines is still the phrase
    assert "апдейтом, а не пересдачей" not in flat
    assert "правки в review — апдейтом" not in flat.lower()
    assert "ровно одна сдача" not in flat.lower(), (
        "an unconditional single-submission rule contradicts the resubmission rule"
    )
    assert "hub-submit-task" in text
    for claim in RESUBMISSION_CLAIMS:
        assert claim in flat.lower(), f"seed lost the rule itself: {claim}"
    assert "только сдай" in flat, "the hub's submit-only order stays an exception"
    # The draft must be the whole active text with the fix on top, not a
    # shorter version a person would lose sections by activating.
    assert "## Вопрос — это стоп" in text
    assert "## Правила пилота" in text
    assert "#1054" in text and "#1265" in text

    # The spec teaches the same rules to people who write the order; it is the
    # second place the same two phrases lived.
    spec = " ".join(
        (
            Path(__file__).resolve().parents[1]
            / "docs/specs/orchestrator-executor-environment.md"
        )
        .read_text(encoding="utf-8")
        .split()
    ).lower()
    assert "апдейтом, а не пересдачей" not in spec
    assert "ровно одна сдача" not in spec
    assert "hub-submit-task" in spec and "#1054" in spec
    for claim in RESUBMISSION_CLAIMS:
        assert claim.lower() in spec, f"spec lost the rule itself: {claim}"

    name = "executor-pair-discipline"
    populations = {
        "foreign author": (1, OLD_TEXT, "active", "denis", "denis"),
        "seeded, activated by a person": (1, OLD_TEXT, "active", "seed", "denis"),
    }
    for label, active_row in populations.items():
        await _install(db, name, [active_row])
        await seed_default_skills(db)
        await seed_default_skills(db)

        assert await _served(db, name) == OLD_TEXT, label
        versions = await _versions(db, name)
        active = [v for v in versions if v["status"] == "active"]
        assert [(v["version"], v["activated_by"]) for v in active] == [
            (1, active_row[4])
        ], label
        drafts = [v for v in versions if v["status"] == "draft"]
        assert [d["content"] for d in drafts] == [text], (
            f"{label}: the new text waits as exactly one draft"
        )


# --- #1701: навыки из файлов skills/<name>/SKILL.md --------------------------

import hashlib  # noqa: E402
import logging  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import zipfile  # noqa: E402

import pytest  # noqa: E402

from hub import skill_files  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _write_skill(root: Path, name: str, body: str = "тело", **front) -> str:
    fields = {"name": name, "description": f"описание {name}", **front}
    text = "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\n" + body
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_bytes(text.encode("utf-8"))
    return text


@pytest.fixture
def skills_tree(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(skill_files, "skills_root", lambda: root)
    return root


async def test_skills_are_seeded_from_files_once(
    db: aiosqlite.Connection, skills_tree: Path
):
    """AC-1: пустая база, два старта — навыки активны с текстом файлов."""
    texts = {n: _write_skill(skills_tree, n) for n in ("zz-one", "zz-two")}
    await seed_default_skills(db)
    first = {n: await _versions(db, n) for n in texts}
    await seed_default_skills(db)
    second = {n: await _versions(db, n) for n in texts}

    for name, text in texts.items():
        assert [(v["version"], v["status"], v["content"]) for v in first[name]] == [
            (1, "active", text)
        ]
        assert first[name][0]["created_by"] == "seed"
    assert second == first, "второй старт ничего не меняет"

    # И настоящее дерево репозитория: каждый его файл засеян как лежит.
    real_root = REPO / "skills"
    loaded = skill_files.load_skill_files(real_root)
    names = {f.name for f in loaded}
    assert {
        "hub-task-prep",
        "hub-development",
        "hub-testing",
        "project-context",
    } <= names
    assert "local-executor-hub-repo" in names
    assert not names & skill_files.CODE_SEEDED_SKILLS


async def test_real_skill_files_are_seeded_into_an_empty_library(
    db: aiosqlite.Connection,
):
    await seed_default_skills(db)
    for f in skill_files.load_skill_files(REPO / "skills"):
        assert await _served(db, f.name) == f.content, f.name


async def test_seed_skill_is_promoted_from_a_changed_file(
    db: aiosqlite.Connection, skills_tree: Path
):
    """AC-2: активный seed со старым текстом — в обоих видах — заменяется."""
    text = _write_skill(skills_tree, "zz-promote")
    for label, activated_by in (("seed", "seed"), ("legacy row", "")):
        await _install(
            db, "zz-promote", [(1, OLD_TEXT, "active", "seed", activated_by)]
        )
        await seed_default_skills(db)
        assert await _served(db, "zz-promote") == text, label
        versions = await _versions(db, "zz-promote")
        assert [(v["version"], v["status"]) for v in versions] == [
            (1, "draft"),
            (2, "active"),
        ], label
        assert (
            versions[1]["created_by"] == "seed"
            and versions[1]["activated_by"] == "seed"
        )


async def test_a_human_published_skill_is_not_overwritten_by_a_file(
    db: aiosqlite.Connection, skills_tree: Path, caplog
):
    """AC-3: активная версия человека цела, файл лежит черновиком, есть warning."""
    text = _write_skill(skills_tree, "zz-human")
    await _install(db, "zz-human", [(1, OLD_TEXT, "active", "denis", "denis")])
    with caplog.at_level(logging.WARNING, logger="hub.db"):
        await seed_default_skills(db)
        await seed_default_skills(db)

    assert await _served(db, "zz-human") == OLD_TEXT
    versions = await _versions(db, "zz-human")
    assert [(v["version"], v["status"], v["content"]) for v in versions] == [
        (1, "active", OLD_TEXT),
        (2, "draft", text),
    ]
    warned = [r for r in caplog.records if "zz-human" in r.getMessage()]
    assert len(warned) == 1, "предупреждение с именем навыка, один раз"


async def test_freshness_is_by_hash_not_by_version_number(
    db: aiosqlite.Connection, skills_tree: Path
):
    """AC-4: откат файла выбирает версию по хешу, а не по большему номеру."""
    old = _write_skill(skills_tree, "zz-revert", body="старое")
    new = old.replace("старое", "новое")
    assert skill_files.text_hash(old) != skill_files.text_hash(new)
    # Файл откатили на старое; база помнит новое под большим номером.
    await _install(
        db,
        "zz-revert",
        [
            (1, old, "draft", "seed", ""),
            (5, new, "active", "seed", "seed"),
        ],
    )
    await seed_default_skills(db)

    assert await _served(db, "zz-revert") == old
    assert [(v["version"], v["status"]) for v in await _versions(db, "zz-revert")] == [
        (1, "active"),
        (5, "draft"),
    ], "новой версии не заведено: нужный текст уже был"

    # Черновик человека с большим номером не мешает: активная seed-версия
    # со старым текстом всё равно заменяется, номер берётся после черновика.
    await _install(
        db,
        "zz-revert",
        [
            (1, OLD_TEXT, "active", "seed", "seed"),
            (7, "черновик человека", "draft", "denis", ""),
        ],
    )
    await seed_default_skills(db)
    assert await _served(db, "zz-revert") == old
    assert max(v["version"] for v in await _versions(db, "zz-revert")) == 8


def test_a_skill_file_without_a_matching_name_fails(tmp_path):
    """AC-6: проверка красная и называет файл."""
    root = tmp_path / "skills"
    _write_skill(root, "good-one")
    (root / "no-name").mkdir()
    (root / "no-name" / "SKILL.md").write_text("---\ndescription: d\n---\nx\n")
    _write_skill(root, "wrong-dir")
    (root / "wrong-dir" / "SKILL.md").write_text(
        "---\nname: other\ndescription: d\n---\nx\n"
    )
    (root / "no-desc").mkdir()
    (root / "no-desc" / "SKILL.md").write_text("---\nname: no-desc\n---\nx\n")
    (root / "bare").mkdir()
    (root / "bare" / "SKILL.md").write_text("# без front-matter\n")
    _write_skill(root, "executor-pair-discipline")

    problems = skill_files.skill_file_problems(root)
    joined = "\n".join(problems)
    for name in ("no-name", "wrong-dir", "no-desc", "bare", "executor-pair-discipline"):
        assert f"/{name}/SKILL.md" in joined, name
    assert "good-one" not in joined
    assert [f.name for f in skill_files.load_skill_files(root)] == ["good-one"]


def test_every_skill_file_in_the_repository_is_valid():
    assert skill_files.skill_file_problems(REPO / "skills") == []
    # Исключение без front-matter — только выгрузка прода как есть.
    assert skill_files.LEGACY_NO_FRONT_MATTER == {"local-executor-hub-repo"}


#: Хеши активных навыков прода на 15.10.2026 (выгрузка GET /api/skills/{name}).
PROD_ACTIVE = {
    "local-executor-hub-repo": (
        "1d1da7a5867cde77bc91dcb59cf78e85fdcf2cea6c5198ffc3a98478a91b5e37",  # pragma: allowlist secret
        4,
        "pda_claude",
        "admin",
    ),
    "hub-review-report-reading": (
        "d875e35d38027e5fe6c97e68e8cfc3f5efabfa37180367d450504ffdefe4a474",  # pragma: allowlist secret
        2,
        "admin",
        "admin",
    ),
    "hub-submit-task": (
        "5bba46c231528b9b90a7835630df241d102e060703d0be86627e0c44cc75a041",  # pragma: allowlist secret
        5,
        "admin",
        "admin",
    ),
    "executor-pair-discipline": (
        "6453bf6dbe61d6c4dbbdc7667ea15f7a125f41c6a5579c0114c7a8fd8bfd35ca",  # pragma: allowlist secret
        5,
        "seed",
        "admin",
    ),
    "machine-review-cycle": (
        "399a4429d456525ec98356e8c2e4c8237c9c24bc0b58b77c18be013373b237fa",  # pragma: allowlist secret
        5,
        "seed",
        "seed",
    ),
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def test_first_rollout_leaves_every_active_prod_skill_untouched(
    db: aiosqlite.Connection,
):
    """Главное требование выката: хеши совпали с базой — ни версии, ни черновика."""
    sources = {
        "local-executor-hub-repo": (REPO / "skills/local-executor-hub-repo/SKILL.md"),
        "hub-review-report-reading": REPO / "skills/hub-review-report-reading/SKILL.md",
        "hub-submit-task": REPO / "skills/hub-submit-task/SKILL.md",
    }
    texts = {n: p.read_bytes().decode("utf-8") for n, p in sources.items()}
    texts["executor-pair-discipline"] = EXECUTOR_PAIR_DISCIPLINE_SKILL
    texts["machine-review-cycle"] = MACHINE_REVIEW_CYCLE_SKILL
    for name, (digest, version, created_by, activated_by) in PROD_ACTIVE.items():
        assert _sha(texts[name]) == digest, f"{name}: текст разошёлся с базой прода"
        await _install(
            db, name, [(version, texts[name], "active", created_by, activated_by)]
        )
    # multi-agent-review в проде правил человек (v10): активная версия не наша.
    # Её черновик из кода уже лежит рядом и сегодня, до этой задачи.
    from hub.db import MULTI_AGENT_REVIEW_SKILL

    mar = [
        (10, "текст v10, правка admin", "active", "admin", "admin"),
        (11, MULTI_AGENT_REVIEW_SKILL, "draft", "seed", ""),
    ]
    await _install(db, "multi-agent-review", mar)

    names = [*PROD_ACTIVE, "multi-agent-review"]
    before = {n: await _versions(db, n) for n in names}
    await seed_default_skills(db)
    await seed_default_skills(db)
    after = {n: await _versions(db, n) for n in names}

    assert after == before


async def test_a_code_seeded_name_never_comes_from_a_file(
    db: aiosqlite.Connection, skills_tree: Path
):
    """Два источника недопустимы: файл с именем кодового навыка не засевается."""
    _write_skill(skills_tree, "executor-pair-discipline", body="из файла")
    await _install(db, "executor-pair-discipline", [])
    await seed_default_skills(db)
    assert (
        await _served(db, "executor-pair-discipline") == EXECUTOR_PAIR_DISCIPLINE_SKILL
    )


def test_skills_are_seeded_from_the_built_wheel(tmp_path):
    """AC-5: wheel собран, старт вне checkout засевает навыки из пакета."""
    out = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out), str(REPO)],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(out.glob("*.whl"))
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(site)
    assert (site / "hub" / "_skills" / "hub-task-prep" / "SKILL.md").is_file()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    script = (
        "import asyncio, sys, hub\n"
        "from pathlib import Path\n"
        f"assert Path(hub.__file__).resolve().is_relative_to(Path({str(site)!r}).resolve()), hub.__file__\n"
        "import aiosqlite\n"
        "from hub.db import _SCHEMA, _migrate, seed_default_skills\n"
        "async def main():\n"
        "    db = await aiosqlite.connect(':memory:')\n"
        "    db.row_factory = aiosqlite.Row\n"
        "    await db.executescript(_SCHEMA)\n"
        "    await _migrate(db)\n"
        "    await seed_default_skills(db)\n"
        "    cur = await db.execute(\"SELECT name FROM skills WHERE status='active'\")\n"
        "    print(sorted(r[0] for r in await cur.fetchall()))\n"
        "asyncio.run(main())\n"
    )
    env = {"PYTHONPATH": str(site), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=elsewhere,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    names = eval(proc.stdout.strip().splitlines()[-1])
    assert {
        "hub-task-prep",
        "hub-development",
        "hub-testing",
        "project-context",
    } <= set(names)
