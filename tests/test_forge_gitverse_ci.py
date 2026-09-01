"""CI-проба GitVerse через Actions API (#1117, эпик #1112).

Ответы в тестах — не выдумка и не пересказ документации: формы сняты с живого
mrpda/snip-portal 01.09.2026, где шесть настоящих прогонов. Три отличия от
GitHub, каждое из которых ломает наивный перенос кода:

  * поля ``conclusion`` НЕТ вовсе, исход несёт сам ``status``;
  * ``ref`` приходит полным — "refs/heads/main", не "main";
  * голова называется ``commit_sha`` у прогона и ``head_sha`` у джоба.
"""

from __future__ import annotations

import httpx
import pytest

from hub.integrations.forge.gitverse import GitVerseForge
from hub.integrations.protocols import CIProbeOutcome

TOKEN = "test-token-not-a-real-secret"
# НЕ настоящий sha и намеренно не hex: сорок шестнадцатеричных знаков
# сканер секретов читает как «строку с высокой энтропией» и красит CI.
# Тестам форма коммита безразлична — они сравнивают строки на равенство.
HEAD = "head-commit-of-the-pull-request"


@pytest.fixture
def patched_httpx(monkeypatch):
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


def _forge() -> GitVerseForge:
    return GitVerseForge(token=TOKEN, base_url="https://api.example", version="1")


def _run(sha: str, status: str, ref: str = "refs/heads/task-1/x") -> dict:
    """Прогон в той форме, в какой его отдаёт живой GitVerse."""
    return {
        "id": 1443246,
        "name": "ci.yaml",
        "commit_sha": sha,
        "event": "push",
        "ref": ref,
        "started": "2026-08-31T22:37:55Z",
        "status": status,
        "title": "работа",
    }


def _pr(sha: str = HEAD) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "number": 7,
            "head": {"ref": "task-1/x", "sha": sha},
            "base": {"ref": "main"},
        },
    )


def _runs(*items: dict) -> httpx.Response:
    return httpx.Response(
        200, json={"total_count": len(items), "workflow_runs": list(items)}
    )


# ---------------------------------------------------------------------------
# Исход по коммиту
# ---------------------------------------------------------------------------


async def test_probe_matches_run_by_head_sha(patched_httpx):
    """AC-1. Берётся прогон ИМЕННО этого коммита, а не первый в списке.

    Порядок в списке — это «когда запустили», а вопрос — «что с головой PR».
    Свежий прогон соседнего коммита ответил бы за чужую работу.
    """
    seen, responses = patched_httpx
    responses.extend(
        [
            _pr(),  # pr_head_sha
            _pr(),  # pr_refs
            _runs(_run("другой-коммит", "failure"), _run(HEAD, "success")),
        ]
    )

    probe = await _forge().check_pr_ci(7, gh_repo="own/rep")

    assert probe.outcome is CIProbeOutcome.passed
    # И спрашивали по ветке PR, а не по чему-то ещё. Сравниваем разобранный
    # параметр, а не строку URL: слэш в имени ветки уезжает в %2F, и проверка
    # подстрокой краснела бы на верном коде.
    assert seen[-1].url.params.get("branch") == "task-1/x"


async def test_status_carries_the_verdict_because_conclusion_does_not_exist(
    patched_httpx,
):
    """У прогона нет conclusion — success/failure лежат в status.

    Перенести сюда чтение conclusion от GitHub значило бы получить пустоту на
    каждом прогоне и объявить любой результат нераспознанным.
    """
    seen, responses = patched_httpx
    forge = _forge()

    responses.extend([_pr(), _pr(), _runs(_run(HEAD, "failure"))])
    assert (await forge.check_pr_ci(7, gh_repo="own/rep")).outcome is (
        CIProbeOutcome.failed
    )

    responses.extend([_pr(), _pr(), _runs(_run(HEAD, "success"))])
    assert (await forge.check_pr_ci(7, gh_repo="own/rep")).outcome is (
        CIProbeOutcome.passed
    )


async def test_red_next_to_green_is_red(patched_httpx):
    """Один упавший прогон рядом с зелёным даёт красный ответ."""
    seen, responses = patched_httpx
    responses.extend(
        [_pr(), _pr(), _runs(_run(HEAD, "success"), _run(HEAD, "failure"))]
    )

    assert (await _forge().check_pr_ci(7, gh_repo="own/rep")).outcome is (
        CIProbeOutcome.failed
    )


async def test_unknown_status_is_unavailable_not_pending(patched_httpx):
    """Нераспознанное значение status НЕ читается как «ждём» и не как «прошло».

    Значение у ИДУЩЕГО прогона на живом API не наблюдалось ни разу — все шесть
    были завершены. Пока это не измерено, любое незнакомое слово обязано
    давать unavailable, и само слово обязано попасть в details: иначе
    следующий начнёт с того же перебора, с которого начинали мы. Ошибка в
    сторону pending страшнее — гейт будет ждать вечно.
    """
    seen, responses = patched_httpx
    responses.extend([_pr(), _pr(), _runs(_run(HEAD, "неведомое"))])

    probe = await _forge().check_pr_ci(7, gh_repo="own/rep")

    assert probe.outcome is CIProbeOutcome.unavailable
    assert probe.outcome is not CIProbeOutcome.pending
    assert "неведомое" in (probe.details or "")


async def test_four_situations_give_four_outcomes(patched_httpx):
    """AC-2. absent, missing_run, pending и unavailable остаются различимы (#419).

    Это не педантизм: absent пускает доставку мимо CI, missing_run говорит
    «подожди ещё», pending — «идёт», unavailable — «спроси снова». Схлопни их,
    и гейт либо доставит непроверенное, либо встанет навсегда.
    """
    seen, responses = patched_httpx
    forge = _forge()

    # 1. Прогонов на этот sha нет, И в репозитории нет workflow вовсе.
    responses.extend(
        [
            _pr(),
            _pr(),
            _runs(),
            httpx.Response(200, json={"total_count": 0, "workflows": []}),
        ]
    )
    assert (await forge.check_pr_ci(7, gh_repo="own/rep")).outcome is (
        CIProbeOutcome.absent
    )

    # 2. Workflow есть, а прогона на этот sha ещё нет.
    responses.extend(
        [
            _pr(),
            _pr(),
            _runs(),
            httpx.Response(200, json={"total_count": 1, "workflows": [{"name": "CI"}]}),
        ]
    )
    probe = await forge.check_pr_ci(7, gh_repo="own/rep")
    assert probe.outcome is CIProbeOutcome.missing_run
    assert probe.details == HEAD, "какой именно коммит ждёт прогона — часть ответа"

    # 3. Прогон идёт.
    responses.extend([_pr(), _pr(), _runs(_run(HEAD, "running"))])
    assert (await forge.check_pr_ci(7, gh_repo="own/rep")).outcome is (
        CIProbeOutcome.pending
    )

    # 4. Спросить не удалось.
    responses.extend([_pr(), _pr(), httpx.Response(403, json={})])
    assert (await forge.check_pr_ci(7, gh_repo="own/rep")).outcome is (
        CIProbeOutcome.unavailable
    )


async def test_broken_workflows_endpoint_never_becomes_absent(
    patched_httpx, monkeypatch
):
    """AC-4. 500 и 404 от /actions/workflows — это НЕ «CI тут нет».

    Измерено: обычный репозиторий отвечает 200, ПУСТОЙ — 500, а репозиторий
    без прав администратора — 404. Прочитать любой из них как absent значило
    бы сказать «проверять нечего» про репозиторий, где никто не смотрел, и
    пустить доставку мимо CI.
    """
    seen, responses = patched_httpx
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    forge = _forge()

    responses.extend([_pr(), _pr(), _runs()] + [httpx.Response(500)] * 3)
    probe = await forge.check_pr_ci(7, gh_repo="own/rep")
    assert probe.outcome is CIProbeOutcome.unavailable

    responses.extend([_pr(), _pr(), _runs(), httpx.Response(404, json={})])
    probe = await forge.check_pr_ci(7, gh_repo="own/rep")
    assert probe.outcome is CIProbeOutcome.unavailable
    assert probe.outcome is not CIProbeOutcome.absent


async def _no_sleep(_seconds):
    return None


async def test_no_head_sha_is_not_a_verdict(patched_httpx, monkeypatch):
    """PR без читаемой головы — «спросить не удалось», а не «проверок нет»."""
    seen, responses = patched_httpx
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    responses.extend([httpx.Response(500)] * 3)

    probe = await _forge().check_pr_ci(7, gh_repo="own/rep")

    assert probe.outcome is CIProbeOutcome.unavailable
    assert "head_sha" in probe.reason


# ---------------------------------------------------------------------------
# История ветки
# ---------------------------------------------------------------------------


async def test_branch_runs_fill_conclusion_the_consumers_expect(patched_httpx):
    """Потребитель читает conclusion — отдаём ему его, хотя у форжа его нет.

    Иначе каждый вызывающий обязан был бы знать, на каком форже он сейчас, и
    правило «исход лежит в status» расползлось бы по всему хабу.
    """
    seen, responses = patched_httpx
    responses.append(
        _runs(_run("aaa", "success"), _run("bbb", "failure"), _run("ccc", "running"))
    )

    runs = await _forge().branch_ci_runs("main", gh_repo="own/rep")

    assert runs is not None
    assert [r["sha"] for r in runs] == ["aaa", "bbb", "ccc"]
    assert runs[0]["status"] == "completed" and runs[0]["conclusion"] == "success"
    assert runs[1]["conclusion"] == "failure"
    # Идущий прогон исхода ещё не имеет, и выдумывать его нельзя.
    assert runs[2]["status"] == "in_progress" and runs[2]["conclusion"] == ""


async def test_branch_runs_answer_none_when_unreadable(patched_httpx, monkeypatch):
    """None, а не пустой список: «нет прогонов» и «не смотрели» — разное (#725)."""
    seen, responses = patched_httpx
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    responses.extend([httpx.Response(500)] * 3)

    assert await _forge().branch_ci_runs("main", gh_repo="own/rep") is None


# ---------------------------------------------------------------------------
# Логи упавших джобов
# ---------------------------------------------------------------------------


async def test_failure_logs_are_not_truncated_by_the_transport(patched_httpx):
    """AC-3. Отсечку по объёму делает вызывающий, а не транспорт.

    Транспорт режет тело до 300 символов — для диагностики этого хватает, а
    для логов джоба тело И ЕСТЬ ответ. Не отмени обрезку, max_log_chars стал
    бы украшением, и разработчик получал бы 300 символов вместо трассы.
    """
    seen, responses = patched_httpx
    long_log = "СТРОКА ЛОГА\n" * 500
    responses.extend(
        [
            _runs(_run(HEAD, "failure")),
            _pr(),  # голова PR: логи берутся у ЕЁ прогона, а не у первого
            httpx.Response(
                200,
                json={
                    "total_count": 2,
                    "jobs": [
                        {"id": 1, "name": "Backend (pytest)", "status": "failure"},
                        {"id": 2, "name": "Frontend", "status": "success"},
                    ],
                },
            ),
            httpx.Response(200, text=long_log),
        ]
    )

    result = await _forge().ci_failure_logs(
        7, "task-1/x", max_log_chars=1000, gh_repo="own/rep"
    )

    assert result["failed_checks"] == ["Backend (pytest)"], "успешный джоб не упавший"
    # По СОДЕРЖАНИЮ, а не по длине: транспортные 300 символов плюс заголовок
    # «--- имя джоба ---» дают строку длиннее 300, и проверка длиной проходила
    # бы на обрезанном логе. Найдено мутацией, а не рассуждением.
    assert result["log_summary"].count("СТРОКА ЛОГА") > 50, (
        "транспортная обрезка отменена — иначе тут был бы огрызок в 300 символов"
    )
    assert len(result["log_summary"]) <= 1000 + len("... (truncated) ...\n")
    assert "actions/runs/1443246" in result["run_url"]


async def test_failure_logs_stay_quiet_when_there_is_nothing_to_show(
    patched_httpx, monkeypatch
):
    """Пустой ответ вместо исключения: вызывающий и так знает, что CI красный."""
    seen, responses = patched_httpx
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    responses.extend([httpx.Response(500)] * 3)

    result = await _forge().ci_failure_logs(7, "task-1/x", gh_repo="own/rep")

    assert result == {"failed_checks": [], "log_summary": "", "run_url": ""}


# ---------------------------------------------------------------------------
# Мелочь, на которой ломается наивный перенос
# ---------------------------------------------------------------------------


def test_full_ref_is_shortened():
    """ "refs/heads/main" → "main": сравнение в лоб дало бы пустой результат,
    неотличимый от «прогонов нет»."""
    assert GitVerseForge._short_ref("refs/heads/main") == "main"
    assert GitVerseForge._short_ref("refs/heads/task-1/x") == "task-1/x"
    assert GitVerseForge._short_ref("main") == "main"


async def test_workflow_presence_asks_the_api_not_a_directory(patched_httpx):
    """AC-4. «Есть ли тут CI» спрашивается у API, а не у каталога.

    Соблазн посмотреть содержимое .github/workflows велик и отвечает неверно:
    раннер GitVerse обрабатывает ОБА каталога — и .gitverse/workflows/, и
    .github/workflows/, — так что репозиторий со своим CI в первом из них был
    бы объявлен «без CI», а доставка поехала бы мимо проверок.

    Проверяется по тому, КУДА ушёл запрос, а не по совпадению ответа.
    """
    seen, responses = patched_httpx
    responses.append(
        httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflows": [{"name": "CI", "path": ".gitverse/workflows/ci.yaml"}],
            },
        )
    )

    assert await _forge().has_workflows(gh_repo="own/rep") is True

    assert len(seen) == 1
    assert seen[0].url.path == "/repos/own/rep/actions/workflows"
    assert "contents" not in str(seen[0].url), "содержимое каталога не спрашивается"


async def test_failure_logs_belong_to_the_head_of_this_pr(patched_httpx):
    """Логи берутся у прогона ГОЛОВЫ PR, а не у первого в ветке.

    На ветке с историей первым лежит самый свежий прогон — возможно, чужого
    коммита. Показать его логи значит отправить человека чинить чужое
    падение, и он это заметит не сразу.
    """
    seen, responses = patched_httpx
    other, mine = "commit-of-somebody-else", HEAD
    responses.extend(
        [
            _runs(
                {**_run(other, "failure"), "id": 111},
                {**_run(mine, "failure"), "id": 222},
            ),
            _pr(),  # pr_head_sha
            httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "jobs": [{"id": 9, "name": "Backend", "status": "failure"}],
                },
            ),
            httpx.Response(200, text="след падения нашего коммита"),
        ]
    )

    result = await _forge().ci_failure_logs(7, "task-1/x", gh_repo="own/rep")

    assert "actions/runs/222" in result["run_url"], "взят прогон нашей головы"
    assert "111" not in result["run_url"]
    assert "след падения нашего коммита" in result["log_summary"]


async def test_no_run_for_this_head_shows_nothing_rather_than_someone_elses(
    patched_httpx,
):
    """Прогона на нашу голову нет — показываем пустоту, а не чужой лог."""
    seen, responses = patched_httpx
    responses.extend([_runs(_run("commit-of-somebody-else", "failure")), _pr()])

    result = await _forge().ci_failure_logs(7, "task-1/x", gh_repo="own/rep")

    assert result == {"failed_checks": [], "log_summary": "", "run_url": ""}


# ---------------------------------------------------------------------------
# Один коммит — несколько прогонов: перезапуск, правка workflow, флейк
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "order",
    [
        pytest.param("newest_first", id="новее-первым-как-отдаёт-живой-API"),
        pytest.param("oldest_first", id="старее-первым-если-порядок-изменится"),
    ],
)
async def test_green_then_red_on_one_sha_is_red(patched_httpx, order):
    """AC-1. Зелёный, а следом красный на ТОМ ЖЕ коммите — это красный.

    Так бывает от перезапуска, правки workflow и флейка. Промах здесь самый
    дорогой из возможных для CI-пробы: гейт откроет доставку по устаревшему
    зелёному, и непроверенное уедет в базовую ветку.

    Проверяется при ОБОИХ порядках списка. Живой API отдаёт новые первыми, но
    ответ не имеет права зависеть от порядка: он про множество прогонов
    коммита, а не про то, какой из них попался первым.
    """
    seen, responses = patched_httpx
    green = {**_run(HEAD, "success"), "id": 1}
    red = {**_run(HEAD, "failure"), "id": 2}
    pair = [red, green] if order == "newest_first" else [green, red]
    responses.extend([_pr(), _pr(), _runs(*pair)])

    probe = await _forge().check_pr_ci(7, gh_repo="own/rep")

    assert probe.outcome is CIProbeOutcome.failed, (
        "последующее падение на том же коммите отменяет прежний зелёный"
    )


@pytest.mark.parametrize(
    "order",
    [
        pytest.param("newest_first", id="новее-первым"),
        pytest.param("oldest_first", id="старее-первым"),
    ],
)
async def test_failure_logs_take_the_latest_failed_run(patched_httpx, order):
    """Логи — у ПОСЛЕДНЕГО падения коммита, а не у первого попавшегося.

    Если коммит падал дважды, чинить надо по свежему логу: старый может
    относиться к уже исправленному шагу. Порядок в ответе тоже не должен
    решать — прогоны сравниваются по времени запуска.
    """
    seen, responses = patched_httpx
    old_fail = {
        **_run(HEAD, "failure"),
        "id": 1,
        "started": "2026-08-31T10:00:00Z",
    }
    new_fail = {
        **_run(HEAD, "failure"),
        "id": 2,
        "started": "2026-08-31T12:00:00Z",
    }
    pair = [new_fail, old_fail] if order == "newest_first" else [old_fail, new_fail]
    responses.extend(
        [
            _runs(*pair),
            _pr(),
            httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "jobs": [{"id": 9, "name": "Backend", "status": "failure"}],
                },
            ),
            httpx.Response(200, text="след свежего падения"),
        ]
    )

    result = await _forge().ci_failure_logs(7, "task-1/x", gh_repo="own/rep")

    assert "actions/runs/2" in result["run_url"], "взят самый свежий упавший прогон"
