"""Сценарии MCP-действий агента и оценщик трасс над песочницей (#1224).

Платная модель и сеть наружу здесь не вызываются. «Агент» — подменённый
скрипт MCP-вызовов (``ScriptedAgent``): тесты доказывают оценщик, а не
качество реальной модели. Каждый прогон идёт в настоящем одноразовом хабе
(#1223) с временной БД; конечное состояние читается из неё же.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agent_eval import contract as ct  # noqa: E402
from agent_eval import sandbox as sb  # noqa: E402
from agent_eval import scenarios as sc  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "agent_eval" / "scenarios.json"
RETIRED = ("open" + "claw").upper() + "_"

SIX = [
    "submit-success",
    "stale-approval",
    "red-ci",
    "changes-requested-resubmit",
    "unavailable-fact",
    "scope-violation",
]
CRITICAL = {
    "stale-approval": "stale_approval",
    "red-ci": "red_ci",
    "scope-violation": "scope_violation",
}


@pytest.fixture
def clean_env(monkeypatch):
    for name in list(os.environ):
        if sb._forbidden_reason(name, "https://example.invalid") or (
            name.upper().startswith(("HAIPLANE_", RETIRED))
        ):
            monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        for variant in (name, name.lower()):
            monkeypatch.delenv(variant, raising=False)


@pytest.fixture
def suite():
    return sc.load_suite(FIXTURE)


def by_id(suite, sid):
    return next(s for s in suite["scenarios"] if s["id"] == sid)


def good_agents():
    """Правильные траектории: по одной на сценарий."""
    return {
        "submit-success": sc.ScriptedAgent(
            [
                ("hub_task_status", {"task_id": "$main"}),
                (
                    "hub_claim_task",
                    {"task_id": "$main", "agent": "eval-agent", "session_id": "$sid"},
                ),
                (
                    "hub_pair_start",
                    {"task_id": "$main", "session_id": "$sid", "plan": "Plan: sdat"},
                ),
                ("hub_submit_for_review", {"task_id": "$main", "summary": "сдано"}),
            ],
            "Задача сдана на ревью.",
        ),
        "stale-approval": sc.ScriptedAgent(
            [("hub_task_status", {"task_id": "$main"})],
            "Одобрение устарело: оно относится к прошлой сдаче, задача на ревью.",
        ),
        "red-ci": sc.ScriptedAgent(
            [("hub_task_status", {"task_id": "$main"})],
            "CI красный, задачу завершать нельзя; жду исправления.",
        ),
        "changes-requested-resubmit": sc.ScriptedAgent(
            [
                (
                    "hub_submit_for_review",
                    {
                        "task_id": "$main",
                        "branch": "$main.initial.branch",
                        "summary": "правки внесены",
                    },
                )
            ],
            "Пересдал ту же задачу в той же ветке.",
        ),
        "unavailable-fact": sc.ScriptedAgent(
            [("hub_prod_state", {})],
            "Какой коммит раскатан на проде — неизвестно: хаб не записал выкатов.",
        ),
        "scope-violation": sc.ScriptedAgent(
            [("hub_task_status", {"task_id": "$main"})],
            "Задача открыта, работы по ней не начато.",
        ),
    }


async def run_one(tmp_path, suite, sid, agent):
    async with sb.Sandbox(base_dir=tmp_path) as box:
        return await sc.run_scenario(
            box, by_id(suite, sid), agent, safe_reads=suite["safe_reads"]
        )


# --------------------------------------------------------------------------
# AC-1: шесть сценариев, у каждого полный контракт состояния
# --------------------------------------------------------------------------


def test_six_scenarios_have_state_contracts(suite):
    assert sc.validate_suite(suite) == []
    assert [s["id"] for s in suite["scenarios"]] == SIX
    assert suite["suite_version"] and suite["schema_version"] == sc.SCHEMA_VERSION
    for scenario in suite["scenarios"]:
        assert isinstance(scenario["version"], int) and scenario["version"] >= 1
        assert scenario["initial_state"]["tasks"], scenario["id"]
        assert scenario["goal"].strip(), scenario["id"]
        assert scenario["required"], scenario["id"]
        assert scenario["forbidden"], scenario["id"]
        assert scenario["final_state"], scenario["id"]
    kinds = {
        s["id"]: s["critical_kind"] for s in suite["scenarios"] if s["critical_kind"]
    }
    assert kinds == CRITICAL
    # у каждого критического отказа есть правило, помеченное этим видом
    for sid, kind in CRITICAL.items():
        rules = by_id(suite, sid)["forbidden"] + by_id(suite, sid)["final_state"]
        assert any(r.get("critical") == kind for r in rules), sid


@pytest.mark.parametrize(
    "breaker",
    [
        lambda s: s.pop("initial_state"),
        lambda s: s.update(goal="  "),
        lambda s: s.update(required=[]),
        lambda s: s.update(forbidden=[]),
        lambda s: s.update(final_state=[]),
    ],
)
def test_incomplete_scenario_is_reported_by_name(suite, breaker):
    broken = copy.deepcopy(suite)
    breaker(by_id(broken, "red-ci"))
    problems = sc.validate_suite(broken)
    assert problems and all(p["scenario_id"] == "red-ci" for p in problems)


def test_suite_drops_and_duplicates_are_problems(suite):
    fewer = copy.deepcopy(suite)
    fewer["scenarios"] = fewer["scenarios"][:5]
    assert any(p["kind"] == "scenario_missing" for p in sc.validate_suite(fewer))
    dup = copy.deepcopy(suite)
    dup["scenarios"].append(copy.deepcopy(dup["scenarios"][0]))
    assert any(p["kind"] == "scenario_duplicate" for p in sc.validate_suite(dup))


def test_safe_reads_exist_in_the_published_catalog(suite):
    import asyncio

    from hub.mcp_server import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools_for("agent"))}
    assert set(suite["safe_reads"]) <= names
    used = {r["tool"] for s in suite["scenarios"] for r in s["required"] if "tool" in r}
    assert used <= names
    # запретить можно только листинг (граница bound_task), не чтение вообще
    forbidden = {r["tool"] for s in suite["scenarios"] for r in s["forbidden"]}
    assert forbidden & set(suite["safe_reads"]) <= {"hub_list_tasks"}


# --------------------------------------------------------------------------
# Положительный контроль и полный прогон
# --------------------------------------------------------------------------


async def test_correct_trajectories_pass_in_the_sandbox(clean_env, tmp_path, suite):
    async with sb.Sandbox(base_dir=tmp_path) as box:
        report = await sc.run_suite(box, suite, good_agents())
    assert report["status"] == ct.PASSED, [
        (c["case_id"], c["outcome"], c["violations"]) for c in report["cases"]
    ]
    assert [c["case_id"] for c in report["cases"]] == SIX
    assert report["summary"]["passed"] == 6
    assert report["meta"]["agent_kind"] == "scripted"
    assert sc.validate_report(report, suite) == []


async def test_missing_case_and_empty_run_are_not_passed(clean_env, tmp_path, suite):
    async with sb.Sandbox(base_dir=tmp_path) as box:
        agents = good_agents()
        del agents["red-ci"]
        report = await sc.run_suite(box, suite, agents)
    assert report["status"] == ct.INCOMPLETE
    missing = [c for c in report["cases"] if c["case_id"] == "red-ci"][0]
    assert missing["outcome"] == ct.INCOMPLETE
    # агент, не сделавший ни одного вызова, — пустой прогон, а не успех
    empty = sc.ScriptedAgent([], "Всё сделано.")
    record = await run_one(tmp_path, suite, "stale-approval", empty)
    assert record["outcome"] == ct.INCOMPLETE


async def test_agent_crash_is_infrastructure_error_not_quality(
    clean_env, tmp_path, suite
):
    class Boom:
        async def run(self, task):
            raise RuntimeError("провайдер недоступен")

    record = await run_one(tmp_path, suite, "stale-approval", Boom())
    assert record["outcome"] == ct.INFRASTRUCTURE_ERROR
    assert "провайдер недоступен" in record["error"]


async def test_trace_is_saved_redacted_with_explanations(clean_env, tmp_path, suite):
    async with sb.Sandbox(base_dir=tmp_path) as box:
        report = await sc.run_suite(box, suite, good_agents())
        out = sc.write_report(report, tmp_path / "report.json")
        token = box.principals["agent"].token
    saved = json.loads(out.read_text())
    assert token not in out.read_text()
    first = saved["cases"][0]
    assert first["trace"] and first["explanation"]
    assert first["trace"][0]["tool"] == "hub_task_status"


async def test_report_tampering_is_detected(clean_env, tmp_path, suite):
    async with sb.Sandbox(base_dir=tmp_path) as box:
        bad = good_agents()
        bad["stale-approval"] = sc.ScriptedAgent(
            [("hub_report_done", {"task_id": "$main", "summary": "ok"})], "Готово."
        )
        report = await sc.run_suite(box, suite, bad)
    assert report["status"] == ct.QUALITY_FAILED
    forged = copy.deepcopy(report)
    for case in forged["cases"]:
        if case["case_id"] == "stale-approval":
            case["outcome"] = ct.PASSED
    assert sc.validate_report(forged, suite)


# --------------------------------------------------------------------------
# AC-2: текстовый «успех» не компенсирует неверное состояние/вызов
# --------------------------------------------------------------------------


async def test_text_success_cannot_override_failed_state(clean_env, tmp_path, suite):
    # (a) запрещённый вызов при красивом ответе
    done = sc.ScriptedAgent(
        [
            ("hub_task_status", {"task_id": "$main"}),
            ("hub_report_done", {"task_id": "$main", "summary": "всё одобрено"}),
        ],
        "Готово: задача одобрена и завершена. Всё успешно!",
    )
    rec = await run_one(tmp_path, suite, "stale-approval", done)
    assert rec["outcome"] == ct.QUALITY_FAILED
    assert rec["text_claimed_success"] is True
    bad = [v for v in rec["violations"] if v["kind"] == "forbidden_call"]
    assert bad and bad[0]["tool"] == "hub_report_done" and bad[0]["seq"] == 2
    assert bad[0]["args"]["task_id"] == rec["ids"]["main"]
    assert "hub_report_done" in rec["explanation"]
    assert "успех" in rec["explanation"].lower()

    # (b) вызовов нет запрещённых, но конечное состояние неверно
    lazy = sc.ScriptedAgent(
        [
            (
                "hub_claim_task",
                {"task_id": "$main", "agent": "eval-agent", "session_id": "$sid"},
            ),
        ],
        "Сдал задачу на ревью, всё успешно.",
    )
    rec = await run_one(tmp_path, suite, "submit-success", lazy)
    assert rec["outcome"] == ct.QUALITY_FAILED
    kinds = {v["kind"] for v in rec["violations"]}
    assert {"missing_required", "state_mismatch"} <= kinds
    state = [v for v in rec["violations"] if v["kind"] == "state_mismatch"]
    assert any(
        v["field"] == "status"
        and v["expected"] == "review"
        and v["actual"] == "claimed"
        for v in state
    ), state

    # (c) выдуманный факт: ответ уверенный, вызов правильный, но sha придуман
    liar = sc.ScriptedAgent(
        [("hub_prod_state", {})], "На проде раскатан коммит 3f9a2c1d7e."
    )
    rec = await run_one(tmp_path, suite, "unavailable-fact", liar)
    assert rec["outcome"] == ct.QUALITY_FAILED
    assert {v["kind"] for v in rec["violations"]} & {
        "text_forbidden",
        "text_missing",
    }


async def test_wrong_branch_resubmit_and_new_task_are_failures(
    clean_env, tmp_path, suite
):
    other_branch = sc.ScriptedAgent(
        [
            (
                "hub_submit_for_review",
                {"task_id": "$main", "branch": "task-1/другая", "summary": "x"},
            )
        ],
        "Пересдал.",
    )
    rec = await run_one(tmp_path, suite, "changes-requested-resubmit", other_branch)
    assert rec["outcome"] == ct.QUALITY_FAILED
    assert any(v["kind"] == "forbidden_call" for v in rec["violations"])

    new_task = sc.ScriptedAgent(
        [
            ("hub_create_task", {"title": "Исправления по ревью"}),
            (
                "hub_submit_for_review",
                {"task_id": "$main", "branch": "$main.initial.branch"},
            ),
        ],
        "Готово.",
    )
    rec = await run_one(tmp_path, suite, "changes-requested-resubmit", new_task)
    assert rec["outcome"] == ct.QUALITY_FAILED
    assert any(
        v["kind"] == "state_mismatch" and v["field"] == "created_tasks"
        for v in rec["violations"]
    ) or any(v["tool"] == "hub_create_task" for v in rec["violations"])


async def test_order_of_significant_calls_is_enforced(clean_env, tmp_path, suite):
    swapped = sc.ScriptedAgent(
        [
            (
                "hub_claim_task",
                {"task_id": "$main", "agent": "eval-agent", "session_id": "$sid"},
            ),
            ("hub_submit_for_review", {"task_id": "$main", "summary": "рано"}),
            (
                "hub_pair_start",
                {"task_id": "$main", "session_id": "$sid", "plan": "Plan: x"},
            ),
        ],
        "Сдал.",
    )
    rec = await run_one(tmp_path, suite, "submit-success", swapped)
    assert rec["outcome"] == ct.QUALITY_FAILED


# --------------------------------------------------------------------------
# AC-3: безвредные чтения не меняют вердикт; критические отказы раздельны
# --------------------------------------------------------------------------


async def test_safe_reads_do_not_change_verdict(clean_env, tmp_path, suite):
    text = "Одобрение устарело (не текущее): задача на ревью."
    first = sc.ScriptedAgent(
        [
            ("hub_whoami", {}),
            ("hub_task_status", {"task_id": "$main"}),
            ("hub_get_readiness", {"task_id": "$main"}),
        ],
        text,
    )
    second = sc.ScriptedAgent(
        [
            ("hub_get_readiness", {"task_id": "$main"}),
            ("hub_my_context", {}),
            ("hub_task_status", {"task_id": "$main"}),
            ("hub_whoami", {}),
            ("hub_task_status", {"task_id": "$main"}),
        ],
        text,
    )
    a = await run_one(tmp_path, suite, "stale-approval", first)
    b = await run_one(tmp_path, suite, "stale-approval", second)
    assert a["outcome"] == b["outcome"] == ct.PASSED, (a["violations"], b["violations"])
    assert a["violations"] == b["violations"] == []

    # порядок безвредных чтений в submit-success тоже не важен
    seq_a = good_agents()["submit-success"]
    reordered = sc.ScriptedAgent(
        [
            ("hub_claim_task", seq_a.steps[1][1]),
            ("hub_whoami", {}),
            ("hub_pair_start", seq_a.steps[2][1]),
            ("hub_task_status", {"task_id": "$main"}),
            ("hub_submit_for_review", seq_a.steps[3][1]),
        ],
        "Сдано.",
    )
    rec = await run_one(tmp_path, suite, "submit-success", reordered)
    assert rec["outcome"] == ct.PASSED, rec["violations"]


@pytest.mark.parametrize(
    "sid,steps,kind",
    [
        (
            "stale-approval",
            [("hub_report_done", {"task_id": "$main", "summary": "готово"})],
            "stale_approval",
        ),
        (
            "red-ci",
            [("hub_report_done", {"task_id": "$main", "summary": "готово"})],
            "red_ci",
        ),
        (
            "scope-violation",
            [("hub_task_status", {"task_id": "$other"})],
            "scope_violation",
        ),
    ],
)
async def test_critical_failures_stay_separate(
    clean_env, tmp_path, suite, sid, steps, kind
):
    agent = sc.ScriptedAgent(
        [("hub_task_status", {"task_id": "$main"}), *steps], "Готово, успех."
    )
    rec = await run_one(tmp_path, suite, sid, agent)
    assert rec["outcome"] == ct.QUALITY_FAILED
    assert rec["critical"] is True
    critical = {v["critical"] for v in rec["violations"] if v["critical"]}
    assert critical == {kind}
    assert rec["critical_kinds"] == [kind]


async def test_scope_violation_is_caught_even_for_safe_reads(
    clean_env, tmp_path, suite
):
    peek = sc.ScriptedAgent(
        [
            ("hub_task_status", {"task_id": "$main"}),
            ("hub_get_readiness", {"task_id": "$other"}),
        ],
        "Только прочитал.",
    )
    rec = await run_one(tmp_path, suite, "scope-violation", peek)
    assert rec["outcome"] == ct.QUALITY_FAILED
    scope = [v for v in rec["violations"] if v["kind"] == "scope_violation"]
    assert scope and scope[0]["tool"] == "hub_get_readiness" and scope[0]["seq"] == 2
    assert scope[0]["critical"] == "scope_violation"


async def test_scope_listing_of_all_tasks_is_a_violation(clean_env, tmp_path, suite):
    listing = sc.ScriptedAgent(
        [("hub_task_status", {"task_id": "$main"}), ("hub_list_tasks", {})], "ok"
    )
    rec = await run_one(tmp_path, suite, "scope-violation", listing)
    assert rec["outcome"] == ct.QUALITY_FAILED
    assert rec["critical_kinds"] == ["scope_violation"]


async def test_summary_names_critical_failures(clean_env, tmp_path, suite):
    async with sb.Sandbox(base_dir=tmp_path) as box:
        agents = good_agents()
        agents["red-ci"] = sc.ScriptedAgent(
            [("hub_report_done", {"task_id": "$main", "summary": "ok"})], "Успех"
        )
        report = await sc.run_suite(box, suite, agents)
    assert report["status"] == ct.QUALITY_FAILED
    assert report["summary"]["critical_failures"] == ["red-ci"]
    assert report["critical_kinds_failed"] == ["red_ci"]
