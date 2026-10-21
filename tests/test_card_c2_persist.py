"""Раскрытие блоков карточки C2 переживает обновление HTMX (#1812, возврат gen1)."""

from __future__ import annotations

import re

from tests.test_decision_brief import _task_in


async def _page(client, db) -> str:
    task_id = await _task_in(db, "running")
    for i in range(5):
        await client.post(
            f"/api/tasks/{task_id}/updates",
            json={"agent": "dev", "kind": "status", "content": f"запись {i}"},
        )
    return (await client.get(f"/tasks/{task_id}")).text


async def test_every_togglable_block_has_a_stable_id(client, db):
    page = await _page(client, db)
    for ident in ("card-details", "c2-checks", "c2-orig", "task-feed-all"):
        assert re.search(rf'<details[^>]*\bid="{ident}"', page), ident


async def test_script_keeps_state_by_id_and_restores_after_swap(client, db):
    page = await _page(client, db)
    script = page[page.rindex("<script>") : page.rindex("</script>")]
    assert "sessionStorage" in script
    # Хранилище недоступно (приватный режим): страница не падает.
    assert re.search(r"try\s*\{[^}]*sessionStorage", script)
    assert "htmx:afterSwap" in script
    # Состояние снимается до замены: toggle асинхронен и мог не успеть.
    assert "htmx:beforeSwap" in script
    # Фрагмент адреса не перебивает ручное закрытие при замене.
    assert "data-c2-forced" not in script
    assert re.search(r"restore\(false\)", script)
    assert "toggle" in script
    # Ширина - только начальное значение: без записанного состояния.
    assert "=== undefined" in script or "== null" in script
    # «К действию» не отменяет переход: фрагмент пишется в адрес.
    assert "preventDefault" not in script
