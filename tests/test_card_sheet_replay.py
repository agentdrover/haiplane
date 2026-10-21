"""Автообновление и защита листа от устаревшего экрана (#1813, AC-2)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient

from tests.card_sheet_support import SHEET_HEADERS, inside, sheet_form
from tests.test_csrf_templates import parse_html
from tests.web_support import _web_task_in_review

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "tests" / "card_sheet_probe.js"
SCRIPT = ROOT / "hub" / "templates" / "partials" / "card_sheets_script.html"


async def test_sheet_survives_refresh_and_refuses_stale_generation(
    client: AsyncClient, db
):
    task_id = await _web_task_in_review(client)
    page = (await client.get(f"/tasks/{task_id}")).text
    root = parse_html(page)

    # 1. Листы и ввод вне замещаемого фрагмента; сам фрагмент без полей ввода.
    live = next(n for n in root.walk() if n.attrs.get("id") == "c2-live")
    assert live.attrs["hx-select"] == "#c2-live"
    assert live.attrs["hx-target"] == "this"
    assert "every 15s" in live.attrs["hx-trigger"]
    assert not [n for n in live.walk() if n.tag in {"textarea", "input", "select"}]
    form, data = sheet_form(page, "sh-cr")
    assert not inside(root, form, "c2-live")
    holder = next(n for n in root.walk() if n.attrs.get("id") == "c2-sheets")
    for key in ("status", "submission-generation", "statement-generation"):
        assert holder.attrs[f"data-built-{key}"] == live.attrs[f"data-{key}"]
    # 2. Раньше 15-секундный триггер заменял всю .main: теперь так не делает никто.
    for node in root.walk():
        trigger = node.attrs.get("hx-trigger", "")
        if "every 15s" in trigger:
            assert node.attrs.get("hx-target") != ".main", node.attrs

    # 3. Исполнитель пересдал: живое поколение выросло, лист построен на старом.
    stale = data["expected_generation"]
    from hub import repository as repo

    await db.execute(
        "UPDATE tasks SET submission_generation = submission_generation + 1 "
        "WHERE id = ?",
        (task_id,),
    )
    await db.commit()
    live_gen = str((await repo.get_task(db, task_id))["submission_generation"])
    assert live_gen != stale

    refused = await client.post(
        form.attrs["action"],
        data={**data, "verdict": "changes_requested", "comments": "мой текст"},
        headers=SHEET_HEADERS,
    )
    assert refused.status_code == 409
    assert 'class="c2-sheet-note"' in refused.text
    assert "поколение" in refused.text and live_gen in refused.text
    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["latest_review"] is None

    # Новая страница несёт живое поколение и в маркере, и в новом листе.
    fresh = (await client.get(f"/tasks/{task_id}")).text
    fresh_root = parse_html(fresh)
    marker = next(n for n in fresh_root.walk() if n.attrs.get("id") == "c2-live")
    assert marker.attrs["data-submission-generation"] == live_gen
    _, fresh_data = sheet_form(fresh, "sh-cr")
    assert fresh_data["expected_generation"] == live_gen


@pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")
def test_sheet_script_keeps_typed_text_and_warns_about_a_new_submission():
    done = subprocess.run(
        ["node", str(PROBE), str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got["typed_text_after_swap"] == "мой текст"
    assert got["warning_hidden_before"] is True
    assert got["warning_shown_after_generation_change"] is True
    assert got["submit_stays_enabled"] is True
    assert got["escape_closes_and_returns_focus"] is True
    # Панель ушла вперёд, а листы остались старыми: перезагрузка только без листа.
    assert got["no_reload_while_open"] is True
    assert got["reload_after_close_when_panel_moved_on"] is True
    assert got["storage_free"] is True
