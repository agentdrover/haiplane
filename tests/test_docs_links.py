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


def test_implementer_path_spec_freezes_intake_961():
    # #984: path pack lives next to #961; intake SDD must point at it.
    path_spec = REPO_ROOT / "docs" / "issues" / "chat-pair-implementer-path.md"
    intake = REPO_ROOT / "docs" / "issues" / "task-961-chat-pair.md"
    assert path_spec.is_file()
    intake_text = intake.read_text(encoding="utf-8")
    assert "Intake заморожен" in intake_text
    assert "chat-pair-implementer-path.md" in intake_text
    spec = path_spec.read_text(encoding="utf-8")
    assert "kind=implementer" in spec
    assert "#983" in spec


def test_implementer_sdd_matches_allowlist_and_freezes_intake_role():
    # #979: full SDD next to #961; presentational role stays intake-only.
    sdd = (
        REPO_ROOT / "docs" / "issues" / "task-979-chat-pair-implementer.md"
    ).read_text(encoding="utf-8")
    assert "CHAT_PAIR_IMPLEMENTER_ALLOWLIST" in sdd
    assert "POST   /api/tasks/{task_id}/pair-start" in sdd
    assert "POST /api/tasks" in sdd  # closed-routes table names create
    assert "chat_pair_task_not_open" in sdd
    intake = (REPO_ROOT / "docs" / "issues" / "task-961-chat-pair.md").read_text(
        encoding="utf-8"
    )
    assert "Intake-only" in intake
    assert "task-979-chat-pair-implementer.md" in intake
