"""Пакет доказательств стюарда: чем он полон и чем честно пуст (#1074).

Пакет — единственный вход судьи, поэтому проверяется не «собирается ли он»,
а два свойства, ради которых он существует: в нём есть каждый источник из
закрытого множества контракта (#1022), и отсутствие данных в нём нельзя
прочитать как благополучный ответ (#762, #750).
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from hub import repository as repo
from hub.models import STEWARD_GROUND_SOURCES
from hub.services.ci_report import VALIDATION_PASS
from hub.services.steward_evidence import (
    ABSENT,
    NO_REPORT,
    PRESENT,
    REPORT_OTHER_GENERATION,
    build_evidence_packet,
    present,
)
from tests.test_finding_evidence import (
    _init_repo,
    _sha,
    _task_on_clone,
)


async def _submit(
    db: aiosqlite.Connection, task_id: int, *, generation: int, sha: str
) -> None:
    """Одна сдача: и запись в ledger, и закреплённые поля задачи."""
    await repo.record_submission(
        db, task_id=task_id, generation=generation, sha=sha, base_branch="main"
    )
    await repo.update_task(
        db, task_id, submission_generation=generation, submission_sha=sha
    )
    await db.commit()


async def _report(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    generation: int,
    sha: str,
    confirmed: list[dict] | None = None,
) -> None:
    await _submit(db, task_id, generation=generation, sha=sha)
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        findings_confirmed=json.dumps(confirmed or []),
        incomplete=False,
        raw_count=3,
    )
    await db.commit()


async def _green_ci(db: aiosqlite.Connection, task_id: int, sha: str) -> None:
    await repo.upsert_ci_run_report(
        db,
        task_id=task_id,
        head_sha=sha,
        ac_results="[]",
        validation_status=VALIDATION_PASS,
        validation_log="",
        reason="",
        reported_by="ci",
    )
    await db.commit()


async def test_packet_contains_all_sources(db: aiosqlite.Connection, tmp_path: Path):
    """#1074 AC-1: каждый источник закрытого множества стоит в пакете.

    Не «часть источников, какие удалось собрать»: источник, которого в пакете
    нет вовсе, и источник, про который сказано «данных нет», — разные
    утверждения, и только второе хаб действительно знает. Плюс brief, который
    несёт постановку и локаторы.
    """
    clone = _init_repo(tmp_path / "packet-full")
    sha = _sha(clone)
    task_id = await _task_on_clone(db, clone, title="packet full")
    await _report(db, task_id, generation=1, sha=sha)
    await _green_ci(db, task_id, sha)

    packet = await build_evidence_packet(db, task_id)

    assert packet is not None
    assert packet.generation == 1
    assert packet.brief is not None
    assert set(packet.facts) == set(STEWARD_GROUND_SOURCES)
    # Подготовленные факты присутствуют и несут значение, а не только состояние.
    report = packet.fact("machine_review_report")
    assert report.state == PRESENT
    assert report.value["generation"] == 1
    ci = packet.fact("ci_pinned_sha")
    assert ci.state == PRESENT
    assert ci.value["passed"] is True
    # Зависимостей у задачи нет — и это установленный факт, а не пробел.
    deps = packet.fact("dependency_state")
    assert deps.state == PRESENT
    assert deps.value["blocked_by"] == []


async def test_absent_report_is_absence_not_clean(
    db: aiosqlite.Connection, tmp_path: Path
):
    """#1074 AC-2: «отчёта нет» и «отчёт без находок» — разные состояния.

    Это тот самый промах, который #762 сделал на клоне, а харнесс v7 — на
    raw_count=0: пустота читалась как чистота. Здесь два пакета собираются
    рядом, и различие между ними видно программно, без разбора текста.
    """
    clone = _init_repo(tmp_path / "packet-absent")
    sha = _sha(clone)
    silent = await _task_on_clone(db, clone, title="no report at all")
    await _submit(db, silent, generation=1, sha=sha)
    clean = await _task_on_clone(db, clone, title="empty report")
    await _report(db, clean, generation=1, sha=sha, confirmed=[])

    no_report = (await build_evidence_packet(db, silent)).fact("machine_review_report")
    empty_report = (await build_evidence_packet(db, clean)).fact(
        "machine_review_report"
    )

    assert no_report.state == ABSENT
    assert no_report.reason == NO_REPORT
    assert not no_report.value
    assert empty_report.state == PRESENT
    assert empty_report.value["confirmed"] == []
    # Ни одно поле не позволяет спутать их: отсутствие не спеллится значением.
    assert no_report.state != empty_report.state
    assert "machine_review_report" in (await build_evidence_packet(db, silent)).absent_sources()
    assert "machine_review_report" not in (
        await build_evidence_packet(db, clean)
    ).absent_sources()


async def test_source_outside_closed_set_is_refused(db: aiosqlite.Connection):
    """#1074 AC-3: источник вне множества #1022 отвергается с его именем.

    Молча записанный чужой источник дал бы стюарду основание, которое хаб
    перепроверить не может, — ровно то, что пакет и закрывает.
    """
    with pytest.raises(ValueError) as err:
        present("chat_transcript", "переписка пары")

    assert "chat_transcript" in str(err.value)
    for source in STEWARD_GROUND_SOURCES:
        assert source in str(err.value)


async def test_stale_report_is_not_current_evidence(
    db: aiosqlite.Connection, tmp_path: Path
):
    """#1074 AC-4: отчёт прошлой генерации — отсутствие, а не отчёт.

    Он описывает другой код. Пакет называет генерацию, которую отчёт на самом
    деле покрывает, чтобы читатель видел причину, а не голое «нет».
    """
    clone = _init_repo(tmp_path / "packet-stale")
    sha = _sha(clone)
    task_id = await _task_on_clone(db, clone, title="stale report")
    await _report(db, task_id, generation=1, sha=sha)
    await _submit(db, task_id, generation=2, sha=sha)

    fact = (await build_evidence_packet(db, task_id)).fact("machine_review_report")

    assert fact.state == ABSENT
    assert fact.reason == REPORT_OTHER_GENERATION
    assert "1" in fact.detail and "2" in fact.detail
    assert not fact.value
