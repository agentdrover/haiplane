from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from hub.db import _SCHEMA, _migrate, seed_system_roles, _table_exists
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
        self, task_id, title, *, branch_slug="", repo=None, base_branch=None
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
