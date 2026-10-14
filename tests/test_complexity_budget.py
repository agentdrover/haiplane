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


# --- #1693: исключение без задачи у потолка краснит бюджет ------------------


def _entry(function: str, max_: int, removed_by: str, rule: str = "PLR0915") -> dict:
    return {
        "path": "hub/x.py",
        "function": function,
        "rule": rule,
        "measured": max_,
        "max": max_,
        "removed_by": removed_by,
    }


def _violation(function: str, value: int, rule: str = "PLR0915") -> budget.Violation:
    return budget.Violation("hub/x.py", function, rule, value, 60)


def _run_main(monkeypatch, capsys, tmp_path, waivers, violations, *extra):
    """Прогнать настоящий main() по синтетическому ledger и нарушениям."""
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps({"_comment": [], "headroom_pct": 15.0, "waivers": waivers}),
        encoding="utf-8",
    )
    monkeypatch.setattr(budget, "collect_violations", lambda target=None: violations)
    code = budget.main(["--ledger", str(ledger), "--json", *extra])
    return code, json.loads(capsys.readouterr().out)


UNCLAIMED = "unclaimed — долг без задачи"


@pytest.mark.parametrize(
    ("value", "max_", "removed_by", "expected_rc"),
    [
        (90, 100, UNCLAIMED, 1),  # ровно 0,90 — красный
        (89, 100, UNCLAIMED, 0),  # 0,89 — ещё нет
        (95, 100, "task:#1690 — разбиение", 0),
        (95, 100, "permanent — плоский список аргументов", 0),
        (50, 100, "", 1),
        (50, 100, "TODO: задача или причина", 1),
        (50, 100, "permanent —", 1),
        (50, 100, "permanent", 1),
        (50, 100, "unclaimed —   ", 1),
        (50, 100, "task:# — без номера", 1),
        (101, 100, "task:#1690 — выросла", 1),  # live > max, как раньше
    ],
)
def test_unclaimed_waiver_near_its_ceiling_fails_the_budget(
    monkeypatch, capsys, tmp_path, value, max_, removed_by, expected_rc
):
    code, out = _run_main(
        monkeypatch,
        capsys,
        tmp_path,
        [_entry("probe", max_, removed_by)],
        [_violation("probe", value)],
    )
    assert code == expected_rc, out
    assert out["ok"] is (expected_rc == 0)


def test_unclaimed_near_ceiling_names_the_function_hint_and_json_reason(
    monkeypatch, capsys, tmp_path
):
    waivers = [_entry("hot_fn", 100, UNCLAIMED), _entry("cool_fn", 100, UNCLAIMED)]
    violations = [_violation("hot_fn", 93), _violation("cool_fn", 10)]
    code, out = _run_main(monkeypatch, capsys, tmp_path, waivers, violations)
    assert code == 1
    assert [row["function"] for row in out["unclaimed_near_ceiling"]] == ["hot_fn"]
    assert out["unclaimed_near_ceiling"][0]["max"] == 100

    text = budget.format_report(
        budget.check(violations, budget.load_ledger(tmp_path / "ledger.json"))
    )
    assert "hot_fn" in text
    assert "назначьте task:#N или permanent — <причина>" in text


def test_invalid_removed_by_is_listed_in_json(monkeypatch, capsys, tmp_path):
    code, out = _run_main(
        monkeypatch,
        capsys,
        tmp_path,
        [_entry("probe", 100, "TODO: задача или причина")],
        [_violation("probe", 10)],
    )
    assert code == 1
    assert [row["key"] for row in out["invalid_removed_by"]] == [
        "hub/x.py::probe::PLR0915"
    ]


def test_verdict_does_not_depend_on_the_measured_field():
    stale_measure = _entry("probe", 100, UNCLAIMED)
    stale_measure["measured"] = 1
    ledger = {"hub/x.py::probe::PLR0915": stale_measure}
    assert not budget.check([_violation("probe", 95)], ledger).ok
    stale_measure["measured"] = 95
    assert budget.check([_violation("probe", 10)], ledger).ok


def test_add_update_and_its_parts_have_no_waivers(violations, ledger):
    """#1690 AC-1: add_update и вынесенные этапы укладываются в пороги без исключений."""
    parts = ("hub/services/lifecycle.py::add_update", "hub/services/task_updates.py")
    assert [k for k in ledger if k.startswith(parts)] == []
    assert [
        v.key
        for v in violations
        if v.path == "hub/services/task_updates.py"
        or v.key == "hub/services/lifecycle.py::add_update"
    ] == []


def test_repository_ledger_passes_and_refresh_keeps_ceilings(
    violations, ledger, monkeypatch, tmp_path
):
    """AC-2: реальный ledger зелёный; --refresh-measured не трогает ни один max."""
    assert budget.check(violations, ledger).ok

    hot = {
        "hub/app.py::api_task_context::PLR0915": "task:#1691",
        "hub/app.py::api_task_context::C901": "task:#1691",
        "hub/web.py::_web_task_detail_page::PLR0915": "task:#1691",
        "hub/mcp_server.py::hub_project_status::C901": "task:#1691",
        "hub/mcp_server.py::hub_prepare_developer_task::PLR0915": "task:#1691",
        "hub/mcp_server.py::hub_prepare_developer_task::C901": "task:#1691",
        "hub/services/lifecycle.py::create_task::C901": "task:#1692",
    }
    # Запись снимают разбиением функции (#1690–#1692): снятая запись — не ошибка.
    for key, task in hot.items():
        if key in ledger:
            assert str(ledger[key]["removed_by"]).startswith(task), key

    copy = tmp_path / "ledger.json"
    original = json.loads(budget.LEDGER_PATH.read_text(encoding="utf-8"))
    for row in original["waivers"]:
        row["measured"] = 1  # заведомо устаревший замер
    copy.write_text(json.dumps(original, ensure_ascii=False, indent=2) + "\n", "utf-8")

    monkeypatch.setattr(budget, "collect_violations", lambda target=None: violations)
    assert budget.main(["--ledger", str(copy), "--refresh-measured"]) == 0

    refreshed = budget.load_ledger(copy)
    assert set(refreshed) == set(ledger)
    for key, row in refreshed.items():
        assert row["max"] == ledger[key]["max"], key
        assert row["removed_by"] == ledger[key]["removed_by"], key
    live = {f"{v.key}::{v.rule}": v.value for v in violations}
    assert {k: r["measured"] for k, r in refreshed.items()} == live


def test_refresh_measured_and_update_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit):
        budget.main(
            ["--ledger", str(tmp_path / "x.json"), "--update", "--refresh-measured"]
        )
