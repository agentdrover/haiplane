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

import shlex
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPO_ROOT / "Makefile"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _carries_parallel_flag(tokens: list[str]) -> bool:
    """Есть ли среди токенов команды флаг параллельного прогона pytest-xdist.

    Написаний больше одного, и проверять надо все: ``-n auto``, ``-nauto``,
    ``-n4``, ``--numprocesses=auto``, ``--numprocesses auto``. Проверено
    исполнением — pytest включает xdist на КАЖДОМ из них, в том числе на
    слитной короткой форме и на списочном ``addopts = ["-n", "auto"]``.
    Проверка на одно написание пропускала бы три из четырёх.
    """
    for token in tokens:
        if token.startswith("--"):
            if token == "--numprocesses" or token.startswith("--numprocesses="):
                return True
            continue
        if token.startswith("-n"):  # -n, -nauto, -n4
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
    """Строки рецепта цели Makefile (табулированные), с учётом переносов."""
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    recipe: list[str] = []
    collecting = False
    for line in lines:
        if line.startswith(f"{target}:"):
            collecting = True
            continue
        if collecting:
            if line.startswith("\t"):
                recipe.append(line.lstrip("\t").rstrip("\\").strip())
            elif line.strip() and not line.startswith("#"):
                break
    if not recipe:
        pytest.fail(f"в Makefile нет рецепта цели {target!r}")
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
        return shlex.split(addopts)
    if isinstance(addopts, (list, tuple)):
        return [str(item) for item in addopts]
    return [str(addopts)]


# ---- флаг доходит туда, где нужен ----


def test_the_ci_test_step_runs_pytest_in_parallel() -> None:
    run = _step_by_id("tests").get("run", "")
    tokens = shlex.split(run)
    assert "pytest" in tokens, f"шаг tests больше не гоняет pytest: {run!r}"
    assert _carries_parallel_flag(tokens), (
        f"шаг tests потерял флаг параллельности: {run!r}. Порядок и написание "
        "флага роли не играют — проверяются токены, а не подстрока."
    )


def test_the_make_target_runs_pytest_in_parallel() -> None:
    tokens = [t for line in _make_recipe("test") for t in shlex.split(line)]
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
        assert not _carries_parallel_flag(shlex.split(str(runner))), (
            f"ac-runner={runner!r} гонит AC-локаторы параллельно: вывод -v "
            "придёт строками с [gw0] первым токеном, парсер отбросит их все, "
            "и каждый AC станет not_found при зелёном CI (шаг помечен "
            "continue-on-error)."
        )
