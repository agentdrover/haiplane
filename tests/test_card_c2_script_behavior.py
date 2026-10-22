"""Скрипт раскрытия блоков карточки C2 на модели DOM (#1812, возврат gen1)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "tests" / "card_c2_script_probe.js"
SCRIPT = ROOT / "hub" / "templates" / "partials" / "card_c2_script.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")


def _probe() -> dict:
    done = subprocess.run(
        ["node", str(PROBE), str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_open_state_survives_the_htmx_swap():
    got = _probe()
    assert got["narrow_initial"] is False
    assert got["manual_open_survives_swap"] is True


def test_width_is_only_the_initial_value_and_manual_close_wins():
    got = _probe()
    assert got["wide_initial"] is True
    # Закрыли на широком экране, замена пришла раньше события toggle.
    assert got["wide_manual_close_survives_swap"] is False


def test_fragment_opens_on_load_but_does_not_override_a_close_on_swap():
    got = _probe()
    assert got["hash_opens_on_load"] is True
    assert got["hash_does_not_override_close_on_swap"] is False


def test_feedback_open_is_shown_on_load_then_respects_a_close():
    got = _probe()
    assert got["feedback_open_on_load"] is True
    assert got["feedback_close_survives_swap"] is False


def test_unavailable_storage_does_not_break_the_page():
    assert _probe()["no_storage_ok"] is True
