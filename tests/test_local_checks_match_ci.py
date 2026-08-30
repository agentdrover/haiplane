"""make lint зелёный при красном шаге CI стоил трёх пересдач за сутки (#1080):
Check formatting падало в CI дважды, bandit — один раз, хотя `make lint`
прошёл локально. Причина — `make lint`/`make check` покрывали четверть
статических шагов джоба test: остальные существовали только в ci.yml.

Этот файл закрепляет две вещи исполнением, а не чтением конфигов:
  * неотформатированный файл красит `make lint` (иначе снова можно сдать
    зелёное дерево, которое падает в CI на форматировании);
  * каждый статический шаг джоба test либо покрыт целью, входящей в
    `make check`, либо явно внесён в EXCEPTIONS с причиной — новый шаг без
    того и другого красит этот тест, а не проходит незамеченным.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPO_ROOT / "Makefile"

# Шаги джоба test, которые статической проверкой не являются — подготовка
# окружения, отчётность или advisory-находки, которые красным не бывают.
# Ключ — точное имя шага (`- name:` в ci.yml). Мёртвая запись (имени больше
# нет среди шагов) — это способ тихо перестать проверять покрытие, поэтому
# test_exceptions_name_only_real_steps держит список живым.
EXCEPTIONS: dict[str, str] = {
    "Checkout": "подготовка окружения, не проверка",
    "Install uv": "подготовка окружения, не проверка",
    "Set up Python": "подготовка окружения, не проверка",
    "Install dependencies": "подготовка окружения, не проверка",
    "Detect tree already tested (skip pytest on exact dedup)": (
        "оптимизация прогона, не проверка — #1077"
    ),
    "Surface parity (warning only)": ("только PR и warning-only: exit 0 всегда"),
    "Report AC tests and validation to Hub": "отчётность, не проверка",
    "Dependency vulnerability audit": (
        "advisory: continue-on-error, находки уезжают драфтами в хаб, красным не бывает"
    ),
    "File audit findings as Hub drafts": (
        "advisory: continue-on-error, находки уезжают драфтами в хаб, красным не бывает"
    ),
}

# Подстрока команды -> человекочитаемое имя инструмента. Нормализация по
# инструменту, а не побайтовое сравнение команд: переформулировка одной и той
# же команды в ci.yml (порядок флагов, добавленный аргумент) не должна ронять
# этот тест зря — важно, что инструмент вызван и там, и там.
SIGNATURES: dict[str, str] = {
    "ruff check": "ruff check",
    "ruff format --check": "ruff format --check",
    "mypy": "mypy",
    "complexity_budget.py": "complexity_budget.py",
    "mcp_catalog_budget.py": "mcp_catalog_budget.py",
    "pytest": "pytest",
    "bandit": "bandit",
    "secret_scan.py": "secret_scan.py",
}

_STEP_NAME_RE = re.compile(r"^\s*- name:\s*(.+?)\s*$", re.MULTILINE)


def _ci_test_steps(text: str) -> list[tuple[str, str]]:
    """Пары (имя шага, текст шага) из джоба ``test:`` файла ci.yml.

    Джоб ``deploy:`` вне этого правила: он не статическая проверка, а вынос
    в прод, и живёт по собственным гейтам (always()/continue-on-error).
    Текст читается как есть, без yaml-парсера: тест должен ловить дословную
    правку run-блока, а не пересобранное AST.
    """
    lines = text.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line == "  test:\n")
    end = next(i for i, line in enumerate(lines) if line == "  deploy:\n" and i > start)
    body = "".join(lines[start:end])

    matches = list(_STEP_NAME_RE.finditer(body))
    steps: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        chunk_start = m.start()
        chunk_end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        steps.append((m.group(1), body[chunk_start:chunk_end]))
    return steps


def _target_body(lines: list[str], target: str) -> str:
    """Тело рецепта make-цели ``target`` (строки, начинающиеся с таба)."""
    body_lines: list[str] = []
    in_target = False
    for line in lines:
        if in_target:
            if line.startswith("\t"):
                body_lines.append(line)
                continue
            break
        if line == f"{target}:":
            in_target = True
    return "\n".join(body_lines)


def _make_check_body(makefile_text: str) -> str:
    """Тела всех целей, перечисленных в строке ``check: ...``, одним текстом.

    ``check`` ссылается на цели напрямую (lint, types, budget, security,
    test) — рекурсия по вложенным целям не нужна, ни одна из них сама на
    другую не ссылается.
    """
    lines = makefile_text.splitlines()
    check_line = next(line for line in lines if line.startswith("check:"))
    targets = check_line.split(":", 1)[1].split()
    return "\n".join(_target_body(lines, target) for target in targets)


def _uncovered_steps(ci_text: str, makefile_text: str) -> list[str]:
    """Шаги джоба test, не покрытые ``make check`` и не внесённые в EXCEPTIONS."""
    check_body = _make_check_body(makefile_text)
    problems: list[str] = []
    for name, chunk in _ci_test_steps(ci_text):
        if name in EXCEPTIONS:
            continue
        matched = [sig for sig in SIGNATURES if sig in chunk]
        # Без единой известной сигнатуры — шаг непрослеживаем. С сигнатурой,
        # которой нет в теле make check, — шаг прослежен, но не покрыт.
        # Обе ситуации значат одно и то же для автора: до этой сдачи никто
        # не гонял этот шаг локально.
        uncovered = not matched or any(sig not in check_body for sig in matched)
        if uncovered:
            problems.append(
                f"шаг '{name}': добавьте команду в цель, входящую в make check, "
                "или внесите шаг в EXCEPTIONS с причиной"
            )
    return problems


def test_lint_target_fails_on_unformatted_file() -> None:
    """Проверено исполнением, а не чтением Makefile: `make lint` обязан
    упасть на неотформатированном файле, а не пройти его молча — это и есть
    инвариант задачи #1080, а не деталь реализации ruff.
    """
    scratch = REPO_ROOT / "tests" / "_scratch_unformatted_1080.py"
    # Валидный Python, который `ruff format` переформатирует (пробелы вокруг
    # `=` не по стандарту). Имя без префикса test_ — pytest его не соберёт.
    scratch.write_text("x=1\ny =  2\n", encoding="utf-8")
    try:
        proc = subprocess.run(
            ["make", "lint"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode != 0, combined
        assert scratch.name in combined, combined
    finally:
        scratch.unlink(missing_ok=True)


def test_every_ci_static_check_has_a_make_target() -> None:
    ci_text = CI_YML.read_text(encoding="utf-8")
    makefile_text = MAKEFILE.read_text(encoding="utf-8")
    assert _uncovered_steps(ci_text, makefile_text) == []


def test_a_new_uncovered_step_breaks_the_build() -> None:
    """Синтетика: шаг, добавленный в ci.yml без make-цели и без EXCEPTIONS,
    обязан провалить _uncovered_steps — иначе проверка молчит именно в том
    случае, ради которого она заведена.
    """
    ci_text = CI_YML.read_text(encoding="utf-8")
    makefile_text = MAKEFILE.read_text(encoding="utf-8")
    injected_step = (
        "      - name: Imaginary new check\n        run: uv run imaginary-tool hub\n"
    )
    mutated = ci_text.replace("  deploy:\n", injected_step + "  deploy:\n", 1)
    assert mutated != ci_text

    problems = _uncovered_steps(mutated, makefile_text)
    assert len(problems) == 1
    assert "Imaginary new check" in problems[0]
    assert "EXCEPTIONS" in problems[0]


def test_exceptions_name_only_real_steps() -> None:
    """Мёртвое исключение — способ тихо перестать проверять шаг, чьё имя
    поменялось или пропало (#1080)."""
    ci_text = CI_YML.read_text(encoding="utf-8")
    real_names = {name for name, _ in _ci_test_steps(ci_text)}
    for name in EXCEPTIONS:
        assert name in real_names, name
