"""MCP-проекция брифа ревью с догрузкой разделов (#1682).

Бриф на проде: медиана ~37 тыс. знаков, максимум 207 тыс.; клиент не берёт такой
ответ и показывает агенту 0-2 КБ. Проверяется то, ради чего проекция заведена:
ответ влезает в потолок, измеренный на транспортном результате; решающая часть
не усечена и не превращена в «полно/нет»; разделы склеиваются обратно в целое;
REST, пакет стюарда и packet_hash не тронуты.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
from typing import Any
from unittest.mock import patch

import pytest

from hub import cli, hub_instance, mcp_server
from hub import repository as repo
from hub.models import ReviewBrief
from hub.services import review_brief_projection as proj
from hub.services.steward_evidence import (
    build_evidence_packet,
    packet_hash,
    packet_payload,
)
from tests.test_mcp_server import stateless_hub  # noqa: F401  (fixture, via usefixtures)

_HARD = "Кавычки \"двойные\" и 'одинарные', обратный слэш C:\\путь\\файл, \tтаб\nперевод строки\n"


def _prose(n: int, tag: str) -> str:
    return (tag + " " + _HARD) * n


def _entries(count: int, addresses: int, *, width: int = 120) -> list[dict[str, Any]]:
    states = ["untouched_sites", "only_tests", "no_callers", "all_touched"]
    return [
        {
            "symbol": f"hub.services.module_{i}.symbol_{i}",
            "defined_in": f"hub/services/module_{i}.py",
            "state": states[i % 4],
            "statement": _prose(3, f"s{i}"),
            "total_sites": addresses + 3 if i % 4 == 0 else 3,
            "untouched": [
                f"hub/some/deep/package/path_{i}_{j}.py:{j + 10}:" + "x" * width
                for j in range(addresses if i % 4 == 0 else 0)
            ],
        }
        for i in range(count)
    ]


def _acs(count: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"AC-{i}",
            "given": _prose(2, "given")[:480],
            "when": _prose(2, "when")[:480],
            "then": f"Результат {i}: " + _prose(5, "then")[:480],
            "verifiable_by": "test",
            "test_ref": f"tests/test_x.py::test_{i}",
        }
        for i in range(1, count + 1)
    ]


def _machine(confirmed: int, unresolved: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": 7,
        "task_id": 1,
        "submission_generation": 2,
        "profile": "deep",
        "findings_confirmed": [
            {
                "title": f"Находка {i} " + "т" * 100,
                "severity": "high" if i % 2 else "low",
            }
            for i in range(confirmed)
        ],
        "findings_rejected": [
            {"title": f"Снята {i}", "reason": "не воспроизводится"} for i in range(3)
        ],
        "unresolved": [
            {"title": f"Не разобрана {i}", "why": "верификатор упал"}
            for i in range(unresolved)
        ],
        "lost_dimensions": ["security"],
        **extra,
    }


def _brief(
    *,
    ac: int = 25,
    symbols: int = 60,
    addresses: int = 20,
    prose: int = 60,
    confirmed: int = 12,
    unresolved: int = 3,
    **over: Any,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "task_id": 1647,
        "title": "Бриф: «кавычки» и \\ обратные",
        "status": "review",
        "description": _prose(prose, "description"),
        "technical_hints": _prose(prose, "hints"),
        "scope_in": [_prose(2, f"in{i}") for i in range(10)],
        "scope_growth": [_prose(5, f"g{i}") for i in range(20)],
        "latest_submission_summary": _prose(prose, "summary"),
        "acceptance_criteria": _acs(ac),
        "call_sites": {
            "status": "analysed",
            "summary": "12 символов; эвристика: поиск по имени, не по типам",
            "note": "динамические вызовы не видны",
            "entries": _entries(symbols, addresses),
            "only_tests_state": "named",
            "only_tests": [
                {
                    "symbol": "hub.services.module_1.symbol_1",
                    "outcome": "cleared",
                    "call_path": "tools/call",
                }
            ],
        },
        "submission_generation": 2,
        "submission_sha": "a" * 40,
        "current_branch_tip": "a" * 40,
        "sha_check": "match",
        "sha_check_reason": "tip равен сданному",
        "branch": "task-1647/x",
        "latest_review": {
            "verdict": "changes_requested",
            "submission_generation": 1,
            "is_current": False,
            "findings": [{"id": 1, "severity": "high", "message": "m"}],
        },
        "machine_review": _machine(confirmed, unresolved, incomplete=None),
        "evidence_coverage": {
            "state": "partial",
            "headline": "частично",
            "checks_missing": [{"check": "ci_run_report", "reason": "нет отчёта"}],
        },
        "ci_run_report": {
            "state": "unknown",
            "reason": "нет отчёта",
            "trust": "unverified",
            "trust_reason": "digest не совпал",
            "provenance": {"run_id": "42"},
            "provenance_note": "со слов репортёра",
        },
    }
    data.update(over)
    return ReviewBrief(**data).model_dump(mode="json")


SHAPES = {
    "1647": lambda: _brief(),
    "1652": lambda: _brief(ac=14, symbols=25, addresses=9, prose=25, confirmed=4),
    "1664": lambda: _brief(
        ac=9, symbols=8, addresses=5, prose=8, confirmed=0, unresolved=0
    ),
}


@pytest.fixture
def served(monkeypatch):
    """_api_get отдаёт заданный бриф; транспорт, auth и сериализатор настоящие."""
    box: dict[str, Any] = {}

    async def fake_get(path: str, **_kw: Any) -> Any:
        return copy.deepcopy(box["brief"])

    monkeypatch.setattr(mcp_server, "_api_get", fake_get)
    # Эхо экземпляра входит в конверт и в бюджет знаков: длина server_id - это
    # имя хоста. Фиксируем значения, чтобы число показанных AC не зависело от
    # машины (#1985 Nit, testing-playbook #1851).
    monkeypatch.setattr(
        hub_instance,
        "instance_echo_fields",
        lambda: {
            "instance": "prod",
            "base_url": "https://agenthai.ru",
            "server_id": "vm-5c8197",
        },
    )
    return box


async def _mcp(hub, name: str = "hub_get_review_brief", **args: Any) -> dict[str, Any]:
    from tests.test_mcp_server import (
        _AGENT_TOKEN,
        _call_tool,
        _hub_process,
        _rpc_result,
    )

    async with _hub_process():
        resp = await _call_tool(hub, _AGENT_TOKEN, name, {"task_id": 1647, **args})
    return _rpc_result(resp)


def _wire_chars(result: dict[str, Any]) -> int:
    return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


def _logged(caplog) -> list[dict[str, Any]]:
    rows = []
    for rec in caplog.records:
        if rec.getMessage().startswith("review_brief_mcp"):
            rows.append(dict(p.split("=", 1) for p in rec.getMessage().split()[1:]))
    return rows


def _text(result: dict[str, Any]) -> str:
    return json.loads(result["content"][0]["text"])["message"]


# --- AC-1 ---------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.usefixtures("stateless_hub")
async def test_mcp_brief_fits_the_budget_and_keeps_the_mandatory_fields(
    client,
    served,
    caplog,
    shape,  # noqa: F811
):
    """AC-1: ответ на проводе ≤ 12 000 знаков, решающие поля на месте, размер в логе."""
    brief = SHAPES[shape]()
    served["brief"] = brief
    assert len(json.dumps(brief, ensure_ascii=False)) > 30_000, (
        "фикстура — большой бриф"
    )
    caplog.set_level(logging.INFO, logger=proj.__name__)

    result = await _mcp(client)

    wire = _wire_chars(result)
    assert wire <= 12_000, wire
    row = _logged(caplog)[-1]
    assert int(row["chars"]) == wire, "в логе та же величина, что на проводе"
    assert row["mode"] == "projection" and row["offset"] == "0"
    out = result["structuredContent"]["brief"]
    for key in (
        "task_id", "title", "status", "submission_generation", "submission_sha",
        "branch", "current_branch_tip", "sha_check", "sha_check_reason",
        "self_review_warning", "current_generation_review", "evidence_coverage",
        "ci_run_report", "ci_integrity",
    ):  # fmt: skip
        assert out[key] == brief[key], key
    assert out["ci_run_report"]["trust"] == "unverified"
    assert out["ci_run_report"]["trust_reason"] == "digest не совпал"
    assert out["latest_review"]["verdict"] == "changes_requested"
    assert out["latest_review"]["is_current"] is False
    assert out["review_report"] == (
        {"state": brief["review_report"]["state"]} if brief["review_report"] else None
    )
    mr, src = out["machine_review"], brief["machine_review"]
    for key in (
        "id",
        "is_current",
        "outcome",
        "profile",
        "incomplete",
        "incomplete_reason",
    ):
        assert mr[key] == src[key], key
    assert mr["incomplete"] is None, "«не заявлено» не стало «полно»"
    assert mr["findings_confirmed_count"] == len(src["findings_confirmed"])
    assert mr["findings_rejected_count"] == len(src["findings_rejected"])
    assert mr["unresolved_count"] == len(src["unresolved"])
    assert mr["lost_dimensions_count"] == 1
    blocks = out["blocks"]
    for name in ("acceptance_criteria", "call_sites"):
        block = blocks[name]
        assert block["shown"] + block["omitted"] == block["total"], name
        assert block["omitted"] == 0 or block["hint"], (
            "урезанный блок называет параметр"
        )
    if shape == "1664":
        # #1985: pin_verified - решающее поле machine_review и стоит знаков;
        # на границе бюджета число показанных AC зависит от длины имени хоста
        # в конверте (server_id); эхо зафиксировано в фикстуре served.
        assert blocks["acceptance_criteria"]["shown"] == 9
    text = _text(result)
    assert text.startswith("Review brief for task #1647")
    assert "AC-1:" in text or blocks["acceptance_criteria"]["shown"] == 0
    assert "digest не совпал" in text


@pytest.mark.usefixtures("stateless_hub")
async def test_every_issue_is_logged_with_its_mode(
    client,
    served,
    caplog,  # noqa: F811
):
    """AC-1: размер пишется при КАЖДОЙ выдаче — проекция, раздел, full, превышение."""
    served["brief"] = SHAPES["1647"]()
    caplog.set_level(logging.INFO, logger=proj.__name__)
    results = {
        "projection": await _mcp(client),
        "section": await _mcp(client, section="call_sites"),
        "full": await _mcp(client, section="full"),
    }
    rows = {r["mode"]: r for r in _logged(caplog)}
    for mode, result in results.items():
        assert int(rows[mode]["chars"]) == _wire_chars(result), mode
    assert rows["section"]["section"] == "call_sites"
    assert int(rows["full"]["chars"]) > 12_000, "full — прежний ответ без проекции"


# --- AC-2 ---------------------------------------------------------------------


def test_projection_never_hides_findings_or_unknown_states():
    """AC-2: состояния передаются как есть; находки — severity и title."""
    base = _brief(
        review_report={"state": "stale", "machine_review": None},
        sha_check="diverged",
        sha_check_reason="ветка ушла вперёд",
        current_branch_tip="b" * 40,
        ci_integrity={
            "state": "suspicious",
            "gap": "",
            "note": "n",
            "answer": "",
            "question": "q",
            "signs": [{"path": "ci.yml", "line": 3, "rule": "r"}],
        },  # fmt: skip
        evidence_coverage={
            "state": "none",
            "headline": "ничего не проверено",
            "checks_missing": [
                {"check": f"c{i}", "reason": f"нет {i}"} for i in range(8)
            ],
        },
    )
    before = copy.deepcopy(base)
    result = proj.brief_result(base)
    assert base == before, "исходный бриф не мутирован"
    out = result.structuredContent["brief"]
    text = json.loads(result.content[0].text)["message"]

    assert out["machine_review"]["incomplete"] is None
    assert "unknown (never stated)" in text and "incomplete=no" not in text
    assert out["review_report"]["state"] == "stale"
    assert (
        out["sha_check"] == "diverged"
        and out["sha_check_reason"] == "ветка ушла вперёд"
    )
    assert out["current_branch_tip"] == "b" * 40
    assert out["evidence_coverage"] == base["evidence_coverage"], "причины целиком"
    assert out["ci_integrity"] == base["ci_integrity"]
    confirmed = out["machine_review"]["findings_confirmed"]
    assert (
        confirmed["total"] == 12
        and confirmed["shown"] == 10
        and confirmed["omitted"] == 2
    )
    assert all(
        set(f) >= {"severity", "title"} and len(f["title"]) <= 200
        for f in confirmed["items"]
    )
    unresolved = out["machine_review"]["unresolved"]
    assert unresolved["total"] == 3 and unresolved["shown"] == 3
    assert "… and 2 more" in text and "Unresolved findings: 3" in text

    incomplete = _brief(
        machine_review=_machine(0, 2, incomplete=True, incomplete_reason="environment"),
    )
    got = proj.brief_result(incomplete).structuredContent["brief"]["machine_review"]
    assert got["incomplete"] is True and got["incomplete_reason"] == "environment"
    assert got["findings_confirmed_count"] == 0 and got["unresolved_count"] == 2
    assert got["unresolved"]["items"], "нет подтверждённых, но неразобранные видны"

    none_at_all = _brief(machine_review=None)
    assert (
        proj.brief_result(none_at_all).structuredContent["brief"]["machine_review"]
        is None
    )


# --- AC-3 ---------------------------------------------------------------------


def test_call_sites_projection_lists_five_once_with_counts():
    """AC-3: 12 мест, 9 незатронутых: shown=5, omitted=4, 5 адресов один раз."""
    twelve = {
        "symbol": "hub.twelve", "defined_in": "hub/t.py", "state": "untouched_sites",
        "statement": "s", "total_sites": 12,
        "untouched": [f"hub/site_{i}.py:{i + 1}" for i in range(9)],
    }  # fmt: skip
    others = [
        {"symbol": "hub.t_only", "defined_in": "hub/a.py", "state": "only_tests",
         "statement": "s", "total_sites": 2, "untouched": []},
        {"symbol": "hub.nobody", "defined_in": "hub/b.py", "state": "no_callers",
         "statement": "s", "total_sites": 0, "untouched": []},
        {"symbol": "hub.done", "defined_in": "hub/c.py", "state": "all_touched",
         "statement": "s", "total_sites": 4, "untouched": []},
    ]  # fmt: skip
    brief = _brief(
        ac=2,
        confirmed=1,
        unresolved=0,
        call_sites={
            "status": "analysed",
            "summary": "эвристика по имени",
            "note": "динамика не видна",
            "entries": [*others, twelve],
        },
    )
    result = proj.brief_result(brief)
    block = result.structuredContent["brief"]["blocks"]["call_sites"]
    row = next(r for r in block["items"] if r["symbol"] == "hub.twelve")
    assert (row["total_sites"], row["untouched_total"]) == (12, 9)
    assert (row["shown"], row["omitted"]) == (5, 4)
    assert row["shown"] + row["omitted"] == row["untouched_total"]
    assert row["untouched"] == twelve["untouched"][:5]
    assert [r["state"] for r in block["items"]][0] == "untouched_sites", (
        "проблемные первыми"
    )
    assert {r["state"] for r in block["items"]} == {
        "untouched_sites", "only_tests", "no_callers", "all_touched"
    }  # fmt: skip
    assert block["caveats"]["summary"] == "эвристика по имени"
    assert block["caveats"]["note"] == "динамика не видна"
    text = json.loads(result.content[0].text)["message"]
    for address in twelve["untouched"][:5]:
        assert text.count(address) == 1, address
    assert twelve["untouched"][5] not in text

    many = _brief(
        ac=2,
        confirmed=1,
        unresolved=0,
        call_sites={"status": "analysed", "entries": _entries(25, 3)},
    )
    got = proj.brief_result(many).structuredContent["brief"]["blocks"]["call_sites"]
    assert got["total"] == 25 and got["shown"] <= 10
    assert got["shown"] + got["omitted"] == 25


# --- AC-4 ---------------------------------------------------------------------


def _pages(brief: dict[str, Any], section: str) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    offset, expect = 0, ""
    while offset is not None:
        result = proj.brief_result(brief, section, offset, expect)
        assert "page" in result.structuredContent, result.structuredContent
        assert proj.result_chars(result) <= 12_000 or (
            result.structuredContent["page"]["bounds"]["note"] == "превышен потолок"
        )
        page = result.structuredContent["page"]
        pages.append(page)
        offset, expect = page["next_offset"], page["fingerprint"]
        assert len(pages) < 500
    return pages


def test_sections_page_back_into_the_whole():
    """AC-4: склейка страниц равна разделу; большой элемент один; ошибки и full."""
    big = _brief(ac=25, symbols=80)
    big["call_sites"]["entries"].append(
        {"symbol": "hub.huge", "defined_in": "h.py", "state": "untouched_sites",
         "statement": "s", "total_sites": 900,
         "untouched": [f"hub/huge/{i}.py:1:" + "z" * 200 for i in range(900)]}
    )  # fmt: skip
    before = copy.deepcopy(big)

    pages = _pages(big, "call_sites")
    assert [p for p in pages if "bounds" in p] and len(pages) > 3
    joined = [e for p in pages for e in p["items"]]
    extras = [
        {"_list": "only_tests", "value": o} for o in big["call_sites"]["only_tests"]
    ]
    assert joined == [*big["call_sites"]["entries"], *extras], "без пропусков и дублей"
    meta = {
        k: v for k, v in big["call_sites"].items() if k not in ("entries", "only_tests")
    }
    assert pages[0]["meta"] == meta and all("meta" not in p for p in pages[1:])
    huge_pages = [
        p for p in pages if any(e.get("symbol") == "hub.huge" for e in p["items"])
    ]
    assert len(huge_pages) == 1 and len(huge_pages[0]["items"]) == 1
    assert huge_pages[0]["bounds"]["note"] == "превышен потолок"
    assert pages[-1]["next_offset"] is None and pages[-1]["total"] == len(joined)

    acs = [a for p in _pages(big, "acceptance_criteria") for a in p["items"]]
    assert acs == big["acceptance_criteria"]

    machine = _pages(big, "machine_review")
    expected = [
        {"kind": key, "value": item}
        for key, value in big["machine_review"].items()
        if isinstance(value, list)
        for item in value
    ]
    assert [i for p in machine for i in p["items"]] == expected
    assert (
        machine[0]["meta"]["profile"] == "deep"
        and machine[0]["meta"]["incomplete"] is None
    )

    statement = _pages(big, "statement")
    text = "".join(p["text"] for p in statement)
    assert len(statement) > 1 and big["description"] in text
    assert text.index("## description") < text.index("## technical_hints")
    assert text == proj._statement_section(big).text
    assert big == before, "бриф не мутирован"

    unknown = proj.brief_result(big, "nope")
    payload = unknown.structuredContent
    assert payload["reason"] == "unknown_section"
    assert set(payload["allowed"]) == {
        "call_sites", "acceptance_criteria", "statement", "machine_review",
        "verdict_findings", "full",
    }  # fmt: skip
    assert (
        proj.brief_result(big, "call_sites", 10_000).structuredContent["reason"]
        == "bad_offset"
    )
    assert (
        proj.brief_result(big, "call_sites", -1).structuredContent["reason"]
        == "bad_offset"
    )


@pytest.mark.usefixtures("stateless_hub")
async def test_full_is_the_unprojected_answer_and_cli_flags_follow(
    client,
    served,
    capsys,  # noqa: F811
):
    """AC-4: section=full — прежний ответ; CLI: без флагов проекция, --section, --full."""
    brief = SHAPES["1652"]()
    served["brief"] = brief
    full = await _mcp(client, section="full")
    assert full["structuredContent"]["brief"] == brief, "бриф целиком, без проекции"
    text = _text(full)
    assert (
        text.startswith("Review brief for task #1647")
        and "Acceptance criteria:" in text
    )
    assert "[projection" not in text

    def run(**kw: Any) -> dict[str, Any]:
        ns = argparse.Namespace(
            task_id=1647, section="", offset=0, expect="", full=False, **kw
        )
        with patch.object(cli, "_api", lambda *_a, **_k: copy.deepcopy(brief)):
            rc = cli.cmd_review_brief(ns)
        out = capsys.readouterr().out
        return {"rc": rc, "json": json.loads(out[out.index("{") :])}

    default = run()
    assert default["rc"] == 0 and default["json"]["projection"] is True
    ns_items: list[Any] = []
    offset, expect = 0, ""
    while offset is not None:
        ns = argparse.Namespace(
            task_id=1647, section="acceptance_criteria", offset=offset, expect=expect,
            full=False,
        )  # fmt: skip
        with patch.object(cli, "_api", lambda *_a, **_k: copy.deepcopy(brief)):
            assert cli.cmd_review_brief(ns) == 0
        out = capsys.readouterr().out
        page = json.loads(out[out.index("{") :])
        ns_items += page["items"]
        offset, expect = page["next_offset"], page["fingerprint"]
    assert ns_items == brief["acceptance_criteria"]

    whole = argparse.Namespace(task_id=1647, section="", offset=0, expect="", full=True)
    with patch.object(cli, "_api", lambda *_a, **_k: copy.deepcopy(brief)):
        assert cli.cmd_review_brief(whole) == 0
    out = capsys.readouterr().out
    assert json.loads(out[out.index("{") :]) == brief

    stale = argparse.Namespace(
        task_id=1647, section="statement", offset=0, expect="0" * 64, full=False
    )
    with patch.object(cli, "_api", lambda *_a, **_k: copy.deepcopy(brief)):
        assert cli.cmd_review_brief(stale) == 2, "несовпадение fingerprint — код 2"
    assert json.loads(capsys.readouterr().out)["reason"] == "section_changed"


# --- AC-5 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"current_branch_tip": "c" * 40},
        {"submission_generation": 3, "submission_sha": "d" * 40},
    ],
    ids=["branch-moved", "resubmitted"],
)
def test_paging_refuses_a_changed_section(change):
    """AC-5: сдвиг ветки или пересдача между страницами — ошибка, не смешанная страница."""
    brief = _brief(symbols=80)
    first = proj.brief_result(brief, "call_sites").structuredContent["page"]
    assert first["next_offset"]
    good = proj.brief_result(
        brief, "call_sites", first["next_offset"], first["fingerprint"]
    )
    assert "page" in good.structuredContent, "тот же бриф — продолжение работает"

    moved = {**copy.deepcopy(brief), **change}
    refused = proj.brief_result(
        moved, "call_sites", first["next_offset"], first["fingerprint"]
    )
    payload = refused.structuredContent
    assert payload["reason"] == "section_changed" and "page" not in payload
    assert (
        "начните с offset=0" in payload["message"]
        and "раздел изменился" in payload["message"]
    )
    assert payload["fingerprint"] != first["fingerprint"]
    assert payload["expected"] == first["fingerprint"]
    for key in ("submission_generation", "submission_sha", "current_branch_tip"):
        assert payload[key] == moved[key], key
    assert "items" not in payload


# --- AC-6 ---------------------------------------------------------------------


@pytest.fixture
async def real_auth(client, db, monkeypatch):
    """Настоящий auth: человек, автор, ревьюер, steward и две chat-pair сессии."""
    import httpx
    from httpx import ASGITransport
    from types import SimpleNamespace

    from hub import config
    from hub.app import app
    from hub.services import admin as admin_svc
    from hub.services import chat_pair as cp
    from hub.services import mcp_telemetry

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {"env-bot-token": config.TokenIdentity("env-bot", "agent")},
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.delenv("HAIPLANE_HUB_TOKEN", raising=False)
    tokens: dict[str, str] = {}
    human = await admin_svc.create_principal(
        db, kind="human", username="alice", role_slug="operator"
    )
    tokens["human"] = (await admin_svc.create_api_key(db, human["id"], name="h"))[
        "plaintext_key"
    ]
    for label, role in (
        ("author", "agent"),
        ("reviewer", "agent"),
        ("steward", "steward"),
    ):
        who = await admin_svc.create_principal(
            db, kind="agent", username=f"{label}-bot", role_slug=role
        )
        tokens[label] = (await admin_svc.create_api_key(db, who["id"], name=label))[
            "plaintext_key"
        ]
    headers = {"Authorization": f"Bearer {tokens['human']}"}
    created = await client.post("/api/tasks", json={"title": "AC-6"}, headers=headers)
    task_id = created.json()["id"]
    await db.execute(
        "UPDATE tasks SET status='review', submission_generation=1, "
        "assigned_agent='author-bot' WHERE id=?",
        (task_id,),
    )
    await db.commit()
    for kind in ("reviewer", "steward_advisor"):
        code, _ = await cp.issue_code(
            db, human["id"], kind=kind, bound_task_id=task_id, bound_generation=1
        )
        resp = await client.post(
            "/api/auth/chat-pair/redeem",
            json={"code": code},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        assert resp.status_code == 200, (kind, resp.status_code, resp.text)
        tokens[f"pair_{kind}"] = resp.json()["token"]
    real = httpx.AsyncClient

    def in_process(*a: Any, **kw: Any) -> httpx.AsyncClient:
        kw["transport"] = ASGITransport(app=app)
        return real(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", in_process)
    mcp_telemetry.set_telemetry_sink(db)
    try:
        yield SimpleNamespace(client=client, task_id=task_id, tokens=tokens)
    finally:
        mcp_telemetry.set_telemetry_sink(None)


def _is_rpc_result(resp: Any) -> bool:
    try:
        return "result" in resp.json()
    except ValueError:
        return "result" in resp.text


async def test_mcp_brief_keeps_the_route_permissions(real_auth):
    """AC-6: кому REST отказал, тому MCP не даёт бриф; self_review_warning как у маршрута."""
    from tests.test_mcp_server import _call_tool, _hub_process, _rpc_result

    hub = real_auth
    assert all(hub.tokens.values()), hub.tokens.keys()
    outcomes: dict[str, str] = {}
    async with _hub_process():
        for who, token in hub.tokens.items():
            rest = await hub.client.get(
                f"/api/tasks/{hub.task_id}/review-brief",
                headers={"Authorization": f"Bearer {token}"},
            )
            for args in ({}, {"section": "acceptance_criteria"}, {"section": "full"}):
                resp = await _call_tool(
                    hub.client, token, "hub_get_review_brief",
                    {"task_id": hub.task_id, **args},
                )  # fmt: skip
                structured = (
                    _rpc_result(resp).get("structuredContent") or {}
                    if resp.status_code == 200 and _is_rpc_result(resp)
                    else {}
                )
                payload = structured.get("brief") or structured.get("page")
                if who.startswith("pair_"):
                    # Chat-pair сессии ходят только по своим REST-маршрутам: через
                    # /mcp им бриф не отдают — нового пути к нему нет.
                    assert not payload, (who, args)
                elif rest.status_code == 200:
                    assert payload, (who, args, structured)
                    warned = bool(payload.get("self_review_warning"))
                    assert warned == bool(rest.json().get("self_review_warning")), who
                else:
                    assert not payload, (
                        f"{who} {args}: REST {rest.status_code}, MCP дал бриф"
                    )
            outcomes[who] = str(rest.status_code)
    assert outcomes["human"] == "200" and outcomes["author"] == "200"
    assert outcomes["steward"] == "403", "steward не получает бриф"
    assert outcomes["pair_steward_advisor"] == "403", "advisor не получает бриф"
    assert outcomes["pair_reviewer"] == "200", "chat-pair reviewer читает свой бриф"
    author = await hub.client.get(
        f"/api/tasks/{hub.task_id}/review-brief",
        headers={"Authorization": f"Bearer {hub.tokens['author']}"},
    )
    assert author.json()["self_review_warning"]["reason"] == "self_review_forbidden"
    reviewer = await hub.client.get(
        f"/api/tasks/{hub.task_id}/review-brief",
        headers={"Authorization": f"Bearer {hub.tokens['reviewer']}"},
    )
    assert reviewer.json()["self_review_warning"] is None


# --- AC-7 ---------------------------------------------------------------------

# Эталон снят на develop 42e3505a (до правки), а не на 7184b67d из карточки:
# 7184b67d старше #1686-#1688 (ci_run_report.trust, ci_integrity), и бриф на нём
# уже иного состава. Часы — нормализация created_at/written_at и снятие timings.
# #1706: бриф получил поле review_verdict_revision (ревизия стоящего вердикта),
# эталон переснят осознанно; пакет стюарда и его хеш остались прежними.
# #1830: machine_review получил author_answers и undisposed_author_lines (пустые
# здесь); переснят REST-бриф и пакет стюарда, packet_hash прежний.
# #1858: REST-бриф получил cause_task (null здесь); переснят он и пакет стюарда.
# #1913: REST-бриф читает наблюдения без сети — изменились только sha_check_reason
# («вершину хаб не наблюдал»), stacking_warning («не сверена») и verdict_route
# (условие «проверка по сети при показе не делалась»), ci_integrity («unscanned»:
# скан делает проход поллера, не показ). Пакет стюарда: REST-чтение
# больше не делает скан целостности CI (fetch и запись), поэтому в route.reason пакета
# нет «Справка по CI: … не проверена»; scan_hash в блоке ci_integrity; packet_hash прежний.
# #1985: machine_review получил reviewed_sha и pin_verified (REST-бриф; пакет стюарда и packet_hash проверяются ниже).
# #2026: блок ci_evidence получил поле expectation, ProfileDowngradeView — rules: REST-бриф и payload пакета стюарда; packet_hash прежний.
# #1933: machine_review получил поле ac_tests_unread ([] у этой задачи): REST-бриф и payload пакета стюарда; packet_hash прежний.
# #2090: REST-бриф получил поле outcome_ownership (null у этой задачи: без метрики): переснят REST-бриф и payload пакета стюарда; packet_hash прежний.
GOLDEN_REST_BRIEF = "57f8f14cc22417f9e2df81abae11337332f034dc09419594388480a4d489dfa1"  # pragma: allowlist secret
GOLDEN_STEWARD_PACKET = "751786f606ea18a17fa38739f929e347ba5260ea67cc47cc9a83029bc9d74b9e"  # pragma: allowlist secret
GOLDEN_PACKET_HASH = "0c8efa3186370f7c4b7f3f292de594595531006708519918658804d34e38d9c2"  # pragma: allowlist secret


def _normalised(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: ("<t>" if k in ("created_at", "written_at") else _normalised(v))
            for k, v in node.items()
            if k != "timings"
        }
    if isinstance(node, list):
        return [_normalised(v) for v in node]
    return node


def _digest(node: Any) -> str:
    blob = json.dumps(
        _normalised(node), sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(blob.encode()).hexdigest()


async def test_rest_and_steward_packet_are_unchanged(db, client):
    """AC-7: REST-бриф, пакет steward-evidence и packet_hash побайтно равны эталону."""
    tid = (
        await client.post(
            "/api/tasks",
            json={"title": "Golden", "task_type": "task", "description": "d"},
        )
    ).json()["id"]
    await client.post(
        f"/api/tasks/{tid}/refine",
        json={
            "acceptance_criteria": [
                {"id": "AC-1", "given": "g", "when": "w", "then": "t",
                 "verifiable_by": "test", "test_ref": "tests/test_a.py::test_ok"}
            ]
        },
    )  # fmt: skip
    await repo.update_task(
        db, tid, status="review", branch="task-1/x", submission_sha="a" * 40,
        submission_generation=1,
    )  # fmt: skip
    await repo.record_submission(
        db, task_id=tid, generation=1, sha="a" * 40, base_branch="main"
    )
    await repo.insert_machine_review(
        db, task_id=tid, submission_generation=1,
        findings_confirmed=json.dumps([{"title": "T", "severity": "high"}]),
        incomplete=False, raw_count=3,
    )  # fmt: skip
    await db.commit()

    brief = (await client.get(f"/api/tasks/{tid}/review-brief")).json()
    packet = await build_evidence_packet(db, tid, 1)
    assert _digest(brief) == GOLDEN_REST_BRIEF
    assert _digest(packet_payload(packet)) == GOLDEN_STEWARD_PACKET
    assert packet_hash(packet) == GOLDEN_PACKET_HASH

    # Проекция всех видов ничего не мутирует и REST после неё прежний.
    snapshot = copy.deepcopy(brief)
    for section in (
        "",
        "call_sites",
        "acceptance_criteria",
        "statement",
        "machine_review",
    ):
        proj.brief_result(brief, section)
    assert brief == snapshot
    rich = _brief()
    rich_before = copy.deepcopy(rich)
    for section in ("", *[n for n in proj.SECTIONS if n != "full"]):
        proj.brief_result(rich, section)
    assert rich == rich_before, "проекция не мутирует бриф (порядок символов, списки)"


# --- AC-8 ---------------------------------------------------------------------


def test_oversized_mandatory_part_is_sent_whole_and_flagged():
    """AC-8: решающая часть больше потолка — целиком, с пометкой «превышен потолок»."""
    signs = [{"path": f"ci/{i}.yml", "line": i, "rule": "r" * 80} for i in range(400)]
    brief = _brief(
        ci_integrity={
            "state": "suspicious",
            "gap": "",
            "note": "n" * 300,
            "answer": "",
            "question": "q",
            "signs": signs,
        }  # fmt: skip
    )
    result = proj.brief_result(brief)
    assert proj.result_chars(result) > 12_000
    out = result.structuredContent["brief"]
    assert out["ci_integrity"]["signs"] == signs, "ничего не усечено"
    assert out["bounds"]["note"] == "превышен потолок"
    assert "превышен потолок" in json.loads(result.content[0].text)["message"]
    assert all(
        out["blocks"][n]["shown"] == 0 for n in ("acceptance_criteria", "call_sites")
    ), "сверх потолка необязательное не добавляется"
    assert out["blocks"]["acceptance_criteria"]["total"] == 25, "счётчики остаются"
    fits = proj.brief_result(_brief()).structuredContent["brief"]
    assert "bounds" not in fits


# --- review round on e5f52936 -------------------------------------------------


@pytest.mark.usefixtures("stateless_hub")
async def test_unknown_arguments_are_inside_the_budget_and_the_log(
    client, served, caplog
):
    """P2-1: funnel добавляет unknown_arguments; бюджет и лог — на итоговом виде."""
    served["brief"] = SHAPES["1647"]()
    caplog.set_level(logging.INFO, logger=proj.__name__)

    plain = await _mcp(client)
    extra = await _mcp(client, limit=100, another_unknown_argument="x")

    assert extra["structuredContent"]["unknown_arguments"] == [
        "another_unknown_argument",
        "limit",
    ]
    for result in (plain, extra):
        assert _wire_chars(result) <= 12_000, _wire_chars(result)
    rows = _logged(caplog)
    assert [int(r["chars"]) for r in rows[-2:]] == [
        _wire_chars(plain),
        _wire_chars(extra),
    ], "лог после всех преобразований равен размеру на проводе"


def test_page_with_large_metadata_stays_within_the_ceiling():
    """P2-2: метаданные не раздувают страницу: списки и крупные тексты — элементы."""
    brief = _brief(
        call_sites={
            "status": "analysed",
            "summary": "с" * 3000,
            "entries": [
                {"symbol": f"s{i}", "defined_in": "a.py", "state": "all_touched",
                 "statement": "x", "total_sites": 1, "untouched": []}
                for i in range(200)
            ],
            "only_tests_state": "named",
            "only_tests": [
                {"symbol": f"s{i}", "outcome": "cleared", "call_path": "p" * 40}
                for i in range(200)
            ],
        }
    )  # fmt: skip
    pages = _pages(brief, "call_sites")
    assert len(pages) > 2
    assert all("bounds" not in p for p in pages), "ни одна страница не сверх потолка"
    items = [i for p in pages for i in p["items"]]
    assert len(items) == 200 + 200 + 1, "символы, only_tests и крупный текст"
    assert {"_list": "summary", "value": "с" * 3000} in items
    assert (
        pages[0]["meta"]["status"] == "analysed" and "summary" not in pages[0]["meta"]
    )
    machine = _brief(
        machine_review=_machine(3, 1, incomplete=True, incomplete_reason="н" * 2000)
    )
    mpages = _pages(machine, "machine_review")
    assert {"kind": "incomplete_reason", "value": "н" * 2000} in [
        i for p in mpages for i in p["items"]
    ]


def test_cut_elements_carry_a_hint_and_a_truncated_marker():
    """P3-4: подсказка и truncated и тогда, когда урезан сам показанный элемент."""
    acs = _acs(1)
    assert len(acs[0]["then"]) > 200
    twelve = {
        "symbol": "hub.twelve", "defined_in": "hub/t.py", "state": "untouched_sites",
        "statement": "s", "total_sites": 12,
        "untouched": [f"hub/site_{i}.py:{i + 1}" for i in range(9)],
    }  # fmt: skip
    brief = _brief(
        ac=1,
        confirmed=1,
        unresolved=0,
        call_sites={"status": "analysed", "entries": [twelve]},
    )
    brief["acceptance_criteria"] = acs
    blocks = proj.brief_result(brief).structuredContent["brief"]["blocks"]
    ac = blocks["acceptance_criteria"]
    assert (ac["shown"], ac["omitted"]) == (1, 0)
    assert ac["truncated"] is True and ac["hint"] == "section=acceptance_criteria"
    assert ac["items"][0]["truncated"] is True
    cs = blocks["call_sites"]
    assert (cs["shown"], cs["omitted"]) == (1, 0)
    assert cs["truncated"] is True and cs["hint"] == "section=call_sites"
    assert cs["items"][0]["truncated"] is True

    whole = proj.brief_result(
        _brief(ac=1, call_sites={"status": "analysed", "entries": []})
    )
    short = _brief(ac=1, confirmed=0, unresolved=0)
    short["acceptance_criteria"][0]["then"] = "коротко"
    short["call_sites"]["entries"] = []
    clean = proj.brief_result(short).structuredContent["brief"]["blocks"]
    assert clean["acceptance_criteria"]["truncated"] is False
    assert clean["acceptance_criteria"]["hint"] == ""
    assert whole is not None


def test_self_approved_verdict_is_named_in_the_default_projection():
    """P2-3: [SELF-APPROVED] остаётся в тексте проекции."""
    brief = _brief(
        latest_review={
            "verdict": "approved",
            "submission_generation": 2,
            "is_current": True,
            "self_approved": True,
        }
    )
    text = json.loads(proj.brief_result(brief).content[0].text)["message"]
    assert "[SELF-APPROVED: solo mode, not independent]" in text


# --- review round 2: one classification of every field ------------------------

from pydantic import BaseModel  # noqa: E402

from hub.services import review_brief_fields as fields  # noqa: E402


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    import typing

    for arg in (annotation, *typing.get_args(annotation)):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def _unclassified(model: type[BaseModel], prefix: str = "") -> list[str]:
    missing = []
    for name, info in model.model_fields.items():
        path = f"{prefix}{name}"
        nested = _nested_model(info.annotation)
        split = any(k.startswith(path + ".") for k in fields.FIELD_CLASS)
        if nested is not None and (split or path in fields.REDUCED):
            # A split or REDUCED parent is not classified as a whole: each child is.
            if path not in fields.FIELD_CLASS and not split:
                missing.append(path)
            missing += _unclassified(nested, path + ".")
        elif path not in fields.FIELD_CLASS:
            missing.append(path)
    return missing


def _resolves(path: str) -> bool:
    model: type[BaseModel] | None = ReviewBrief
    for part in path.split("."):
        if model is None or part not in model.model_fields:
            return False
        model = _nested_model(model.model_fields[part].annotation)
    return True


def test_every_brief_field_has_a_class():
    """Новое поле брифа заставляет решить: решающее оно или нет."""
    assert _unclassified(ReviewBrief) == []
    assert [p for p in fields.FIELD_CLASS if not _resolves(p)] == [], "stale entries"
    for path, (klass, reason) in fields.FIELD_CLASS.items():
        assert klass in {"MANDATORY", "BLOCK", "TEXT", "FULL_ONLY"}, path
        if klass == "FULL_ONLY":
            assert reason.strip(), f"{path}: FULL_ONLY needs a reason"
    parents = {p.split(".")[0] for p in fields.FIELD_CLASS if "." in p}
    assert parents == {
        "latest_review",
        "review_report",
        "machine_review",
        "locator_resolution",
        "base_merge",
        "diff_base",
    }
    # Every REDUCED parent has its children classified (the walk below enforces
    # that they are all there); and every TEXT field has a place in the answer.
    for parent in fields.REDUCED:
        assert any(k.startswith(parent + ".") for k in fields.FIELD_CLASS), parent
    assert set(proj._STATEMENT_FIELDS) == set(fields.fields_of("TEXT"))


def test_a_new_field_is_refused_until_it_is_classified(monkeypatch):
    """Проверка проверки: убрали запись из таблицы - обход её находит."""
    trimmed = dict(fields.FIELD_CLASS)
    del trimmed["prepass"], trimmed["machine_review.self_reviewed"]
    monkeypatch.setattr(fields, "FIELD_CLASS", trimmed)
    assert set(_unclassified(ReviewBrief)) == {
        "prepass",
        "machine_review.self_reviewed",
    }


def _clean() -> dict[str, Any]:
    """A small brief where every decision field says 'fine'."""
    return _brief(
        ac=3,
        symbols=1,
        addresses=0,
        prose=1,
        confirmed=0,
        unresolved=0,
        machine_review={
            **_machine(
                0,
                0,
                incomplete=False,
                principal_id=5,
                self_reviewed=False,
                is_current=True,
                lost_dimensions=[],
                findings_confirmed=[{"title": "t", "severity": "low"}],
                findings_rejected=[{"title": "r", "reason": "x"}],
                dispositions=[
                    {
                        "finding_index": 0,
                        "finding_uid": "u1",
                        "finding_title": "t",
                        "disposition": "fixed",
                        "decided_by": "a",
                    }
                ],
                observation_closures=[
                    {"finding_uid": "u1", "live_check_id": 1, "hand": "h"}
                ],
                deferred_repeats=[{"finding_uid": "u1", "linked_task_id": 5}],
            ),
            "unresolved": [{"title": "u", "why": "w"}],
        },
        locator_resolution=[
            {"ac_id": "AC-1", "locator": "t::x", "status": "resolvable", "reason": ""}
        ],
        base_merge={"state": "clean", "reason": "ok", "files": []},
        latest_review={
            "verdict": "approved",
            "submission_generation": 2,
            "is_current": True,
            "findings": [
                {"id": 1, "severity": "low", "message": "m", "scope": "in_scope"}
            ],
        },
        ci_run_report={"state": "current", "reason": "ok", "trust": "verified"},
        ci_evidence={
            "mutations": {"state": "received", "reason": "ok"},
            "baseline": {"state": "received", "reason": "ok"},
        },
        diff_base={"base": "develop", "state": "resolved", "reason": "", "sha": "a"},
        prepass={
            "state": "passed",
            "reason": "ok",
            "passed": ["ruff"],
            "head_sha": "a",
        },
        validation={
            "state": "verified",
            "verified": True,
            "needs_human_compare": False,
        },
        ac_test_results=[
            {"ac_id": f"AC-{i}", "status": "pass", "is_current": True}
            for i in (1, 2, 3)
        ],
    )


def _text_of(brief: dict[str, Any]) -> str:
    return json.loads(proj.brief_result(brief).content[0].text)["message"]


def _answer(brief: dict[str, Any]) -> dict[str, Any]:
    return proj.brief_result(brief).structuredContent["brief"]


def test_failures_self_review_and_downgrade_change_the_default_answer():
    """Три воспроизведения Codex: провал, самоконтроль и понижение видны в тексте."""
    base = _clean()
    base_text, base_answer = _text_of(base), _answer(base)

    failing = copy.deepcopy(base)
    failing["prepass"] |= {"state": "failed", "failed": ["mypy"]}
    failing["validation"] |= {
        "state": "not_run",
        "verified": False,
        "needs_human_compare": True,
    }
    failing["ac_test_results"][2]["status"] = "fail"
    failing["ac_test_results"].append(
        {"ac_id": "AC-9", "status": "not_found", "is_current": True}
    )
    failing["ac_test_results"].append(
        {"ac_id": "AC-8", "status": "fail", "is_current": False}
    )
    text = _text_of(failing)
    assert _answer(failing) != base_answer and text != base_text
    assert "Pre-pass [failed] FAILED" in text and "failed: mypy" in text
    assert "verified=False NEEDS HUMAN COMPARE" in text
    assert "NOT PASSING: AC-3=fail, AC-9=not_found" in text
    assert "AC-8" not in text, "прошлый (не текущий) результат не красный, но посчитан"
    assert _answer(failing)["ac_test_results"]["stale"] == 1
    assert "Pre-pass [passed]" in base_text and "NOT PASSING" not in base_text

    selfreviewed = copy.deepcopy(base)
    selfreviewed["machine_review"] |= {"self_reviewed": True}
    assert _answer(selfreviewed) != base_answer
    assert "SELF-REVIEWED: the author reviewed their own work" in _text_of(selfreviewed)
    unproven = copy.deepcopy(base)
    unproven["machine_review"] |= {"principal_id": None}
    assert "independence NOT established" in _text_of(unproven)
    assert "independence NOT established" not in base_text

    downgraded = copy.deepcopy(base)
    downgraded["profile_downgrade"] = {
        "required_profile": "deep",
        "ordered_profile": "lite",
        "reasons": ["cap"],
        "state": "downgraded",
        "channel": "cloud",
        "dispatch_id": 3,
        "headline": "требовался deep, заказан lite: потолок дня",
    }
    assert _answer(downgraded)["profile_downgrade"]["required_profile"] == "deep"
    assert "PROFILE DOWNGRADED: требовался deep, заказан lite" in _text_of(downgraded)
    assert "PROFILE DOWNGRADED" not in base_text


def test_a_failing_ac_is_listed_even_when_the_ac_block_is_cut():
    brief = _clean()
    brief["acceptance_criteria"] = _acs(25)
    brief["ac_test_results"] = [
        {
            "ac_id": f"AC-{i}",
            "status": "fail" if i == 25 else "pass",
            "is_current": True,
        }
        for i in range(1, 26)
    ]
    answer = _answer(brief)
    assert answer["blocks"]["acceptance_criteria"]["omitted"] > 0
    assert answer["ac_test_results"]["negative"] == [
        {"ac_id": "AC-25", "status": "fail"}
    ]
    assert "NOT PASSING: AC-25=fail" in _text_of(brief)


# For every MANDATORY entry of the table: where to put a 'bad' value, and which.
_BAD: dict[str, tuple[str, Any]] = {
    "task_id": ("task_id", 2),
    "title": ("title", "другая"),
    "status": ("status", "running"),
    "project": ("project", {"id": 1, "slug": "zz"}),
    "locator_resolution": (
        "locator_resolution",
        [
            {"ac_id": "AC-1", "locator": "t::x", "status": "resolvable", "reason": ""},
            {"ac_id": "AC-2", "locator": None, "status": "missing", "reason": ""},
        ],
    ),
    "ac_test_results": (
        "ac_test_results",
        [{"ac_id": "AC-1", "status": "fail", "is_current": True}],
    ),
    "ci_run_report": ("ci_run_report.trust", "unverified"),
    "ci_integrity": (
        "ci_integrity",
        {
            "state": "suspicious",
            "gap": "разрыв-ци",
            "signs": [],
            "question": "вопрос-ци",
        },
    ),
    "ci_evidence": ("ci_evidence.mutations.state", "not_received"),
    "prepass": ("prepass.state", "failed"),
    "validation": ("validation.needs_human_compare", True),
    "live_check": ("live_check.sha_mismatch", True),
    "statement_freshness": (
        "statement_freshness",
        {"state": "stale", "reason": "причина-устарела"},
    ),
    "branch": ("branch", "other"),
    "diff_command": ("diff_command", "git diff x"),
    "diff_base": ("diff_base.state", "unverified"),
    "base_merge": ("base_merge.state", "conflicting"),
    "evidence_coverage": ("evidence_coverage.state", "none"),
    "review_cycle": ("review_cycle", 4),
    "review_circle": ("review_circle.laps", 3),
    "profile_downgrade": (
        "profile_downgrade",
        {"headline": "заголовок-понижения", "required_profile": "deep"},
    ),
    "verdict_route": ("verdict_route", {"line": "вердикт: человек", "final": "human"}),
    "catalogue_rules": (
        "catalogue_rules",
        [{"category": "c", "check_ref": "t::x", "matched_areas": []}],
    ),
    "submission_generation": ("submission_generation", 9),
    "review_verdict_revision": ("review_verdict_revision", 9),
    "submission_sha": ("submission_sha", "f" * 40),
    "current_branch_tip": ("current_branch_tip", "e" * 40),
    "sha_check": ("sha_check", "diverged"),
    "sha_check_reason": ("sha_check_reason", "другая причина"),
    "review_in_flight": ("review_in_flight", {"headline": "ещё идёт"}),
    "current_generation_review": ("current_generation_review.has_review", False),
    "self_review_warning": (
        "self_review_warning",
        {"reason": "self_review_forbidden", "message": "m", "hint": "h"},
    ),
    "stacking_warning": ("stacking_warning", "стек веток"),
    "cause_task": (
        "cause_task",
        {
            "state": "known",
            "task_id": 7,
            "title": "Причина-семь",
            "link": "/tasks/7",
            "affected_areas": ["hub/seven.py"],
            "areas_note": "заявленные области",
        },
    ),
    "path_notices": (
        "path_notices",
        {"generation": 1, "state": "matched", "text": "ручной шаг"},
    ),
    "result_kind": ("result_kind", "state"),
    "state_review": (
        "state_review",
        {"headline": "заголовок-состояния", "label": "метка-состояния"},
    ),
    "latest_review.verdict": ("latest_review.verdict", "changes_requested"),
    "latest_review.submission_generation": ("latest_review.submission_generation", 7),
    "latest_review.is_current": ("latest_review.is_current", False),
    "latest_review.closed_by_decision": ("latest_review.closed_by_decision", True),
    "latest_review.self_approved": ("latest_review.self_approved", True),
    "latest_review.findings": (
        "latest_review.findings",
        [
            {"id": 1, "severity": "low", "message": "m", "scope": "in_scope"},
            {"id": 2, "severity": "high", "message": "m2"},
        ],
    ),
    "review_report.state": ("review_report.state", "stale"),
    "review_report.branch": ("review_report.branch", "b2"),
    "review_report.submission_sha": ("review_report.submission_sha", "9" * 40),
    "review_report.diff_files": ("review_report.diff_files", 40),
    "review_report.diff_lines": ("review_report.diff_lines", 4000),
    "review_report.diff_note": ("review_report.diff_note", "объём не измерен"),
    "review_report.machine_review": ("review_report.machine_review", {"id": 99}),
    "machine_review.id": ("machine_review.id", 99),
    "machine_review.is_current": ("machine_review.is_current", False),
    "machine_review.model": ("machine_review.model", "m2"),
    "machine_review.raw_count": ("machine_review.raw_count", 9),
    "machine_review.findings_confirmed": (
        "machine_review.findings_confirmed",
        [{"title": "t", "severity": "low"}, {"title": "t2", "severity": "high"}],
    ),
    "machine_review.findings_rejected": (
        "machine_review.findings_rejected",
        [{"title": "r"}, {"title": "r2"}],
    ),
    "machine_review.incomplete": ("machine_review.incomplete", None),
    "machine_review.incomplete_reason": (
        "machine_review.incomplete_reason",
        "environment",
    ),
    "machine_review.unresolved": (
        "machine_review.unresolved",
        [{"title": "u"}, {"title": "u2"}],
    ),
    "machine_review.lost_dimensions": ("machine_review.lost_dimensions", ["security"]),
    "machine_review.ac_tests_unread": ("machine_review.ac_tests_unread", ["t.py::t"]),
    "machine_review.profile": ("machine_review.profile", "lite"),
    "machine_review.submitted_by": ("machine_review.submitted_by", "author"),
    "machine_review.self_reviewed": ("machine_review.self_reviewed", True),
    "machine_review.principal_id": ("machine_review.principal_id", None),
    "machine_review.pin_verified": ("machine_review.pin_verified", True),
    "machine_review.ci_integrity_verdict": (
        "machine_review.ci_integrity_verdict",
        "suspicious",
    ),
    "machine_review.ci_integrity_explanation": (
        "machine_review.ci_integrity_explanation",
        "почему",
    ),
    "machine_review.dispositions": ("machine_review.dispositions", [{"x": 1}]),
    "machine_review.observation_closures": (
        "machine_review.observation_closures",
        [{"x": 1}],
    ),
    "machine_review.deferred_repeats": ("machine_review.deferred_repeats", [{"x": 1}]),
    "machine_review.outcome": ("machine_review.outcome", "clean_run"),
    "machine_review.outcome_label": ("machine_review.outcome_label", "иное"),
    "machine_review.outcome_names_clean": ("machine_review.outcome_names_clean", True),
    "machine_review.second_door_channel": (
        "machine_review.second_door_channel",
        "local",
    ),
    "machine_review.second_door_reason": (
        "machine_review.second_door_reason",
        "нет cloud",
    ),
}


def _child(parent: str, name: str, value: Any) -> tuple[str, tuple[str, Any]]:
    """A child of the first element of a list: the CONTENT changes, the length not."""
    return f"{parent}.{name}", (f"{parent}.0.{name}", value)


_BAD |= dict(
    [
        _child("machine_review.findings_confirmed", "title", "другая"),
        _child("machine_review.findings_confirmed", "severity", "high"),
        _child("machine_review.findings_confirmed", "finding_uid", "zzz"),
        _child("machine_review.unresolved", "title", "другая"),
        _child("machine_review.unresolved", "finding_uid", "zzz"),
        _child("machine_review.dispositions", "finding_uid", "u2"),
        _child("machine_review.dispositions", "finding_index", 3),
        _child("machine_review.dispositions", "disposition", "wont_fix"),
        _child("machine_review.dispositions", "decided_by", "other"),
        _child("machine_review.observation_closures", "finding_uid", "u2"),
        _child("machine_review.observation_closures", "live_check_id", 2),
        _child("machine_review.observation_closures", "hand", "other"),
        _child("machine_review.deferred_repeats", "finding_uid", "u2"),
        _child("machine_review.deferred_repeats", "linked_task_id", 6),
        _child("latest_review.findings", "id", 2),
        _child("latest_review.findings", "severity", "high"),
        _child("latest_review.findings", "message", "другое"),
        _child("latest_review.findings", "scope", "out_of_scope"),
        _child("latest_review.findings", "linked_task_id", 9),
        _child("latest_review.findings", "file", "a.py"),
        _child("latest_review.findings", "line", 3),
        _child("locator_resolution", "ac_id", "AC-7"),
        _child("locator_resolution", "status", "missing"),
    ]
)
_BAD |= {
    "base_merge.state": ("base_merge.state", "conflicting"),
    "base_merge.reason": ("base_merge.reason", "другая"),
    "base_merge.files": ("base_merge.files", ["a.py"]),
    "diff_base.state": ("diff_base.state", "unverified"),
    "diff_base.reason": ("diff_base.reason", "не проверена"),
    "constraints": ("constraints", ["не трогать REST"]),
    "out_of_scope_for_review": ("out_of_scope_for_review", ["этап 2"]),
    "review_checklist": ("review_checklist", ["проверить конверт"]),
}


def _set(node: dict[str, Any], dotted: str, value: Any) -> None:
    *parents, last = dotted.split(".")
    for part in parents:
        if isinstance(node, list):
            node = node[int(part)]
            continue
        if not isinstance(node.get(part), (dict, list)):
            node[part] = {}
        node = node[part]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value


def _mandatory_paths() -> list[str]:
    return [p for p, (klass, _r) in fields.FIELD_CLASS.items() if klass == "MANDATORY"]


def test_every_mandatory_field_is_covered_by_a_bad_value():
    assert sorted(_BAD) == sorted(_mandatory_paths()), "new MANDATORY needs a bad value"


@pytest.mark.parametrize("path", sorted(_BAD))
def test_flipping_a_mandatory_field_changes_the_default_answer(path):
    """Каждое MANDATORY-поле: «плохое» значение меняет и данные, и текст ответа."""
    base = _clean()
    # The shape the field has when it says 'fine'; the bad value must differ from it.
    set_path, bad = _BAD[path]
    flipped = copy.deepcopy(base)
    _set(flipped, set_path, bad)
    assert flipped != base, "the bad value is really a change of the input"
    assert _answer(flipped) != _answer(base), path
    # The text carries every flip except pure identifiers/counters, which the
    # structured half carries; both halves are measured by the same budget.
    if path not in _STRUCTURED_ONLY:
        assert _text_of(flipped) != _text_of(base), path
        # Размер ответа меняется и без поля (счётчики): сторож проверяет ТЕКСТ
        # поля. Хотя бы одна строка «плохого» значения обязана попасть в текст.
        if path not in _TEXT_ECHO_NOT_CHECKED:
            echoed = [
                x
                for x in _string_leaves(bad)
                if x in _text_of(flipped) and x not in _text_of(base)
            ]
            assert echoed or not _string_leaves(bad), (path, "поле не напечатано")


#: Поля, текст которых зависит от значения целиком (печатаются при условии):
#: проверка строкового листа к ним не применима, их ловит прежнее сравнение.
_TEXT_ECHO_NOT_CHECKED = {"base_merge", "base_merge.state", "latest_review.verdict"}


def _string_leaves(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _string_leaves(v)]
    if isinstance(value, list):
        return [s for v in value for s in _string_leaves(v)]
    return [value] if isinstance(value, str) and value else []


_STRUCTURED_ONLY = {
    "task_id",
    "status",
    "project",
    "submission_generation",
    "result_kind",
    "review_cycle",
    "latest_review.submission_generation",
    "review_report.state",
    "review_report.branch",
    "review_report.submission_sha",
    "review_report.machine_review",
    "machine_review.raw_count",
    "machine_review.findings_rejected",
    "machine_review.outcome_label",
    "machine_review.outcome_names_clean",
    "machine_review.submitted_by",
    "machine_review.outcome",
    "machine_review.lost_dimensions",
    "machine_review.dispositions.finding_index",
    "locator_resolution.ac_id",
    "base_merge.reason",
    "base_merge.files",
    "diff_base.reason",
}


# --- review round 3 ----------------------------------------------------------


def test_every_text_field_is_rendered_with_counts():
    """P2-1: каждое TEXT-поле реально есть в ответе - с shown/omitted/total."""
    brief = _clean()
    for name in fields.fields_of("TEXT"):
        value = brief.get(name)
        brief[name] = (
            ["элемент один", "элемент два"]
            if isinstance(value, list)
            else "текст " + name
        )
    statement = _answer(brief)["blocks"]["statement"]
    assert set(statement) == set(fields.fields_of("TEXT"))
    for name, block in statement.items():
        assert (
            block["total"] > 0 and block["shown"] + block["omitted"] == block["total"]
        ), name
        assert {"shown", "omitted", "total", "hint"} <= set(block), name
    text = _text_of(brief)
    for name in fields.fields_of("TEXT"):
        assert f"Statement/{name}:" in text, name


def test_review_boundaries_are_carried_in_both_halves():
    """P2-1: constraints, out_of_scope_for_review, review_checklist - решающие."""
    brief = _clean()
    brief["constraints"] = ["REST не меняется"]
    brief["out_of_scope_for_review"] = ["этап 2 #1652"]
    brief["review_checklist"] = ["проверить конверт"]
    answer, text = _answer(brief), _text_of(brief)
    assert answer["constraints"] == ["REST не меняется"]
    assert answer["out_of_scope_for_review"] == ["этап 2 #1652"]
    assert answer["review_checklist"] == ["проверить конверт"]
    for needle in (
        "Constraints: REST не меняется",
        "Out of scope for review: этап 2 #1652",
        "  - проверить конверт",
    ):
        assert needle in text, needle


def test_dispositions_closures_and_repeats_carry_the_decision_value():
    """P2-2: wont_fix и false_positive по одной находке - разные ответы."""
    a, b = _clean(), _clean()
    a["machine_review"]["dispositions"][0]["disposition"] = "wont_fix"
    b["machine_review"]["dispositions"][0]["disposition"] = "false_positive"
    assert _answer(a) != _answer(b)
    assert "disposition u1: wont_fix" in _text_of(a)
    assert "disposition u1: false_positive" in _text_of(b)
    row = _answer(a)["machine_review"]["dispositions"][0]
    assert row == {
        "finding_uid": "u1",
        "finding_index": 0,
        "disposition": "wont_fix",
        "decided_by": "a",
    }, "uid и решение, без свободного текста"
    c = _clean()
    c["machine_review"]["deferred_repeats"][0]["linked_task_id"] = 77
    assert "deferred repeat u1 -> task #77" in _text_of(c)
    d = _clean()
    d["machine_review"]["observation_closures"][0]["hand"] = "other"
    assert "closed by live check 1: u1 (hand other)" in _text_of(d)


def test_verdict_findings_keep_scope_and_linked_task():
    """P2-3: in_scope и out_of_scope находки не похожи друг на друга."""
    a, b = _clean(), _clean()
    a["latest_review"]["findings"] = [
        {"id": 4, "severity": "high", "message": "m", "scope": "in_scope"}
    ]
    b["latest_review"]["findings"] = [
        {
            "id": 4,
            "severity": "high",
            "message": "m",
            "scope": "out_of_scope",
            "linked_task_id": 9,
        }
    ]
    assert _answer(a) != _answer(b)
    row = _answer(b)["latest_review"]["findings"]["items"][0]
    assert (row["id"], row["scope"], row["linked_task_id"]) == (4, "out_of_scope", 9)
    assert "id=4 scope=out_of_scope linked_task_id=9" in _text_of(b)


# --- review round 4 ----------------------------------------------------------


def _verdict_with(n: int) -> dict[str, Any]:
    return {
        "verdict": "changes_requested",
        "submission_generation": 2,
        "is_current": True,
        "findings": [
            {
                "id": i,
                "severity": "high",
                "message": f"находка {i}",
                "file": f"hub/f{i}.py",
                "line": i,
                "scope": "out_of_scope" if i % 2 else "in_scope",
                **({"linked_task_id": 100 + i} if i % 2 else {}),
            }
            for i in range(1, n + 1)
        ],
    }


def test_identifiers_are_carried_whole_only_prose_is_cut():
    """Путь файла находки не режется: два длинных пути, различные после 200-го знака."""
    prefix = "hub/" + "d" * 205
    a, b = _clean(), _clean()
    a["latest_review"]["findings"][0]["file"] = prefix + "/a.py"
    b["latest_review"]["findings"][0]["file"] = prefix + "/b.py"
    assert len(prefix) > 200
    row_a = _answer(a)["latest_review"]["findings"]["items"][0]
    assert row_a["file"] == prefix + "/a.py", "файл целиком"
    assert _answer(a) != _answer(b) and _text_of(a) != _text_of(b)
    assert prefix + "/a.py" in _text_of(a)
    # prose is still cut, and says so through the digest hint source (section).
    long_message = _clean()
    long_message["latest_review"]["findings"][0]["message"] = "м" * 400
    cut = _answer(long_message)["latest_review"]["findings"]["items"][0]["message"]
    assert len(cut) == 200 and cut.endswith("…")
    assert proj.PROSE_FIELDS == {"title", "message"}


def test_all_verdict_findings_are_recovered_by_paging_the_named_section():
    brief = _clean()
    brief["latest_review"] = _verdict_with(11)
    digest = _answer(brief)["latest_review"]["findings"]
    assert (digest["total"], digest["shown"], digest["omitted"]) == (11, 10, 1)
    assert digest["hint"] == "section=verdict_findings"
    assert "… and 1 more (section=verdict_findings)" in _text_of(brief)
    pages = _pages(brief, "verdict_findings")
    got = [f for p in pages for f in p["items"]]
    assert [f["id"] for f in got] == list(range(1, 12))
    eleventh = got[10]
    assert (eleventh["scope"], eleventh["linked_task_id"]) == ("out_of_scope", 111)
    assert pages[0]["meta"]["verdict"] == "changes_requested"


def _walk_blocks(node: Any, path: str = ""):
    """Every dict of the projection that reports omitted items and a hint."""
    if isinstance(node, dict):
        if node.get("omitted") and "hint" in node:
            yield path, node
        for key, value in node.items():
            yield from _walk_blocks(value, f"{path}.{key}" if path else key)


def test_every_hint_names_a_section_that_holds_the_omitted_items():
    brief = _brief(
        ac=25, symbols=30, confirmed=12, unresolved=12, latest_review=_verdict_with(11)
    )
    answer = proj.brief_result(brief).structuredContent["brief"]
    seen = {path: block for path, block in _walk_blocks(answer)}
    assert {
        "blocks.acceptance_criteria",
        "blocks.call_sites",
        "machine_review.findings_confirmed",
        "machine_review.unresolved",
        "latest_review.findings",
    } <= set(seen), sorted(seen)
    for path, block in seen.items():
        hint = block["hint"]
        assert hint.startswith("section="), (path, hint)
        name = hint.split("=", 1)[1]
        assert name in proj.SECTIONS and name != "full", (path, name)
        pages = _pages(brief, name)
        items = [i for p in pages for i in p.get("items", [])]
        if path.startswith("blocks.statement."):
            field = path.rsplit(".", 1)[1]
            assert brief[field] if isinstance(brief[field], str) else True
            continue
        source = {
            "blocks.acceptance_criteria": brief["acceptance_criteria"],
            "blocks.call_sites": brief["call_sites"]["entries"],
            "latest_review.findings": brief["latest_review"]["findings"],
        }.get(path)
        if source is None:  # machine_review.<list>: tagged elements
            key = path.split(".", 1)[1]
            source = [{"kind": key, "value": v} for v in brief["machine_review"][key]]
        assert block["total"] == len(source), path
        missing = [e for e in source if e not in items]
        assert missing == [], (path, hint, len(missing))
    # statement texts: the hinted section holds the whole field
    statement = "".join(p["text"] for p in _pages(brief, "statement"))
    for field, block in answer["blocks"].get("statement", {}).items():
        if block["omitted"]:
            assert block["hint"] == "section=statement"
            value = brief[field]
            parts = [value] if isinstance(value, str) else [str(v) for v in value]
            assert all(part in statement for part in parts), field


def test_the_cause_task_line_is_in_both_texts():
    """#1858 (машинное ревью cd05af1b): текст брифа несёт блок причины."""
    from hub import mcp_server

    known = _clean()
    known["cause_task"] = _BAD["cause_task"][1]
    gone = _clean()
    gone["cause_task"] = {"state": "no_data", "note": "нет данных: причина недоступна"}
    for brief in (known, gone, _clean()):
        full = mcp_server._full_brief_result(brief).content[0].text
        assert ("Причина-семь" in _text_of(brief)) == (brief is known)
        assert ("Причина-семь" in full) == (brief is known)
    for text in (_text_of(known), mcp_server._full_brief_result(known).content[0].text):
        assert "Задача-причина #7: Причина-семь (/tasks/7)" in text, text
        assert "заявленные области: hub/seven.py" in text, text
        assert "cause_kind=" in text, text
    for text in (_text_of(gone), mcp_server._full_brief_result(gone).content[0].text):
        assert "нет данных: причина недоступна" in text, text
        assert "Задача-причина #" not in text
    for text in (
        _text_of(_clean()),
        mcp_server._full_brief_result(_clean()).content[0].text,
    ):
        assert "Задача-причина" not in text and "причина недоступна" not in text
