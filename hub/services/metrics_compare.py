"""Сравнение окон, ряды и ранжирование проблемных мест (#1490, эпик #1462).

Чистые функции: значения показателей берутся из разделов, которые считает
``orchestration._indicator_sections`` тем же кодом, что и агрегат окна. Здесь
ничего не считается из базы — только сравнивается и упорядочивается.

Пороги — из принятой владельцем спеки метрик (``docs/specs/metrics-ux.md``):

* ``MIN_COMPARE_N`` = 10 наблюдений в КАЖДОМ из двух окон (§5.1). Ниже —
  ``insufficient_data``, а не сравнение: дельта не выдумывается.
* ``FLAT_BELOW`` = 5%: относительное изменение меньше — «без изменений» (§5.1).
* ``WORSE_FROM`` = 10%: относительное ухудшение от этого порога попадает в
  «Проблемные места» (§7, п. 3).
* Улучшившиеся показатели в список не попадают; ухудшение без достаточной
  выборки в обоих окнах — тоже.

Порядок проблемных мест (§7), каждая группа выше следующей:

1. ``debt_unchecked`` — категория долга без проверки: по числу задач, затем
   по числу находок, затем по названию;
2. ``rule_breached`` — пробитое правило: по числу пробоев, затем по названию;
3. ``worsened`` — ухудшение против прошлого окна: по относительному
   ухудшению (от нуля — выше всех), затем по ключу показателя;
4. ``data_gap`` — пробел данных: по размеру пробела, затем по названию.

Показывается не больше ``MAX_SPOTS`` строк; остаток — числом в
``problem_spots_more``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hub.services.metrics_scope import MAX_SERIES_BUCKETS, MIN_COMPARE_N

FLAT_BELOW = 0.05
WORSE_FROM = 0.10
MAX_SPOTS = 7
#: Доля стадии «не записана» от которой это пробел данных (§7, п. 4).
UNKNOWN_STAGE_GAP_SHARE = 0.25

INSUFFICIENT = "insufficient_data"
COMPARED = "compared"
REASON_NO_CURRENT = "no_current_data"
REASON_NO_PREVIOUS = "no_previous_data"
REASON_BELOW_MIN = "below_min_n"

#: Порядок групп проблемных мест; индекс — приоритет.
SPOT_KINDS = ("debt_unchecked", "rule_breached", "worsened", "data_gap")

Extracted = tuple[float | int | None, int]


def _first_pass(sections: dict[str, Any]) -> Extracted:
    row = sections.get("review_outcomes") or {}
    return row.get("first_pass_acceptance_rate"), int(row.get("tasks") or 0)


def _precision(sections: dict[str, Any]) -> Extracted:
    row = sections.get("dispositions") or {}
    return row.get("precision"), int(row.get("judged") or 0)


def _tokens_per_fixed(sections: dict[str, Any]) -> Extracted:
    value, n = sections.get("provider_tokens_per_fixed") or (None, 0)
    return value, int(n or 0)


def _touches(sections: dict[str, Any]) -> Extracted:
    row = sections.get("human_touches") or {}
    return row.get("touches_per_delivered"), int(row.get("delivered_tasks") or 0)


def _cfr(sections: dict[str, Any]) -> Extracted:
    rows = (sections.get("change_failure_rate") or {}).get("by_project") or []
    deploys = sum(int(r["deploys"]) for r in rows)
    failed = sum(int(r["failed_deploys"]) for r in rows)
    return (round(failed / deploys, 3) if deploys else None), deploys


def _cycle_feature(sections: dict[str, Any]) -> Extracted:
    for row in sections.get("cycle_times") or []:
        if row.get("work_type") == "feature":
            return row.get("median_hours"), int(row.get("tasks") or 0)
    return None, 0


@dataclass(frozen=True)
class Indicator:
    """Ключевой показатель спеки §4: где он лежит в ответе и что значит «хорошо»."""

    key: str
    label: str
    unit: str
    good_when: str
    #: Поле-источник в ``practice_metrics`` — для подписи и перехода к строкам.
    source_field: str
    #: Зависит ли показатель от модели-ревьюера (спека §8).
    model_dependent: bool
    extract: Callable[[dict[str, Any]], Extracted]


INDICATORS: tuple[Indicator, ...] = (
    Indicator(
        "first_pass",
        "С первого раза (first-pass)",
        "share",
        "higher",
        "review_outcomes.first_pass_acceptance_rate",
        False,
        _first_pass,
    ),
    Indicator(
        "precision",
        "Precision ревью",
        "share",
        "higher",
        "machine_reviews.dispositions.precision",
        True,
        _precision,
    ),
    Indicator(
        "provider_tokens_per_fixed",
        "Цена настоящей находки",
        "tokens",
        "lower",
        "machine_reviews.provider_tokens_per_fixed",
        True,
        _tokens_per_fixed,
    ),
    Indicator(
        "touches_per_delivered",
        "Касаний человека на доставленную",
        "count",
        "lower",
        "human_touches.touches_per_delivered",
        False,
        _touches,
    ),
    Indicator(
        "change_failure_rate",
        "Change failure rate",
        "share",
        "lower",
        "change_failure_rate.by_project[].rate",
        False,
        _cfr,
    ),
    Indicator(
        "cycle_time_feature_median_hours",
        "Cycle time, feature, медиана",
        "hours",
        "lower",
        "cycle_times[work_type=feature].median_hours",
        False,
        _cycle_feature,
    ),
)


def compare_values(
    current: Extracted, previous: Extracted, good_when: str
) -> dict[str, Any]:
    """Текущее, прошлое и дельта; дельта только при выборке в обоих окнах."""
    cur, cur_n = current
    prev, prev_n = previous
    out: dict[str, Any] = {
        "current": cur,
        "current_n": cur_n,
        "previous": prev,
        "previous_n": prev_n,
        "delta": None,
        "delta_rel": None,
        "direction": None,
        "status": INSUFFICIENT,
        "reason": "",
        "min_n": MIN_COMPARE_N,
    }
    if cur is None or cur_n == 0:
        out["reason"] = REASON_NO_CURRENT
        return out
    if prev is None or prev_n == 0:
        out["reason"] = REASON_NO_PREVIOUS
        return out
    if cur_n < MIN_COMPARE_N or prev_n < MIN_COMPARE_N:
        out["reason"] = REASON_BELOW_MIN
        return out
    delta = cur - prev
    rel = delta / abs(prev) if prev else None
    out["status"] = COMPARED
    out["delta"] = round(delta, 3)
    out["delta_rel"] = round(rel, 3) if rel is not None else None
    if delta == 0 or (rel is not None and abs(rel) < FLAT_BELOW):
        out["direction"] = "flat"
    else:
        up_is_good = good_when == "higher"
        out["direction"] = "better" if (delta > 0) == up_is_good else "worse"
    return out


def indicator_values(sections: dict[str, Any]) -> list[dict[str, Any]]:
    """Значение и n каждого ключевого показателя по разделам одного окна."""
    rows = []
    for ind in INDICATORS:
        value, n = ind.extract(sections)
        rows.append({"key": ind.key, "value": value, "n": n})
    return rows


def build_comparison(
    current: dict[str, Any], previous: dict[str, Any], previous_window: dict[str, Any]
) -> dict[str, Any]:
    """Сравнение окна с предыдущим: по строке на показатель."""
    rows = []
    for ind in INDICATORS:
        row = compare_values(ind.extract(current), ind.extract(previous), ind.good_when)
        rows.append(
            {
                "key": ind.key,
                "label": ind.label,
                "unit": ind.unit,
                "good_when": ind.good_when,
                "source_field": ind.source_field,
                "model_dependent": ind.model_dependent,
                **row,
            }
        )
    return {
        "min_compare_n": MIN_COMPARE_N,
        "flat_below": FLAT_BELOW,
        "worse_from": WORSE_FROM,
        "previous_window": previous_window,
        "indicators": rows,
    }


def build_series(
    bucket_sections: list[tuple[dict[str, Any], dict[str, Any]]],
    bucket_days: int,
    dropped: int = 0,
) -> dict[str, Any]:
    """Ряды по интервалам: пустой интервал — ``value: None``, не ноль.

    Если окно длиннее лимита интервалов, старые отброшены, и это названо:
    ``truncated``, ``dropped_buckets`` и ``starts_at`` — фактическое начало.
    """
    indicators = []
    for ind in INDICATORS:
        points = []
        for window, sections in bucket_sections:
            value, n = ind.extract(sections)
            points.append(
                {
                    "from": window["from"],
                    "to": window["to"],
                    # Без наблюдений значения нет: ноль был бы ответом.
                    "value": value if n else None,
                    "n": n,
                }
            )
        indicators.append(
            {
                "key": ind.key,
                "label": ind.label,
                "unit": ind.unit,
                "good_when": ind.good_when,
                "model_dependent": ind.model_dependent,
                "points": points,
            }
        )
    return {
        "bucket_days": bucket_days,
        "truncated": dropped > 0,
        "dropped_buckets": dropped,
        "max_buckets": MAX_SERIES_BUCKETS,
        "starts_at": bucket_sections[0][0]["from"] if bucket_sections else None,
        "indicators": indicators,
    }


def _worsening(row: dict[str, Any]) -> tuple[bool, float]:
    """(заявлять ли ухудшение, вес для сортировки). От нуля вес бесконечен."""
    if row["status"] != COMPARED or row["direction"] != "worse":
        return False, 0.0
    rel = row["delta_rel"]
    if rel is None:
        return True, float("inf")
    return abs(rel) >= WORSE_FROM, abs(rel)


def _debt_spots(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [d for d in metrics.get("category_debt") or [] if not d.get("covered")]
    rows.sort(key=lambda d: (-int(d["tasks"]), -int(d["findings"]), d["category"]))
    return [
        {
            "kind": "debt_unchecked",
            "title": d["category"],
            "size": int(d["tasks"]),
            "source_field": "category_debt[].covered",
            "reason": (
                f"{d['findings']} находок в {d['tasks']} задачах, проверка не заведена"
            ),
        }
        for d in rows
    ]


def _rule_spots(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    rows = list((metrics.get("rule_breaches") or {}).get("breached") or [])
    rows.sort(key=lambda r: (-int(r["breaches"]), r["category"]))
    return [
        {
            "kind": "rule_breached",
            "title": r["category"],
            "size": int(r["breaches"]),
            "source_field": "rule_breaches.breached[]",
            "reason": f"{r['breaches']} находок после заведения правила",
        }
        for r in rows
    ]


def _worsened_spots(comparison: dict[str, Any] | None) -> list[dict[str, Any]]:
    scored = []
    for row in (comparison or {}).get("indicators") or []:
        claimed, weight = _worsening(row)
        if claimed:
            scored.append((weight, row))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["key"]))
    return [
        {
            "kind": "worsened",
            "title": row["label"],
            "size": weight if weight != float("inf") else None,
            "source_field": row["source_field"],
            "reason": (
                f"было {row['previous']} (n={row['previous_n']}), "
                f"стало {row['current']} (n={row['current_n']})"
            ),
            "key": row["key"],
        }
        for weight, row in scored
    ]


def _gap_candidates(metrics: dict[str, Any]) -> list[tuple[int, str, str, str]]:
    economy = metrics.get("review_economy") or {}
    disp = (metrics.get("machine_reviews") or {}).get("dispositions") or {}
    shift = metrics.get("shift_left") or {}
    candidates = [
        (
            int((economy.get("reconciliation") or {}).get("gap") or 0),
            "Расхождение отчётов и оплаченных прогонов",
            "review_economy.reconciliation.gap",
            "отчётов и оплаченных прогонов не сходятся",
        ),
        (
            int(disp.get("confirmed_unjudged") or 0),
            "Неразобранные подтверждённые находки",
            "machine_reviews.dispositions.confirmed_unjudged",
            "подтверждённые находки без ответа",
        ),
        (
            int((economy.get("runs") or {}).get("unbilled") or 0),
            "Прогоны без счёта",
            "review_economy.runs.unbilled",
            "у прогона нет счёта провайдера",
        ),
    ]
    share = shift.get("unknown_share")
    if share is not None and share >= UNKNOWN_STAGE_GAP_SHARE:
        candidates.append(
            (
                int(shift.get("unknown") or 0),
                "Стадия дефекта не записана",
                "shift_left.unknown_share",
                f"стадия неизвестна у {shift.get('unknown')} из "
                f"{shift.get('defects')} дефектов",
            )
        )
    return candidates


def _gap_spots(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [c for c in _gap_candidates(metrics) if c[0] > 0]
    rows.sort(key=lambda c: (-c[0], c[1]))
    return [
        {
            "kind": "data_gap",
            "title": title,
            "size": size,
            "source_field": source,
            "reason": f"{size}: {why}",
        }
        for size, title, source, why in rows
    ]


def rank_problem_spots(
    metrics: dict[str, Any], comparison: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], int]:
    """Ранжированные проблемные места и число не поместившихся.

    Порядок групп и внутри групп задан в докстринге модуля. Без сравнения
    (нет предыдущего окна) группа ``worsened`` пуста: ухудшение не заявляется.
    """
    groups = (
        _debt_spots(metrics),
        _rule_spots(metrics),
        _worsened_spots(comparison),
        _gap_spots(metrics),
    )
    flat = [spot for group in groups for spot in group]
    shown = flat[:MAX_SPOTS]
    return [{"rank": i + 1, **spot} for i, spot in enumerate(shown)], max(
        len(flat) - MAX_SPOTS, 0
    )
