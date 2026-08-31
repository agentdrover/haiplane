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
    for suffix in ("HUB_URL", "HUB_CI_TOKEN", "HUB_CI_PYTEST", "HUB_CI_CHECKS"):
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
