"""The hub's own workflow keeps its env contract (Wave 5: canonical only).

``.github/workflows/ci.yml`` is read as TEXT on purpose: the contract under
test is literal — which secret expressions the workflow evaluates and which
env key names its shell bodies read. The deploy-callback ``run:`` reads
``$HAIPLANE_HUB_URL``, so the env KEY names and the ``run:`` body must move
together. A renamed key with an unchanged shell body keeps CI green and
silently stops deploy reporting — exactly the failure this file exists to
make loud.
"""

from __future__ import annotations

import re

import pytest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

URL_SECRET = "${{ secrets.HAIPLANE_HUB_URL }}"
TOKEN_SECRET = "${{ secrets.HAIPLANE_HUB_CI_TOKEN }}"

# Собрано конкатенацией: контракт Волны 5 в том, что этих имён в файле НЕТ,
# а страж (tests/test_no_legacy_name.py) не должен ловить сам контракт.
LEGACY_PREFIX = "OPEN" + "CLAW" + "_"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_deploy_callback_env_keys_match_run_body() -> None:
    text = _text()
    # The env KEYS are canonical — the shell bodies below read them by name.
    assert f"HAIPLANE_HUB_URL: {URL_SECRET}" in text
    assert f"HAIPLANE_HUB_CI_TOKEN: {TOKEN_SECRET}" in text
    # The run: body reads the same canonical env names.
    assert '"$HAIPLANE_HUB_URL/api/deploys"' in text
    assert "Bearer $HAIPLANE_HUB_CI_TOKEN" in text


def test_report_and_audit_use_canonical_secrets() -> None:
    text = _text()
    # Collect step inputs (the hub-ci-report composite action): the URL and the
    # READ key. The report key is not an input of this job any more (#1686).
    assert f"hub-url: {URL_SECRET}" in text
    assert "hub-read-token: ${{ secrets.HAIPLANE_HUB_CI_READ_TOKEN }}" in text
    import re

    # `github-token:` of the adoption download is the workflow's own read-only
    # token, not the hub report key; only the bare `hub-token:` input is banned.
    assert not re.search(r"(?<![\w-])hub-token:", text)


def test_no_legacy_names_anywhere() -> None:
    assert LEGACY_PREFIX not in _text().upper(), (
        "Wave 5: the workflow must reference no legacy secret or env name"
    )


def test_manual_runs_buy_checks_not_a_deploy() -> None:
    """#1196: ручной запуск не должен становиться дорогой к выкату.

    Триггер ``workflow_dispatch`` добавлен, чтобы получить прогон на УЖЕ
    существующем коммите (наблюдённый тупик #1185). Джоба ``deploy`` при этом
    обязана остаться недостижимой: её условие требует push-события, а
    ``workflow_dispatch`` им не является. Условие читается ТЕКСТОМ по той же
    причине, что и остальной файл: расширение ``if`` до ручного события
    оставит CI зелёным и молча превратит проверку в выкат.
    """
    text = _text()
    assert "  workflow_dispatch:\n" in text, (
        "триггер ручного запуска должен быть объявлен — без него прогон на "
        "существующем коммите родить нечем"
    )
    assert (
        "github.event_name == 'push' && github.ref == 'refs/heads/main' }}" in text
    ), (
        "deploy обязан требовать push в main: ручной запуск покупает проверки, "
        "а не выкат"
    )


# ---- #1606: mutations and baseline are decided by the GitHub EVENT ------------


def _doc() -> dict:
    import yaml

    return yaml.safe_load(_text())


def _steps(job: str = "test") -> dict:
    return {s.get("id"): s for s in _doc()["jobs"][job]["steps"] if s.get("id")}


def _decide(
    tmp_path: Path, *, event: str, action: str, head: str, base: str
) -> tuple[bool, str]:
    """Run the workflow's own decision step and read its answer and log line."""
    import os
    import subprocess

    step = _steps("treedup")["evidence"]
    out = tmp_path / "github_output"
    out.write_text("")
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_OUTPUT": str(out),
        # The names the step's env: block hands the script.
        **{k: _expr(v, event, action, head, base) for k, v in step["env"].items()},
    }
    done = subprocess.run(
        ["bash", "-c", step["run"]], env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    answers = [ln for ln in out.read_text().splitlines() if ln.startswith("run=")]
    assert len(answers) == 1, out.read_text()
    return answers[0] == "run=true", done.stdout


def _expr(value: str, event: str, action: str, head: str, base: str) -> str:
    """Evaluate the few github.* expressions the decision step is allowed to use."""
    ctx = {
        "github.event_name": event,
        "github.event.action": action,
        "github.head_ref": head if event == "pull_request" else "",
        "github.ref_name": head,
        "github.base_ref": base,
    }
    text = value.strip()
    assert text.startswith("${{") and text.endswith("}}"), text
    parts = [p.strip() for p in text[3:-2].split("||")]
    for part in parts:
        if part in ctx:
            if ctx[part]:
                return ctx[part]
        else:
            return part.strip("'")
    return ""


def test_mutations_and_baseline_skip_branch_sync_and_non_task_prs(tmp_path) -> None:
    """#1606 AC-1: only a manual run or a PR opening of a task branch pays for them."""
    cases = [
        # (event, action, head, base, expected, reason fragment)
        ("workflow_dispatch", "", "task-9/x", "", True, ""),
        ("pull_request", "opened", "task-9/x", "develop", True, ""),
        ("pull_request", "reopened", "task-9/x", "develop", True, ""),
        ("pull_request", "synchronize", "task-9/x", "develop", False, "synchronize"),
        ("pull_request", "opened", "develop", "main", False, "task"),
        ("pull_request", "synchronize", "develop", "main", False, "task"),
        ("pull_request", "opened", "main", "develop", False, "task"),
        ("pull_request", "opened", "dependabot/pip/x", "develop", False, "task"),
        ("push", "", "develop", "", False, "push"),
        ("workflow_dispatch", "", "develop", "", False, "task"),
    ]
    for event, action, head, base, expected, fragment in cases:
        ran, log = _decide(tmp_path, event=event, action=action, head=head, base=base)
        label = (event, action, head, base)
        assert ran is expected, label
        if not expected:
            lines = [ln for ln in log.splitlines() if ln.strip()]
            assert len(lines) == 1, f"the reason is ONE line: {label} {log!r}"
            assert "skipped" in lines[0] and fragment in lines[0], (label, lines)

    # #1785/#1921: the mutation legs and the baseline are jobs of their own, gated
    # by the decision of job treedup (the same answers, moved).
    steps = {**_steps("mutation_legs"), **_steps("baseline")}
    gate = "needs.treedup.outputs.evidence_run == 'true'"
    for job in ("mutation_legs", "baseline", "evidence"):
        assert gate in _doc()["jobs"][job]["if"], job
        assert "github.event_name == 'pull_request'" not in _doc()["jobs"][job]["if"]

    # The base argument is the PR base, and develop for a manual run: the
    # ACTUAL argument is evaluated, not just grepped for.
    for step_id in ("mutations", "baseline"):
        run = steps[step_id]["run"]
        assert "origin/${{ github.base_ref || 'develop' }}" in run, step_id
        assert '--base "origin/${{ github.base_ref }}"' not in run
    assert _expr(
        "${{ github.base_ref || 'develop' }}", "workflow_dispatch", "", "t", ""
    ) == ("develop")

    # The reporter learns whether the evidence was EXPECTED, so absent is not
    # the same as failed (#1785: the decision, not the outcome of a step).
    sent = next(
        s
        for s in _doc()["jobs"]["test"]["steps"]
        if "hub-ci-report" in str(s.get("uses"))
    )["with"]
    assert sent["evidence-run"] == "${{ needs.treedup.outputs.evidence_run }}"
    assert "mutations-outcome" not in sent and "baseline-outcome" not in sent
    assert sent["event-action"] == "${{ github.event.action }}"

    # What must NOT change: the deploy dependency and the tree-dedup scope.
    assert _doc()["jobs"]["deploy"]["needs"] == "test"
    assert _steps("treedup")["treedup"]["if"] == "${{ github.event_name == 'push' }}"


def test_evidence_decision_survives_a_red_earlier_step() -> None:
    """#1606: nothing may skip the decision; a red mutation step must not skip the baseline.

    #1785: the decision is the FIRST step of job treedup and carries no `if`, so
    no failing step can leave its output empty; the baseline keeps `!cancelled()`
    because the mutation step before it may end red.
    """
    treedup_steps = _doc()["jobs"]["treedup"]["steps"]
    assert treedup_steps[0]["id"] == "evidence"
    assert "if" not in treedup_steps[0]
    # #1921: the baseline is a job that does not wait for the legs, so a red leg
    # cannot skip it; the merge runs on !cancelled() for the same reason.
    assert _doc()["jobs"]["baseline"]["needs"] == "treedup"
    assert "!cancelled()" in _doc()["jobs"]["evidence"]["if"]
    ran, _log = _decide(
        Path(__import__("tempfile").mkdtemp()),
        event="pull_request",
        action="opened",
        head="task-9/x",
        base="develop",
    )
    assert ran is True


# ---- #1620: the deploy ships hashed exports of uv.lock ------------------------

UV_VERSION = "0.12.18"
ROOT = Path(__file__).resolve().parents[1]


def _step(job: str, name: str) -> dict:
    for step in _doc()["jobs"][job]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step {name!r} in job {job!r}")


def _uv_versions() -> dict[str, str]:
    """The uv version each place pins: the two setup-uv steps and the Dockerfile."""
    import re

    found: dict[str, str] = {}
    for job in ("test", "pytest", "deploy"):
        step = _step(job, "Install uv")
        assert str(step["uses"]).startswith("astral-sh/setup-uv@")
        found[f"ci:{job}"] = str(step.get("with", {}).get("version", ""))
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pins = set(re.findall(r"ghcr\.io/astral-sh/uv:(\d+\.\d+\.\d+)", docker))
    assert len(pins) == 1, f"the Dockerfile must pin ONE uv version: {pins}"
    found["Dockerfile"] = pins.pop()
    return found


def test_the_deploy_ships_hashed_lock_exports() -> None:
    """AC-3 (#1620): deploy ставит закреплённый uv и Python 3.11, проверяет lock и
    экспортирует два хешированных набора ДО rsync; needs и условие main не тронуты;
    test-задание ставит ровно закоммиченный lock; версия uv одна во всех местах."""
    doc = _doc()
    deploy = doc["jobs"]["deploy"]
    assert deploy["needs"] == "test"
    # #1796: the condition carries its own status check (see the AC-1 test).
    assert "github.event_name == 'push'" in deploy["if"]
    assert "github.ref == 'refs/heads/main'" in deploy["if"]

    # One uv version everywhere: a floating one rewrites uv.lock under us.
    assert _uv_versions() == {
        "ci:test": UV_VERSION,
        "ci:pytest": UV_VERSION,
        "ci:deploy": UV_VERSION,
        "Dockerfile": UV_VERSION,
    }

    names = [s.get("name") for s in deploy["steps"]]
    python = _step("deploy", "Set up Python")
    assert str(python["uses"]).startswith("actions/setup-python@")
    assert str(python["with"]["python-version"]) == "3.11"

    export = _step("deploy", "Export hashed dependency sets from uv.lock")
    lines = [ln.strip() for ln in export["run"].splitlines() if ln.strip()]
    assert lines[0] == "uv lock --check", "lock is checked before it is exported"
    runtime = (
        "uv export --frozen --no-dev --no-emit-project --format requirements-txt "
        "--no-header -o requirements.runtime.txt"
    )
    build = (
        "uv export --frozen --only-group build --no-emit-project --format "
        "requirements-txt --no-header -o requirements.build.txt"
    )
    assert runtime in lines and build in lines, lines
    assert "--no-hashes" not in export["run"], "the sets must stay hashed"

    # Everything happens in the tree the NEXT step rsyncs to staging.
    sync = names.index("Sync working tree to staging")
    for name in ("Install uv", "Set up Python", export["name"]):
        assert names.index(name) < sync, f"{name} must come before the rsync"
    rsync = _step("deploy", "Sync working tree to staging")["run"]
    for excluded in ("requirements.runtime.txt", "requirements.build.txt"):
        assert f"--exclude '{excluded}'" not in rsync, "the exports must reach staging"

    # The test job tests the committed lock, not a re-resolved one.
    install = _step("test", "Install dependencies")
    assert install["run"].strip() == "uv sync --locked --dev"


# ---- #1686: the code of a task branch never sees the report key -----------------

WORKFLOWS = ROOT / ".github" / "workflows"
REPORT_SECRETS = ("HAIPLANE_HUB_CI_TOKEN", "HAIPLANE_HUB_CI_AUDIT_TOKEN")
# The only places that may hold the report key: the trusted workflow that runs
# from the default branch, and the deploy job (main only, environment production).
ALLOWED_HOLDERS = {("ci-report.yml", "report"), ("ci.yml", "deploy")}


def _mentions(node) -> list[str]:
    import json

    text = json.dumps(node, default=str)
    return [name for name in REPORT_SECRETS if name in text]


def test_branch_code_never_sees_the_report_token() -> None:
    """AC-1 (#1686): who may read the report key is a closed list, read from YAML."""
    import re

    import yaml

    holders: set[tuple[str, str]] = set()
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        top = {k: v for k, v in doc.items() if k != "jobs"}
        assert not _mentions(top), f"{path.name}: workflow-level mention"
        for job_id, job in doc["jobs"].items():
            if _mentions(job):
                holders.add((path.name, job_id))
    assert holders == ALLOWED_HOLDERS, holders

    # The composite action and the satellite template are not holders either.
    for action in (ROOT / ".github" / "actions").rglob("*.y*ml"):
        assert "secrets." not in action.read_text(encoding="utf-8"), action

    # The job of the branch's code: no report key, not even as an input name.
    test_job = _doc()["jobs"]["test"]
    for step in test_job["steps"]:
        assert not _mentions(step), step.get("name")
        assert "hub-token" not in (step.get("with") or {}), step.get("name")
    assert "environment" not in test_job, "job test needs no environment secrets"
    names = [s.get("name") for s in test_job["steps"]]
    assert "File audit findings as Hub drafts" not in names
    upload = _step("test", "Upload results for the trusted report")
    assert upload["with"]["name"] == "hub-ci-results-${{ github.run_attempt }}"

    # ci-report.yml: workflow_run only, the default branch, nothing built there.
    report_text = (WORKFLOWS / "ci-report.yml").read_text(encoding="utf-8")
    report = yaml.safe_load(report_text)
    triggers = report.get("on", report.get(True))
    assert set(triggers) == {"workflow_run"}, triggers
    assert triggers["workflow_run"]["workflows"] == ["CI"]
    assert triggers["workflow_run"]["types"] == ["completed"]
    job = report["jobs"]["report"]
    assert job["environment"] == "hub-report"
    checkout = next(s for s in job["steps"] if "checkout" in str(s.get("uses")))
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert checkout["with"]["persist-credentials"] is False
    code = "\n".join(
        line for line in report_text.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("cache", "uv sync", "setup-uv", "setup-python", "pip install"):
        assert forbidden not in code, forbidden
    for step in job["steps"]:
        if "uses" in step:
            assert re.search(r"@[0-9a-f]{40}$", step["uses"]), step["uses"]
    sender = next(s for s in job["steps"] if "run" in s)
    assert sender["run"].strip() == "python3 scripts/ci_trusted_report.py"

    # What the script checks before it sends anything.
    script = (ROOT / "scripts" / "ci_trusted_report.py").read_text(encoding="utf-8")
    assert 'EXPECTED_WORKFLOW_PATH = ".github/workflows/ci.yml"' in script
    assert "head_repository" in script and "ALLOWED_EVENTS" in script


def test_the_timing_read_stays_a_read_of_the_github_api() -> None:
    """#1700: the third send adds no way to run anything from the run's branch."""
    script = (ROOT / "scripts" / "ci_trusted_report.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "eval(", "exec(", "__import__"):
        assert forbidden not in script, forbidden
    assert "/attempts/{attempt}/jobs" in script
    import yaml

    report = yaml.safe_load((WORKFLOWS / "ci-report.yml").read_text(encoding="utf-8"))
    steps = report["jobs"]["report"]["steps"]
    assert [s.get("name") for s in steps] == [
        "Checkout the default branch",
        "Send the results of the finished run",
    ], "the timing rides the existing step"


# ---- #1767: the suite runs as parts, one job reports ----------------------

FULL_SUITE = "uv run pytest -q -n auto --durations=30"
# #1785: the number of parts is the matrix of ci.yml and is read back through
# strategy.job-total; no digit is written here.
SHARD_ENV = "HAIPLANE_TEST_SHARD=${{ matrix.shard }}/${{ strategy.job-total }} "
PART_COMMAND = (
    SHARD_ENV + "uv run pytest -p scripts.pytest_shard -q -n auto --durations=30"
)
_EXPR = {
    "needs.treedup.outputs.skip": "skip",
    "needs.treedup.outputs.evidence_run": "evid",
    "needs.pytest.result": "result",
}


def _holds(
    condition: str,
    skip: str,
    result: str,
    previous_ok: bool = True,
    evidence_run: str = "",
    cancelled: bool = False,
) -> bool:
    """Evaluate the small `${{ ... }}` conditions of these jobs.

    Only the grammar the jobs use is accepted: the three `needs` values, string
    literals, ==, !=, &&, ||, !, parentheses and the status functions always()
    and cancelled(). Anything else fails the test instead of being guessed
    (so a condition that reads the outcome of another job cannot pass
    unnoticed). Like GitHub, a condition with no status function gets an
    implicit `success()`: it holds only if the previous steps of the job
    succeeded (`previous_ok`).
    """
    import re

    text = condition.strip()
    assert text.startswith("${{") and text.endswith("}}"), condition
    code = text[3:-2]
    for name, var in _EXPR.items():
        code = code.replace(name, var)
    has_status = "always()" in code or "cancelled()" in code
    code = code.replace("always()", " True ").replace("cancelled()", " cancelled ")
    code = code.replace("&&", " and ").replace("||", " or ")
    code = re.sub(r"!(?!=)", " not ", code)
    leftover = re.sub(
        r"'[^']*'|\b(?:skip|result|evid|cancelled|and|or|not|True)\b|==|!=|[()\s]",
        "",
        code,
    )
    assert leftover == "", f"unexpected syntax {leftover!r} in {condition!r}"
    value = bool(
        eval(
            code,
            {"__builtins__": {}},
            {
                "skip": skip,
                "result": result,
                "evid": evidence_run,
                "cancelled": cancelled,
            },
        )
    )  # noqa: S307  # nosec B307
    return value if has_status else (previous_ok and value)


def _aggregator_rc(
    result: str, skip: str, previous_ok: bool = True
) -> tuple[str, int | None, str]:
    """(outcome, return code, output) of step `tests` of job test for these needs."""
    import subprocess

    step = _steps()["tests"]
    if not _holds(step["if"], skip, result, previous_ok):
        return "skipped", None, ""
    env = {"PATH": __import__("os").environ["PATH"]}
    for key, value in step["env"].items():
        assert value == "${{ needs.pytest.result }}", (key, value)
        env[key] = result
    done = subprocess.run(
        ["bash", "-c", step["run"]], env=env, capture_output=True, text=True
    )
    return "ran", done.returncode, done.stdout + done.stderr


def test_the_suite_runs_in_three_shards_and_one_job_reports() -> None:
    """AC-3: the job layout, the aggregator table, the reused full-suite line."""
    import itertools
    import json

    doc = _doc()
    jobs = doc["jobs"]

    # treedup: the decision, no matrix; its answer is the job's output.
    treedup = jobs["treedup"]
    assert "strategy" not in treedup
    assert treedup["outputs"] == {
        "skip": "${{ steps.treedup.outputs.skip }}",
        "evidence_run": "${{ steps.evidence.outputs.run }}",
        # #1919: the adoption of the dispatch run's evidence (see its own test).
        "adopt": ADOPT_OUTPUT,
        "adopted_run_id": "${{ steps.adopt.outputs.adopted_run_id }}",
    }

    # pytest: three parts, none of which may hide another's failure, and none
    # of which holds anything the branch's code could use to forge a report.
    part = jobs["pytest"]
    assert part["needs"] == "treedup"
    assert part["strategy"]["fail-fast"] is False
    total = len(part["strategy"]["matrix"]["shard"])
    assert part["strategy"]["matrix"] == {"shard": list(range(1, total + 1))}
    assert "environment" not in part
    assert "secrets." not in json.dumps(part), "job pytest holds no secret"
    for step in part["steps"]:
        assert "hub-ci-report" not in str(step.get("uses")), step.get("name")
        assert "upload-artifact" not in str(step.get("uses")), step.get("name")
        assert "HAIPLANE_HUB" not in json.dumps(step), step.get("name")
    # Every step of a part is skipped when the tree was already green.
    for step in part["steps"]:
        for skip, expected in (("true", False), ("", True), ("false", True)):
            assert _holds(step["if"], skip, "") is expected, (step["name"], skip)

    # test: the single required check, after both, whatever their outcome.
    test = jobs["test"]
    assert test["name"] == "Ruff and pytest"
    assert test["needs"] == ["treedup", "pytest", "evidence", "static"]
    # A cancelled run must still run job test (and end red): `!cancelled()`
    # would skip it, and a skipped required check can count as passed.
    assert test["if"] == "${{ always() }}"
    assert "!cancelled()" not in test["if"] and "always()" in test["if"]
    assert jobs["deploy"]["needs"] == "test"

    # The report artifact and the AC collection live in job test and nowhere
    # else; job evidence uploads its own intermediate artifact (#1785).
    for name, job in jobs.items():
        steps = job["steps"]
        uploads = [
            str(s["with"]["name"])
            for s in steps
            if "upload-artifact" in str(s.get("uses"))
        ]
        has_report = any("hub-ci-report" in str(s.get("uses")) for s in steps)
        assert has_report is (name == "test"), name
        expected = {
            "test": ["hub-ci-results-${{ github.run_attempt }}"],
            "evidence": ["hub-ci-evidence"],
            # #1921: an adopted run keeps the adopted files as the artifact of
            # THIS run right in treedup; the legs and the baseline hand their
            # reports to job evidence under names of their own
            "treedup": ["hub-ci-evidence"],
            "mutation_legs": ["hub-ci-mutations-leg-${{ matrix.leg }}"],
            "baseline": ["hub-ci-baseline"],
            # #1920: the audit file; a name outside the prefix the trusted
            # report reads (hub-ci-results-)
            "static": ["hub-ci-static"],
            # #1996: the evidence of a failed browser scenario (trace, screenshots,
            # server log); `if: failure()`, a name outside the report prefix.
            "gui": ["gui-failure-evidence"],
        }.get(name, [])
        assert uploads == expected, name

    # The last step of job test: any cancellation, even after the aggregator
    # passed, ends the job red (the steps with implicit success() are skipped).
    import subprocess

    last = test["steps"][-1]
    assert last["name"] == "Fail a cancelled run"
    assert last["if"] == "${{ cancelled() }}"
    assert subprocess.run(["bash", "-c", last["run"]], capture_output=True).returncode

    # The aggregator: every outcome of the parts, with and without dedup.
    for skip, result, previous_ok in itertools.product(
        ("", "false", "true"),
        ("success", "failure", "cancelled", "skipped"),
        (True, False),  # False: an earlier step (Lint, install) failed
    ):
        outcome, rc, out = _aggregator_rc(result, skip, previous_ok)
        label = (skip, result, previous_ok, out)
        if result == "success" and skip == "true":
            assert outcome == "skipped", label  # the tree was already green
        elif result == "success":
            assert (outcome, rc) == ("ran", 0), label
        else:
            # a failed, cancelled or skipped part is never green or skipped
            assert outcome == "ran" and rc not in (0, None), label
            assert result in out, label  # and the outcome is named

    # The full-suite line: the reporter keeps it byte for byte, the step it
    # points at is the aggregator, and every part runs exactly that command
    # plus the plugin and its own number.
    reporter = next(s for s in test["steps"] if "hub-ci-report" in str(s.get("uses")))
    lines = [ln.strip() for ln in reporter["with"]["ran-commands"].splitlines()]
    assert f"${{{{ steps.tests.outcome }}}} {FULL_SUITE}" in lines
    part_run = next(s for s in part["steps"] if s.get("id") == "shard")["run"].strip()
    assert part_run == PART_COMMAND
    assert (
        part_run.removeprefix(SHARD_ENV).replace(" -p scripts.pytest_shard", "")
        == FULL_SUITE
    )
    assert "pytest" not in _step("test", "Tests outcome of the parts")["run"]

    # No new collection hook where the AC locators would be read as unknown.
    for conftest in (ROOT / "tests").rglob("conftest.py"):
        text = conftest.read_text(encoding="utf-8")
        for hook in ("pytest_collection_modifyitems", "pytest_collection_finish"):
            assert hook not in text, f"{conftest}: {hook}"


_STATUS_FUNCTION = __import__("re").compile(
    r"\b(?:success|failure|cancelled|always)\(\)"
)


def _status_calls_outside_if(node, path="") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{path}/{key}"
            if key == "if":
                continue  # the only place a status function is allowed
            found += _status_calls_outside_if(value, where)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found += _status_calls_outside_if(value, f"{path}[{i}]")
    elif isinstance(node, str) and _STATUS_FUNCTION.search(node):
        found.append(path)
    return found


def test_status_functions_are_used_only_in_if_conditions() -> None:
    """#1767: GitHub rejects success()/failure()/cancelled()/always() in `env`,
    `run`, `with` and the like (actionlint: "calling function ... is not allowed
    here"). The workflow is walked as YAML, not grepped."""
    assert _status_calls_outside_if(_doc()) == []
    probe = {"jobs": {"j": {"steps": [{"env": {"X": "${{ cancelled() }}"}}]}}}
    assert _status_calls_outside_if(probe) == ["/jobs/j/steps[0]/env/X"]


# ---- #1785: the evidence runs beside the parts ---------------------------------

MUTATIONS_STEP = "Mutations of changed functions (warning only)"
BASELINE_STEP = "Red-test baseline on merge-base (data only)"
EVIDENCE_GATE = "needs.treedup.outputs.evidence_run == 'true'"
EVIDENCE_ARTIFACT = "hub-ci-evidence"


def _same_setup(left: dict, right: dict) -> bool:
    return all(left.get(k) == right.get(k) for k in ("uses", "with", "run", "if"))


# #1921: the adopted run and the recounted run both end in the SAME artifact.
NOT_ADOPTED = "needs.treedup.outputs.adopt != 'true'"
ADOPT_OUTPUT = (
    "${{ steps.adopt.outputs.adopt == 'true' && "
    "steps.adopt_upload.outcome == 'success' && 'true' || 'false' }}"
)


def _setup_steps_equal_to_test(job: dict) -> None:
    by_name = {s["name"]: s for s in _doc()["jobs"]["test"]["steps"]}
    own = {s["name"]: s for s in job["steps"]}
    assert _same_setup(own["Checkout"], by_name["Checkout"])
    for name in (
        "Install uv",
        "Set up Python",
        "Install age and openssh-server (backup tests)",
        "Install dependencies",
    ):
        assert _same_setup(own[name], by_name[name]), name
    assert own["Checkout"]["with"] == {"fetch-depth": 0}


def test_mutations_and_baseline_run_beside_the_parts() -> None:
    """#1785/#1921: the legs, the baseline and the merge; gates and hand-over."""
    import json
    import re

    doc = _doc()
    jobs = doc["jobs"]
    treedup, legs, baseline, evidence, test = (
        jobs["treedup"],
        jobs["mutation_legs"],
        jobs["baseline"],
        jobs["evidence"],
        jobs["test"],
    )

    # The decision is made once, in treedup.
    assert treedup["outputs"]["evidence_run"] == "${{ steps.evidence.outputs.run }}"

    # The legs and the baseline: after treedup only, red without consequence,
    # nothing to forge a report with, and skipped when the evidence was adopted.
    for job in (legs, baseline):
        assert job["needs"] == "treedup"
        assert job["continue-on-error"] is True
        assert "environment" not in job
        dumped = json.dumps(job)
        assert "secrets." not in dumped and "HAIPLANE_HUB" not in dumped
        assert "hub-ci-report" not in dumped and "hub-read-token" not in dumped
        assert job["env"] == test["env"] == {"CI_REQUIRE_ROOT_TESTS": "1"}
        assert job["if"] == "${{ " + EVIDENCE_GATE + " && " + NOT_ADOPTED + " }}"
        _setup_steps_equal_to_test(job)
    # a failing leg never skips the baseline: it does not wait for the legs
    assert baseline["needs"] == "treedup"

    # The legs: one matrix, the number of legs lives only there.
    assert legs["name"] == "Mutations leg ${{ matrix.leg }}/${{ strategy.job-total }}"
    assert legs["strategy"]["fail-fast"] is False
    assert legs["strategy"]["matrix"] == {"leg": [1, 2, 3]}
    own = {s["name"]: s for s in legs["steps"]}
    mutations = own[MUTATIONS_STEP]
    assert mutations["id"] == "mutations"
    assert mutations["continue-on-error"] is True
    assert mutations["timeout-minutes"] == 15
    assert mutations["env"] == {
        "LEG": "${{ matrix.leg }}",
        "LEGS": "${{ strategy.job-total }}",
    }
    run = mutations["run"]
    assert "budget=$((540 / LEGS))" in run
    assert 'if [ "$budget" -lt 60 ]; then budget=60; fi' in run
    assert '--shard "$LEG/$LEGS"' in run
    assert "--baseline-timeout 540" in run
    assert '--budget-seconds "$budget"' in run
    assert "--json-out mutation-report.json" in run
    assert "origin/${{ github.base_ref || 'develop' }}" in run
    hand_over = own["Hand the mutation report of this leg over"]
    assert hand_over["if"] == "${{ always() }}"
    assert hand_over["continue-on-error"] is True
    assert hand_over["with"]["name"] == "hub-ci-mutations-leg-${{ matrix.leg }}"
    assert not hand_over["with"]["name"].startswith("hub-ci-results-")
    assert hand_over["with"]["path"] == "mutation-report.json"
    assert hand_over["with"]["if-no-files-found"] == "ignore"
    assert hand_over is legs["steps"][-1]

    # The baseline job: the same step as before, its own artifact.
    steps_b = {s["name"]: s for s in baseline["steps"]}
    step_b = steps_b[BASELINE_STEP]
    assert step_b["id"] == "baseline" and step_b["continue-on-error"] is True
    assert step_b["timeout-minutes"] == 10
    assert "--json-out red-test-baseline.json" in step_b["run"]
    assert "origin/${{ github.base_ref || 'develop' }}" in step_b["run"]
    hand_b = steps_b["Hand the baseline report over"]
    assert hand_b["with"]["name"] == "hub-ci-baseline"
    assert hand_b["with"]["path"] == "red-test-baseline.json"
    assert hand_b["if"] == "${{ always() }}"

    # The merge: the job name and the artifact the rest of the pipeline knows.
    assert evidence["name"] == "Mutations and red-test baseline"
    assert evidence["needs"] == ["treedup", "mutation_legs", "baseline"]
    assert evidence["if"] == (
        "${{ !cancelled() && " + EVIDENCE_GATE + " && " + NOT_ADOPTED + " }}"
    )
    assert evidence["continue-on-error"] is True
    assert "environment" not in evidence and "env" not in evidence
    dumped_e = json.dumps(evidence)
    assert "secrets." not in dumped_e and "HAIPLANE_HUB" not in dumped_e
    assert "setup-uv" not in dumped_e and "setup-python" not in dumped_e, (
        "the merge installs nothing: standard library only"
    )
    merge = next(
        s for s in evidence["steps"] if "merge_mutation_legs" in str(s.get("run"))
    )
    assert merge["run"].count("python3 -I scripts/merge_mutation_legs.py") == 1
    assert "--legs-dir legs --out mutation-report.json" in merge["run"].replace(
        "\\\n", ""
    ).replace("\n", " ")
    pulls = [s for s in evidence["steps"] if "download-artifact" in str(s.get("uses"))]
    assert [s["with"].get("pattern") or s["with"].get("name") for s in pulls] == [
        "hub-ci-mutations-leg-*",
        "hub-ci-baseline",
    ]
    assert all(s["continue-on-error"] is True for s in pulls)

    # The hand-over to job test: whatever files exist, outside the prefix the
    # trusted report reads.
    upload = next(
        s for s in evidence["steps"] if "upload-artifact" in str(s.get("uses"))
    )
    assert upload["with"]["name"] == EVIDENCE_ARTIFACT
    assert "run_attempt" not in upload["with"]["name"], "partial reruns find it"
    assert upload["with"]["overwrite"] is True
    assert not upload["with"]["name"].startswith("hub-ci-results-")
    assert upload["if"] == "${{ always() }}"
    assert upload["continue-on-error"] is True
    assert upload["with"]["if-no-files-found"] == "ignore"
    assert upload["with"]["retention-days"] == 3, "a late PR run still finds it"
    assert upload["with"]["path"].split() == [
        "mutation-report.json",
        "red-test-baseline.json",
    ]
    assert upload is evidence["steps"][-1]
    assert "hub-ci-results-" in json.dumps(test) and "hub-ci-results-" not in dumped_e

    # Job test: waits for the evidence, has none of its steps, and reads nothing
    # from it except the downloaded files.
    assert test["needs"] == ["treedup", "pytest", "evidence", "static"]
    assert test["if"] == "${{ always() }}"
    by_name = {s["name"]: s for s in test["steps"]}
    ids = {s.get("id") for s in test["steps"]}
    assert not ids & {"evidence", "mutations", "baseline"}
    assert MUTATIONS_STEP not in by_name and BASELINE_STEP not in by_name
    text = json.dumps(test)
    assert not re.search(r"steps\.(mutations|baseline|evidence)\b", text)
    assert "needs.evidence" not in text, "the outcome of job evidence is read nowhere"

    names = [s["name"] for s in test["steps"]]
    download = by_name["Download the evidence files of job evidence"]
    assert "download-artifact" in download["uses"]
    assert download["if"] == "${{ !cancelled() && " + EVIDENCE_GATE + " }}"
    assert download["continue-on-error"] is True
    assert download["with"]["name"] == upload["with"]["name"] == EVIDENCE_ARTIFACT
    assert download["with"]["path"] == "."
    collect = "Collect AC test and validation results for the Hub"
    assert names.index(download["name"]) < names.index(collect)
    assert names[-1] == "Fail a cancelled run", "the last step stays the last"


def test_rerun_failed_of_a_leg_reruns_the_merge_and_the_reporter() -> None:
    """#1921: the way a lost leg is put back lives in the `needs` of three jobs.

    "Re-run failed jobs" takes the failed jobs and everything that needs them. A
    job with a job-level `continue-on-error` that fails has conclusion `failure`
    in the API while the run stays `success` (measured on a throwaway workflow of
    this shape, run 39430519486), so the leg is a failed job. The merge and the
    reporter are rerun with it ONLY while they depend on it: transitively for
    job test. A change that cuts that chain turns "rerun the leg" into "nothing
    sends a second report".
    """
    jobs = _doc()["jobs"]

    def needs(name: str) -> set[str]:
        raw = jobs[name].get("needs", [])
        return {raw} if isinstance(raw, str) else set(raw)

    def dependents(root: str) -> set[str]:
        found, frontier = set(), {root}
        while frontier:
            frontier = {n for n in jobs if needs(n) & frontier} - found
            found |= frontier
        return found

    for leg in ("mutation_legs", "baseline"):
        assert jobs[leg]["continue-on-error"] is True, (
            "a red leg must not redden the run"
        )
        assert {"evidence", "test"} <= dependents(leg), leg
    # the merge runs after a failed leg too (it says what is missing)
    assert "!cancelled()" in jobs["evidence"]["if"]
    # and the reporter is not left behind by a failed merge
    assert jobs["test"]["if"] == "${{ always() }}"


def test_evidence_adoption_keeps_artifact_contract() -> None:
    """#1919/#1921: the adopted files become this run's artifact, in job treedup."""
    import json

    from hub.services import ci_integrity

    doc = _doc()
    jobs = doc["jobs"]
    treedup = jobs["treedup"]

    assert treedup["outputs"]["adopt"] == ADOPT_OUTPUT, (
        "adopt is true only when the upload of the files succeeded too"
    )
    assert (
        treedup["outputs"]["adopted_run_id"]
        == "${{ steps.adopt.outputs.adopted_run_id }}"
    )
    decide = next(s for s in treedup["steps"] if s.get("id") == "adopt")
    # Only opened/reopened PR runs of a task branch look for a source.
    assert decide["if"] == (
        "${{ github.event_name == 'pull_request' && (github.event.action == "
        "'opened' || github.event.action == 'reopened') && "
        "startsWith(github.head_ref, 'task-') }}"
    )
    assert decide["continue-on-error"] is True, "a failure here is a recount"
    assert decide["run"].startswith("python3 -I scripts/adopt_evidence.py decide")
    assert "--out-dir adopted-evidence" in decide["run"]
    assert "secrets." not in json.dumps(decide)
    # The base of the PR run is what the script reads from git, not an input.
    assert "base.sha" not in json.dumps(decide)
    for script in ("scripts/adopt_evidence.py", "scripts/merge_mutation_legs.py"):
        assert script in ci_integrity.CI_SCRIPTS, script

    steps = treedup["steps"]
    names = [s["name"] for s in steps]
    keep = next(s for s in steps if s.get("id") == "adopt_upload")
    assert "upload-artifact" in keep["uses"]
    assert keep["if"] == "${{ steps.adopt.outputs.adopt == 'true' }}"
    assert keep["continue-on-error"] is True
    assert keep["with"]["name"] == EVIDENCE_ARTIFACT
    assert keep["with"]["overwrite"] is True
    assert keep["with"]["if-no-files-found"] == "error"
    assert keep["with"]["path"].split() == [
        "adopted-evidence/mutation-report.json",
        "adopted-evidence/red-test-baseline.json",
    ]
    assert names.index(decide["name"]) < names.index(keep["name"])
    # Nothing downloads another run's artifact any more: the files come from the
    # one step that decided.
    assert "run-id:" not in _text()

    # An adopted run computes nothing, and a recounted one computes everything.
    for name in ("mutation_legs", "baseline", "evidence"):
        assert NOT_ADOPTED in jobs[name]["if"], name
    assert NOT_ADOPTED not in jobs["test"]["if"]
    # The token of this workflow is read-only on actions.
    assert doc["permissions"] == {"contents": "read", "actions": "read"}
    assert jobs["test"]["needs"] == ["treedup", "pytest", "evidence", "static"]


def _step_runs(
    condition: str, *, static_result: str, previous_ok: bool, cancelled: bool = False
) -> bool:
    """Whether a STEP of job test runs, with the strict evaluator of the job graph.

    The status functions of a step look at the previous steps of the same job:
    ``success()`` is ``previous_ok``. A condition without a status function gets
    the implicit ``success()``, exactly as on GitHub.
    """
    env: dict[str, object] = {
        "needs.static.result": static_result,
        "needs.pytest.result": "success",
        "needs.treedup.outputs.skip": "",
        "needs.treedup.outputs.evidence_run": "true",
    }
    calls = {
        "success()": previous_ok,
        "failure()": not previous_ok,
        "cancelled()": cancelled,
        "always()": True,
    }
    text = condition.strip()
    assert text.startswith("${{") and text.endswith("}}"), condition
    parser = _Condition(_tokenize(text[3:-2]), env, calls)
    value = bool(parser.parse())
    return value if parser.status_called else (previous_ok and value)


def test_static_failure_turns_test_job_red() -> None:
    """#1920 AC-1: a red job `static` makes the required check red, and only that.

    The collector, the download of the evidence and the upload of the report
    still run (they carry their own status functions), so a red static does not
    cost the hub its report.
    """
    import os
    import subprocess

    test = _doc()["jobs"]["test"]
    steps = test["steps"]
    names = [s["name"] for s in steps]
    judge = steps[names.index("Static checks outcome")]
    # The judge reads the result of job `static` and nothing else. Without this
    # the behaviour table below is blind to the wiring: pointing the variable at
    # another job (needs.pytest.result) kept every case green while a red static
    # left the required check green. The same lock `_aggregator_rc` puts on
    # PARTS_RESULT for the parts.
    assert judge["env"] == {"STATIC_RESULT": "${{ needs.static.result }}"}
    assert judge["run"].count("$STATIC_RESULT") >= 1

    for result, red in (
        ("success", False),
        ("failure", True),
        ("cancelled", True),
        ("skipped", True),
        ("", True),
    ):
        assert _step_runs(judge["if"], static_result=result, previous_ok=not red), (
            result
        )
        env = {"PATH": os.environ["PATH"], "STATIC_RESULT": result}
        done = subprocess.run(
            ["bash", "-c", judge["run"]], env=env, capture_output=True, text=True
        )
        assert (done.returncode != 0) is red, (result, done.stdout, done.stderr)

    # After the judge nothing may depend on the implicit success(): with a red
    # judge (previous_ok False) these still run, and the last one does not.
    after = steps[names.index("Static checks outcome") + 1 :]
    assert [s["name"] for s in after] == [
        "Download the evidence files of job evidence",
        "Collect AC test and validation results for the Hub",
        "Fetch the audit file of job static",
        "Upload results for the trusted report",
        "Fail a cancelled run",
    ]
    for step in after[:-1]:
        assert _step_runs(step["if"], static_result="failure", previous_ok=False), step[
            "name"
        ]
    assert not _step_runs(
        after[-1]["if"], static_result="failure", previous_ok=False
    ), "only a cancelled run ends in the last step"
    # A static job in the needs of the one required check; deploy reads test.
    assert "static" in test["needs"]
    assert _doc()["jobs"]["deploy"]["needs"] == "test"


def test_ran_commands_and_checks_come_from_static() -> None:
    """#1920 AC-2: the strings the collector matches are those of the real steps."""
    import re

    doc = _doc()
    jobs = doc["jobs"]
    static, test = jobs["static"], jobs["test"]
    by_id = {s["id"]: s for s in static["steps"] if "id" in s}
    collect = next(
        s
        for s in test["steps"]
        if s["name"] == "Collect AC test and validation results for the Hub"
    )["with"]

    # job outputs: the outcome of the step with the same id, nothing else
    assert static["outputs"] == {
        key: "${{ steps." + key + ".outcome }}" for key in ("lint", "format", "types")
    }
    checks = [part.strip() for part in str(collect["checks"]).split(",")]
    assert checks == [
        "lint=${{ needs.static.outputs.lint }}",
        "format=${{ needs.static.outputs.format }}",
        "types=${{ needs.static.outputs.types }}",
        "tests=${{ steps.tests.outcome }}",
    ]
    ran = [line.strip() for line in str(collect["ran-commands"]).splitlines() if line]
    commands = {
        key: re.sub(r"^\$\{\{ [^}]+ \}\} ", "", line)
        for key, line in zip(("lint", "format", "types", "tests"), ran)
    }
    for key in ("lint", "format", "types"):
        assert ran[("lint", "format", "types").index(key)].startswith(
            "${{ needs.static.outputs." + key + " }} "
        ), key
        assert commands[key] == by_id[key]["run"], (key, "drifted from the run: line")
    # the full suite line is the one every task card declares (byte for byte)
    assert commands["tests"] == "uv run pytest -q -n auto --durations=30"
    # the vocabulary of the report is closed: no new check key appeared
    assert [c.split("=")[0] for c in checks] == ["lint", "format", "types", "tests"]


def _run_reporter_as_the_action_does(
    tmp_path: Path, *, evidence_run: str, mutations: bool, baseline: bool
) -> dict:
    """Run ci.yml's collect step: its `with` -> the action's env -> the script.

    Nothing is filled in by hand: the inputs are what ci.yml passes (with its
    expressions evaluated), the env is what action.yml builds from them, the
    shell is the action's own `run`, and the answer is the results.json the
    script writes. The hub is a local server that serves one task card.
    """
    import http.server
    import json
    import os
    import re
    import subprocess
    import sys
    import threading

    class Hub(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps(
                {"id": 1785, "acceptance_criteria": [], "validation_commands": []}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    work = tmp_path / "workspace"
    work.mkdir()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        step = next(
            s
            for s in _doc()["jobs"]["test"]["steps"]
            if "hub-ci-report" in str(s.get("uses"))
        )
        action = __import__("yaml").safe_load(
            (ROOT / ".github/actions/hub-ci-report/action.yml").read_text()
        )

        def value(raw: object) -> str:
            text = str(raw)
            text = text.replace(
                "${{ needs.treedup.outputs.evidence_run }}", evidence_run
            )
            text = text.replace("${{ github.event.action }}", "opened")
            text = re.sub(r"\$\{\{ steps\.\w+\.outcome \}\}", "success", text)
            # #1920: the outcomes of lint/format/types come from job static
            text = re.sub(r"\$\{\{ needs\.static\.outputs\.\w+ \}\}", "success", text)
            text = text.replace("${{ secrets.HAIPLANE_HUB_URL }}", server_url)
            text = text.replace("${{ secrets.HAIPLANE_HUB_CI_READ_TOKEN }}", "reader")
            text = text.replace(
                "${{ github.event.pull_request.head.sha || github.sha }}", "sha-head"
            )
            assert "${{" not in text, f"unevaluated expression: {text!r}"
            return text

        inputs = {k: str(v.get("default", "")) for k, v in action["inputs"].items()}
        inputs.update({k: value(v) for k, v in step["with"].items()})

        run_step = action["runs"]["steps"][0]
        env = {"PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"}
        for key, raw in run_step["env"].items():
            match = re.fullmatch(r"\$\{\{ inputs\.([\w-]+) \}\}", str(raw))
            assert match, (key, raw)
            env[key] = inputs[match.group(1)]
        env.update(
            GITHUB_ACTION_PATH=str(ROOT / ".github/actions/hub-ci-report"),
            GITHUB_HEAD_REF="task-1785/x",
            GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_RUN_ID="7",
            GITHUB_REPOSITORY="o/r",
            NO_PROXY="127.0.0.1",
        )
        # The files the download step would leave in the workspace.
        files = {
            inputs["mutations-file"]: {"state": "ran", "survived": 0, "survivors": []},
            inputs["baseline-file"]: {"state": "ran", "merge_base": "m", "tests": {}},
        }
        for (name, content), present in zip(files.items(), (mutations, baseline)):
            if present:
                (work / name).write_text(json.dumps(content))
        done = subprocess.run(
            ["bash", "-c", run_step["run"]],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        results = work / inputs["results-dir"] / "results.json"
        assert results.exists(), done.stdout + done.stderr
        return json.loads(results.read_text())
    finally:
        server.shutdown()
        server.server_close()


def test_evidence_reaches_the_report_only_when_it_ran(tmp_path) -> None:
    """AC-2: the table, through the real path workflow -> action -> reporter."""
    import itertools

    download = _step("test", "Download the evidence files of job evidence")["if"]
    table = itertools.product(
        ("true", "false"),  # the decision of treedup
        (True, False),  # mutation-report.json is in the workspace
        (True, False),  # red-test-baseline.json is in the workspace
        ("success", "failure"),  # the result of job evidence
        (True, False),  # an earlier step of job test failed
    )
    for run, mut, base, job_result, previous_failed in table:
        label = (run, mut, base, job_result, previous_failed)
        # The `if` of the download step, translated like GitHub does (implicit
        # success() only without a status function). The result of job evidence
        # cannot enter it: the evaluator rejects every name it does not know.
        fetched = _holds(
            download, "", "", previous_ok=not previous_failed, evidence_run=run
        )
        assert fetched is (run == "true"), label  # also after a red step
        cancelled = _holds(
            download,
            "",
            "",
            previous_ok=not previous_failed,
            evidence_run=run,
            cancelled=True,
        )
        assert cancelled is False, label

        case = tmp_path / "-".join(map(str, label))
        case.mkdir()
        results = _run_reporter_as_the_action_does(
            case, evidence_run=run, mutations=mut, baseline=base
        )
        for key, present in (("mutations", mut), ("baseline", base)):
            block = results.get(key)
            if run == "false":
                assert key not in results, label  # not expected: no key at all
            elif present:
                assert block and block["state"] == "ran", label  # the file's data
                assert block["provenance"]["run_id"] == "7", label
            else:
                # expected and no file: an error that names the cause, never an
                # absent key and never "no survivors"
                assert block and block["state"] == "error", label
                assert block["reason"] and "survivors" not in block, label


def test_the_number_of_parts_is_set_once_by_the_matrix() -> None:
    """AC-4: [1..N], N >= 4, read back through strategy.job-total; the real tests/ split."""
    import re

    from tests.test_pytest_shard import assert_parts_of_tests_dir_are_the_whole

    doc = _doc()
    part = doc["jobs"]["pytest"]
    shards = part["strategy"]["matrix"]["shard"]
    total = len(shards)
    assert shards == list(range(1, total + 1)) and total >= 4, shards
    assert part["name"] == "Tests part ${{ matrix.shard }}/${{ strategy.job-total }}"
    run = next(s for s in part["steps"] if s.get("id") == "shard")["run"].strip()
    assert run == PART_COMMAND

    # No number of parts anywhere else: no `/<digit>` after the shard, no
    # `part N/<digit>` in a name, and the aggregator's name has no digit.
    text = _text()
    assert not re.search(r"matrix\.shard\s*\}\}\s*/\s*\d", text)
    assert not re.search(r"[Pp]arts?\s+\d+\s*/\s*\d+", text)
    assert not re.search(r"\d", _step("test", "Tests outcome of the parts")["name"])

    assert_parts_of_tests_dir_are_the_whole(total)


# ---- #1796: the deploy condition survives a skipped ancestor -------------------


_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op>==|!=|&&|\|\||[()!])"
    r"|(?P<name>[A-Za-z_][\w.-]*(?:\(\))?))"
)
_STATUS_CALLS = ("success()", "failure()", "cancelled()", "always()")


def _tokenize(text: str) -> list[tuple[str, str]]:
    """Condition text -> (kind, value); anything GitHub could read otherwise raises."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if not text[pos:].strip():
            break
        match = _TOKEN.match(text, pos)
        if match is None:
            raise ValueError(f"unsupported syntax at {text[pos:]!r}")
        kind = match.lastgroup or ""
        value = match.group(kind)
        if kind == "str":
            value = value[1:-1].replace("''", "'")
        tokens.append((kind, value))
        pos = match.end()
    return tokens


def _number(value: object) -> float:
    """GitHub's loose-equality coercion of a non-matching pair to a number."""
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value) if str(value).strip() else 0.0  # type: ignore[arg-type]
    except ValueError:
        return float("nan")


def _loosely_equal(left: object, right: object) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()  # GitHub compares strings ignoring case
    if isinstance(left, bool) and isinstance(right, bool):
        return left == right
    return _number(left) == _number(right)  # NaN never equals anything


class _Condition:
    """GitHub precedence: ``!`` over ``==``/``!=`` over ``&&`` over ``||``."""

    def __init__(self, tokens, env: dict[str, object], calls: dict[str, bool]) -> None:
        self.tokens, self.env, self.calls, self.at = tokens, env, calls, 0
        self.status_called = False

    def peek(self) -> tuple[str, str]:
        return self.tokens[self.at] if self.at < len(self.tokens) else ("end", "")

    def take(self) -> tuple[str, str]:
        token = self.peek()
        self.at += 1
        return token

    def parse(self) -> object:
        value = self.or_()
        if self.peek()[0] != "end":
            raise ValueError(f"unexpected {self.peek()[1]!r}")
        return value

    def or_(self) -> object:
        value = self.and_()
        while self.peek() == ("op", "||"):
            self.take()
            right = self.and_()  # both sides read: an unknown name must raise
            value = value if value else right
        return value

    def and_(self) -> object:
        value = self.equality()
        while self.peek() == ("op", "&&"):
            self.take()
            right = self.equality()
            value = right if value else value
        return value

    def equality(self) -> object:
        value = self.unary()
        while self.peek()[1] in ("==", "!=") and self.peek()[0] == "op":
            equal = self.take()[1] == "=="
            same = _loosely_equal(value, self.unary())
            value = same if equal else not same
        return value

    def unary(self) -> object:
        if self.peek() == ("op", "!"):
            self.take()
            return not self.unary()
        return self.primary()

    def primary(self) -> object:
        kind, value = self.take()
        if kind == "str":
            return value  # a literal is a string, never a call or a name
        if kind == "op" and value == "(":
            inner = self.or_()
            if self.take() != ("op", ")"):
                raise ValueError("unclosed parenthesis")
            return inner
        if kind == "name" and value in _STATUS_CALLS:
            self.status_called = True
            return self.calls[value]
        if kind == "name" and value in self.env:
            return self.env[value]
        raise ValueError(f"unsupported name {value!r}")


def _job_condition_holds(
    condition: str,
    job: str,
    results: dict[str, str],
    *,
    event: str,
    ref: str,
    cancelled: bool = False,
) -> bool:
    """Evaluate a job-level ``if`` the way GitHub does, over the real job graph.

    Supported subset (#1800): single-quoted literals; github.event_name,
    github.ref, needs.<job>.result; success()/failure()/cancelled()/always();
    ``!``, ``==``, ``!=``, ``&&``, ``||`` and parentheses, with GitHub's
    precedence and case-insensitive string comparison. Anything else raises.
    A condition with no status function gets an implicit success(): every
    transitive ancestor in ``needs`` must have the result "success".
    """
    jobs = _doc()["jobs"]

    def ancestors(name: str) -> set[str]:
        needs = jobs[name].get("needs", [])
        direct = [needs] if isinstance(needs, str) else list(needs)
        found = set(direct)
        for parent in direct:
            found |= ancestors(parent)
        return found

    needs = jobs[job].get("needs", [])
    direct = {needs} if isinstance(needs, str) else set(needs)
    parents = ancestors(job)
    success = all(results[a] == "success" for a in parents)
    env: dict[str, object] = {"github.event_name": event, "github.ref": ref}
    # GitHub puts only the DIRECT dependencies into the needs context
    env.update({f"needs.{name}.result": results[name] for name in direct})
    calls = {
        "success()": success,
        "failure()": any(results[a] == "failure" for a in parents),
        "cancelled()": cancelled,
        "always()": True,
    }
    text = condition.strip()
    if text.startswith("${{"):
        assert text.endswith("}}"), condition
        text = text[3:-2]
    parser = _Condition(_tokenize(text), env, calls)
    value = bool(parser.parse())
    return value if parser.status_called else (success and value)


def _deploy_cases(condition: str) -> dict[str, bool]:
    ok = {
        "treedup": "success",
        "pytest": "success",
        "evidence": "skipped",
        "static": "success",
        # #1921: the ancestors of evidence count for the implicit success() too
        "mutation_legs": "skipped",
        "baseline": "skipped",
    }
    out: dict[str, bool] = {}

    def run(name: str, test: str, event: str, ref: str, cancelled: bool = False):
        out[name] = _job_condition_holds(
            condition,
            "deploy",
            {**ok, "test": test},
            event=event,
            ref=ref,
            cancelled=cancelled,
        )

    main = "refs/heads/main"
    run("push_main_skipped_evidence", "success", "push", main)
    run("test_failure", "failure", "push", main)
    run("test_cancelled", "cancelled", "push", main)
    run("test_skipped", "skipped", "push", main)
    run("workflow_cancelled", "success", "push", main, cancelled=True)
    run("push_develop", "success", "push", "refs/heads/develop")
    run("pull_request", "success", "pull_request", "refs/pull/1/merge")
    run("workflow_dispatch", "success", "workflow_dispatch", main)
    return out


def test_deploy_runs_when_tests_pass_even_with_a_skipped_ancestor() -> None:
    """AC-1 (#1796): push в main при test=success выкатывается, хотя предок
    evidence пропущен; во всех прочих случаях выката нет. Условие — настоящее
    deploy.if из ci.yml, исходы предков идут через неявный success()."""
    cases = _deploy_cases(_doc()["jobs"]["deploy"]["if"])
    assert cases == {
        "push_main_skipped_evidence": True,
        "test_failure": False,
        "test_cancelled": False,
        "test_skipped": False,
        "workflow_cancelled": False,
        "push_develop": False,
        "pull_request": False,
        "workflow_dispatch": False,
    }


def test_the_pre_1796_condition_skipped_the_deploy() -> None:
    """Вычислитель видит дефект: старое условие даёт False в главном случае."""
    old = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert _deploy_cases(old)["push_main_skipped_evidence"] is False


# ---- #1800: the evaluator refuses what GitHub would not mean ------------------

_OK = {
    "treedup": "success",
    "pytest": "success",
    "evidence": "success",
    "static": "success",
    "mutation_legs": "success",
    "baseline": "success",
}


def _eval(condition: str, test: str = "success", ancestors=None, **kw) -> bool:
    results = {**(ancestors or _OK), "test": test}
    kw.setdefault("event", "push")
    kw.setdefault("ref", "refs/heads/main")
    return _job_condition_holds(condition, "deploy", results, **kw)


def test_the_job_condition_evaluator_refuses_what_github_would_not_mean() -> None:
    """AC-2 (#1800): unknown names, literals-as-calls, ! priority, string case."""
    # an unknown identifier is an error, not a Python variable
    for name in ("ok", "was_cancelled", "s0", "os", "__import__"):
        with pytest.raises(ValueError):
            _eval(f"{name} && success()")
    with pytest.raises(ValueError):
        _eval("github.sha == 'x'")
    with pytest.raises(ValueError):
        _eval("success() && needs.pytest.result == 'success'")  # not a direct need
    with pytest.raises(ValueError):
        _eval("success() ? 1 : 2")
    with pytest.raises(ValueError):
        _eval("(success()")

    # a literal is text: it neither calls cancelled() nor lifts the implicit
    # success(), so a skipped ancestor still makes the job false
    skipped = {**_OK, "evidence": "skipped"}
    assert _eval("'cancelled()' == 'cancelled()'", ancestors=skipped) is False
    assert _eval("'cancelled()' == 'cancelled()'") is True
    assert _eval("'success()'", ancestors=skipped) is False

    # `!` binds tighter than `==`: (!needs.test.result) == 'failure' is
    # false == 'failure', which GitHub reads as 0 == NaN, so false
    mixed = "!cancelled() && !needs.test.result == 'failure'"
    assert _eval(mixed, test="success") is False
    assert _eval(mixed, test="failure") is False
    assert _eval("!(needs.test.result == 'failure')", test="success") is True
    assert _eval("!cancelled() && !needs.test.result == 'x'", test="x") is False

    # strings compare without regard to case
    assert _eval("github.ref == 'REFS/HEADS/MAIN'") is True
    assert _eval("github.ref != 'REFS/HEADS/MAIN'") is False

    # `&&` over `||`, and a status function leaves no implicit success()
    assert _eval("!always() || success()") is True
    assert _eval("always()", test="failure", ancestors=skipped) is True
    assert _eval("failure()", test="failure") is True
