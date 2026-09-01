"""GitVerse как форж: REST-клиент к api.gitverse.ru (#1115, эпик #1112).

CLI вроде ``gh`` у GitVerse нет — единственный вход это HTTP API. Форма
путей повторяет GitHub (``/repos/{owner}/{repo}/pulls``), но три вещи
отличаются, и каждая уже стоила бы часа отладки:

1. Заголовок версии обязателен на КАЖДОМ запросе. Без него сервер отвечает
   400 с пустым телом — ответ, неотличимый от проблемы с авторизацией.
   Поэтому он ставится в одном месте и приделан к транспорту, а не к
   вызывающим: забыть его в одном методе из семнадцати слишком легко.
2. Токен нужен и для ПУБЛИЧНЫХ репозиториев: анонимного режима нет (401).
3. Мержа в публичном API нет вовсе. Есть только ``GET .../merge`` со
   смыслом «влит ли уже» (204 да / 404 нет). Сам мерж — задача #1116.
4. Черновики У ФОРЖА ЕСТЬ, и снять черновик через API можно только косвенно
   — сняв префикс ``Draft:`` с заголовка. Поле ``is_draft`` на запись
   недоступно. Подробности у ``mark_pr_ready``.

Проверено живыми запросами 31.08.2026 против настоящего репозитория.
Документация: https://gitverse.ru/docs/developers/public-api
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from hub import config
from hub.integrations.protocols import (
    CIProbeOutcome,
    CIProbeResult,
    MergeabilityOutcome,
)

log = logging.getLogger(__name__)

_TIMEOUT = 30.0
#: Сколько раз повторить запрос, который сервер попросил повторить.
_RETRIES = 2

#: Причины, которые адаптер называет, пока соответствующая задача не сделана.
#: Отказ по имени, а не молчаливый ``False``: преждевременно заведённый
#: GitVerse-проект должен встать громко и с указанием, чего именно не хватает.
_MERGE_PENDING = "мерж на GitVerse ещё не реализован — задача #1116"


class GitVerseResponse:
    """Ответ, у которого различимы «не смогли» и «сервер сказал нет».

    ``ok`` False со ``status`` — сервер ответил и отказал; ``status`` None —
    до сервера не дошли. Это разные диагнозы и разные руки, и клиент,
    сливающий их в ``None``, лишает вызывающего возможности их различить —
    ровно дефект #419 в новых декорациях.
    """

    __slots__ = ("status", "data", "text", "reason")

    def __init__(
        self,
        status: int | None,
        data: Any = None,
        text: str = "",
        reason: str = "",
    ) -> None:
        self.status = status
        self.data = data
        self.text = text
        self.reason = reason

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


class GitVerseForge:
    """Concrete forge plugin backed by the GitVerse REST API."""

    name = "gitverse"
    #: Мержа в публичном API GitVerse нет вовсе — сливать обязан вызывающий,
    #: локальным git (#1116).
    can_merge_via_api = False

    def __init__(
        self,
        token: str | None = None,
        base_url: str | None = None,
        version: str | None = None,
    ) -> None:
        self._token = (token if token is not None else config.GITVERSE_TOKEN).strip()
        self._base = (base_url or config.GITVERSE_API_URL).rstrip("/")
        self._version = (version or config.GITVERSE_API_VERSION).strip()

    # -- транспорт ----------------------------------------------------------

    def _is_configured(self) -> bool:
        return bool(self._token)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": f"application/vnd.gitverse.object+json;version={self._version}",
            "Authorization": f"Bearer {self._token}",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = _TIMEOUT,
        keep_text: bool = False,
    ) -> GitVerseResponse:
        """Один запрос с ретраями там, где они уместны, и только там.

        ``keep_text`` отменяет обрезку тела. По умолчанию тело режется до 300
        символов, потому что почти везде оно нужно ЛИШЬ для диагностики, и
        целиком оно только раздувает логи. Ровно одно исключение — логи
        упавшего джоба (#1117): там тело и есть ответ, и обрезка превратила бы
        max_log_chars у вызывающего в украшение.

        Ретраится 429 (сервер сам просит подождать) и 5xx (его сторона). 4xx
        не ретраится вовсе: повторять запрос, который отвергли по существу,
        значит жечь квоту и оттягивать момент, когда причина будет названа.
        """
        if not self._is_configured():
            return GitVerseResponse(None, reason="gitverse_token_not_configured")

        url = f"{self._base}{path}"
        delay = 1.0
        last = GitVerseResponse(None, reason="gitverse_no_attempt")
        for attempt in range(_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.request(
                        method,
                        url,
                        json=json_body,
                        params=params,
                        headers=self._headers(),
                    )
            except Exception as exc:  # noqa: BLE001 - degradation is the contract
                # Тип исключения, а не его текст: httpx кладёт в сообщение URL,
                # а тот приходит из вызывающего кода.
                last = GitVerseResponse(
                    None, reason=f"gitverse_transport_error:{type(exc).__name__}"
                )
                log.warning(
                    "gitverse %s %s failed: %s", method, path, type(exc).__name__
                )
                if attempt < _RETRIES:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                return last

            self._note_rate_limit(resp)
            if resp.status_code == 429 or resp.status_code >= 500:
                last = GitVerseResponse(
                    resp.status_code,
                    text=resp.text[:300],
                    reason=f"gitverse_http_{resp.status_code}",
                )
                if attempt < _RETRIES:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                return last

            # 400 у этого API приходит с ПУСТЫМ телом, и это самый частый
            # ответ на забытый или неверный заголовок версии. Проверка стоит
            # ДО разбора тела, а не в обработчике ошибки разбора: пустое тело
            # нечего разбирать, и подсказка, спрятанная в except, не сработала
            # бы ровно в том случае, ради которого написана.
            if resp.status_code == 400 and not resp.content:
                return GitVerseResponse(
                    400,
                    text="",
                    reason=(
                        "gitverse_http_400_empty_body"
                        " (проверьте заголовок Accept с версией)"
                    ),
                )

            data: Any = None
            # При keep_text разбор JSON не делается ВОВСЕ, а не «делается и
            # прощается». Логи джоба приходят простым текстом, и попытка
            # разобрать их уводила ответ в ветку «невалидный JSON», где тело
            # режется независимо от флага, — то есть флаг молча не работал
            # ровно там, ради чего заведён. Поймано мутационной проверкой.
            if resp.content and not keep_text:
                try:
                    data = resp.json()
                except ValueError:
                    return GitVerseResponse(
                        resp.status_code,
                        text=resp.text[:300],
                        reason="gitverse_invalid_json",
                    )
            reason = (
                ""
                if 200 <= resp.status_code < 300
                else (f"gitverse_http_{resp.status_code}")
            )
            return GitVerseResponse(
                resp.status_code,
                data=data,
                text=resp.text if keep_text else resp.text[:300],
                reason=reason,
            )
        return last

    @staticmethod
    def _note_rate_limit(resp: httpx.Response) -> None:
        """Предупредить до того, как квота кончится посреди доставки.

        Сервер сам сообщает остаток; читать его дешевле, чем разбираться
        задним числом, почему цикл поллера вдруг весь стал ``unavailable``.
        """
        raw = resp.headers.get("gitverse-ratelimit-user-remaining")
        if raw is None:
            return
        try:
            left = int(raw)
        except ValueError:
            return
        if left <= 100:
            log.warning(
                "gitverse rate limit is running out: %s requests left this hour", left
            )

    def _repo(self, gh_repo: str | None) -> str:
        return (gh_repo or config.REPO_NAME or "").strip("/")

    # -- pull requests ------------------------------------------------------

    async def create_pr(
        self,
        title: str,
        body: str,
        branch: str,
        base: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> int | None:
        slug = self._repo(gh_repo)
        if not slug:
            log.error("gitverse create_pr: репозиторий не назван")
            return None
        resp = await self._request(
            "POST",
            f"/repos/{slug}/pulls",
            json_body={"title": title, "body": body, "head": branch, "base": base},
        )
        if resp.ok and isinstance(resp.data, dict):
            number = resp.data.get("number")
            if isinstance(number, int):
                log.info("Created GitVerse PR #%d for branch %s", number, branch)
                return number
        # PR на эту ветку мог существовать до нас — это не ошибка, а тот же
        # ответ, что у GitHub-адаптера на "already exists".
        existing = await self.pr_for_branch(branch, repo=repo, gh_repo=gh_repo)
        if existing is not None:
            return existing
        log.error(
            "gitverse create_pr failed for %s: %s %s",
            branch,
            resp.reason or resp.status,
            resp.text,
        )
        return None

    async def pr_for_branch(
        self, branch: str, *, repo: str | None = None, gh_repo: str | None = None
    ) -> int | None:
        slug = self._repo(gh_repo)
        if not slug:
            return None
        resp = await self._request(
            "GET", f"/repos/{slug}/pulls", params={"state": "open"}
        )
        if not resp.ok or not isinstance(resp.data, list):
            return None
        for pr in resp.data:
            if not isinstance(pr, dict):
                continue
            head = pr.get("head")
            ref = head.get("ref") if isinstance(head, dict) else None
            if str(ref or "") == branch and isinstance(pr.get("number"), int):
                return int(pr["number"])
        return None

    async def open_or_update_pr(
        self,
        base: str,
        head: str,
        title: str,
        body: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> int | None:
        """Найти и обновить, иначе создать — идемпотентно, как у GitHub (#812)."""
        slug = self._repo(gh_repo)
        if not slug:
            return None
        existing = await self.pr_for_branch(head, repo=repo, gh_repo=gh_repo)
        if existing is not None:
            await self._request(
                "PATCH",
                f"/repos/{slug}/pulls/{existing}",
                json_body={"title": title, "body": body},
            )
            return existing
        return await self.create_pr(title, body, head, base, repo=repo, gh_repo=gh_repo)

    async def _pr(self, pr_number: int, gh_repo: str | None) -> GitVerseResponse:
        slug = self._repo(gh_repo)
        if not slug:
            return GitVerseResponse(None, reason="gitverse_repo_not_named")
        return await self._request("GET", f"/repos/{slug}/pulls/{pr_number}")

    async def pr_state(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> str:
        """ "open" | "merged" | "closed" | "absent" | "" — как у GitHub-адаптера.

        Пустая строка означает «спросить не удалось» и никогда не читается как
        ответ: «не посмотрели» и «закрыт» ведут к противоположным решениям о
        доставке (#802, правило #725). 404 — это ОТВЕТ: такого PR в этом
        репозитории нет (#959).
        """
        resp = await self._pr(pr_number, gh_repo)
        if resp.status == 404:
            return "absent"
        if not resp.ok or not isinstance(resp.data, dict):
            return ""
        # У GitVerse, как и у Gitea, влитый PR остаётся в состоянии "closed" и
        # отличается флагом. Читать одно поле state было бы неверно: доставка
        # прочла бы влитый PR как закрытый вручную.
        if resp.data.get("merged") is True or resp.data.get("merged_at"):
            return "merged"
        return str(resp.data.get("state") or "").lower()

    async def pr_is_draft(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> bool:
        """Считает ли GitVerse этот PR черновиком (поле ``is_draft``).

        Черновики у GitVerse ЕСТЬ, и это важно ровно по той причине, по
        которой важно было на GitHub (#1053): влить черновик нельзя, а отказ
        мержа приходит тем же самым булевым, что и конфликт или отозванный
        токен — и отправляет задачу в needs_decision с неверным диагнозом.

        False при неудачном запросе — правило #498: молчание не обвинение, и
        попытка мержа всё равно состоится. Отличать «не черновик» от «не
        посмотрели» здесь незачем: оба ведут к одному действию.
        """
        resp = await self._pr(pr_number, gh_repo)
        if not resp.ok or not isinstance(resp.data, dict):
            log.info(
                "PR #%d draft probe unavailable: %s",
                pr_number,
                resp.reason or resp.status,
            )
            return False
        return bool(resp.data.get("is_draft"))

    async def mark_pr_ready(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> bool:
        """Снять с PR статус черновика. Одобрение хабом и есть сигнал (#1053).

        Прямого способа нет: ``is_draft`` на запись недоступен, PATCH
        принимает только title, body, state, base и maintainer_can_modify.
        Документированный обходной путь один — снять префикс ``Draft:`` с
        заголовка, после чего форж снимает отметку сам.

        Отсюда случай, в котором API бессилен: черновик, выставленный
        ЧЕКБОКСОМ, а не префиксом. Тогда заголовок править нечего, и метод
        отвечает False с названной причиной — а не True, потому что «мы
        что-то отправили». Ложное True здесь дороже отказа: гейт пошёл бы
        мержить черновик и получил бы отказ без диагноза.

        Результат сверяется повторным чтением, а не выводится из кода PATCH:
        снятие отметки — побочный эффект правки заголовка, и утверждать, что
        он случился, можно только увидев его.
        """
        slug = self._repo(gh_repo)
        if not slug:
            return False
        resp = await self._pr(pr_number, gh_repo)
        if not resp.ok or not isinstance(resp.data, dict):
            return False
        if not resp.data.get("is_draft"):
            return True  # уже готов — переводить нечего

        title = str(resp.data.get("title") or "")
        stripped = re.sub(r"^\s*draft:\s*", "", title, count=1, flags=re.IGNORECASE)
        if stripped == title or not stripped:
            log.warning(
                "PR #%d: черновик выставлен не префиксом заголовка — "
                "снять его через API нельзя, нужен человек в вебе",
                pr_number,
            )
            return False

        patch = await self._request(
            "PATCH", f"/repos/{slug}/pulls/{pr_number}", json_body={"title": stripped}
        )
        if not patch.ok:
            log.warning(
                "PR #%d: заголовок не удалось поправить: %s",
                pr_number,
                patch.reason or patch.status,
            )
            return False

        again = await self._pr(pr_number, gh_repo)
        if not again.ok or not isinstance(again.data, dict):
            return False
        return not again.data.get("is_draft")

    async def pr_head_sha(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> str:
        resp = await self._pr(pr_number, gh_repo)
        if not resp.ok or not isinstance(resp.data, dict):
            return ""
        head = resp.data.get("head")
        if not isinstance(head, dict):
            return ""
        return str(head.get("sha") or "").strip()

    async def pr_refs(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> tuple[str, str]:
        resp = await self._pr(pr_number, gh_repo)
        if not resp.ok or not isinstance(resp.data, dict):
            return ("", "")
        base = resp.data.get("base")
        head = resp.data.get("head")
        return (
            str((base or {}).get("ref") or "").strip()
            if isinstance(base, dict)
            else "",
            str((head or {}).get("ref") or "").strip()
            if isinstance(head, dict)
            else "",
        )

    async def _pr_merged(
        self, pr_number: int, *, gh_repo: str | None = None
    ) -> bool | None:
        """Влит ли PR — по единственному endpoint'у, который об этом говорит.

        Приватный намеренно: у операции пока нет потребителя, а ForgePlugin
        описывает то, что хаб СПРАШИВАЕТ, а не всё, что адаптер умеет. #1116
        поднимет её в протокол вместе с реализацией мержа — тогда и GitHub, и
        noop обязаны будут на неё ответить, и это будет осмысленно.

        ``GET /repos/{owner}/{repo}/pulls/{n}/merge``: 204 — влит, 404 — нет.
        Третий ответ (None) означает «спросить не удалось», и он обязан
        оставаться отличимым: доставка в #1116 признаёт мерж состоявшимся
        только по 204, а не по коду возврата push.
        """
        slug = self._repo(gh_repo)
        if not slug:
            return None
        resp = await self._request("GET", f"/repos/{slug}/pulls/{pr_number}/merge")
        if resp.status == 204:
            return True
        if resp.status == 404:
            return False
        return None

    async def merge_commit_sha(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> str:
        resp = await self._pr(pr_number, gh_repo)
        if not resp.ok or not isinstance(resp.data, dict):
            return ""
        return str(resp.data.get("merge_commit_sha") or "").strip()

    async def pr_mergeability(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> tuple[MergeabilityOutcome, str]:
        """Сводится в #1116 — здесь отказ, который называет задачу.

        Отвечать ``conflicting`` или ``mergeable`` без проверки нельзя: первое
        подняло бы ложную тревогу, второе пустило бы доставку вслепую.
        ``unavailable`` — единственный честный ответ, пока проверки нет.
        """
        return (MergeabilityOutcome.unavailable, _MERGE_PENDING)

    async def merge_pr(
        self,
        pr_number: int,
        subject: str,
        *,
        delete_branch: bool = True,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool:
        """Форж слить не может — и говорит это, а не молчит (#1116).

        ``can_merge_via_api = False`` объявлено на классе, и вызывающий обязан
        читать его ДО вызова: мерж делается локальным git в ``git_ops``. Если
        управление дошло сюда, значит кто-то положился на попытку вместо
        объявленной способности — и узнать об этом лучше по строке в логе, чем
        по молча недоставленной задаче.
        """
        log.error(
            "gitverse merge_pr вызван, хотя can_merge_via_api=False: "
            "мерж на этом форже делается локальным git (#1116)"
        )
        return False

    async def close_pr(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> bool:
        """Закрыть PR, ничего не вливая.

        Обязателен именно здесь: измерено 01.09.2026, что GitVerse НЕ замечает
        мержа пушем — после merge --no-ff головы в базу PR остаётся open,
        merged=False, merge_commit_sha=None. Незакрытый PR висел бы открытым
        вечно, а pr_for_branch находил бы его на уже доставленной ветке и
        заставлял гейт открывать доставку заново.
        """
        slug = self._repo(gh_repo)
        if not slug:
            return False
        resp = await self._request(
            "PATCH", f"/repos/{slug}/pulls/{pr_number}", json_body={"state": "closed"}
        )
        if resp.ok:
            return True
        log.warning("PR #%d не закрыт: %s", pr_number, resp.reason or resp.status)
        return False

    async def branch_contains(
        self,
        branch: str,
        sha: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool | None:
        """Достижим ли ``sha`` в ``branch`` на remote, или None.

        Это и есть доказательство доставки на GitVerse, потому что через PR
        его получить нельзя ни при каком условии: доставленный и брошенный PR
        отвечают одинаково (closed, merged=False), а до закрытия — вообще
        одинаково с недоставленным (open, /merge → 404).

        Спрашивается у compare, а не у списка коммитов: список пришлось бы
        листать страницами и решать, где остановиться, а «не нашли на первых
        ста» неотличимо от «нет вовсе».
        """
        slug = self._repo(gh_repo)
        if not slug or not sha:
            return None
        resp = await self._request("GET", f"/repos/{slug}/compare/{sha}...{branch}")
        if not resp.ok or not isinstance(resp.data, dict):
            return None
        status = str(resp.data.get("status") or "").strip().lower()
        if status in ("ahead", "identical"):
            return True
        if status in ("behind", "diverged"):
            return False
        # Незнакомое слово — не повод сказать «нет»: это «спросить не удалось».
        log.warning("gitverse compare вернул нераспознанный status=%r", status)
        return None

    # -- CI -----------------------------------------------------------------
    #
    # Единственный источник CI-факта у GitVerse — Actions runs: ни commit
    # statuses, ни check-runs у него нет. Формы ниже сняты с живого
    # mrpda/snip-portal 01.09.2026 (шесть настоящих прогонов), а не взяты из
    # документации, и отличаются от GitHub тремя способами:
    #
    #   1. Поля ``conclusion`` НЕТ ВОВСЕ. Исход несёт сам ``status``, и
    #      наблюдались значения "success" и "failure". То есть status у
    #      GitVerse — это conclusion у GitHub, а не его status.
    #   2. ``ref`` приходит ПОЛНЫМ: "refs/heads/main", а не "main".
    #      Сравнение с именем ветки в лоб даёт пустоту, неотличимую от
    #      «прогонов нет».
    #   3. Голова называется по-разному в соседних endpoint'ах: у прогона
    #      ``commit_sha``, у джоба ``head_sha``.

    #: Значения ``status``, наблюдённые у ЗАВЕРШЁННЫХ прогонов.
    _RUN_SUCCESS = ("success",)
    _RUN_FAILURE = ("failure", "cancelled", "timed_out", "error")
    #: Значения, при которых прогон ещё идёт. Список — предположение по
    #: аналогии с GitHub: у живых прогонов такое состояние не наблюдалось ни
    #: разу, все шесть были завершены. Поэтому он НЕ страховочный: всё, чего
    #: здесь нет и нет в двух списках выше, уходит в unavailable вместе с
    #: самим значением, а не в pending. Ошибиться в сторону «ждём» страшнее:
    #: гейт будет ждать вечно, и никто не узнает, почему.
    _RUN_RUNNING = ("waiting", "running", "queued", "pending", "in_progress")

    @staticmethod
    def _short_ref(ref: str) -> str:
        """ "refs/heads/main" → "main". Всё прочее — как пришло."""
        return ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref

    async def _runs(
        self,
        *,
        gh_repo: str | None,
        branch: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]] | None:
        """Прогоны репозитория, новые первыми, или None если спросить не вышло."""
        slug = self._repo(gh_repo)
        if not slug:
            return None
        params: dict[str, Any] = {"per_page": max(1, min(limit, 100))}
        if branch:
            params["branch"] = branch
        resp = await self._request("GET", f"/repos/{slug}/actions/runs", params=params)
        if not resp.ok or not isinstance(resp.data, dict):
            return None
        runs = resp.data.get("workflow_runs")
        return runs if isinstance(runs, list) else None

    def _outcome_of(self, runs: list[dict[str, Any]]) -> CIProbeResult:
        """Свести набор прогонов в один исход, не приукрашивая незнание.

        Порядок проверок тот же, что у GitHub-адаптера, и он не произволен:
        сначала «ещё идёт», потом «упало», и только если ни одного из них —
        «прошло». Иначе один зелёный прогон рядом с красным дал бы зелёный
        ответ.
        """
        statuses = [str(r.get("status") or "").strip().lower() for r in runs]
        if any(s in self._RUN_RUNNING for s in statuses):
            return CIProbeResult(CIProbeOutcome.pending, "gitverse_runs_running")
        if any(s in self._RUN_FAILURE for s in statuses):
            return CIProbeResult(CIProbeOutcome.failed, "gitverse_runs_failed")
        if statuses and all(s in self._RUN_SUCCESS for s in statuses):
            return CIProbeResult(CIProbeOutcome.passed, "gitverse_runs_passed")
        # Нераспознанное значение — НЕ повод сказать «ждём» или «прошло».
        # Само значение уходит в details: без него следующий, кто это увидит,
        # начнёт с того же перебора, с которого начинал я.
        return CIProbeResult(
            CIProbeOutcome.unavailable,
            "gitverse_runs_unknown_state",
            details=",".join(sorted(set(statuses))) or "нет прогонов",
        )

    async def _absent_or_missing_run(
        self, reason: str, *, gh_repo: str | None, sha: str
    ) -> CIProbeResult:
        """Отличить «в репозитории нет CI» от «для этого коммита ещё нет прогона».

        Схлопывание этих двух — дефект #419: первое означает, что доставке
        нечего ждать, второе — что ждать надо ещё немного.
        """
        has = await self.has_workflows(gh_repo=gh_repo)
        if has is None:
            return CIProbeResult(
                CIProbeOutcome.unavailable, "gitverse_workflows_unavailable"
            )
        if not has:
            return CIProbeResult(CIProbeOutcome.absent, reason)
        return CIProbeResult(CIProbeOutcome.missing_run, reason, details=sha or None)

    async def check_pr_ci(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> CIProbeResult:
        """Исход CI для головы этого PR.

        Отбор идёт по ``commit_sha``, а не по первому прогону в списке:
        порядок — это «когда запустили», а вопрос — «что с ЭТИМ коммитом».
        Ветка PR берётся у самого PR, потому что ветвь запроса могла
        разойтись с тем, что думает вызывающий.
        """
        head_sha = await self.pr_head_sha(pr_number, repo=repo, gh_repo=gh_repo)
        if not head_sha:
            return CIProbeResult(
                CIProbeOutcome.unavailable,
                "gitverse_head_sha_unavailable",
                details=f"PR #{pr_number}",
            )
        _base, head_ref = await self.pr_refs(pr_number, repo=repo, gh_repo=gh_repo)
        runs = await self._runs(gh_repo=gh_repo, branch=head_ref)
        if runs is None:
            return CIProbeResult(
                CIProbeOutcome.unavailable, "gitverse_runs_unavailable"
            )
        mine = [r for r in runs if str(r.get("commit_sha") or "") == head_sha]
        if not mine:
            return await self._absent_or_missing_run(
                "no_workflow_runs", gh_repo=gh_repo, sha=head_sha
            )
        return self._outcome_of(mine)

    async def branch_ci_runs(
        self,
        branch: str,
        limit: int = 20,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """Прогоны ветки, новые первыми, или None если спросить не вышло (#929).

        None, а не пустой список: «прогонов нет» и «не смогли посмотреть»
        ведут к противоположным выводам о том, зелёная ли база (#725).

        Форма приводится к той, которую уже ждут потребители, и здесь же
        чинится главное расхождение: у GitVerse нет ``conclusion``, а исход
        лежит в ``status``. Заполняем ОБА поля одним значением — потребитель
        не должен знать, на каком форже он сейчас.
        """
        runs = await self._runs(gh_repo=gh_repo, branch=branch, limit=limit)
        if runs is None:
            return None
        out: list[dict[str, Any]] = []
        for r in runs:
            status = str(r.get("status") or "").strip().lower()
            out.append(
                {
                    "sha": str(r.get("commit_sha") or ""),
                    # Завершённый прогон у GitVerse несёт исход в status;
                    # потребители читают conclusion — отдаём им обе клетки.
                    "status": (
                        "in_progress" if status in self._RUN_RUNNING else "completed"
                    ),
                    "conclusion": ("" if status in self._RUN_RUNNING else status),
                    "created_at": str(r.get("started") or ""),
                    "name": str(r.get("name") or ""),
                }
            )
        return out

    async def ci_failure_logs(
        self,
        pr_number: int,
        branch: str,
        max_log_chars: int = 12000,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> dict[str, Any]:
        """Имена упавших джобов и хвост их логов.

        Пустой ответ здесь означает «не нашли, что показать», и это не
        обвинение: вызывающий и так знает, что CI красный — он для того и
        пришёл. Поэтому ни одна ветка не поднимает исключение.
        """
        result: dict[str, Any] = {"failed_checks": [], "log_summary": "", "run_url": ""}
        slug = self._repo(gh_repo)
        if not slug:
            return result
        runs = await self._runs(gh_repo=gh_repo, branch=branch, limit=20)
        if not runs:
            return result

        # Прогон ГОЛОВЫ этого PR, а не первый в списке ветки. На ветке с
        # историей первым лежит самый свежий прогон — возможно, чужого
        # коммита, — и человек чинил бы по логам чужого падения. Порядок в
        # списке отвечает на «когда запустили», а вопрос здесь — «что упало
        # у ЭТОГО коммита».
        head_sha = await self.pr_head_sha(pr_number, repo=repo, gh_repo=gh_repo)
        mine = [r for r in runs if str(r.get("commit_sha") or "") == head_sha]
        if not mine:
            # Головы не знаем или прогонов на неё нет: показать чужой лог
            # хуже, чем не показать никакого — он уведёт чинить не то.
            return result
        failed_runs = [
            r
            for r in mine
            if str(r.get("status") or "").strip().lower() in self._RUN_FAILURE
        ]

        # ПОСЛЕДНИЙ по времени запуска, а не первый в списке. Один коммит даёт
        # несколько прогонов сплошь и рядом — перезапуск, правка workflow,
        # флейк, — и чинить надо по свежему логу: старый может относиться к
        # уже исправленному шагу. Живой API отдаёт новые первыми, но опираться
        # на это нельзя: порядок нигде не обещан, а цена ошибки — человек,
        # который час чинит то, что уже починено.
        def _started(entry: dict[str, Any]) -> str:
            return str(entry.get("started") or "")

        run = max(failed_runs or mine, key=_started)
        run_id = run.get("id")
        if run_id is None:
            return result
        result["run_url"] = f"https://gitverse.ru/{slug}/actions/runs/{run_id}"

        jobs_resp = await self._request(
            "GET", f"/repos/{slug}/actions/runs/{run_id}/jobs"
        )
        jobs = []
        if jobs_resp.ok and isinstance(jobs_resp.data, dict):
            raw = jobs_resp.data.get("jobs")
            jobs = raw if isinstance(raw, list) else []
        failed = [
            j
            for j in jobs
            if str(j.get("status") or "").strip().lower() in self._RUN_FAILURE
        ]
        result["failed_checks"] = [str(j.get("name") or "") for j in failed]

        chunks: list[str] = []
        for job in failed:
            job_id = job.get("id")
            if job_id is None:
                continue
            log_resp = await self._request(
                "GET",
                f"/repos/{slug}/actions/jobs/{job_id}/logs",
                timeout=60.0,
                keep_text=True,
            )
            if log_resp.ok and log_resp.text:
                chunks.append(f"--- {job.get('name')} ---\n{log_resp.text}")
        summary = "\n".join(chunks)
        if len(summary) > max_log_chars:
            summary = "... (truncated) ...\n" + summary[-max_log_chars:]
        result["log_summary"] = summary
        return result

    async def has_workflows(
        self, *, repo: str | None = None, gh_repo: str | None = None
    ) -> bool | None:
        """Есть ли в репозитории workflow — спрашивается у API, не у каталога.

        Раннер GitVerse обрабатывает и ``.gitverse/workflows/``, и
        ``.github/workflows/``, так что вопрос «есть ли тут CI», заданный
        одному каталогу, отвечается неверно.

        ИЗМЕРЕНО 31.08.2026 на трёх репозиториях, и ответ зависит от их
        состояния, а не только от прав:
        - обычный репозиторий (mrpda/snip-portal, 3 ветки) — 200 и
          ``{"total_count": 1, "workflows": [...]}``;
        - ПУСТОЙ репозиторий без веток (mrpda/hub) — 500;
        - репозиторий без прав администратора у токена — 404.

        То есть endpoint рабочий, и оба нерабочих случая честно сводятся к
        ``None`` — «спросить не удалось». Выдавать их за ``False`` нельзя:
        это сказало бы «CI тут нет» про репозиторий, где никто не смотрел.
        """
        slug = self._repo(gh_repo)
        if not slug:
            return None
        resp = await self._request("GET", f"/repos/{slug}/actions/workflows")
        if not resp.ok:
            return None
        payload = resp.data
        if isinstance(payload, list):
            return bool(payload)
        if not isinstance(payload, dict):
            return None
        workflows = payload.get("workflows") or payload.get("items")
        if isinstance(workflows, list):
            return bool(workflows)
        try:
            return int(payload.get("total_count") or 0) > 0
        except (TypeError, ValueError):
            return None

    # -- release ------------------------------------------------------------

    async def compare_subjects(
        self,
        base: str,
        head: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[str]:
        """Заголовки коммитов, которые ``head`` несёт сверх ``base``, новые первыми.

        Заголовок режется здесь по первой строке — у GitVerse нет jq-параметра,
        которым это делает GitHub-адаптер. Резать надо именно по КОММИТУ, а не
        по строкам всего текста: многострочное сообщение иначе считается за
        несколько коммитов, и релиз приписывает себе чужие задачи (#963).
        """
        slug = self._repo(gh_repo)
        if not slug:
            return []
        resp = await self._request("GET", f"/repos/{slug}/compare/{base}...{head}")
        if not resp.ok or not isinstance(resp.data, dict):
            return []
        commits = resp.data.get("commits")
        if not isinstance(commits, list):
            return []
        subjects: list[str] = []
        for item in commits:
            if not isinstance(item, dict):
                continue
            commit = item.get("commit")
            message = (commit or {}).get("message") if isinstance(commit, dict) else ""
            subject = str(message or "").split("\n", 1)[0].strip()
            if subject:
                subjects.append(subject)
        subjects.reverse()
        return subjects

    async def merge_branches(
        self,
        into_branch: str,
        from_branch: str,
        message: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> tuple[str, str]:
        """Сводится в #1116: серверного мержа веток у GitVerse тоже нет."""
        return ("unavailable", _MERGE_PENDING)
