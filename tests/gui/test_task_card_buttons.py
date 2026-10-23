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

from datetime import datetime, timedelta
from typing import Any

from playwright.sync_api import expect

from tests.gui.conftest import Stand

STALE = "Форма устарела"
REASON = "Исполнитель пропал: заберите сдачу, пожалуйста — ёжик не ответил"


def _open(page: Any, stand: Stand, task_id: int) -> None:
    page.goto(f"{stand.url}/tasks/{task_id}")
    page.wait_for_load_state("domcontentloaded")


def _frozen_clock(page: Any) -> None:
    """Часы страницы стоят, пока тест сам их не двинет.

    Без паузы fake-часы Playwright идут в реальном времени: на медленном CI
    подготовка (открытие листа) могла занять 15 с, и разрешённый запрос чистой
    карточки попадал в seen, а тест обвинял защиту ввода."""
    start = datetime(2026, 1, 1, 12, 0, 0)
    page.clock.install(time=start)
    page.clock.pause_at(start + timedelta(hours=1))


def _set_details(page: Any, want_open: bool) -> None:
    """«Подробности» на широком экране уже раскрыты, а панель действий при этом
    скрыта: приводим блок в нужное состояние, а не переключаем вслепую."""
    page.wait_for_function("document.getElementById('card-details') !== null")
    if page.evaluate("document.getElementById('card-details').open") != want_open:
        page.locator("#card-details > summary").click()


def _open_details(page: Any) -> None:
    """Прежние формы лежат в «Подробностях» (#1812): раскрыть и отправлять их."""
    _set_details(page, True)


def _open_return_sheet(page: Any) -> None:
    """Путь MC-4 (#1813): «Другие действия» -> лист «Вернуть в работу»."""
    # Нижняя панель прячется, пока на экране видна «К действию» (card_sheets_script):
    # низкое окно и прокрутка вниз выводят справку из вида, и панель появляется.
    page.set_viewport_size({"width": 1400, "height": 420})
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    page.wait_for_selector(".c2-bar:not(.c2-bar--covered)", timeout=10_000)
    page.locator("#c2-more > summary").click()
    page.locator('a[data-sheet="sh-return"]').click()
    page.locator("#sh-return-reason").wait_for(state="visible")


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
            "HTMX не загрузился: подмена unpkg на tests/gui/vendor не сработала"
        ) from exc
    assert page.context.htmx_served, "HTMX пришёл не из tests/gui/vendor"


def test_return_to_work_from_review_with_cyrillic_reason(
    page: Any, stand: Stand
) -> None:
    """Сервисный путь: прежняя форма в «Подробностях»."""
    task_id = stand.seed_review_task(title="вернуть из review")
    _open(page, stand, task_id)

    _open_details(page)
    page.locator("#task-return-reason").fill(REASON)
    page.locator("#card-details").get_by_role("button", name="Вернуть в работу").click()

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


def test_return_to_work_through_the_mc4_sheet(page: Any, stand: Stand) -> None:
    """Путь MC-4: «Другие действия» -> лист, кириллическая причина доходит до базы."""
    task_id = stand.seed_review_task(title="вернуть листом")
    _open(page, stand, task_id)

    _open_return_sheet(page)
    page.locator("#sh-return-reason").fill(REASON)
    page.locator('#sh-return button[type="submit"]').click()

    stand.wait_for(
        lambda: stand.status_of(task_id) == "open",
        what=f"задача #{task_id} вернулась в open листом",
        seconds=10,
    )
    # Настоящий успех листа: форма помечена отправленной (либо карточка уже
    # перечитана), и записанный ввод больше не считается недописанным.
    page.wait_for_function(
        "(() => { const f = document.querySelector('#sh-return form');"
        " return !f || f.getAttribute('data-sent') === 'true'; })()"
    )
    assert page.evaluate("window.hubHasUnsavedInput()") is False
    expect(page.locator("#c2-live")).to_have_attribute("data-status", "open")
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
    _frozen_clock(page)
    seen = _refresh_requests(page, task_id)
    _open(page, stand, task_id)
    page.wait_for_function("typeof window.htmx === 'object'")
    assert page.evaluate("window.hubHasUnsavedInput()") is False

    stand.run(_rename(task_id, "Название после автообновления"))
    page.clock.fast_forward(16_000)

    # Новое название приходит в свёрнутый «Исходное название» внутри #c2-live (MC-4):
    # видимости не требуем, проверяем текст.
    expect(page.locator("#c2-live .c2-orig-title")).to_have_text(
        "Название после автообновления", timeout=10_000
    )
    assert seen, "автообновление не сходило за карточкой"


def test_autorefresh_is_blocked_while_typing_and_the_button_still_works(
    page: Any, stand: Stand
) -> None:
    task_id = stand.seed_task(title="карточка с вводом", status="running")
    _frozen_clock(page)
    seen = _refresh_requests(page, task_id)
    _open(page, stand, task_id)
    page.wait_for_function("typeof window.htmx === 'object'")

    _open_details(page)
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
    page.locator("#card-details").get_by_role("button", name="Вернуть в работу").click()
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

    _open_details(page)
    page.locator("#task-return-reason").fill(REASON)
    # Статус меняется за спиной открытой страницы: хаб откажет при отправке.
    stand.run(_force_status(task_id, "open"))
    page.locator("#card-details").get_by_role("button", name="Вернуть в работу").click()

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


def test_autorefresh_is_blocked_while_typing_in_the_mc4_sheet(
    page: Any, stand: Stand
) -> None:
    """Тот же контракт #1996 для поля листа MC-4: ввод не затирается и не опрашивается."""
    task_id = stand.seed_task(title="карточка с вводом в листе", status="running")
    _frozen_clock(page)
    seen = _refresh_requests(page, task_id)
    _open(page, stand, task_id)
    page.wait_for_function("typeof window.htmx === 'object'")

    _open_return_sheet(page)
    field = page.locator("#sh-return-reason")
    field.fill(REASON)
    assert page.evaluate("window.hubHasUnsavedInput()") is True

    stand.run(_rename(task_id, "Название, которого нельзя увидеть при вводе в листе"))
    page.clock.fast_forward(16_000)
    page.clock.run_for(1_000)

    assert seen == [], "автообновление сходило за карточкой при недописанном вводе"
    assert field.input_value() == REASON

    # После ожидания лист отправляет набранную кириллицу.
    page.locator('#sh-return button[type="submit"]').click()
    stand.wait_for(
        lambda: stand.status_of(task_id) == "open",
        what=f"задача #{task_id} вернулась в open листом из running",
        seconds=10,
    )
    notes = [u for u in stand.updates_of(task_id) if u["kind"] == "decision"]
    assert notes and REASON in notes[-1]["content"]


def test_the_sheet_guard_refuses_a_stale_screen(page: Any, stand: Stand) -> None:
    """Гард листа (card_sheet.py): статус сменился за спиной - отказ на листе, ввод цел."""
    task_id = stand.seed_review_task(title="гард листа")
    _open(page, stand, task_id)

    _open_return_sheet(page)
    page.locator("#sh-return-reason").fill(REASON)
    stand.run(_force_status(task_id, "open"))
    page.locator('#sh-return button[type="submit"]').click()

    alert = page.locator('#sh-return .c2-sheet-result [role="alert"]')
    alert.wait_for(timeout=10_000)
    text = alert.inner_text()
    assert "Состояние задачи изменилось" in text, text
    assert "open" in text, text
    assert page.locator("#sh-return-reason").input_value() == REASON
    assert stand.status_of(task_id) == "open"
    notes = [u for u in stand.updates_of(task_id) if u["kind"] == "decision"]
    assert not notes, "отказ не должен оставить запись о возврате"


def test_refresh_after_a_sheet_action_is_allowed_despite_typed_input(
    page: Any, stand: Stand
) -> None:
    """c2-refresh (после успешного листа, #1813) не блокируется защитой ввода."""
    task_id = stand.seed_task(title="обновление после листа", status="running")
    seen = _refresh_requests(page, task_id)
    _open(page, stand, task_id)
    page.wait_for_function("typeof window.htmx === 'object'")

    _open_return_sheet(page)
    field = page.locator("#sh-return-reason")
    field.fill(REASON)
    assert page.evaluate("window.hubHasUnsavedInput()") is True

    stand.run(_rename(task_id, "Название после действия листа"))
    page.evaluate(
        "window.htmx.trigger(document.getElementById('c2-live'), 'c2-refresh')"
    )

    expect(page.locator("#c2-live .c2-orig-title")).to_have_text(
        "Название после действия листа", timeout=10_000
    )
    assert seen, "обновление после действия листа не сходило за карточкой"
    assert field.input_value() == REASON


def test_a_late_autorefresh_answer_does_not_replace_typed_input(
    page: Any, stand: Stand
) -> None:
    """Запрос ушёл на чистой карточке, ответ придёт после ввода: замены нет (beforeSwap)."""
    task_id = stand.seed_task(title="поздний ответ", status="running")
    _frozen_clock(page)
    held: list[Any] = []

    def hold(route: Any) -> None:
        if route.request.headers.get("hx-request"):
            held.append(route)
        else:
            route.continue_()

    page.route(f"**/tasks/{task_id}", hold)
    _open(page, stand, task_id)
    page.wait_for_function("typeof window.htmx === 'object'")
    page.evaluate(
        "document.addEventListener('htmx:afterRequest', () => { window.lateDone = true; })"
    )
    stand.run(_rename(task_id, "Название из позднего ответа"))
    page.clock.fast_forward(16_000)
    for _ in range(40):
        if held:
            break
        page.wait_for_timeout(100)
    assert held, "автообновление чистой карточки не ушло за ответом"

    _open_return_sheet(page)
    field = page.locator("#sh-return-reason")
    field.fill(REASON)
    held[0].continue_()
    # Ответ обработан htmx (beforeSwap уже отработал), только потом проверяем.
    page.wait_for_function("window.lateDone === true", timeout=10_000)

    assert field.input_value() == REASON
    assert "Название из позднего ответа" not in page.locator("#c2-live").inner_html()


def test_manual_refresh_still_replaces_the_card_over_typed_input(
    page: Any, stand: Stand
) -> None:
    """Ручной Refresh Status - клик, защита ввода его не гасит."""
    task_id = stand.seed_task(title="ручное обновление", status="running")
    _open(page, stand, task_id)
    page.wait_for_function("typeof window.htmx === 'object'")
    _open_details(page)
    page.locator("#task-return-reason").fill(REASON)
    stand.run(_rename(task_id, "Название после ручного обновления"))
    page.get_by_role("button", name="Refresh Status").click()
    expect(page.locator("h1.task-hero-title")).to_contain_text(
        "Название после ручного обновления", timeout=10_000
    )


def test_input_typed_while_the_sheet_post_is_in_flight_is_not_lost(
    page: Any, stand: Stand
) -> None:
    """#2035 gen3: на сервер ушло A, пока ответ шёл, человек набрал B - B не «отправлено»."""
    task_id = stand.seed_task(title="правка во время отправки", status="running")
    _open(page, stand, task_id)
    page.wait_for_function("typeof window.htmx === 'object'")
    held: list[Any] = []

    def hold(route: Any) -> None:
        if route.request.method == "POST":
            held.append(route)
        else:
            route.continue_()

    page.route(f"**/tasks/{task_id}/web-return-to-work", hold)
    _open_return_sheet(page)
    field = page.locator("#sh-return-reason")
    field.fill("Первая версия причины")
    page.evaluate("window.marker = 'alive'")
    page.locator('#sh-return button[type="submit"]').click()
    for _ in range(40):
        if held:
            break
        page.wait_for_timeout(100)
    assert held, "отправка листа не ушла"

    field.fill(REASON)  # правка, пока ответ задержан
    held[0].continue_()
    stand.wait_for(
        lambda: stand.status_of(task_id) == "open",
        what=f"задача #{task_id} вернулась в open",
        seconds=10,
    )
    notes = [u for u in stand.updates_of(task_id) if u["kind"] == "decision"]
    assert notes and "Первая версия причины" in notes[-1]["content"]
    # Ответ листа обработан браузером и карточка перечитана - только потом проверки.
    page.wait_for_selector(
        "#sh-return [data-c2-done]", state="attached", timeout=10_000
    )
    expect(page.locator("#c2-live")).to_have_attribute(
        "data-status", "open", timeout=10_000
    )

    form = page.locator("#sh-return form")
    assert form.get_attribute("data-sent") != "true", (
        "B ушло бы под меткой отправленного"
    )
    assert page.evaluate("window.hubHasUnsavedInput()") is True
    page.keyboard.press("Escape")  # закрытие листа не должно перезагрузить страницу
    page.wait_for_timeout(1_000)
    assert page.evaluate("window.marker") == "alive", "страница перезагрузилась"
    assert field.input_value() == REASON


def test_typing_into_a_sent_sheet_withdraws_the_sent_mark(
    page: Any, stand: Stand
) -> None:
    """Повторный ввод в отправленный лист снимает метку и блокирует перезагрузку."""
    task_id = stand.seed_task(title="ввод после отправки", status="running")
    _open(page, stand, task_id)
    page.wait_for_function("typeof window.htmx === 'object'")
    held: list[Any] = []

    def hold(route: Any) -> None:
        if route.request.headers.get("hx-request") and route.request.method == "GET":
            held.append(route)  # обновление после листа задержано: страница стоит
        else:
            route.continue_()

    page.route(f"**/tasks/{task_id}", hold)
    _open_return_sheet(page)
    field = page.locator("#sh-return-reason")
    field.fill(REASON)
    page.evaluate("window.marker = 'alive'")
    page.locator('#sh-return button[type="submit"]').click()
    stand.wait_for(
        lambda: stand.status_of(task_id) == "open",
        what=f"задача #{task_id} вернулась в open",
        seconds=10,
    )
    form = page.locator("#sh-return form")
    expect(form).to_have_attribute("data-sent", "true")
    assert page.evaluate("window.hubHasUnsavedInput()") is False

    field.fill("Новый текст после отправки")
    assert form.get_attribute("data-sent") != "true"
    assert page.evaluate("window.hubHasUnsavedInput()") is True
    for _ in range(40):
        if held:
            break
        page.wait_for_timeout(100)
    assert held, "обновление после листа не ушло"
    for route in held:
        route.continue_()
    page.wait_for_function(
        "document.getElementById('c2-live').dataset.status === 'open'"
    )
    page.keyboard.press("Escape")
    page.wait_for_timeout(1_000)
    assert page.evaluate("window.marker") == "alive", "страница перезагрузилась"
    assert field.input_value() == "Новый текст после отправки"


def test_programmatic_edit_after_a_sent_sheet_is_unsaved_input(
    page: Any, stand: Stand
) -> None:
    """#2035 gen5: изменение значения БЕЗ событий после успеха - несохранённый ввод."""
    task_id = stand.seed_task(title="правка без событий", status="running")
    _open(page, stand, task_id)
    page.wait_for_function("typeof window.htmx === 'object'")
    held: list[Any] = []
    posts: list[str] = []

    def hold(route: Any) -> None:
        if route.request.headers.get("hx-request") and route.request.method == "GET":
            held.append(route)  # страница стоит: обновление после листа задержано
        else:
            if route.request.method == "POST":
                posts.append(route.request.post_data or "")
            route.continue_()

    page.route(f"**/tasks/{task_id}", hold)
    page.route(f"**/tasks/{task_id}/web-return-to-work", hold)
    _open_return_sheet(page)
    field = page.locator("#sh-return-reason")
    field.fill(REASON)
    page.evaluate("window.marker = 'alive'")
    page.locator('#sh-return button[type="submit"]').click()
    stand.wait_for(
        lambda: stand.status_of(task_id) == "open",
        what=f"задача #{task_id} вернулась в open",
        seconds=10,
    )
    page.wait_for_selector(
        "#sh-return [data-c2-done]", state="attached", timeout=10_000
    )
    assert page.evaluate("window.hubHasUnsavedInput()") is False
    assert len(posts) == 1

    # Значение меняется скриптом, без input/change.
    page.evaluate(
        "document.getElementById('sh-return-reason').value = 'Правка скриптом'"
    )
    # Повторное открытие листа пересчитывает кнопку по сравнению со снимком.
    # (без Escape: закрытие листа само проверяет ввод и обновило бы признак)
    page.evaluate("window.hubSheets.open('sh-return')")
    expect(page.locator('#sh-return button[type="submit"]')).to_be_visible()
    # Повторную отправку решает сравнение со снимком, а не флаг: пересылаем форму
    # сразу после правки скриптом, ДО любой проверки ввода, которая обновила бы флаг.
    page.evaluate("document.querySelector('#sh-return form').requestSubmit()")
    for _ in range(40):
        if len(posts) == 2:
            break
        page.wait_for_timeout(100)
    assert len(posts) == 2, "повторная отправка после правки не ушла"
    assert page.evaluate("window.hubHasUnsavedInput()") is True
    for route in held:
        route.continue_()


def test_the_confirmed_snapshot_covers_select_checkbox_and_radio(
    page: Any, stand: Stand
) -> None:
    """Сравнение со снимком: text, select, checkbox, radio - любая правка без событий."""
    task_id = stand.seed_task(title="снимок полей", status="running")
    _open(page, stand, task_id)
    page.wait_for_function("typeof window.htmx === 'object'")
    page.evaluate(
        """() => {
        const f = document.createElement('form');
        f.id = 'probe-form';
        f.innerHTML = '<input type="text" name="t" value="">'
          + '<select name="s"><option value="a">a</option><option value="b">b</option></select>'
          + '<input type="checkbox" name="c" value="1">'
          + '<input type="radio" name="r" value="x"><input type="radio" name="r" value="y">';
        document.getElementById('main').appendChild(f);
        f.t.value = 'отправлено'; f.s.value = 'b'; f.c.checked = true; f.r[1].checked = true;
        window.hubConfirmForm(f);
    }"""
    )
    assert page.evaluate("window.hubHasUnsavedInput()") is False
    for change in (
        "f.t.value = 'иное'",
        "f.s.value = 'a'",
        "f.c.checked = false",
        "f.r[0].checked = true",
    ):
        page.evaluate(
            "(c) => { const f = document.getElementById('probe-form');"
            " f.t.value = 'отправлено'; f.s.value = 'b'; f.c.checked = true;"
            " f.r[1].checked = true; eval(c); }",
            change,
        )
        assert page.evaluate("window.hubHasUnsavedInput()") is True, change
        page.evaluate(
            "() => { const f = document.getElementById('probe-form');"
            " f.t.value = 'отправлено'; f.s.value = 'b'; f.c.checked = true;"
            " f.r[1].checked = true; }"
        )
        assert page.evaluate("window.hubHasUnsavedInput()") is False


def test_snapshot_distinguishes_empty_choice_and_changed_values(
    page: Any, stand: Stand
) -> None:
    """Пустой выбор и выбор пустой опции различаются; смена value при том же checked."""
    task_id = stand.seed_task(title="снимок: тонкие случаи", status="running")
    _open(page, stand, task_id)
    page.wait_for_function("typeof window.htmx === 'object'")
    page.evaluate(
        """() => {
        const f = document.createElement('form');
        f.id = 'probe-form';
        f.innerHTML = '<select name="m" multiple><option value="">нет</option>'
          + '<option value="z">z</option></select>'
          + '<input type="radio" name="r" value="a" checked>'
          + '<input type="checkbox" name="c" value="1" checked>';
        document.getElementById('main').appendChild(f);
        for (const o of f.m.options) { o.selected = false; }
        window.hubConfirmForm(f);
    }"""
    )
    assert page.evaluate("window.hubHasUnsavedInput()") is False
    for change in (
        "f.m.options[0].selected = true",
        "f.r.value = 'b'",
        "f.c.value = '2'",
    ):
        page.evaluate(
            "(c) => { const f = document.getElementById('probe-form'); eval(c); }",
            change,
        )
        assert page.evaluate("window.hubHasUnsavedInput()") is True, change
        page.evaluate(
            "() => { const f = document.getElementById('probe-form');"
            " for (const o of f.m.options) { o.selected = false; }"
            " f.r.value = 'a'; f.c.value = '1'; }"
        )
        assert page.evaluate("window.hubHasUnsavedInput()") is False, change


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
