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
    # #546's own validation_commands end with a Russian sentence describing a
    # manual check on a live PR. Executed in a shell that is a non-zero exit —
    # and the gate would read "validation failed" about work that is fine.
    commands = [
        # A real executable, chosen to return instantly: this list must never be
        # worth executing. Put "uv run pytest -q" here and a regression that
        # stops filtering prose would rerun the whole suite inside this test
        # rather than failing it.
        "ruff --version",
        "Проверка на живом PR: бриф отдаёт непустой ac_test_results",
    ]
    status, log_tail, reason = script.run_validation(commands)
    assert status == "unknown"
    assert log_tail == "", "nothing was run, so there is nothing to quote"
    assert "не являются командами" in reason
    assert "Проверка на живом PR" in reason, "name the offending entry"


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
