"""The diff is scanned for changes to how CI gets or passes results (#1688).

A mechanical scan finds the explicit tricks, the reviewer explains them, the
owner reads a short note, and automation never decides such a submission. The
scan is tested on real unified diffs; the routing is tested through the real
autopilot, the steward gatekeeper and the verdict transaction.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import aiosqlite
import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub import repository_ci_integrity as store
from hub.models import CIIntegrityAnswer, MachineReviewSubmit, ReviewVerdict
from hub.services import auto_verdict, ci_integrity, steward_applied, steward_apply
from hub.services.ci_integrity import (
    CLEAN,
    SIGNS,
    UNREADABLE,
    Standing,
    scan_diff,
)
from tests.test_auto_verdict import (
    _CLEAN_REVIEW,
    _TIP,
    _PinnedGitOps,
    _post_review,
    _submitted_task,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _diff(
    path: str, added: list[str], *, old: str | None = None, start: int = 1
) -> str:
    """A ``-U0`` unified diff that adds ``added`` lines to ``path``."""
    head = f"diff --git a/{old or path} b/{path}\n"
    if old and old != path:
        head += f"similarity index 90%\nrename from {old}\nrename to {path}\n"
    body = (
        f"--- a/{old or path}\n+++ b/{path}\n"
        f"@@ -{start},0 +{start},{len(added)} @@\n"
        + "".join(f"+{line}\n" for line in added)
    )
    return head + body


def _rules(diff: str, pyproject: str | None = None) -> set[tuple[str, str]]:
    return {(s.path, s.rule) for s in scan_diff(diff, pyproject)}


_PYPROJECT = """[project]
name = "x"

[tool.pytest.ini_options]
addopts = "-q"
testpaths = ["tests"]

[tool.ruff]
line-length = 88
"""

_WORKFLOW_DIFF = _diff(".github/workflows/ci.yml", ["      - run: echo changed"])


# ---- AC-1 ----------------------------------------------------------------


def test_the_scan_names_each_kind_of_ci_input():
    """AC-1: each of seven kinds of CI input gives a sign with path and rule; a plain diff none."""
    cases = [
        (_WORKFLOW_DIFF, ".github/workflows/ci.yml", "ci_workflow", None),
        (
            _diff("scripts/ci_report_to_hub.py", ["x = 1"]),
            "scripts/ci_report_to_hub.py",
            "ci_script",
            None,
        ),
        (
            _diff("tests/conftest.py", ["import os"]),
            "tests/conftest.py",
            "conftest",
            None,
        ),
        (
            _diff("pyproject.toml", ['addopts = "-q -p evil"'], start=5),
            "pyproject.toml",
            "pytest_config",
            _PYPROJECT,
        ),
        (_diff("uv.lock", ['version = "1"']), "uv.lock", "lockfile", None),
        (
            _diff("tests/test_x.py", ["def pytest_runtest_makereport(item, call):"]),
            "tests/test_x.py",
            "pytest_report_hook",
            None,
        ),
        (
            _diff("tests/test_x.py", ['t = os.environ["HAIPLANE_HUB_CI_TOKEN"]']),
            "tests/test_x.py",
            "token_read",
            None,
        ),
    ]
    for diff, path, rule, pyproject in cases:
        assert (path, rule) in _rules(diff, pyproject), (path, rule)
    plain = _diff("hub/app.py", ["x = 1"]) + _diff("tests/test_app.py", ["assert 1"])
    assert scan_diff(plain) == []
    # a pyproject edit outside the pytest tables is not a sign
    other = _diff("pyproject.toml", ["line-length = 100"], start=9)
    assert scan_diff(other, _PYPROJECT) == []


def test_every_line_rule_and_both_sides_of_a_rename_or_delete():
    """The other hook names, report files, exits and detached processes; renames and deletions count."""
    hits = {
        "def pytest_sessionfinish(session):": "pytest_report_hook",
        "def pytest_terminal_summary(terminalreporter):": "pytest_report_hook",
        "def pytest_runtest_logreport(report):": "pytest_report_hook",
        "def pytest_collection_modifyitems(items):": "pytest_report_hook",
        'open("hub-ci-results/ac.json", "w")': "report_file",
        'p = "mutation-report.json"': "report_file",
        'p = "red-test-baseline.json"': "report_file",
        'p = "audit.json"': "report_file",
        "os._exit(0)": "hard_exit",
        "subprocess.Popen(cmd, start_new_session=True)": "detached",
        "threading.Thread(target=f, daemon=True).start()": "detached",
        'tok = os.getenv("CI_HUB_TOKEN")': "token_read",
    }
    for line, rule in hits.items():
        assert ("tests/test_y.py", rule) in _rules(_diff("tests/test_y.py", [line])), (
            line
        )
    # line rules do not apply to production code
    assert scan_diff(_diff("hub/app.py", ["os._exit(0)"])) == []
    # a rename out of a watched path, a rename into one, and a deletion
    moved_away = (
        "diff --git a/tests/conftest.py b/tests/helpers.py\n"
        "similarity index 100%\nrename from tests/conftest.py\n"
        "rename to tests/helpers.py\n"
    )
    assert ("tests/conftest.py", "conftest") in _rules(moved_away)
    moved_in = _diff("tests/conftest.py", ["x = 1"], old="tests/helpers.py")
    assert ("tests/conftest.py", "conftest") in _rules(moved_in)
    deleted = (
        "diff --git a/scripts/secret_scan.py b/scripts/secret_scan.py\n"
        "deleted file mode 100644\nindex 1111111..0000000\n"
        "--- a/scripts/secret_scan.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x = 1\n"
    )
    assert ("scripts/secret_scan.py", "ci_script") in _rules(deleted)
    # the Makefile and the other pytest config files
    assert ("Makefile", "ci_makefile") in _rules(_diff("Makefile", ["security:"]))
    assert ("tox.ini", "pytest_config") in _rules(_diff("tox.ini", ["[pytest]"]))


def test_pyproject_pytest_tables_are_found_in_the_changed_lines():
    """A removed table header, a deletion inside a table and an unread file all count."""
    removed_header = (
        "diff --git a/pyproject.toml b/pyproject.toml\n"
        "--- a/pyproject.toml\n+++ b/pyproject.toml\n"
        '@@ -4,2 +3,0 @@\n-[tool.pytest.ini_options]\n-addopts = "-q"\n'
    )
    assert ("pyproject.toml", "pytest_config") in _rules(removed_header, "x = 1\n")
    inside_deletion = (
        "diff --git a/pyproject.toml b/pyproject.toml\n"
        "--- a/pyproject.toml\n+++ b/pyproject.toml\n"
        '@@ -6,1 +5,0 @@\n-asyncio_mode = "auto"\n'
    )
    assert ("pyproject.toml", "pytest_config") in _rules(inside_deletion, _PYPROJECT)
    unread = _diff("pyproject.toml", ["line-length = 100"], start=9)
    assert ("pyproject.toml", "pytest_config") in _rules(unread, None)
    assert ci_integrity.pytest_sections(_PYPROJECT) == [(4, 7)]


def test_the_ci_script_list_covers_what_ci_calls():
    """Every script that ci.yml, the actions and ci-report.yml call is on the list."""
    called: set[str] = set()
    sources = [
        REPO_ROOT / ".github/workflows/ci.yml",
        REPO_ROOT / ".github/workflows/ci-report.yml",
    ]
    sources += list((REPO_ROOT / ".github/actions").rglob("*.yml"))
    assert len(sources) >= 3
    for src in sources:
        called |= set(
            re.findall(
                r"(?:scripts|deploy)/[A-Za-z0-9_./-]+\.(?:py|sh)", src.read_text()
            )
        )
    assert "scripts/ci_report_to_hub.py" in called, "the reading itself works"
    missing = called - ci_integrity.CI_SCRIPTS
    assert not missing, f"CI calls scripts the integrity scan does not watch: {missing}"
    assert all((REPO_ROOT / p).exists() for p in called)


# ---- the stored scan and the routing ---------------------------------------


@pytest.fixture
def ci_diff(monkeypatch):
    """What the pinned commit's diff says; set ``ci_diff.value`` (None = not received)."""

    class Box:
        value: str | None = ""

    box = Box()

    async def branch_diff(self, repo_path, base, branch, context=0):
        if isinstance(box.value, Exception):
            raise box.value
        return box.value

    monkeypatch.setattr(_PinnedGitOps, "branch_diff", branch_diff)
    return box


async def _auto_task(client, db, monkeypatch, slug: str) -> int:
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    return await _submitted_task(client, db, slug, {"verdict": "auto"})


async def _status(client: AsyncClient, task_id: int) -> dict:
    return (await client.get(f"/api/tasks/{task_id}")).json()


async def test_an_explained_ci_change_goes_to_the_owner_with_a_note(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    """AC-2: signs + explained -> no automated verdict; card and brief carry a note <= 600."""
    ci_diff.value = _WORKFLOW_DIFF
    task_id = await _auto_task(client, db, monkeypatch, "ci-explained")
    long_note = "Меняется шаг отчёта CI: токен теперь берётся из окружения. " * 20
    await _post_review(
        client,
        task_id,
        ci_integrity={"verdict": "explained", "explanation_ru": long_note},
    )

    body = await _status(client, task_id)
    assert body["status"] == "review", "the autopilot did not decide"
    assert body["review_verdict"] is None
    # the card: the route line names the owner and carries the note
    route = body["verdict_route"]
    assert route["final"] == "human" and route["code"] == "ci_integrity"
    assert "Меняется шаг отчёта CI" in route["line"]
    # the brief: signs, the question and the note
    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    block = brief["ci_integrity"]
    assert block["answer"] == "explained" and block["state"] == SIGNS
    assert 0 < len(block["note"]) <= 600
    assert {s["rule"] for s in block["signs"]} == {"ci_workflow"}
    assert "зачем" in block["question"]
    # the steward gatekeeper refuses to apply an approve of it
    refusals = await steward_apply.integrity_refusals(db, task_id)
    assert refusals and "входы CI" in refusals[0][1]
    # and the verdict transaction rereads the database and refuses too
    with pytest.raises(HTTPException) as err:
        await steward_applied._record(db, task_id, ReviewVerdict.approved, 1)
    assert err.value.status_code == 422 and "ci_integrity" in str(err.value.detail)
    # a human verdict is not touched
    ok = await client.post(
        f"/api/tasks/{task_id}/review-verdict", json={"verdict": "approved"}
    )
    assert ok.status_code == 200, ok.text


async def test_the_steward_gate_and_route_name_the_ci_change(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    """The steward's gatekeeper refuses the approve and the route names the human."""
    from hub.services.verdict_route import verdict_route

    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    ci_diff.value = _WORKFLOW_DIFF
    task_id = await _submitted_task(client, db, "ci-steward", {"verdict": "steward"})
    await ci_integrity.ensure_scan(db, task_id)
    await _post_review(
        client,
        task_id,
        ci_integrity={"verdict": "explained", "explanation_ru": "ci.yml по задаче"},
    )
    refusals = await steward_apply.apply_refusals(db, task_id)
    assert any("входы CI" in detail for _, detail in refusals), refusals
    route = await verdict_route(db, task_id)
    assert route.final == "human" and route.code == auto_verdict.CODE_CI_INTEGRITY
    assert "ci.yml по задаче" in route.reason


async def test_a_suspicious_ci_change_is_a_finding(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    """AC-3: signs + suspicious -> a confirmed high finding with the reviewer's text."""
    ci_diff.value = _diff("tests/conftest.py", ["def pytest_sessionfinish(session):"])
    task_id = await _auto_task(client, db, monkeypatch, "ci-suspicious")
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            **_CLEAN_REVIEW,
            "ci_integrity": {
                "verdict": "suspicious",
                "explanation_ru": "хук подменяет итог",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    findings = resp.json()["findings_confirmed"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "high"
    assert findings[0]["category"] == "ci_integrity"
    assert "хук подменяет итог" in findings[0]["detail"]
    assert findings[0]["finding_uid"], "addressable like any finding"
    stance = await auto_verdict.autopilot_stance(db, task_id)
    assert stance.outcome == auto_verdict.OUTCOME_REFUSE
    assert stance.code == auto_verdict.CODE_UNCLEAN, "the road of any confirmed finding"
    assert (await _status(client, task_id))["status"] == "review"
    # the finding is added once however many times the same body is re-read
    once = ci_integrity.with_suspicion_finding(
        MachineReviewSubmit(
            incomplete=False,
            ci_integrity=CIIntegrityAnswer(verdict="suspicious", explanation_ru="x"),
        )
    )
    assert ci_integrity.with_suspicion_finding(once) is once


async def test_a_change_without_signs_flows_as_before(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    """AC-4: no signs -> the autopilot approves, no section in the order, no brief block."""
    ci_diff.value = _diff("hub/app.py", ["x = 1"]) + _diff(
        "tests/test_app.py", ["assert 1"]
    )
    task_id = await _auto_task(client, db, monkeypatch, "ci-clean")
    await _post_review(client, task_id)

    body = await _status(client, task_id)
    assert body["review_verdict"] == "approved"
    stored = await store.get_ci_integrity_scan(db, task_id, _TIP)
    assert stored is not None and stored["state"] == CLEAN
    assert await ci_integrity.order_section(db, task_id) == ""
    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert brief["ci_integrity"] is None


async def test_the_reviewer_order_gets_the_section_only_with_signs(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    """The order names the paths, the rules and the question, and the answer shape."""
    ci_diff.value = _WORKFLOW_DIFF
    task_id = await _submitted_task(client, db, "ci-order", {"verdict": "human"})
    section = await ci_integrity.order_section(db, task_id)
    assert ".github/workflows/ci.yml" in section and "ci_workflow" in section
    assert ci_integrity.QUESTION in section
    assert '"ci_integrity"' in section and "explained|suspicious" in section


async def test_a_missing_diff_is_never_clean(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    """AC-5: a diff git did not give is a sign, and automation does not decide."""
    for value in (None, RuntimeError("git exploded")):
        ci_diff.value = value
        slug = "ci-missing-" + ("none" if value is None else "raise")
        task_id = await _auto_task(client, db, monkeypatch, slug)
        await _post_review(client, task_id)

        stored = await store.get_ci_integrity_scan(db, task_id, _TIP)
        assert stored is not None and stored["state"] == UNREADABLE
        task = dict(await repo.get_task(db, task_id))
        standing = await ci_integrity.standing_of(db, task)
        assert standing.gap and "дифф не получен" in standing.gap
        assert (await _status(client, task_id))["status"] == "review"
        stance = await auto_verdict.autopilot_stance(db, task_id)
        assert stance.code == auto_verdict.CODE_CI_INTEGRITY
        block = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()[
            "ci_integrity"
        ]
        assert block["state"] == UNREADABLE and "дифф не получен" in block["note"]
    # a retry after git recovers: unreadable is not remembered as an answer
    ci_diff.value = ""
    assert (await ci_integrity.ensure_scan(db, task_id)).state == CLEAN
    # no commit pinned at all is the same kind of "unknown"
    assert Standing(ci_integrity.Scan(UNREADABLE)).gap


# ---- AC-6: every way a report arrives ---------------------------------------


async def test_the_integrity_answer_arrives_by_every_path(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    """AC-6: REST, MCP and the text block store the same field; no answer -> 'ревьюер не ответил'."""
    from hub import mcp_server
    from hub.services.machine_review_intake import (
        ORIGIN_RUN_TEXT,
        record_machine_review,
    )
    from hub.services.review_dispatch import REPORT_FENCE, parse_report_block

    ci_diff.value = _WORKFLOW_DIFF
    answer = {"verdict": "explained", "explanation_ru": "Меняется ci.yml по задаче."}

    # REST
    rest = await _auto_task(client, db, monkeypatch, "ci-path-rest")
    await _post_review(client, rest, ci_integrity=answer)

    # MCP: the tool posts to the same route
    mcp = await _auto_task(client, db, monkeypatch, "ci-path-mcp")

    async def _post(path: str, body: dict) -> dict:
        resp = await client.post(path, json=body)
        assert resp.status_code == 200, resp.text
        return resp.json()

    monkeypatch.setattr(mcp_server, "_api_post", _post)
    await mcp_server.hub_submit_machine_review(
        mcp,
        raw_count=2,
        incomplete=False,
        findings_rejected=_CLEAN_REVIEW["findings_rejected"],
        model=_CLEAN_REVIEW["model"],
        agent="reviewer-bot",
        ci_integrity=answer,
    )

    # the text block of a run
    text = await _auto_task(client, db, monkeypatch, "ci-path-text")
    payload = {**_CLEAN_REVIEW, "ci_integrity": answer}
    report = parse_report_block(f"done\n```{REPORT_FENCE}\n{json.dumps(payload)}\n```")
    assert report is not None
    await record_machine_review(
        db, text, report, principal_id=None, username="run", origin=ORIGIN_RUN_TEXT
    )

    stored = []
    for task_id in (rest, mcp, text):
        row = dict(await repo.get_latest_machine_review(db, task_id))
        stored.append((row["ci_integrity_verdict"], row["ci_integrity_explanation"]))
        await ci_integrity.ensure_scan(db, task_id)
        task = dict(await repo.get_task(db, task_id))
        assert (await ci_integrity.standing_of(db, task)).answer == "explained"
    assert stored == [("explained", answer["explanation_ru"])] * 3

    # an answer outside the vocabulary and a too long note do not lose the report
    odd = parse_report_block(
        f"```{REPORT_FENCE}\n"
        + json.dumps(
            {
                **_CLEAN_REVIEW,
                "ci_integrity": {"verdict": "fine", "explanation_ru": "я" * 900},
            }
        )
        + "\n```"
    )
    assert odd is not None and odd.ci_integrity.verdict == ""
    assert len(odd.ci_integrity.explanation_ru) <= 600

    # signs and NO answer: automation does not decide, the note says so
    silent = await _auto_task(client, db, monkeypatch, "ci-path-silent")
    await _post_review(client, silent)
    task = dict(await repo.get_task(db, silent))
    standing = await ci_integrity.standing_of(db, task)
    assert standing.answer == "" and standing.gap
    assert "ревьюер не ответил" in standing.note.lower()
    assert (await _status(client, silent))["status"] == "review"
    assert (await ci_integrity.brief_state(db, silent))["answer"] == ""


# ---- the migration on an old schema --------------------------------------------


async def test_the_migration_is_idempotent_on_an_old_schema(monkeypatch):
    """R3: the table and both columns arrive on a schema without them; old rows keep their meaning."""
    from hub import db as db_module

    new = {
        "create_ci_integrity_scans",
        "add_machine_reviews_ci_integrity_verdict",
        "add_machine_reviews_ci_integrity_explanation",
    }
    full = list(db_module._MIGRATIONS)
    assert new <= {name for name, _ in full}
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.executescript(db_module._SCHEMA)
        monkeypatch.setattr(
            db_module, "_MIGRATIONS", [m for m in full if m[0] not in new]
        )
        await db_module._migrate(conn)
        cols = {
            r["name"]
            for r in await conn.execute_fetchall("PRAGMA table_info(machine_reviews)")
        }
        assert "ci_integrity_verdict" not in cols, "this is the OLD schema"
        await conn.execute(
            "INSERT INTO machine_reviews (task_id, submission_generation, raw_count, "
            "findings_confirmed, findings_rejected, submitted_by) "
            "VALUES (1, 1, 3, '[]', '[]', 'old-reviewer')"
        )
        await conn.commit()

        monkeypatch.setattr(db_module, "_MIGRATIONS", full)
        await db_module._migrate(conn)
        await db_module._migrate(conn)  # twice: idempotent
        info = {
            r["name"]: r
            for r in await conn.execute_fetchall("PRAGMA table_info(machine_reviews)")
        }
        for name in ("ci_integrity_verdict", "ci_integrity_explanation"):
            assert info[name]["notnull"] == 1 and info[name]["dflt_value"] == "''"
        row = (
            await conn.execute_fetchall(
                "SELECT raw_count, ci_integrity_verdict, ci_integrity_explanation "
                "FROM machine_reviews"
            )
        )[0]
        assert row["raw_count"] == 3
        assert (row["ci_integrity_verdict"], row["ci_integrity_explanation"]) == (
            "",
            "",
        )
        assert await conn.execute_fetchall("SELECT * FROM ci_integrity_scans") == []
        applied = {
            r[0] for r in await conn.execute_fetchall("SELECT name FROM _migrations")
        }
        assert new <= applied
    finally:
        await conn.close()
