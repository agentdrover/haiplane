"""Cursor Cloud Agents API client (#756): spec-shaped requests, honest Nones.

The client's whole contract is degradation: no key → no network call; any
error → None + one warning; the request body follows the v1 spec so the
dispatcher (#757) never builds payloads itself.
"""

from __future__ import annotations

import json
from pathlib import Path

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
    # Голый model.id — форма без параметров (#1417 AC-2); умолчание по каталогу
    # проверяет test_review_model_shape_matches_recorded_models_catalog.
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL_PARAMS", "")

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


async def _ordered_review_body(monkeypatch) -> dict:
    import json

    recorder = _Recorder(httpx.Response(200, json={"agent": {"id": "bc-1"}}))
    _patch_transport(monkeypatch, recorder)
    await cursor_cloud.create_review_agent(
        repo_url="https://github.com/o/r",
        starting_ref="task-1/x",
        model_id="grok-4.6",
        prompt_text="review",
        hub_mcp_url="https://hub/mcp",
        reviewer_token="tok",
    )
    assert recorder.request is not None
    return json.loads(recorder.request.content)


#: Записанная выдержка GET /v1/models (#1423): откуда и что в ней — поле
#: ``_recorded`` в самом файле. Умолчание заказа сверяется с ней, а не с
#: тем, что тест считает правильным форматом.
RECORDED_MODELS_CATALOG: dict = json.loads(
    (Path(__file__).parent / "fixtures" / "cursor_models_catalog.json").read_text()
)


class _Provider:
    """Каталог на GET /v1/models, агент на POST /v1/agents; помнит всё."""

    def __init__(self, catalog: httpx.Response | Exception):
        self.catalog = catalog
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path == "/v1/models":
            if isinstance(self.catalog, Exception):
                raise self.catalog
            return self.catalog
        if request.method == "POST" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"agent": {"id": "bc-1"}})
        raise AssertionError(f"unexpected call {request.method} {request.url}")

    def catalog_reads(self) -> int:
        return sum(1 for r in self.requests if r.url.path == "/v1/models")

    def ordered_models(self) -> list[dict]:
        return [
            json.loads(r.content)["model"]
            for r in self.requests
            if r.url.path == "/v1/agents"
        ]


async def _order(monkeypatch, provider: _Provider, model_id: str) -> None:
    original_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(provider.handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    await cursor_cloud.create_review_agent(
        repo_url="https://github.com/o/r",
        starting_ref="task-1/x",
        model_id=model_id,
        prompt_text="review",
        hub_mcp_url="https://hub/mcp",
        reviewer_token="tok",
    )


async def test_review_model_shape_matches_recorded_models_catalog(
    monkeypatch, _configured
):
    """#1423 AC-3: умолчание берёт вариант из каталога, а не собирает свой.

    #1417 слал ``[{fast:false}]`` — пары такой в variants grok-4.6 нет, и
    Cursor отвечал 400 invalid_model на каждый заказ. Теперь: вариант
    isDefault этой модели, в нём только fast → false, и такая пара обязана
    СТОЯТЬ в variants. Умолчание проверяется как есть: переменной в
    окружении теста нет, значение в config — то, что служба получит без
    drop-in'а.
    """
    import os

    assert "CURSOR_REVIEW_MODEL_PARAMS" not in os.environ
    provider = _Provider(httpx.Response(200, json=RECORDED_MODELS_CATALOG))

    await _order(monkeypatch, provider, "grok-4.6")
    await _order(monkeypatch, provider, "grok-4.7")
    await _order(monkeypatch, provider, "composer-2.5")

    grok46, grok47, composer = provider.ordered_models()
    assert grok46 == {
        "id": "grok-4.6",
        "params": [{"id": "effort", "value": "high"}, {"id": "fast", "value": "false"}],
    }
    assert grok47 == {
        "id": "grok-4.7",
        "params": [
            {"id": "context", "value": "500k"},
            {"id": "reasoning_effort", "value": "high"},
            {"id": "fast", "value": "false"},
        ],
    }
    assert composer == {
        "id": "composer-2.5",
        "params": [{"id": "fast", "value": "false"}],
    }
    # Каждая отправленная форма — дословно пара из записанных variants.
    for model in provider.ordered_models():
        (item,) = [
            i for i in RECORDED_MODELS_CATALOG["items"] if i["id"] == model["id"]
        ]
        assert model["params"] in [v["params"] for v in item["variants"]]
    assert provider.catalog_reads() == 1, "каталог кэшируется, а не читается на заказ"


async def test_catalog_without_slow_pair_or_model_orders_bare(monkeypatch, _configured):
    """#1423: нет пары с fast=false или модели в каталоге — заказ без params."""
    catalog = {
        "items": [
            {
                "id": "grok-4.6",
                "variants": [
                    {
                        "params": [
                            {"id": "effort", "value": "high"},
                            {"id": "fast", "value": "true"},
                        ],
                        "isDefault": True,
                    },
                    {
                        "params": [
                            {"id": "effort", "value": "low"},
                            {"id": "fast", "value": "false"},
                        ]
                    },
                ],
            }
        ]
    }
    provider = _Provider(httpx.Response(200, json=catalog))

    await _order(monkeypatch, provider, "grok-4.6")
    await _order(monkeypatch, provider, "gpt-unknown")

    assert provider.ordered_models() == [{"id": "grok-4.6"}, {"id": "gpt-unknown"}]


@pytest.mark.parametrize(
    "catalog",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, json={"items": "junk"}),
        httpx.ConnectTimeout("slow"),
    ],
)
async def test_unreadable_catalog_orders_bare(monkeypatch, _configured, catalog):
    """#1423: каталог не прочитан — заказ без params, ревью не теряется."""
    provider = _Provider(catalog)
    await _order(monkeypatch, provider, "grok-4.6")
    assert provider.ordered_models() == [{"id": "grok-4.6"}]


async def test_explicit_review_params_win_over_catalog(monkeypatch, _configured):
    """#1423: явная настройка владельца — как есть, каталог даже не читается."""
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL_PARAMS", "effort=high,fast=false")
    provider = _Provider(httpx.Response(200, json=RECORDED_MODELS_CATALOG))
    await _order(monkeypatch, provider, "grok-4.6")
    assert provider.ordered_models() == [
        {
            "id": "grok-4.6",
            "params": [
                {"id": "effort", "value": "high"},
                {"id": "fast", "value": "false"},
            ],
        }
    ]
    assert provider.catalog_reads() == 0


async def test_empty_review_model_params_sends_bare_model(monkeypatch, _configured):
    """#1417 AC-2: явно пустая настройка — тело без params, как до задачи."""
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL_PARAMS", "")
    body = await _ordered_review_body(monkeypatch)
    assert body["model"] == {"id": "grok-4.6"}


def test_review_model_params_parse_pairs_and_skip_junk():
    params = cursor_cloud.parse_model_params(" fast=false , effort=high,junk,=x,")
    assert params == [
        {"id": "fast", "value": "false"},
        {"id": "effort", "value": "high"},
    ]
    assert cursor_cloud.model_variant("grok-4.6", params) == (
        "grok-4.6 fast=false,effort=high"
    )
    assert cursor_cloud.model_variant("grok-4.6", []) == "grok-4.6"


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
    assert "1 agents listed, 0 carry a name" in caplog.text, (
        "лог обязан различать два мира: поля нет вовсе (0 named) против "
        "поле есть, но метка в нём не выжила — живого замера у нас нет, и "
        "эта запись его заменяет"
    )
    assert "unproven" in caplog.text, "молчащее поле обязано быть названо в логе"


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


# --- #1206: имя есть, но чужое — наблюдённая форма ------------------------


async def test_foreign_names_are_not_a_confirmed_absence(monkeypatch, caplog):
    """#1206: страница с чужими именами не доказывает, что метка дожила.

    Форма, наблюдённая 06.09: у осиротевших агентов поле name БЫЛО, а
    значения провайдер придумал из промта («Суждение стюарда гейта»,
    «Код-ревью задачи haiplane») — см. комментарий у AGENT_MARKER_PREFIX.
    Страж «ключа name нет ни у кого» на такой странице не срабатывает:
    имена есть. Сверка на равенство не находит метку и заканчивается
    подтверждённой пустотой — тем самым разрешением купить второго.

    Различить два мира — «провайдер метку не сохранил» и «нашего агента
    правда нет» — по такой странице НЕЛЬЗЯ: в обоих метка не встречается
    ни разу. Значит, спросить не удалось.
    """
    pages = _Pages(
        [
            [
                {"id": "bc-1", "name": "Суждение стюарда гейта"},
                {"id": "bc-2", "name": "Код-ревью задачи haiplane"},
            ]
        ]
    )
    monkeypatch.setattr(cursor_cloud, "list_agents", pages)

    with caplog.at_level("WARNING"):
        seen = await cursor_cloud.find_agent_by_name("haiplane:review:t1206:g1:a1")

    assert seen.asked is False, (
        "ни одно имя не несёт нашей метки — round-trip не подтверждён, "
        "и «не нашли» здесь неотличимо от «имя не сохраняется»"
    )
    assert seen.agent_id == "" and seen.run_id == ""
    assert "2 agents listed, 2 carry a name" in caplog.text, (
        "мир «поле есть, метка не выжила» обязан быть отличим в логе от "
        "мира «поля нет вовсе»: другого замера round-trip у нас нет"
    )
    assert "marker" in caplog.text, "непроверенный round-trip обязан быть назван"


async def test_the_marker_is_recognised_by_prefix_not_by_substring(monkeypatch):
    """#1206: «Код-ревью задачи haiplane» — не метка.

    Признак своего заказа — начало строки `haiplane:`, а не слово
    «haiplane» где-то внутри: автоимя из промта почти всегда упоминает
    проект, и вхождение объявило бы round-trip доказанным по чужой
    строке. Тогда страж умер бы ровно там, где он нужен.
    """
    pages = _Pages([[{"id": "bc-1", "name": "haiplane review of task 1206"}]])
    monkeypatch.setattr(cursor_cloud, "list_agents", pages)

    seen = await cursor_cloud.find_agent_by_name("haiplane:review:t1206:g1:a1")

    assert seen.asked is False, "упоминание проекта в чужом имени ничего не доказывает"


async def test_an_empty_name_value_is_not_evidence(monkeypatch):
    """#1206: ключ есть, значения нет — доказательства тоже нет.

    `name: ""` и `name: null` проходят проверку «ключ на месте», но
    сравнивать с меткой в них нечего. Считать такую страницу прочитанной
    значило бы вернуть подтверждённую пустоту на пустом месте.
    """
    pages = _Pages([[{"id": "bc-1", "name": ""}, {"id": "bc-2", "name": None}]])
    monkeypatch.setattr(cursor_cloud, "list_agents", pages)

    seen = await cursor_cloud.find_agent_by_name("haiplane:review:t1206:g1:a1")

    assert seen.asked is False, "пустое имя не отличает наш заказ от чужого"


async def test_a_marker_of_another_order_confirms_the_absence(monkeypatch):
    """#1206: чужая НАША метка на странице — доказательство round-trip.

    Если провайдер вернул метку соседнего поколения или другой задачи,
    значит поле переживает создание. Тогда «нашей метки здесь нет» —
    настоящий ответ, и повтор после подтверждённой пустоты (#1199 AC-2)
    остаётся живым. Иначе новое правило съело бы весь путь повтора.
    """
    pages = _Pages(
        [
            [
                {
                    "id": "bc-other",
                    "name": cursor_cloud.agent_marker("review", 1198, 1),
                },
                {"id": "bc-alien", "name": "Суждение стюарда гейта"},
            ]
        ]
    )
    monkeypatch.setattr(cursor_cloud, "list_agents", pages)

    seen = await cursor_cloud.find_agent_by_name("haiplane:review:t1206:g1:a1")

    assert seen.asked is True, "метка дожила у соседа — значит спросить удалось"
    assert seen.agent_id == "", "нашего агента нет, и это сказано прямо"


async def test_a_marker_on_a_later_page_still_answers(monkeypatch):
    """#1206: доказательство round-trip копится по всем прочитанным страницам.

    Первая страница может целиком состоять из чужих имён — список идёт
    newest-first, и свежие заказы могли быть не наши. Обрывать обход на
    ней значило бы уходить в blind там, где ответ есть страницей ниже.
    """
    marker = "haiplane:review:t1206:g1:a1"
    pages = _Pages(
        [
            [{"id": "bc-alien", "name": "Суждение стюарда гейта"}],
            [{"id": "bc-mine", "name": marker, "latestRunId": "run-7"}],
        ]
    )
    monkeypatch.setattr(cursor_cloud, "list_agents", pages)

    seen = await cursor_cloud.find_agent_by_name(marker)

    assert seen == cursor_cloud.Reconciliation("bc-mine", "run-7", True)
    assert pages.calls == 2, "вторая страница обязана быть прочитана"


async def test_a_marker_seen_on_an_earlier_page_still_answers(monkeypatch):
    """#1206: доказательство round-trip не сгорает на следующей странице.

    Метка нашей формы, встреченная на первой странице, доказывает, что
    поле переживает создание, — и продолжает доказывать это на второй.
    Если бы признак пересчитывался с нуля на каждой странице, обход
    последней страницы с чужими именами уводил бы в blind даже там, где
    round-trip уже подтверждён этим же ответом, и повтор после
    подтверждённой пустоты (#1199 AC-2) исчезал бы от одной чужой строки.
    """
    pages = _Pages(
        [
            [{"id": "bc-old", "name": cursor_cloud.agent_marker("review", 1198, 1)}],
            [{"id": "bc-alien", "name": "Суждение стюарда гейта"}],
        ]
    )
    monkeypatch.setattr(cursor_cloud, "list_agents", pages)

    seen = await cursor_cloud.find_agent_by_name("haiplane:review:t1206:g1:a1")

    assert pages.calls == 2, "обход обязан дойти до конца"
    assert seen.asked is True, (
        "метка дожила на первой странице — round-trip доказан для всего ответа"
    )
    assert seen.agent_id == "", "нашего агента нет, и это сказано прямо"
