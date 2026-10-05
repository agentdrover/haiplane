"""Documentation link checks."""

from __future__ import annotations

import re
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
        # The whole directory, not a list of names: "issues" invited per-task
        # working papers and collected five of them. The one document there
        # worth publishing — the steward spec (#1002) — moved to docs/specs/,
        # which is what it always was. A pointer at the old path is a bug now.
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


def test_steward_spec_links_resolve():
    # #1002: the steward spec cites hub modules by relative path so a reader can
    # check every claim it makes about the code. A moved or renamed module must
    # break this suite rather than leave the document quietly pointing at
    # nothing — the citations are the reason to trust it.
    spec = REPO_ROOT / "docs" / "specs" / "steward-agent.md"
    assert spec.is_file()
    text = spec.read_text(encoding="utf-8")
    targets = re.findall(r"\]\((\.\./\.\./[^)#\s]+)\)", text)
    assert targets, "spec cites no repository files at all"
    for rel in targets:
        assert (spec.parent / rel).resolve().is_file(), f"broken link: {rel}"


GUIDE_PATH = REPO_ROOT / "docs" / "agent-mcp-operator-guide.md"
DEPLOY_WINDOW_ANCHOR = "deploy-window-mcp-disconnect"


def _anchor_exists(doc: Path, anchor: str) -> bool:
    """True when ``doc`` has an explicit HTML anchor or a heading with that slug."""
    text = doc.read_text(encoding="utf-8")
    if re.search(rf'<a\s+(?:id|name)="{re.escape(anchor)}"\s*>', text):
        return True
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*$", text, flags=re.MULTILINE):
        slug = re.sub(r"[^\w\s-]", "", heading.lower()).strip().replace(" ", "-")
        if slug == anchor:
            return True
    return False


def test_operator_guide_names_the_deploy_window_mcp_failure():
    # #1585 AC-1: a session that started in the release window can keep the hub
    # MCP disconnected. The guide names the symptom, the cause and the way out.
    text = GUIDE_PATH.read_text(encoding="utf-8")
    assert _anchor_exists(GUIDE_PATH, DEPLOY_WINDOW_ANCHOR)
    troubleshooting = text[text.index("## 7. Troubleshooting") :]
    troubleshooting = troubleshooting[: troubleshooting.index("\n---")]
    rows = [
        line
        for line in troubleshooting.splitlines()
        if line.startswith("|") and f'id="{DEPLOY_WINDOW_ANCHOR}"' in line
    ]
    assert len(rows) == 1, "one Troubleshooting row must carry the anchor"
    row = rows[0]
    for needle in (
        "/healthz",
        "200",
        "Claude Code",
        "/mcp",
        "REST",
        "HAIPLANE_HUB_TOKEN",
    ):
        assert needle in row, f"deploy-window row lacks {needle!r}"
    assert "деплой" in row.lower()
    # the way out goes in order: healthz, then /mcp; REST only while MCP is down
    assert row.index("/healthz") < row.index("/mcp")
    assert "пока MCP недоступен" in row
    # /mcp is a Claude Code command, not a universal one
    assert "Claude Code" in row[: row.index("/mcp")]
    # no promise that the client recovers by itself, no window as a guarantee
    lowered = row.lower()
    for forbidden in ("переподключится сам", "гарантир", "всегда переподключ"):
        assert forbidden not in lowered
    assert "может остаться" in lowered
    # a variable name only, never a secret value
    assert not re.search(r"Bearer\s+[A-Za-z0-9_\-]{16,}", row)


def test_session_restart_spec_links_the_deploy_window_row():
    # #1585 AC-2: the spec points at the guide row. Both the path and the anchor
    # must exist: a fragment is not checked by the generic link test.
    spec = REPO_ROOT / "docs" / "specs" / "mcp-session-restart.md"
    text = spec.read_text(encoding="utf-8")
    links = re.findall(r"\]\(([^)\s#]*agent-mcp-operator-guide\.md)#([^)\s]+)\)", text)
    matching = [(path, frag) for path, frag in links if frag == DEPLOY_WINDOW_ANCHOR]
    assert matching, "spec does not link the deploy-window row by anchor"
    for path, frag in matching:
        target = (spec.parent / path).resolve()
        assert target.is_file(), f"broken link: {path}"
        assert _anchor_exists(target, frag), f"missing anchor {frag} in {path}"
    assert "stateful" in text, "spec must say its 404 scenario is the old transport"


def test_operator_guide_matches_stateless_mcp():
    # #1585 AC-3: hub/mcp_server.py runs stateless_http=True, so the guide must
    # not ask for Mcp-Session-Id or call its absence an error (#1364).
    server = (REPO_ROOT / "hub" / "mcp_server.py").read_text(encoding="utf-8")
    assert "stateless_http=True" in server
    text = GUIDE_PATH.read_text(encoding="utf-8")
    assert "stateless" in text
    assert "не требует" in text
    for stale in (
        '-H "Mcp-Session-Id',
        "SESSION=",
        "нужен `Mcp-Session-Id`",
        "есть **`Mcp-Session-Id`**",
        "передайте во все follow-up",
        "**с тем же** `Mcp-Session-Id`",
        "без `Mcp-Session-Id`",
        "Передайте `Mcp-Session-Id`",
        "Добавьте `Mcp-Session-Id`",
    ):
        assert stale not in text, f"guide still requires a session id: {stale}"
    onboarding = (REPO_ROOT / "docs" / "agent-onboarding.md").read_text(
        encoding="utf-8"
    )
    assert "возвращает `Mcp-Session-Id`" not in onboarding
    for doc in (text, onboarding):
        for line in doc.splitlines():
            if "Mcp-Session-Id" in line:
                assert re.search(r"не (требу|нужен|передаёт|выда)", line), line
