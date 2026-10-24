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


def _rules(diff: str, head: dict | None = None, base: dict | None = None):
    return {(s.path, s.rule) for s in scan_diff(diff, head, base)}


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
            {"pyproject.toml": _PYPROJECT.replace('"-q"', '"-q -p evil"')},
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
    for diff, path, rule, head in cases:
        got = _rules(diff, head, {"pyproject.toml": _PYPROJECT})
        assert (path, rule) in got, (path, rule)
    plain = _diff("hub/app.py", ["x = 1"]) + _diff("tests/test_app.py", ["assert 1"])
    assert scan_diff(plain, {"tests/test_app.py": "assert 1\n"}) == []
    # a pyproject edit outside the pytest tables is not a sign
    other = _diff("pyproject.toml", ["line-length = 100"], start=9)
    head = {"pyproject.toml": _PYPROJECT.replace("= 88", "= 100")}
    assert scan_diff(other, head, {"pyproject.toml": _PYPROJECT}) == []


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


def test_layer_rules_config_is_a_ci_input():
    """#1852: the layer table / ledger and the script are CI inputs: edit, delete, rename."""
    config = "docs/agent-context/layer-rules.json"
    script = "scripts/layer_rules.py"
    for path, rule in ((config, "layer_rules_config"), (script, "ci_script")):
        assert (path, rule) in _rules(_diff(path, ["x"])), f"edit {path}"
        deleted = (
            f"diff --git a/{path} b/{path}\ndeleted file mode 100644\n"
            f"index 1111111..0000000\n--- a/{path}\n+++ /dev/null\n"
            "@@ -1,1 +0,0 @@\n-x\n"
        )
        assert (path, rule) in _rules(deleted), f"delete {path}"
        moved_away = (
            f"diff --git a/{path} b/docs/other.json\nsimilarity index 100%\n"
            f"rename from {path}\nrename to docs/other.json\n"
        )
        assert (path, rule) in _rules(moved_away), f"rename away {path}"
        moved_in = _diff(path, ["x"], old="docs/other.json")
        assert (path, rule) in _rules(moved_in), f"rename into {path}"
    # a neighbour json beside it is not an input
    assert _rules(_diff("docs/agent-context/other.json", ["x"])) == set()


def test_pytest_configuration_is_compared_as_data_not_as_text():
    """Quoted and dotted spellings, new pytest.toml files, deletions and unread files all count."""
    bare = _diff("pyproject.toml", ["x = 1"], start=9)
    cases = {
        # [tool."pytest".ini_options] is the same table as [tool.pytest.ini_options]
        "quoted table": '[project]\nname = "x"\n\n[tool."pytest".ini_options]\naddopts = "-p evil"\n',
        "dotted key": '[project]\nname = "x"\n[tool]\npytest.ini_options.addopts = "-p evil"\n',
        "new table": '[project]\nname = "x"\n[tool.pytest]\nini_options = {addopts = "-q"}\n',
    }
    for name, head in cases.items():
        got = _rules(bare, {"pyproject.toml": head}, {"pyproject.toml": _PYPROJECT})
        assert ("pyproject.toml", "pytest_config") in got, name
    # an unrelated change, in any spelling of the table, stays quiet
    same = _PYPROJECT.replace(
        "[tool.pytest.ini_options]", '[tool."pytest".ini_options]'
    )
    assert not _rules(bare, {"pyproject.toml": same}, {"pyproject.toml": _PYPROJECT})
    # unread or broken files are never "no change"
    assert _rules(bare, None, {"pyproject.toml": _PYPROJECT})
    assert _rules(bare, {"pyproject.toml": "= broken"}, {"pyproject.toml": ""})
    # new files that pytest reads first, and the other config homes
    for path in ("pytest.toml", ".pytest.toml", "pytest.ini", "tox.ini", "setup.cfg"):
        assert (path, "pytest_config") in _rules(_diff(path, ["[pytest]"])), path
    deleted = (
        "diff --git a/pyproject.toml b/pyproject.toml\ndeleted file mode 100644\n"
        "--- a/pyproject.toml\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n"
    )
    assert ("pyproject.toml", "pytest_config") in _rules(deleted)


def test_env_reads_are_found_in_the_syntax_tree_across_lines():
    """os.environ[...] and os.getenv(...) spread over lines are seen; unparseable text is a sign."""
    multi = (
        "import os\n\n"
        "tok = os.environ[\n"
        '    "HAIPLANE_HUB_CI_TOKEN"\n'
        "]\n"
        "other = os.getenv(\n"
        '    "SOME_TOKEN",\n'
        '    "",\n'
        ")\n"
        'third = os.environ.get(\n    "HAIPLANE_X"\n)\n'
    )
    diff = _diff("tests/test_z.py", multi.splitlines(), start=1)
    got = scan_diff(diff, {"tests/test_z.py": multi})
    assert {s.line for s in got if s.rule == "token_read"} == {3, 6, 10}
    assert not any(s.rule == "text_not_read" for s in got)
    # only the middle line of a multi-line read is added: the read is still this diff's
    middle = _diff("tests/test_z.py", ['    "HAIPLANE_HUB_CI_TOKEN"'], start=4)
    only = scan_diff(middle, {"tests/test_z.py": multi})
    assert {s.line for s in only if s.rule == "token_read"} == {3}
    # the line regex alone would have missed all three
    assert not [
        s
        for s in scan_diff(_diff("tests/test_z.py", ["]"]), {"tests/test_z.py": "]"})
        if s.rule == "token_read"
    ]
    # an environment read that touches no added line is not this diff's
    old = "import os\nx = os.environ['HAIPLANE_A']\ny = 1\n"
    added_y = _diff("tests/test_z.py", ["y = 1"], start=3)
    assert not any(
        s.rule == "token_read" for s in scan_diff(added_y, {"tests/test_z.py": old})
    )
    # unparseable or unread: a sign, with the line regex as the fallback
    broken = ['t = os.environ["HAIPLANE_HUB_CI_TOKEN"', "def ("]
    bd = _diff("tests/test_z.py", broken)
    rules = {s.rule for s in scan_diff(bd, {"tests/test_z.py": "\n".join(broken)})}
    assert "text_not_read" in rules and "token_read" in rules
    assert "text_not_read" in {s.rule for s in scan_diff(bd)}


def test_a_binary_representation_of_a_test_file_is_not_clean():
    """git's «Binary files differ» for a test or conftest file is a sign, not silence."""
    binary = (
        "diff --git a/tests/test_q.py b/tests/test_q.py\nindex 111..222 100644\n"
        "Binary files a/tests/test_q.py and b/tests/test_q.py differ\n"
    )
    assert ("tests/test_q.py", "text_not_read") in _rules(binary)
    no_hunk = "diff --git a/tests/test_q.py b/tests/test_q.py\nindex 111..222 100644\n"
    assert ("tests/test_q.py", "text_not_read") in _rules(no_hunk)
    pure_rename = (
        "diff --git a/tests/a.py b/tests/b.py\nsimilarity index 100%\n"
        "rename from tests/a.py\nrename to tests/b.py\n"
    )
    assert _rules(pure_rename) == set()


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
    # #1767: a script can also be called as a pytest plugin, ``-p scripts.name``,
    # which the path pattern above does not see.
    for src in sources:
        called |= {
            module.replace(".", "/") + ".py"
            for module in re.findall(r"-p (scripts\.[A-Za-z0-9_.]+)", src.read_text())
        }
    assert "scripts/pytest_shard.py" in called, "the plugin form is read"
    missing = called - ci_integrity.CI_SCRIPTS
    assert not missing, f"CI calls scripts the integrity scan does not watch: {missing}"
    assert all((REPO_ROOT / p).exists() for p in called)


# ---- the stored scan and the routing ---------------------------------------


@pytest.fixture
def ci_diff(monkeypatch):
    """What the pinned commit says: ``value`` is its diff (None = not received),
    ``files`` maps a path to its text at that commit, ``head`` overrides the sha
    the clone claims to have resolved."""

    class Box:
        value: str | None = ""
        files: dict = {}
        head: str | None = None

    box = Box()
    box.files = {}

    async def integrity_read(self, repo_path, base, sha):
        if isinstance(box.value, Exception):
            raise box.value
        if box.value is None:
            return None
        return {"diff": box.value, "base_sha": "b" * 40, "head_sha": box.head or sha}

    async def read_file_at_ref(self, repo_path, ref, path, *, limit_chars=30000):
        if path in box.files:
            return {"state": "present", "content": box.files[path], "truncated": False}
        return {"state": "missing", "content": "", "truncated": False}

    monkeypatch.setattr(_PinnedGitOps, "integrity_read", integrity_read)
    monkeypatch.setattr(_PinnedGitOps, "read_file_at_ref", read_file_at_ref)
    return box


async def _auto_task(client, db, monkeypatch, slug: str) -> int:
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    return await _submitted_task(client, db, slug, {"verdict": "auto"})


async def _seen_hash(db: aiosqlite.Connection, task_id: int) -> str:
    """Хеш скана из блока брифа — то, что ревьюер возвращает в ответе (#1913)."""
    block = await ci_integrity.brief_state(db, task_id)
    assert block and block["scan_hash"], block
    return str(block["scan_hash"])


async def _status(client: AsyncClient, task_id: int) -> dict:
    return (await client.get(f"/api/tasks/{task_id}")).json()


async def test_an_explained_ci_change_goes_to_the_owner_with_a_note(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    """AC-2: signs + explained -> no automated verdict; card and brief carry a note <= 600."""
    ci_diff.value = _WORKFLOW_DIFF
    task_id = await _auto_task(client, db, monkeypatch, "ci-explained")
    seen = await _seen_hash(db, task_id)  # #1913: хеш скана из брифа
    long_note = "Меняется шаг отчёта CI: токен теперь берётся из окружения. " * 20
    await _post_review(
        client,
        task_id,
        ci_integrity={
            "verdict": "explained",
            "explanation_ru": long_note,
            "scan_hash": seen,
        },
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
    # (#2037: the steward's right is read there as well, so the verdict is delegated)
    project = await repo.resolve_project_for_task(db, task_id)
    await repo.update_project(db, project["id"], gate_policy='{"verdict": "steward"}')
    await db.commit()

    async def _granted_now(_db):  # #2040: and the global act grant
        return True

    monkeypatch.setattr("hub.services.steward_shadow.act_granted_now", _granted_now)
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
    seen = await _seen_hash(db, task_id)
    await _post_review(
        client,
        task_id,
        ci_integrity={
            "verdict": "explained",
            "explanation_ru": "ci.yml по задаче",
            "scan_hash": seen,
        },
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
    ci_diff.files = {"tests/test_app.py": "assert 1\n"}
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
    base_answer = {
        "verdict": "explained",
        "explanation_ru": "Меняется ci.yml по задаче.",
    }

    # REST
    rest = await _auto_task(client, db, monkeypatch, "ci-path-rest")
    answer = {**base_answer, "scan_hash": await _seen_hash(db, rest)}
    await _post_review(client, rest, ci_integrity=answer)

    # MCP: the tool posts to the same route
    mcp = await _auto_task(client, db, monkeypatch, "ci-path-mcp")
    answer = {**base_answer, "scan_hash": await _seen_hash(db, mcp)}

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
    answer = {**base_answer, "scan_hash": await _seen_hash(db, text)}
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


# ---- the exact commit, the real git ---------------------------------------


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    done = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _commit(ws: Path, name: str, text: str, message: str) -> str:
    (ws / name).parent.mkdir(parents=True, exist_ok=True)
    (ws / name).write_text(text)
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", message)
    return _git(ws, "rev-parse", "HEAD")


@pytest.fixture
def clone(tmp_path):
    """origin + a workspace clone on ``main`` with one commit."""
    import subprocess

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    ws = tmp_path / "ws"
    subprocess.run(["git", "clone", "-q", str(origin), str(ws)], check=True)
    _git(ws, "checkout", "-q", "-b", "main")
    _commit(ws, "README.md", "hi\n", "base")
    _git(ws, "push", "-q", "origin", "main")
    return ws


async def test_the_scan_reads_the_exact_commit_not_a_branch_named_like_it(clone):
    """P1: a remote branch NAMED with the sha of a bad commit B, pointing at a clean A, does not swap B."""
    from hub.integrations.git_ops import GitOpsIntegration

    ws = clone
    _git(ws, "checkout", "-q", "-b", "clean")
    clean = _commit(ws, "docs/a.md", "a\n", "clean A")
    _git(ws, "checkout", "-q", "main")
    _git(ws, "checkout", "-q", "-b", "evil")
    evil = _commit(
        ws,
        "tests/conftest.py",
        "def pytest_sessionfinish(session):\n    pass\n",
        "bad B",
    )
    _git(ws, "push", "-q", "origin", f"{clean}:refs/heads/{evil}")
    _git(ws, "fetch", "-q", "origin")
    assert _git(ws, "rev-parse", f"origin/{evil}") == clean, "the shadow ref exists"

    got = await GitOpsIntegration().integrity_read(str(ws), "main", evil)
    assert got is not None and got["head_sha"] == evil
    assert "tests/conftest.py" in got["diff"] and "docs/a.md" not in got["diff"]
    # the old resolver reads the shadow, which is the defect
    shadow = await GitOpsIntegration().branch_diff(str(ws), "main", evil)
    assert shadow is not None and "docs/a.md" in shadow and "conftest" not in shadow
    # an abbreviated or unknown sha is not a commit to scan
    assert await GitOpsIntegration().integrity_read(str(ws), "main", evil[:12]) is None
    assert await GitOpsIntegration().integrity_read(str(ws), "main", "f" * 40) is None
    # a base name that is not a branch cannot be shadowed into a diff either
    assert await GitOpsIntegration().integrity_read(str(ws), "nope", evil) is None


async def test_a_result_for_another_commit_is_never_stored_as_this_ones(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    """The service refuses a diff whose head is not the pinned sha."""
    ci_diff.value = ""
    ci_diff.head = "c" * 40
    task_id = await _submitted_task(client, db, "ci-other-sha", {"verdict": "human"})
    scan = await ci_integrity.ensure_scan(db, task_id)
    assert scan.state == UNREADABLE and "другому коммиту" in scan.reason
    stored = await store.get_ci_integrity_scan(db, task_id, _TIP)
    assert stored is not None and stored["state"] == UNREADABLE


async def test_a_dash_diff_attribute_does_not_hide_a_hook(clone):
    """P2: with `-diff` git prints «Binary files differ»; the scan asks for text and sees the hook."""
    from hub.integrations.git_ops import GitOpsIntegration

    ws = clone
    _git(ws, "checkout", "-q", "-b", "attr")
    _commit(ws, ".gitattributes", "tests/** -diff\n", "attr")
    sha = _commit(
        ws, "tests/test_h.py", "def pytest_sessionfinish(session):\n    pass\n", "hook"
    )
    plain = _git(ws, "diff", "-U0", "main...attr")
    assert "Binary files" in plain, "the trick works against a plain diff"
    got = await GitOpsIntegration().integrity_read(str(ws), "main", sha)
    assert got is not None and "pytest_sessionfinish" in got["diff"]
    rules = {s.rule for s in scan_diff(got["diff"], {"tests/test_h.py": "x = 1\n"})}
    assert "pytest_report_hook" in rules


# ---- self-approval and the card of a manual gate -----------------------------


async def test_an_agent_cannot_self_approve_a_ci_change(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    """P1: REVIEW_SELF_APPROVE=allow is automation for this check; a clean diff keeps its path."""
    from hub.models import TaskReviewVerdict
    from hub.services.lifecycle import record_review_verdict

    ci_diff.value = _WORKFLOW_DIFF
    signed = await _submitted_task(client, db, "ci-self", {"verdict": "human"})
    with pytest.raises(HTTPException) as err:
        await record_review_verdict(
            db,
            signed,
            TaskReviewVerdict(agent="dev-agent", verdict=ReviewVerdict.approved),
            self_approved=True,
            agent_caller=True,
        )
    assert err.value.status_code == 422 and "ci_integrity" in str(err.value.detail)
    assert (await _status(client, signed))["review_verdict"] is None
    # a human (not an agent token) stays free to decide
    await record_review_verdict(
        db,
        signed,
        TaskReviewVerdict(agent="denis", verdict=ReviewVerdict.approved),
        self_approved=True,
        agent_caller=False,
    )
    assert (await _status(client, signed))["review_verdict"] == "approved"

    # a clean diff keeps the current path for the agent's own approval
    ci_diff.value = ""
    clean = await _submitted_task(client, db, "ci-self-clean", {"verdict": "human"})
    await record_review_verdict(
        db,
        clean,
        TaskReviewVerdict(agent="dev-agent", verdict=ReviewVerdict.approved),
        self_approved=True,
        agent_caller=True,
    )
    assert (await _status(client, clean))["review_verdict"] == "approved"


async def test_the_manual_gate_card_keeps_the_owners_note(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    """P2: verdict=human or a disabled autopilot still shows the stored note on the route line."""
    ci_diff.value = _WORKFLOW_DIFF
    task_id = await _submitted_task(client, db, "ci-manual", {"verdict": "human"})
    seen = await _seen_hash(db, task_id)
    await _post_review(
        client,
        task_id,
        ci_integrity={
            "verdict": "explained",
            "explanation_ru": "Правка ci.yml по задаче.",
            "scan_hash": seen,
        },
    )
    await ci_integrity.ensure_scan(db, task_id)
    for observe in (False, True):
        from hub.services.verdict_route import verdict_route

        route = await verdict_route(db, task_id, observe=observe)
        assert route.final == "human"
        assert "Правка ci.yml по задаче." in route.reason
        assert "Правка ci.yml по задаче." in route.as_dict()["line"]
    body = await _status(client, task_id)
    assert "Правка ci.yml по задаче." in body["verdict_route"]["line"]


# ---- diverged base: the merge-base is the comparison point -------------------

_PP = '[project]\nname = "x"\nversion = "{v}"\n\n[tool.pytest.ini_options]\naddopts = "{a}"\n'


async def _scan_real(clone, monkeypatch, sha: str):
    from unittest.mock import AsyncMock

    from hub.integrations.git_ops import GitOpsIntegration
    from hub.integrations.registry import plugins
    from hub.services import orchestration

    monkeypatch.setattr(plugins, "git_ops", GitOpsIntegration())
    monkeypatch.setattr(
        orchestration,
        "project_git_context",
        AsyncMock(return_value={"repo": str(clone), "base_branch": "main"}),
    )
    return await ci_integrity._scan_now(None, 1, sha)


async def test_a_moved_base_does_not_change_what_the_branch_changed(clone, monkeypatch):
    """P1: the diff and the base pyproject.toml both come from the merge-base."""
    ws = clone
    _commit(ws, "pyproject.toml", _PP.format(v="1", a="-q"), "pyproject")
    _git(ws, "push", "-q", "origin", "main")
    fork = _git(ws, "rev-parse", "HEAD")
    # task A changes only the version
    _git(ws, "checkout", "-q", "-b", "task-a", fork)
    only_version = _commit(ws, "pyproject.toml", _PP.format(v="2", a="-q"), "version")
    # task B changes pytest to what develop will say
    _git(ws, "checkout", "-q", "-b", "task-b", fork)
    same_as_main = _commit(ws, "pyproject.toml", _PP.format(v="1", a="-q -x"), "pytest")
    # the base moves on: pytest addopts changed there
    _git(ws, "checkout", "-q", "main")
    _commit(ws, "pyproject.toml", _PP.format(v="1", a="-q -x"), "main moved")
    _git(ws, "push", "-q", "origin", "main")

    from hub.integrations.git_ops import GitOpsIntegration

    got = await GitOpsIntegration().integrity_read(str(ws), "main", only_version)
    assert got is not None and got["base_sha"] == fork, "the merge-base, not the tip"

    a = await _scan_real(ws, monkeypatch, only_version)
    assert a.state == CLEAN, a  # A: not blocked by a base that moved
    b = await _scan_real(ws, monkeypatch, same_as_main)
    assert b.state == SIGNS and {s.rule for s in b.signs} == {"pytest_config"}, (
        b
    )  # B: still seen


# ---- keyword and computed environment keys ------------------------------------


def test_environment_keys_by_keyword_and_computed_keys_are_seen():
    """P1: key=/name= forms are token reads; a key the scan cannot evaluate is a sign."""

    def rules(src: str) -> set[str]:
        diff = _diff("tests/test_k.py", src.splitlines())
        return {s.rule for s in scan_diff(diff, {"tests/test_k.py": src})}

    assert "token_read" in rules(
        'import os\nx = os.environ.get(key="HAIPLANE_HUB_CI_TOKEN")\n'
    )
    assert "token_read" in rules('import os\nx = os.getenv(key="SOME_TOKEN")\n')
    assert "token_read" in rules(
        'import os\nx = os.getenv(name="HAIPLANE_A", default="")\n'
    )
    assert "env_key_computed" in rules("import os\nk = 'a'\nx = os.environ[k]\n")
    assert "env_key_computed" in rules(
        "import os\nx = os.environ.get('HAI' + 'PLANE_X')\n"
    )
    assert "env_key_computed" in rules("import os\nx = os.getenv(key=name)\n")
    assert "env_key_computed" in rules("import os\nx = os.getenv(**kw)\n")
    # a harmless constant key is neither
    assert not rules("import os\nx = os.environ.get('HOME')\n") & {
        "token_read",
        "env_key_computed",
    }


def test_pytest_config_is_a_sign_at_any_depth():
    """pytest finds the config nearest to the tests: nested files count like the root ones."""
    for path in (
        "tests/pytest.ini",
        "pkg/pytest.ini",
        "tests/tox.ini",
        "src/setup.cfg",
        "tests/pytest.toml",
        "a/b/.pytest.toml",
    ):
        assert (path, "pytest_config") in _rules(_diff(path, ["[pytest]"])), path
    nested = _diff("pkg/pyproject.toml", ["x = 1"])
    assert ("pkg/pyproject.toml", "pytest_config") in _rules(nested)
    assert _rules(_diff("pkg/setup.py", ["x = 1"])) == set()


async def _answered(client, db, task_id: int, **answer) -> dict:
    await _post_review(
        client,
        task_id,
        ci_integrity={"verdict": "explained", "explanation_ru": "ок", **answer},
    )
    return dict(await repo.get_latest_machine_review(db, task_id))


async def test_a_blind_answer_does_not_lift_the_refusal_on_later_signs(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    """#1913: бриф без скана говорит «unscanned»; ответ вслепую не хранится.

    Привязка положительная, со стороны отчёта: ответ несёт хеш скана, который
    ревьюер видел. Скан появился после брифа — хеша у ревьюера нет.
    """
    ci_diff.value = _WORKFLOW_DIFF
    task_id = await _auto_task(client, db, monkeypatch, "ci-blind")
    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert brief["ci_integrity"]["state"] == "unscanned"
    assert brief["ci_integrity"]["scan_hash"] == ""
    await ci_integrity.ensure_scan(db, task_id)  # проход поллера
    row = await _answered(client, db, task_id, explanation_ru="вслепую: ок")
    assert (row["ci_integrity_verdict"], row["ci_integrity_explanation"]) == ("", "")
    task = dict(await repo.get_task(db, task_id))
    standing = await ci_integrity.standing_of(db, task)
    assert standing.answer == "" and standing.gap, "отказ автоматики остаётся"


async def test_building_the_brief_does_not_open_the_answer(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    """Сборки брифа (стюард, REST любого читателя, карточка) ответ не открывают."""
    from hub.services.review_brief import build_review_brief

    ci_diff.value = _WORKFLOW_DIFF
    task_id = await _auto_task(client, db, monkeypatch, "ci-builds")
    await ci_integrity.ensure_scan(db, task_id)
    await build_review_brief(db, task_id)
    await build_review_brief(db, task_id, observe_only=True)
    await client.get(f"/api/tasks/{task_id}/review-brief")
    row = await _answered(client, db, task_id)  # без scan_hash
    assert row["ci_integrity_verdict"] == ""


async def test_an_answer_without_or_for_another_scan_hash_is_dropped(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    ci_diff.value = _WORKFLOW_DIFF
    task_id = await _auto_task(client, db, monkeypatch, "ci-otherhash")
    seen = await _seen_hash(db, task_id)
    row = await _answered(client, db, task_id, scan_hash="0" * 16)
    assert row["ci_integrity_verdict"] == ""
    # Хеш от другого коммита: тот же скан, другой sha — другой хеш.
    other = ci_integrity.scan_hash_of(
        "b" * 40,
        ci_integrity.Scan(
            ci_integrity.SIGNS,
            tuple((await ci_integrity.ensure_scan(db, task_id)).signs),
        ),
    )
    assert other and other != seen
    row = await _answered(client, db, task_id, scan_hash=other)
    assert row["ci_integrity_verdict"] == ""
    row = await _answered(client, db, task_id, scan_hash=seen)
    assert row["ci_integrity_verdict"] == "explained"


async def test_the_answer_survives_a_restart_of_the_hub(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, ci_diff
):
    """Хеш считается из базы, не из памяти процесса: рестарт между брифом и отчётом безвреден."""
    from hub.services import lifecycle

    ci_diff.value = _WORKFLOW_DIFF
    task_id = await _auto_task(client, db, monkeypatch, "ci-restart")
    seen = await _seen_hash(db, task_id)
    lifecycle.forget_observed_tips()  # то, что делает рестарт
    row = await _answered(client, db, task_id, scan_hash=seen)
    assert row["ci_integrity_verdict"] == "explained"


def test_a_null_or_numeric_scan_hash_does_not_cost_the_report():
    """Текстовый блок — единственный путь облачного ревьюера: сбой поля не теряет отчёт."""
    for raw in (None, 12345, " ABC "):
        answer = CIIntegrityAnswer(verdict="explained", scan_hash=raw)  # type: ignore[arg-type]
        assert isinstance(answer.scan_hash, str)
    assert CIIntegrityAnswer(scan_hash=None).scan_hash == ""  # type: ignore[arg-type]
