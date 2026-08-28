"""Documentation link checks."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_readme_links_agent_mcp_operator_guide():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    guide = REPO_ROOT / "docs" / "agent-mcp-operator-guide.md"
    assert guide.is_file()
    assert "docs/agent-mcp-operator-guide.md" in readme


def test_agent_mcp_guide_covers_troubleshooting_errors():
    text = (REPO_ROOT / "docs" / "agent-mcp-operator-guide.md").read_text(
        encoding="utf-8"
    )
    for needle in ("401", "421", "406", "Missing session"):
        assert needle in text, f"missing troubleshooting coverage for {needle}"


def test_operator_guide_separates_intake_and_implementer_pairing():
    # #982: 4b is the implementer path. 4a must stay intake copy.
    text = (REPO_ROOT / "docs" / "agent-mcp-operator-guide.md").read_text(
        encoding="utf-8"
    )
    assert "## 4a. Cloud / iOS: чат без MCP" in text
    assert "постановку и уточнение задач" in text
    assert "## 4b. Cloud: исполнитель на одну open-задачу" in text
    assert "kind=implementer" in text
    assert "Передать в облачный чат" in text
    assert "git_mode=remote" in text
    idx_4a = text.index("## 4a.")
    idx_4b = text.index("## 4b.")
    assert idx_4a < idx_4b
    intake = text[idx_4a:idx_4b]
    assert "Передать в облачный чат" not in intake
    section_4b = text[idx_4b:]
    assert "отдельная задача (#983)" not in section_4b
    assert "Продления нет" in section_4b
    assert "возвращает её в `open`" in section_4b


def test_no_links_to_removed_internal_docs():
    """#1004: the per-task SDDs and admin design drafts left the public repo.

    They were working notes, not product documentation. What must not survive
    them is a dangling pointer: a reader following one lands on nothing.
    """
    removed = (
        "docs/issues/",
        "admin-section-design",
        "admin-ui-functional-spec",
        "software-development-workflow-implementation-plan",
    )
    roots = [REPO_ROOT / "docs", REPO_ROOT / "skills", REPO_ROOT / "agents"]
    files = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "README.en.md",
        REPO_ROOT / "AGENTS.md",
    ]
    for root in roots:
        if root.is_dir():
            files.extend(p for p in root.rglob("*") if p.suffix in {".md", ".html"})
    for path in files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for needle in removed:
            assert needle not in text, f"{path} still points at removed {needle}"


def test_implementer_allowlist_stays_pinned():
    """#1004: the deleted SDD used to hold this list; the test outlives it.

    ``CHAT_PAIR_IMPLEMENTER_ALLOWLIST`` is what a restricted implementer
    session may call, so a route arriving there silently is exactly the
    change nobody should be able to make in passing. The per-task SDD that
    used to pin it left the public repository with the rest of the working
    papers, and for a few minutes this surface had no guard at all. Pinning
    it here costs one deliberate edit per intended change and refuses the
    accidental one.
    """
    from hub.auth import CHAT_PAIR_IMPLEMENTER_ALLOWLIST

    assert set(CHAT_PAIR_IMPLEMENTER_ALLOWLIST) == {
        ("GET", "/api/whoami"),
        ("GET", "/api/diagnostics/identity"),
        ("GET", "/api/tasks/{task_id}"),
        ("GET", "/api/tasks/{task_id}/tree"),
        ("GET", "/api/tasks/{task_id}/context"),
        ("GET", "/api/tasks/{task_id}/readiness"),
        ("GET", "/api/tasks/{task_id}/review-brief"),
        ("GET", "/api/tasks/{task_id}/acceptance_criteria"),
        ("GET", "/api/tasks/{task_id}/updates"),
        ("POST", "/api/tasks/{task_id}/updates"),
        ("POST", "/api/tasks/{task_id}/question"),
        ("POST", "/api/tasks/{task_id}/claim"),
        ("POST", "/api/tasks/{task_id}/pair-start"),
        ("POST", "/api/tasks/{task_id}/submit-review"),
        ("POST", "/api/tasks/{task_id}/declare-wait"),
        ("POST", "/api/sessions/register"),
        ("POST", "/api/sessions/{session_id}/heartbeat"),
        ("POST", "/api/auth/chat-pair/redeem"),
        ("POST", "/api/auth/chat-pair/revoke"),
    }
