"""The hub's own workflow keeps its env contract through the rename (Wave 4).

``.github/workflows/ci.yml`` is read as TEXT on purpose: the contract under
test is literal — which secret expressions the workflow evaluates and which
env key names its shell bodies read. The deploy-callback ``run:`` reads
``$OPENCLAW_HUB_URL``, so the env KEY names must stay legacy even after the
secret VALUES move to ``HAIPLANE_* || OPENCLAW_*``. A renamed key with an
unchanged shell body keeps CI green and silently stops deploy reporting —
exactly the failure this file exists to make loud.
"""

from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

URL_FALLBACK = "${{ secrets.HAIPLANE_HUB_URL || secrets.OPENCLAW_HUB_URL }}"
TOKEN_FALLBACK = "${{ secrets.HAIPLANE_HUB_CI_TOKEN || secrets.OPENCLAW_HUB_CI_TOKEN }}"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_deploy_callback_keeps_legacy_env_keys() -> None:
    text = _text()
    # The env KEYS stay legacy — the shell body below reads them by name.
    assert f"OPENCLAW_HUB_URL: {URL_FALLBACK}" in text
    assert f"OPENCLAW_HUB_CI_TOKEN: {TOKEN_FALLBACK}" in text
    # The values contain the new-name-first fallback.
    assert "secrets.HAIPLANE_HUB_URL ||" in text
    assert "secrets.HAIPLANE_HUB_CI_TOKEN ||" in text
    # The run: body still reads the legacy env names.
    assert '"$OPENCLAW_HUB_URL/api/deploys"' in text
    assert "Bearer $OPENCLAW_HUB_CI_TOKEN" in text


def test_report_and_audit_use_fallback_expressions() -> None:
    text = _text()
    # Report step inputs (the hub-ci-report composite action).
    assert f"hub-url: {URL_FALLBACK}" in text
    assert f"hub-token: {TOKEN_FALLBACK}" in text
    # No bare legacy-only secret expression may survive anywhere: it would
    # keep one consumer pinned to the old secret after the others moved.
    assert "${{ secrets.OPENCLAW_HUB_URL }}" not in text
    assert "${{ secrets.OPENCLAW_HUB_CI_TOKEN }}" not in text
