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
    # Report step inputs (the hub-ci-report composite action).
    assert f"hub-url: {URL_SECRET}" in text
    assert f"hub-token: {TOKEN_SECRET}" in text


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
        "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" in text
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

    step = _steps()["evidence"]
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

    steps = _steps()
    gate = "steps.evidence.outputs.run == 'true'"
    assert gate in steps["mutations"]["if"]
    assert gate in steps["baseline"]["if"]
    assert "github.event_name == 'pull_request'" not in steps["mutations"]["if"]

    # The base argument is the PR base, and develop for a manual run: the
    # ACTUAL argument is evaluated, not just grepped for.
    for step_id in ("mutations", "baseline"):
        run = steps[step_id]["run"]
        assert "origin/${{ github.base_ref || 'develop' }}" in run, step_id
        assert '--base "origin/${{ github.base_ref }}"' not in run
    assert _expr(
        "${{ github.base_ref || 'develop' }}", "workflow_dispatch", "", "t", ""
    ) == ("develop")

    # The reporter learns what each step did, so absent ≠ failed.
    sent = next(
        s
        for s in _doc()["jobs"]["test"]["steps"]
        if "hub-ci-report" in str(s.get("uses"))
    )["with"]
    assert sent["mutations-outcome"] == "${{ steps.mutations.outcome }}"
    assert sent["baseline-outcome"] == "${{ steps.baseline.outcome }}"
    assert sent["event-action"] == "${{ github.event.action }}"

    # What must NOT change: the deploy dependency and the tree-dedup scope.
    assert _doc()["jobs"]["deploy"]["needs"] == "test"
    assert steps["treedup"]["if"] == "${{ github.event_name == 'push' }}"


def test_evidence_decision_survives_a_red_earlier_step() -> None:
    """#1606: a red Test must not skip `evidence` and, through it, the baseline."""
    steps = _steps()
    assert steps["evidence"]["if"] == "${{ !cancelled() }}"
    assert "!cancelled()" in steps["baseline"]["if"]
    # With the earlier step red, the decision step still runs and says yes.
    ran, _log = _decide(
        Path(__import__("tempfile").mkdtemp()),
        event="pull_request",
        action="opened",
        head="task-9/x",
        base="develop",
    )
    assert ran is True
