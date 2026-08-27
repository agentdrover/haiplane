from __future__ import annotations

import subprocess
from pathlib import Path

from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from hub.db import (
    _SCHEMA,
    _migrate,
    seed_system_roles,
    seed_chat_pair_agent,
    _table_exists,
)
from hub.integrations.noop import (
    NoopDispatch,
    NoopGitHub,
    NoopGitOps,
    NoopNotes,
    NoopTranscripts,
    NoopVast,
)
from hub.integrations.registry import plugins


class MockDispatch(NoopDispatch):
    """Dispatch mock that returns a predictable job_id on submit."""

    def is_available(self):
        return True

    async def submit_task(
        self, message, runtime="auto", repo_root=None, agent=None, task_id=None
    ):
        return {"job_id": "test-job-1"}

    def build_enriched_message(
        self, title, description, updates=None, branch="", breadcrumb=""
    ):
        return f"test message: {title}"


class MockGitOps(NoopGitOps):
    async def create_branch(self, task_id, title, repo=None, base_branch=None):
        from hub.integrations.git_ops import _slugify

        return f"task-{task_id}/{_slugify(title)}"

    async def pair_prepare_branch(
        self,
        task_id,
        title,
        *,
        branch_slug="",
        repo=None,
        base_branch=None,
        notify=None,
    ):
        from hub.integrations.git_ops import _slugify

        slug = branch_slug or _slugify(title)
        return f"task-{task_id}/{slug}"

    async def pair_restore_workspace_base(
        self, task_id, *, repo=None, base_branch=None
    ):
        return False

    async def checkout(self, branch, repo=None):
        return True


@pytest.fixture(autouse=True)
def _setup_mock_plugins():
    """Install mock plugins for all tests, restore originals after."""
    orig_dispatch = plugins.dispatch
    orig_git_ops = plugins.git_ops
    orig_github = plugins.github
    orig_notes = plugins.notes
    orig_vast = plugins.vast
    orig_transcripts = plugins.transcripts

    plugins.dispatch = MockDispatch()
    plugins.git_ops = MockGitOps()
    plugins.github = NoopGitHub()
    plugins.notes = NoopNotes()
    plugins.vast = NoopVast()
    plugins.transcripts = NoopTranscripts()

    yield

    plugins.dispatch = orig_dispatch
    plugins.git_ops = orig_git_ops
    plugins.github = orig_github
    plugins.notes = orig_notes
    plugins.vast = orig_vast
    plugins.transcripts = orig_transcripts


@pytest.fixture
async def db():
    """In-memory SQLite database with Hub schema and migrations."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.executescript(_SCHEMA)
    await _migrate(conn)
    if await _table_exists(conn, "roles"):
        await seed_system_roles(conn)
    if await _table_exists(conn, "principals"):
        await seed_chat_pair_agent(conn)
    yield conn
    await conn.close()


@pytest.fixture
async def client(db):
    """httpx AsyncClient wired to the FastAPI app with in-memory DB."""
    with patch("hub.poller.start_poller", return_value=AsyncMock()):
        from hub.app import app

        app.state.db = db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


def _git_in(root: Path, *args: str) -> str:
    """Run git in ``root`` with a hermetic environment, returning stdout."""
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


@pytest.fixture
def history(tmp_path: Path) -> dict[str, str]:
    """A repo where one commit shipped and one is merged but still waiting (#497).

    ``released`` is the tip of main; ``shipped`` is behind it, so it is part of
    what production runs. ``pending`` sits on a side branch — merged work no
    release has picked up yet, which is the case the hub could not see before.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git_in(root, "init", "-b", "main")
    (root / "a.py").write_text("a = 1\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "shipped")
    shipped = _git_in(root, "rev-parse", "HEAD")
    (root / "b.py").write_text("b = 2\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "released")
    released = _git_in(root, "rev-parse", "HEAD")
    _git_in(root, "checkout", "-q", "-b", "later", shipped)
    (root / "c.py").write_text("c = 3\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "merged but not released")
    pending = _git_in(root, "rev-parse", "HEAD")
    return {
        "repo": str(root),
        "shipped": shipped,
        "released": released,
        "pending": pending,
    }


@pytest.fixture
def squash_release(tmp_path: Path) -> dict[str, str]:
    """develop released into main by squash, plus work merged after it."""
    root = tmp_path / "squashed"
    root.mkdir()
    _git_in(root, "init", "-b", "main")
    (root / "a.py").write_text("a = 1\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "base")

    _git_in(root, "checkout", "-q", "-b", "develop")
    (root / "b.py").write_text("b = 2\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "gate merges the task into develop")
    task_merge = _git_in(root, "rev-parse", "HEAD")
    (root / "c.py").write_text("c = 3\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "another task rides the same release")
    develop_tip = _git_in(root, "rev-parse", "HEAD")

    # The release itself: exactly what the poller does — squash, not merge.
    _git_in(root, "checkout", "-q", "main")
    _git_in(root, "merge", "--squash", "develop")
    _git_in(root, "commit", "-m", "release: develop -> main")
    released = _git_in(root, "rev-parse", "HEAD")

    # Work that landed in develop AFTER the release went out.
    _git_in(root, "checkout", "-q", "develop")
    (root / "d.py").write_text("d = 4\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "merged after the release")
    after_release = _git_in(root, "rev-parse", "HEAD")

    return {
        "repo": str(root),
        "task_merge": task_merge,
        "develop_tip": develop_tip,
        "released": released,
        "after_release": after_release,
    }
