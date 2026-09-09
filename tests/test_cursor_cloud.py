"""Cursor Cloud Agents API client (#756): spec-shaped requests, honest Nones.

The client's whole contract is degradation: no key → no network call; any
error → None + one warning; the request body follows the v1 spec so the
dispatcher (#757) never builds payloads itself.
"""

from __future__ import annotations

import httpx
import pytest

from hub import brand, config
from hub.integrations import cursor_cloud


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setattr(config, "CURSOR_API_KEY", "key-under-test")
    monkeypatch.setattr(config, "CURSOR_API_URL", "https://api.cursor.test")


class _Recorder:
    """Captures the outgoing request and plays back a canned response."""

    def __init__(self, response: httpx.Response | Exception):
        self.response = response
        self.request: httpx.Request | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _patch_transport(monkeypatch, recorder: _Recorder) -> None:
    original_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(recorder.handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


async def test_unconfigured_client_degrades(monkeypatch, client):
    # AC-1 (#756): no key → None without touching the network, and the
    # health surface says so.
    monkeypatch.setattr(config, "CURSOR_API_KEY", "")

    health = (await client.get("/health")).json()
    assert health["cursor_cloud_configured"] is False

    # Only now poison the transport: the ASGI test client above rides the
    # same httpx.AsyncClient, so the tripwire must come after it.
    def _explode(*_a, **_k):
        raise AssertionError("no network call may happen without a key")

    monkeypatch.setattr(httpx.AsyncClient, "request", _explode)

    assert not cursor_cloud.is_configured()
    assert await cursor_cloud.list_models() is None
    assert await cursor_cloud.get_run("bc-x", "run-y") is None
    assert (
        await cursor_cloud.create_review_agent(
            repo_url="https://github.com/o/r",
            starting_ref="task-1/x",
            model_id="grok-4",
            prompt_text="review",
            hub_mcp_url="https://hub/mcp",
            reviewer_token="tok",
        )
        is None
    )


async def test_review_agent_request_matches_spec(monkeypatch, _configured):
    # AC-2 (#756): the body follows the v1 spec — repos/startingRef,
    # model.id, hub MCP with the REVIEWER bearer, no auto-PR.
    recorder = _Recorder(httpx.Response(200, json={"agent": {"id": "bc-1"}}))
    _patch_transport(monkeypatch, recorder)

    result = await cursor_cloud.create_review_agent(
        repo_url="https://github.com/mrPDA/spike",
        starting_ref="task-9/branch",
        model_id="grok-4",
        prompt_text="review per skill v8",
        hub_mcp_url="https://agenthai.ru/mcp",
        reviewer_token="reviewer-token",
    )

    assert result == {"agent": {"id": "bc-1"}}
    assert recorder.request is not None
    assert recorder.request.method == "POST"
    assert str(recorder.request.url) == "https://api.cursor.test/v1/agents"
    assert recorder.request.headers["Authorization"] == "Bearer key-under-test"
    import json

    body = json.loads(recorder.request.content)
    assert body["repos"] == [
        {"url": "https://github.com/mrPDA/spike", "startingRef": "task-9/branch"}
    ]
    assert body["model"] == {"id": "grok-4"}
    assert body["autoCreatePR"] is False
    assert body["workOnCurrentBranch"] is False
    (mcp,) = body["mcpServers"]
    assert mcp["name"] == brand.MCP_SERVER_NAME
    assert mcp["url"] == "https://agenthai.ru/mcp"
    assert mcp["headers"]["Authorization"] == "Bearer reviewer-token"


async def test_api_errors_degrade_to_none(monkeypatch, _configured):
    # AC-3 (#756): HTTP errors, transport failures and junk bodies all
    # collapse to None — never an exception across the boundary.
    for canned in (
        httpx.Response(500, text="boom"),
        httpx.Response(403, json={"error": "nope"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["list", "not", "object"]),
        httpx.ConnectTimeout("slow"),
    ):
        recorder = _Recorder(canned)
        _patch_transport(monkeypatch, recorder)
        assert await cursor_cloud.list_models() is None
        assert await cursor_cloud.get_usage("bc-1", "run-1") is None


async def test_the_incident_body_becomes_a_capacity_refusal(monkeypatch, _configured):
    """Настоящий ответ провайдера доходит до решения о замене (#1182).

    Находка ревью №249, high: механизм замены был проверен, а СИГНАЛ,
    который его включает, — нет. Все AC-тесты подставляли Refusal руками,
    поэтому разбор тела не проверял никто: мутация ``_error_code`` в вечную
    пустую строку оставляла сюит зелёным, а в проде давала код, не
    попадающий в CAPACITY_CODES, — тот самый повтор одной и той же модели,
    против которого задача и заведена.

    Тело здесь — дословно ответ Cursor от 06.09.2026 на #1175.
    """
    from hub.services.steward_shadow import is_capacity_refusal

    recorder = _Recorder(
        httpx.Response(
            400,
            json={
                "error": {
                    "code": "usage_limit_exceeded",
                    "message": (
                        "Usage-based pricing required. Background Agent "
                        "requires at least $2 remaining until your hard limit."
                    ),
                }
            },
        )
    )
    _patch_transport(monkeypatch, recorder)

    created, refusal = await cursor_cloud.create_agent_attempt(
        repo_url="https://github.com/o/r",
        starting_ref="task-1/x",
        model_id="gpt-5.3-codex",
        prompt_text="judge",
        hub_mcp_url="https://agenthai.ru/mcp",
        reviewer_token="t",
    )

    assert created is None
    assert refusal is not None
    assert refusal.status == 400
    assert refusal.code == "usage_limit_exceeded"
    assert refusal.is_transport is False
    # Цепочка целиком: тело → код → решение. Проверять только поле кода
    # значило бы снова остановиться на шаг раньше места, где решают.
    assert is_capacity_refusal(refusal) is True


async def test_an_unnamed_error_is_not_a_capacity_signal(monkeypatch, _configured):
    """Тело без кода не выдумывает код (#762: отсутствие — не значение).

    Три формы, каждая встречается у беты: ошибка строкой, ошибка без поля
    code, нечитаемое тело. Ни одна не должна дать имя, по которому меняют
    судью: незнание — не повод считать модель недоступной.
    """
    from hub.services.steward_shadow import is_capacity_refusal

    for canned in (
        httpx.Response(403, json={"error": "nope"}),
        httpx.Response(400, json={"error": {"message": "no code here"}}),
        httpx.Response(400, text="not json at all"),
    ):
        recorder = _Recorder(canned)
        _patch_transport(monkeypatch, recorder)
        _created, refusal = await cursor_cloud.create_agent_attempt(
            repo_url="https://github.com/o/r",
            starting_ref="task-1/x",
            model_id="gpt-5.3-codex",
            prompt_text="judge",
            hub_mcp_url="https://agenthai.ru/mcp",
            reviewer_token="t",
        )
        assert refusal is not None
        assert refusal.code == ""
        assert is_capacity_refusal(refusal) is False


async def test_a_silent_exception_still_names_its_class(
    monkeypatch, _configured, caplog
):
    """#1199 AC-5: у таймаутов текст пуст, и без класса запись бесполезна.

    06.09 в журнале осталось «cursor cloud POST /v1/agents failed:» и
    ничего. Причину пришлось восстанавливать по арифметике времени — час
    работы там, где хватило бы одного слова. Пустой str() — подпись именно
    таймаута: у ConnectError и RemoteProtocolError текст есть.
    """
    import logging

    recorder = _Recorder(httpx.ReadTimeout(""))
    _patch_transport(monkeypatch, recorder)

    with caplog.at_level(logging.WARNING, logger="hub.integrations.cursor_cloud"):
        body, refusal = await cursor_cloud.create_agent_attempt(
            repo_url="https://github.com/o/r",
            starting_ref="task-1/x",
            model_id="grok-4",
            prompt_text="review",
            hub_mcp_url="https://agenthai.ru/mcp",
            reviewer_token="t",
        )

    assert body is None and refusal is not None
    assert refusal.is_transport, "таймаут — повторяемый сбой, а не отказ"
    assert "ReadTimeout" in refusal.detail, "класс доезжает до вызывающего"
    assert "ReadTimeout" in caplog.text, "и до журнала"
    assert not caplog.text.rstrip().endswith("failed:"), (
        "запись, обрывающаяся на «failed:», не называет ничего"
    )


async def test_the_marker_is_exact_and_carries_the_generation():
    """#1199: метка различает поколения одной задачи.

    Подбор сравнивает имена на равенство; если метка не несёт поколение,
    вторая сдача подберёт агента первой и получит суждение о старом коде.
    """
    first = cursor_cloud.agent_marker("review", 1199, 1)
    second = cursor_cloud.agent_marker("review", 1199, 2)
    steward = cursor_cloud.agent_marker("steward", 1199, 1)

    assert first != second, "поколение обязано входить в метку"
    assert first != steward, "разные виды заказов не путаются между собой"
    assert first == cursor_cloud.agent_marker("review", 1199, 1), "метка устойчива"
    assert " " not in first, "по метке сравнивают, а не читают — без сюрпризов"


async def test_the_name_reaches_the_request_body(monkeypatch, _configured):
    """#1199: метка действительно уезжает провайдеру.

    Схема провайдера отвергает незнакомые ключи («Unrecognized key(s)»,
    проверено пробой 07.09), поэтому поле обязано быть именно `name` —
    и обязано отсутствовать, когда метку не просили.
    """
    import json as _json

    # Транспорт подставляется ОДИН раз: второй вызов _patch_transport
    # перекрыл бы сам себя, и запрос ушёл бы в первый рекордер.
    recorder = _Recorder(httpx.Response(200, json={"agent": {"id": "bc-1"}}))
    _patch_transport(monkeypatch, recorder)

    async def _attempt_with(name: str):
        await cursor_cloud.create_agent_attempt(
            repo_url="https://github.com/o/r",
            starting_ref="task-1/x",
            model_id="grok-4",
            prompt_text="review",
            hub_mcp_url="https://agenthai.ru/mcp",
            reviewer_token="t",
            name=name,
        )
        return _json.loads(recorder.request.content)

    marked = await _attempt_with("haiplane:review:t1:g1")
    assert marked["name"] == "haiplane:review:t1:g1"

    plain = await _attempt_with("")
    assert "name" not in plain, (
        "без метки поля быть не должно: пустое имя обещало бы подбор, "
        "которого никто не делает"
    )


async def test_creating_an_agent_waits_longer_than_a_plain_call(monkeypatch):
    """#269: у создания свой срок — наблюдённые 59 секунд против общих 30.

    Брошенный на тридцатой секунде запрос всё равно создавал агента, и хаб
    терял его идентификатор. Отдельный срок убирает большую часть таких
    случаев до всякой сверки.
    """
    seen: dict[str, float] = {}

    async def _capture(method, path, json_body=None, timeout=cursor_cloud._TIMEOUT):
        seen["timeout"] = timeout
        return {"agent": {"id": "bc-1"}}, None

    monkeypatch.setattr(cursor_cloud, "_attempt", _capture)
    await cursor_cloud.create_agent_attempt(
        repo_url="https://github.com/o/r",
        starting_ref="task-1/x",
        model_id="grok-4",
        prompt_text="review",
        hub_mcp_url="https://agenthai.ru/mcp",
        reviewer_token="t",
    )

    assert seen["timeout"] == cursor_cloud._CREATE_TIMEOUT
    assert cursor_cloud._CREATE_TIMEOUT > cursor_cloud._TIMEOUT, (
        "срок создания обязан быть больше общего, иначе разделение бессмысленно"
    )


async def test_the_marker_separates_a_top_up_from_the_first_order():
    """#269: номер попытки входит в метку.

    Без него добор той же генерации неотличим от первого заказа, и сверка
    подберёт чужого судью.
    """
    first = cursor_cloud.agent_marker("review", 1199, 1, 1)
    top_up = cursor_cloud.agent_marker("review", 1199, 1, 2)
    assert first != top_up
    assert cursor_cloud.agent_marker("review", 1199, 1, 1) == first


# --- #1206: подбор по имени держится на поле, которого может не быть ------


class _Pages:
    """Подставка страниц списка агентов.

    Форма ответа — `items` и `nextCursor`, как у настоящего API (проверено
    вызовом 06.09.2026 и записано в докстроке list_agents).
    """

    def __init__(self, pages: list[list[object]]):
        self.pages = pages
        self.calls = 0

    async def __call__(self, limit: int = 50, cursor: str = ""):
        self.calls += 1
        index = int(cursor or 0)
        if index >= len(self.pages):
            return {"items": [], "nextCursor": ""}
        nxt = str(index + 1) if index + 1 < len(self.pages) else ""
        return {"items": self.pages[index], "nextCursor": nxt}


async def test_a_listing_without_the_name_field_is_not_a_confirmed_absence(
    monkeypatch, caplog
):
    """#1206 AC-2: элементы без поля name — «спросить не удалось».

    Весь подбор сравнивает item['name'] с меткой. Если провайдер имя не
    возвращает, обход не найдёт ничего НИКОГДА, а прежний код называл это
    подтверждённой пустотой — тем самым разрешением купить второго
    оплаченного агента. Отсутствие поля не есть отсутствие агента (#762).
    """
    pages = _Pages([[{"id": "bc-1", "createdAt": "2026-09-09T00:00:00Z"}]])
    monkeypatch.setattr(cursor_cloud, "list_agents", pages)

    with caplog.at_level("WARNING"):
        seen = await cursor_cloud.find_agent_by_name("haiplane:review:t1206:g1:a1")

    assert seen.asked is False, (
        "ответ без поля name неотличим от «агента нет» — значит, спросить "
        "не удалось, ровно как на теле не той формы"
    )
    assert seen.agent_id == "" and seen.run_id == ""
    assert pages.calls == 1, (
        "непрочитанная страница обрывает обход, а не листает дальше"
    )
    assert "no name field" in caplog.text, "молчащее поле обязано быть названо в логе"


async def test_an_empty_listing_is_still_a_confirmed_absence(monkeypatch):
    """#1206: пустой список остаётся «агента нет» — это не поломано.

    Без этого граница повтор после подтверждённой пустоты (#1199 AC-2)
    перестал бы существовать, и каждый обрыв уходил бы к человеку.
    """
    pages = _Pages([[]])
    monkeypatch.setattr(cursor_cloud, "list_agents", pages)

    seen = await cursor_cloud.find_agent_by_name("haiplane:review:t1206:g1:a1")

    assert seen.asked is True, "спросили и получили ответ: агентов нет"
    assert seen.agent_id == ""


async def test_a_named_neighbour_still_answers_for_the_whole_page(monkeypatch):
    """#1206: поле есть хотя бы у одного — страница прочитана.

    Чужой агент без имени рядом с нашим не должен превращать удачный
    подбор в «спросить не удалось»: иначе новое правило съело бы сам путь.
    """
    marker = "haiplane:review:t1206:g1:a1"
    pages = _Pages(
        [
            [
                {"id": "bc-anon"},
                {"id": "bc-mine", "name": marker, "latestRunId": "run-9"},
            ]
        ]
    )
    monkeypatch.setattr(cursor_cloud, "list_agents", pages)

    seen = await cursor_cloud.find_agent_by_name(marker)

    assert seen == cursor_cloud.Reconciliation("bc-mine", "run-9", True)
