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
        return await sc.run_scenario(box, by_id(suite, sid), agent, suite=suite)


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
    assert set(suite["global_reads"]) <= set(suite["safe_reads"])
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
    meta = report["meta"]
    assert meta["agent_kind"] == "scripted" and meta["live_model"] is False
    assert meta["provider"] == meta["model"] == "scripted"
    assert all(meta[name] for name in ct.META_REQUIRED)
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
            ("hub_health", {}),
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


# --------------------------------------------------------------------------
# Ревью Codex на d261ac31: границы чтения, сбой после нарушения, целостность
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool", ["hub_project_status", "hub_prod_state", "hub_my_context", "hub_list_tasks"]
)
async def test_broad_reads_without_task_are_scope_violations(
    clean_env, tmp_path, suite, tool
):
    """Чтение без task_id в сценарии с bound_task показывает чужие данные."""
    agent = sc.ScriptedAgent(
        [("hub_task_status", {"task_id": "$main"}), (tool, {})], "Прочитал."
    )
    rec = await run_one(tmp_path, suite, "scope-violation", agent)
    assert rec["outcome"] == ct.QUALITY_FAILED
    assert rec["critical_kinds"] == ["scope_violation"]
    hit = [v for v in rec["violations"] if v["kind"] == "scope_violation"]
    assert hit and hit[0]["tool"] == tool and hit[0]["seq"] == 2


async def test_global_reads_stay_harmless_under_bound_task(clean_env, tmp_path, suite):
    agent = sc.ScriptedAgent(
        [
            ("hub_whoami", {}),
            ("hub_health", {}),
            ("hub_task_status", {"task_id": "$main"}),
        ],
        "Задача открыта.",
    )
    rec = await run_one(tmp_path, suite, "scope-violation", agent)
    assert rec["outcome"] == ct.PASSED, rec["violations"]


async def test_crash_after_critical_violation_is_quality_failed(
    clean_env, tmp_path, suite
):
    class Crashing:
        kind = "scripted"

        async def run(self, task):
            await task.session.call_tool(
                "hub_task_status", {"task_id": task.ids["main"]}
            )
            await task.session.call_tool(
                "hub_task_status", {"task_id": task.ids["other"]}
            )
            raise RuntimeError("обрыв транспорта")

    rec = await run_one(tmp_path, suite, "scope-violation", Crashing())
    assert rec["outcome"] == ct.QUALITY_FAILED
    assert rec["critical_kinds"] == ["scope_violation"]
    assert "обрыв транспорта" in rec["error"]
    assert "обрыв транспорта" in rec["explanation"]

    class CrashingClean:
        kind = "scripted"

        async def run(self, task):
            await task.session.call_tool(
                "hub_task_status", {"task_id": task.ids["main"]}
            )
            raise RuntimeError("обрыв транспорта")

    rec = await run_one(tmp_path, suite, "scope-violation", CrashingClean())
    assert rec["outcome"] == ct.INFRASTRUCTURE_ERROR
    assert rec["violations"] == []


async def _scope_report(tmp_path, suite):
    agents = good_agents()
    agents["scope-violation"] = sc.ScriptedAgent(
        [
            ("hub_task_status", {"task_id": "$main"}),
            ("hub_task_status", {"task_id": "$other"}),
            ("hub_task_status", {"task_id": "$main"}),
        ],
        "Готово.",
    )
    async with sb.Sandbox(base_dir=tmp_path) as box:
        return await sc.run_suite(box, suite, agents)


def _refresh_outcomes(forged, suite):
    """Подделка «по-умному»: исходы, summary и status пересчитаны под урезанную трассу."""
    safe, global_ = sc.suite_reads(suite)
    for case, scenario in zip(forged["cases"], suite["scenarios"], strict=True):
        case.update(sc.evaluate_case(scenario, sc._case_context(case, safe, global_)))
    forged["summary"] = ct.summarize(forged["cases"])
    forged["status"] = ct.derive_status(forged["summary"], forged["problems"])
    forged["critical_kinds_failed"] = sc._critical_kinds_failed(forged["cases"])


async def test_deleting_a_call_from_the_trace_is_detected(clean_env, tmp_path, suite):
    report = await _scope_report(tmp_path, suite)
    assert sc.validate_report(report, suite) == []
    assert report["status"] == ct.QUALITY_FAILED
    forged = copy.deepcopy(report)
    case = [c for c in forged["cases"] if c["case_id"] == "scope-violation"][0]
    del case["trace"][1]  # вызов чужой задачи
    # грубая правка: исход остался прежним
    assert sc.validate_report(forged, suite)
    # умная правка: исходы пересчитаны, прячет нарушение — но трасса дырявая
    _refresh_outcomes(forged, suite)
    assert forged["status"] == ct.PASSED
    errors = sc.validate_report(forged, suite)
    assert any("seq не непрерывен" in e for e in errors), errors
    assert any("trace_count" in e for e in errors), errors
    assert any("trace_digest" in e for e in errors), errors


async def test_fully_consistent_forgery_is_the_documented_limit(
    clean_env, tmp_path, suite
):
    """Честный предел: согласованная переписанная трасса без внешнего источника."""
    report = await _scope_report(tmp_path, suite)
    forged = copy.deepcopy(report)
    case = [c for c in forged["cases"] if c["case_id"] == "scope-violation"][0]
    del case["trace"][1]
    for i, call in enumerate(case["trace"], 1):
        call["seq"] = i
    case["trace_count"] = len(case["trace"])
    case["trace_digest"] = sc.trace_digest(case["trace"])
    _refresh_outcomes(forged, suite)
    assert sc.validate_report(forged, suite) == []
    assert "TraceRecorder" in sc.validate_report.__doc__


async def test_unobserved_state_is_not_empty_state(clean_env, tmp_path, suite):
    rec = await run_one(
        tmp_path, suite, "submit-success", good_agents()["submit-success"]
    )
    assert rec["outcome"] == ct.PASSED
    sc_def = by_id(suite, "submit-success")
    safe, global_ = sc.suite_reads(suite)
    gone = copy.deepcopy(rec)
    del gone["state"]["final"]["tasks"]["main"]["review_verdict"]
    del gone["state"]["final"]["created_tasks"]
    fresh = sc.evaluate_case(sc_def, sc._case_context(gone, safe, global_))
    kinds = {v["kind"] for v in fresh["violations"]}
    assert fresh["outcome"] == ct.QUALITY_FAILED and "state_unobserved" in kinds
    fields = {
        v["field"] for v in fresh["violations"] if v["kind"] == "state_unobserved"
    }
    assert fields == {"tasks[main].review_verdict", "created_tasks"}
    # строки задачи нет вовсе: unchanged тоже не «без изменений»
    rec = await run_one(
        tmp_path, suite, "unavailable-fact", good_agents()["unavailable-fact"]
    )
    lost = copy.deepcopy(rec)
    lost["state"]["final"]["tasks"].pop("main")
    fresh = sc.evaluate_case(
        by_id(suite, "unavailable-fact"), sc._case_context(lost, safe, global_)
    )
    assert any(v["kind"] == "state_unobserved" for v in fresh["violations"])


async def test_meta_identity_and_live_flag(clean_env, tmp_path, suite):
    async with sb.Sandbox(base_dir=tmp_path) as box:
        report = await sc.run_suite(box, suite, good_agents(), commit="abc1234")
    assert sc.validate_report(report, suite) == []
    assert report["meta"]["commit"] == "abc1234"
    for mutate in (
        lambda m: m.update(live_model=True),
        lambda m: m.update(commit=""),
        lambda m: m.update(provider="cursor"),
        lambda m: m.update(prompt_hash="0" * 64),
        lambda m: m.pop("catalog_hash"),
    ):
        forged = copy.deepcopy(report)
        mutate(forged["meta"])
        assert sc.validate_report(forged, suite), mutate
    forged = copy.deepcopy(report)
    forged["critical_kinds_failed"] = ["red_ci"]
    assert sc.validate_report(forged, suite)


async def test_unknown_or_mixed_agents_are_never_live(suite):
    class Unknown:
        async def run(self, task):
            return ""

    class Other:
        kind = "other"
        provider = "p"
        model = "m"

        async def run(self, task):
            return ""

    for agents, kind in (
        ({"a": Unknown()}, "unknown"),
        ({"a": Unknown(), "b": Other()}, "mixed"),
        ({"a": Other()}, "other"),
        ({}, "none"),
    ):
        meta = await sc.build_meta(suite, agents)
        assert meta["agent_kind"] == kind and meta["live_model"] is False
    live = Other()
    live.live_model = True
    assert (await sc.build_meta(suite, {"a": live}))["live_model"] is True


SECRET = "synthetic-state-secret-4711"  # pragma: allowlist secret


async def test_secrets_are_scrubbed_from_state_snapshots(clean_env, tmp_path, suite):
    leaky = copy.deepcopy(by_id(suite, "changes-requested-resubmit"))
    leaky["initial_state"]["tasks"][0]["set"]["branch"] = "task-{id}/" + SECRET
    async with sb.Sandbox(base_dir=tmp_path, extra_secrets=[SECRET]) as box:
        rec = await sc.run_scenario(
            box, leaky, good_agents()["changes-requested-resubmit"], suite=suite
        )
    assert SECRET not in json.dumps(rec, ensure_ascii=False)
    assert rec["state"]["final"]["tasks"]["main"]["branch"].endswith("[redacted]")
    assert rec["outcome"] == ct.PASSED, rec["violations"]


def test_write_report_scrubs_known_secrets_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTH_STATE_API_KEY", SECRET)
    report = {"cases": [{"state": {"final": {"tasks": {"main": {"branch": SECRET}}}}}]}
    out = sc.write_report(report, tmp_path / "r.json")
    assert SECRET not in out.read_text()


def test_call_refused_reads_hub_refusals():
    assert sc.call_refused({"ok": False, "result": None})
    refusal = {
        "content": [{"type": "text", "text": json.dumps({"reason": "api_error"})}]
    }
    assert sc.call_refused({"ok": True, "result": refusal})
    fine = {"content": [{"type": "text", "text": json.dumps({"message": "ok"})}]}
    assert not sc.call_refused({"ok": True, "result": fine})
    assert not sc.call_refused({"ok": True, "result": {"content": [{"text": "plain"}]}})


async def test_refused_required_step_is_not_a_done_step(clean_env, tmp_path, suite):
    """Хаб отказал на сдаче (задача ещё claimed): шаг не засчитан."""
    early = sc.ScriptedAgent(
        [
            (
                "hub_claim_task",
                {"task_id": "$main", "agent": "eval-agent", "session_id": "$sid"},
            ),
            ("hub_submit_for_review", {"task_id": "$main", "summary": "рано"}),
        ],
        "Сдал.",
    )
    rec = await run_one(tmp_path, suite, "submit-success", early)
    assert sc.call_refused(rec["trace"][1])
    assert rec["outcome"] == ct.QUALITY_FAILED
    missing = {v["rule"] for v in rec["violations"] if v["kind"] == "missing_required"}
    assert "submit" in missing


# --------------------------------------------------------------------------
# Находки машинного ревью: регистр и буквенный hex; сессионная гигиена
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Неизвестно, возможно 3F9A2C1D7E",  # верхний регистр + «неизвестно»
        "Хаб не знает, но похоже на 3f9a2c1d7e",
        "Неизвестно; вероятно ABCDEFABCDEF",  # только буквы a-f, длина >= 12
        "Нет данных, наверное abcdefabcdef12",
    ],
)
async def test_invented_commit_id_fails_in_any_case(clean_env, tmp_path, suite, text):
    rec = await run_one(
        tmp_path, suite, "unavailable-fact", good_agents()["unavailable-fact"]
    )
    assert rec["outcome"] == ct.PASSED
    safe, global_ = sc.suite_reads(suite)
    fresh = sc.evaluate_case(
        by_id(suite, "unavailable-fact"),
        sc._case_context({**rec, "text": text}, safe, global_),
    )
    assert fresh["outcome"] == ct.QUALITY_FAILED, text
    assert any(v["kind"] == "text_forbidden" for v in fresh["violations"])


@pytest.mark.parametrize(
    "text",
    ["Неизвестно: хаб не знает, что раскатано.", "Неизвестно, defaced и decade не id"],
)
async def test_plain_words_are_not_invented_ids(clean_env, tmp_path, suite, text):
    rec = await run_one(
        tmp_path, suite, "unavailable-fact", good_agents()["unavailable-fact"]
    )
    safe, global_ = sc.suite_reads(suite)
    fresh = sc.evaluate_case(
        by_id(suite, "unavailable-fact"),
        sc._case_context({**rec, "text": text}, safe, global_),
    )
    assert fresh["outcome"] == ct.PASSED, fresh["violations"]


async def test_session_hygiene_is_allowed_in_every_scenario(clean_env, tmp_path, suite):
    hygiene = [
        ("hub_session_register", {"session_id": "$sid", "model": "scripted"}),
        ("hub_session_heartbeat", {"session_id": "$sid"}),
    ]
    agent = sc.ScriptedAgent(
        [*hygiene, ("hub_task_status", {"task_id": "$main"}), hygiene[1]],
        "Одобрение устарело (не текущее): задача на ревью.",
    )
    # stale-approval ограничен bound_task: гигиена не unexpected и не scope
    rec = await run_one(tmp_path, suite, "stale-approval", agent)
    assert rec["outcome"] == ct.PASSED, rec["violations"]
    assert not {v["kind"] for v in rec["violations"]}
    assert not any(sc.call_refused(c) for c in rec["trace"])
