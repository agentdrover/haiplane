#!/usr/bin/env python3
"""Разовый запрос спайка #1168: почему сдача уходит с детерминированного пути.

Спайк, а не механизм. Скрипт ничего не меняет и никуда не пишет: он читает
архив хаба и раскладывает отчёты машинного ревью по четырём корзинам, чтобы
на вопрос «какая доля уходов вызвана качеством ОТЧЁТА, а не качеством кода»
отвечало число, а не интуиция.

Почему запрос вообще нужен, хотя есть hub_practice_metrics
---------------------------------------------------------
Уход с детерминированного пути по качеству отчёта МОЛЧАЛИВ. В
``hub/services/auto_verdict.py`` громкие основания (security, бюджет
токенов, расхождение с соседом) зовут ``_escalate``, который пишет апдейт и
событие ``verdict_escalated``. Условие ``grounds.unattended_blockers`` —
именно то, про которое спрашивает спайк, — возвращает ``False`` без единой
записи. Считать эти уходы по событиям нельзя: событий нет. Единственный
источник — строки ``machine_reviews``, и их надо разложить заново.

Второе: ``unresolved`` не агрегируется в hub_practice_metrics вовсе, хотя
поле пишется в ``machine_review_intake``. Ни доля, ни распределение по
корзинам из метрик не выводятся.

Приоритет отнесения (иначе сумма корзин больше числа уходов)
-----------------------------------------------------------
Условия в auto_verdict соединены через OR и срабатывают вместе. Приоритет
задан ДО подсчёта и не меняется:

    no_data  >  incomplete  >  confirmed  >  unresolved

Первые две и последняя — про качество отчёта; ``confirmed`` — единственная
корзина про качество кода. Число отчётов, где сработало больше одного
условия, печатается отдельно: без него «ровно одна корзина» — это подгонка,
а не факт.

ВАЖНО: приоритет — соглашение о ПОДСЧЁТЕ, а не порядок проверок в коде.
В ``auto_verdict.maybe_auto_verdict`` первым стоит ``unattended_blockers``
(confirmed | unresolved | incomplete), и только потом ``raw_count < 1``.
То есть отчёт, который и неполон, и пуст, в проде уходит по ``incomplete``,
а здесь отнесён к ``no_data``. На головное число это не влияет: обе корзины
— про качество отчёта, и доля quality/departures от перестановки внутри
группы не меняется. На разбивку ПО корзинам влияет, и поэтому сказано вслух.

Три разных определения «нет данных» — и почему здесь взято третье
----------------------------------------------------------------
Их действительно три, и они не совпадают. Разница не косметическая: она
решает, попадёт отчёт в числитель или нет.

1. ``auto_verdict``: ``raw_count < 1`` И неудача проверки proven-empty
   (#769). Проверка ходит к провайдеру за usage и сверяет его с
   ``tokens_spent`` (``_proven_empty_usage``, auto_verdict.py:153-182).
   Воспроизвести её по архиву НЕЛЬЗЯ: нужен живой вызов.
2. ``hub_practice_metrics.no_data_reports``: отрицание
   ``REPORT_HAS_EVIDENCE_SQL`` (orchestration.py:212-218) — ни кандидатов,
   ни находок, ни токенов, ни второго агента.
3. Здесь: ``raw_count < 1`` И ``tokens_spent`` пуст или ноль.

Третье выбрано потому, что оно ТОЧНО, а не приблизительно, отвечает на
вопрос «ушёл ли отчёт». Из кода: proven-empty требует непустого
``tokens_spent``, согласного с оплаченным usage в пределах 25%
(``_USAGE_MISMATCH_SHARE``) при поле не ниже ``EMPTY_REVIEW_MIN_USAGE``.
Если ``tokens_spent`` пуст или ноль, ни одно из условий выполниться не
может — ``_proven_empty_usage`` вернёт ``None``, и уход происходит ВСЕГДА.
Никакого живого вызова для этого вывода не нужно.

Обратное неверно: ``raw_count < 1`` при НЕПУСТОМ ``tokens_spent`` может как
уйти, так и остаться — ответ зависит от живого usage и от потолка класса
(``_within_proven_empty_ceiling``, #835). Такие отчёты не отнесены никуда:
они печатаются отдельной строкой «неразрешимо по архиву». Это отсутствие
данных, а не ноль, и складывать их с уходами нельзя.

Запуск (нужен доступ на чтение к файлу базы хаба):

    python3 scripts/spike_1168_escalation_buckets.py --db ~/.local/state/haiplane-hub/hub.db
    python3 scripts/spike_1168_escalation_buckets.py --selftest

``--selftest`` не трогает базу: он проверяет саму функцию отнесения на
наборе, где условия нарочно перекрываются.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys

#: Корзины про качество ОТЧЁТА. ``confirmed`` сюда не входит намеренно:
#: подтверждённая находка — это про код, и вторая попытка ревью её не
#: отменяет.
REPORT_QUALITY_BUCKETS = ("no_data", "incomplete", "unresolved")

#: Порядок = приоритет отнесения. Первое сработавшее условие забирает отчёт.
BUCKET_ORDER = ("no_data", "incomplete", "confirmed", "unresolved")


def _raw(report: dict) -> int:
    return int(report.get("raw_count") or 0)


def _tokens(report: dict) -> int:
    return int(report.get("tokens_spent") or 0)


def undecidable_no_data(report: dict) -> bool:
    """``raw_count < 1`` при непустом ``tokens_spent`` — ответа в архиве нет.

    Такой отчёт уходит или остаётся по итогу proven-empty (#769): нужен
    живой usage провайдера и потолок класса задачи. Ни то, ни другое из
    ``machine_reviews`` не читается, поэтому отчёт не относится ни к одной
    корзине и считается отдельно.
    """
    return _raw(report) < 1 and _tokens(report) > 0


def has_evidence(report: dict) -> bool:
    """Python-двойник ``REPORT_HAS_EVIDENCE_SQL`` (orchestration.py:212-218).

    Нужен только для сверки с ``hub_practice_metrics.no_data_reports``:
    метрика считает по этому определению, корзины — по своему. Две цифры
    рядом лучше одной, выданной за обе.

    Двойник уже есть в проде — ``orchestration.report_has_evidence``; здесь
    он переписан не по недосмотру, а потому, что скрипт разовый и хаб не
    импортирует вовсе: он читает файл базы через ``sqlite3`` и обязан
    работать там, где пакета ``hub`` на пути нет. Условия сверены со SQL
    построчно (orchestration.py:212-218).
    """
    return bool(
        _raw(report) > 0
        or (report.get("confirmed_n") or 0) > 0
        or (report.get("rejected_n") or 0) > 0
        or _tokens(report) > 0
        or int(report.get("provider_tokens") or 0) > 0
        or int(report.get("agent_count") or 0) > 1
    )


def conditions(report: dict) -> dict[str, bool]:
    """Какие условия ухода сработали на этом отчёте — все, а не первое.

    ``no_data`` — НЕ просто ``raw_count < 1``: см. раздел про три
    определения в докстроке модуля. Здесь взят тот случай, в котором уход
    происходит наверняка и без живого вызова: кандидатов ноль И
    ``tokens_spent`` пуст, а значит proven-empty не может состояться.
    Пограничный случай (кандидатов ноль, но токены есть) сюда не попадает
    и учитывается через :func:`undecidable_no_data`.
    """
    return {
        "no_data": _raw(report) < 1 and _tokens(report) <= 0,
        "incomplete": bool(report.get("incomplete")),
        "confirmed": bool(report.get("confirmed_n") or 0),
        "unresolved": bool(report.get("unresolved_n") or 0),
    }


def bucket_of(report: dict) -> str | None:
    """Ровно одна корзина по заранее заданному приоритету, либо None.

    None означает, что ни одно условие не сработало: отчёт чист и уход с
    детерминированного пути этим отчётом не вызван.
    """
    fired = conditions(report)
    for name in BUCKET_ORDER:
        if fired[name]:
            return name
    return None


def _len_json(blob) -> int:
    if not blob:
        return 0
    try:
        parsed = json.loads(blob)
    except (TypeError, ValueError):
        return 0
    return len(parsed) if isinstance(parsed, list) else 0


def load_reports(db_path: str, since_days: int) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, task_id, submission_generation, harness_skill, "
            "       harness_version, profile, raw_count, incomplete, "
            "       findings_confirmed, findings_rejected, unresolved, "
            "       tokens_spent, provider_tokens, agent_count, "
            "       model, created_at "
            "FROM machine_reviews "
            "WHERE created_at >= datetime('now', ?) "
            "ORDER BY task_id, submission_generation, id",
            (f"-{int(since_days)} days",),
        ).fetchall()
    finally:
        con.close()
    reports = []
    for row in rows:
        item = dict(row)
        item["confirmed_n"] = _len_json(item.pop("findings_confirmed"))
        item["rejected_n"] = _len_json(item.pop("findings_rejected"))
        item["unresolved_n"] = _len_json(item.pop("unresolved"))
        reports.append(item)
    return reports


def report_lines(reports: list[dict]) -> list[str]:
    """Текст отчёта. Каждое число — из одного и того же набора строк."""
    out: list[str] = []
    total = len(reports)

    # AC-2: харнесс v7 считается ОТДЕЛЬНО. Его no_data — закрытый дефект
    # #750 (raw_count=0), то есть исправленное прошлое, а не текущее
    # состояние; в число, которое сравнивается с порогом, он не входит.
    v7 = [r for r in reports if r.get("harness_version") == 7]
    current = [r for r in reports if r.get("harness_version") != 7]

    out.append(f"отчётов в окне: {total}")
    out.append(f"  харнесс v7 (закрытый дефект #750, вне основного числа): {len(v7)}")
    out.append(f"  основное число (всё, кроме v7): {len(current)}")

    buckets: collections.Counter = collections.Counter()
    multi = 0
    clean = 0
    undecidable = 0
    for r in current:
        fired = conditions(r)
        n_fired = sum(1 for v in fired.values() if v)
        if n_fired == 0:
            # Пограничный случай proven-empty считается ОТДЕЛЬНО и только
            # тогда, когда ни одно другое условие не сработало: если отчёт
            # и так неполон, он уходит независимо от исхода proven-empty,
            # и неразрешимость на него не влияет.
            if undecidable_no_data(r):
                undecidable += 1
            else:
                clean += 1
            continue
        if n_fired > 1:
            multi += 1
        name = bucket_of(r)
        assert name is not None
        buckets[name] += 1

    departures = sum(buckets.values())
    out.append("")
    out.append(f"уходов с детерминированного пути: {departures}")
    out.append(f"чистых отчётов (ни одно условие не сработало): {clean}")
    out.append(f"сумма корзин: {departures} — совпадает с числом уходов")
    out.append(f"из них сработало больше одного условия: {multi}")
    out.append(
        f"неразрешимо по архиву (кандидатов 0, но токены есть): {undecidable} "
        "— в корзины НЕ отнесены: исход зависит от живого usage (#769)"
    )
    # Сверка с hub_practice_metrics: метрика считает no_data по другому
    # определению. Числа должны расходиться, и видеть это расхождение
    # лучше, чем считать одно из них «тем же самым».
    by_metric = sum(1 for r in current if not has_evidence(r))
    out.append(
        f"для сверки: no_data по определению метрики (нет свидетельств вовсе): "
        f"{by_metric}"
    )
    out.append("")
    for name in BUCKET_ORDER:
        n = buckets[name]
        share = f"{n / departures:.1%}" if departures else "—"
        kind = "отчёт" if name in REPORT_QUALITY_BUCKETS else "код"
        out.append(f"  {name:<11} {n:>4}  {share:>7}  ({kind})")

    quality = sum(buckets[b] for b in REPORT_QUALITY_BUCKETS)
    share = quality / departures if departures else None
    out.append("")
    out.append(
        "доля уходов ПО КАЧЕСТВУ ОТЧЁТА: "
        + (
            f"{quality}/{departures} = {share:.1%}"
            if share is not None
            else "нет уходов"
        )
    )
    out.append("порог решения (назван до подсчёта): 10%")

    # Признак круга (пункт спайка про «повтор БЕЗ изменения ловится, а круг
    # с настоящими правками — нет»): траектория по поколениям одной задачи.
    # Полезная работа сводит confirmed к нулю; круг оставляет unresolved на
    # месте, сколько бы правок ни было внесено.
    out.append("")
    out.append("траектория по поколениям (задачи с 2+ отчётами на разных поколениях):")
    by_task: dict[int, list[dict]] = collections.defaultdict(list)
    for r in current:
        by_task[r["task_id"]].append(r)
    circles = 0
    converged = 0
    for task_id, rs in sorted(by_task.items()):
        gens = sorted({r["submission_generation"] for r in rs})
        if len(gens) < 2:
            continue
        first = min(rs, key=lambda r: (r["submission_generation"], r["id"]))
        last = max(rs, key=lambda r: (r["submission_generation"], r["id"]))
        trend_c = last["confirmed_n"] - first["confirmed_n"]
        if last["confirmed_n"] == 0 and last["unresolved_n"] == 0:
            converged += 1
            mark = "сошлось"
        elif trend_c <= 0 and last["unresolved_n"] >= first["unresolved_n"]:
            circles += 1
            mark = "КРУГ: confirmed падает, unresolved не падает"
        else:
            mark = "—"
        out.append(
            f"  #{task_id}: поколений {len(gens)}, "
            f"confirmed {first['confirmed_n']}→{last['confirmed_n']}, "
            f"unresolved {first['unresolved_n']}→{last['unresolved_n']}  {mark}"
        )
    out.append(f"  сошлось: {converged}; с признаком круга: {circles}")

    out.append("")
    out.append("цена одной второй попытки (provider-токены, по профилям):")
    by_profile: dict[str, list[int]] = collections.defaultdict(list)
    for r in current:
        tokens = r.get("provider_tokens")
        if tokens:
            by_profile[(r.get("profile") or "не заявлен")].append(int(tokens))
    for profile, values in sorted(by_profile.items()):
        avg = sum(values) // len(values)
        out.append(
            f"  {profile:<11} {avg:>12,} токенов/прогон  (выборка {len(values)})"
        )
    if not by_profile:
        out.append("  недобор: ни у одного отчёта в окне нет provider_tokens")
    return out


def selftest() -> int:
    """Проверка самой функции отнесения — без базы.

    Смысл в перекрытии: набор подобран так, что почти каждый отчёт
    удовлетворяет нескольким условиям сразу. Если приоритет сломается,
    сумма корзин разъедется с числом уходов.
    """
    cases = [
        # (отчёт, ожидаемая корзина)
        (
            {"raw_count": 0, "incomplete": 1, "confirmed_n": 3, "unresolved_n": 2},
            "no_data",
        ),
        (
            {"raw_count": 5, "incomplete": 1, "confirmed_n": 3, "unresolved_n": 2},
            "incomplete",
        ),
        (
            {"raw_count": 5, "incomplete": 0, "confirmed_n": 3, "unresolved_n": 2},
            "confirmed",
        ),
        (
            {"raw_count": 5, "incomplete": 0, "confirmed_n": 0, "unresolved_n": 2},
            "unresolved",
        ),
        ({"raw_count": 5, "incomplete": 0, "confirmed_n": 0, "unresolved_n": 0}, None),
        # raw_count=0 — «ноль кандидатов», а не «ноль находок»: корзина
        # no_data забирает отчёт даже когда всё остальное молчит.
        (
            {"raw_count": 0, "incomplete": 0, "confirmed_n": 0, "unresolved_n": 0},
            "no_data",
        ),
        # Пограничный случай proven-empty: кандидатов ноль, но прогон
        # оплачен. Уход НЕ предрешён — корзины нет. Раньше здесь стояло
        # безусловное no_data, и это была ошибка: отчёт мог остаться на
        # детерминированном пути (#769).
        (
            {
                "raw_count": 0,
                "tokens_spent": 250000,
                "incomplete": 0,
                "confirmed_n": 0,
                "unresolved_n": 0,
            },
            None,
        ),
        # Тот же оплаченный пустой прогон, но ещё и неполный: уходит по
        # incomplete независимо от исхода proven-empty.
        (
            {
                "raw_count": 0,
                "tokens_spent": 250000,
                "incomplete": 1,
                "confirmed_n": 0,
                "unresolved_n": 0,
            },
            "incomplete",
        ),
    ]
    failures = 0
    for report, expected in cases:
        got = bucket_of(report)
        if got != expected:
            print(f"FAIL: {report} → {got}, ожидалось {expected}")
            failures += 1
    departures = [c for c, _ in cases if bucket_of(c) is not None]
    counted = collections.Counter(bucket_of(c) for c in departures)
    if sum(counted.values()) != len(departures):
        print("FAIL: сумма корзин не равна числу уходов")
        failures += 1
    # Каждый уход попал ровно в одну корзину, хотя условий сработало больше.
    overlapping = [c for c in departures if sum(conditions(c).values()) > 1]
    if len(overlapping) < 3:
        print("FAIL: набор не проверяет перекрытие условий")
        failures += 1
    if failures:
        print(f"selftest: провалов {failures}")
        return 1
    print(
        f"selftest: ок, случаев {len(cases)}, из них с перекрытием {len(overlapping)}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "HAIPLANE_HUB_DB",
            os.path.expanduser("~/.local/state/haiplane-hub/hub.db"),
        ),
        help="путь к файлу базы хаба (только чтение)",
    )
    parser.add_argument("--since-days", type=int, default=90)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if not os.path.exists(args.db):
        print(f"базы нет: {args.db}", file=sys.stderr)
        print(
            "спайк считает по архиву хаба; без файла базы числа не получить — "
            "это отсутствие данных, а не ноль",
            file=sys.stderr,
        )
        return 2
    reports = load_reports(args.db, args.since_days)
    print(f"окно: {args.since_days} дней, база: {args.db}")
    for line in report_lines(reports):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
