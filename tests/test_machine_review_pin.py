"""Отчёт ревью с доказуемой привязкой к сдаче: expected_generation + reviewed_sha (#1985).

Без пина отчёт получает ТЕКУЩЕЕ поколение в момент приёма, и отчёт, изученный на
старой сдаче, после пересдачи неотличим от отчёта о новой. С пином (оба поля)
хаб сверяет заявленные поколение и полный sha с таблицей ``submissions`` и с
текущим поколением задачи в той же транзакции, что и запись.
"""

from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import config
from hub import db as db_module
from hub import repository as repo
from hub.db import fetchall
from hub.models import MachineReviewSubmit
from hub.services import chat_pair
from hub.services import machine_review_intake as intake
from hub.services.review_dispatch import REPORT_FENCE, parse_report_block
from tests.test_auto_verdict import _CLEAN_REVIEW, _TIP, _submitted_task

OTHER_SHA = "b" * 40


async def _task(client: AsyncClient, db: aiosqlite.Connection, slug: str) -> int:
    config.AUTO_APPROVE_MAX_CLASS = "off"
    task_id = await _submitted_task(client, db, slug, None)
    row = await repo.get_task(db, task_id)
    assert row["submission_generation"] == 1 and row["submission_sha"] == _TIP
    return task_id


async def _post(client: AsyncClient, task_id: int, **pin) -> object:
    return await client.post(
        f"/api/tasks/{task_id}/machine-review", json={**_CLEAN_REVIEW, **pin}
    )


async def _rows(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    await db.commit()
    got = await fetchall(
        db, "SELECT * FROM machine_reviews WHERE task_id=?", (task_id,)
    )
    return [dict(r) for r in got]


async def _resubmit(dsn: str, task_id: int, generation: int, sha: str) -> None:
    """Пересдача на отдельном соединении: новое поколение и новая строка журнала."""
    conn = await db_module.connect(dsn)
    try:
        await repo.record_submission(
            conn, task_id=task_id, generation=generation, sha=sha, base_branch="main"
        )
        await repo.update_task(
            conn, task_id, submission_generation=generation, submission_sha=sha
        )
        await conn.commit()
    finally:
        await conn.close()


async def test_a_pinned_report_is_recorded_as_verified(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    """AC-1: совпавший пин записан проверенным и виден в брифе и на карточке."""
    task_id = await _task(client, db, "pin-ok")

    resp = await _post(
        client, task_id, expected_generation=1, reviewed_sha=_TIP.upper()
    )

    assert resp.status_code == 200, resp.text
    stored = resp.json()
    assert stored["pin_verified"] is True
    assert stored["reviewed_sha"] == _TIP  # нижний регистр, полный sha
    assert stored["submission_generation"] == 1
    rows = await _rows(db, task_id)
    assert [(r["pin_verified"], r["reviewed_sha"]) for r in rows] == [(1, _TIP)]

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert brief["machine_review"]["pin_verified"] is True
    assert brief["machine_review"]["reviewed_sha"] == _TIP
    text = (await client.get(f"/api/tasks/{task_id}/review-brief?full=0")).text
    assert "pin verified" in text or "pin_verified" in text

    card = await client.get(f"/tasks/{task_id}")
    assert card.status_code == 200
    assert "пин проверен" in card.text


async def test_a_stale_or_partial_pin_is_refused(
    client: AsyncClient, db: aiosqlite.Connection, db_dsn: str
) -> None:
    """AC-2: чужое поколение, чужой sha, гонка с пересдачей -> 409; одно поле -> 422."""
    task_id = await _task(client, db, "pin-stale")

    # Одно поле из двух: 422, ничего не записано.
    for partial in ({"expected_generation": 1}, {"reviewed_sha": _TIP}):
        resp = await _post(client, task_id, **partial)
        assert resp.status_code == 422, (partial, resp.text)
    # Формат: не 40 hex и поколение < 1.
    assert (
        await _post(client, task_id, expected_generation=1, reviewed_sha="abc")
    ).status_code == 422
    assert (
        await _post(client, task_id, expected_generation=0, reviewed_sha=_TIP)
    ).status_code == 422

    # Не тот sha на текущем поколении и не то поколение.
    for pin in (
        {"expected_generation": 1, "reviewed_sha": OTHER_SHA},
        {"expected_generation": 2, "reviewed_sha": _TIP},
    ):
        resp = await _post(client, task_id, **pin)
        assert resp.status_code == 409, (pin, resp.text)
        assert resp.json()["detail"]["reason"] == "review_pin_mismatch"

    # Пересдача между изучением диффа и отправкой: пин поколения 1 при текущем 2.
    await _resubmit(db_dsn, task_id, 2, OTHER_SHA)
    resp = await _post(client, task_id, expected_generation=1, reviewed_sha=_TIP)
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "review_pin_mismatch"
    assert await _rows(db, task_id) == []


async def test_a_resubmission_cannot_land_between_the_check_and_the_write(
    client: AsyncClient, db: aiosqlite.Connection, db_dsn: str, monkeypatch
) -> None:
    """Гонка на двух соединениях: сверка и INSERT держат один write-лок.

    Пока отчёт между сверкой и записью, пересдача на втором соединении не может
    записать (lock), а после коммита отчёта пересдача проходит. Второй порядок:
    пересдача успела раньше - отчёт получает 409 и не записывается.
    """
    task_id = await _task(client, db, "pin-race")
    await db.commit()
    body = MachineReviewSubmit(
        **{**_CLEAN_REVIEW, "expected_generation": 1, "reviewed_sha": _TIP}
    )
    blocked: list[str] = []
    real_insert = repo.insert_machine_review

    async def insert_while_resubmitting(*args, **kwargs):
        other = await db_module.connect(db_dsn)
        try:
            await other.execute("PRAGMA busy_timeout = 50")
            try:
                await repo.update_task(other, task_id, submission_generation=2)
                await other.commit()
                blocked.append("resubmission slipped in")
            except Exception as exc:  # noqa: BLE001 - locked is the expected outcome
                blocked.append(str(exc))
                await other.rollback()
        finally:
            await other.close()
        return await real_insert(*args, **kwargs)

    monkeypatch.setattr(repo, "insert_machine_review", insert_while_resubmitting)
    conn = await db_module.connect(db_dsn)
    try:
        view = await intake.record_machine_review(
            conn, task_id, body, principal_id=None, username="reviewer-x"
        )
    finally:
        await conn.close()
    assert view.pin_verified is True
    assert blocked and "locked" in blocked[0], blocked

    # Второй порядок: пересдача раньше записи.
    task2 = await _task(client, db, "pin-race-late")
    await db.commit()
    await _resubmit(db_dsn, task2, 2, OTHER_SHA)
    conn = await db_module.connect(db_dsn)
    try:
        with pytest.raises(Exception) as err:
            await intake.record_machine_review(
                conn, task2, body, principal_id=None, username="reviewer-x"
            )
    finally:
        await conn.close()
    assert getattr(err.value, "status_code", None) == 409
    assert await _rows(db, task2) == []

    # Одновременно: что бы ни победило, записанный пин не врёт.
    task3 = await _task(client, db, "pin-race-gather")
    await db.commit()

    async def report() -> object:
        c = await db_module.connect(db_dsn)
        try:
            return await intake.record_machine_review(
                c, task3, body, principal_id=None, username="reviewer-x"
            )
        except Exception as exc:  # noqa: BLE001
            return exc
        finally:
            await c.close()

    monkeypatch.setattr(repo, "insert_machine_review", real_insert)
    await asyncio.gather(report(), _resubmit(db_dsn, task3, 2, OTHER_SHA))
    for row in await _rows(db, task3):
        assert row["pin_verified"] == 1 and row["reviewed_sha"] == _TIP
        assert row["submission_generation"] == 1


async def _reviewer_session_headers(
    client: AsyncClient, db: aiosqlite.Connection, task_id: int, generation: int
) -> dict[str, str]:
    await db.execute(
        "INSERT OR IGNORE INTO principals (id, kind, username, status) "
        "VALUES (41, 'agent', 'pin-reviewer', 'active')"
    )
    code, _ = await chat_pair.issue_code(
        db, 41, kind="reviewer", bound_task_id=task_id, bound_generation=generation
    )
    await db.commit()
    resp = await client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": code},
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def test_unpinned_senders_keep_working(
    client: AsyncClient, db: aiosqlite.Connection, db_dsn: str, monkeypatch
) -> None:
    """AC-3: без пина приём прежний; пин тела не заменяет ограничение сессии."""
    task_id = await _task(client, db, "pin-compat")

    # Существующий отправитель без пина.
    plain = await _post(client, task_id)
    assert plain.status_code == 200, plain.text
    assert plain.json()["pin_verified"] is False
    assert plain.json()["reviewed_sha"] == ""

    # Reviewer chat-pair без пина тела - как раньше; пин тела совпал - проверен.
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    auth = await _reviewer_session_headers(client, db, task_id, 1)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review", json=_CLEAN_REVIEW, headers=auth
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pin_verified"] is False
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={**_CLEAN_REVIEW, "expected_generation": 1, "reviewed_sha": _TIP},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pin_verified"] is True

    # Сессия выписана на поколение 1, задача пересдана: прежний 403, пин тела
    # (даже верный для нового поколения) его не отменяет.
    await _resubmit(db_dsn, task_id, 2, OTHER_SHA)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={**_CLEAN_REVIEW, "expected_generation": 2, "reviewed_sha": OTHER_SHA},
        headers=auth,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["reason"] == "chat_pair_generation_moved"

    # Сессия верна, тело расходится с submissions - новый 409, не 403.
    fresh = await _reviewer_session_headers(client, db, task_id, 2)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={**_CLEAN_REVIEW, "expected_generation": 2, "reviewed_sha": _TIP},
        headers=fresh,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason"] == "review_pin_mismatch"


async def test_text_reports_and_carried_copies_never_claim_a_pin(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    """Пин из текста рана игнорируется, копия переноса его не наследует."""
    task_id = await _task(client, db, "pin-text")
    text = (
        f"```{REPORT_FENCE}\n"
        + json.dumps({**_CLEAN_REVIEW, "reviewed_sha": _TIP})
        + "\n```"
    )
    parsed = parse_report_block(text)
    assert parsed is not None  # одно поле из текста не губит отчёт
    assert parsed.reviewed_sha is None and parsed.expected_generation is None

    stored = await intake.record_machine_review(
        db,
        task_id,
        MachineReviewSubmit(**{**_CLEAN_REVIEW}),
        principal_id=None,
        username="x",
        origin="run_text",
    )
    assert stored.pin_verified is False

    copied = await repo.insert_machine_review(
        db, task_id=task_id, submission_generation=1, carried_from_review_id=stored.id
    )
    await db.commit()
    row = [r for r in await _rows(db, task_id) if r["id"] == copied][0]
    assert (row["pin_verified"], row["reviewed_sha"]) == (0, "")
