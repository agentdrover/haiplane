"""Индекс поведенческой спецификации из файла в репозитории (#2214).

Файл ``specs/<capability>/spec.md`` на ветке по умолчанию проекта хаб читает
через git_ops по ref ``origin/<default_branch>`` и кладёт в индекс требования
с двумя ревизиями: SHA blob файла и sha256 канонического JSON блока
``{id, obligation, scenarios}``. Тесты идут по настоящему git-репозиторию
(bare origin + клон), а не по подменённому плагину: ревизия файла - это ответ
самого git.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hub import repository as repo
from hub import repository_spec_index as store
from hub.integrations import git_spec
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins
from hub.services import spec_index
from hub.services.spec_format import SpecParseError, parse_spec_file

BRANCH = "develop"

AUTH_SPEC = """# Auth

## REQ-1
Obligation: The hub refuses a login with an empty password.

### Scenario
- Given: a registered user
- When: the user logs in with an empty password
- Then: the hub answers 401
- Verifiable by: test
- Test ref: tests/test_auth.py::test_empty_password_refused

## REQ-2
Obligation: The hub locks the account after five failed logins.

### Scenario
- Given: a user with four failed logins
- When: the fifth login fails
- Then: the account is locked
- Verifiable by: test
- Test ref: tests/test_auth.py::test_lock_after_five
"""

BILLING_SPEC = """## REQ-10
Obligation: An invoice is issued once per period.

### Scenario
- Given: an open period
- When: the period closes
- Then: one invoice exists
- Verifiable by: review
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(root),
        },
    ).stdout.strip()


class Origin:
    """Bare origin + клон: ветка по умолчанию живёт на origin, клон её читает."""

    def __init__(self, base: Path) -> None:
        self.bare = base / "origin.git"
        self.clone = base / "clone"
        base.mkdir(parents=True, exist_ok=True)
        _git(base, "init", "--bare", "-b", BRANCH, str(self.bare))
        _git(base, "clone", "-q", str(self.bare), str(self.clone))
        _git(self.clone, "checkout", "-q", "-b", BRANCH)

    def publish(self, files: dict[str, str | None], message: str = "spec") -> None:
        """Записать (или удалить: None) файлы, закоммитить и запушить в origin."""
        for rel, text in files.items():
            path = self.clone / rel
            if text is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-q", "-m", message)
        _git(self.clone, "push", "-q", "origin", f"{BRANCH}:{BRANCH}")

    def blob(self, rel: str) -> str:
        return _git(self.clone, "rev-parse", f"origin/{BRANCH}:{rel}")


def _revision(req_id: str, obligation: str, scenarios: list[dict[str, str]]) -> str:
    block = {"id": req_id, "obligation": obligation, "scenarios": scenarios}
    text = json.dumps(block, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def origin(tmp_path: Path) -> Origin:
    return Origin(tmp_path / "git")


@pytest.fixture
async def project_id(db, origin: Origin, monkeypatch) -> int:
    monkeypatch.setattr(plugins, "git_ops", GitOpsIntegration())
    pid = await repo.create_project(
        db,
        slug="specs-proj",
        name="Specs",
        workspace_path=str(origin.clone),
        default_branch=BRANCH,
    )
    await db.commit()
    return pid


async def _snapshot(db, pid: int) -> dict[str, Any]:
    """Всё, что индекс хранит по проекту, - для сравнения «до» и «после»."""
    return {
        "state": await spec_index.index_state(db, pid),
        "files": await spec_index.indexed_files(db, pid),
        "requirements": [
            r.model_dump() for r in await spec_index.indexed_requirements(db, pid)
        ],
    }


async def test_index_stores_requirement_and_both_revisions(db, origin, project_id):
    """AC-1: id, обязательство, сценарий, SHA blob файла и хеш блока требования."""
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})

    result = await spec_index.index_project(db, project_id)

    assert result.state == "indexed", result.detail
    assert result.ref == f"origin/{BRANCH}"
    reqs = {r.id: r for r in await spec_index.indexed_requirements(db, project_id)}
    assert sorted(reqs) == ["REQ-1", "REQ-2"]
    first = reqs["REQ-1"]
    assert first.obligation == "The hub refuses a login with an empty password."
    assert first.path == "specs/auth/spec.md"
    assert first.capability == "auth"
    scenario = {
        "given": "a registered user",
        "when": "the user logs in with an empty password",
        "then": "the hub answers 401",
        "verifiable_by": "test",
        "test_ref": "tests/test_auth.py::test_empty_password_refused",
    }
    assert [s.model_dump() for s in first.scenarios] == [scenario]
    assert first.file_revision == origin.blob("specs/auth/spec.md")
    assert first.revision == _revision("REQ-1", first.obligation, [scenario])
    files = await spec_index.indexed_files(db, project_id)
    assert files == [
        {
            "path": "specs/auth/spec.md",
            "capability": "auth",
            "revision": origin.blob("specs/auth/spec.md"),
        }
    ]
    state = await spec_index.index_state(db, project_id)
    assert state is not None
    assert state["commit_sha"] == _git(origin.clone, "rev-parse", f"origin/{BRANCH}")


async def test_reindex_of_unchanged_file_keeps_revisions(db, origin, project_id):
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    await spec_index.index_project(db, project_id)
    before = await _snapshot(db, project_id)

    # Рабочая копия не источник: правка без коммита индекс не двигает.
    (origin.clone / "specs/auth/spec.md").write_text("garbage, not a spec\n")
    again = await spec_index.index_project(db, project_id)

    assert again.state == "indexed", again.detail
    after = await _snapshot(db, project_id)
    assert after["requirements"] == before["requirements"]
    assert after["files"] == before["files"]


async def test_edit_of_one_requirement_ages_only_its_own_revision(
    db, origin, project_id
):
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    await spec_index.index_project(db, project_id)
    before = {r.id: r for r in await spec_index.indexed_requirements(db, project_id)}

    edited = AUTH_SPEC.replace("five failed logins", "three failed logins")
    origin.publish({"specs/auth/spec.md": edited}, "edit REQ-2")
    await spec_index.index_project(db, project_id)
    after = {r.id: r for r in await spec_index.indexed_requirements(db, project_id)}

    assert after["REQ-1"].revision == before["REQ-1"].revision
    assert after["REQ-2"].revision != before["REQ-2"].revision
    assert after["REQ-1"].file_revision == after["REQ-2"].file_revision
    assert after["REQ-1"].file_revision != before["REQ-1"].file_revision


async def test_only_spec_files_of_the_convention_are_indexed(db, origin, project_id):
    origin.publish(
        {
            "specs/auth/spec.md": AUTH_SPEC,
            "specs/billing/spec.md": BILLING_SPEC,
            "specs/auth/notes.md": "## not a requirement\n",
            "specs/deep/er/spec.md": "broken but out of convention\n",
            "docs/spec.md": "broken but out of convention\n",
        }
    )

    result = await spec_index.index_project(db, project_id)

    assert result.state == "indexed", result.detail
    assert [f["path"] for f in await spec_index.indexed_files(db, project_id)] == [
        "specs/auth/spec.md",
        "specs/billing/spec.md",
    ]
    ids = [r.id for r in await spec_index.indexed_requirements(db, project_id)]
    assert ids == ["REQ-1", "REQ-2", "REQ-10"]


async def test_bad_spec_keeps_previous_index(db, origin, project_id):
    """AC-2: дубль id или ошибка разбора - отказ с именем файла, индекс цел."""
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    assert (await spec_index.index_project(db, project_id)).state == "indexed"
    good = await _snapshot(db, project_id)

    # Два файла с одним REQ-1.
    clash = BILLING_SPEC.replace("REQ-10", "REQ-1")
    origin.publish({"specs/billing/spec.md": clash}, "duplicate id across files")
    dup = await spec_index.index_project(db, project_id)
    assert dup.state == "refused"
    assert "specs/billing/spec.md" in dup.detail
    assert "REQ-1" in dup.detail
    assert {issue.file for issue in dup.issues} >= {"specs/billing/spec.md"}
    assert await _snapshot(db, project_id) == good

    # Файл не разбирается; прежний дубль при этом исправлен.
    origin.publish(
        {"specs/billing/spec.md": "## REQ-7\nno obligation line here\n"}, "bad file"
    )
    bad = await spec_index.index_project(db, project_id)
    assert bad.state == "refused"
    assert [i.file for i in bad.issues] == ["specs/billing/spec.md"]
    assert "specs/billing/spec.md" in bad.detail
    assert await _snapshot(db, project_id) == good

    # Файл с дублем внутри себя.
    origin.publish({"specs/billing/spec.md": BILLING_SPEC * 2}, "twice in one file")
    twice = await spec_index.index_project(db, project_id)
    assert twice.state == "refused"
    assert "specs/billing/spec.md" in twice.detail
    assert await _snapshot(db, project_id) == good

    # Починка возвращает индекс в строй: отказ ничего не залипил.
    origin.publish({"specs/billing/spec.md": BILLING_SPEC}, "fixed")
    fixed = await spec_index.index_project(db, project_id)
    assert fixed.state == "indexed", fixed.detail
    ids = [r.id for r in await spec_index.indexed_requirements(db, project_id)]
    assert ids == ["REQ-1", "REQ-2", "REQ-10"]


async def test_first_index_refused_leaves_nothing(db, origin, project_id):
    origin.publish({"specs/auth/spec.md": "## REQ-x\nObligation: bad id\n"})

    result = await spec_index.index_project(db, project_id)

    assert result.state == "refused"
    assert await spec_index.index_state(db, project_id) is None
    assert await spec_index.indexed_requirements(db, project_id) == []


async def test_unreadable_ref_is_not_an_empty_spec(db, origin, project_id):
    """Git не ответил - это «неизвестно»: прежний индекс не стирается."""
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    await spec_index.index_project(db, project_id)
    good = await _snapshot(db, project_id)
    await db.execute(
        "UPDATE projects SET default_branch='no-such-branch' WHERE id=?",
        (project_id,),
    )
    await db.commit()

    result = await spec_index.index_project(db, project_id)

    assert result.state == "unreadable"
    assert "no-such-branch" in result.detail
    assert await _snapshot(db, project_id) == good


async def test_removed_spec_file_empties_its_part_of_the_index(db, origin, project_id):
    origin.publish(
        {"specs/auth/spec.md": AUTH_SPEC, "specs/billing/spec.md": BILLING_SPEC}
    )
    await spec_index.index_project(db, project_id)

    origin.publish({"specs/billing/spec.md": None}, "drop billing")
    result = await spec_index.index_project(db, project_id)

    assert result.state == "indexed"
    ids = [r.id for r in await spec_index.indexed_requirements(db, project_id)]
    assert ids == ["REQ-1", "REQ-2"]


async def test_failed_write_rolls_back_the_whole_index(
    db, origin, project_id, monkeypatch
):
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    await spec_index.index_project(db, project_id)
    good = await _snapshot(db, project_id)
    origin.publish({"specs/billing/spec.md": BILLING_SPEC}, "add billing")

    real_insert = store.insert_requirement
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return await real_insert(*args, **kwargs)

    monkeypatch.setattr(store, "insert_requirement", flaky)
    with pytest.raises(RuntimeError):
        await spec_index.index_project(db, project_id)

    assert await _snapshot(db, project_id) == good


def test_parser_rejects_ids_outside_the_req_pattern():
    for bad in ("REQ-", "REQ-1a", "req-1", "R-1", "REQ-01x"):
        with pytest.raises(SpecParseError):
            parse_spec_file(f"## {bad}\nObligation: x\n")


def test_parser_requires_scenario_fields():
    text = "## REQ-1\nObligation: x\n\n### Scenario\n- Given: a\n- When: b\n"
    with pytest.raises(SpecParseError, match="then"):
        parse_spec_file(text)


def test_parser_normalizes_whitespace_so_layout_does_not_age_a_requirement():
    spaced = "## REQ-1\nObligation:   a  long\n   obligation\n"
    tight = "## REQ-1\nObligation: a long obligation\n"
    assert parse_spec_file(spaced) == parse_spec_file(tight)


def test_parser_refuses_an_unknown_field_instead_of_gluing_it_to_the_previous_one():
    """``Note:`` под полем не приписывается к его значению молча."""
    text = (
        "## REQ-1\nObligation: x\n\n### Scenario\n- Given: a\n- When: b\n"
        "- Then: c\n- Verifiable by: test\n- Test ref: tests/t.py::t\n"
        "Note: reviewed by the team\n"
    )
    with pytest.raises(SpecParseError, match="unknown field"):
        parse_spec_file(text)
    with pytest.raises(SpecParseError, match="unexpected text"):
        parse_spec_file("## REQ-1\nObligation: x\nstray words\n")


def test_parser_joins_an_indented_continuation_into_the_value():
    text = "## REQ-1\nObligation: refuses\n  an empty password\n"
    assert parse_spec_file(text)[0].obligation == "refuses an empty password"


async def test_an_oversized_spec_file_is_refused_unread(
    db, origin, project_id, monkeypatch
):
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    monkeypatch.setattr(git_spec, "MAX_SPEC_BYTES", len(AUTH_SPEC) - 1)
    blob_reads: list[str] = []
    real = git_spec.proc.run_capped

    async def spy(*cmd, **kw):
        if "cat-file" in cmd:
            blob_reads.append(cmd[-1])
        return await real(*cmd, **kw)

    monkeypatch.setattr(git_spec.proc, "run_capped", spy)

    result = await spec_index.index_project(db, project_id)

    assert result.state == "refused"
    assert [i.file for i in result.issues] == ["specs/auth/spec.md"]
    assert "larger than" in result.detail
    assert blob_reads == [], "the size comes from the tree: no read of a too-big file"


async def test_too_many_spec_files_is_unreadable_not_a_partial_index(
    db, origin, project_id, monkeypatch
):
    origin.publish(
        {"specs/auth/spec.md": AUTH_SPEC, "specs/billing/spec.md": BILLING_SPEC}
    )
    monkeypatch.setattr(git_spec, "MAX_SPEC_FILES", 1)

    result = await spec_index.index_project(db, project_id)

    assert result.state == "unreadable"
    assert "more than 1 spec files" in result.detail
    assert await spec_index.index_state(db, project_id) is None


async def test_a_git_timeout_is_named_a_failure_not_a_missing_ref(
    db, origin, project_id, monkeypatch
):
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})

    async def timed_out(*cmd, **kw):
        return git_spec.proc.TIMEOUT_RC, "", "timed out after 30s: git -C rev-parse"

    monkeypatch.setattr(git_spec.proc, "run", timed_out)

    result = await spec_index.index_project(db, project_id)

    assert result.state == "unreadable"
    assert "timed out" in result.detail
    assert "is not readable" not in result.detail
