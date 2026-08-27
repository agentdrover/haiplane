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
