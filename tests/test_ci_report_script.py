"""The CI-side reporter never fails the job and never reports a guess (#546).

The delivery gate (#605/#606) decides whether a PR may merge by reading the CI
job's outcome. A reporting step that can go red would therefore block merges for
every task in the repository, so every failure path here has to end in "print a
reason, exit 0". The second rule is that what did not run is reported as not run:
validation_commands mix real commands with prose written for humans, and handing
prose to a shell produces an exit code that looks like the work is broken.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci_report_to_hub.py"


def _load():
    spec = importlib.util.spec_from_file_location("ci_report_to_hub", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script():
    return _load()


@pytest.fixture(autouse=True)
def _clean_prefixed_env(monkeypatch):
    """Neither prefix may leak in from the developer's shell (Task 4)."""
    for suffix in (
        "HUB_URL",
        "HUB_CI_TOKEN",
        "HUB_CI_PYTEST",
        "HUB_CI_CHECKS",
        "HUB_CI_MUTATIONS",
        "HUB_CI_BASELINE",
        "HUB_CI_MUTATIONS_OUTCOME",
        "HUB_CI_BASELINE_OUTCOME",
        "HUB_CI_EVENT_ACTION",
    ):
        monkeypatch.delenv(f"HAIPLANE_{suffix}", raising=False)


def test_env_get_reads_canonical_only(script, monkeypatch):
    """Wave 5: the reporter reads HAIPLANE_* and nothing else."""
    monkeypatch.setenv("OPEN" + "CLAW" + "_HUB_CI_PYTEST", "old runner")
    assert script.env_get("HUB_CI_PYTEST") == ""
    monkeypatch.setenv("HAIPLANE_HUB_CI_PYTEST", "new runner")
    assert script.env_get("HUB_CI_PYTEST") == "new runner"
    monkeypatch.setenv("HAIPLANE_HUB_CI_PYTEST", "")
    assert script.env_get("HUB_CI_PYTEST") == "", (
        "an empty canonical value counts as unset"
    )


def test_hub_credentials_accepted_under_the_haiplane_prefix(script, monkeypatch):
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.example")
    monkeypatch.setenv("HAIPLANE_HUB_CI_TOKEN", "new-prefix-token")  # noqa: S105
    monkeypatch.setenv("GITHUB_HEAD_REF", "task-546/x")
    monkeypatch.setenv("HEAD_SHA", "sha-head")

    seen: dict[str, str] = {}

    def fake_request(url, token, payload=None):
        seen.setdefault("token", token)
        if payload is None:
            return {"acceptance_criteria": [], "validation_commands": []}
        seen["payload_url"] = url
        return {"applied": True, "reason": "ok"}

    monkeypatch.setattr(script, "hub_request", fake_request)
    assert script.main() == 0
    assert seen["token"] == "new-prefix-token"  # noqa: S105
    assert seen["payload_url"].startswith("https://hub.example/")


def test_ac_runner_reads_the_haiplane_prefix(script, monkeypatch):
    monkeypatch.setenv("HAIPLANE_HUB_CI_PYTEST", "python3 -m pytest")
    assert script.ac_runner() == ["python3", "-m", "pytest"]


def test_prose_among_validation_commands_reports_unknown_not_failure(script):
    """Prose never becomes a failure (#546) — the reason this filter exists.

    #546's own validation_commands end with a Russian sentence describing a
    manual check on a live PR. Handed to a shell that is a non-zero exit, and
    the gate would read "validation failed" about work that is fine.

    CHANGED BY #1103, deliberately: the status and the no-fail invariant are
    exactly as before, but the assertion "nothing was run" is gone. It pinned
    the blast radius, not the invariant — the filter used to suppress every
    real command standing beside the prose. `ruff --version` is still chosen
    for being instant, and now it is chosen for being SAFE to execute rather
    than for never being executed.
    """
    commands = [
        "ruff --version",
        "Проверка на живом PR: бриф отдаёт непустой ac_test_results",
    ]
    status, log_tail, reason = script.run_validation(commands)
    assert status == "unknown", "an unchecked entry means the list proves nothing"
    assert status != "fail", "prose is not a failure of the work"
    assert "не являются командами" in reason
    assert "Проверка на живом PR" in reason, "name the offending entry"
    assert "ruff --version" in log_tail, (
        "the real command beside the prose must have run — suppressing it was "
        "the defect #1103 closed"
    )


def test_no_validation_commands_is_skipped_with_a_reason(script):
    status, _log, reason = script.run_validation([])
    assert status == "skipped"
    assert reason


def test_only_resolvable_executables_count_as_commands(script):
    assert script.is_command("uv run pytest -q") is True
    assert script.is_command("Проверка на живом PR: смотри бриф") is False
    assert script.is_command("") is False
    assert script.is_command("   ") is False
    assert script.is_command("definitely-not-installed-xyz --flag") is False
    # A shell one-liner is not handed over either: the first token must be a real
    # executable, so `rm -rf / && echo ok` style entries cannot arrive by accident.
    assert script.is_command("&& echo hi") is False


def test_task_id_comes_from_the_branch_and_missing_is_not_an_error(script, monkeypatch):
    monkeypatch.setenv("GITHUB_HEAD_REF", "task-546/ac-validation-commands-ci")
    assert script.task_id_from_branch() == 546

    # push builds carry GITHUB_REF_NAME instead.
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setenv("GITHUB_REF_NAME", "task-607/cyrillic-slug-fallback")
    assert script.task_id_from_branch() == 607

    # Anything else is unknown, not a failure: develop, main, dependabot, a
    # branch made before the naming convention existed.
    for branch in ("develop", "main", "dependabot/uv/click-8.4.2", ""):
        monkeypatch.setenv("GITHUB_REF_NAME", branch)
        assert script.task_id_from_branch() is None


def test_without_hub_credentials_the_step_reports_nothing_and_succeeds(
    script, monkeypatch, capsys
):
    monkeypatch.delenv("HAIPLANE_HUB_URL", raising=False)
    monkeypatch.delenv("HAIPLANE_HUB_CI_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_HEAD_REF", "task-546/x")

    assert script.main() == 0, "a missing secret must not fail the job"
    out = capsys.readouterr().out
    assert "not configured" in out
    assert "unknown" in out, "say what the hub will conclude, not just what failed"


def test_an_unreachable_hub_does_not_fail_the_job(script, monkeypatch, capsys):
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.invalid")
    monkeypatch.setenv("HAIPLANE_HUB_CI_TOKEN", "irrelevant")  # noqa: S105
    monkeypatch.setenv("GITHUB_HEAD_REF", "task-546/x")
    monkeypatch.setenv("GITHUB_SHA", "sha-github-head")
    monkeypatch.delenv("HEAD_SHA", raising=False)
    monkeypatch.setattr(script, "hub_request", lambda *a, **k: None)

    assert script.main() == 0
    assert "hub" in capsys.readouterr().out.lower()


def test_the_reported_commit_is_the_branch_head_not_the_merge(script, monkeypatch):
    # On pull_request events GitHub checks out a throwaway merge of head into
    # base and sets GITHUB_SHA to THAT commit. The hub pins the branch tip at
    # submission (#572), so a report keyed on GITHUB_SHA names a commit that
    # exists in no branch — it could never match, and every run would be filed
    # and never applied. The workflow passes the head in HEAD_SHA; it wins.
    monkeypatch.setenv("HEAD_SHA", "sha-branch-head")
    monkeypatch.setenv("GITHUB_SHA", "sha-throwaway-merge")
    assert script.reported_commit() == "sha-branch-head"

    # push runs carry no HEAD_SHA of their own; there GITHUB_SHA IS the tip.
    monkeypatch.delenv("HEAD_SHA", raising=False)
    assert script.reported_commit() == "sha-throwaway-merge"

    # Neither present: nothing to report about.
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    assert script.reported_commit() == ""


def test_a_missing_commit_sha_stops_the_report(script, monkeypatch, capsys):
    # Without a commit the report could be applied to code it never ran.
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.example")
    monkeypatch.setenv("HAIPLANE_HUB_CI_TOKEN", "irrelevant")  # noqa: S105
    monkeypatch.setenv("GITHUB_HEAD_REF", "task-546/x")
    monkeypatch.setenv("GITHUB_SHA", "")
    monkeypatch.delenv("HEAD_SHA", raising=False)
    called = []
    monkeypatch.setattr(script, "hub_request", lambda *a, **k: called.append(a) or {})

    assert script.main() == 0
    assert called == [], "nothing may be sent without a commit"
    assert "no commit to report" in capsys.readouterr().out


def test_unrun_tests_are_reported_as_not_found(script, monkeypatch):
    # The runner reports what it observed; an AC whose test never ran is
    # not_found, never fail. The hub's gate then reads "no evidence", not
    # "broken work".
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.example")
    monkeypatch.setenv("HAIPLANE_HUB_CI_TOKEN", "irrelevant")  # noqa: S105
    monkeypatch.setenv("GITHUB_HEAD_REF", "task-546/x")
    monkeypatch.setenv("HEAD_SHA", "sha-github-head")

    sent: list[dict] = []

    def fake_request(url, token, payload=None):
        if payload is None:
            return {
                "acceptance_criteria": [
                    {"id": "AC-1", "verifiable_by": "test", "test_ref": "t.py::a"},
                    {"id": "AC-2", "verifiable_by": "test", "test_ref": "t.py::b"},
                    {"id": "AC-3", "verifiable_by": "manual", "test_ref": None},
                ],
                "validation_commands": [],
            }
        sent.append(payload)
        return {"applied": True, "reason": "ok"}

    monkeypatch.setattr(script, "hub_request", fake_request)
    monkeypatch.setattr(script, "run_nodeids", lambda nodeids: {"t.py::a": True})

    assert script.main() == 0
    assert sent[0]["ac_results"] == {"AC-1": "pass", "AC-2": "not_found"}
    assert "AC-3" not in sent[0]["ac_results"], (
        "manual AC are not the runner's business"
    )
    assert sent[0]["head_sha"] == "sha-github-head"


def test_ac_runner_is_configurable(script, monkeypatch):
    """#761 AC-1: the runner is this repository's default, not a law.

    The reporter was written for a repository that runs ``uv run pytest``. A
    satellite with different tooling would have reported not_found for every
    AC — evidence missing for a reason that has nothing to do with the work.
    """
    monkeypatch.delenv("HAIPLANE_HUB_CI_PYTEST", raising=False)
    assert script.ac_runner()[:3] == ["uv", "run", "pytest"], (
        "without configuration the hub's own runner must stay"
    )

    monkeypatch.setenv("HAIPLANE_HUB_CI_PYTEST", "python3 -m pytest")
    assert script.ac_runner() == ["python3", "-m", "pytest"]

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        raise OSError("not actually running anything")

    monkeypatch.setattr(script.subprocess, "run", fake_run)
    assert script.run_nodeids(["t.py::a"]) == {}
    assert captured["cmd"][:3] == ["python3", "-m", "pytest"], (
        "the configured runner must be the one invoked"
    )
    assert "t.py::a" in captured["cmd"]


def test_missing_runner_reports_not_found(script, monkeypatch, capsys):
    """#761 AC-2: an absent runner is missing evidence, never a failure."""
    monkeypatch.setenv("HAIPLANE_HUB_CI_PYTEST", "definitely-not-installed-xyz")
    assert script.ac_runner() == []
    assert script.run_nodeids(["t.py::a"]) == {}
    assert "not on PATH" in capsys.readouterr().out

    monkeypatch.setenv("HAIPLANE_HUB_CI_PYTEST", "   ")
    assert script.ac_runner() == [], "an empty runner is not a reason to guess"

    monkeypatch.setenv("HAIPLANE_HUB_CI_PYTEST", 'pytest "unclosed')
    assert script.ac_runner() == [], "an unparsable runner must not reach a shell"


def test_action_manifest_uses_no_github_context():
    """#761: the manifest must not name the github context — anywhere.

    The runner evaluates every expression in an action manifest, descriptions
    included, and the github context does not exist there: a single example
    written literally fails the WHOLE action at load time. That failure is
    invisible in the check list, because the calling step is
    continue-on-error by design (a reporting hiccup must not block merges),
    so it shows up only as evidence quietly never arriving. Caught exactly
    that way on the first run of this task.
    """
    manifest = (
        Path(__file__).resolve().parent.parent
        / ".github"
        / "actions"
        / "hub-ci-report"
        / "action.yml"
    ).read_text()
    offenders = [
        line.strip()
        for line in manifest.splitlines()
        if "${{" in line and "inputs." not in line
    ]
    assert not offenders, (
        f"only inputs.* may be interpolated in the manifest; found: {offenders}"
    )


def test_ci_workflow_reports_through_the_action():
    """#761 AC-5 (static half): one reporter, and it is given the branch head."""
    workflow = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
    ).read_text()
    assert "uses: ./.github/actions/hub-ci-report" in workflow
    assert "head-sha: ${{ github.event.pull_request.head.sha || github.sha }}" in (
        workflow
    ), "the workflow — where the github context DOES exist — passes the head"
    assert "run: uv run python scripts/ci_report_to_hub.py" not in workflow, (
        "the old inline path would be a second reporter to keep in step"
    )


# ---- #1081: what the job already ran is not run a second time ----------------


def test_validation_reuses_the_jobs_own_test_outcome(script, monkeypatch):
    """AC-1: the suite the job just ran is not run again by the reporter.

    The card declares ``uv run pytest -q``; the job ran ``uv run pytest -q -n
    auto``. Different strings, same assertion about the same tree — the worker
    count does not change which tests run. Before this, the reporter executed
    the suite a second time and the step cost as much as the Test step itself.
    """
    ran = {"uv run pytest -q -n auto": "pass", "uv run mypy hub": "pass"}

    def explode(*a, **k):  # noqa: ARG001 - reuse must not reach the shell
        raise AssertionError("re-ran a command the job had already run")

    # monkeypatch, не присваивание: script.subprocess — ЭТОТ ЖЕ глобальный
    # модуль subprocess, и голое присваивание протекает во все следующие
    # тесты сессии (измерено: оно превращало `false` в соседнем тесте в
    # no-op, и тот падал только при запуске файла целиком).
    monkeypatch.setattr(script.subprocess, "run", explode)
    status, log_tail, reason = script.run_validation(
        ["uv run pytest -q", "uv run mypy hub"], ran
    )

    assert status == "pass", reason
    assert "uv run pytest -q -n auto" in log_tail


def test_a_command_the_job_did_not_run_is_still_executed(script):
    """AC-2: the saving must never become a skipped check.

    A narrower selection is NOT the same assertion: a positional argument picks
    a subset, so a green whole-suite run proves nothing about it. It runs, and
    when it fails the validation still fails.
    """
    ran = {"uv run pytest -q -n auto": "pass"}

    status, _log, reason = script.run_validation(["false"], ran)
    assert status == "fail", "a command the job never ran must still be executed"
    assert "false" in reason

    narrower = "uv run pytest -q tests/test_ci_report_script.py"
    assert script.already_proven(narrower, ran) is None, (
        "a positional argument narrows the run — the suite's outcome does not "
        "prove it, and assuming otherwise is how validation becomes fiction"
    )


def test_reused_validation_says_so_in_the_log(script, capsys):
    """AC-3: silent reuse is indistinguishable from a skipped check."""
    ran = {"uv run ruff check hub tests": "pass"}
    script.run_validation(["uv run ruff check hub tests"], ran)

    printed = capsys.readouterr().out
    assert "not re-run" in printed
    assert "uv run ruff check hub tests" in printed


def test_only_whitelisted_flags_may_differ(script):
    """An unrecognised flag is not proven harmless, so it is not reused."""
    ran = {"uv run pytest -q -n auto": "pass"}
    for narrower in (
        "uv run pytest -q -k auth",  # -k selects
        "uv run pytest -q -m slow",  # -m selects
        "uv run pytest -q --ignore=tests/test_web.py",  # --ignore selects
        "uv run pytest -q --deselect tests/x.py::t",
        "uv run pytest -q --maxfail=1",
        "uv run pytest -q --lf",
    ):
        assert script.already_proven(narrower, ran) is None, narrower
    for same in ("uv run pytest -q", "uv run pytest -n 4 -v", "uv run pytest"):
        assert script.already_proven(same, ran) is not None, same


def test_a_failed_outcome_is_reused_as_a_failure(script):
    """Reuse carries the outcome, not an assumption that it was green."""
    ran = {"uv run mypy hub": "fail"}
    status, _log, reason = script.run_validation(["uv run mypy hub"], ran)
    assert status == "fail"
    assert "uv run mypy hub" in reason


def test_ran_commands_parsing_drops_what_it_cannot_read(script):
    """Silence in, silence out — an outcome-less line grants nothing."""
    parsed = script.parse_ran_commands(
        "success uv run mypy hub\n"
        "\n"  # blank
        " uv run ruff check hub tests\n"  # step never ran: empty outcome
        "banana uv run something\n"  # unrecognised outcome
        "failure uv run pytest -q -n auto\n"
    )
    assert parsed == {"uv run mypy hub": "pass", "uv run pytest -q -n auto": "fail"}
    assert script.parse_ran_commands("") == {}


def test_the_workflow_hands_the_reporter_what_it_ran():
    """The commands passed to the action must match the steps that ran them.

    A drifted copy stops matching silently and the duplicate run returns, so
    the pairing is asserted rather than trusted to review.
    """
    import yaml

    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    steps = [s for job in doc["jobs"].values() for s in job.get("steps") or []]
    by_id = {s.get("id"): s for s in steps if s.get("id")}
    reporters = [s for s in steps if "hub-ci-report" in str(s.get("uses", ""))]
    assert reporters, "no hub-ci-report step in ci.yml"

    # В workflow выражения ещё НЕ вычислены, а внутри выражения есть свои
    # пробелы — деление по первому прочло бы "${{" как исход. В рантайме
    # репортер получает уже вычисленное значение ("success uv run ..."), и
    # поэтому parse_ran_commands делить по первому пробелу может, а этот тест
    # не может.
    pairing = re.compile(r"^\$\{\{\s*steps\.(\w+)\.outcome\s*\}\}\s+(.+)$")
    declared = {}
    for line in (reporters[0]["with"].get("ran-commands") or "").splitlines():
        match = pairing.match(line.strip())
        if match:
            declared[match.group(2).strip()] = match.group(1)
    assert declared, "the reporter is told nothing about what this job ran"

    for command, step_id in declared.items():
        assert step_id in by_id, f"steps.{step_id} names no step in this workflow"
        assert by_id[step_id]["run"].strip() == command, (
            f"step {step_id!r} runs {by_id[step_id]['run'].strip()!r} but the "
            f"reporter is told it ran {command!r} — a drifted copy stops "
            "matching and the duplicate run comes back"
        )


def test_plugin_flags_are_never_treated_as_non_selecting(script):
    """``-p`` loads and DISABLES plugins, and one of them is the collector.

    Measured against real pytest in this repository: ``--collect-only -q``
    reports 2794 tests, and ``-p no:python`` reports none at all — the built-in
    ``python`` plugin is what collects Python tests. Whitelisting ``-p`` as
    "does not select" therefore declared a command that runs NOTHING proven by
    the job's green suite. Every other whitelisted flag was checked the same
    way, by collection count rather than by reading the docs.
    """
    ran = {"uv run pytest -q -n auto": "pass"}
    for command in (
        "uv run pytest -q -p no:python",
        "uv run pytest -q -p no:cacheprovider",
        "uv run pytest -q -p my_plugin",
        # Attached form: the one that actually slipped through while -p was
        # whitelisted, because "-p=..." carries its value and never becomes a
        # positional. pytest itself rejects it (ImportError), so reusing the
        # suite's pass reports a certain failure as green.
        "uv run pytest -q -p=no:python",
    ):
        assert script.already_proven(command, ran) is None, command
    # Pinned structurally as well, and not out of pedantry: the loop above
    # passes even with -p whitelisted for every SPACED form, because a spaced
    # value falls through to "positional" and differs anyway. Only the attached
    # form and this assertion actually hold the line — a mutation restoring -p
    # to the set has to fail something.
    assert "-p" not in script._NON_SELECTING_FLAGS, (
        "-p disables plugins, and `-p no:python` collects 0 of 2794 tests; "
        "treating it as non-selecting declares a command that runs nothing "
        "proven by the job's green suite"
    )


def test_a_dangling_flag_does_not_swallow_a_test_path(script):
    """A path eaten as a flag's value turns a narrowed run into "whole suite".

    ``uv run pytest -n tests/test_web.py`` is a card with a dangling ``-n``.
    Real pytest exits 4 on it. Consuming the path as the value of ``-n`` left
    the selection empty, keyed the command as the entire suite and declared it
    proven — reporting a certain failure as a pass, which is the one direction
    this mechanism must never be wrong in.
    """
    ran = {"uv run pytest -q -n auto": "pass"}
    for command in (
        "uv run pytest -n tests/test_web.py",
        "uv run pytest --tb tests/test_web.py",
        "uv run pytest --color tests/test_web.py",
        "uv run pytest --dist tests/x.py::test_y",
    ):
        assert script.already_proven(command, ran) is None, command
    # A real value is still consumed: worker count does not select anything.
    assert script.already_proven("uv run pytest -n 4 -v", ran) is not None
    assert script.already_proven("uv run pytest --tb=short -q", ran) is not None


def test_a_different_launcher_is_a_different_command(script):
    """The prefix up to ``pytest`` is part of the key, wrapper flags included."""
    ran = {"uv run pytest -q -n auto": "pass"}
    for command in ("python -m pytest -q", "uv run --no-cache pytest -q", "pytest -q"):
        assert script.already_proven(command, ran) is None, command


# ---- #1103: each entry is judged on its own -------------------------------


def test_prose_does_not_suppress_the_real_commands(script):
    """AC-1: a note written for a human costs only itself.

    Before this, the prose check sat before the execution loop and returned for
    the WHOLE list, so a task with real commands and one note got zero executed
    checks. Measured on #1077: its two entries begin with a real `gh` and
    continue in prose after a dash; the reporter step took 2s and ran nothing,
    and the task was approved and delivered on that.
    """
    commands = [
        "ruff --version",
        "После мержа: прогоны develop показывают пропуск pytest по маркеру",
        "python3 --version",
    ]
    status, log_tail, reason = script.run_validation(commands)

    assert "ruff --version" in log_tail
    assert "python3 --version" in log_tail
    assert status != "fail", "prose must never be reported as a failed check"
    assert "исполнено и прошло 2 из 3" in reason


def test_partial_validation_is_not_reported_as_pass(script):
    """AC-2: everything runnable passing is still not a passing list.

    The hub's vocabulary is pass | fail | unknown | skipped, and `unknown` is
    the only honest member for a partly checked list: `pass` would claim
    evidence for entries nobody looked at, which is worse than the blunt
    "nothing ran" this task replaced.
    """
    status, _log, reason = script.run_validation(
        ["ruff --version", "Проверить вручную на живом PR"]
    )
    assert status == "unknown"
    assert "не являются командами" in reason

    # All entries executable and green — only then is the list a pass.
    status, _log, reason = script.run_validation(["ruff --version"])
    assert status == "pass", reason


def test_a_failing_command_beside_prose_is_still_a_failure(script):
    """The substantive gain: a broken check no longer hides behind a note.

    Before, a mixed list short-circuited to `unknown` and the failing command
    was never executed, so a real breakage read as "not checked". Now it reads
    as what it is.
    """
    status, log_tail, reason = script.run_validation(
        ["false", "Ручная проверка: смотри бриф"]
    )
    assert status == "fail", "the command ran and failed — that is a fact"
    assert "false" in reason
    assert "false" in log_tail


def test_each_entry_is_accounted_for_by_name(script):
    """AC-3: partial coverage must be visible, not inferred from silence."""
    commands = [
        "ruff --version",
        "Проверка вручную на живом PR",
        "Ещё одна заметка для человека",
    ]
    _status, log_tail, reason = script.run_validation(commands)

    for entry in commands:
        assert entry in log_tail, f"entry not accounted for in the log: {entry}"
    assert "не команда" in log_tail, "say WHY an entry was not executed"
    assert "исполнено и прошло 1 из 3" in reason


def test_a_reused_outcome_counts_as_executed(script):
    """#1081 and #1103 compose: reuse is executed evidence, not a skipped entry.

    The job ran the command; this process only declined to run it twice. If
    reuse were counted as "not executed", a fully covered list would report
    itself as partial and the saving would look like a gap.
    """
    ran = {"uv run pytest -q -n auto": "pass"}
    status, log_tail, reason = script.run_validation(["uv run pytest -q"], ran)
    assert status == "pass", reason
    assert "not re-run" in log_tail

    status, _log, reason = script.run_validation(
        ["uv run pytest -q", "Заметка для человека"], ran
    )
    assert status == "unknown"
    assert "исполнено и прошло 1 из 2" in reason


# ---- #1270: the mutation run travels under its own key -----------------------


def _capture_payload(script, monkeypatch) -> dict:
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.example")
    monkeypatch.setenv("HAIPLANE_HUB_CI_TOKEN", "irrelevant")  # noqa: S105
    monkeypatch.setenv("GITHUB_HEAD_REF", "task-1270/x")
    monkeypatch.setenv("HEAD_SHA", "sha-head")
    seen: dict = {}

    def fake_request(url, token, payload=None):
        if payload is None:
            return {"acceptance_criteria": [], "validation_commands": []}
        seen.update(payload)
        return {"applied": True, "reason": "ok", "mutations_state": "ran"}

    monkeypatch.setattr(script, "hub_request", fake_request)
    assert script.main() == 0
    return seen


def test_the_mutation_report_is_sent_under_its_own_key(script, monkeypatch, tmp_path):
    report = {
        "state": "ran",
        "survivors": [{"file": "a.py", "function": "f", "line": 2}],
    }
    path = tmp_path / "mutations.json"
    path.write_text(json.dumps(report))
    monkeypatch.setenv("HAIPLANE_HUB_CI_MUTATIONS", str(path))

    payload = _capture_payload(script, monkeypatch)

    sent = dict(payload["mutations"])
    assert sent.pop("provenance") is not None, (
        "#1606: the run is named inside the block"
    )
    assert sent == report
    assert "mutation" not in payload["checks"], "a warning is not a check outcome"


def test_a_missing_or_unreadable_mutation_report_is_not_sent(
    script, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("HAIPLANE_HUB_CI_MUTATIONS", str(tmp_path / "absent.json"))
    assert "mutations" not in _capture_payload(script, monkeypatch)
    assert "absent.json" in capsys.readouterr().out

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    monkeypatch.setenv("HAIPLANE_HUB_CI_MUTATIONS", str(broken))
    assert "mutations" not in _capture_payload(script, monkeypatch)


def test_a_long_survivor_list_is_trimmed_and_says_so(script):
    report = {
        "state": "ran",
        "survived": 80,
        "survivors": [
            {"file": "a.py", "function": "f", "line": i, "diff": "d" * 600}
            for i in range(80)
        ],
    }
    trimmed = script.trim_mutations(report)
    assert len(json.dumps(trimmed)) < 30_000
    assert trimmed["survived"] == 80, "the count is never trimmed"
    assert trimmed["survivors_trimmed"] == 80 - len(trimmed["survivors"])
    assert all(s["line"] is not None for s in trimmed["survivors"])


def test_ci_workflow_runs_mutations_after_tests_and_cannot_fail_the_job():
    """#1270 AC-3 (static half): warning step, PR only, its JSON reaches the reporter."""
    import yaml

    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    steps = [s for job in doc["jobs"].values() for s in job.get("steps") or []]
    names = [s.get("id") or s.get("name") for s in steps]
    mutation = next(s for s in steps if s.get("id") == "mutations")

    assert mutation["continue-on-error"] is True
    assert "steps.evidence.outputs.run" in mutation["if"], "#1606: decided by event"
    assert "timeout-minutes" in mutation
    assert "scripts/mutation_changed.py" in mutation["run"]
    assert names.index("mutations") > names.index("tests")

    reporter = next(s for s in steps if "hub-ci-report" in str(s.get("uses", "")))
    assert names.index("mutations") < steps.index(reporter)
    json_out = re.search(r"--json-out\s+(\S+)", mutation["run"]).group(1)
    assert reporter["with"]["mutations-file"] == json_out.strip("\"'")


# ---- #913: the red-test baseline travels under its own key --------------------


def test_a_long_baseline_keeps_the_ac_tests_and_says_so(script):
    tests = {f"tests/test_big.py::test_{i}": "passed" for i in range(500)}
    tests["tests/test_big.py::test_ac[1]"] = "failed"
    tests["tests/test_big.py::test_ac[2]"] = "failed"
    report = {"state": "ran", "merge_base": "m", "tests": tests}

    trimmed = script.trim_baseline(report, {"tests/test_big.py::test_ac"})

    assert len(json.dumps(trimmed)) < 30_000
    assert trimmed["tests"]["tests/test_big.py::test_ac[1]"] == "failed"
    assert trimmed["tests"]["tests/test_big.py::test_ac[2]"] == "failed"
    assert trimmed["tests_trimmed"] == len(tests) - len(trimmed["tests"])
    assert trimmed["tests_trimmed"] > 0


def test_a_missing_baseline_report_is_not_sent(script, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HAIPLANE_HUB_CI_BASELINE", str(tmp_path / "absent.json"))
    assert "baseline" not in _capture_payload(script, monkeypatch)
    assert "absent.json" in capsys.readouterr().out


_INNER_TESTS = """
import pytest

def test_passes():
    assert True

def test_fails():
    assert False

@pytest.mark.parametrize("n", [1, 2, 3])
def test_param(n):
    assert n != 2
"""


@pytest.fixture
def inner_suite(script, monkeypatch, tmp_path):
    """A real pytest run against a throwaway suite, through the real runner."""
    import shlex
    import sys

    (tmp_path / "test_inner.py").write_text(_INNER_TESTS)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "HAIPLANE_HUB_CI_PYTEST", f"{shlex.quote(sys.executable)} -m pytest"
    )
    return "test_inner.py"


def test_a_missing_nodeid_does_not_hide_the_others(script, inner_suite, capsys):
    """#1581 AC-1: one absent test_ref is not_found alone, never all of them."""
    passing = f"{inner_suite}::test_passes"
    failing = f"{inner_suite}::test_fails"
    missing = f"{inner_suite}::test_typo_in_the_card"

    result = script.run_nodeids([passing, missing, failing])

    assert result == {passing: True, failing: False}, (
        "the existing ids keep their real outcome; the absent one is not in "
        "the result, which the caller reports as not_found"
    )
    assert "test_typo_in_the_card" in capsys.readouterr().out, (
        "the reason for not_found is named in the log, with the id"
    )


def test_all_present_nodeids_report_as_before(script, inner_suite):
    """#1581 AC-2: with nothing missing the outcome is what it was, in one run."""
    passing = f"{inner_suite}::test_passes"
    parametrized = f"{inner_suite}::test_param"

    calls: list[list[str]] = []
    real_run = script.subprocess.run

    def counting_run(cmd, **kwargs):
        calls.append(cmd)
        return real_run(cmd, **kwargs)

    script.subprocess.run = counting_run
    try:
        result = script.run_nodeids([passing, parametrized])
    finally:
        script.subprocess.run = real_run

    assert result == {passing: True, parametrized: False}, (
        "one failing case fails the parametrized AC"
    )
    assert len(calls) == 1, "nothing missing: no extra pytest start"


def test_a_missing_file_takes_only_its_own_ids_out(script, inner_suite, capsys):
    """#1581 round 2: pytest names a missing FILE, not the ids inside it."""
    passing = f"{inner_suite}::test_passes"
    gone = ["no_such_dir/test_gone.py::test_x", "no_such_dir/test_gone.py::test_y"]

    result = script.run_nodeids([gone[0], passing, gone[1]])

    assert result == {passing: True}, (
        "both ids of the absent file are not_found, the existing one is pass"
    )
    assert "test_gone.py" in capsys.readouterr().out


def test_a_missing_file_and_a_missing_test_each_get_their_own_outcome(
    script, inner_suite, capsys
):
    passing = f"{inner_suite}::test_passes"
    failing = f"{inner_suite}::test_fails"
    no_test = f"{inner_suite}::test_typo"
    no_file = "no_such_dir/test_gone.py::test_x"

    result = script.run_nodeids([no_file, passing, no_test, failing])

    assert result == {passing: True, failing: False}
    out = capsys.readouterr().out
    assert "test_typo" in out and "test_gone.py" in out


def test_a_pytest_failure_naming_nothing_is_not_retried(script, monkeypatch):
    """No named missing id: behave as before, one run, no loop."""
    calls = []

    class Proc:
        stdout = "collecting ... \nno tests ran\n"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Proc()

    monkeypatch.setenv("HAIPLANE_HUB_CI_PYTEST", "python3 -m pytest")
    monkeypatch.setattr(script.subprocess, "run", fake_run)
    assert script.run_nodeids(["t.py::a", "t.py::b"]) == {}
    assert len(calls) == 1


def test_a_same_named_file_elsewhere_does_not_take_the_id_out(
    script, inner_suite, tmp_path
):
    """#1581 round 3: nested/test_inner.py missing is not test_inner.py."""
    passing = f"{inner_suite}::test_passes"
    gone = f"nested/{inner_suite}::test_missing"

    assert script.run_nodeids([gone, passing]) == {passing: True}


def test_an_absolute_reported_path_still_matches_a_relative_id(
    script, inner_suite, tmp_path
):
    """pytest prints '<abs>/test_inner.py::t' for a relative argument."""
    passing = f"{inner_suite}::test_passes"
    missing = f"{inner_suite}::test_typo"
    absolute = f"ERROR: not found: {tmp_path / missing}\n(no match in any of [])\n"

    assert script._missing_nodeids(absolute, [passing, missing]) == {
        missing: f"not found: {tmp_path / missing}"
    }
    nested = f"ERROR: file or directory not found: {tmp_path / 'nested' / inner_suite}"
    assert script._missing_nodeids(nested, [passing]) == {}


# ---- #1606: skipped / failed / present are three different payloads ----------


def _provenance_env(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "4242")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.example")
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("HAIPLANE_HUB_CI_EVENT_ACTION", "opened")


def test_evidence_keys_distinguish_skipped_failed_and_present(
    script, monkeypatch, tmp_path
):
    """AC-3: a step that did not run sends no key; one that ran says what happened."""
    _provenance_env(monkeypatch)
    valid = tmp_path / "ok.json"
    valid.write_text(json.dumps({"state": "ran", "survivors": []}))
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    absent = tmp_path / "absent.json"

    def run(kind: str, outcome: str, path) -> dict:
        monkeypatch.setenv(f"HAIPLANE_HUB_CI_{kind}", str(path))
        monkeypatch.setenv(f"HAIPLANE_HUB_CI_{kind}_OUTCOME", outcome)
        return _capture_payload(script, monkeypatch)

    for kind, key in (("MUTATIONS", "mutations"), ("BASELINE", "baseline")):
        # skipped: the key is absent — not {} and not an error.
        assert key not in run(kind, "skipped", absent)
        assert key not in run(kind, "skipped", valid), "a skipped step sends nothing"

        # ran, but no file (timeout before the write): an explicit error.
        timed_out = run(kind, "cancelled", absent)[key]
        assert timed_out["state"] == "error"
        assert timed_out["reason"], "the cause must be named"
        assert "provenance" in timed_out

        # ran, file is broken: an explicit error too.
        garbage = run(kind, "failure", broken)[key]
        assert garbage["state"] == "error" and garbage["reason"]

        # ran, file is valid: the data, plus where it came from.
        good = run(kind, "success", valid)[key]
        assert good["state"] == "ran"
        assert good["provenance"]["run_id"] == "4242"
        assert good["provenance"]["run_url"] == (
            "https://github.example/org/repo/actions/runs/4242"
        )
        assert good["provenance"]["event"] == "pull_request.opened"
        assert good["provenance"]["at"], "a time must be stated"
        monkeypatch.delenv(f"HAIPLANE_HUB_CI_{kind}")
        monkeypatch.delenv(f"HAIPLANE_HUB_CI_{kind}_OUTCOME")


def test_an_executed_step_with_foreign_content_is_an_error_not_evidence(
    script, monkeypatch, tmp_path
):
    """#1606: `{}` or a foreign object is not a mutation/baseline report."""
    _provenance_env(monkeypatch)
    for kind, key in (("MUTATIONS", "mutations"), ("BASELINE", "baseline")):
        for content in ({}, {"unexpected": "x"}, {"state": "banana"}):
            path = tmp_path / f"{key}.json"
            path.write_text(json.dumps(content))
            monkeypatch.setenv(f"HAIPLANE_HUB_CI_{kind}", str(path))
            monkeypatch.setenv(f"HAIPLANE_HUB_CI_{kind}_OUTCOME", "success")
            block = _capture_payload(script, monkeypatch)[key]
            assert block["state"] == "error", (key, content)
            assert block["reason"] and "unexpected" not in block
            assert block["provenance"]["run_id"] == "4242"
        # Without a stated outcome (an old caller) foreign content sends nothing.
        monkeypatch.delenv(f"HAIPLANE_HUB_CI_{kind}_OUTCOME")
        assert key not in _capture_payload(script, monkeypatch)
        monkeypatch.delenv(f"HAIPLANE_HUB_CI_{kind}")


def test_a_spent_deadline_still_sends_the_report_naming_what_did_not_run(
    script, monkeypatch
):
    """#1666: the step is capped by CI; the reporter must send before that.

    With the clock past the overall deadline no command is launched, the
    commands left are named as not run (never as pass), and the POST happens.
    """
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.example")
    monkeypatch.setenv("HAIPLANE_HUB_CI_TOKEN", "irrelevant")  # noqa: S105
    monkeypatch.setenv("GITHUB_HEAD_REF", "task-1666/x")
    monkeypatch.setenv("HEAD_SHA", "sha-head")
    clock = {"now": 1000.0}
    monkeypatch.setattr(script.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(script, "_STARTED", 1000.0)
    sent: dict = {}

    def fake_request(url, token, payload=None):
        if payload is None:
            return {
                "acceptance_criteria": [],
                "validation_commands": ["ruff --version", "ruff check hub"],
            }
        sent.update(payload)
        return {"applied": True, "reason": "ok"}

    def fake_run(*args, **kwargs):
        raise AssertionError("no command may start after the deadline")

    monkeypatch.setattr(script, "hub_request", fake_request)
    monkeypatch.setattr(script.subprocess, "run", fake_run)
    clock["now"] += script._REPORT_DEADLINE + 1  # 17 minutes spent before validation
    assert script.main() == 0
    assert sent, "the report must be sent even when the deadline is spent"
    assert sent["validation_status"] == "unknown"
    assert "deadline" in sent["reason"]
    assert "ruff --version" in sent["reason"]
    assert "не выполнена" in sent["validation_log"]


def test_durations_flags_do_not_change_what_a_pytest_run_selects(script):
    """#1666: the Test step prints --durations=30; that must not break reuse.

    The step's outcome is handed to the reporter under its exact command line.
    If the flag made the selection key None, the task's `uv run pytest -q`
    would no longer be proven and the whole suite would run a second time
    inside the 20 minute step (#1081).
    """
    plain = script._selection_key("uv run pytest -q")
    assert plain is not None
    for cmd in (
        "uv run pytest -q -n auto --durations=30",
        "uv run pytest -q -n auto --durations 30",
        "uv run pytest -q --durations=30 --durations-min=1.0",
        "uv run pytest -q --durations 30 --durations-min 1.0",
    ):
        assert script._selection_key(cmd) == plain, cmd
    ran = {"uv run pytest -q -n auto --durations=30": "pass"}
    assert script.already_proven("uv run pytest -q", ran) == (
        "pass",
        "uv run pytest -q -n auto --durations=30",
    )
    # a path after the flag is still a selection, never swallowed as its value
    assert (
        script._selection_key("uv run pytest -q --durations tests/test_web.py") != plain
    )


# ---- #1686: the branch's code never sees a hub key -------------------------


_ENV_PROBE_TEST = """\
import json, os, pathlib


def test_dump_env():
    pathlib.Path(os.environ.get("PROBE_OUT", "/dev/null") + ".test").write_text(
        json.dumps(sorted(os.environ))
    )
"""


def test_tests_and_validation_run_without_hub_secrets(script, monkeypatch, tmp_path):
    """AC-2: a test or a validation command of the branch finds no HAIPLANE_HUB_*."""
    import sys

    probe_test = tmp_path / "test_env_probe.py"
    probe_test.write_text(_ENV_PROBE_TEST)
    probe_cmd = tmp_path / "dump_env.py"
    probe_cmd.write_text(
        "import json, os, pathlib\n"
        "pathlib.Path(os.environ['PROBE_OUT'] + '.cmd').write_text("
        "json.dumps(sorted(os.environ)))\n"
    )
    out = tmp_path / "probe"
    results_dir = tmp_path / "results"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROBE_OUT", str(out))
    monkeypatch.setenv("KEEP_ME", "1")
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.example")
    monkeypatch.setenv("HAIPLANE_HUB_CI_READ_TOKEN", "read-secret")  # noqa: S105
    monkeypatch.setenv("HAIPLANE_HUB_CI_TOKEN", "report-secret")  # noqa: S105
    monkeypatch.setenv("HAIPLANE_HUB_CI_AUDIT_TOKEN", "audit-secret")  # noqa: S105
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")  # noqa: S105
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "cloud-secret")  # noqa: S105
    monkeypatch.setenv("HAIPLANE_HUB_CI_RESULTS_DIR", str(results_dir))
    monkeypatch.setenv("HAIPLANE_HUB_CI_PYTEST", f"{sys.executable} -m pytest")
    monkeypatch.setenv("GITHUB_HEAD_REF", "task-1686/x")
    monkeypatch.setenv("HEAD_SHA", "sha-head")

    posts: list[tuple[str, str]] = []

    def fake_request(url, token, payload=None):
        posts.append((url, token))
        if payload is not None:
            return {"applied": True, "reason": "ok"}
        return {
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "verifiable_by": "test",
                    "test_ref": "test_env_probe.py::test_dump_env",
                }
            ],
            "validation_commands": [f"{sys.executable} {probe_cmd}"],
        }

    monkeypatch.setattr(script, "hub_request", fake_request)
    assert script.main() == 0

    for suffix in (".test", ".cmd"):
        names = json.loads(Path(str(out) + suffix).read_text())
        assert "KEEP_ME" in names and "PATH" in names, "the rest of the env stays"
        for name in names:
            assert not name.startswith("HAIPLANE_HUB_"), (suffix, name)
        assert "GITHUB_TOKEN" not in names and "AWS_SECRET_ACCESS_KEY" not in names

    # Collect mode: the card is read with the READ key, nothing is sent, and
    # the results wait in the directory the workflow uploads.
    assert posts == [("https://hub.example/api/tasks/1686", "read-secret")]
    written = json.loads((results_dir / "results.json").read_text())
    assert written["task_id"] == 1686 and written["head_sha"] == "sha-head"
    assert written["ac_results"] == {"AC-1": "pass"}
    assert written["validation_status"] == "pass"


def test_child_env_drops_the_hub_namespace_and_secret_names(script):
    env = {
        "PATH": "/bin",
        "HAIPLANE_HUB_CI_TOKEN": "x",
        "haiplane_hub_url": "x",
        "ACTIONS_RUNTIME_TOKEN": "x",
        "MY_PASSWORD": "x",
        "HOME": "/h",
        "HAIPLANE_OTHER": "kept",
    }
    assert script.child_env(env) == {
        "PATH": "/bin",
        "HOME": "/h",
        "HAIPLANE_OTHER": "kept",
    }


# ---- #1686: the trusted reporter reads an artifact as data -------------------


_TRUSTED = Path(__file__).resolve().parent.parent / "scripts" / "ci_trusted_report.py"


@pytest.fixture
def trusted():
    spec = importlib.util.spec_from_file_location("ci_trusted_report", _TRUSTED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REPO = "agentdrover/haiplane"
SHA = "a" * 40


def _event(**over) -> dict:
    run = {
        "id": 9001,
        "run_attempt": 2,
        "path": ".github/workflows/ci.yml",
        "event": "pull_request",
        "head_sha": SHA,
        "head_branch": "task-1686/x",
        "conclusion": "success",
        "head_repository": {"full_name": REPO},
    }
    run.update(over)
    return {"workflow_run": run}


def _results(**over) -> dict:
    doc = {
        "schema": 1,
        "task_id": 1686,
        "head_sha": SHA,
        "ac_results": {"AC-1": "pass"},
        "validation_status": "pass",
        "validation_log": "ok",
        "reason": "",
        "checks": {"lint": "pass"},
    }
    doc.update(over)
    return doc


def _zip(members: dict[str, bytes], *, symlinks: tuple[str, ...] = ()) -> bytes:
    import io
    import stat
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            if name in symlinks:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, data)
    return buf.getvalue()


class _FakeApi:
    def __init__(self, blob: bytes, *, artifacts=None):
        self.blob = blob
        self.calls: list[tuple] = []
        self._artifacts = artifacts

    def list_artifacts(self, repository, run_id, name):
        import hashlib

        self.calls.append(("list", repository, run_id, name))
        if self._artifacts is not None:
            return {"artifacts": self._artifacts}
        return {
            "artifacts": [
                {
                    "id": 77,
                    "name": name,
                    "size_in_bytes": len(self.blob),
                    "expired": False,
                    "digest": "sha256:" + hashlib.sha256(self.blob).hexdigest(),
                    "archive_download_url": "https://api.invalid/zip",
                    "workflow_run": {"id": 9001, "head_sha": SHA},
                }
            ]
        }

    def download(self, url):
        self.calls.append(("download", url))
        return self.blob


def _tree(root: Path) -> list[tuple[str, bytes]]:
    return sorted(
        (str(p.relative_to(root)), p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    )


def _run(trusted, tmp_path, event, api, posts, **env):
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    (checkout / "scripts").mkdir(exist_ok=True)
    (checkout / "scripts" / "ci_report_to_hub.py").write_text("# trusted\n")
    environ = {
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_WORKSPACE": str(checkout),
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "HAIPLANE_HUB_URL": "https://hub.example",
        "HAIPLANE_HUB_CI_TOKEN": "report-key",
        **env,
    }

    def post(url, token, payload):
        posts.append((url, token, payload))
        return {"applied": True, "reason": "ok", "id": 5}

    return trusted.run(event, environ, api, post)


def test_the_trusted_reporter_treats_the_artifact_as_data(trusted, tmp_path, capsys):
    """AC-3: a hostile archive is refused whole; hostile text is only text."""
    import stat  # noqa: F401

    ok = json.dumps(_results()).encode()
    cases = {
        "parent traversal": _zip(
            {"results.json": ok, "../scripts/ci_report_to_hub.py": b"x"}
        ),
        "absolute path": _zip({"results.json": ok, "/etc/cron.d/x": b"x"}),
        # The link itself is a valid results file: only the link check refuses it.
        "symbolic link": _zip({"results.json": ok}, symlinks=("results.json",)),
        "an extra file": _zip({"results.json": ok, "evil.sh": b"rm -rf /"}),
        "a nested path": _zip({"sub/results.json": ok}),
        "too many files": _zip({f"f{i}.json": b"{}" for i in range(11)}),
        "archive size": _zip(
            {"results.json": ok, "audit.json": os.urandom(6 * 1024 * 1024)}
        ),
        "unpacked size": _zip(
            {"results.json": ok, "audit.json": b"0" * (21 * 1024 * 1024)}
        ),
    }
    checkout = tmp_path / "checkout"
    for label, blob in cases.items():
        posts: list = []
        _run(trusted, tmp_path, _event(), _FakeApi(blob), posts)
        assert posts == [], label
        log = capsys.readouterr().out
        assert "not reporting" in log, (label, log)
        assert _tree(checkout) == [("scripts/ci_report_to_hub.py", b"# trusted\n")], (
            label,
            "the checkout is untouched",
        )
        # The download directory is wiped by the test, as a fresh runner would.
        import shutil

        shutil.rmtree(tmp_path / "runner", ignore_errors=True)

    # A string that would run in a shell reaches the hub as the text it is.
    marker = tmp_path / "x"
    hostile = f"$(touch {marker}) `touch {marker}` ; touch {marker}"
    posts = []
    blob = _zip({"results.json": json.dumps(_results(validation_log=hostile)).encode()})
    _run(trusted, tmp_path, _event(), _FakeApi(blob), posts)
    assert len(posts) == 1
    url, token, body = posts[0]
    assert url == "https://hub.example/api/tasks/1686/ci-run-report"
    assert token == "report-key"
    assert body["validation_log"] == hostile
    assert not marker.exists(), "nothing in the artifact was executed"
    assert _tree(checkout) == [("scripts/ci_report_to_hub.py", b"# trusted\n")]
    # The facts about the run come from the event; the artifact is named.
    assert body["head_sha"] == SHA
    assert (body["run_id"], body["run_attempt"], body["event"]) == (
        "9001",
        "2",
        "pull_request",
    )
    assert body["artifact_id"] == "77" and body["artifact_digest"].startswith("sha256:")
    assert body["head_repository"] == REPO and body["run_conclusion"] == "success"


def test_only_the_canonical_ci_run_is_reported(trusted, tmp_path, capsys):
    """AC-4: another workflow, another repository, another commit: nothing is sent."""
    import shutil

    blob = _zip({"results.json": json.dumps(_results()).encode()})
    refused = {
        "another workflow": (_event(path=".github/workflows/other.yml"), "path"),
        "another repository": (
            _event(head_repository={"full_name": "evil/haiplane"}),
            "head repository",
        ),
        "an event outside the list": (_event(event="schedule"), "event"),
        "the artifact is about another commit": (_event(head_sha="b" * 40), "commit"),
    }
    for label, (event, fragment) in refused.items():
        posts: list = []
        api = _FakeApi(blob)
        if label.startswith("the artifact"):
            # The artifact says SHA, the event says another commit.
            api = _FakeApi(blob, artifacts=None)
        _run(trusted, tmp_path, event, api, posts)
        assert posts == [], label
        log = capsys.readouterr().out
        assert "not reporting" in log and fragment in log, (label, log)
        if label != "the artifact is about another commit":
            assert api.calls == [], f"{label}: the artifact was not even fetched"
        shutil.rmtree(tmp_path / "runner", ignore_errors=True)

    # A results file that names another commit than the event is refused too.
    posts = []
    shutil.rmtree(tmp_path / "runner", ignore_errors=True)
    wrong = _zip({"results.json": json.dumps(_results(head_sha="c" * 40)).encode()})
    _run(trusted, tmp_path, _event(), _FakeApi(wrong), posts)
    assert posts == [] and "another commit" in capsys.readouterr().out
    shutil.rmtree(tmp_path / "runner", ignore_errors=True)

    # The documented "@ref" suffix of the path is not a different workflow.
    posts = []
    suffixed = _event(path=".github/workflows/ci.yml@refs/heads/task-1686/x")
    _run(trusted, tmp_path, suffixed, _FakeApi(blob), posts)
    assert len(posts) == 1
    assert posts[0][2]["workflow_path"].endswith("@refs/heads/task-1686/x")


def test_the_artifact_of_another_attempt_is_not_substituted(trusted, tmp_path, capsys):
    blob = _zip({"results.json": json.dumps(_results()).encode()})
    other_attempt = {
        "id": 1,
        "name": "hub-ci-results-1",
        "size_in_bytes": 10,
        "expired": False,
        "archive_download_url": "u",
    }
    posts: list = []
    api = _FakeApi(blob, artifacts=[other_attempt])
    _run(trusted, tmp_path, _event(), api, posts)
    assert posts == [] and "no artifact" in capsys.readouterr().out
    assert ("download", "u") not in api.calls

    twice = [{**other_attempt, "name": "hub-ci-results-2"}] * 2
    _run(trusted, tmp_path, _event(), _FakeApi(blob, artifacts=twice), posts)
    assert posts == [] and "refusing to choose" in capsys.readouterr().out


def test_audit_drafts_are_filed_for_pushes_to_develop_only(trusted, tmp_path):
    audit = {
        "dependencies": [
            {
                "name": "pkg",
                "version": "1",
                "vulns": [{"id": "PYSEC-1", "fix_versions": []}],
            }
        ]
    }
    blob = _zip({"audit.json": json.dumps(audit).encode()})
    for event, expected in (
        (_event(event="push", head_branch="develop"), 1),
        (_event(event="push", head_branch="main"), 1),
        (_event(event="push", head_branch="task-1/x"), 0),
        (_event(event="pull_request", head_branch="develop"), 0),
        (_event(event="workflow_dispatch", head_branch="develop"), 0),
    ):
        import shutil

        shutil.rmtree(tmp_path / "runner", ignore_errors=True)
        posts: list = []
        _run(
            trusted,
            tmp_path,
            event,
            _FakeApi(blob),
            posts,
            HAIPLANE_HUB_CI_AUDIT_TOKEN="audit-key",
        )
        drafts = [p for p in posts if p[0].endswith("/api/tasks")]
        assert len(drafts) == expected, event
        for _url, token, body in drafts:
            assert token == "audit-key", "drafts use the audit key, not the report key"
            assert body["client_request_id"] == "pip-audit:PYSEC-1"
            assert body["source"] == "agent"


# ---- #1700: the timing of every finished attempt of a task branch ------------


def _step(name, seconds, conclusion="success"):
    """One GitHub step record that lasted ``seconds`` (fixed times, no clock)."""
    return {
        "name": name,
        "conclusion": conclusion,
        "started_at": "2026-10-15T10:00:00Z",
        "completed_at": f"2026-10-15T10:{seconds // 60:02d}:{seconds % 60:02d}Z",
    }


def _jobs() -> list[dict]:
    return [
        {
            "name": "Ruff and pytest",
            "conclusion": "success",
            "started_at": "2026-10-15T10:00:00Z",
            "completed_at": "2026-10-15T10:29:00Z",
            "steps": [
                _step("Lint", 40),
                _step("Test", 540),
                _step("Mutations of changed functions (warning only)", 600),
                _step("Red-test baseline on merge-base (data only)", 90, "skipped"),
                _step("Collect AC test and validation results for the Hub", 545),
            ],
        },
        {
            "name": "Compose cold start",
            "conclusion": "skipped",
            "started_at": "2026-10-15T10:00:00Z",
            "completed_at": "2026-10-15T10:00:00Z",
            "steps": [],
        },
    ]


class _TimedApi(_FakeApi):
    def __init__(self, blob, *, jobs=None, jobs_error=None, **kw):
        super().__init__(blob, **kw)
        self.jobs = jobs
        self.jobs_error = jobs_error

    def list_jobs(self, repository, run_id, attempt):
        self.calls.append(("jobs", repository, run_id, attempt))
        if self.jobs_error is not None:
            raise self.jobs_error
        return self.jobs


def _finished(**over) -> dict:
    return _event(updated_at="2026-10-15T10:31:12Z", **over)


def _timing(posts) -> list[dict]:
    return [p[2] for p in posts if p[0].endswith("/ci-run-timing")]


def test_timing_is_sent_with_job_and_step_durations(trusted, tmp_path):
    """AC-1: run, attempt, event time, job times and the named steps' seconds."""
    posts: list = []
    blob = _zip({"results.json": json.dumps(_results()).encode()})
    _run(trusted, tmp_path, _finished(), _TimedApi(blob, jobs=_jobs()), posts)
    (body,) = _timing(posts)
    assert [p[0] for p in posts if p[2] is body] == [
        "https://hub.example/api/tasks/1686/ci-run-timing"
    ]
    assert (body["run_id"], body["run_attempt"], body["head_sha"]) == ("9001", "2", SHA)
    assert body["completed_at"] == "2026-10-15T10:31:12Z", "the time of the event"
    assert body["state"] == "ok" and body["conclusion"] == "success"
    main, other = body["jobs"]
    assert (main["name"], main["started_at"], main["completed_at"]) == (
        "Ruff and pytest",
        "2026-10-15T10:00:00Z",
        "2026-10-15T10:29:00Z",
    )
    assert [(s["key"], s["seconds"], s["conclusion"]) for s in main["steps"]] == [
        ("test", 540.0, "success"),
        ("mutations", 600.0, "success"),
        ("baseline", None, "skipped"),
        ("ac_collect", 545.0, "success"),
    ], "Lint is not a named step; a skipped step has no duration"
    assert other["steps"] == [] and other["conclusion"] == "skipped"
    assert len([p for p in posts if p[0].endswith("/ci-run-report")]) == 1


class _Boom(_TimedApi):
    def download(self, url):
        raise OSError("storage is down")


def test_timing_is_sent_without_a_usable_artifact(trusted, tmp_path, capsys):
    """AC-2: no artifact, a broken ZIP and a failed download each still time the run."""
    import shutil

    good = _zip({"results.json": json.dumps(_results()).encode()})
    cases = {
        "cancelled before upload": _TimedApi(good, jobs=_jobs(), artifacts=[]),
        "a broken archive": _TimedApi(b"this is not a zip", jobs=_jobs()),
        "a failed download": _Boom(good, jobs=_jobs()),
    }
    for label, api in cases.items():
        shutil.rmtree(tmp_path / "runner", ignore_errors=True)
        posts: list = []
        assert (
            _run(trusted, tmp_path, _finished(conclusion="cancelled"), api, posts) == 0
        )
        timing = _timing(posts)
        assert len(timing) == 1, label
        assert timing[0]["state"] == "ok" and timing[0]["conclusion"] == "cancelled"
        assert len(timing[0]["jobs"]) == 2, label
        assert not [p for p in posts if p[0].endswith("/ci-run-report")], (
            label,
            "the rules for accepting a CI report did not change",
        )
        shown = capsys.readouterr().out
        assert "not reporting this run" in shown or "reporting nothing" in shown, label


def test_timing_unavailable_when_jobs_cannot_be_read(trusted, tmp_path, capsys):
    """AC-3: a failed jobs request, or no jobs at all, still reports the finish."""
    import urllib.error

    blob = _zip({"results.json": json.dumps(_results()).encode()})
    posts: list = []
    down = _TimedApi(blob, jobs_error=urllib.error.URLError("rate limited"))
    assert _run(trusted, tmp_path, _finished(), down, posts) == 0
    (body,) = _timing(posts)
    assert body["state"] == "timing_unavailable" and body["jobs"] == []
    assert "rate limited" in body["reason"]
    assert body["completed_at"] == "2026-10-15T10:31:12Z"
    assert "timing_unavailable" in capsys.readouterr().out
    assert len([p for p in posts if p[0].endswith("/ci-run-report")]) == 1, (
        "the CI report goes as before"
    )

    import shutil

    shutil.rmtree(tmp_path / "runner", ignore_errors=True)
    posts = []
    queued = _TimedApi(blob, jobs=[])
    assert (
        _run(trusted, tmp_path, _finished(conclusion="cancelled"), queued, posts) == 0
    )
    (body,) = _timing(posts)
    assert body["state"] == "ok" and body["jobs"] == []


def test_timing_is_only_for_task_branches_with_an_event_time(trusted, tmp_path):
    blob = _zip({"results.json": json.dumps(_results()).encode()})
    for event in (
        _finished(head_branch="develop"),
        _event(),  # no updated_at in the event
    ):
        posts: list = []
        _run(trusted, tmp_path, event, _TimedApi(blob, jobs=_jobs()), posts)
        assert _timing(posts) == []
