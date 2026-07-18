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
