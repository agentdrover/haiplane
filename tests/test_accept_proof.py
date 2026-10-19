"""Единый предикат «сдачу можно принять» (#1810, круг 5).

«Принять» — доказанные условия, а не «сомнений не набралось»: каждое условие
вне белого списка отчётов тоже ломает совет, и выдать «Принять» мимо
``prove_accept`` нельзя.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from hub import repository as repo
from hub.integrations.registry import plugins
from hub.services import decision_brief
from hub.services.accept_proof import AcceptProof
from tests.test_decision_brief_whitelist import ACCEPT, _action, _task, observe_tip


async def test_a_proof_cannot_be_forged():
    with pytest.raises(TypeError):
        AcceptProof("a" * 40)


@pytest.mark.parametrize("current", [True, False])
async def test_a_standing_changes_requested_blocks_accept(
    client, db: aiosqlite.Connection, monkeypatch, current: bool
):
    task_id, _ = await _task(client, db, monkeypatch, f"ap-verdict-{current}", 1)
    task = dict(await repo.get_task(db, task_id) or {})
    generation = int(task["submission_generation"])
    finding = {"id": 1, "severity": "high", "message": "сломано", "scope": "in_scope"}
    await db.execute(
        "UPDATE tasks SET review_verdict='changes_requested', "
        "review_verdict_generation=?, review_findings=? WHERE id=?",
        (generation if current else generation - 1, json.dumps([finding]), task_id),
    )
    await db.commit()
    got = await _action(db, task_id)
    # Вердикт прошлой сдачи код с тех пор пересдан — не действует.
    assert (got != ACCEPT) is current


async def test_branch_must_stand_on_the_submitted_sha(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Совпадение вершины — по последнему НАБЛЮДЕНИЮ хаба; ушла — не «Принять»."""
    task_id, _ = await _task(client, db, monkeypatch, "ap-sha-diverged", 1)
    assert await _action(db, task_id) == ACCEPT

    async def moved(*_a, **_k):
        return "b" * 40

    monkeypatch.setattr(plugins.git_ops, "head_sha", moved)
    await observe_tip(db, task_id)  # хаб увидел новую вершину
    assert await _action(db, task_id) != ACCEPT


@pytest.mark.parametrize("age_minutes", [None, 24 * 60])
async def test_no_fresh_observation_means_tip_not_verified(
    client, db: aiosqlite.Connection, monkeypatch, age_minutes
):
    """Нет наблюдения или оно устарело: «Проверить: вершина не сверена»."""
    from datetime import UTC, datetime, timedelta

    from hub.services import lifecycle

    task_id, _ = await _task(client, db, monkeypatch, f"ap-obs-{age_minutes}", 1)
    task = dict(await repo.get_task(db, task_id) or {})
    key = (task_id, task["branch"])
    if age_minutes is None:
        lifecycle._observed_tips.pop(key, None)
    else:
        sha, _at = lifecycle._observed_tips[key]
        lifecycle._observed_tips[key] = (
            sha,
            datetime.now(UTC) - timedelta(minutes=age_minutes),
        )
    got = await decision_brief.build(db, task)
    assert got["recommend"]["action"] == "Проверить"
    assert any("вершина" in r["text"].lower() for r in got["risks"])


async def test_showing_the_brief_never_touches_the_network(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Карточка перерисовывается каждые 15 с, входящие — по каждой задаче: без fetch."""
    from hub import services

    task_id, _ = await _task(client, db, monkeypatch, "ap-nonet", 1)
    calls: list[str] = []

    async def spy(*_a, **_k):
        calls.append("fetch_base")
        return True, ""

    monkeypatch.setattr(plugins.git_ops, "fetch_base", spy)
    task = dict(await repo.get_task(db, task_id) or {})
    await decision_brief.build(db, task)
    assert calls == [], "build зовёт git fetch"
    await services.get_inbox_data(db)
    assert calls == [], "входящие зовут git fetch"
    from hub import web

    assert await web._card_decision_brief(db, task, False) is not None
    assert calls == [], "блок справки карточки зовёт git fetch"


@pytest.mark.parametrize("scope,blocks", [("in_scope", True), ("out_of_scope", False)])
@pytest.mark.parametrize("verdict", ["approved", "changes_requested"])
async def test_open_in_scope_findings_block_accept_under_any_verdict(
    client,
    db: aiosqlite.Connection,
    monkeypatch,
    verdict: str,
    scope: str,
    blocks: bool,
):
    """Выбрано: in_scope-находки текущего поколения блокируют при ЛЮБОМ вердикте."""
    task_id, _ = await _task(client, db, monkeypatch, f"ap-find-{verdict}-{scope}", 1)
    task = dict(await repo.get_task(db, task_id) or {})
    finding = {"id": 1, "severity": "high", "message": "сломано", "scope": scope}
    await db.execute(
        "UPDATE tasks SET review_verdict=?, review_verdict_generation=?, "
        "review_findings=? WHERE id=?",
        (verdict, task["submission_generation"], json.dumps([finding]), task_id),
    )
    await db.commit()
    got = await _action(db, task_id)
    if verdict == "changes_requested":
        assert got != ACCEPT
    else:
        assert (got != ACCEPT) is blocks


@pytest.mark.parametrize(
    "section", ["unresolved", "lost_dimensions", "findings_rejected"]
)
@pytest.mark.parametrize("broken", ["not json", "{}", ""])
async def test_a_broken_report_section_is_unreadable_not_empty(
    client, db: aiosqlite.Connection, monkeypatch, section: str, broken: str
):
    task_id, ids = await _task(
        client, db, monkeypatch, f"ap-{section}-{len(broken)}", 1
    )
    await db.execute(
        f"UPDATE machine_reviews SET {section}=? WHERE id=?",  # nosec B608
        (broken, ids[0]),
    )
    await db.commit()
    assert await _action(db, task_id) != ACCEPT


async def _carry(db, task_id: int, source_id: int, **over) -> int:
    """Перенос #1361: копия в текущем поколении без токенов, с пометкой источника."""
    task = dict(await repo.get_task(db, task_id) or {})
    generation = int(task["submission_generation"])
    await db.execute(
        "UPDATE machine_reviews SET submission_generation=? WHERE id=?",
        (generation - 1, source_id),
    )
    source = dict(await repo.get_machine_review(db, source_id) or {})
    carried = await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill=source["harness_skill"],
        harness_version=source["harness_version"],
        agent_count=source["agent_count"],
        orchestrator=source["orchestrator"],
        model=source["model"],
        raw_count=int(source["raw_count"]),
        findings_confirmed="[]",
        findings_rejected=source["findings_rejected"],
        submitted_by=source["submitted_by"],
        incomplete=False,
        unresolved="[]",
        lost_dimensions="[]",
        profile=source["profile"],
        principal_id=source["principal_id"],
        carried_from_review_id=over.get("root", source_id),
        ci_integrity=("", ""),
    )
    await db.commit()
    return carried


async def test_a_carried_report_is_judged_through_its_source(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id, ids = await _task(client, db, monkeypatch, "ap-carry", 1)
    await _carry(db, task_id, ids[0])
    # Копия без токенов, исходный отчёт с provider_tokens>0: перенос годен.
    assert await _action(db, task_id) == ACCEPT


async def test_a_carried_report_with_a_weak_source_does_not_accept(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id, ids = await _task(client, db, monkeypatch, "ap-carry-weak", 1)
    await db.execute(
        "UPDATE machine_reviews SET provider_tokens=NULL WHERE id=?", (ids[0],)
    )
    await _carry(db, task_id, ids[0])
    assert await _action(db, task_id) != ACCEPT


async def test_a_carried_report_without_its_source_is_unreadable(
    client, db: aiosqlite.Connection, monkeypatch
):
    task_id, ids = await _task(client, db, monkeypatch, "ap-carry-lost", 1)
    await _carry(db, task_id, ids[0], root=987654)
    assert await _action(db, task_id) != ACCEPT


def test_the_brief_has_no_accept_emitter_at_all():
    """Решение владельца (#1810, 20.10): справка «Принять» не выдаёт — эмиттеров нет."""
    import inspect
    import re

    source = re.sub(r"\s+", " ", inspect.getsource(decision_brief))
    assert "ACCEPT" not in source
    assert "VERDICT_APPROVED" not in source


async def test_a_carried_report_needs_every_report_of_the_source_generation(
    client, db: aiosqlite.Connection, monkeypatch
):
    """A (unresolved) и чистый B одного поколения; переносится B — всё равно не «Принять»."""
    task_id, ids = await _task(client, db, monkeypatch, "ap-carry-all", 2)
    await db.execute(
        "UPDATE machine_reviews SET unresolved=? WHERE id=?",
        ('[{"title":"не проверено","why":"x"}]', ids[0]),
    )
    task = dict(await repo.get_task(db, task_id) or {})
    generation = int(task["submission_generation"])
    # Оба отчёта — поколение «до пересдачи»; в текущее переносится только B.
    await db.execute(
        "UPDATE machine_reviews SET submission_generation=? WHERE id IN (?, ?)",
        (generation - 1, ids[0], ids[1]),
    )
    source = dict(await repo.get_machine_review(db, ids[1]) or {})
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill=source["harness_skill"],
        harness_version=source["harness_version"],
        agent_count=source["agent_count"],
        orchestrator=source["orchestrator"],
        model=source["model"],
        raw_count=int(source["raw_count"]),
        findings_confirmed="[]",
        findings_rejected=source["findings_rejected"],
        submitted_by=source["submitted_by"],
        incomplete=False,
        unresolved="[]",
        lost_dimensions="[]",
        profile=source["profile"],
        principal_id=source["principal_id"],
        carried_from_review_id=ids[1],
        ci_integrity=("", ""),
    )
    await db.commit()
    assert await _action(db, task_id) != ACCEPT


async def _copy(db, task_id: int, generation: int, source_id: int, **over) -> int:
    """Копия переноса #1361 в поколении ``generation``; ссылка схлопнута до корня."""
    source = dict(await repo.get_machine_review(db, source_id) or {})
    carried = await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill=source["harness_skill"],
        harness_version=source["harness_version"],
        agent_count=source["agent_count"],
        orchestrator=source["orchestrator"],
        model=source["model"],
        raw_count=int(source["raw_count"]),
        findings_confirmed=over.get("findings_confirmed", "[]"),
        findings_rejected=source["findings_rejected"],
        submitted_by=source["submitted_by"],
        incomplete=False,
        unresolved=over.get("unresolved", "[]"),
        lost_dimensions=over.get("lost_dimensions", "[]"),
        profile=source["profile"],
        principal_id=source["principal_id"],
        carried_from_review_id=source_id,
        ci_integrity=("", ""),
    )
    await db.commit()
    return carried


@pytest.mark.parametrize("middle_is_clean", [True, False])
async def test_a_double_carry_unfolds_every_generation(
    client, db: aiosqlite.Connection, monkeypatch, middle_is_clean: bool
):
    """gen1 чистый A; gen2: B + копия A; gen3: повторный перенос (ссылка — на A).

    Промежуточное поколение не теряется: если в нём был B с unresolved — не «Принять».
    """
    task_id, ids = await _task(
        client, db, monkeypatch, f"ap-double-{middle_is_clean}", 2
    )
    a, b = ids
    unresolved = "[]" if middle_is_clean else '[{"title":"не проверено","why":"x"}]'
    await db.execute(
        "UPDATE machine_reviews SET submission_generation=2, unresolved=? WHERE id=?",
        (unresolved, b),
    )
    await db.execute(
        "UPDATE machine_reviews SET submission_generation=1 WHERE id=?", (a,)
    )
    await _copy(db, task_id, 2, a)
    await _copy(db, task_id, 3, a)
    await db.execute("UPDATE tasks SET submission_generation=3 WHERE id=?", (task_id,))
    await db.commit()
    assert (await _action(db, task_id) == ACCEPT) is middle_is_clean


@pytest.mark.parametrize(
    "over",
    [
        {"unresolved": '[{"title":"в копии","why":"x"}]'},
        {"lost_dimensions": '["security: files not read"]'},
        {
            "findings_confirmed": (
                '[{"title":"в копии","severity":"high","category":"c","file":"a.py",'
                '"line":1,"locator":"lines","start_line":1,"detail":"d"}]'
            )
        },
        {"unresolved": "не json"},
    ],
)
async def test_the_carried_copy_itself_is_read_and_judged(
    client, db: aiosqlite.Connection, monkeypatch, over: dict
):
    """Копия — тоже отчёт: её разделы читаются строго и проходят белый список."""
    task_id, ids = await _task(
        client, db, monkeypatch, f"ap-copy-{len(str(over))}-{sorted(over)[0]}", 1
    )
    task = dict(await repo.get_task(db, task_id) or {})
    generation = int(task["submission_generation"])
    await db.execute(
        "UPDATE machine_reviews SET submission_generation=? WHERE id=?",
        (generation - 1, ids[0]),
    )
    await _copy(db, task_id, generation, ids[0], **over)
    assert await _action(db, task_id) != ACCEPT


async def test_every_display_path_is_free_of_git_fetch(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Запрет на уровне ВСЕХ путей показа: build, входящие, блок карточки и полный GET."""
    from hub import services, web

    task_id, _ = await _task(client, db, monkeypatch, "ap-nofetch-all", 1)
    calls: list[str] = []

    def spy(name: str):
        async def _spy(*_a, **_k):
            calls.append(name)
            return ("b" * 40) if name == "head_sha" else (True, "")

        return _spy

    monkeypatch.setattr(plugins.git_ops, "fetch_base", spy("fetch_base"))
    monkeypatch.setattr(plugins.git_ops, "head_sha", spy("head_sha"))
    task = dict(await repo.get_task(db, task_id) or {})
    await decision_brief.build(db, task)
    await services.get_inbox_data(db)
    await web._card_decision_brief(db, task, False)
    assert calls == [], f"пути справки зовут git: {calls}"
    assert (await client.get(f"/tasks/{task_id}")).status_code == 200
    assert calls == [], f"полный GET /tasks/{{id}} зовёт git: {calls}"


async def test_completed_without_delivery_data_does_not_claim_a_merge(
    db: aiosqlite.Connection,
):
    from tests.test_decision_brief_fresh_review import _build, _completed

    got = await _build(db, await _completed(db))
    assert got["recommend"]["action"] == "Проверить"
    assert "влит" not in (got["happened"] + " ".join(got["recommend"]["why"])).lower()
