"""Главные кнопки карточки задачи в настоящем браузере (#1996).

Каждый сценарий — реальный клик по странице хаба, который работает в режиме CSRF
require, и проверка состояния в базе отдельным соединением. Серверные тесты не
видят ни JS, ни HTMX, ни того, как браузер отправляет форму, поэтому дефекты
«вернуть в работу» и вложенной формы дошли до прода мимо них.

Задачи у сценариев независимые: стенд один на сессию, а общего состояния между
тестами нет.

Отказ сервиса проверяется браузером: TestClient #1995 видит HTML и charset, но
не то, что человек получает на экране после клика.
"""

from __future__ import annotations

from typing import Any

from tests.gui.conftest import Stand

STALE = "Форма устарела"
REASON = "Исполнитель пропал: заберите сдачу, пожалуйста — ёжик не ответил"


def _open(page: Any, stand: Stand, task_id: int) -> None:
    page.goto(f"{stand.url}/tasks/{task_id}")
    page.wait_for_load_state("domcontentloaded")


def _no_stale_form(page: Any) -> None:
    assert STALE not in page.locator("body").inner_text(), (
        "хаб отклонил форму как устаревшую"
    )


def test_htmx_is_loaded_on_the_card(page: Any, stand: Stand) -> None:
    task_id = stand.seed_review_task(title="HTMX загружен")
    _open(page, stand, task_id)
    try:
        page.wait_for_function("typeof window.htmx === 'object'")
    except Exception as exc:  # noqa: BLE001 - Playwright TimeoutError
        raise AssertionError(
            "HTMX не загрузился: unpkg недоступен (страница грузит "
            "https://unpkg.com/htmx.org@2.0.4, как прод)"
        ) from exc


def test_return_to_work_from_review_with_cyrillic_reason(
    page: Any, stand: Stand
) -> None:
    task_id = stand.seed_review_task(title="вернуть из review")
    _open(page, stand, task_id)

    page.get_by_role("link", name="К действию").click()
    page.locator("#task-return-reason").fill(REASON)
    page.get_by_role("button", name="Вернуть в работу").click()

    stand.wait_for(
        lambda: stand.status_of(task_id) == "open",
        what=f"задача #{task_id} вернулась в open",
        seconds=10,
    )
    _no_stale_form(page)
    row = stand.task(task_id)
    assert row["claimed_by"] in (None, "")
    notes = [u for u in stand.updates_of(task_id) if u["kind"] == "decision"]
    assert notes and REASON in notes[-1]["content"]
    assert notes[-1]["agent"] == "probe-owner"


def test_launch_on_the_k8_slot_orders_a_pending_run(page: Any, stand: Stand) -> None:
    task_id = stand.seed_slot_world(title="запуск на k8")
    dialogs: list[str] = []

    def accept(dialog: Any) -> None:
        dialogs.append(dialog.message)
        dialog.accept()

    page.on("dialog", accept)
    _open(page, stand, task_id)
    page.get_by_role("button", name="Запустить на k8").click()

    orders = stand.wait_for(
        lambda: stand.query(
            "SELECT host, task_id, state FROM executor_orders WHERE task_id=?",
            (task_id,),
        ),
        what=f"заказ на задачу #{task_id}",
        seconds=15,
    )
    assert dialogs and "k8" in dialogs[0] and f"#{task_id}" in dialogs[0]
    assert orders == [{"host": "k8", "task_id": task_id, "state": "pending"}]
    page.wait_for_url(f"{stand.url}/tasks/{task_id}")
    _no_stale_form(page)
    assert "Заказ #" in page.locator("body").inner_text()


def test_verdict_approved_with_comment(page: Any, stand: Stand) -> None:
    task_id = stand.seed_review_task(title="вердикт approve")
    _open(page, stand, task_id)

    page.locator("#review-comments").fill("Принимаю: всё проверено глазами")
    page.get_by_role("button", name="Approve", exact=True).click()

    stand.wait_for(
        lambda: stand.task(task_id)["review_verdict"] == "approved",
        what=f"вердикт approved по задаче #{task_id}",
        seconds=10,
    )
    _no_stale_form(page)
    row = stand.task(task_id)
    assert row["review_verdict_generation"] == 1
    review = [u for u in stand.updates_of(task_id) if u["kind"] == "review"]
    assert review and "APPROVED" in review[-1]["content"]
    assert "Принимаю: всё проверено глазами" in review[-1]["content"]


def test_verdict_changes_requested_with_comment(page: Any, stand: Stand) -> None:
    task_id = stand.seed_review_task(title="вердикт changes_requested")
    _open(page, stand, task_id)

    page.locator("#review-comments").fill("Исправьте тест возврата")
    page.get_by_role("button", name="Request Changes").click()

    stand.wait_for(
        lambda: stand.task(task_id)["review_verdict"] == "changes_requested",
        what=f"вердикт changes_requested по задаче #{task_id}",
        seconds=10,
    )
    _no_stale_form(page)
    row = stand.task(task_id)
    assert row["review_cycle"] == 1
    review = [u for u in stand.updates_of(task_id) if u["kind"] == "review"]
    assert review and "CHANGES_REQUESTED" in review[-1]["content"]
    assert "Исправьте тест возврата" in review[-1]["content"]


def test_draft_is_approved_to_backlog(page: Any, stand: Stand) -> None:
    task_id = stand.seed_ready_draft(title="одобрение черновика")
    assert stand.status_of(task_id) == "draft"
    _open(page, stand, task_id)

    page.get_by_role("button", name="Approve to Backlog").click()

    stand.wait_for(
        lambda: stand.status_of(task_id) == "open",
        what=f"черновик #{task_id} одобрен",
        seconds=10,
    )
    _no_stale_form(page)


def _refresh_requests(page: Any, task_id: int) -> list[str]:
    seen: list[str] = []
    page.on(
        "request",
        lambda request: (
            seen.append(request.url)
            if request.method == "GET"
            and request.url.rstrip("/").endswith(f"/tasks/{task_id}")
            and request.headers.get("hx-request")
            else None
        ),
    )
    return seen


def test_autorefresh_updates_a_clean_card(page: Any, stand: Stand) -> None:
    task_id = stand.seed_task(title="чистая карточка", status="running")
    page.clock.install()
    seen = _refresh_requests(page, task_id)
    _open(page, stand, task_id)
    page.wait_for_function("typeof window.htmx === 'object'")
    assert page.evaluate("window.hubHasUnsavedInput()") is False

    stand.run(_rename(task_id, "Название после автообновления"))
    page.clock.fast_forward(16_000)

    page.get_by_text("Название после автообновления").first.wait_for(timeout=10_000)
    assert seen, "автообновление не сходило за карточкой"


def test_autorefresh_is_blocked_while_typing_and_the_button_still_works(
    page: Any, stand: Stand
) -> None:
    task_id = stand.seed_task(title="карточка с вводом", status="running")
    page.clock.install()
    seen = _refresh_requests(page, task_id)
    _open(page, stand, task_id)
    page.wait_for_function("typeof window.htmx === 'object'")

    field = page.locator("#task-return-reason")
    field.fill(REASON)
    assert page.evaluate("window.hubHasUnsavedInput()") is True

    stand.run(_rename(task_id, "Название, которого нельзя увидеть при вводе"))
    page.clock.fast_forward(16_000)
    page.clock.run_for(1_000)

    assert seen == [], "автообновление сходило за карточкой при недописанном вводе"
    assert field.input_value() == REASON
    assert "нельзя увидеть при вводе" not in page.locator("body").inner_text()

    # После блокировки кнопка срабатывает и берёт набранное.
    page.get_by_role("button", name="Вернуть в работу").click()
    stand.wait_for(
        lambda: stand.status_of(task_id) == "open",
        what=f"задача #{task_id} вернулась в open из running",
        seconds=10,
    )
    _no_stale_form(page)
    notes = [u for u in stand.updates_of(task_id) if u["kind"] == "decision"]
    assert notes and REASON in notes[-1]["content"]


def test_a_service_refusal_is_shown_in_russian(page: Any, stand: Stand) -> None:
    task_id = stand.seed_review_task(title="отказ сервиса")
    _open(page, stand, task_id)

    page.get_by_role("link", name="К действию").click()
    page.locator("#task-return-reason").fill(REASON)
    # Статус меняется за спиной открытой страницы: хаб откажет при отправке.
    stand.run(_force_status(task_id, "open"))
    page.get_by_role("button", name="Вернуть в работу").click()

    alert = page.locator('[role="alert"]').first
    alert.wait_for(timeout=10_000)
    text = page.locator("body").inner_text()
    assert "Хаб отклонил запрос" in text, text[:400]
    assert "current status: open" in text, text[:400]
    assert '{"detail"' not in text, "браузер получил голый JSON"
    assert "Ð" not in text and "Ñ" not in text, "кракозябры вместо кириллицы"
    _no_stale_form(page)
    # Задача уже open: карточка честно показывает новый статус, формы возврата
    # на ней больше нет, поэтому сохранность ввода здесь не проверяется — её
    # держат серверные тесты #1995 (test_refused_card_forms_keep_the_typed_text).
    assert stand.status_of(task_id) == "open"


def _force_status(task_id: int, status: str) -> Any:
    async def work(conn: Any) -> None:
        await conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
        )
        await conn.commit()

    return work


def _rename(task_id: int, title: str) -> Any:
    from hub import repository as repo

    async def work(conn: Any) -> None:
        await repo.update_task(conn, task_id, title=title)

    return work
