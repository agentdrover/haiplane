"""Thin client for the Cursor Cloud Agents API v1 (#756).

Spec: https://cursor.com/docs/cloud-agent/api/endpoints — PUBLIC BETA, so
the whole module lives by one contract: every method returns ``dict |
None`` and ``None`` means "could not" — no key, network trouble, non-2xx,
unparsable body, changed schema. One ``log.warning`` per failure, never an
exception across the boundary. Consumers (the review dispatcher, #757) are
required to live with ``None``: a broken beta must break nothing except
the automation it powers.

Nobody calls this module until #757 lands — shipping it changes no hub
behavior, the same shadow-step pattern as #581 and #743.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, NamedTuple, cast

import httpx

from hub import brand, config

log = logging.getLogger(__name__)

_TIMEOUT = 30.0
#: Создание агента отдельно: наблюдено 06.09, что провайдер отвечал 59 секунд
#: при общем терпении в 30, и клиент бросал вызов, который на деле удался
#: (#1199). Остальные вызовы модуля — короткие GET, им общий срок годится.
_CREATE_TIMEOUT = 120.0


def is_configured() -> bool:
    return bool((config.CURSOR_API_KEY or "").strip())


@dataclass(frozen=True)
class Refusal:
    """Почему провайдер не принял запрос, в разбираемом виде (#1182).

    Модуль по контракту схлопывает любой провал в ``None``, и это верно
    для потребителей, которым важен только факт: сломанная бета не должна
    ломать ничего, кроме автоматики, которую она питает. Но один
    потребитель — выбор судьи — обязан различать два разных провала.
    «Эта модель здесь недоступна» и «связь оборвалась» требуют разных
    решений: первое означает взять другую модель, второе — попробовать ту
    же ещё раз. Схлопнутые в одно, они дают судью, зависящего от качества
    сети, а такое суждение невоспроизводимо.

    Поэтому деталь отказа не заменяет ``None``, а сопровождает его: старый
    контракт цел, а тот, кому нужно, спрашивает отдельно.
    """

    #: HTTP-код, если ответ вообще был; 0 — до ответа не дошло.
    status: int = 0
    #: ``error.code`` из тела, если провайдер его назвал.
    code: str = ""
    #: Короткий фрагмент ответа — для человека в журнале, не для решений.
    detail: str = ""

    @property
    def is_transport(self) -> bool:
        """Сеть, таймаут, нечитаемый ответ — то, что стоит повторить как есть."""
        return self.status == 0 or self.status // 100 == 5


async def _attempt(
    method: str,
    path: str,
    json_body: dict[str, Any] | None = None,
    timeout: float = _TIMEOUT,
) -> tuple[dict[str, Any] | None, Refusal | None]:
    """Один защищённый заход: ``(тело, отказ)``, и ровно одно из них не None.

    Здесь живёт вся работа; ``_request`` — вид на неё для тех, кому хватает
    факта провала.
    """
    if not is_configured():
        return None, Refusal(detail="CURSOR_API_KEY не задан")
    url = f"{(config.CURSOR_API_URL or 'https://api.cursor.com').rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {config.CURSOR_API_KEY.strip()}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=json_body, headers=headers)
        if resp.status_code // 100 != 2:
            log.warning(
                "cursor cloud %s %s -> HTTP %s: %s",
                method,
                path,
                resp.status_code,
                resp.text[:300],
            )
            return None, Refusal(
                status=resp.status_code,
                code=_error_code(resp),
                detail=resp.text[:300],
            )
        body = resp.json()
        if not isinstance(body, dict):
            log.warning("cursor cloud %s %s -> non-object body", method, path)
            return None, Refusal(status=resp.status_code, detail="тело не объект")
        return body, None
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        # Класс, а не только текст (#1199). У таймаутов httpx/httpcore
        # ``str(exc)`` ПУСТ — в журнале оставалось «failed:» и ничего, и
        # причину пришлось восстанавливать по арифметике времени. Класс
        # называет её сразу: ReadTimeout и ConnectError — разные разговоры.
        detail = f"{type(exc).__name__}: {exc}".strip().rstrip(":").strip()
        log.warning("cursor cloud %s %s failed: %s", method, path, detail)
        return None, Refusal(detail=detail[:300])


def _error_code(resp: httpx.Response) -> str:
    """``error.code`` из тела ответа, или пустая строка.

    Тело беты может оказаться чем угодно, поэтому разбор целиком защищён:
    имя, которого мы не смогли прочитать, — это отсутствие имени, а не
    повод для исключения через границу, которая обещала их не пропускать.
    """
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 - см. контракт модуля
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    code = error.get("code")
    return code.strip() if isinstance(code, str) else ""


async def _request(
    method: str, path: str, json_body: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """One guarded round-trip; every failure mode collapses to None.

    Контракт не изменился: потребителям, которым важен только факт провала,
    по-прежнему возвращается ``None``. Различать причины умеет ``_attempt``,
    и спрашивает его ровно тот, кому это нужно (#1182).
    """
    body, _ = await _attempt(method, path, json_body)
    return body


#: Префикс имени, по которому хаб узнаёт агента, которого заказал сам.
#: Провайдер иначе придумывает имя из промта, и полагаться на него нельзя:
#: из трёх осиротевших агентов 06.09 двое не называли задачу вовсе
#: («Суждение стюарда гейта», «Код-ревью задачи haiplane»).
AGENT_MARKER_PREFIX = "haiplane"


def agent_marker(kind: str, task_id: int, generation: int, attempt: int = 1) -> str:
    """Имя агента: читаемое человеку и разбираемое машиной (#1199).

    Одна строка служит двум разным читателям, и это осознанный компромисс,
    а не небрежность: имя видно в интерфейсе Cursor, где по нему ищет
    человек, и оно же — единственный признак, по которому хаб может узнать
    СВОЙ заказ, если ответ на создание не дошёл. Отдельного поля под метку
    у провайдера нет: тело запроса проверяется строго, незнакомый ключ
    отвергается с ``Unrecognized key(s)`` (проверено пробой 07.09.2026).

    Формат намеренно скучный и без пробелов вокруг разделителей — по нему
    сравнивают на равенство, а не разбирают регулярным выражением.

    ``attempt`` обязателен по смыслу, хотя и со значением по умолчанию
    (находка ревью №269, high). Добор лестницы (#879) заказывает ВТОРОГО
    ревьюера на ТУ ЖЕ генерацию, то есть под тем же именем, — и сверка
    после оборвавшегося добора подобрала бы агента первого, дешёвого
    прогона. Второго POST при этом не случилось бы, но сдача получила бы
    чужого судью, а настоящий остался бы сиротой. Ровно этого запрещает
    AC-4: подбор похожего хуже, чем неподбор.
    """
    return (
        f"{AGENT_MARKER_PREFIX}:{kind}:t{int(task_id)}"
        f":g{int(generation)}:a{int(attempt)}"
    )


async def list_agents(limit: int = 50, cursor: str = "") -> dict[str, Any] | None:
    """Страница списка агентов; ``None`` — не смогли спросить.

    Ответ несёт ключ ``items`` (НЕ ``agents`` и не ``data``) и ``nextCursor``
    для следующей страницы — проверено вызовом 06.09.2026. Разбирать надо
    именно эту форму: выдуманная дала бы пустой список, а пустой список
    здесь означал бы «агента нет» и разрешил бы купить второго.
    """
    path = f"/v1/agents?limit={int(limit)}"
    if cursor:
        path += f"&cursor={cursor}"
    return await _request("GET", path)


class Reconciliation(NamedTuple):
    """Чем кончилась сверка с провайдером (#1199).

    Два поля, потому что вопросов два, и один ответ на оба уже стоил нам
    поколения: «агента нет» разрешает повторить вызов, а «не смогли
    спросить» не разрешает ничего — угаданный агент хуже пропущенного.
    Возвращать на оба случая один ``None`` значило бы повторить ошибку
    #1185 в новом месте.
    """

    #: Идентификатор нашего агента; пусто — не найден.
    agent_id: str
    #: Его последний прогон: список отдаёт latestRunId, и без него подбор
    #: половинчатый — свип не увидит статус и не восстановит отчёт (#269).
    run_id: str
    #: Удалось ли вообще прочитать список. False — ответа нет, а не «нет».
    asked: bool


async def find_agent_by_name(name: str, pages: int = 3) -> Reconciliation:
    """Найти агента с ТОЧНО таким именем и сказать, спросить ли удалось.

    Сравнение на РАВЕНСТВО, не на вхождение: «похожий» агент хуже второго
    агента, потому что второй хотя бы честно свой. Метка соседнего
    поколения отличается одним символом, и вхождение отдало бы сдаче
    чужого судью.

    Весь подбор держится на одном допущении: провайдер возвращает в списке
    то самое поле ``name``, которое хаб положил при создании. Если бы он
    его не возвращал, обход не нашёл бы НИЧЕГО и никогда — и сказал бы об
    этом словом «агента нет», то есть выдал бы разрешение купить второго.
    Механизм против двойной покупки сам бы её и санкционировал.

    Живой round-trip метки НЕ ЗАМЕРЕН (#1206), поэтому «не нашли» само по
    себе ничего не значит: оно одинаково выглядит и когда нашего агента
    правда нет, и когда провайдер выбросил метку и вернул своё имя.
    Различает эти миры один признак — встретилась ли в ответе хоть одна
    метка нашей формы. Наблюдённая 06.09 форма именно такая: поле name
    ЕСТЬ, а значения придуманы из промта (см. AGENT_MARKER_PREFIX), и
    страж «ключа нет ни у кого» на ней не срабатывает.

    Поэтому подтверждённой пустотой считается только ответ, в котором
    метка нашей формы встретилась хотя бы раз (round-trip доказан этим же
    ответом), либо пустой список — там нет ни агентов, ни вопроса.
    Непустой ответ без единой нашей метки — «спросить не удалось».
    """
    if not name:
        return Reconciliation("", "", True)
    cursor = ""
    listed = 0
    named = 0
    seen_marker = False
    for _ in range(max(1, pages)):
        page = await list_agents(cursor=cursor)
        if page is None:
            return Reconciliation("", "", False)
        items = page.get("items")
        if not isinstance(items, list):
            # Тело не той формы — это «прочитать не смог», а не «пусто»
            # (находка ревью №269). Пустой обход по чужой форме дал бы
            # подтверждённую пустоту, а она разрешает купить второго
            # агента: отсутствие данных снова стало бы значением (#762).
            log.warning("cursor cloud /v1/agents: no items list in body")
            return Reconciliation("", "", False)
        listed += len(items)
        for item in items:
            if not isinstance(item, dict):
                continue
            value = item.get("name")
            if isinstance(value, str) and value:
                named += 1
            else:
                # Ключа нет, значение пустое или не строка — сравнивать не
                # с чем. Такой элемент не доказывает round-trip и не
                # опровергает его: он просто молчит.
                continue
            if value.startswith(f"{AGENT_MARKER_PREFIX}:"):
                # Метка НАШЕЙ формы (не просто слово «haiplane» внутри
                # автоимени из промта): этим же ответом доказано, что имя
                # переживает создание.
                seen_marker = True
            if value == name:
                found = str(item.get("id") or "").strip()
                if found:
                    return Reconciliation(
                        found, str(item.get("latestRunId") or "").strip(), True
                    )
        cursor = str(page.get("nextCursor") or "")
        if not cursor:
            break
    if listed and not seen_marker:
        # Агенты в ответе есть, а метки нашей формы нет ни одной. Отсюда
        # неразличимы «нашего заказа тут нет» и «провайдер метку не
        # хранит», а второе делает подбор невозможным НАВСЕГДА. Назвать
        # это «агента нет» значило бы выдать разрешение купить второго —
        # ровно то, против чего построен весь путь (#762, #1206).
        # Счётчик ``named`` тут не для красоты: он единственный, кто
        # разделит два мира в проде, пока живого замера нет (#1206 AC-1).
        # named=0 — поля нет вовсе; named>0 — поле есть, но наша метка в
        # нём не выжила. Это и будет запись замера, когда обрыв случится.
        log.warning(
            "cursor cloud /v1/agents: %s agents listed, %s carry a name, "
            "none carries a %s: marker — round-trip of our name field is "
            "unproven",
            listed,
            named,
            AGENT_MARKER_PREFIX,
        )
        return Reconciliation("", "", False)
    return Reconciliation("", "", True)


#: Параметр модели в заказе: ``{"id": "fast", "value": "false"}`` (#1417).
ModelParam = dict[str, str]

#: Значение CURSOR_REVIEW_MODEL_PARAMS «подобрать по каталогу» (#1423).
REVIEW_PARAMS_FROM_CATALOG = "auto"

#: Сколько живёт прочитанный каталог моделей (#1423). Час: варианты у
#: Cursor меняются с выпуском моделей, а не между заказами, а заказов ревью —
#: единицы в час, так что чтение на каждый заказ удваивало бы запросы ради
#: свежести, которой никто не ждёт. Устаревание дороже одного лишнего
#: запроса не выходит: вариант, который провайдер перестал принимать, ловит
#: запасной путь (любой 400 → один заказ без params), а через час каталог
#: перечитывается сам. Неудачное чтение не кэшируется — следующий заказ
#: спросит снова.
MODELS_CATALOG_TTL_SECONDS = 3600.0

_models_catalog: dict[str, Any] = {}


def forget_models_catalog() -> None:
    """Забыть прочитанный каталог — для тестов и ручного сброса."""
    _models_catalog.clear()


def parse_model_params(raw: str) -> list[ModelParam]:
    """``"effort=high,fast=false"`` → ``[{id, value}, ...]``.

    Запись без ``=`` или с пустым именем пропускается с предупреждением —
    послать провайдеру параметр, которого владелец не называл, хуже, чем не
    послать никакого.
    """
    params: list[ModelParam] = []
    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if not item:
            continue
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            log.warning("CURSOR_REVIEW_MODEL_PARAMS: skipped malformed %r", item)
            continue
        params.append({"id": key.strip(), "value": value.strip()})
    return params


def _as_params(raw: Any) -> list[ModelParam] | None:
    """``variants[].params`` в виде ``[{id, value}]`` или None, если форма чужая."""
    if not isinstance(raw, list):
        return None
    params: list[ModelParam] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        key, value = item.get("id"), item.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        params.append({"id": key, "value": value})
    return params


def _catalog_item(catalog: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    items = catalog.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        aliases = item.get("aliases")
        names = [item.get("id")] + (aliases if isinstance(aliases, list) else [])
        if model_id in names:
            return item
    return None


def slow_default_variant(catalog: dict[str, Any], model_id: str) -> list[ModelParam]:
    """Вариант isDefault модели с fast=false — только если он есть в variants.

    Cursor принимает в ``model.params`` лишь полные пары из ``variants``
    (25.09.2026: ``[{fast:false}]`` для grok-4.6 → 400 invalid_model, #1423),
    поэтому пара не собирается, а находится: берётся вариант по умолчанию,
    в нём меняется только fast, и получившийся набор обязан стоять в
    variants. Нет модели, нет isDefault, нет fast или нет такой пары —
    пустой список, заказ без params: вариант по умолчанию дороже, но он
    заведомо допустим.
    """
    item = _catalog_item(catalog, model_id)
    variants = item.get("variants") if item else None
    if not isinstance(variants, list):
        return []
    offered = [
        p
        for p in (_as_params(v.get("params")) for v in variants if isinstance(v, dict))
        if p is not None
    ]
    default = next(
        (
            _as_params(v.get("params"))
            for v in variants
            if isinstance(v, dict) and v.get("isDefault") is True
        ),
        None,
    )
    if not default or not any(p["id"] == "fast" for p in default):
        return []
    wanted = {p["id"]: ("false" if p["id"] == "fast" else p["value"]) for p in default}
    for params in offered:
        if {p["id"]: p["value"] for p in params} == wanted and len(params) == len(
            wanted
        ):
            return params
    return []


async def _read_models_catalog() -> dict[str, Any] | None:
    """Каталог моделей из кэша или свежим чтением; None — прочитать не вышло."""
    now = time.monotonic()
    cached = _models_catalog.get("body")
    if cached is not None and now - _models_catalog.get("at", 0.0) < (
        MODELS_CATALOG_TTL_SECONDS
    ):
        return cast(dict[str, Any], cached)
    body = await list_models()
    if body is None or not isinstance(body.get("items"), list):
        return None
    _models_catalog.update(body=body, at=now)
    return body


async def review_params_for(model_id: str) -> list[ModelParam]:
    """Параметры заказа ревьюера под КОНКРЕТНУЮ модель (#1423).

    Порядок: пустая настройка — без params (быстрая мера владельца, как до
    #1417); явные пары — как есть, их выбрал человек, и если провайдер их
    отвергнет, запасной путь закажет без них; ``auto`` (умолчание) — вариант
    по умолчанию этой модели с fast=false из GET /v1/models. Каталог не
    прочитан — без params: ревью дороже, но оно есть.
    """
    raw = (config.CURSOR_REVIEW_MODEL_PARAMS or "").strip()
    if raw.lower() != REVIEW_PARAMS_FROM_CATALOG:
        return parse_model_params(raw)
    catalog = await _read_models_catalog()
    return slow_default_variant(catalog, model_id) if catalog is not None else []


def model_variant(model_id: str, params: list[ModelParam] | None) -> str:
    """Модель с параметрами одной строкой: ``grok-4.6 fast=false`` (#1417)."""
    tail = ",".join(f"{p['id']}={p['value']}" for p in params or [])
    return f"{model_id} {tail}" if tail else model_id


async def create_review_agent(
    *,
    repo_url: str,
    starting_ref: str,
    model_id: str,
    prompt_text: str,
    hub_mcp_url: str,
    reviewer_token: str,
    name: str = "",
    model_params: list[ModelParam] | None = None,
) -> dict[str, Any] | None:
    """Queue a cloud agent that reviews ``starting_ref`` of ``repo_url``.

    The agent gets the hub's own MCP inline, authenticated as the REVIEWER
    principal — the report comes back through our contract
    (hub_get_review_brief / hub_submit_machine_review), not through git:
    ``autoCreatePR=false`` and ``workOnCurrentBranch=false`` keep any
    accidental commits on a throwaway cursor/ branch.

    ``model_params=None`` — параметры ревьюера под эту модель (#1423): по
    умолчанию вариант из каталога с fast=false, та же модель без наценки.
    """
    created, _ = await create_agent_attempt(
        repo_url=repo_url,
        starting_ref=starting_ref,
        model_id=model_id,
        prompt_text=prompt_text,
        hub_mcp_url=hub_mcp_url,
        reviewer_token=reviewer_token,
        name=name,
        model_params=(
            await review_params_for(model_id) if model_params is None else model_params
        ),
    )
    return created


async def create_agent_attempt(
    *,
    repo_url: str,
    starting_ref: str,
    model_id: str,
    prompt_text: str,
    hub_mcp_url: str,
    reviewer_token: str,
    name: str = "",
    model_params: list[ModelParam] | None = None,
) -> tuple[dict[str, Any] | None, Refusal | None]:
    """То же, что :func:`create_review_agent`, но с причиной отказа (#1182).

    Нужна одному потребителю — выбору судьи, который обязан отличить
    «эта модель недоступна» от «связь оборвалась». Тело запроса собирается
    здесь, а ``create_review_agent`` остаётся видом на неё: два способа
    собрать один и тот же запрос разошлись бы, и разошёлся бы тот, который
    реже читают.

    ``model_params`` (#1417) уходят в ``model.params`` как есть; пусто или
    ``None`` — голый ``model.id``, как до задачи. Умолчания тут нет
    намеренно: судья стюарда зовёт этот же шов, и параметры ревьюера ему
    не принадлежат.
    """
    model: dict[str, Any] = {"id": model_id}
    if model_params:
        model["params"] = [dict(p) for p in model_params]
    body: dict[str, Any] = {
        "prompt": {"text": prompt_text},
        "model": model,
        "repos": [{"url": repo_url, "startingRef": starting_ref}],
        "autoCreatePR": False,
        "workOnCurrentBranch": False,
        "mcpServers": [
            {
                "name": brand.MCP_SERVER_NAME,
                "type": "http",
                "url": hub_mcp_url,
                "headers": {"Authorization": f"Bearer {reviewer_token}"},
            }
        ],
    }
    if name:
        # Метка кладётся ТОЛЬКО когда её попросили: пустое имя означает, что
        # вызывающий подбирать не собирается, и придумывать за него метку —
        # значит обещать восстановление, которого никто не делает.
        body["name"] = name
    # Создание ждёт дольше остальных вызовов: брошенный на 30-й секунде
    # запрос всё равно создавал агента, и хаб терял его идентификатор.
    return await _attempt("POST", "/v1/agents", body, timeout=_CREATE_TIMEOUT)


async def get_run(agent_id: str, run_id: str) -> dict[str, Any] | None:
    return await _request("GET", f"/v1/agents/{agent_id}/runs/{run_id}")


async def get_usage(agent_id: str, run_id: str | None = None) -> dict[str, Any] | None:
    path = f"/v1/agents/{agent_id}/usage"
    if run_id:
        path += f"?runId={run_id}"
    return await _request("GET", path)


async def list_models() -> dict[str, Any] | None:
    return await _request("GET", "/v1/models")
