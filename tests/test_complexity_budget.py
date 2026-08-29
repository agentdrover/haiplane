"""Бюджет сложности держится проверкой, а не памятью автора (#1066).

До этой задачи в линте не было ни одного правила сложности: `ruff check hub
tests` был зелёным всё время, пока submit_for_review рос до 173 операторов и
цикломатики 46. Здесь проверяется не то, что репозиторий красив, а то, что
страж работает во все четыре стороны: пропускает сегодняшнее состояние,
краснеет на новом нарушении, краснеет на исключении, пережившем свою причину,
и не позволяет погасить целый файл одной записью.

Последние два — про то, как такие списки умирают. Список, в котором можно
оставить запись без нарушения, за полгода превращается в унаследованный мусор,
и его перестают читать. Исключение на файл гасит вместе с сегодняшним
нарушением все будущие в том же файле — то есть выключает проверку там, где
она нужнее всего.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import complexity_budget as budget  # noqa: E402


@pytest.fixture(scope="module")
def violations() -> list[budget.Violation]:
    """Фактические нарушения на дереве — один прогон ruff на весь модуль."""
    return budget.collect_violations()


@pytest.fixture(scope="module")
def ledger() -> dict[str, dict]:
    return budget.load_ledger()


def test_the_repository_is_green_under_the_budget(violations, ledger):
    """AC-1: сегодняшнее состояние проходит СО списком исключений."""
    report = budget.check(violations, ledger)
    assert report.ok, budget.format_report(report)


def test_ruff_check_stays_green():
    """AC-1: обычный линт не краснеет от появления порогов в pyproject.

    Пороги в [tool.ruff.lint.mccabe] и [tool.ruff.lint.pylint] инертны, пока
    правила не выбраны в lint.select. Их выбирает только проверка бюджета —
    иначе `ruff check` покраснел бы на 36 нарушениях, а погасить их в конфиге
    можно лишь per-file-ignores, что запрещено (см. AC-4).
    """
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "hub", "tests"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_new_complex_function_turns_the_check_red(tmp_path: Path):
    """AC-2: функция сверх порога и вне ledger краснит и называет себя.

    Прогоняется настоящий ruff по настоящему файлу, а не подсовывается готовый
    Violation: проверка должна доказывать, что правило ВЫБРАНО и срабатывает,
    а не что структура данных умеет хранить нарушение.
    """
    branches = "\n".join(f"    if value == {n}:\n        return {n}" for n in range(40))
    module = tmp_path / "offender.py"
    module.write_text(
        f"def a_freshly_grown_function(value):\n{branches}\n    return -1\n",
        encoding="utf-8",
    )

    found = budget.collect_violations(str(tmp_path))
    assert found, "ruff не нашёл нарушения — правило не выбрано проверкой"

    names = {v.function for v in found}
    assert "a_freshly_grown_function" in names

    report = budget.check(found, ledger={})
    assert not report.ok
    text = budget.format_report(report)
    assert "a_freshly_grown_function" in text
    assert "offender.py" in text


def test_a_noqa_comment_cannot_silence_the_budget(tmp_path: Path):
    """Бюджет не глушится комментарием — иначе весь ledger обходится строкой.

    Проверено на живом ruff: без ``--ignore-noqa`` та же функция с
    ``# noqa: C901`` проходит как чистая. Это и есть самый дешёвый способ
    обойти правило, и он должен быть закрыт до того, как кто-то его найдёт.
    """
    branches = "\n".join(f"    if value == {n}:\n        return {n}" for n in range(40))
    module = tmp_path / "silenced.py"
    module.write_text(
        f"def a_silenced_function(value):  # noqa: C901, PLR0915\n"
        f"{branches}\n    return -1\n",
        encoding="utf-8",
    )

    found = budget.collect_violations(str(tmp_path))
    names = {v.function for v in found}
    assert "a_silenced_function" in names, (
        "noqa спрятал функцию от бюджета — проверка обходится одним комментарием"
    )


@pytest.mark.parametrize("dropped", sorted(budget.load_ledger()))
def test_dropping_any_waiver_turns_the_check_red(violations, ledger, dropped):
    """AC-3: снятие любой записи краснит — значит ledger описывает реальность.

    Без этого фиктивный или раздутый список проходил бы так же, как честный:
    можно было бы поставить порог в потолок, оставить список пустым и назвать
    это работающим правилом.

    Параметры берутся из самого ledger, а не из числа 36: список задуман
    убывающим, и захардкоженная длина превратила бы часть проверок в тихие
    skip ровно тогда, когда записи начнут сниматься.
    """
    trimmed = {k: v for k, v in ledger.items() if k != dropped}

    report = budget.check(violations, trimmed)
    assert not report.ok
    assert report.new, "снятая запись обязана вернуться как новое нарушение"


def test_a_stale_waiver_turns_the_check_red(ledger):
    """AC-3 (вторая сторона): запись без нарушения тоже краснит."""
    inflated = dict(ledger)
    inflated["hub/nowhere.py::gone_long_ago::C901"] = {
        "path": "hub/nowhere.py",
        "function": "gone_long_ago",
        "rule": "C901",
        "measured": 99,
        "max": 99,
        "removed_by": "unclaimed",
    }
    report = budget.check([], inflated)
    assert not report.ok
    assert "gone_long_ago" in budget.format_report(report)


def test_no_waiver_covers_a_whole_file_or_directory(ledger):
    """AC-4: каждая запись адресует функцию, а не файл и не каталог."""
    offenders = []
    for key, entry in ledger.items():
        function = str(entry.get("function", ""))
        path = str(entry.get("path", ""))
        if not function or function.startswith("<"):
            offenders.append(f"{key}: запись без имени функции")
        if not path.endswith(".py"):
            offenders.append(f"{key}: путь не файл — {path!r}")
    assert not offenders, offenders


def test_every_waiver_declares_why_it_is_there(ledger):
    """Причина — единственное поле, которое машина не выводит.

    Три значения: ``task:#NNNN`` — снимается названной задачей; ``permanent``
    — остаётся, и написано почему; ``unclaimed`` — долг без задачи. Последнее
    разрешено намеренно: запретить его значило бы заставить автора выдумать
    номер задачи, и список стал бы врать вместо того, чтобы называть непокрытое.
    """
    offenders = []
    for key, entry in ledger.items():
        reason = str(entry.get("removed_by", "")).strip()
        kind = reason.split("—")[0].strip()
        if kind.startswith("task:#"):
            if not kind[6:].isdigit():
                offenders.append(f"{key}: task без номера — {reason!r}")
        elif kind in ("permanent", "unclaimed"):
            if len(reason) <= len(kind):
                offenders.append(f"{key}: {kind} без объяснения")
        else:
            offenders.append(f"{key}: непонятная причина — {reason!r}")
    assert not offenders, offenders


def test_the_thresholds_live_in_pyproject_only(violations):
    """У числа одно место: скрипт не носит свою копию порога.

    Проверяется не текст скрипта, а результат: порог, с которым ruff реально
    сравнивал, приезжает в каждое нарушение. Если бы скрипт нёс свою копию,
    эти числа могли бы разойтись с pyproject, и разошлись бы молча.
    """
    config = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.ruff.lint.mccabe]" in config
    assert "[tool.ruff.lint.pylint]" in config

    limits = {v.rule: v.limit for v in violations}
    assert limits, "нет нарушений — порог не с чем сверить"
    for rule, limit in sorted(limits.items()):
        assert f"= {limit}" in config, f"{rule}: порог {limit} не найден в pyproject"


def test_the_ledger_is_machine_readable_and_ordered():
    """Ledger — данные, а не текст: прирост обязан быть видимой строкой в диффе."""
    raw = json.loads(
        (REPO_ROOT / "docs" / "agent-context" / "complexity-budget.json").read_text(
            encoding="utf-8"
        )
    )
    keys = [f"{w['path']}::{w['function']}::{w['rule']}" for w in raw["waivers"]]
    assert keys == sorted(keys), "порядок записей нестабилен — диффы будут шумными"
    assert len(keys) == len(set(keys)), "дублирующиеся записи"
