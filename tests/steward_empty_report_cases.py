"""Пустой машинный отчёт и самостоятельный approve стюарда: тела тестов (#2082).

Тела лежат здесь, а в ``test_steward_applied`` и ``test_effective_policy`` на
них только ссылки: эти файлы у потолка размера, а критерии приёмки называют
тесты по этим файлам.
"""

from __future__ import annotations

import aiosqlite
import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub.models import ReviewVerdict
from hub.services.steward_applied import apply_judgement

__all__ = [
    "test_an_empty_report_without_proven_work_is_not_self_approved",
    "test_empty_report_rule_is_shared_with_the_autopilot",
]


async def _everything(db: aiosqlite.Connection) -> dict[str, list]:
    """Все строки всех таблиц: чистое чтение не меняет ни одной."""
    tables = [
        r[0]
        for r in await (
            await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    ]
    out: dict[str, list] = {}
    for name in sorted(tables):
        cur = await db.execute(f"SELECT * FROM {name}")  # noqa: S608 — имена из sqlite_master
        out[name] = [tuple(r) for r in await cur.fetchall()]
    return out


async def test_an_empty_report_without_proven_work_is_not_self_approved(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-1: raw_count=0 и работа ревьюера не доказана - approve не записан, причина названа.

    Ответы провайдера подставлены: недоступен, usage ниже порога, токены
    расходятся с отчётом. Живой путь поллера и прямой ``apply_judgement`` дают
    один отказ; повторный тик строку не дублирует.
    """
    from hub.services import verdict_evidence
    from hub.services.steward_advisor import apply_advisor_outcomes
    from tests.steward_double_clean_support import feed
    from tests.steward_empty_report_support import DOWN, PROVEN, empty_case

    monkeypatch.setattr(config, "PROVEN_EMPTY_MAX_CLASS", "r1")
    monkeypatch.setattr(config, "EMPTY_REVIEW_MIN_USAGE", 200_000)
    scenes = {
        "down": (DOWN, PROVEN),
        "below_floor": (100, 100),
        "mismatch": (PROVEN, 10),
    }
    for label, (usage, tokens) in scenes.items():
        case, _calls = await empty_case(
            db, monkeypatch, f"empty-{label}", usage=usage, tokens_spent=tokens
        )
        assert await apply_advisor_outcomes(db) == 1, label

        task = dict(await repo.get_task(db, case.task_id))
        assert task["status"] == "review", label
        assert not (task.get("review_verdict") or ""), label
        assert task["verdict_owner"] == "human", label
        assert task["verdict_owner_reason"].startswith("no_data_report"), label

        def lines(text: str) -> list[str]:
            return [ln for ln in text.splitlines() if "пустой отчёт — нет данных" in ln]

        assert len(lines(await feed(db, case.task_id))) == 1, label
        shown = verdict_evidence.format_block(
            await verdict_evidence.build(db, case.task_id, 1)
        )
        assert "пустой отчёт — нет данных" in shown, label

        assert await apply_advisor_outcomes(db) == 0, label
        assert len(lines(await feed(db, case.task_id))) == 1, label

        with pytest.raises(HTTPException) as direct:
            await apply_judgement(db, case.task_id, 1)
        assert getattr(direct.value, "code", "") == "no_data_report", label
        assert not (
            dict(await repo.get_task(db, case.task_id)).get("review_verdict") or ""
        ), label

    # Контроль: работа доказана, класс в пределах - approve применён, провайдер
    # спрошен один раз за всё применение.
    case, calls = await empty_case(db, monkeypatch, "empty-proven")
    assert await apply_advisor_outcomes(db) == 1
    task = dict(await repo.get_task(db, case.task_id))
    assert task["review_verdict"] == ReviewVerdict.approved.value
    assert len(calls) == 1, calls


async def test_empty_report_rule_is_shared_with_the_autopilot(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-3: класс сдачи и потолок решает общий с автопилотом предикат, без копии условия."""
    from hub.services import auto_verdict
    from hub.services.steward_advisor import apply_advisor_outcomes
    from tests.steward_empty_report_support import PROVEN, empty_case

    decide = auto_verdict.empty_report_decision
    review = {"raw_count": 0, "tokens_spent": PROVEN, "id": 1}
    for ceiling, risk, usage, code in (
        ("r1", "R1", PROVEN, ""),
        ("r1", "R2", PROVEN, "above_proven_empty_ceiling"),
        ("r1", "", PROVEN, "above_proven_empty_ceiling"),
        ("", "R0", PROVEN, "above_proven_empty_ceiling"),
        ("r9", "R0", PROVEN, "above_proven_empty_ceiling"),
        ("r1", "R1", None, "no_data_report"),
    ):
        monkeypatch.setattr(config, "PROVEN_EMPTY_MAX_CLASS", ceiling)
        got = decide({"risk_class": risk}, review, usage, checked=True)
        assert got.code == code, (ceiling, risk, usage, got)
    monkeypatch.setattr(config, "PROVEN_EMPTY_MAX_CLASS", "r1")
    assert not decide({"risk_class": "R3"}, {"raw_count": 2}, None, checked=True).code

    # Стюард: те же классы приводят к тому же применению или отказу.
    for risk, applied in (("R1", True), ("R2", False)):
        case, _calls = await empty_case(db, monkeypatch, f"shared-{risk}", risk=risk)
        assert await apply_advisor_outcomes(db) == 1
        task = dict(await repo.get_task(db, case.task_id))
        if applied:
            assert task["review_verdict"] == ReviewVerdict.approved.value
        else:
            assert not (task.get("review_verdict") or "")
            assert "above_proven_empty_ceiling" in task["verdict_owner_reason"]

    # Одно правило: подмена предиката меняет исход и у автопилота, и у стюарда.
    calls: list[str] = []

    def sentinel(task, report, usage, *, checked):
        calls.append("asked")
        return auto_verdict.EmptyReportDecision(code="sentinel_code", reason="подмена")

    monkeypatch.setattr(auto_verdict, "empty_report_decision", sentinel)
    case, _calls = await empty_case(db, monkeypatch, "shared-sentinel")
    assert await apply_advisor_outcomes(db) == 1
    owner = dict(await repo.get_task(db, case.task_id))["verdict_owner_reason"]
    assert "sentinel_code" in owner, owner
    report = dict(await repo.get_latest_machine_review(db, case.task_id))
    task = dict(await repo.get_task(db, case.task_id))
    ctx = auto_verdict._Ctx(
        task=task, project=None, generation=1, observe=False, review=report
    )
    stance = await auto_verdict._empty_report_stage(db, case.task_id, ctx)
    assert stance is not None and stance.code == "sentinel_code"
    assert len(calls) >= 2


async def test_an_empty_report_blocks_candidate_readiness(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#2082): кандидат с пустым отчётом не ready - blocked с кодом, если
    # класс выше потолка пустого ревью, и unknown, пока биллинг не проверен;
    # без сетевого запроса и без записи в базу. Маршрут вердикта называет то же.
    from hub.integrations import cursor_cloud
    from hub.services.verdict_route import verdict_route
    from tests.steward_empty_report_support import empty_case

    monkeypatch.setattr(config, "PROVEN_EMPTY_MAX_CLASS", "r1")
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    proofless, _ = await empty_case(db, monkeypatch, "ready-empty-r1", risk="R1")
    above, _ = await empty_case(db, monkeypatch, "ready-empty-r2", risk="R2")
    seen, _ = await empty_case(db, monkeypatch, "ready-seen", risk="R1", raw_count=2)

    network: list[str] = []

    async def _no_network(*_args, **_kwargs):
        network.append("get_usage")
        raise AssertionError("readiness must not reach the provider")

    monkeypatch.setattr(cursor_cloud, "get_usage", _no_network)
    before = await _everything(db)
    writes: list[str] = []
    real_execute = aiosqlite.Connection.execute

    def _watch(self, sql, *args, **kwargs):
        if sql.lstrip().split(" ", 1)[0].upper() in (
            "INSERT",
            "UPDATE",
            "DELETE",
            "REPLACE",
        ):
            writes.append(sql[:80])
        return real_execute(self, sql, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "execute", _watch)
    for slug in ("ready-empty-r1", "ready-empty-r2", "ready-seen"):
        resp = await client.get(f"/api/projects/{slug}/effective-policy")
        assert resp.status_code == 200
    monkeypatch.setattr(aiosqlite.Connection, "execute", real_execute)
    assert network == [] and writes == []
    assert await _everything(db) == before

    got: dict[int, dict] = {}
    for slug in ("ready-empty-r1", "ready-empty-r2", "ready-seen"):
        body = (await client.get(f"/api/projects/{slug}/effective-policy")).json()
        for item in body["steward"]["readiness"]["candidates"]:
            got[item["task_id"]] = item
    assert got[proofless.task_id]["state"] == "unknown"
    assert got[proofless.task_id]["reasons"][0]["code"] == "no_data_report"
    assert got[above.task_id]["state"] == "blocked"
    assert got[above.task_id]["reasons"][0]["code"] == "above_proven_empty_ceiling"
    assert got[seen.task_id]["state"] == "ready"

    # Маршрут вердикта (общий для REST, MCP и CLI) называет условие и запрет.
    from tests.steward_double_clean_support import grant_act

    from hub.services import steward_shadow

    await grant_act(monkeypatch)
    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    real_mode = steward_shadow.mode_report

    async def _act_mode(_db):
        return {**await real_mode(_db), "effective": "act"}

    monkeypatch.setattr(steward_shadow, "mode_report", _act_mode)
    for case in (proofless, above):
        await repo.update_task(db, case.task_id, submission_model="claude-opus-5")
    waiting = await verdict_route(db, proofless.task_id)
    assert waiting.decider == "steward" and "биллинг" in waiting.condition
    forbidden = await verdict_route(db, above.task_id)
    assert forbidden.final == "human"
    assert forbidden.code == "above_proven_empty_ceiling"
    assert network == []
