"""Импорт выгрузки usage Cursor владельцем (#1413).

Живая проба 27.09: API агента заказа 420 видит один прогон и 1,95 млн
токенов, а выгрузка Cursor по тому же Cloud Agent ID — 17 событий и 13,58 млн.
Полная цена deep есть только в выгрузке, и её приносит владелец. Строки ниже
синтетические, того же формата; реальных ID агентов здесь нет.
"""

from __future__ import annotations

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.services.cursor_usage_import import (
    MAX_CSV_CHARS,
    REQUIRED_COLUMNS,
    UsageImportRefused,
    import_cursor_usage,
)
from hub.services.orchestration import practice_metrics

HEADER = ",".join(REQUIRED_COLUMNS)
DEEP_AGENT = "bc-test-deep-0001"
LITE_AGENT = "bc-test-lite-0002"
FOREIGN_AGENT = "bc-test-foreign-0003"
DEEP_TOKENS = [800_000 + 1_000 * i for i in range(17)]


def _line(agent: str, minute: int, tokens: int, model: str = "grok-4.6") -> str:
    date = f"2026-09-25T10:{minute:02d}:00.000Z"
    return (
        f'"{date}","{agent}","","Included","{model}","No",'
        f'"{tokens // 2}","{tokens // 4}","{tokens // 8}","1000","{tokens}","Included"'
    )


def _export(*lines: str) -> str:
    return "\n".join([HEADER, *lines]) + "\n"


def _deep_lines(start: int = 0, stop: int = 17) -> list[str]:
    return [_line(DEEP_AGENT, i, DEEP_TOKENS[i]) for i in range(start, stop)]


FOREIGN = [_line(FOREIGN_AGENT, 1, 5_000), _line("", 2, 7_000)]


async def _order(db: aiosqlite.Connection, agent_id: str, *, api: int | None) -> int:
    task_id = await repo.create_task(
        db,
        title=f"review of {agent_id}",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status="draft",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    did = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id=agent_id,
        run_id="run",
        model="grok-4.6",
        profile="deep" if "deep" in agent_id else "lite",
        channel="cloud",
    )
    if api is not None:
        await repo.set_review_dispatch_provider_tokens(db, did, api)
    await repo.set_review_dispatch_status(db, did, "done")
    await db.commit()
    return did


async def _row(db: aiosqlite.Connection, did: int) -> dict:
    rows = await db.execute_fetchall(
        "SELECT * FROM review_dispatches WHERE id=?", (did,)
    )
    return dict(rows[0])


async def _events(db: aiosqlite.Connection) -> int:
    rows = await db.execute_fetchall("SELECT COUNT(*) FROM cursor_usage_events")
    return int(rows[0][0])


async def test_export_bills_review_orders_by_agent_id(db: aiosqlite.Connection):
    """AC-4: сухой прогон — план; запись — сумма 17 строк, API-число цело."""
    did = await _order(db, DEEP_AGENT, api=1_825_481)
    text = _export(*_deep_lines(), *FOREIGN)

    dry = await import_cursor_usage(db, text, apply=False)
    assert dry["apply"] is False and dry["written"] == 0
    assert await _events(db) == 0
    assert (await _row(db, did))["billed_tokens"] is None
    plan = dry["orders"][0]
    assert (plan["dispatch_id"], plan["billed_events"]) == (did, 17)
    assert plan["billed_tokens"] == sum(DEEP_TOKENS)

    done = await import_cursor_usage(db, text, apply=True)
    assert done["written"] == 17
    row = await _row(db, did)
    assert (row["billed_tokens"], row["billed_events"]) == (sum(DEEP_TOKENS), 17)
    assert row["provider_tokens"] == 1_825_481, "API-число не стирается"
    assert done["foreign"] == {"events": 2, "tokens": 12_000, "agents": 2}
    assert await _events(db) == 17, "строки чужих агентов не пишутся"


async def test_import_is_idempotent_strict_and_human_only(
    client, db: aiosqlite.Connection, monkeypatch
):
    """AC-5: повтор и перекрытие не удваивают; битый файл — отказ; агент — 403."""
    did = await _order(db, DEEP_AGENT, api=1_825_481)
    await import_cursor_usage(db, _export(*_deep_lines(0, 10)), apply=True)
    again = await import_cursor_usage(db, _export(*_deep_lines(5, 17)), apply=True)
    assert (again["written"], again["duplicate_events"]) == (7, 5)
    await import_cursor_usage(db, _export(*_deep_lines()), apply=True)
    row = await _row(db, did)
    assert (row["billed_tokens"], row["billed_events"]) == (sum(DEEP_TOKENS), 17)

    no_columns = "Date,Cloud Agent ID,Model\n" + '"2026-09-25","x","m"\n'
    with pytest.raises(UsageImportRefused) as missing:
        await import_cursor_usage(db, no_columns, apply=True)
    assert missing.value.code == "missing_columns"
    assert "Total Tokens" in missing.value.extra["columns"]

    bad = _export(
        _line(DEEP_AGENT, 40, 1).replace('"1","Included"', '"n/a","Included"')
    )
    with pytest.raises(UsageImportRefused) as broken:
        await import_cursor_usage(db, bad, apply=True)
    assert broken.value.code == "bad_tokens"
    assert broken.value.extra["lines"] == [2]
    assert await _events(db) == 17, "битый файл ничего не записал"

    with pytest.raises(UsageImportRefused) as huge:
        await import_cursor_usage(db, "x" * (MAX_CSV_CHARS + 1), apply=False)
    assert huge.value.code == "too_large"

    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "admin-token": TokenIdentity("owner", "admin"),
            "agent-token": TokenIdentity("bot", "agent"),
        },
    )
    url = "/api/admin/cursor-usage/import"
    body = {"csv": _export(_line(LITE_AGENT, 1, 10))}
    refused = await client.post(
        url, json=body, headers={"Authorization": "Bearer agent-token"}
    )
    assert refused.status_code == 403
    dry = await client.post(
        url, json=body, headers={"Authorization": "Bearer admin-token"}
    )
    assert dry.status_code == 200, dry.text
    assert dry.json()["apply"] is False
    rejected = await client.post(
        url,
        json={"csv": no_columns, "apply": True},
        headers={"Authorization": "Bearer admin-token"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "missing_columns"
    assert await _events(db) == 17


async def test_review_economy_prefers_billed_tokens_and_names_coverage(
    db: aiosqlite.Connection,
):
    """AC-6: выгрузка, где есть; иначе API как нижняя граница; доля покрытия."""
    await _order(db, DEEP_AGENT, api=1_825_481)
    await _order(db, "bc-test-deep-0004", api=2_000_000)
    await import_cursor_usage(db, _export(*_deep_lines()), apply=True)

    runs = (await practice_metrics(db))["review_economy"]["runs"]
    deep = next(r for r in runs["by_profile"] if r["profile"] == "deep")
    assert deep["provider_tokens_total"] == sum(DEEP_TOKENS) + 2_000_000
    assert (deep["export_billed_runs"], deep["api_lower_bound_runs"]) == (1, 1)
    assert deep["export_coverage_share"] == 0.5
    assert runs["export_coverage_share"] == 0.5
    assert "нижняя граница" in runs["cost_source_note"]


def test_cli_import_is_dry_unless_apply_is_named(tmp_path, capsys):
    from unittest.mock import MagicMock, patch

    from hub import cli

    export = tmp_path / "usage.csv"
    export.write_text(_export(_line(LITE_AGENT, 1, 10)), encoding="utf-8")
    payload = {
        "apply": False,
        "rows": 1,
        "written": 0,
        "new_events": 1,
        "duplicate_events": 0,
        "foreign": {"events": 0, "tokens": 0, "agents": 0},
        "orders": [
            {
                "dispatch_id": 7,
                "task_id": 3,
                "agent_id": LITE_AGENT,
                "profile": "lite",
                "api_tokens": 9,
                "new_events": 1,
                "billed_events": 1,
                "billed_tokens": 10,
            }
        ],
    }
    api = MagicMock(return_value=payload)
    parser = cli.build_parser()
    args = parser.parse_args(["cursor-usage-import", str(export)])
    with patch.object(cli, "_api", api):
        assert args.func(args) == 0
    method, path, body = api.call_args.args
    assert (method, path, body["apply"]) == (
        "POST",
        "/api/admin/cursor-usage/import",
        False,
    )
    assert body["csv"].startswith(HEADER)
    out = capsys.readouterr().out
    assert "сухой прогон" in out and "заказ #7" in out

    args = parser.parse_args(["cursor-usage-import", str(export), "--apply"])
    with patch.object(cli, "_api", api):
        args.func(args)
    assert api.call_args.args[2]["apply"] is True


async def _report(db: aiosqlite.Connection, did: int) -> None:
    row = await _row(db, did)
    await repo.insert_machine_review(
        db,
        task_id=row["task_id"],
        submission_generation=1,
        harness_skill="multi-agent-review",
        raw_count=0,
        findings_confirmed="[]",
        incomplete=False,
        profile=row["profile"],
    )
    await db.execute(
        "UPDATE tasks SET submission_generation = 1 WHERE id = ?", (row["task_id"],)
    )


async def test_export_bill_counts_as_paid_wherever_a_bill_is_checked(
    db: aiosqlite.Connection,
):
    """Находка 169548cc1e7fa359: API промолчал, выгрузка есть — счёт получен."""
    silent = await _order(db, DEEP_AGENT, api=None)
    await _report(db, silent)
    # Сдача, где рядом с оплаченным по выгрузке заказом лежит неоплаченный:
    # отчёт оплачен выгрузкой, в «счёт не получен» он не идёт.
    mixed = await _order(db, LITE_AGENT, api=None)
    await _report(db, mixed)
    stray = await repo.create_review_dispatch(
        db,
        task_id=(await _row(db, mixed))["task_id"],
        submission_generation=1,
        agent_id="bc-test-stray-0005",
        run_id="run",
        model="grok-4.6",
        profile="lite",
        channel="cloud",
    )
    await repo.set_review_dispatch_status(db, stray, "done")
    failed = await _order(db, "bc-test-failed-0006", api=None)
    await repo.set_review_dispatch_status(db, failed, "failed")
    await db.commit()
    lines = [
        *_deep_lines(0, 2),
        _line(LITE_AGENT, 30, 300),
        _line("bc-test-failed-0006", 31, 700),
    ]
    await import_cursor_usage(db, _export(*lines), apply=True)

    metrics = await practice_metrics(db)
    rec = metrics["review_economy"]["reconciliation"]
    buckets = {b["bucket"]: b["count"] for b in rec["buckets"]}
    assert buckets["dispatch_without_bill"] == 0
    assert buckets["unexplained"] == 0
    wasted = metrics["review_dispatches"]
    assert (wasted["wasted_dispatches"], wasted["wasted_provider_tokens_total"]) == (
        1,
        700,
    )
    assert wasted["unknown_usage"] == 1, "только stray: ни API, ни выгрузки"
