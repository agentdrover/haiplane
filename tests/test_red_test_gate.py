"""Гейт красного теста (#913): падение до фикса берётся из прогона CI.

Багфикс сдавался без доказательства воспроизведения: единственным следом были
мутации сдачи (#1436) — слова автора. Теперь CI прогоняет изменённые тестовые
файлы ветки поверх кода merge-base (scripts/red_test_baseline.py) и отдаёт
статус каждого теста полем ``baseline`` в отчёте о закреплённом коммите. Хаб
решает, что из этого относится к AC, и по политике проекта ``bug_red_test``
(off|warn|require) пропускает сдачу бага или отказывает ей.

Красное — только ``failed``: ``error`` (ImportError, падение сборки) значит,
что тест упал по неверной причине и ничего не воспроизвёл.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess  # nosec B404 - the test drives git and the baseline script
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import cli
from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import AcceptanceCriterion, TaskRefine, validated_gate_policy
from hub.services import red_test_gate
from hub.services.project_policy import BUG_RED_TEST_KEY, bug_red_test_of

_ROOT = Path(__file__).resolve().parents[1]
_BASELINE_SCRIPT = _ROOT / "scripts" / "red_test_baseline.py"
_REPORTER = _ROOT / "scripts" / "ci_report_to_hub.py"

_TIP = "c" * 40
_AREAS = ["hub/example.py", "tests/test_example.py"]
_REF_1 = "tests/test_example.py::test_one"
_REF_2 = "tests/test_example.py::test_two"
_UNPROVEN = "bug_red_test_unproven"


class _PinnedGitOps(NoopGitOps):
    async def fetch_base(self, repo: str, base: str):
        return True, ""

    async def head_sha(self, repo: str, base: str) -> str:
        return _TIP

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        return list(_AREAS)


async def _node(
    db: aiosqlite.Connection, *, title: str, task_type: str, parent_id: int | None
) -> int:
    return await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type=task_type,
        parent_id=parent_id,
        priority="medium",
    )


def _ac(ac_id: str, verifiable_by: str, test_ref: str | None) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=ac_id,
        given="дано",
        when="когда",
        then="тогда",
        verifiable_by=verifiable_by,
        test_ref=test_ref,
    )


async def _running_bug(
    db: aiosqlite.Connection,
    slug: str,
    mode: str | None,
    *,
    work_type: str = "bug",
) -> int:
    """Pair-задача в running: два AC с тестом и один ручной."""
    pid = await repo.create_project(
        db, slug=slug, name=slug.title(), workspace_path="/tmp/ws"
    )
    if mode is not None:
        await repo.update_project(
            db, pid, gate_policy=json.dumps({BUG_RED_TEST_KEY: mode})
        )
    epic = await _node(db, title="epic", task_type="epic", parent_id=None)
    await repo.update_task(db, epic, project_id=pid)
    feature = await _node(db, title="feature", task_type="feature", parent_id=epic)
    task_id = await _node(db, title="bug", task_type="task", parent_id=feature)
    await repo.update_task(db, task_id, work_type=work_type)
    await repo.add_task_update(db, task_id, "dev", "status", "Plan: work")
    await repo.update_task_structured(db, task_id, TaskRefine(affected_areas=_AREAS))
    await repo.add_acceptance_criterion(db, task_id, _ac("AC-1", "test", _REF_1))
    await repo.add_acceptance_criterion(db, task_id, _ac("AC-2", "test", _REF_2))
    await repo.add_acceptance_criterion(db, task_id, _ac("AC-3", "manual", None))
    await db.commit()

    plugins.git_ops = _PinnedGitOps()
    started = await services.pair_start_task(db, task_id, caller="dev-agent")
    assert started.status.value == "running"
    return task_id


async def _report_baseline(
    client: AsyncClient, task_id: int, baseline: dict[str, Any] | None
) -> dict[str, Any]:
    """Отчёт CI о закреплённом коммите — через REST, как его шлёт репортёр."""
    body: dict[str, Any] = {"head_sha": _TIP, "ac_results": {}}
    if baseline is not None:
        body["baseline"] = baseline
    resp = await _post_as_ci(client, task_id, body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _post_as_ci(client: AsyncClient, task_id: int, body: dict[str, Any]):
    """POST ci-run-report под токеном с tasks.ci_report — как ходит CI."""
    from hub import config

    tokens = config.parse_tokens("ci:ci-token:admin")
    with (
        patch.object(config, "HUB_TOKENS", tokens),
        patch.object(config, "HUB_AUTH_DISABLED", False),
    ):
        return await client.post(
            f"/api/tasks/{task_id}/ci-run-report",
            json=body,
            headers={"Authorization": "Bearer ci-token"},
        )


def _baseline(tests: dict[str, str], **extra: Any) -> dict[str, Any]:
    return {"state": "ran", "merge_base": "b" * 40, "tests": tests, **extra}


async def _feed(db: aiosqlite.Connection, task_id: int) -> list[dict[str, Any]]:
    return [dict(u) for u in await repo.get_task_updates(db, task_id)]


def _detail(resp) -> dict[str, Any]:
    body = resp.json()
    detail = body.get("detail", body)
    assert isinstance(detail, dict), resp.text
    return detail


async def _submit(client: AsyncClient, task_id: int, **extra: Any):
    body = {"model": "claude-opus-5-5", "summary": "починено", **extra}
    with patch(
        "hub.services.review_dispatch.maybe_dispatch_review", new=AsyncMock()
    ) as dispatch:
        resp = await client.post(f"/api/tasks/{task_id}/submit-review", json=body)
    return resp, dispatch


async def _assert_nothing_recorded(db, task_id: int, feed_before: int, dispatch):
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "running"
    assert row["submission_generation"] == 0
    assert await repo.get_submission(db, task_id, 1) is None
    dispatch.assert_not_awaited()
    assert len(await _feed(db, task_id)) == feed_before


# --------------------------------------------------------------------------
# AC-1: require отказывает багу без красного baseline — до записи поколения
# --------------------------------------------------------------------------


async def test_bug_without_failing_baseline_is_refused(client: AsyncClient, db):
    task_id = await _running_bug(db, "red-req", "require")
    feed_before = len(await _feed(db, task_id))

    # Отчёта о закреплённом коммите нет вовсе — «нет baseline» по каждому AC.
    resp, dispatch = await _submit(client, task_id)
    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["reason"] == _UNPROVEN
    by_ac = {v.split(" ", 1)[0]: v for v in detail["violations"]}
    assert set(by_ac) == {"AC-1", "AC-2"}, "ручной AC доказательства не требует"
    for ac_id, ref in (("AC-1", _REF_1), ("AC-2", _REF_2)):
        assert ref in by_ac[ac_id]
        assert "нет baseline" in by_ac[ac_id], by_ac[ac_id]
    assert _TIP[:12] in detail["message"]
    await _assert_nothing_recorded(db, task_id, feed_before, dispatch)

    # Отчёт есть, но AC-1 был зелёным до фикса: отказ называет только его.
    await _report_baseline(
        client, task_id, _baseline({_REF_1: "passed", _REF_2: "failed"})
    )
    feed_before = len(await _feed(db, task_id))
    resp, dispatch = await _submit(client, task_id)
    assert resp.status_code == 422, resp.text
    violations = _detail(resp)["violations"]
    assert len(violations) == 1, violations
    assert violations[0].startswith("AC-1") and "зелёный до фикса" in violations[0]
    await _assert_nothing_recorded(db, task_id, feed_before, dispatch)

    # Отчёт без поля baseline (старый репортёр) — тоже «нет baseline».
    await _report_baseline(client, task_id, None)
    resp, _ = await _submit(client, task_id)
    assert resp.status_code == 422, resp.text
    assert all("нет baseline" in v for v in _detail(resp)["violations"])


# --------------------------------------------------------------------------
# AC-2: каждый тест AC — failed в baseline: пропуск, доказательство в карточке
# --------------------------------------------------------------------------


async def test_baseline_failure_evidence_accepted(client: AsyncClient, db):
    task_id = await _running_bug(db, "red-ok", "require")
    reported = await _report_baseline(
        client,
        task_id,
        _baseline({_REF_1: "failed", _REF_2: "failed", "tests/x.py::t": "passed"}),
    )
    assert reported["baseline_state"] == "ran"

    resp, dispatch = await _submit(client, task_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "review"
    assert resp.json()["submission_generation"] == 1
    dispatch.assert_awaited_once()

    status = await client.get(f"/api/tasks/{task_id}")
    card = [u["content"] or "" for u in status.json()["updates"]]
    evidence = [c for c in card if c.startswith(red_test_gate.EVIDENCE_HEADER)]
    assert len(evidence) == 1, card
    text = evidence[0]
    assert f"AC-1 {_REF_1} — failed" in text
    assert f"AC-2 {_REF_2} — failed" in text
    assert _TIP[:12] in text and ("b" * 12) in text, "коммит и merge-base названы"
    assert not any(c.startswith(red_test_gate.ALERT_HEADER) for c in card)


# --------------------------------------------------------------------------
# AC-3: мутации и слова автора — не доказательство; error — не красный
# --------------------------------------------------------------------------


async def test_agent_claim_without_run_is_not_evidence(client: AsyncClient, db):
    task_id = await _running_bug(db, "red-claim", "require")
    claimed = [
        {"ac": "AC-1", "mutation": "вернул баг", "failed_test": _REF_1},
        {"ac": "AC-2", "mutation": "вернул баг", "failed_test": _REF_2},
    ]
    resp, dispatch = await _submit(
        client,
        task_id,
        summary="оба теста падали до фикса, проверено руками",
        mutations=claimed,
    )
    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["reason"] == _UNPROVEN
    assert all("нет baseline" in v for v in detail["violations"])
    assert "mutations" in detail["hint"], "подсказка говорит, что мутации не в счёт"
    dispatch.assert_not_awaited()

    # error — ImportError или падение сборки — за воспроизведение не считается.
    await _report_baseline(
        client,
        task_id,
        _baseline(
            {_REF_1: "error", _REF_2: "failed"},
            collection_errors={},
        ),
    )
    resp, _ = await _submit(client, task_id, mutations=claimed)
    assert resp.status_code == 422, resp.text
    violations = _detail(resp)["violations"]
    assert len(violations) == 1 and violations[0].startswith("AC-1"), violations
    assert "неверной причине" in violations[0] and "error" in violations[0]

    # Файл теста не собрался на базе — nodeid в baseline нет, но это error,
    # а не «нет baseline»: причина названа точнее.
    await _report_baseline(
        client,
        task_id,
        _baseline(
            {_REF_2: "failed"},
            collection_errors={"tests/test_example.py": "ImportError: no name x"},
        ),
    )
    resp, _ = await _submit(client, task_id)
    violations = _detail(resp)["violations"]
    assert len(violations) == 1 and violations[0].startswith("AC-1"), violations
    assert "не собрался" in violations[0]


# --------------------------------------------------------------------------
# AC-4: шаг CI на настоящем git — тест ветки поверх кода merge-base
# --------------------------------------------------------------------------


def _git(repo_dir: Path, *args: str) -> str:
    out = subprocess.run(  # nosec B603 B607 - fixed git argv in a temp repo
        ["git", *args],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _write(repo_dir: Path, rel: str, text: str) -> None:
    path = repo_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _bug_repo(tmp_path: Path) -> Path:
    """develop с багом; ветка task-* — фикс и новый тест одним коммитом."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-q", "-b", "develop")
    _git(repo_dir, "config", "user.email", "t@example.invalid")
    _git(repo_dir, "config", "user.name", "t")
    _git(repo_dir, "config", "commit.gpgsign", "false")
    _write(repo_dir, "calc.py", "def add(a, b):\n    return a - b\n")
    _write(repo_dir, "tests/test_old.py", "def test_untouched():\n    pass\n")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "base with a bug")

    _git(repo_dir, "checkout", "-q", "-b", "task-1/fix-add")
    _write(
        repo_dir,
        "calc.py",
        "def add(a, b):\n    return a + b\n\n\ndef twice(a):\n    return 2 * a\n",
    )
    _write(
        repo_dir,
        "tests/test_calc.py",
        "from calc import add\n\n\n"
        "def test_add():\n    assert add(2, 2) == 4\n\n\n"
        "def test_twice_inside():\n    from calc import twice\n\n"
        "    assert twice(2) == 4\n",
    )
    _write(
        repo_dir,
        "tests/test_new_api.py",
        "from calc import twice\n\n\ndef test_twice():\n    assert twice(3) == 6\n",
    )
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "fix add + its test, one commit")
    return repo_dir


def _load_reporter():
    spec = importlib.util.spec_from_file_location("ci_report_to_hub", _REPORTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_baseline_step_runs_branch_tests_on_merge_base(tmp_path, monkeypatch):
    repo_dir = _bug_repo(tmp_path)
    head = _git(repo_dir, "rev-parse", "HEAD")
    out = tmp_path / "baseline.json"

    run = subprocess.run(  # nosec B603 - the script under test, fixed argv
        [
            sys.executable,
            str(_BASELINE_SCRIPT),
            "--base",
            "develop",
            "--json-out",
            str(out),
        ],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert run.returncode == 0, "падение на базе — данные, джоб не краснеет"
    report = json.loads(out.read_text(encoding="utf-8"))

    assert report["state"] == "ran", report
    assert report["head"] == head
    assert report["merge_base"] == _git(repo_dir, "merge-base", "develop", head)
    tests = report["tests"]
    # Новый тест запущен поверх кода базы (там add ещё вычитает) — failed.
    assert tests["tests/test_calc.py::test_add"] == "failed", tests
    # Импорт внутри теста того, чего на базе нет, — error, а не красный.
    assert tests["tests/test_calc.py::test_twice_inside"] == "error", tests
    # Модуль, не собравшийся на импорте, — error по каждому его тесту.
    assert "tests/test_new_api.py" in report["collection_errors"]
    assert (
        red_test_gate.baseline_status(report, "tests/test_new_api.py::test_twice")
        == "error"
    )
    # Неизменённые тестовые файлы не гоняются, рабочее дерево ветки не тронуто.
    assert not any(n.startswith("tests/test_old.py") for n in tests)
    assert _git(repo_dir, "status", "--porcelain") == ""
    assert _git(repo_dir, "worktree", "list").count("\n") == 0, "worktree убран"

    # Шаг стоит в CI: не красит джоб, только на ветках task-*, его JSON
    # доезжает до репортёра, а репортёр кладёт его отдельным ключом.
    import yaml

    workflow = _ROOT / ".github" / "workflows" / "ci.yml"
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    steps = [s for job in doc["jobs"].values() for s in job.get("steps") or []]
    step = next(s for s in steps if s.get("id") == "baseline")
    assert step["continue-on-error"] is True
    assert "task-" in step["if"]
    assert "scripts/red_test_baseline.py" in step["run"]
    reporter_step = next(s for s in steps if "hub-ci-report" in str(s.get("uses")))
    assert steps.index(step) < steps.index(reporter_step)
    assert reporter_step["with"]["baseline-file"] in step["run"]

    reporter = _load_reporter()
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.example")
    monkeypatch.setenv("HAIPLANE_HUB_CI_TOKEN", "irrelevant")  # noqa: S105
    monkeypatch.setenv("GITHUB_HEAD_REF", "task-913/x")
    monkeypatch.setenv("HEAD_SHA", head)
    monkeypatch.setenv("HAIPLANE_HUB_CI_BASELINE", str(out))
    for suffix in ("HUB_CI_PYTEST", "HUB_CI_CHECKS", "HUB_CI_MUTATIONS"):
        monkeypatch.delenv(f"HAIPLANE_{suffix}", raising=False)
    sent: dict[str, Any] = {}

    def fake_request(url, token, payload=None):
        if payload is None:
            return {"acceptance_criteria": [], "validation_commands": []}
        sent.update(payload)
        return {"applied": False, "reason": "x", "baseline_state": "ran"}

    monkeypatch.setattr(reporter, "hub_request", fake_request)
    assert reporter.main() == 0
    assert sent["baseline"]["tests"] == tests
    assert "baseline" not in sent["checks"]


# --------------------------------------------------------------------------
# Механика вокруг AC: политика, warn, off, поверхности
# --------------------------------------------------------------------------


def test_bug_red_test_policy_reads_off_warn_require():
    assert bug_red_test_of({}) == "off"
    assert bug_red_test_of(None) == "off"  # type: ignore[arg-type]
    for mode in ("off", "warn", "require"):
        assert bug_red_test_of({BUG_RED_TEST_KEY: mode}) == mode
    # Ключ есть, но значение нечитаемое — warn: гейт явно хотели.
    assert bug_red_test_of({BUG_RED_TEST_KEY: "requier"}) == "warn"
    assert bug_red_test_of({BUG_RED_TEST_KEY: True}) == "warn"
    assert validated_gate_policy({BUG_RED_TEST_KEY: "warn"}) == {
        BUG_RED_TEST_KEY: "warn"
    }
    with pytest.raises(ValueError, match=BUG_RED_TEST_KEY):
        validated_gate_policy({BUG_RED_TEST_KEY: "strict"})


async def test_warn_writes_one_record_and_off_changes_nothing(client: AsyncClient, db):
    task_id = await _running_bug(db, "red-warn", "warn")
    await _report_baseline(client, task_id, _baseline({_REF_1: "failed"}))
    resp, dispatch = await _submit(client, task_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "review"
    dispatch.assert_awaited_once()
    card = [u["content"] or "" for u in await _feed(db, task_id)]
    alerts = [c for c in card if c.startswith(red_test_gate.ALERT_HEADER)]
    assert len(alerts) == 1, card
    assert "AC-2" in alerts[0] and "AC-1" not in alerts[0].split("Доказано")[0]
    assert f"AC-1 {_REF_1} — failed" in alerts[0], "доказанное тоже названо"

    headers = (red_test_gate.ALERT_HEADER, red_test_gate.EVIDENCE_HEADER)
    cases = (("red-off", "off", "bug"), ("red-none", None, "bug"))
    cases += (("red-feature", "require", "feature"),)
    for slug, mode, work_type in cases:
        task_id = await _running_bug(db, slug, mode, work_type=work_type)
        resp, _ = await _submit(client, task_id)
        assert resp.status_code == 200, (slug, resp.text)
        card = [u["content"] or "" for u in await _feed(db, task_id)]
        assert not any(c.startswith(headers) for c in card), slug


async def test_ci_report_keeps_baseline_and_refuses_unknown_status(
    client: AsyncClient, db
):
    task_id = await _running_bug(db, "red-intake", "off")
    stored_absent = await _report_baseline(client, task_id, None)
    assert stored_absent["baseline_state"] == "not_reported"
    row = dict(await repo.get_ci_run_report(db, task_id, _TIP))
    assert row["baseline"] == "{}"

    baseline = _baseline({_REF_1: "failed"})
    await _report_baseline(client, task_id, baseline)
    row = dict(await repo.get_ci_run_report(db, task_id, _TIP))
    assert json.loads(row["baseline"]) == baseline
    assert json.loads(row["mutations"]) == {}, "рядом с mutations, не вместо"

    bad = await _post_as_ci(
        client, task_id, {"head_sha": _TIP, "baseline": _baseline({_REF_1: "red"})}
    )
    assert bad.status_code == 400, bad.text
    assert "baseline" in bad.text
    huge = await _post_as_ci(
        client,
        task_id,
        {"head_sha": _TIP, "baseline": {"state": "ran", "blob": "x" * 40_000}},
    )
    assert huge.status_code == 400, huge.text


def test_cli_prints_the_refusal_per_ac(capsys):
    detail = {
        "reason": _UNPROVEN,
        "message": "bug_red_test (require) refused",
        "violations": [f"AC-1 {_REF_1}: нет baseline", f"AC-2 {_REF_2}: error"],
        "hint": "Ничего не записано.",
    }
    cli._print_http_error(422, json.dumps({"detail": detail}))
    err = capsys.readouterr().err
    assert "  - AC-1 " in err and "  - AC-2 " in err
    assert "Ничего не записано." in err
    assert "bug_red_test" in cli.SUBMIT_REVIEW_EPILOG


async def test_mcp_submit_describes_the_gate_and_relays_the_refusal():
    from hub import mcp_server
    from hub.mcp_server import HubApiError, hub_submit_for_review, mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    assert "bug_red_test" in (tools["hub_submit_for_review"].description or "")

    payload = {
        "reason": _UNPROVEN,
        "actor_hint": "agent",
        "violations": [f"AC-1 {_REF_1}: нет baseline"],
    }
    with (
        patch.object(mcp_server, "_api_get", new=AsyncMock(return_value={})),
        patch.object(
            mcp_server,
            "_api_post",
            new=AsyncMock(side_effect=HubApiError(payload)),
        ),
    ):
        out = json.loads(await hub_submit_for_review(7, model="m", summary="s"))
    assert out["reason"] == _UNPROVEN
    assert out["violations"] == payload["violations"]


def test_cli_submit_review_carries_no_evidence_of_its_own():
    """Доказательство идёт от CI: у CLI нет флага, которым его можно заявить."""
    api = MagicMock(return_value={"id": 7, "status": "review"})
    argv = ["oc-hub", "submit-review", "7", "--model", "m", "--summary", "s"]
    with patch.object(sys, "argv", argv), patch.object(cli, "_api", api):
        assert cli.main() == 0
    _, _, body = api.call_args.args
    assert "baseline" not in body
