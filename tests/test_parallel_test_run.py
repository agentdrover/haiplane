"""Прогон тестов идёт параллельно, но репортер AC остаётся последовательным.

Инвариант: флаг параллельности доходит до ШАГА ТЕСТОВ и не доходит до
РЕПОРТЕРА AC. Он держится не вкусом, а вот чем.

Шаг «Report AC tests» в .github/workflows/ci.yml гоняет nodeid-ы через
scripts/ci_report_to_hub.py, а тот разбирает вывод ``-v`` построчно и ждёт
nodeid первым токеном строки::

    tests/test_x.py::test_y PASSED [ 16%]        # разбирается
    [gw0] [ 16%] PASSED tests/test_x.py::test_y  # первым токеном [gw0]

Для второй формы ``"[gw0]".split("[", 1)[0]`` даёт пустую строку, она не
совпадает ни с одним ожидаемым nodeid, и строка молча отбрасывается. Всё, чего
нет в разобранном, репортер объявляет ``not_found``. Шаг помечен
``continue-on-error: true``, поэтому CI при этом ЗЕЛЁНЫЙ, а на проде стоит
``AC_LOCATOR=require`` — то есть гейт перестаёт получать доказательства и не
сообщает об этом. Проверено исполнением: ``run_nodeids`` с раннером
``uv run pytest`` возвращает ``{nodeid: True}``, с ``uv run pytest -n auto`` —
пустой словарь.

Отсюда три требования, и каждое проверяется ПО СМЫСЛУ, а не по написанию.
Ранняя версия этого файла сверяла подстроки ``"run: uv run pytest -q -n auto"``
по всему тексту файла и была неправа с обеих сторон: закомментированный целиком
шаг Test оставлял её зелёной (подстрока уцелела в тексте комментария, прогона
тестов в CI не было вовсе), а безобидная перестановка флагов, кавычки или
блочный скаляр ``run: |`` — роняли при исправном поведении. Обе стороны
воспроизведены мутациями, поэтому здесь разбираются YAML и рецепт make, а
флаг ищется среди ТОКЕНОВ команды.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPO_ROOT / "Makefile"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _split(command: str) -> list[str]:
    """Токены команды, БЕЗ закомментированного хвоста.

    ``comments=True`` обязателен. Без него `#` — обычный токен, и рецепт
    ``\\t# uv run pytest -q -n auto`` (то есть цель, не гоняющая ничего)
    выглядел бы как рабочая параллельная команда. Проверено исполнением:
    ровно так эта проверка и зеленела, пока сюда не добавили comments.
    """
    return shlex.split(command, comments=True)


def _pytest_argv(tokens: list[str]) -> list[str]:
    """Аргументы САМОГО pytest — всё после токена ``pytest``.

    Судить по всей командной строке нельзя: она включает обёртку, а у ``uv``
    есть своя короткая ``-n`` (``--no-cache``, проверено по ``uv run --help``).
    ``uv run -n pytest`` последователен, но по всей строке выглядел бы
    параллельным — и проверка «шаг тестов гоняет параллельно» осталась бы
    зелёной на полностью последовательном прогоне.
    """
    for i, token in enumerate(tokens):
        if token == "pytest" or token.endswith("/pytest"):
            return tokens[i + 1 :]
    return tokens  # pytest не назван (например, это addopts) — судим по всему


def _carries_parallel_flag(tokens: list[str]) -> bool:
    """Включает ли команда параллельный прогон pytest-xdist.

    Написаний больше одного, и проверять надо все: ``-n auto``, ``-nauto``,
    ``-n4``, ``--numprocesses=auto``, ``--numprocesses auto``. Проверено
    исполнением — pytest включает xdist на КАЖДОМ из них, в том числе на
    слитной короткой форме и на списочном ``addopts = ["-n", "auto"]``.

    Значение 0 — исключение, и не косметическое: ``-n0`` гонит всё в
    мастер-процессе, вывод ``-v`` остаётся с nodeid первым токеном (проверено),
    репортер AC его разбирает. Считать такую команду параллельной значило бы
    браковать безобидный раннер и объявлять «параллельным» последовательный шаг.
    """
    argv = _pytest_argv(tokens)
    for i, token in enumerate(argv):
        if token == "--numprocesses" or token == "-n":
            value = argv[i + 1] if i + 1 < len(argv) else ""
        elif token.startswith("--numprocesses="):
            value = token.split("=", 1)[1]
        elif token.startswith("-n") and not token.startswith("--"):
            value = token[2:].lstrip("=")
        else:
            continue
        if value.strip() == "0":
            continue
        return True
    return False


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps() -> list[dict]:
    steps: list[dict] = []
    for job in _workflow().get("jobs", {}).values():
        steps.extend(job.get("steps", []) or [])
    return steps


def _step_by_id(step_id: str) -> dict:
    for step in _steps():
        if step.get("id") == step_id:
            return step
    pytest.fail(
        f"в ci.yml нет шага с id={step_id!r} — шаг переименован, отключён или "
        "закомментирован. Проверка идёт по разобранному YAML именно поэтому: "
        "закомментированный шаг оставлял бы подстроку в тексте файла."
    )


def _make_recipe(target: str) -> list[str]:
    """Строки рецепта цели Makefile, без комментариев и с учётом переносов.

    ``test : deps`` с пробелом перед двоеточием — легальная запись, поэтому
    цель ищется регулярным выражением, а не ``startswith``. Продолжение
    логической строки после ``\\`` не обязано начинаться с табуляции, поэтому
    сбор продолжается, пока предыдущая строка кончалась обратным слешем: иначе
    разбитая на две строки команда обрывалась бы и флаг «терялся» на исправном
    Makefile.
    """
    target_re = re.compile(rf"^{re.escape(target)}\s*:")
    recipe: list[str] = []
    collecting = False
    continued = False
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if not collecting:
            collecting = bool(target_re.match(line))
            continue
        if not (line.startswith("\t") or continued):
            if line.strip() and not line.lstrip().startswith("#"):
                break
            continue
        continued = line.rstrip().endswith("\\")
        body = line.strip().rstrip("\\").strip()
        # Комментарий рецепта начинается с ТАБУЛЯЦИИ, а не с нулевой колонки:
        # отбрасывать по startswith("#") значило бы принять закомментированную
        # команду за рабочую (проверено исполнением).
        if body and not body.startswith("#"):
            recipe.append(body)
    if not recipe:
        pytest.fail(
            f"в Makefile нет исполняемого рецепта цели {target!r} — цель "
            "отсутствует либо её команды закомментированы"
        )
    return recipe


def _addopts() -> object:
    """``addopts`` из pyproject; "" когда ключа нет.

    Секция индексируется напрямую, без защитной цепочки .get: пропасть
    безобидно она не может — там же лежит ``asyncio_mode``, без которого
    падает сотня с лишним асинхронных файлов, — так что KeyError здесь был бы
    сопутствующим симптомом, а не ложной тревогой.
    """
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return config["tool"]["pytest"]["ini_options"].get("addopts", "")


def _addopts_tokens(addopts: object) -> list[str]:
    """addopts законно пишется и строкой, и TOML-массивом — принимаем оба."""
    if isinstance(addopts, str):
        return _split(addopts)
    if isinstance(addopts, (list, tuple)):
        return [str(item) for item in addopts]
    return [str(addopts)]


# ---- флаг доходит туда, где нужен ----


def test_the_ci_test_step_runs_pytest_in_parallel() -> None:
    run = _step_by_id("tests").get("run", "")
    tokens = _split(run)
    assert "pytest" in tokens, f"шаг tests больше не гоняет pytest: {run!r}"
    assert _carries_parallel_flag(tokens), (
        f"шаг tests потерял флаг параллельности: {run!r}. Порядок и написание "
        "флага роли не играют — проверяются токены, а не подстрока."
    )


def test_the_make_target_runs_pytest_in_parallel() -> None:
    tokens = [t for line in _make_recipe("test") for t in _split(line)]
    assert "pytest" in tokens, f"цель test больше не гоняет pytest: {tokens}"
    assert _carries_parallel_flag(tokens), (
        f"цель test потеряла флаг параллельности: {tokens}"
    )


def test_the_parallel_runner_is_a_declared_dependency() -> None:
    # Без объявленной зависимости `uv sync --dev` в CI не поставит xdist, и шаг
    # тестов упадёт на неизвестном флаге -n — то есть проверки выше проходили
    # бы, а CI всё равно был бы красным.
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dev = config["dependency-groups"]["dev"]
    assert any(spec.startswith("pytest-xdist") for spec in dev), dev


# ---- и не доходит туда, где ломает доказательства ----


def test_addopts_never_carries_the_parallel_flag() -> None:
    """См. модульный докстринг: addopts достался бы и репортеру AC."""
    addopts = _addopts()
    assert not _carries_parallel_flag(_addopts_tokens(addopts)), (
        "флаг параллельности в addopts достаётся шагу Report AC tests: его "
        "парсер ждёт nodeid первым токеном строки -v, а xdist ставит там "
        f"[gw0], и каждый AC становится not_found. addopts={addopts!r}"
    )


def test_the_ac_runner_stays_sequential() -> None:
    """Второй маршрут того же дефекта — сам раннер репортера, не addopts.

    Значение приходит в HAIPLANE_HUB_CI_PYTEST и подставляется в argv целиком
    (scripts/ci_report_to_hub.py: ac_runner → run_nodeids); ни одна из этих
    функций флаги не фильтрует. Для сателлитных проектов оно и вовсе берётся
    из конфига проекта через hub/services/workflow_seed.py, где делается
    только strip. Значит запрет держится здесь или нигде.
    """
    reporters = [
        step
        for step in _steps()
        if "hub-ci-report" in str(step.get("uses", ""))
        and "ac-runner" in (step.get("with") or {})
    ]
    assert reporters, "в ci.yml не найден шаг hub-ci-report со входом ac-runner"
    for step in reporters:
        runner = step["with"]["ac-runner"]
        assert not _carries_parallel_flag(_split(str(runner))), (
            f"ac-runner={runner!r} гонит AC-локаторы параллельно: вывод -v "
            "придёт строками с [gw0] первым токеном, парсер отбросит их все, "
            "и каждый AC станет not_found при зелёном CI (шаг помечен "
            "continue-on-error)."
        )
