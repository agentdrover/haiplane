"""Прогон тестов идёт параллельно, но репортер AC остаётся последовательным.

Файлы читаются как ТЕКСТ намеренно, по образцу tests/test_ci_workflow_contract.py:
контракт здесь буквальный — какой именно командой CI и Makefile запускают
pytest, и какого флага НЕ должно быть в общей конфигурации.

Второй тест — не стилистика, а защита от тихой поломки. Шаг «Report AC tests»
в .github/workflows/ci.yml гоняет нужные nodeid через scripts/ci_report_to_hub.py,
а тот разбирает вывод ``-v`` построчно и ждёт nodeid первым токеном строки:

    tests/test_x.py::test_y PASSED [ 16%]

Под xdist первым токеном становится ``[gw0]``:

    [gw0] [ 16%] PASSED tests/test_x.py::test_y

Такая строка не совпадает ни с одним ожидаемым nodeid и молча отбрасывается —
каждый AC ушёл бы в not_found, то есть SDD-гейт перестал бы получать
доказательства, оставаясь при этом зелёным. Поэтому ``-n`` живёт в двух явных
командах запуска, а не в ``addopts``, который достался бы и репортеру.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPO_ROOT / "Makefile"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def test_ci_and_makefile_run_pytest_in_parallel() -> None:
    assert "run: uv run pytest -q -n auto" in WORKFLOW.read_text(encoding="utf-8")
    assert "uv run pytest -q -n auto" in MAKEFILE.read_text(encoding="utf-8")


def test_the_parallel_runner_is_a_declared_dependency() -> None:
    # Без объявленной зависимости `uv sync --dev` в CI не поставит xdist, и шаг
    # тестов упадёт на неизвестном флаге -n — то есть первый тест этого файла
    # проходил бы, а CI всё равно был бы красным.
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dev = config["dependency-groups"]["dev"]
    assert any(spec.startswith("pytest-xdist") for spec in dev), dev


def test_addopts_never_carries_the_parallel_flag() -> None:
    """См. модульный докстринг: addopts достался бы репортеру AC."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"].get("addopts", "")
    assert "-n" not in str(addopts).split(), (
        "-n в addopts достаётся шагу Report AC tests: его парсер ждёт nodeid "
        "первым токеном строки -v, а xdist ставит там [gw0], и каждый AC "
        f"становится not_found. addopts={addopts!r}"
    )
