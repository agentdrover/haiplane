"""Клиент GitVerse: транспорт, честность ответов, живой контракт (#1115)."""

from __future__ import annotations

import os

import httpx
import pytest

from hub.integrations.forge.gitverse import GitVerseForge
from hub.integrations.protocols import (
    CIProbeOutcome,
    ForgePlugin,
    MergeabilityOutcome,
)

TOKEN = "test-token-not-a-real-secret"


@pytest.fixture
def patched_httpx(monkeypatch):
    """Подмена httpx.AsyncClient на клиент с MockTransport."""
    seen: list[httpx.Request] = []
    responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0) if responses else httpx.Response(200, json={})

    original = httpx.AsyncClient

    class Patched(original):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Patched)
    return seen, responses


# ---------------------------------------------------------------------------
# Контракт
# ---------------------------------------------------------------------------


def test_gitverse_forge_satisfies_the_protocol():
    """AC: адаптер реализует ForgePlugin целиком и ничего сверх него.

    Односторонняя проверка живёт в tests/test_forge_registry.py и уже
    покрывает GitVerseForge через список реализаций; здесь фиксируется сам
    факт соответствия, чтобы падение читалось как «адаптер разошёлся с
    контрактом», а не как ошибка в чужом тесте.
    """
    assert isinstance(GitVerseForge(token=TOKEN), ForgePlugin)
    assert GitVerseForge(token=TOKEN).name == "gitverse"


# ---------------------------------------------------------------------------
# Транспорт
# ---------------------------------------------------------------------------


async def test_every_request_carries_version_and_auth(patched_httpx):
    """AC-1. Заголовок версии и авторизация — на КАЖДОМ запросе.

    Это самая дорогая мелочь интеграции: без заголовка версии сервер
    отвечает 400 с пустым телом, неотличимым от проблемы авторизации.
    Проверяется не один метод, а несколько разных — заголовки ставит
    транспорт, и цена ошибки в том, что забыть его можно в одном методе.
    """
    seen, responses = patched_httpx
    responses.extend(
        [
            httpx.Response(200, json={"number": 7, "head": {"sha": "abc"}}),
            httpx.Response(200, json=[]),
            httpx.Response(204),
        ]
    )
    forge = GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")

    await forge.pr_head_sha(7, gh_repo="own/rep")
    await forge.pr_for_branch("task-1/x", gh_repo="own/rep")
    await forge._pr_merged(7, gh_repo="own/rep")

    assert len(seen) == 3
    for request in seen:
        assert (
            request.headers["accept"]
            == "application/vnd.gitverse.object+json;version=1"
        )
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert str(request.url).startswith("https://api.example/repos/own/rep")


async def test_error_causes_stay_distinct(patched_httpx, monkeypatch):
    """AC-2. 401, 403, 404, 400-без-тела и обрыв сети — пять разных диагнозов.

    Схлопывание их в одно «не получилось» — это дефект #419, перенесённый в
    новый транспорт: у каждого своя рука, которая чинит.
    """
    seen, responses = patched_httpx
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    forge = GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")

    responses.append(httpx.Response(401, text=""))
    assert (await forge._request("GET", "/x")).reason == "gitverse_http_401"

    responses.append(httpx.Response(403, json={"message": "no"}))
    assert (await forge._request("GET", "/x")).reason == "gitverse_http_403"

    responses.append(httpx.Response(404, json={"message": "gone"}))
    r404 = await forge._request("GET", "/x")
    assert r404.status == 404 and not r404.ok

    # 400 с пустым телом — самый частый ответ на забытую версию, и причина
    # обязана это подсказывать, а не молчать вместе с сервером.
    responses.append(httpx.Response(400, text=""))
    r400 = await forge._request("GET", "/x")
    assert r400.status == 400
    assert "Accept" in r400.reason

    # До сервера не дошли: статуса нет вовсе — это НЕ то же, что отказ.
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    class Broken(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(boom)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Broken)
    down = await forge._request("GET", "/x")
    assert down.status is None
    assert down.reason.startswith("gitverse_transport_error")


async def _no_sleep(_seconds):
    return None


async def test_only_retryable_codes_are_retried(patched_httpx, monkeypatch):
    """429 и 5xx повторяются, 4xx — нет.

    Повторять запрос, отвергнутый по существу, значит жечь квоту (3000 в час
    на пользователя) и оттягивать момент, когда причина будет названа.
    """
    seen, responses = patched_httpx
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    forge = GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")

    responses.extend([httpx.Response(429)] * 3)
    await forge._request("GET", "/x")
    assert len(seen) == 3, "429 просит подождать — повторяем"

    seen.clear()
    responses.extend([httpx.Response(500)] * 3)
    await forge._request("GET", "/x")
    assert len(seen) == 3, "5xx — сторона сервера, повторяем"

    seen.clear()
    responses.append(httpx.Response(403))
    await forge._request("GET", "/x")
    assert len(seen) == 1, "4xx повторять нечего — причина не изменится"


async def test_token_never_reaches_the_log(patched_httpx, caplog, monkeypatch):
    """Токен не попадает в логи ни целиком, ни хвостом."""
    import logging

    seen, responses = patched_httpx
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    forge = GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")

    with caplog.at_level(logging.DEBUG):
        responses.extend([httpx.Response(429)] * 3)
        await forge._request("GET", "/x")
        responses.append(httpx.Response(500, text="server said no"))
        responses.extend([httpx.Response(500)] * 2)
        await forge._request("GET", "/x")

    assert TOKEN not in caplog.text
    assert TOKEN[-8:] not in caplog.text


async def test_without_a_token_the_client_says_so(monkeypatch):
    """Ненастроенный форж отвечает причиной, а не притворяется работающим."""
    forge = GitVerseForge(token="", base_url="https://api.example", version="1")
    resp = await forge._request("GET", "/x")
    assert resp.status is None
    assert resp.reason == "gitverse_token_not_configured"


# ---------------------------------------------------------------------------
# Чтение pull request'ов
# ---------------------------------------------------------------------------


async def test_merged_pr_is_not_reported_as_closed(patched_httpx):
    """Влитый PR не читается как закрытый вручную.

    У GitVerse, как и у Gitea, влитый PR остаётся в состоянии "closed" и
    отличается отдельным полем. Прочитать одно ``state`` — значит сказать
    гейту, что работу закрыли, а не доставили.
    """
    seen, responses = patched_httpx
    forge = GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")

    responses.append(httpx.Response(200, json={"state": "closed", "merged": True}))
    assert await forge.pr_state(1, gh_repo="own/rep") == "merged"

    responses.append(httpx.Response(200, json={"state": "closed", "merged": False}))
    assert await forge.pr_state(1, gh_repo="own/rep") == "closed"

    responses.append(httpx.Response(200, json={"state": "open"}))
    assert await forge.pr_state(1, gh_repo="own/rep") == "open"


async def test_absent_pr_is_an_answer_but_a_broken_link_is_not(
    patched_httpx, monkeypatch
):
    """404 — это ответ «такого PR тут нет» (#959); обрыв — пустая строка."""
    seen, responses = patched_httpx
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    forge = GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")

    responses.append(httpx.Response(404, json={"message": "not found"}))
    assert await forge.pr_state(999, gh_repo="own/rep") == "absent"

    responses.extend([httpx.Response(500)] * 3)
    assert await forge.pr_state(999, gh_repo="own/rep") == "", (
        "«не посмотрели» обязано отличаться от «нет такого»"
    )


async def test_merge_status_has_three_answers(patched_httpx, monkeypatch):
    """204 — влит, 404 — нет, всё прочее — «спросить не удалось».

    Третий ответ существует не для полноты: доставка в #1116 признаёт мерж
    только по 204, и если ошибку транспорта прочитать как False, она объявит
    влитую работу недоставленной.
    """
    seen, responses = patched_httpx
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    forge = GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")

    responses.append(httpx.Response(204))
    assert await forge._pr_merged(1, gh_repo="own/rep") is True

    responses.append(httpx.Response(404))
    assert await forge._pr_merged(1, gh_repo="own/rep") is False

    responses.extend([httpx.Response(500)] * 3)
    assert await forge._pr_merged(1, gh_repo="own/rep") is None


async def test_compare_cuts_subjects_by_commit_not_by_line(patched_httpx):
    """Многострочное сообщение — один коммит, а не столько, сколько строк.

    Ровно на этом релиз приписывал себе чужие задачи (#963): PR #40 насчитал
    25 «коммитов» для одного, потому что резали текст по строкам.
    """
    seen, responses = patched_httpx
    forge = GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")
    responses.append(
        httpx.Response(
            200,
            json={
                "commits": [
                    {"commit": {"message": "feat(task): первый (#1)\n\nтело\nещё"}},
                    {"commit": {"message": "fix(task): второй (#2)"}},
                ]
            },
        )
    )

    subjects = await forge.compare_subjects("develop", "main", gh_repo="own/rep")

    assert subjects == ["fix(task): второй (#2)", "feat(task): первый (#1)"]


# ---------------------------------------------------------------------------
# Честность про то, чего у форжа нет и чего ещё не сделано
# ---------------------------------------------------------------------------


async def test_absent_capabilities_answer_honestly():
    """AC-4. Чего ЕЩЁ НЕТ у нас — отвечается «спросить не удалось».

    Мерж и CI существуют, но их семантика принадлежит #1116 и #1117. До тех
    пор ответ — «спросить не удалось», и это важнее, чем кажется: ``absent``
    пустил бы доставку мимо CI, ``pending`` заставил бы гейт ждать вечно, а
    ``mergeable`` пустил бы её вслепую.
    """
    forge = GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")

    outcome, detail = await forge.pr_mergeability(1)
    assert outcome is MergeabilityOutcome.unavailable
    assert "#1116" in detail

    state, why = await forge.merge_branches("develop", "main", "msg")
    assert state == "unavailable" and "#1116" in why
    assert await forge.merge_pr(1, "subject") is False

    probe = await forge.check_pr_ci(1)
    assert probe.outcome is CIProbeOutcome.unavailable
    assert probe.outcome is not CIProbeOutcome.absent
    assert await forge.branch_ci_runs("develop") is None


# ---------------------------------------------------------------------------
# Живой контракт
# ---------------------------------------------------------------------------

_LIVE_REPO = os.environ.get("HAIPLANE_GITVERSE_TEST_REPO", "")
_LIVE_TOKEN = os.environ.get("HAIPLANE_GITVERSE_TOKEN", "")


@pytest.mark.skipif(
    not (_LIVE_REPO and _LIVE_TOKEN),
    reason="нет HAIPLANE_GITVERSE_TOKEN и HAIPLANE_GITVERSE_TEST_REPO",
)
async def test_live_contract_against_real_repo():
    """AC-3. Форма ответов живого API совпадает с той, на которую рассчитан код.

    Пропускается, а не краснеет, без токена: расхождение с документацией —
    это факт о вендоре, и узнавать о нём должен тот, у кого есть доступ, а
    не всякий, кто запустил тесты. Но там, где доступ есть, он вскрывается
    ЗДЕСЬ, а не на первой доставке.

    Все вызовы читающие: живой тест не должен ничего менять в репозитории.
    """
    forge = GitVerseForge()
    resp = await forge._request("GET", f"/repos/{_LIVE_REPO}")

    assert resp.ok, f"{resp.status} {resp.reason} {resp.text}"
    assert isinstance(resp.data, dict)
    # Поля, на которые опирается адаптер, — именно они, а не «что-нибудь».
    assert "default_branch" in resp.data
    assert str(resp.data.get("full_name") or "").lower() == _LIVE_REPO.lower()

    # Заголовок версии обязателен: без него тот же запрос отвергается.
    async with httpx.AsyncClient(timeout=20) as client:
        bare = await client.get(
            f"https://api.gitverse.ru/repos/{_LIVE_REPO}",
            headers={"Authorization": f"Bearer {_LIVE_TOKEN}"},
        )
    assert bare.status_code == 400, (
        "если это перестало быть 400 — версия перестала быть обязательной, "
        "и комментарии про заголовок в клиенте пора переписать"
    )

    assert await forge.has_workflows(gh_repo=_LIVE_REPO) in (True, False, None)


# ---------------------------------------------------------------------------
# Черновики: у GitVerse они ЕСТЬ, и снимаются только через заголовок
# ---------------------------------------------------------------------------


async def test_draft_is_read_from_the_pull_request(patched_httpx, monkeypatch):
    """Черновик читается полем is_draft, а не объявляется отсутствующим.

    Первая редакция этого адаптера отвечала жёстким False с обоснованием
    «черновиков у форжа нет». Документация говорит обратное: есть чекбокс
    «Черновик», префикс Draft: в заголовке и поле is_draft в ответе — а
    ВЛИТЬ черновик нельзя. То есть жёсткий False воспроизводил бы #1053:
    гейт пошёл бы мержить черновик и получил бы отказ без диагноза.
    """
    seen, responses = patched_httpx
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    forge = GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")

    responses.append(httpx.Response(200, json={"is_draft": True}))
    assert await forge.pr_is_draft(1, gh_repo="own/rep") is True

    responses.append(httpx.Response(200, json={"is_draft": False}))
    assert await forge.pr_is_draft(1, gh_repo="own/rep") is False

    # Не посмотрели — не обвиняем (#498): мерж всё равно попробуют.
    responses.extend([httpx.Response(500)] * 3)
    assert await forge.pr_is_draft(1, gh_repo="own/rep") is False


async def test_ready_strips_the_draft_prefix_and_verifies(patched_httpx):
    """Готовность подтверждается повторным чтением, а не кодом PATCH.

    Снятие отметки — побочный эффект правки заголовка, и утверждать, что он
    случился, можно только увидев его.
    """
    seen, responses = patched_httpx
    forge = GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")
    responses.extend(
        [
            httpx.Response(200, json={"is_draft": True, "title": "Draft: форж"}),
            httpx.Response(200, json={}),
            httpx.Response(200, json={"is_draft": False, "title": "форж"}),
        ]
    )

    assert await forge.mark_pr_ready(1, gh_repo="own/rep") is True
    patched = [r for r in seen if r.method == "PATCH"]
    assert len(patched) == 1
    assert b'"title":"\xd1\x84\xd0\xbe\xd1\x80\xd0\xb6"' in patched[0].content.replace(
        b" ", b""
    )


async def test_checkbox_draft_is_refused_not_faked(patched_httpx):
    """Черновик без префикса в заголовке API снять НЕ может — и говорит это.

    Ложное True здесь дороже отказа: гейт пошёл бы мержить черновик и
    получил бы отказ без диагноза — ровно то, что чинил #1053.
    """
    seen, responses = patched_httpx
    forge = GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")
    responses.append(
        httpx.Response(200, json={"is_draft": True, "title": "Форж без префикса"})
    )

    assert await forge.mark_pr_ready(1, gh_repo="own/rep") is False
    assert not [r for r in seen if r.method == "PATCH"], (
        "нечего править — и запрос не отправляется"
    )


async def test_ready_pr_needs_no_conversion(patched_httpx):
    """Не черновик — переводить нечего, и лишних запросов не делается."""
    seen, responses = patched_httpx
    forge = GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")
    responses.append(httpx.Response(200, json={"is_draft": False, "title": "форж"}))

    assert await forge.mark_pr_ready(1, gh_repo="own/rep") is True
    assert len(seen) == 1


async def test_ready_is_not_claimed_when_the_flag_survives(patched_httpx):
    """PATCH прошёл, а отметка осталась — это НЕ готовность.

    Тест написан после мутационной проверки: счастливый путь один не
    отличает «проверили результат» от «предположили его», потому что там
    повторное чтение всё равно вернуло бы то, что нужно. Здесь форж
    принимает правку заголовка, но черновиком быть не перестаёт — и метод
    обязан ответить False. Иначе гейт пойдёт мержить черновик, а отказ
    придёт тем же булевым, что конфликт и отозванный токен (#1053).
    """
    seen, responses = patched_httpx
    forge = GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")
    responses.extend(
        [
            httpx.Response(200, json={"is_draft": True, "title": "Draft: форж"}),
            httpx.Response(200, json={}),
            httpx.Response(200, json={"is_draft": True, "title": "форж"}),
        ]
    )

    assert await forge.mark_pr_ready(1, gh_repo="own/rep") is False
