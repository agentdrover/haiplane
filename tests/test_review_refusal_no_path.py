"""Отказ облачного ревьюера не публикует путь сервера (#2006).

Остаток класса #2002: сырой текст исключения транспорта Cursor
(``PermissionError: ... '/srv/reviewer/private.pem'``) шёл в ``Refusal.detail``
и дальше в alert ленты, долг второй двери, headline/reason_detail, бриф, MCP,
инбокс и карточку. Тесты проводят НАСТОЯЩИЙ отказ через ``cursor_cloud._attempt``
и диспетчер, а читают со всех поверхностей сразу.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import aiosqlite
import httpx
from httpx import AsyncClient

from hub import mcp_server
from hub.services import lifecycle
from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.services import accept_proof
from hub.services.dashboard import get_inbox_data
from hub.services.review_availability import (
    REFUSAL_ALERT_PREFIXES,
    generation_review,
    observe_outage,
    refusal_detail,
)
from tests.review_dispatch_support import _DispatchRecorder, _submitted, _wire

SECRET = "/srv/reviewer/private.pem"
LEAK = "/srv/reviewer"


class _Reply:
    status_code = 200
    text = "{}"

    def json(self) -> dict[str, Any]:
        return {"items": []}


def _provider_that_fails(monkeypatch, exc: BaseException, *, reads: bool) -> None:
    """Настоящий ``_attempt`` поверх сети, которая бросает ``exc`` на создании.

    ``reads=True``: список агентов отвечает пусто (подтверждённая пустота —
    ветка «не нашлось»); ``False``: и чтение падает тем же (ветка «вслепую»).
    """

    class _Net:
        def __init__(self, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> _Net:
            return self

        async def __aexit__(self, *_a: Any) -> bool:
            return False

        async def request(self, method: str, _url: str, **_kw: Any) -> _Reply:
            if method == "POST" or not reads:
                raise exc
            return _Reply()

    shim = SimpleNamespace(AsyncClient=_Net, Response=httpx.Response)
    monkeypatch.setattr(cursor_cloud, "httpx", shim)


async def _refused_task(
    client, db, monkeypatch, slug: str, exc: BaseException, *, reads: bool = True
) -> int:
    real = cursor_cloud.create_agent_attempt
    _wire(monkeypatch, _DispatchRecorder(None))
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", real)
    _provider_that_fails(monkeypatch, exc, reads=reads)
    return await _submitted(
        client, db, slug, policy={"review": "dispatch", "verdict": "human"}
    )


async def _surfaces(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, task_id: int
) -> dict[str, str]:
    """Каждая поверхность, где отказ виден людям и агентам, одной строкой."""
    out: dict[str, str] = {}
    for name, url in (
        ("card html", f"/tasks/{task_id}"),
        ("card htmx", f"/tasks/{task_id}"),
        ("api task", f"/api/tasks/{task_id}"),
        ("api updates", f"/api/tasks/{task_id}/updates"),
        ("api brief", f"/api/tasks/{task_id}/review-brief"),
        ("inbox partial", "/partials/inbox"),
    ):
        resp = await client.get(
            url, headers={"HX-Request": "true"} if name == "card htmx" else {}
        )
        assert resp.status_code == 200, (name, resp.text[:300])
        out[name] = resp.text

    async def api_get(path: str, *_a: Any, **_k: Any) -> Any:
        resp = await client.get(path)
        assert resp.status_code == 200, (path, resp.text[:300])
        return resp.json()

    monkeypatch.setattr(mcp_server, "_api_get", api_get)
    for section in ("", "verdict"):
        brief = await mcp_server.hub_get_review_brief(task_id, section=section)
        out[f"mcp brief {section}"] = repr(brief)
    for full in (False, True):
        status = await mcp_server.hub_task_status(task_id, updates=-1, full=full)
        out[f"mcp status full={full}"] = repr(status)
    task = dict(await repo.get_task(db, task_id))
    out["generation_review"] = (await generation_review(db, task)).model_dump_json()
    _proof, blockers, _views = await accept_proof.prove_accept(db, task)
    out["accept_proof"] = repr(blockers)
    out["inbox data"] = json.dumps(await get_inbox_data(db), default=str)
    rows = await db.execute_fetchall(
        "SELECT * FROM review_dispatches WHERE task_id=?",
        (task_id,),
    )
    out["second-door debt"] = repr([dict(r) for r in rows])
    return out


def _assert_clean(surfaces: dict[str, str]) -> None:
    for name, text in surfaces.items():
        assert LEAK not in text, f"{name}: путь сервера опубликован"
        assert "private.pem" not in text, f"{name}: имя файла опубликовано"
        assert "Errno" not in text, f"{name}: сырой текст исключения опубликован"


async def test_a_transport_refusal_publishes_its_kind_not_its_path(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, caplog
):
    """AC-1: PermissionError и ReadTimeout различимы по виду, пути нет нигде."""
    caplog.set_level(logging.WARNING)
    denied = await _refused_task(
        client,
        db,
        monkeypatch,
        "refusal-denied",
        PermissionError(13, "Permission denied", SECRET),
    )
    timed_out = await _refused_task(
        client, db, monkeypatch, "refusal-timeout", httpx.ReadTimeout("")
    )
    denied_view = await _surfaces(client, db, monkeypatch, denied)
    timed_view = await _surfaces(client, db, monkeypatch, timed_out)

    _assert_clean(denied_view)
    _assert_clean(timed_view)
    assert "PermissionError" in denied_view["generation_review"]
    assert "ReadTimeout" not in denied_view["generation_review"]
    assert "ReadTimeout" in timed_view["generation_review"]
    assert "PermissionError" not in timed_view["generation_review"]
    assert "PermissionError" in denied_view["card html"], "вид отказа виден в ленте"
    assert "PermissionError" in denied_view["second-door debt"]
    assert SECRET in caplog.text, "полный текст остаётся в журнале хаба"


async def test_a_blind_transport_refusal_publishes_its_kind_not_its_path(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Вслепую (спросить агента не вышло): путь не в алерте, признак blind цел."""
    blind = await _refused_task(
        client,
        db,
        monkeypatch,
        "refusal-blind",
        PermissionError(13, "Permission denied", SECRET),
        reads=False,
    )
    view = await _surfaces(client, db, monkeypatch, blind)
    _assert_clean(view)
    assert "PermissionError" in view["card html"]
    assert "СПРОСИТЬ его" in view["card html"], "слепое состояние названо как раньше"
    rows = await db.execute_fetchall(
        "SELECT agent_id FROM review_dispatches WHERE task_id=?", (blind,)
    )
    assert [dict(r)["agent_id"] for r in rows] == [], "второй двери на слепом нет"


async def test_a_params_refusal_alert_does_not_carry_the_response_body(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Alert об отвергнутых параметрах берёт только вид ответа, не тело."""
    calls: list[int] = []
    body = cursor_cloud.Refusal(
        status=400, code="invalid_argument", detail=f"bad file {SECRET}"
    )

    async def attempt(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            return None, body
        return {"agent": {"id": "bc-1", "latestRunId": "r-1"}, "run": {}}, None

    _wire(monkeypatch, _DispatchRecorder(None))
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", attempt)

    async def params_for(model_id):
        return [cursor_cloud.ModelParam(id="fast", value="false")]

    monkeypatch.setattr(cursor_cloud, "review_params_for", params_for)
    task_id = await _submitted(
        client, db, "refusal-params", policy={"review": "dispatch"}
    )
    view = await _surfaces(client, db, monkeypatch, task_id)
    assert "провайдер отверг: HTTP 400" in view["card html"], "alert записан"
    stored = [dict(r)["content"] for r in await repo.get_task_updates(db, task_id)]
    assert not [c for c in stored if LEAK in c], "писатель не кладёт тело в базу"
    _assert_clean(view)


async def test_a_stored_refusal_with_a_path_is_read_without_it(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2: алерт, записанный ДО исправления, читается без пути, но как отказ."""
    task_id = await _submitted(
        client, db, "refusal-stored", policy={"review": "dispatch"}, verdict_auto=False
    )
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "review"
    stored = (
        f"{REFUSAL_ALERT_PREFIXES[1]}, и агента с нашей "
        "меткой у него не нашлось за попыток: 2 "
        f"(PermissionError: [Errno 13] Permission denied: '{SECRET}'). "
        "Вердикт остаётся человеку; детали в логе хаба (#757)."
    )
    await repo.add_task_update(db, task_id, "hub", "alert", stored)
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        "[переспрос ревью: сдача 1] Переспрос ревью 1 из 2: заказ #7 отчёта не "
        f"дал — провайдер отказал в создании агента: PermissionError {SECRET}. "
        "Отчёта этой сдачи нет вовсе.",
    )
    # Авторская запись с тем же путём - содержание, а не утечка: не трогать.
    await repo.add_task_update(
        db, task_id, "dev", "status", f"Читаю {SECRET} в тесте, это моя заметка"
    )
    stub = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="",
        run_id="",
        model="m",
        profile="lite",
        profile_assignment="rule",
        reviewer_principal_id=None,
        channel="cloud",
        replaces_dispatch_id=None,
        only_tests=None,
    )
    await repo.owe_second_door(
        db, stub, f"ответ не дошёл (PermissionError: '{SECRET}')"
    )
    await repo.set_review_dispatch_status(db, stub, "failed")
    await db.commit()

    view = await _surfaces(client, db, monkeypatch, task_id)
    note = f"Читаю {SECRET} в тесте, это моя заметка"
    for name, text in view.items():
        if name == "second-door debt":
            continue  # строка заглушки ниже читается через _cloud_refusal_text
        assert LEAK not in text.replace(note, ""), f"{name}: старый алерт с путём"
    assert note in view["api updates"].replace("\\/", "/"), "авторская запись цела"

    task = dict(await repo.get_task(db, task_id))
    seen = await generation_review(db, task)
    assert seen.reason == "provider_refused", "отказ распознан как отказ"
    assert "PermissionError" in seen.reason_detail
    assert LEAK not in seen.headline
    assert refusal_detail(stored).startswith("ответ провайдера не дошёл")
    assert LEAK not in refusal_detail(stored)
    observed = await observe_outage(db)
    assert observed.kind in ("below_threshold", "too_young", "outage")


async def test_the_stored_stub_status_is_published_through_the_cloud_text():
    """_cloud_refusal_text чистит run_status заглушки, а настоящий прогон не меняет."""
    from hub.services.review_dispatch import _cloud_refusal_text

    stub = {"agent_id": ""}
    text = _cloud_refusal_text(stub, f"PermissionError: '{SECRET}'")
    assert LEAK not in text
    assert text.startswith("провайдер отказал в создании агента: ")
    real = {"agent_id": "bc-1", "model": "m"}
    assert "кончился статусом FINISHED" in _cloud_refusal_text(real, "FINISHED")


async def test_the_shared_refusal_keeps_its_detail_for_the_other_consumers():
    """Вне области (executor_launch, steward_shadow): detail прежний, kind рядом."""
    from hub.services.steward_shadow import _provider_answer

    raw = cursor_cloud.Refusal(
        status=0,
        kind="PermissionError",
        detail=f"PermissionError: [Errno 13] Permission denied: '{SECRET}'"[:300],
    )
    assert SECRET in raw.detail
    assert SECRET not in _provider_answer(raw), "steward_shadow публикует вид"
    http = cursor_cloud.Refusal(status=400, code="usage_limit_exceeded", detail="x")
    assert http.kind == "" and http.is_transport is False


async def test_the_real_transport_refusal_keeps_detail_and_gains_a_kind(monkeypatch):
    """_attempt: detail прежний (его читают executor_launch и steward_shadow), kind рядом."""
    monkeypatch.setattr(cursor_cloud.config, "CURSOR_API_KEY", "test-key")
    _provider_that_fails(
        monkeypatch, PermissionError(13, "Permission denied", SECRET), reads=True
    )
    body, refusal = await cursor_cloud._attempt("POST", "/v1/agents", {})
    assert body is None and refusal is not None
    assert (refusal.status, refusal.code, refusal.kind) == (0, "", "PermissionError")
    assert refusal.detail.startswith("PermissionError: [Errno 13]")
    assert SECRET in refusal.detail, (
        "сырой текст остался для вне-областных потребителей"
    )
    assert refusal.is_transport is True


def test_the_refusal_kind_keeps_http_codes_and_limits_distinguishable():
    from hub.services.refusal_publish import safe_detail

    Refusal = cursor_cloud.Refusal
    assert safe_detail(None) == "причина не названа"
    assert safe_detail(
        Refusal(status=400, code="usage_limit_exceeded", detail=SECRET)
    ) == ("HTTP 400, usage_limit_exceeded")
    assert safe_detail(Refusal(status=502, detail=f"gateway {SECRET}")) == "HTTP 502"
    assert safe_detail(Refusal(kind="ReadTimeout", detail="x")) == "ReadTimeout"
    assert safe_detail(Refusal(detail=f"сломалось в {SECRET}")) == "сломалось в <путь>"
    assert safe_detail(Refusal(detail="CURSOR_API_KEY не задан")) == (
        "CURSOR_API_KEY не задан"
    )


def test_the_headline_never_prints_a_path_from_any_reason_detail():
    from hub.models import GenerationReview
    from hub.services.review_availability import _headline

    view = GenerationReview(
        generation=2,
        reason="provider_refused",
        reason_detail=f"PermissionError '{SECRET}'",
    )
    line = _headline(view)
    assert LEAK not in line and "PermissionError" in line
    assert line.startswith("ревью у поколения 2 НЕТ: провайдер ревью отказал")


async def test_the_refusal_alert_keeps_its_prefix_and_authored_notes_stay_whole(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Признаки отказа не сдвинулись: префикс алерта, reason, второй двери нет."""
    from hub.services.refusal_publish import published_update

    task_id = await _refused_task(
        client,
        db,
        monkeypatch,
        "refusal-prefix",
        PermissionError(13, "Permission denied", SECRET),
    )
    rows = [dict(r) for r in await repo.get_task_updates(db, task_id)]
    alerts = [r for r in rows if r["kind"] == "alert" and r["agent"] == "hub"]
    refusal = [a for a in alerts if a["content"].startswith(REFUSAL_ALERT_PREFIXES)]
    assert REFUSAL_ALERT_PREFIXES == (
        "Кросс-модельное ревью НЕ вызвано: провайдер отказал",
        "Кросс-модельное ревью НЕ вызвано: ответ провайдера не дошёл",
    ), "по этим началам отказ узнают сторож, очередь и SQL"
    assert len(refusal) == 1, "ровно один алерт отказа с прежним началом"
    assert LEAK not in refusal[0]["content"], "писатель не кладёт путь в базу"
    assert (
        "Вердикт остаётся человеку; детали в логе хаба (#757)."
        in (refusal[0]["content"])
    )
    seen = await generation_review(db, dict(await repo.get_task(db, task_id)))
    assert seen.reason == "provider_refused"
    stub = await db.execute_fetchall(
        "SELECT run_status FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    assert LEAK not in repr([dict(r) for r in stub]), "долг второй двери без пути"
    # Чистятся только записи хаба-алерты; те же слова у человека не меняются.
    said = refusal[0]["content"] + f" {SECRET}"
    for author, kind in (("dev", "status"), ("dev", "alert"), ("hub", "status")):
        authored = {"kind": kind, "agent": author, "content": said}
        assert published_update(authored) == authored, (author, kind)
    old = {**refusal[0], "content": refusal[0]["content"] + f" {SECRET}"}
    assert LEAK not in published_update(old)["content"]


_STORED = (
    f"{REFUSAL_ALERT_PREFIXES[1]}, и агента с нашей меткой у него не нашлось за "
    f"попыток: 2 (PermissionError: [Errno 13] Permission denied: '{SECRET}'). "
    "Вердикт остаётся человеку; детали в логе хаба (#757)."
)


async def _bare_task(db: aiosqlite.Connection, status: str) -> int:
    task_id = await repo.create_task(
        db,
        title="stall",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status=status,
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()
    return task_id


async def test_the_inbox_stall_grounds_do_not_print_an_old_refusal_path(
    db: aiosqlite.Connection,
):
    """last_stall читает алерт отказа из ленты в обход update_views (Codex P2 #1)."""
    from hub.services.inbox_decisions import _decide_grounds
    from hub.services.review_queue import last_stall

    task_id = await _bare_task(db, "needs_decision")
    await repo.add_task_update(db, task_id, "hub", "alert", _STORED)
    await db.commit()
    reason, _at = await last_stall(db, task_id, "")
    assert LEAK not in reason and "PermissionError" in reason
    grounds = await _decide_grounds(db, {"id": task_id, "status_entered_at": ""})
    assert LEAK not in grounds and "PermissionError" in grounds


async def test_the_needs_info_inbox_rows_carry_no_old_refusal_path(
    db: aiosqlite.Connection,
):
    """get_inbox_data кладёт ленту needs_info-задач в ответ: тоже через публикацию."""
    task_id = await _bare_task(db, "needs_info")
    await repo.add_task_update(db, task_id, "hub", "alert", _STORED)
    await db.commit()
    data = json.dumps(await get_inbox_data(db), default=str)
    assert LEAK not in data and "PermissionError" in data


async def test_an_entry_called_hub_by_a_principal_keeps_its_path(
    client: AsyncClient, db: aiosqlite.Connection
):
    """Автор решает author_kind/principal_id, а не имя agent (Codex P2 #2)."""
    from hub.services.refusal_publish import published_update

    task_id = await _bare_task(db, "review")
    await repo.add_task_update(
        db, task_id, "hub", "alert", _STORED, principal_id=None, author_kind="principal"
    )
    await repo.add_task_update(
        db, task_id, "hub", "alert", _STORED, principal_id=7, author_kind="hub"
    )
    await repo.add_task_update(db, task_id, "hub", "alert", _STORED)
    await db.commit()
    shown = (await client.get(f"/api/tasks/{task_id}/updates")).json()
    by_len = [u["content"] for u in shown]
    assert [LEAK in c for c in by_len] == [True, True, False], by_len
    mine = {"kind": "alert", "agent": "hub", "author_kind": "principal"}
    assert published_update({**mine, "content": _STORED})["content"] == _STORED
    legacy = {"kind": "alert", "agent": "hub", "content": _STORED}
    assert published_update(legacy)["content"] == _STORED, "без author_kind - не хаб"


async def test_the_executor_launch_reason_names_the_kind_not_the_path(monkeypatch):
    """executor_launch больше не кладёт сырой detail в причину (Codex P2 #3)."""
    from hub.services import executor_launch as el

    raw = cursor_cloud.Refusal(
        kind="PermissionError",
        detail=f"PermissionError: [Errno 13] Permission denied: '{SECRET}'",
    )

    async def refuse(**_kw):
        return None, raw

    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", refuse)
    for asked in (True, False):

        async def ask(_name, pages=3, asked=asked):
            return cursor_cloud.Reconciliation("", "", asked)

        monkeypatch.setattr(cursor_cloud, "find_agent_by_name", ask)
        made = await el._create(1, 1, {})
        assert LEAK not in made.failed, made.failed
        assert "PermissionError" in made.failed
        wanted = el.REASON_ANSWER_LOST if asked else el.REASON_ANSWER_BLIND
        assert made.failed.startswith(wanted)


def test_the_steward_run_refusal_payload_names_the_kind_not_the_path():
    """steward_run_refused.payload.detail: вид отказа, не сырой текст (Codex P2 #4)."""
    from hub.services.steward_shadow import _provider_answer

    raw = cursor_cloud.Refusal(
        kind="PermissionError",
        detail=f"PermissionError: [Errno 13] Permission denied: '{SECRET}'",
    )
    assert _provider_answer(raw) == "без кода (PermissionError)"
    body = cursor_cloud.Refusal(
        status=400, code="usage_limit_exceeded", detail=f"в {SECRET}"
    )
    assert _provider_answer(body) == "HTTP 400 usage_limit_exceeded"
    assert _provider_answer(None) == "ответа не было"


_GATE_ERROR = (
    f"merge_gate_error: PermissionError: [Errno 13] Permission denied: '{SECRET}'"
)


async def test_a_stored_merge_gate_event_reaches_the_inbox_without_its_path(
    db: aiosqlite.Connection,
):
    """(a) событие needs_decision с merge_gate_error: last_stall чистит detail."""
    from hub.services.inbox_decisions import _decide_grounds
    from hub.services.review_queue import last_stall

    task_id = await _bare_task(db, "needs_decision")
    await repo.insert_event(
        db,
        kind="needs_decision",
        task_id=task_id,
        actor="hub",
        payload={"reason": "merge_gate", "detail": _GATE_ERROR, "via": "poller"},
    )
    await db.commit()
    reason, _at = await last_stall(db, task_id, "")
    assert LEAK not in reason and "PermissionError" in reason
    grounds = await _decide_grounds(db, {"id": task_id, "status_entered_at": ""})
    assert LEAK not in grounds
    stored = await db.execute_fetchall(
        "SELECT payload FROM events WHERE task_id=?", (task_id,)
    )
    assert SECRET in dict(stored[0])["payload"], "в базе payload как записан"


async def test_the_events_feed_serves_payloads_without_paths_and_keeps_the_rest(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """(b) /api/events и hub_wait_events: старое событие с путём; поля целы."""
    task_id = await _bare_task(db, "review")
    payload = {
        "reason": "judge_unlaunchable",
        "retryable": True,
        "detail": f"без кода (PermissionError: [Errno 13] '{SECRET}')",
        "model": "grok",
    }
    await repo.insert_event(
        db, kind="steward_run_refused", task_id=task_id, actor="hub", payload=payload
    )
    await db.commit()
    shown = (await client.get("/api/events?since=0")).json()
    event = [e for e in shown["events"] if e["kind"] == "steward_run_refused"][0]
    assert LEAK not in json.dumps(event, ensure_ascii=False)
    assert event["task_id"] == task_id and event["actor"] == "hub"
    assert event["payload"]["reason"] == "judge_unlaunchable"
    assert event["payload"]["retryable"] is True and event["payload"]["model"] == "grok"
    assert "PermissionError" in event["payload"]["detail"]

    async def api_get(path: str, *_a: Any, **_k: Any) -> Any:
        resp = await client.get(path)
        assert resp.status_code == 200, (path, resp.text[:300])
        return resp.json()

    monkeypatch.setattr(mcp_server, "_api_get", api_get)
    waited = await mcp_server.hub_wait_events(since=0, wait=0)
    assert LEAK not in repr(waited) and "steward_run_refused" in repr(waited)
    raw = await db.execute_fetchall(
        "SELECT payload FROM events WHERE kind='steward_run_refused'"
    )
    assert SECRET in dict(raw[0])["payload"], "в базе payload как записан"


async def test_an_old_executor_not_launched_alert_is_read_without_its_path(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """(c) «Исполнитель НЕ запущен: …» записан прежним executor_launch."""
    task_id = await _bare_task(db, "running")
    old = (
        "Исполнитель НЕ запущен: ответ на создание агента не дошёл: "
        f"PermissionError: [Errno 13] Permission denied: '{SECRET}'."
    )
    await repo.add_task_update(db, task_id, "hub", "alert", old)
    await db.commit()
    seen = await _surfaces_of_feed(client, monkeypatch, task_id)
    for name, text in seen.items():
        assert LEAK not in text, f"{name}: старый алерт с путём"
    assert "Исполнитель НЕ запущен" in seen["api updates"]


async def _surfaces_of_feed(client, monkeypatch, task_id: int) -> dict[str, str]:
    async def api_get(path: str, *_a: Any, **_k: Any) -> Any:
        resp = await client.get(path)
        assert resp.status_code == 200, (path, resp.text[:300])
        return resp.json()

    monkeypatch.setattr(mcp_server, "_api_get", api_get)
    out = {
        "card html": (await client.get(f"/tasks/{task_id}")).text,
        "api updates": (await client.get(f"/api/tasks/{task_id}/updates")).text,
        "api task": (await client.get(f"/api/tasks/{task_id}")).text,
        "mcp status": repr(await mcp_server.hub_task_status(task_id, updates=-1)),
        "mcp status full": repr(
            await mcp_server.hub_task_status(task_id, updates=-1, full=True)
        ),
    }
    return out


async def test_the_other_alerts_that_embed_an_exception_text_are_read_clean(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Алерты хаба с {exc}/{detail} из форджа и ОС, записанные до исправления."""
    task_id = await _bare_task(db, "running")
    for text in (
        f"Доставка отложена: PR #7 — merge_gate_error: OSError '{SECRET}'. ждём",
        f"Доставка не состоялась: PR #7 — {_GATE_ERROR}. Исправьте",
        f"Ревью одобрено, но PR #7 не доставлен — {_GATE_ERROR}. Решение за человеком",
        f"Доставка по решению человека НЕ выполнена: {_GATE_ERROR}. Условия те же",
        f"Уборка worktree не удалась: PermissionError '{SECRET}'. Сдача не прервана.",
        f"PR #7 влит, но рабочий каталог не возвращён на базовую ветку: '{SECRET}'",
        f"Developer-agent dispatch unavailable: OSError '{SECRET}'",
    ):
        await repo.add_task_update(db, task_id, "hub", "alert", text)
    await db.commit()
    seen = await _surfaces_of_feed(client, monkeypatch, task_id)
    for name, text in seen.items():
        assert LEAK not in text, f"{name}: алерт хаба с путём"


async def test_any_hub_authored_entry_is_scrubbed_and_agent_entries_are_not(
    client: AsyncClient, db: aiosqlite.Connection
):
    """Белый список по автору: новое начало алерта хаба с путём - без пути."""
    from hub.services.refusal_publish import published_update

    task_id = await _bare_task(db, "running")
    text = f"Совсем новый алерт без известного начала: сбой в '{SECRET}'"
    await repo.add_task_update(db, task_id, "hub", "alert", text)
    await repo.add_task_update(db, task_id, "hub", "status", text)
    await repo.add_task_update(
        db, task_id, "dev-agent", "alert", text, author_kind="principal", principal_id=3
    )
    await repo.add_task_update(
        db, task_id, "hub", "report", text, author_kind="principal", principal_id=3
    )
    await db.commit()
    shown = [
        u["content"] for u in (await client.get(f"/api/tasks/{task_id}/updates")).json()
    ]
    assert [LEAK in c for c in shown] == [False, False, True, True], shown
    legacy = {"kind": "alert", "agent": "hub", "content": text}
    assert published_update(legacy)["content"] == text, "без author_kind - не хаб"


def test_typical_hub_lines_come_out_unchanged():
    """public_reason не портит полезный текст хаба: sha, PR, ссылки, ветки, маршруты."""
    from hub.services.refusal_publish import published_update

    sha = "c" * 40
    lines = [
        f"PR #123 влит в develop, коммит {sha}.",
        f"CI на {sha[:12]}: success (run 998877, попытка 2)",
        "https://github.com/agentdrover/haiplane/pull/123 и "
        "https://github.com/agentdrover/haiplane/actions/runs/4567890",
        "base_updated: ветка task-2006/refusal-no-path влита с develop",
        "Файлы: hub/web.py, tests/test_x.py, docs/agent-context/system-map.md",
        "Карточка /tasks/12, API /api/tasks/12/updates, GET /v1/models",
        "Доставка отложена: PR #7 — CI ещё идёт. Попытка 1/3.",
    ]
    for line in lines:
        row = {"agent": "hub", "author_kind": "hub", "kind": "status", "content": line}
        assert published_update(row)["content"] == line, line
    long = ("слово " * 2000).strip()
    row = {"agent": "hub", "author_kind": "hub", "kind": "status", "content": long}
    assert published_update(row)["content"] == long, "длинный текст не режется"


async def test_the_task_result_text_is_published_without_a_path(
    client: AsyncClient, db: aiosqlite.Connection
):
    """result_text задачи (orchestration пишет туда error) читается без пути."""
    task_id = await _bare_task(db, "open")
    await repo.update_task(
        db, task_id, result_text=f"dispatch failed: OSError '{SECRET}' в /tasks/12"
    )
    await db.commit()
    api = (await client.get(f"/api/tasks/{task_id}")).json()
    assert LEAK not in api["result_text"] and "/tasks/12" in api["result_text"]
    card = (await client.get(f"/tasks/{task_id}")).text
    assert LEAK not in card
    stored = await db.execute_fetchall(
        "SELECT result_text FROM tasks WHERE id=?", (task_id,)
    )
    assert SECRET in dict(stored[0])["result_text"], "в базе как записано"


async def test_the_latest_alert_reader_publishes_without_a_path(
    client: AsyncClient, db: aiosqlite.Connection
):
    """_latest_alert: HTMX-заметка о доставке и баннер дашборда без пути."""
    from hub import web

    task_id = await _bare_task(db, "completed")
    await repo.update_task(db, task_id, pr_number=7)
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Доставка по решению человека НЕ выполнена: {_GATE_ERROR}.",
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"{lifecycle.REGISTRY_REFUSAL_PREFIX}: OSError '{SECRET}'",
    )
    await db.commit()
    note = await web._decide_delivery_note(db, task_id, web.DELIVER_DISPOSITION)
    assert "PR #7 НЕ доставлен" in note and "PermissionError" in note
    assert LEAK not in note
    page = await client.get(f"/?delivery_refused={task_id}")
    assert page.status_code == 200 and "OSError" in page.text
    assert LEAK not in page.text
