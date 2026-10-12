"""CLI: доставка, долг исходов, экономика ревью и блокировки релиза."""

from __future__ import annotations

import argparse
import json
from io import StringIO
from unittest.mock import MagicMock, patch

from hub import cli
from tests.cli_support import _run_main


def test_cmd_undelivered_names_observation_and_the_frozen_window() -> None:
    """Находка d7b46ff7: пустой человеческий вывод читается как «чисто».

    ``cmd_undelivered`` берёт тот же ответ API, что и MCP, но обходил только
    два ведра из четырёх. Строка, закрытая наблюдением, и строка, вышедшая за
    окно свипа, до человека не доезжали: после закрытия оператор видел одну
    фразу «No completed task is waiting on an open PR» — то есть «проверено,
    чисто» вместо «закрыто чужим наблюдением, вот кем и по какому коммиту».
    Ровно то правило честности, ради которого MCP печатает третье ведро.

    Мутация по местам применения: снять печать наблюдения ИЛИ печать
    застывших строк — тест падает на своём assert поимённо.
    """
    payload = {
        "undelivered": [],
        "unknown": [
            {
                "task_id": 878,
                "title": "Маховик",
                "reason": "провайдер не ответил",
                "age_hours": 467,
                "still_swept": False,
            }
        ],
        "closed_by_observation": [
            {
                "task_id": 909,
                "title": "Паспорт дефекта",
                "state": "unknown",
                # Своя причина, не совпадающая с причиной строки #878 выше:
                # иначе assert про историю проходил бы за счёт чужого ведра.
                "reason": "репозиторий PR #468 удалён, спросить некого",
                "observed_by": "pda_claude",
                "observed_sha": "19ee3f6faf9f",
                "observed_probe": "git show 19ee3f6faf9f --stat",
                "observed_evidence": "файл на месте, AC-тест зелёный",
            }
        ],
        "sweep_lookback_days": 30,
    }
    mock_api = MagicMock(return_value=payload)
    args = argparse.Namespace(limit=50, json=False)
    out = StringIO()
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=out):
        rc = cli.cmd_undelivered(args)
    assert rc == 0
    text = out.getvalue()

    assert "#909" in text and "pda_claude" in text and "19ee3f6faf9f" in text, (
        "закрытая наблюдением строка обязана быть видна человеку с именем "
        "наблюдавшего и коммитом, иначе закрытие читается как «расхождений нет»"
    )
    assert "19ee3f6faf9f --stat" in text and "AC-тест зелёный" in text
    assert "unknown" in text and "репозиторий PR #468 удалён" in text, (
        "прежний ответ реестра — история, а не стёртое состояние"
    )
    assert "30" in text and "#878" in text, (
        "про строку за краем окна свипа сказано вслух: источники больше не "
        "перепрашиваются"
    )
    assert "архив" not in text.lower()
    # Сердце находки: пустой список открытых PR — это НЕ «проверено, чисто»,
    # пока рядом стоит строка, закрытая чужим наблюдением.
    assert "No completed task is waiting on an open PR." not in text, (
        "фраза «расхождений нет» при закрытой наблюдением строке — штамп: "
        "читатель принимает чужое наблюдение за собственную проверку хаба"
    )


def test_delivery_observe_posts_to_the_observation_endpoint() -> None:
    """Находка ревью Codex, сдача 2, ``hub/app.py:2008-2013``.

    ``undelivered`` советует замёрзшей unknown-строке «записать наблюдение»,
    но CLI не давал команды, которая отправила бы probe/observation/sha на
    ``POST .../observation`` — оператор с одним только CLI не мог
    воспользоваться названным выходом. ``delivery-observe`` — этот вход.
    """
    rc, api = _run_main(
        [
            "delivery-observe",
            "909",
            "--probe",
            "git show 19ee3f6faf9f --stat",
            "--observation",
            "файл на месте, AC-тест зелёный",
            "--sha",
            "19ee3f6faf9f",
        ],
        api_result={"task_id": 909, "closed_state": "unknown"},
    )
    assert rc == 0
    api.assert_called_once_with(
        "POST",
        "/api/delivery/discrepancies/909/observation",
        {
            "probe": "git show 19ee3f6faf9f --stat",
            "observation": "файл на месте, AC-тест зелёный",
            "sha": "19ee3f6faf9f",
        },
    )


def test_cmd_undelivered_frozen_pr_open_is_not_told_to_observe() -> None:
    """Находка ревью #1215, 7be892476f84f99c — та же проверка, что MCP.

    ``cmd_undelivered`` строил единый баннер ``frozen`` из ``(*rows,
    *unknown)`` без разбора состояния: замёрзшей строке с открытым PR
    предлагали «записать наблюдение», хотя эндпоинт отвечает 422
    ``source_still_answers`` — наблюдением закрывают только unknown.
    """
    payload = {
        "undelivered": [
            {
                "task_id": 461,
                "title": "Предпас",
                "pr_number": 461,
                "reason": "PR #461 открыт и не смержен",
                "age_hours": 800,
                "still_swept": False,
            }
        ],
        "unknown": [],
        "closed_by_observation": [],
        "sweep_lookback_days": 30,
    }
    mock_api = MagicMock(return_value=payload)
    args = argparse.Namespace(limit=50, json=False)
    out = StringIO()
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=out):
        rc = cli.cmd_undelivered(args)
    assert rc == 0
    text = out.getvalue()
    assert "#461" in text
    assert "Выход — записать наблюдение" not in text
    assert "источник ещё отвечает" in text


def test_cmd_undelivered_all_clear_stays_locked_when_only_observed_remains() -> None:
    """Находка ревью #1215, d9a42edd0c92224f: вырожденный тест не запирает
    конъюнкт ``not observed``.

    ``test_cmd_undelivered_names_observation_and_the_frozen_window`` выше
    держит ``unknown`` непустым РЯДОМ с ``closed_by_observation`` — то есть
    снятие именно ``and not observed`` из ``if not rows and not unknown and
    not observed:`` (строка вернулась бы к прежнему ``if not rows and not
    unknown:``) остаётся зелёным: ``unknown`` и без того запрещает печатать
    «всё чисто». Настоящая боевая форма после закрытия строки — та, что видна
    у #878/#875/#909: ``undelivered`` и ``unknown`` ОБА пусты, стоит только
    ``closed_by_observation``. Этот тест строит именно её.
    """
    payload = {
        "undelivered": [],
        "unknown": [],
        "closed_by_observation": [
            {
                "task_id": 909,
                "title": "Паспорт дефекта",
                "state": "unknown",
                "reason": "репозиторий PR #468 удалён, спросить некого",
                "observed_by": "pda_claude",
                "observed_sha": "19ee3f6faf9f",
                "observed_probe": "git show 19ee3f6faf9f --stat",
                "observed_evidence": "файл на месте, AC-тест зелёный",
            }
        ],
        "sweep_lookback_days": 30,
    }
    mock_api = MagicMock(return_value=payload)
    args = argparse.Namespace(limit=50, json=False)
    out = StringIO()
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=out):
        rc = cli.cmd_undelivered(args)
    assert rc == 0
    text = out.getvalue()

    assert "#909" in text and "pda_claude" in text
    # Сердце находки: undelivered и unknown оба пусты, но строка закрыта
    # чужим наблюдением, а не хабом — «всё чисто» здесь было бы штампом.
    assert "No completed task is waiting on an open PR." not in text, (
        "unknown пуст, но closed_by_observation — нет: печатать «расхождений "
        "нет» здесь значит выдавать чужое наблюдение за собственный вывод хаба"
    )


def test_cmd_undelivered_still_says_all_clear_when_it_really_is() -> None:
    """Обратная сторона той же находки: молчать, когда сказать нечего.

    Убрать ложное «чисто» легко ценой того, что честное «чисто» исчезнет
    вместе с ним, — и тогда пустой вывод снова придётся толковать. Все четыре
    ведра пусты ровно тогда, когда фраза правдива.
    """
    mock_api = MagicMock(
        return_value={
            "undelivered": [],
            "unknown": [],
            "closed_by_observation": [],
            "sweep_lookback_days": 30,
        }
    )
    args = argparse.Namespace(limit=50, json=False)
    out = StringIO()
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=out):
        rc = cli.cmd_undelivered(args)
    assert rc == 0
    assert "No completed task is waiting on an open PR." in out.getvalue()


def test_delivery_deliver_posts_to_the_registry_deliver_endpoint() -> None:
    """#1333: CLI-вход действия реестра «доставить» — рядом с undelivered."""
    rc, api = _run_main(
        ["delivery-deliver", "1276"],
        api_result={
            "task_id": 1276,
            "delivered": True,
            "state": "delivered",
            "pr_number": 425,
        },
    )
    assert rc == 0
    api.assert_called_once_with("POST", "/api/delivery/discrepancies/1276/deliver", {})


def test_review_economy_prints_the_section_of_practice_metrics(capsys) -> None:
    # #1406: CLI reads the same REST section the page and MCP read.
    econ = {"runs": {"total": 3, "billed": 2, "unbilled": 1}}
    rc, api = _run_main(
        ["review-economy", "--since-days", "30"],
        api_result={"since_days": 30, "review_economy": econ},
    )
    assert rc == 0
    assert api.call_args.args[:2] == ("GET", "/api/metrics/practices?since_days=30")
    assert json.loads(capsys.readouterr().out) == econ


def test_main_release_blocks(capsys) -> None:
    # #1420: `oc-hub release-blocks` reads the cheap route and prints the same
    # «Релиз заблокирован с …» line as prod-state and hub_my_context.
    blocks = {
        "release_blocks": [
            {
                "project": "default",
                "reason": "релизный PR #484 не смержен: ci_fail (checks_failed)",
                "since": "2026-09-25 10:43:47",
                "minutes": 99,
                "ci": {"run_url": "", "failed_checks": ["Ruff and pytest"]},
            }
        ]
    }
    rc, api = _run_main(["release-blocks"], api_result=blocks)
    assert rc == 0
    assert api.call_args.args[1] == "/api/release-blocks"
    out = capsys.readouterr().out
    assert "Релиз заблокирован с 2026-09-25 10:43:47 UTC (default, 99 мин)" in out
    assert "упали: Ruff and pytest" in out

    rc, _ = _run_main(["release-blocks"], api_result={"release_blocks": []})
    assert rc == 0
    assert "Открытых аварий релиза нет" in capsys.readouterr().out


def test_outcome_debt_passes_page_flags() -> None:
    """AC-6. Flags become query parameters; no flags, no query."""
    rc, api = _run_main(["outcome-debt"], api_result={})
    assert rc == 0
    assert api.call_args.args[:2] == ("GET", "/api/metrics/outcome-debt")

    rc, api = _run_main(
        [
            "outcome-debt",
            "--status",
            "overdue",
            "--limit",
            "5",
            "--offset",
            "10",
            "--only-counts",
        ],
        api_result={},
    )
    assert rc == 0
    path = api.call_args.args[1]
    base, _, query = path.partition("?")
    assert base == "/api/metrics/outcome-debt"
    assert sorted(query.split("&")) == [
        "limit=5",
        "offset=10",
        "only_counts=true",
        "status=overdue",
    ]
