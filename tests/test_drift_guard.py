"""Commits that reach a base branch outside the pipeline are noticed (#534).

Branch protection is unavailable on a private repository on GitHub's free
plan, and the client-side hook is bypassed by --no-verify. Prevention is not
on the table; noticing is.

The hard part is not detection, it is not crying wolf. The hub merges through
``gh pr merge --squash``, so its own merges land on the base as ordinary
non-merge commits — the same shape a direct push leaves. Graph shape alone
cannot tell them apart.

Submission #1 used the "(#N)" in the subject as proof of a pull request, and
a direct push titled ``hotfix: bypass auth (#42)`` walked straight through.
Submission #2 required the number to be one the hub had merged — and review
showed that only moved the goalpost, because a merged number is a git log
away and the subject is still written by whoever pushes.

The discriminator is now the SHA of the commit the hub's merge produced.
Nobody chooses their own SHA.
"""

from __future__ import annotations

import aiosqlite
import pytest

from hub import repository as repo
from hub.services import drift_guard
from hub.services.drift_guard import DriftReport, check_project, classify_commits

SEP = "\x1f"


def _log(*rows: tuple[str, str, str]) -> str:
    return "\n".join(SEP.join(r) for r in rows)


class _Git:
    """Git adapter double: canned fetch outcome and log output."""

    def __init__(
        self, *, log: str | None = "", fetch_ok: bool = True, detail: str = ""
    ):
        self._log = log
        self._fetch = (fetch_ok, detail)
        self.fetched: list[tuple[str, str]] = []

    async def fetch_base(self, repo_path: str, base: str):
        self.fetched.append((repo_path, base))
        return self._fetch

    async def first_parent_log(self, repo_path: str, base: str, limit: int):
        return self._log


@pytest.fixture
def git(monkeypatch):
    def _install(**kwargs) -> _Git:
        double = _Git(**kwargs)
        from hub.integrations.registry import plugins

        monkeypatch.setattr(plugins, "git_ops", double)
        return double

    return _install


async def _project_row(db: aiosqlite.Connection) -> int:
    """A project the checker will actually visit: active, with a workspace."""
    created = await repo.create_project(db, slug="p", name="P")
    project_id = created if isinstance(created, int) else created["id"]
    await db.execute(
        "UPDATE projects SET workspace_path='/srv/w', default_branch='develop' "
        "WHERE id=?",
        (project_id,),
    )
    await db.commit()
    return project_id


async def _project_with_baseline(
    db: aiosqlite.Connection, baseline: str = "base000"
) -> int:
    project_id = await _project_row(db)
    await db.execute(
        "UPDATE projects SET drift_baseline_sha=? WHERE id=?", (baseline, project_id)
    )
    await db.commit()
    return project_id


def _project(**overrides) -> dict:
    base = {
        "id": 1,
        "slug": "default",
        "default_branch": "develop",
        "workspace_path": "/srv/workspace",
        "status": "active",
        "archived": 0,
        # Set by default so most tests exercise judgement rather than the
        # first-look path, which deliberately judges nothing.
        "drift_baseline_sha": "base000",
    }
    base.update(overrides)
    return base


# --- AC-1: a direct commit is reported, exactly once -----------------------


async def test_a_commit_written_straight_onto_the_base_is_reported(git):
    git(
        log=_log(
            ("aaa111", "hotfix: straight onto develop", "someone"),
            ("base000", "older history", "hub"),
        )
    )

    report = await check_project(_project())

    assert report.status == "drift"
    assert [c.sha for c in report.commits] == ["aaa111"]
    assert report.commits[0].subject == "hotfix: straight onto develop"


# --- AC-2: no false alarm on the hub's own merges --------------------------


async def test_a_merge_the_hub_performed_is_not_drift(db: aiosqlite.Connection, git):
    """AC-2. The pipeline's own merge must never raise an alert — a guard
    that fires on correct work gets muted, and then the real drift is missed
    too."""
    project_id = await _project_with_baseline(db)
    await repo.record_pipeline_merge(
        db, pr_number=42, merge_sha="ccc333", project_id=project_id
    )
    git(
        log=_log(
            ("ccc333", "fix(task): squashed by the pipeline (#42)", "hub"),
            ("base000", "older history", "hub"),
        )
    )

    reports = await drift_guard.check_all_projects(db)

    assert [r.status for r in reports if r.project_slug == "p"] == ["clean"]


async def test_a_pull_request_number_in_the_subject_proves_nothing(
    db: aiosqlite.Connection, git
):
    """The finding that returned submission #2, and the reason the evidence
    is a SHA.

    Submission #1 excused any "(#N)". Submission #2 required the number to be
    one the hub had merged — which only moved the goalpost: the number is
    text the pusher writes, and a merged one is a git log away. Here PR #42
    really was merged, as a different commit; this push is still a push."""
    project_id = await _project_with_baseline(db)
    await repo.record_pipeline_merge(
        db, pr_number=42, merge_sha="realsha", project_id=project_id
    )
    git(
        log=_log(
            ("forged1", "hotfix: bypass auth (#42)", "someone"),
            ("base000", "older history", "hub"),
        )
    )

    reports = await drift_guard.check_all_projects(db)

    report = next(r for r in reports if r.project_slug == "p")
    assert report.status == "drift", "PR #42 was merged, but not as this commit"
    assert [c.sha for c in report.commits] == ["forged1"]


async def test_one_project_cannot_vouch_for_another(db: aiosqlite.Connection):
    """The second finding of submission #2.

    Merges recorded with a NULL project_id were read as legitimate
    everywhere, so a merge from a task without a project excused a direct
    push in any other project."""
    project_id = await _project_with_baseline(db)
    await repo.record_pipeline_merge(
        db, pr_number=77, merge_sha="shared0", project_id=None
    )

    known = await repo.known_pipeline_shas(db, project_id)

    assert known == set(), "a merge with no project vouches for no project"


# --- AC-3: the base branch is never assumed --------------------------------


async def test_the_project_base_branch_is_used_not_a_default(git):
    """calc-kids lives on master. A hardcoded develop would check a branch
    that does not exist there and report nothing forever."""
    double = git(log="")

    report = await check_project(
        _project(id=2, slug="calc-kids", default_branch="master")
    )

    assert double.fetched == [("/srv/workspace", "master")]
    assert report.base_branch == "master"


# --- AC-4: an unusable environment says so ---------------------------------


async def test_a_failed_fetch_is_unknown_not_clean(git):
    """The distinction this task turns on. On production the hub's own project
    has no remote at all; reporting "clean" there would be reporting on
    nothing while the milestone read as delivered."""
    git(fetch_ok=False, detail="Could not read from remote repository")

    report = await check_project(_project())

    assert report.status == "unknown"
    assert not report.checked
    assert "Could not read" in report.reason


async def test_an_unreadable_log_is_unknown(git):
    git(log=None)

    report = await check_project(_project())

    assert report.status == "unknown"


async def test_a_project_without_a_repository_says_why(git):
    """The state production is actually in: no workspace path recorded."""
    git()

    report = await check_project(_project(workspace_path=""))

    assert report.status == "unknown"
    assert "workspace_path" in report.reason


async def test_a_raising_adapter_does_not_break_the_caller(monkeypatch):
    """Best-effort by contract: the check is a background nicety and must
    never take the flow down with it."""

    class _Boom:
        async def fetch_base(self, repo_path, base):
            raise RuntimeError("ssh exploded")

    from hub.integrations.registry import plugins

    monkeypatch.setattr(plugins, "git_ops", _Boom())

    report = await check_project(_project())

    assert report.status == "unknown"
    assert "ssh exploded" in report.reason


# --- AC-5: the same drift is recorded once ---------------------------------


async def test_the_same_drift_is_recorded_only_once(
    db: aiosqlite.Connection, git, monkeypatch
):
    project_id = await _project_with_baseline(db)
    git(log=_log(("ddd444", "direct push", "someone"), ("base000", "history", "hub")))

    first = await drift_guard.check_all_projects(db)
    second = await drift_guard.check_all_projects(db)

    assert [r.status for r in first if r.project_slug == "p"] == ["drift"]
    assert [r.status for r in second if r.project_slug == "p"] == ["drift"], (
        "the drift is still there — the report says so every time"
    )
    rows = await repo.list_drift_commits(db, project_id)
    assert len(rows) == 1, "but it is recorded once, so the alert fires once"


async def test_a_second_distinct_drift_is_recorded(
    db: aiosqlite.Connection, git, monkeypatch
):
    """Deduplication must not swallow a new violation."""
    project_id = await _project_with_baseline(db)

    git(log=_log(("eee555", "first direct push", "someone"), ("base000", "h", "hub")))
    await drift_guard.check_all_projects(db)
    git(
        log=_log(
            ("fff666", "second direct push", "someone"),
            ("eee555", "first direct push", "someone"),
            ("base000", "h", "hub"),
        )
    )
    await drift_guard.check_all_projects(db)

    rows = await repo.list_drift_commits(db, project_id)
    assert {dict(r)["sha"] for r in rows} == {"eee555", "fff666"}


# --- the classifier, in isolation ------------------------------------------


def test_malformed_log_lines_are_skipped_not_guessed_at():
    """A line the format did not produce is not evidence of anything."""
    assert classify_commits("garbage without separators") == []
    assert classify_commits("") == []
    assert classify_commits(None) == []


def test_a_subject_containing_spaces_survives_parsing():
    commits = classify_commits(_log(("abc", "a subject with spaces", "A Name")))
    assert commits[0].subject == "a subject with spaces"
    assert commits[0].author == "A Name"


# --- the constraint: nothing destructive -----------------------------------


def test_the_module_calls_nothing_that_writes():
    """The task forbids rollback, force-push and deletion outright.

    Checked against the call graph rather than the text: a first version
    grepped the whole file and tripped over the words "direct push" in its own
    docstring. A guard that fires on prose is a guard nobody keeps.
    """
    import ast
    from pathlib import Path

    forbidden = {
        "push",
        "force_push",
        "squash_branch",
        "merge_pr",
        "delete_branch",
        "reset",
        "checkout",
    }
    tree = ast.parse(Path("hub/services/drift_guard.py").read_text())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert not (called & forbidden), (
        f"drift_guard must only observe; it calls {sorted(called & forbidden)}"
    )


def test_report_states_are_the_three_the_task_requires():
    assert DriftReport("p", "develop", "clean").checked is True
    assert DriftReport("p", "develop", "drift").checked is True
    assert DriftReport("p", "develop", "unknown", "no remote").checked is False


# --- the two findings that returned submission #1 --------------------------


async def test_a_detected_drift_reaches_the_operator(db: aiosqlite.Connection, git):
    """A row in a table nobody opens is not an alert.

    Submission #1 recorded drift privately and logged it. Review was right
    that this changes nothing for the person who is supposed to react: the
    activity feed is where they look."""
    project_id = await _project_with_baseline(db)
    git(log=_log(("999aaa", "straight to develop", "someone"), ("base000", "h", "hub")))

    await drift_guard.check_all_projects(db)

    events = await db.execute_fetchall(
        "SELECT kind, payload FROM events WHERE kind='base_branch_drift'"
    )
    assert len(events) == 1, "the drift has to appear in the feed"
    assert "999aaa" in dict(events[0])["payload"]

    activity = await db.execute_fetchall(
        "SELECT summary FROM activity_log WHERE kind='base_branch_drift'"
    )
    assert len(activity) == 1
    assert "develop" in dict(activity[0])["summary"]
    assert project_id


async def test_a_repeat_does_not_repeat_the_alert(db: aiosqlite.Connection, git):
    """AC-5 over the visible channel, not just the table: the operator must
    not be pinged twice for one violation."""
    await _project_with_baseline(db)
    git(log=_log(("888bbb", "straight to develop", "someone"), ("base000", "h", "hub")))

    await drift_guard.check_all_projects(db)
    await drift_guard.check_all_projects(db)

    events = await db.execute_fetchall(
        "SELECT id FROM events WHERE kind='base_branch_drift'"
    )
    assert len(events) == 1


def test_the_check_is_actually_scheduled():
    """Submission #1 shipped the checker with no caller: code and table in
    place, nothing ever running them. Asserted against the source so the
    trigger cannot quietly disappear again."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("hub/poller.py").read_text())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "check_all_projects" in called, (
        "the drift guard has no trigger — it would never run"
    )

    starters = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "start_poller"
    ]
    started = {
        arg.func.id
        for fn in starters
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        for arg in node.args
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
    }
    assert "_drift_watch" in started, "start_poller must launch the drift watch"


async def test_clean_says_how_far_it_looked(db: aiosqlite.Connection, git):
    """The window is fixed and the baseline never moves, so after enough merges
    the baseline drops out the bottom of the window. "Clean" then means "clean
    in the last fifty" — and the word alone does not say so. The report names
    the range it read, and says when the baseline was out of reach."""
    await _project_with_baseline(db, "base000")

    git(log=_log(("base000", "the baseline", "hub")))
    reached = next(
        r for r in await drift_guard.check_all_projects(db) if r.project_slug == "p"
    )
    assert reached.status == "clean"
    assert "checked the last" in reached.reason
    assert "older than that window" not in reached.reason

    # Same project, but the baseline is no longer inside the window.
    git(log=_log(("cccddd", "a merge, long after the baseline", "hub")))
    lost = next(
        r for r in await drift_guard.check_all_projects(db) if r.project_slug == "p"
    )
    assert "older than that window" in lost.reason, (
        "a check that could not reach its baseline must not read as clean overall"
    )


async def test_the_first_look_judges_nothing(db: aiosqlite.Connection, git):
    """History written before the hub recorded its own merges cannot be
    judged. Reporting it would bury the operator in alerts about work that
    went through the pipeline correctly — the noise the recorded risk warns
    destroys the signal."""
    project_id = await _project_row(db)  # no baseline yet
    git(log=_log(("newhead", "some old commit", "someone")))

    reports = await drift_guard.check_all_projects(db)

    report = next(r for r in reports if r.project_slug == "p")
    assert report.status == "clean"
    assert "baseline" in report.reason
    rows = await repo.list_drift_commits(db, project_id)
    assert rows == [], "nothing is reported on the first look"

    projects = await db.execute_fetchall(
        "SELECT drift_baseline_sha FROM projects WHERE id=?", (project_id,)
    )
    assert dict(projects[0])["drift_baseline_sha"] == "newhead"
