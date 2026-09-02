"""The hub counts whether a finding's place was touched (#1039).

A commit that edited the named lines is a fact. It is not a disposition:
the hub never pre-checks «исправлено» and never writes finding_dispositions.
Absence of a clone or a sha is «ответа нет», never «не тронуто» (#762).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.services.finding_evidence import (
    OUTCOME_TOUCHED,
    OUTCOME_UNKNOWN,
    OUTCOME_UNTOUCHED,
    REASON_LOCATOR_NONE,
    REASON_NO_CLONE,
    REASON_SHA_MISSING,
    REASON_TIP_MISSING,
    finding_touch_evidence,
)

_FILE = "hub/target.py"
_FINDING_LINES = {
    "title": "guard drops the flag",
    "severity": "medium",
    "category": "correctness",
    "locator": "lines",
    "file": _FILE,
    "start_line": 5,
    "end_line": 6,
    "line": 5,
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
            "HOME": str(repo),
        },
        check=True,
    )


def _sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write_numbered(path: Path, *, tweak: int | None = None) -> None:
    lines = [f"line-{i}\n" for i in range(1, 21)]
    if tweak is not None:
        lines[tweak - 1] = f"changed-{tweak}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines))


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init")
    _git(root, "config", "user.email", "t@e")
    _git(root, "config", "user.name", "t")
    _write_numbered(root / _FILE)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "baseline")
    return root


async def _task_on_clone(
    db: aiosqlite.Connection, clone: Path, *, title: str = "evidence"
) -> int:
    project_id = await repo.create_project(
        db,
        slug=f"ev-{title.replace(' ', '-')[:20]}",
        name=title,
        workspace_path=str(clone),
        status="active",
    )
    task_id = await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status="review",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, project_id=project_id)
    return task_id


async def _report_on(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    generation: int,
    sha: str,
    finding: dict | None = None,
) -> None:
    await repo.record_submission(
        db, task_id=task_id, generation=generation, sha=sha, base_branch="main"
    )
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        findings_confirmed=json.dumps([finding or _FINDING_LINES]),
        incomplete=False,
    )
    await db.commit()


async def test_a_commit_on_the_lines_is_reported(
    db: aiosqlite.Connection, tmp_path: Path
):
    # AC-1: a later commit that edited the named lines is «место тронуто»
    # with that commit's sha and subject.
    clone = _init_repo(tmp_path / "touched")
    baseline = _sha(clone)
    task_id = await _task_on_clone(db, clone, title="touched lines")
    await _report_on(db, task_id, generation=1, sha=baseline)

    _write_numbered(clone / _FILE, tweak=5)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "narrow the guard")
    fix = _sha(clone)

    evidence = await finding_touch_evidence(db, task_id, _FINDING_LINES, generation=1)
    assert evidence.outcome == OUTCOME_TOUCHED
    shas = [c.sha for c in evidence.commits]
    assert fix in shas
    assert any("narrow the guard" in c.subject for c in evidence.commits)


async def test_a_commit_elsewhere_in_the_file_is_not_evidence(
    db: aiosqlite.Connection, tmp_path: Path
):
    # AC-2: the same file, different lines — that commit is not evidence.
    clone = _init_repo(tmp_path / "elsewhere")
    baseline = _sha(clone)
    task_id = await _task_on_clone(db, clone, title="elsewhere in file")
    await _report_on(db, task_id, generation=1, sha=baseline)

    _write_numbered(clone / _FILE, tweak=18)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "unrelated footer")
    other = _sha(clone)

    evidence = await finding_touch_evidence(db, task_id, _FINDING_LINES, generation=1)
    assert evidence.outcome == OUTCOME_UNTOUCHED
    assert other not in [c.sha for c in evidence.commits]
    assert evidence.commits == ()


async def test_absence_is_never_reported_as_untouched(
    db: aiosqlite.Connection, tmp_path: Path
):
    # AC-3: no clone / missing sha / locator=none are «ответа нет», never
    # the negative fact «не тронуто».
    clone = _init_repo(tmp_path / "present")
    baseline = _sha(clone)
    task_id = await _task_on_clone(db, clone, title="unknown paths")
    await _report_on(db, task_id, generation=1, sha=baseline)

    empty = await repo.create_project(
        db, slug="ev-empty-ws", name="empty", workspace_path="", status="active"
    )
    await repo.update_task(db, task_id, project_id=empty)
    await db.commit()
    no_clone = await finding_touch_evidence(db, task_id, _FINDING_LINES, generation=1)
    assert no_clone.outcome == OUTCOME_UNKNOWN
    assert no_clone.reason == REASON_NO_CLONE
    assert no_clone.outcome != OUTCOME_UNTOUCHED

    missing_sha = await _task_on_clone(db, clone, title="no submission sha")
    await repo.insert_machine_review(
        db,
        task_id=missing_sha,
        submission_generation=1,
        findings_confirmed=json.dumps([_FINDING_LINES]),
        incomplete=False,
    )
    await db.commit()
    no_sha = await finding_touch_evidence(db, missing_sha, _FINDING_LINES, generation=1)
    assert no_sha.outcome == OUTCOME_UNKNOWN
    assert no_sha.reason == REASON_SHA_MISSING
    assert no_sha.outcome != OUTCOME_UNTOUCHED

    none_finding = {**_FINDING_LINES, "locator": "none", "file": "", "line": None}
    none_finding.pop("start_line")
    none_finding.pop("end_line")
    located_none = await finding_touch_evidence(db, task_id, none_finding, generation=1)
    assert located_none.outcome == OUTCOME_UNKNOWN
    assert located_none.reason == REASON_LOCATOR_NONE
    assert located_none.outcome != OUTCOME_UNTOUCHED


async def test_the_baseline_is_the_reports_own_generation(
    db: aiosqlite.Connection, tmp_path: Path
):
    # AC-4: a resubmit overwrites tasks.submission_sha. The early report
    # still measures from its own submissions row.
    clone = _init_repo(tmp_path / "resubmit")
    gen1 = _sha(clone)
    task_id = await _task_on_clone(db, clone, title="early generation")
    await _report_on(db, task_id, generation=1, sha=gen1)

    _write_numbered(clone / _FILE, tweak=5)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "fix on gen1")
    mid = _sha(clone)

    extra = clone / "hub" / "other.py"
    extra.write_text("unrelated\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "second submission")
    gen2 = _sha(clone)
    await repo.record_submission(
        db, task_id=task_id, generation=2, sha=gen2, base_branch="main"
    )
    await repo.update_task(db, task_id, submission_sha=gen2)
    await db.commit()

    evidence = await finding_touch_evidence(db, task_id, _FINDING_LINES, generation=1)
    assert evidence.outcome == OUTCOME_TOUCHED
    shas = [c.sha for c in evidence.commits]
    assert mid in shas
    # Measuring from the overwritten tip would hide the gen1..gen2 work.
    assert gen2 not in shas or mid in shas


async def test_evidence_never_decides(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: Path
):
    # AC-5: even «место тронуто» does not pre-check a radio or insert a
    # disposition row. The human still decides (#876).
    clone = _init_repo(tmp_path / "no-decide")
    baseline = _sha(clone)
    task_id = await _task_on_clone(db, clone, title="fact is not a verdict")
    await _report_on(db, task_id, generation=1, sha=baseline)
    _write_numbered(clone / _FILE, tweak=5)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "touch the lines")
    await db.commit()

    evidence = await finding_touch_evidence(db, task_id, _FINDING_LINES, generation=1)
    assert evidence.outcome == OUTCOME_TOUCHED

    review = await repo.get_latest_machine_review(db, task_id)
    stored = await repo.list_finding_dispositions(db, int(review["id"]))
    assert stored == []

    page = (await client.get(f"/tasks/{task_id}")).text
    assert "Место тронуто после отчёта" in page
    assert "исправление дефекта" not in page.lower()
    # Radios exist for the human; none of them is pre-checked.
    assert 'value="fixed"' in page
    assert 'value="fixed"\n                                        checked' not in page
    assert "checked" not in page.split('value="fixed"')[1][:80]


async def test_card_shows_touch_fact_not_a_fix(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path: Path
):
    # AC-6: the owner sees the fact beside each finding, worded as a touch
    # of the place, not as a claim that the defect was fixed.
    clone = _init_repo(tmp_path / "queue-card")
    baseline = _sha(clone)
    task_id = await _task_on_clone(db, clone, title="mixed facts")
    footer = {
        **_FINDING_LINES,
        "title": "footer leak",
        "start_line": 18,
        "end_line": 19,
        "line": 18,
    }
    unplaced = {"title": "cannot point", "severity": "low", "locator": "none"}
    await _report_on(
        db,
        task_id,
        generation=1,
        sha=baseline,
        finding=None,
    )
    await db.execute(
        "UPDATE machine_reviews SET findings_confirmed=? WHERE task_id=?",
        (json.dumps([_FINDING_LINES, footer, unplaced]), task_id),
    )
    _write_numbered(clone / _FILE, tweak=5)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "touch named lines")
    await db.commit()

    page = (await client.get(f"/tasks/{task_id}")).text
    assert "Место тронуто после отчёта" in page
    assert "Место не тронуто после отчёта" in page
    assert "Ответа нет:" in page
    assert "исправление дефекта" not in page.lower()


# ---------------------------------------------------------------------------
# #1150 — вершина задаётся явно, когда решение принимается О СДАЧЕ
# ---------------------------------------------------------------------------


async def test_an_explicit_head_bounds_the_walk(
    db: aiosqlite.Connection, tmp_path: Path
):
    """Явная вершина обрезает обход там, где сказано, а не на живой ветке.

    Карточка и очередь спрашивают «трогал ли кто-нибудь находку с тех пор»,
    и им нужна ветка. Решение О СДАЧЕ спрашивает другое — «несёт ли ЭТА
    сдача правку», — и ветка тут движущаяся цель: к моменту вопроса она
    может стоять не там, где стояла сдача (#572). Проверяется настоящим
    репозиторием: правка есть в ветке и её нет в закреплённом коммите.
    """
    from hub.services.finding_evidence import evidence_for_report
    from hub.services.finding_identity import finding_uids

    clone = _init_repo(tmp_path / "clone")
    baseline = _sha(clone)
    task_id = await _task_on_clone(db, clone, title="explicit head")
    await _report_on(db, task_id, generation=1, sha=baseline)

    # Сдача закрепила коммит, в котором находка ещё не тронута.
    _write_numbered(clone / _FILE, tweak=19)
    _git(clone, "commit", "-am", "правка в стороне от находки")
    pinned = _sha(clone)

    # А ветка уехала дальше, и ТАМ находка тронута.
    _write_numbered(clone / _FILE, tweak=5)
    _git(clone, "commit", "-am", "правка ровно в месте находки")

    uid = finding_uids([_FINDING_LINES])[0]

    by_branch = await evidence_for_report(db, task_id, [_FINDING_LINES], generation=1)
    assert by_branch[uid]["outcome"] == OUTCOME_TOUCHED, (
        "по живой ветке правка видна — это ответ для карточки"
    )

    by_pinned = await evidence_for_report(
        db, task_id, [_FINDING_LINES], generation=1, head=pinned
    )
    assert by_pinned[uid]["outcome"] == OUTCOME_UNTOUCHED, (
        "сдача этой правки не несёт, и решение о ней обязано читать её sha"
    )


async def test_a_head_that_is_not_in_the_clone_is_unknown(
    db: aiosqlite.Connection, tmp_path: Path
):
    """Ненайденный коммит — «не удалось посмотреть», а не «не тронуто».

    Разница здесь стоит целого прогона: отказ по незнанию отнимает у сдачи
    суждение, а лишний прогон стоит только денег (#762).
    """
    from hub.services.finding_evidence import evidence_for_report
    from hub.services.finding_identity import finding_uids

    clone = _init_repo(tmp_path / "clone")
    baseline = _sha(clone)
    task_id = await _task_on_clone(db, clone, title="absent head")
    await _report_on(db, task_id, generation=1, sha=baseline)

    uid = finding_uids([_FINDING_LINES])[0]
    out = await evidence_for_report(
        db, task_id, [_FINDING_LINES], generation=1, head="f" * 40
    )

    assert out[uid]["outcome"] == OUTCOME_UNKNOWN
    assert out[uid]["reason"] == REASON_TIP_MISSING, (
        "причина обязана называть, ЧЕГО не нашли: без явной проверки ответ "
        "всё равно приходит unknown, но с git_failed — а «коммита нет» и "
        "«git сломался» человек чинит по-разному"
    )
