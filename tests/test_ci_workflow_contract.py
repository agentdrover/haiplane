"""The hub's own workflow keeps its env contract (Wave 5: canonical only).

``.github/workflows/ci.yml`` is read as TEXT on purpose: the contract under
test is literal — which secret expressions the workflow evaluates and which
env key names its shell bodies read. The deploy-callback ``run:`` reads
``$HAIPLANE_HUB_URL``, so the env KEY names and the ``run:`` body must move
together. A renamed key with an unchanged shell body keeps CI green and
silently stops deploy reporting — exactly the failure this file exists to
make loud.
"""

from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

URL_SECRET = "${{ secrets.HAIPLANE_HUB_URL }}"
TOKEN_SECRET = "${{ secrets.HAIPLANE_HUB_CI_TOKEN }}"

# Собрано конкатенацией: контракт Волны 5 в том, что этих имён в файле НЕТ,
# а страж (tests/test_no_legacy_name.py) не должен ловить сам контракт.
LEGACY_PREFIX = "OPEN" + "CLAW" + "_"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_deploy_callback_env_keys_match_run_body() -> None:
    text = _text()
    # The env KEYS are canonical — the shell bodies below read them by name.
    assert f"HAIPLANE_HUB_URL: {URL_SECRET}" in text
    assert f"HAIPLANE_HUB_CI_TOKEN: {TOKEN_SECRET}" in text
    # The run: body reads the same canonical env names.
    assert '"$HAIPLANE_HUB_URL/api/deploys"' in text
    assert "Bearer $HAIPLANE_HUB_CI_TOKEN" in text


def test_report_and_audit_use_canonical_secrets() -> None:
    text = _text()
    # Report step inputs (the hub-ci-report composite action).
    assert f"hub-url: {URL_SECRET}" in text
    assert f"hub-token: {TOKEN_SECRET}" in text


def test_no_legacy_names_anywhere() -> None:
    assert LEGACY_PREFIX not in _text().upper(), (
        "Wave 5: the workflow must reference no legacy secret or env name"
    )


def test_manual_runs_buy_checks_not_a_deploy() -> None:
    """#1196: ручной запуск не должен становиться дорогой к выкату.

    Триггер ``workflow_dispatch`` добавлен, чтобы получить прогон на УЖЕ
    существующем коммите (наблюдённый тупик #1185). Джоба ``deploy`` при этом
    обязана остаться недостижимой: её условие требует push-события, а
    ``workflow_dispatch`` им не является. Условие читается ТЕКСТОМ по той же
    причине, что и остальной файл: расширение ``if`` до ручного события
    оставит CI зелёным и молча превратит проверку в выкат.
    """
    text = _text()
    assert "  workflow_dispatch:\n" in text, (
        "триггер ручного запуска должен быть объявлен — без него прогон на "
        "существующем коммите родить нечем"
    )
    assert (
        "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" in text
    ), (
        "deploy обязан требовать push в main: ручной запуск покупает проверки, "
        "а не выкат"
    )
