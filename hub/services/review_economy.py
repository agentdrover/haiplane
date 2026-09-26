"""Сводка ревью для владельца (#1406, спека review-economy T1).

Один агрегат, который читают все поверхности — REST, MCP, CLI и ``/metrics``:
сколько прогонов ревью куплено, что за них выставил провайдер и что они дали.

Правила, за которые уже заплачено:

* Цена — только ``review_dispatches.provider_tokens``, счёт провайдера.
  ``tokens_spent`` — самоотчёт харнесса, занижен в 12–62 раза; он не входит
  ни в одно число здесь (#828, #1026).
* Прогон без счёта — своя строка. Не ноль (он не бесплатный) и не среднее
  (его цена неизвестна) — #549.
* ``unresolved`` не складывается с ``confirmed`` и не вычитается из него:
  неразрешённые находки оказались настоящими в 24 из 25 случаев (#1235),
  и сумма спрятала бы именно ту часть, которую никто не разобрал.
* Самоотчёт (``self_reviewed``) — не независимое ревью; он считается рядом.
* Доля с выборкой меньше :data:`MIN_SAMPLE` помечается недобором (#1153).

Прогон — строка ``review_dispatches`` с агентом (``agent_id != ''``): заглушка
отказа (#1242) не запускалась и счёта не имеет.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from hub.db import fetchall

#: Ниже этой выборки доля печатается с пометкой «недобор» (#1153).
MIN_SAMPLE = 20

#: Колонка когорты профиля (#1403). Пока её нет — строка «нет данных».
PROFILE_ASSIGNMENT_COLUMN = "profile_assignment"

#: Типы заказа в порядке классификации. Первый совпавший признак решает.
RUN_KINDS: tuple[tuple[str, str], ...] = (
    ("first", "первый заказ сдачи"),
    ("ladder", "добор лестницы (#879)"),
    ("cascade", "каскад: другая модель (#1243)"),
    ("ask_again", "переспрос после потерянного отчёта (#1242)"),
    ("second_door", "вторая дверь: локальный прогон после отказа облака (#1252)"),
)

#: Корзины сверки отчётов со счётом, со знаком вклада в расхождение.
RECONCILIATION_BUCKETS: tuple[tuple[str, str], ...] = (
    ("self_reviewed", "самоотчёт автора: прогона хаба за ним нет"),
    ("no_dispatch", "отчёт без заказа хаба: внешний ревьюер или ручная сдача"),
    ("local_door", "локальная дверь: прогон без счёта провайдера"),
    ("dispatch_without_bill", "облачный заказ, счёт по которому не получен"),
    ("paid_without_report", "оплаченный прогон без отчёта (вычитается)"),
    ("unexplained", "необъяснённый остаток"),
)

_UNDECLARED = "не заявлен"


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _sample(n: int) -> dict[str, Any]:
    return {"n": n, "undersampled": n < MIN_SAMPLE}


async def _runs(db: aiosqlite.Connection, since: str) -> list[dict[str, Any]]:
    """Прогоны окна с типом заказа, каналом и счётом."""
    from hub.services.review_dispatch import MODEL_CASCADE_EVENT

    rows = await fetchall(
        db,
        "SELECT d.id, d.task_id, d.submission_generation AS generation, "
        "d.profile, d.provider_tokens AS bill, d.status, d.channel, "
        "d.replaces_dispatch_id AS replaces, "
        "EXISTS (SELECT 1 FROM review_dispatches p WHERE p.task_id = d.task_id "
        "AND p.submission_generation = d.submission_generation "
        "AND p.agent_id != '' AND p.id < d.id) AS has_earlier "
        "FROM review_dispatches d "
        "WHERE d.agent_id != '' AND d.created_at >= datetime('now', ?) "
        "ORDER BY d.id",
        (since,),
    )
    cascade_rows = await fetchall(
        db,
        "SELECT payload FROM events WHERE kind = ?",
        (MODEL_CASCADE_EVENT,),
    )
    cascade_ids = {_cascade_dispatch_id(r["payload"]) for r in cascade_rows}
    runs = []
    for row in rows:
        run = dict(row)
        run["kind"] = _run_kind(run, cascade_ids)
        run["profile"] = run["profile"] or _UNDECLARED
        runs.append(run)
    return runs


def _cascade_dispatch_id(payload: str | None) -> int | None:
    try:
        value = json.loads(payload or "{}").get("dispatch_id")
    except (ValueError, AttributeError):
        return None
    return int(value) if isinstance(value, int) else None


def _run_kind(run: dict[str, Any], cascade_ids: set[int | None]) -> str:
    if run["id"] in cascade_ids:
        return "cascade"
    if run["replaces"] is not None:
        return "second_door" if run["channel"] == "local" else "ask_again"
    return "ladder" if run["has_earlier"] else "first"


def _bill_row(runs: list[dict[str, Any]]) -> dict[str, Any]:
    billed = [r for r in runs if r["bill"] is not None]
    tokens = sum(int(r["bill"]) for r in billed)
    return {
        "runs": len(runs),
        "billed_runs": len(billed),
        "unbilled_runs": len(runs) - len(billed),
        "provider_tokens_total": tokens,
        # Средняя — только по прогонам со счётом: деление на все прогоны
        # выдало бы прогон без счёта за бесплатный.
        "provider_tokens_per_run": round(tokens / len(billed)) if billed else None,
    }


def _runs_section(runs: list[dict[str, Any]]) -> dict[str, Any]:
    total = _bill_row(runs)
    profiles = sorted({r["profile"] for r in runs})
    return {
        "total": total["runs"],
        "billed": total["billed_runs"],
        "unbilled": total["unbilled_runs"],
        "provider_tokens_total": total["provider_tokens_total"],
        "provider_tokens_per_billed_run": total["provider_tokens_per_run"],
        "by_profile": [
            {"profile": p, **_bill_row([r for r in runs if r["profile"] == p])}
            for p in profiles
        ],
        "by_kind": [
            {
                "kind": kind,
                "label": label,
                **_bill_row([r for r in runs if r["kind"] == kind]),
            }
            for kind, label in RUN_KINDS
        ],
    }


async def _findings_section(db: aiosqlite.Connection, since: str) -> dict[str, Any]:
    """confirmed и unresolved — разными строками; самоотчёт — рядом."""
    from hub.services.orchestration import ORIGINAL_READ_SQL, REPORT_HAS_EVIDENCE_SQL

    rows = await fetchall(
        db,
        "SELECT CASE WHEN profile = '' THEN ? ELSE profile END AS profile, "
        "self_reviewed, "
        f"{REPORT_HAS_EVIDENCE_SQL} AS has_evidence, "  # nosec B608 - module constant
        "json_array_length(findings_confirmed) AS confirmed, "
        "json_array_length(COALESCE(unresolved, '[]')) AS unresolved "
        "FROM machine_reviews WHERE created_at >= datetime('now', ?) "
        # #1361: перенос — не отчёт и не чтение; его находки уже посчитаны
        # в исходном отчёте.
        f"AND {ORIGINAL_READ_SQL}",
        (_UNDECLARED, since),
    )
    reports = [dict(r) for r in rows]
    own = [r for r in reports if r["self_reviewed"]]
    independent = [r for r in reports if not r["self_reviewed"] and r["has_evidence"]]
    with_unresolved = sum(1 for r in independent if r["unresolved"])
    return {
        "independent_reports": len(independent),
        "no_data_reports": sum(
            1 for r in reports if not r["self_reviewed"] and not r["has_evidence"]
        ),
        "reports_with_confirmed": sum(1 for r in independent if r["confirmed"]),
        "confirmed_total": sum(r["confirmed"] for r in independent),
        "reports_with_unresolved": with_unresolved,
        "unresolved_total": sum(r["unresolved"] for r in independent),
        "reports_with_both": sum(
            1 for r in independent if r["confirmed"] and r["unresolved"]
        ),
        "unresolved_report_share": _share(with_unresolved, len(independent)),
        **_sample(len(independent)),
        "self_reviewed": {
            "reports": len(own),
            "confirmed": sum(r["confirmed"] for r in own),
            "unresolved": sum(r["unresolved"] for r in own),
        },
        "by_profile": [
            {
                "profile": p,
                "reports": len(mine),
                "confirmed": sum(r["confirmed"] for r in mine),
                "unresolved": sum(r["unresolved"] for r in mine),
            }
            for p in sorted({r["profile"] for r in independent})
            for mine in [[r for r in independent if r["profile"] == p]]
        ],
    }


#: Строки «CI закреплённого sha» в порядке показа. Вместе дают все прогоны:
#: молчание CI — не успех и не провал, у него свои строки (находка
#: a5370258c13e33c0, тот же принцип, что в ci_report.py и red_base.py).
CI_ROWS: tuple[str, ...] = ("red", "green", "undetermined", "skipped", "no_report")


def _ci_row(reported: bool, status: str | None) -> str:
    """Строка прогона по отчёту CI на закреплённом sha его сдачи."""
    from hub.services.validation_run import FAIL, PASS, SKIPPED

    if not reported:
        return "no_report"
    return {FAIL: "red", PASS: "green", SKIPPED: "skipped"}.get(
        status or "", "undetermined"
    )


async def _red_ci_section(
    db: aiosqlite.Connection, runs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Прогоны, купленные на сдаче, чей закреплённый sha CI назвал fail.

    ``unknown`` (и любой непризнанный статус) — «CI не определился»,
    ``skipped`` — «проверки пропущены»: ни то, ни другое не красное и не
    зелёное, и ни то, ни другое не «отчёта нет» — отчёт есть.
    """
    rows = await fetchall(
        db,
        "SELECT s.task_id, s.generation, c.id IS NOT NULL AS reported, "
        "c.validation_status AS ci "
        "FROM submissions s LEFT JOIN ci_run_reports c "
        "ON c.task_id = s.task_id AND c.head_sha = s.sha",
    )
    ci_of = {
        (r["task_id"], r["generation"]): _ci_row(bool(r["reported"]), r["ci"])
        for r in rows
    }
    by_row: dict[str, list[dict[str, Any]]] = {name: [] for name in CI_ROWS}
    for run in runs:
        by_row[ci_of.get((run["task_id"], run["generation"]), "no_report")].append(run)
    red, green = by_row["red"], by_row["green"]
    bill = _bill_row(red)
    return {
        "runs": bill["runs"],
        "billed_runs": bill["billed_runs"],
        "unbilled_runs": bill["unbilled_runs"],
        "provider_tokens": bill["provider_tokens_total"],
        "runs_on_green_ci": len(green),
        "runs_ci_undetermined": len(by_row["undetermined"]),
        "runs_ci_skipped": len(by_row["skipped"]),
        "runs_without_ci_report": len(by_row["no_report"]),
        "red_share": _share(len(red), len(red) + len(green)),
        **_sample(len(red) + len(green)),
    }


async def _cohort_section(
    db: aiosqlite.Connection, runs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Когорта назначения профиля rule/random (#1403) — если колонка есть."""
    columns = await fetchall(db, "PRAGMA table_info(review_dispatches)")
    if PROFILE_ASSIGNMENT_COLUMN not in {c["name"] for c in columns}:
        return {
            "available": False,
            "note": "нет данных: колонка profile_assignment (#1403) не заведена",
            "cohorts": [],
        }
    ids = [r["id"] for r in runs]
    if not ids:
        return {"available": True, "note": "", "cohorts": []}
    marks = await fetchall(
        db,
        f"SELECT id, {PROFILE_ASSIGNMENT_COLUMN} AS cohort "  # nosec B608 - constant
        "FROM review_dispatches WHERE id IN "
        f"({','.join('?' * len(ids))})",
        ids,
    )
    cohort_of = {m["id"]: (m["cohort"] or _UNDECLARED) for m in marks}
    names = sorted(set(cohort_of.values()))
    return {
        "available": True,
        "note": "",
        "cohorts": [
            {
                "cohort": name,
                **_bill_row([r for r in runs if cohort_of.get(r["id"]) == name]),
            }
            for name in names
        ],
    }


async def _unbilled_report_buckets(
    db: aiosqlite.Connection, since: str
) -> dict[str, int]:
    """Разложить отчёты без своего счёта по заказам их сдачи."""
    from hub.services.orchestration import ORIGINAL_READ_SQL

    rows = await fetchall(
        db,
        "SELECT m.self_reviewed, "  # nosec B608 - module constant
        "SUM(CASE WHEN d.id IS NOT NULL THEN 1 ELSE 0 END) AS orders, "
        "SUM(CASE WHEN d.channel = 'local' THEN 1 ELSE 0 END) AS local_orders, "
        "SUM(CASE WHEN d.channel != 'local' AND d.provider_tokens IS NULL "
        "THEN 1 ELSE 0 END) AS unbilled_orders "
        "FROM machine_reviews m LEFT JOIN review_dispatches d "
        "ON d.task_id = m.task_id "
        "AND d.submission_generation = m.submission_generation "
        "AND d.agent_id != '' "
        "WHERE m.created_at >= datetime('now', ?) AND m.provider_tokens IS NULL "
        # #1361: у переноса счёта нет потому, что прогона не было, — это не
        # отчёт «без счёта». Столбец есть только у machine_reviews.
        f"AND {ORIGINAL_READ_SQL} "
        "GROUP BY m.id",
        (since,),
    )
    counts = dict.fromkeys(
        ("self_reviewed", "no_dispatch", "local_door", "dispatch_without_bill"), 0
    )
    for row in rows:
        if row["self_reviewed"]:
            counts["self_reviewed"] += 1
        elif not row["orders"]:
            counts["no_dispatch"] += 1
        elif row["local_orders"]:
            counts["local_door"] += 1
        elif row["unbilled_orders"]:
            counts["dispatch_without_bill"] += 1
        # Иначе у сдачи есть оплаченный облачный заказ, а счёт на отчёт не
        # лёг: названной причины нет — это часть необъяснённого остатка.
    return counts


async def _reconciliation_section(
    db: aiosqlite.Connection, since: str, runs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Сверка числа отчётов с числом оплаченных прогонов.

    Расхождение раскладывается по названным корзинам; то, что ни одна не
    объясняет, публикуется отдельной корзиной с числом, а не распределяется
    по остальным. Сумма корзин равна расхождению по построению.
    """
    from hub.services.orchestration import ORIGINAL_READ_SQL

    total = await fetchall(
        db,
        "SELECT COUNT(*) AS n FROM machine_reviews "  # nosec B608 - module constant
        # #1361: сверяются отчёты с прогонами; перенос прогоном не был.
        f"WHERE created_at >= datetime('now', ?) AND {ORIGINAL_READ_SQL}",
        (since,),
    )
    reports = int(total[0]["n"] or 0)
    paid = [r for r in runs if r["bill"] is not None]
    counts: dict[str, int] = dict(await _unbilled_report_buckets(db, since))
    counts["paid_without_report"] = -sum(1 for r in paid if r["status"] != "done")
    gap = reports - len(paid)
    counts["unexplained"] = gap - sum(counts.values())
    buckets: list[dict[str, Any]] = [
        {"bucket": key, "label": label, "count": counts[key]}
        for key, label in RECONCILIATION_BUCKETS
    ]
    named = [b for b in buckets if b["bucket"] != "unexplained" and b["count"]]
    main = max(named, key=lambda b: abs(b["count"]), default=None)
    return {
        "reports": reports,
        "paid_runs": len(paid),
        "gap": gap,
        "buckets": buckets,
        "main_cause": main["label"] if main else "",
    }


async def _dispatched_payloads(
    db: aiosqlite.Connection, since: str
) -> list[tuple[tuple[Any, Any], dict[str, Any]]]:
    """Записи «ревью вызвано» окна: ключ сдачи (задача, поколение) и payload."""
    rows = await fetchall(
        db,
        "SELECT task_id, payload FROM events WHERE kind = 'review_dispatched' "
        "AND created_at >= datetime('now', ?)",
        (since,),
    )
    out: list[tuple[tuple[Any, Any], dict[str, Any]]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        if isinstance(payload, dict):
            out.append(((row["task_id"], payload.get("generation")), payload))
    return out


def _names_reason(payload: dict[str, Any], mark: str) -> bool:
    reasons = payload.get("profile_reasons") or []
    return isinstance(reasons, list) and any(mark in str(r) for r in reasons)


async def _deep_cap_section(db: aiosqlite.Connection, since: str) -> dict[str, Any]:
    """Сколько сдач окна суточный потолок deep увёл в lite (#1414).

    Считаются сдачи (задача, поколение) по записям «ревью вызвано»: та же
    причина, что видна в карточке. Доля — от всех вызванных сдач окна;
    пересмотр потолка обещан при доле выше половины за неделю.
    """
    from hub.services.review_dispatch import DEEP_CAP_REASON_MARK

    payloads = await _dispatched_payloads(db, since)
    dispatched = {key for key, _ in payloads}
    capped = {key for key, p in payloads if _names_reason(p, DEEP_CAP_REASON_MARK)}
    return {
        "downgraded_submissions": len(capped),
        "dispatched_submissions": len(dispatched),
        "downgraded_share": _share(len(capped), len(dispatched)),
        **_sample(len(dispatched)),
    }


def _is_resubmission(key: tuple[Any, Any]) -> bool:
    generation = key[1]
    return isinstance(generation, int) and generation >= 2


async def _small_delta_section(db: aiosqlite.Connection, since: str) -> dict[str, Any]:
    """Сколько пересдач окна правило маленькой дельты увело в lite (#1416).

    Доля — от вызванных пересдач (поколение ≥ 2): по ней подбирается порог,
    а эскейпы (#528) на таких пересдачах — условие его пересмотра.
    """
    from hub.services.review_dispatch import SMALL_DELTA_REASON_MARK

    payloads = await _dispatched_payloads(db, since)
    resubmitted = {key for key, _ in payloads if _is_resubmission(key)}
    small = {
        key
        for key, p in payloads
        if key in resubmitted and _names_reason(p, SMALL_DELTA_REASON_MARK)
    }
    return {
        "downgraded_submissions": len(small),
        "resubmissions": len(resubmitted),
        "downgraded_share": _share(len(small), len(resubmitted)),
        **_sample(len(resubmitted)),
    }


def _escapes_section(escaped: dict[str, Any]) -> dict[str, Any]:
    """Эскейпы (#528) с корзинами непосчитанного рядом, не внутри."""
    return {
        "escaped": escaped.get("escaped", 0),
        "bugs_in_window": escaped.get("bugs_in_window", 0),
        "uncounted": {
            "bugs_without_feature": escaped.get("bugs_without_feature", 0),
            "features_without_completion": escaped.get(
                "features_without_completion", 0
            ),
        },
    }


async def review_economy(
    db: aiosqlite.Connection, *, since_days: int, escaped: dict[str, Any]
) -> dict[str, Any]:
    """Раздел ``review_economy`` в ``practice_metrics`` (#1406)."""
    since = f"-{since_days} days"
    runs = await _runs(db, since)
    return {
        "since_days": since_days,
        "min_sample": MIN_SAMPLE,
        "runs": _runs_section(runs),
        "findings": await _findings_section(db, since),
        "red_ci": await _red_ci_section(db, runs),
        "profile_assignment": await _cohort_section(db, runs),
        "reconciliation": await _reconciliation_section(db, since, runs),
        "deep_cap": await _deep_cap_section(db, since),
        "small_delta": await _small_delta_section(db, since),
        "escapes": _escapes_section(escaped),
    }
