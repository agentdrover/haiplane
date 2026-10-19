#!/usr/bin/env python3
"""Оффлайн-счёт ревьюера и стюарда по корпусу прошлых промахов (#1864).

Машинное ревью и судья стюарда пропускали дефекты, которые потом находил Codex
или прод. Смена модели, промпта или порога без набора таких случаев — на глаз.
Корпус ``tests/fixtures/review_eval/cases.json`` — случаи с эталоном (дефект
есть / сдача чистая); этот скрипт читает УЖЕ СОХРАНЁННЫЕ отчёты и суждения и
считает полноту на дефектных и долю ложных тревог на чистых.

    uv run python scripts/review_eval.py
    uv run python scripts/review_eval.py --db /path/to/hub.db --plan-cap 5

Сети нет, база открывается только на чтение, вывод побайтово одинаков на одной
базе и одном корпусе. С ``--plan-cap N`` печатается ПЛАН платного перепрогона
ревьюера и его цена — план, а не запуск: деньги тратит человек, прочитавший
число (образец — ``steward_replay.py --backfill-cap``).

Три источника считаются отдельно, у каждого свой знаменатель и покрытие:

* машинное ревью — «обнаружил»: в любом несамом отчёте поколения (с учётом
  переноса ``carried_from_review_id``, без дублей) есть подтверждённая находка
  с файлом локатора и строкой в окне ±20; нераспознанная (unresolved) даёт
  только «насторожился»; ложная тревога — подтверждённая находка на чистой;
* судья стюарда — «удержал» дефектную (не approve); процедурная эскалация
  (хаб превратил ответ в эскалацию, или судья не смог судить) — отдельно;
* советник — только там, где судья одобрил: object по существу на дефектной —
  «поймал», concur — «пропустил», object на чистой — ложная тревога; object,
  превращённый из concur процедурно, — отдельная строка.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

DEFAULT_CORPUS = Path(__file__).resolve().parent.parent / (
    "tests/fixtures/review_eval/cases.json"
)

#: Окно строк вокруг локатора, в котором находка считается попавшей в место.
LINE_WINDOW = 20
EXPECTATIONS = ("defect", "clean")
#: Окно наблюдения чистой: меньше 14 дней не бывает ни в корпусе, ни в счёте.
MIN_WINDOW_DAYS = 14
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Эскалация судьи, которая не суждение о коде: хаб превратил ответ в
#: эскалацию (#1327) либо судья не мог судить (бюджет, сбой, семейство, нет
#: отчёта). Остальные причины — эскалация по существу, она считается как
#: «удержал».
PROCEDURAL_ESCALATES = frozenset(
    {
        "low_confidence",
        "no_grounds",
        "no_confidence",
        "budget_exhausted",
        "daily_cap",
        "run_failed",
        "run_timeout",
        "policy_not_delegated",
        "no_current_report",
        "precondition_failed",
        "same_family_as_implementer",
        "same_family_as_reviewer",
        "self_authored",
        "injection_suspected",
    }
)

STATUS_SCORED = "scored"
STATUS_SHA_MISMATCH = "sha_mismatch"
STATUS_NO_SUBMISSION = "no_submission"
NO_DATA = "нет данных"


# --- корпус ---------------------------------------------------------------


def _is_int(value: Any) -> bool:
    """int без bool: True — подкласс int и прошёл бы как 1."""
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_date(value: Any) -> bool:
    from datetime import date

    try:
        date.fromisoformat(str(value)[:10])
    except ValueError:
        return False
    return True


def load_corpus(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _validate_locators(
    case: dict[str, Any], where: str, classes: set[str]
) -> list[str]:
    errors: list[str] = []
    for loc in case.get("locators", []):
        file_ = loc.get("file")
        start, end = loc.get("start_line"), loc.get("end_line")
        if not isinstance(file_, str) or not file_.strip():
            errors.append(f"{where}: у локатора нет файла")
        if not (_is_int(start) and _is_int(end) and 1 <= start <= end):
            errors.append(f"{where}: локатор {file_}: диапазон строк неверен")
        cls = loc.get("defect_class")
        if cls is not None and cls not in classes:
            errors.append(f"{where}: класс локатора {cls!r} вне словаря")
    return errors


def _validate_truth(case: dict[str, Any], where: str) -> list[str]:
    truth = case.get("truth")
    if not isinstance(truth, dict):
        return [f"{where}: нет ссылки на истину (truth)"]
    kind = truth.get("kind")
    if kind == "human_review":
        if not _is_int(truth.get("update_id")):
            return [f"{where}: truth.human_review без update_id записи kind=review"]
        return []
    if kind == "prod_defect":
        ids = (truth.get("task_id"), truth.get("caused_by_task_id"))
        if not all(_is_int(i) for i in ids):
            return [f"{where}: truth.prod_defect без task_id и caused_by_task_id"]
        return []
    return [f"{where}: неизвестный вид истины {kind!r}"]


def _validate_expectation(
    case: dict[str, Any], where: str, classes: set[str]
) -> list[str]:
    exp = case.get("expectation")
    if exp == "defect":
        errors = _validate_locators(case, where, classes)
        if case.get("defect_class") not in classes:
            errors.append(f"{where}: класс дефекта вне словаря")
        if (
            not case.get("locators")
            and case.get("truth", {}).get("kind") != "prod_defect"
        ):
            errors.append(f"{where}: у дефекта нет локатора")
        if case.get("truth", {}).get("verdict") not in (None, "CHANGES_REQUESTED"):
            errors.append(f"{where}: вердикт истины дефекта не возврат")
        return errors
    errors = []
    delivery = case.get("delivery") or {}
    if case.get("truth", {}).get("verdict") != "APPROVED":
        errors.append(f"{where}: чистая без вердикта APPROVED")
    if not _is_int(delivery.get("update_id")) or not delivery.get("at"):
        errors.append(f"{where}: у чистой нет записи о доставке (delivery)")
    window = case.get("window_days")
    if not case.get("checked_at") or not _is_int(window):
        errors.append(f"{where}: у чистой нет checked_at и window_days")
    elif window < MIN_WINDOW_DAYS:
        errors.append(f"{where}: window_days {window} меньше {MIN_WINDOW_DAYS}")
    dates = (case.get("checked_at"), delivery.get("at"))
    if not all(_valid_date(d) for d in dates if d):
        errors.append(f"{where}: checked_at или delivery.at не дата")
    return errors


def _shape_errors(case: Any) -> list[str]:
    """Типы полей, на которые опираются остальные проверки и сам счёт.

    Битая форма (не словарь, bool вместо числа, строка вместо списка) должна
    вернуться списком ошибок, а не трейсбеком в середине проверки.
    """
    if not isinstance(case, dict):
        return ["случай не объект"]
    where = str(case.get("case_id", "?"))
    errors: list[str] = []
    if not isinstance(case.get("case_id"), str) or not case["case_id"]:
        errors.append(f"{where}: case_id не непустая строка")
    for field in ("task_id", "submission_generation"):
        if not _is_int(case.get(field)):
            errors.append(f"{where}: {field} не целое число")
    if not isinstance(case.get("submission_sha"), str):
        errors.append(f"{where}: submission_sha не строка")
    for field in ("truth", "delivery"):
        if field in case and not isinstance(case[field], dict):
            errors.append(f"{where}: {field} не объект")
    locators = case.get("locators", [])
    if not isinstance(locators, list) or not all(isinstance(x, dict) for x in locators):
        errors.append(f"{where}: locators не список объектов")
    for field in ("defect_class", "expectation", "checked_at"):
        if field in case and not isinstance(case[field], str):
            errors.append(f"{where}: {field} не строка")
    return errors


def validate_case(case: dict[str, Any], classes: set[str]) -> list[str]:
    shape = _shape_errors(case)
    if shape:
        return shape
    where = str(case.get("case_id", "?"))
    errors: list[str] = []
    if not SHA_RE.match(str(case.get("submission_sha", ""))):
        errors.append(f"{where}: submission_sha не 40 hex")
    if case.get("expectation") not in EXPECTATIONS:
        errors.append(f"{where}: expectation вне {EXPECTATIONS}")
        return errors
    return (
        errors
        + _validate_truth(case, where)
        + _validate_expectation(case, where, classes)
    )


def validate_corpus(corpus: dict[str, Any]) -> list[str]:
    cases = corpus.get("cases")
    if not isinstance(cases, list) or not cases:
        return ["корпус пуст"]
    classes = set(corpus.get("defect_classes") or [])
    errors: list[str] = []
    ids = Counter(
        c["case_id"]
        for c in cases
        if isinstance(c, dict) and isinstance(c.get("case_id"), str)
    )
    errors += [f"{i}: case_id повторён" for i, n in sorted(ids.items()) if n > 1]
    for case in cases:
        errors += validate_case(case, classes)
    return errors


def is_provisional(case: dict[str, Any]) -> bool:
    """Окно наблюдения чистой не истекло: по полям корпуса, не по часам."""
    delivered = str((case.get("delivery") or {}).get("at") or "")[:10]
    checked = str(case.get("checked_at") or "")[:10]
    if not delivered or not checked:
        return True
    from datetime import date

    span = date.fromisoformat(checked) - date.fromisoformat(delivered)
    return span.days < max(int(case.get("window_days") or 0), MIN_WINDOW_DAYS)


# --- сопоставление находки с местом -----------------------------------------


def _norm_path(path: Any) -> str:
    text = str(path or "").strip().replace("\\", "/")
    return text[2:] if text.startswith("./") else text


def _span(finding: dict[str, Any]) -> tuple[int, int] | None:
    start = finding.get("start_line") or finding.get("line")
    if not _is_int(start):
        return None
    end = finding.get("end_line")
    return start, end if _is_int(end) and end >= start else start


def finding_matches(finding: Any, locators: list[dict[str, Any]]) -> bool:
    """Подтверждённая находка попала в место дефекта: файл тот же и строки
    пересекают диапазон локатора, расширенный на ``LINE_WINDOW``.

    Находка без строки не «обнаружила»: файл без строки — только тревога.
    """
    if not isinstance(finding, dict):
        return False
    span = _span(finding)
    if span is None:
        return False
    path = _norm_path(finding.get("file"))
    for loc in locators:
        if _norm_path(loc["file"]) != path:
            continue
        if span[0] <= loc["end_line"] + LINE_WINDOW and span[1] >= (
            loc["start_line"] - LINE_WINDOW
        ):
            return True
    return False


def _names_file_without_line(finding: Any, locators: list[dict[str, Any]]) -> bool:
    """Файл назван, строки нет: ревьюер знал модуль, но не место — тревога."""
    if not isinstance(finding, dict) or _span(finding) is not None:
        return False
    path = _norm_path(finding.get("file"))
    return any(_norm_path(loc["file"]) == path for loc in locators)


# --- чтение базы ------------------------------------------------------------


def open_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{quote(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _json_list(raw: Any) -> list[Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def read_submission_sha(conn: sqlite3.Connection, task: int, gen: int) -> str | None:
    row = conn.execute(
        "SELECT sha FROM submissions WHERE task_id=? AND generation=?", (task, gen)
    ).fetchone()
    return None if row is None else str(row["sha"] or "")


def read_reports(conn: sqlite3.Connection, task: int, gen: int) -> list[dict[str, Any]]:
    """Отчёты поколения без самоотчётов, перенесённые — без дублей."""
    rows = conn.execute(
        "SELECT id, carried_from_review_id, findings_confirmed, unresolved "
        "FROM machine_reviews WHERE task_id=? AND submission_generation=? "
        "AND COALESCE(self_reviewed, 0) = 0 ORDER BY id",
        (task, gen),
    ).fetchall()
    seen: set[int] = set()
    reports = []
    for row in rows:
        key = int(row["carried_from_review_id"] or row["id"])
        if key in seen:
            continue
        seen.add(key)
        reports.append(
            {
                "id": int(row["id"]),
                "confirmed": _json_list(row["findings_confirmed"]),
                "unresolved": _json_list(row["unresolved"]),
            }
        )
    return reports


def read_judgement(
    conn: sqlite3.Connection, task: int, gen: int, kind: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, verdict, submitted_verdict, escalate_reason, "
        "COALESCE(contour, 1) AS contour, judged_id FROM steward_judgements "
        "WHERE task_id=? AND generation=? AND kind=?",
        (task, gen, kind),
    ).fetchone()
    return None if row is None else dict(row)


def read_advisor_run(conn: sqlite3.Connection, task: int, gen: int) -> str | None:
    if not _table_exists(conn, "steward_runs"):
        return None
    row = conn.execute(
        "SELECT status FROM steward_runs WHERE task_id=? AND generation=? "
        "AND kind='advisor'",
        (task, gen),
    ).fetchone()
    return None if row is None else str(row["status"] or "")


# --- оценка случая --------------------------------------------------------


def judge_outcome(judge: dict[str, Any] | None) -> str:
    if judge is None:
        return "none"
    if judge["verdict"] == "approve":
        return "approved"
    if (
        judge["verdict"] == "escalate"
        and str(judge["escalate_reason"] or "") in PROCEDURAL_ESCALATES
    ):
        return "procedural"
    return "held"


def _unresolved_may_touch(item: Any, locators: list[dict[str, Any]]) -> bool:
    """Нераспознанная находка тревожит, если не называет чужой файл.

    По схеме у неё нет файла (title, why): такая тревожит всегда. Если файл
    всё же назван и он не из локаторов, это тревога о другом месте.
    """
    if isinstance(item, dict) and item.get("file"):
        return _norm_path(item["file"]) in {_norm_path(loc["file"]) for loc in locators}
    return True


def machine_outcome(case: dict[str, Any], reports: list[dict[str, Any]]) -> str:
    if not reports:
        return "no_report"
    confirmed = [f for r in reports for f in r["confirmed"]]
    if case["expectation"] == "clean":
        return "false_alarm" if confirmed else "clean_ok"
    locators = case.get("locators") or []
    if not locators:
        return "no_locator"
    if any(finding_matches(f, locators) for f in confirmed):
        return "detected"
    if any(
        _unresolved_may_touch(u, locators) for r in reports for u in r["unresolved"]
    ) or any(_names_file_without_line(f, locators) for f in confirmed):
        return "alerted"
    return "missed"


def advisor_outcome(
    case: dict[str, Any],
    judge: dict[str, Any] | None,
    advisor: dict[str, Any] | None,
    run_status: str | None,
) -> str:
    """Советник — только там, где судья нового контура одобрил."""
    if judge is None or judge["verdict"] != "approve" or int(judge["contour"]) != 2:
        return "not_applicable"
    if advisor is not None:
        if advisor["judged_id"] != judge["id"] or advisor["verdict"] not in (
            "concur",
            "object",
        ):
            return "refused"
        return _advisor_answer(case, advisor)
    if run_status is None:
        return "missing"
    if run_status in ("timeout", "never_started"):
        return "timeout"
    return "pending" if run_status == "open" else "refused"


def _advisor_answer(case: dict[str, Any], advisor: dict[str, Any]) -> str:
    defect = case["expectation"] == "defect"
    if advisor["verdict"] == "concur":
        return "missed" if defect else "ok"
    # object: по существу — только присланный как object; превращённый из concur
    # (низкая уверенность, нет оснований) выводится отдельно.
    if advisor["submitted_verdict"] != "object":
        return "procedural_object"
    return "caught" if defect else "false_alarm"


def evaluate_case(conn: sqlite3.Connection, case: dict[str, Any]) -> dict[str, Any]:
    task, gen = int(case["task_id"]), int(case["submission_generation"])
    row: dict[str, Any] = {
        "case_id": case["case_id"],
        "task_id": task,
        "generation": gen,
        "expectation": case["expectation"],
        "provisional": case["expectation"] == "clean" and is_provisional(case),
    }
    stored = read_submission_sha(conn, task, gen)
    if stored is None:
        return {**row, "status": STATUS_NO_SUBMISSION}
    if stored != case["submission_sha"]:
        return {**row, "status": STATUS_SHA_MISMATCH}
    judge = read_judgement(conn, task, gen, "verdict")
    advisor = read_judgement(conn, task, gen, "advisor")
    return {
        **row,
        "status": STATUS_SCORED,
        "machine": machine_outcome(case, read_reports(conn, task, gen)),
        "judge": judge_outcome(judge),
        "advisor": advisor_outcome(
            case, judge, advisor, read_advisor_run(conn, task, gen)
        ),
    }


# --- счёт -----------------------------------------------------------------


def _rate(part: int, whole: int) -> str:
    if not whole:
        return NO_DATA
    return f"{part}/{whole} ({part * 100 / whole:.1f}%)"


def _tally(rows: list[dict[str, Any]], key: str) -> Counter[str]:
    return Counter(r[key] for r in rows if r["status"] == STATUS_SCORED)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    defects = [r for r in rows if r["expectation"] == "defect"]
    cleans = [r for r in rows if r["expectation"] == "clean"]
    return {
        "cases": len(rows),
        "defects": len(defects),
        "cleans": len(cleans),
        "cleans_provisional": sum(1 for r in cleans if r["provisional"]),
        "excluded": dict(
            sorted(
                Counter(
                    r["status"] for r in rows if r["status"] != STATUS_SCORED
                ).items()
            )
        ),
        "machine_defect": _tally(defects, "machine"),
        "machine_clean": _tally(cleans, "machine"),
        "judge_defect": _tally(defects, "judge"),
        "judge_clean": _tally(cleans, "judge"),
        "advisor_defect": _tally(defects, "advisor"),
        "advisor_clean": _tally(cleans, "advisor"),
        "cleans_scored_provisional": sum(
            1 for r in cleans if r["status"] == STATUS_SCORED and r["provisional"]
        ),
        "defects_scored": sum(1 for r in defects if r["status"] == STATUS_SCORED),
        "cleans_scored": sum(1 for r in cleans if r["status"] == STATUS_SCORED),
    }


def _prov(s: dict[str, Any]) -> str:
    """Оговорка к долям по чистым: окно наблюдения может быть не истекло."""
    p = s["cleans_scored_provisional"]
    if not p:
        return ""
    return f"; предварительно: окно не истекло у {p} из {s['cleans_scored']} чистых в счёте"


def _machine_lines(s: dict[str, Any]) -> list[str]:
    d, c = s["machine_defect"], s["machine_clean"]
    evaluable = d["detected"] + d["alerted"] + d["missed"]
    with_reports = sum(c.values()) - c["no_report"]
    return [
        "МАШИННОЕ РЕВЬЮ",
        f"  полнота на дефектных: {_rate(d['detected'], evaluable)}"
        f" (покрытие {_rate(evaluable, s['defects_scored'])})",
        f"  насторожился без обнаружения: {d['alerted']}; пропущено: {d['missed']}",
        f"  без отчёта: {d['no_report']}; без локатора: {d['no_locator']}",
        f"  ложные тревоги на чистых: {_rate(c['false_alarm'], with_reports)}"
        f" (покрытие {_rate(with_reports, s['cleans_scored'])}, без отчёта {c['no_report']})"
        f"{_prov(s)}",
    ]


def _judged_rate(held: int, approved: int) -> str:
    """Доля «удержал» среди суждений по существу: held + approved."""
    if not held + approved:
        return "нет суждений по существу"
    return _rate(held, held + approved)


def _judge_lines(s: dict[str, Any]) -> list[str]:
    d, c = s["judge_defect"], s["judge_clean"]
    return [
        "СУДЬЯ СТЮАРДА (знаменатель — суждения по существу: удержал + одобрил)",
        f"  удержал дефектную (не approve): {_judged_rate(d['held'], d['approved'])}"
        f" (покрытие {_rate(d['held'] + d['approved'], s['defects_scored'])})",
        f"  ложно удержал чистую: {_judged_rate(c['held'], c['approved'])}"
        f" (покрытие {_rate(c['held'] + c['approved'], s['cleans_scored'])}){_prov(s)}",
        f"  вне знаменателя, дефектные: процедурная эскалация {d['procedural']}"
        f"; суждения нет {d['none']}",
        f"  вне знаменателя, чистые: процедурная эскалация {c['procedural']}"
        f"; суждения нет {c['none']}",
    ]


def _advisor_lines(s: dict[str, Any]) -> list[str]:
    d, c = s["advisor_defect"], s["advisor_clean"]
    d_total = d["caught"] + d["missed"]
    c_total = c["false_alarm"] + c["ok"]
    return [
        "СОВЕТНИК (только где судья одобрил)",
        f"  поймал дефектную: {_rate(d['caught'], d_total)}"
        f" (покрытие {_rate(d_total, s['defects_scored'])})",
        f"  ложные тревоги на чистых: {_rate(c['false_alarm'], c_total)}"
        f" (покрытие {_rate(c_total, s['cleans_scored'])}){_prov(s)}",
        "  вне счёта: "
        + ", ".join(
            f"{name} {d[name] + c[name]}"
            for name in (
                "procedural_object",
                "missing",
                "timeout",
                "pending",
                "refused",
                "not_applicable",
            )
        ),
    ]


def render_report(rows: list[dict[str, Any]]) -> str:
    s = summarize(rows)
    excluded = ", ".join(f"{k} {v}" for k, v in s["excluded"].items()) or "нет"
    lines = [
        "КОРПУС ПРОМАХОВ РЕВЬЮ",
        f"  случаев: {s['cases']} (дефектных {s['defects']}, чистых {s['cleans']},"
        f" из них окно не истекло {s['cleans_provisional']})",
        f"  вне счёта: {excluded}",
        "",
        *_machine_lines(s),
        "",
        *_judge_lines(s),
        "",
        *_advisor_lines(s),
        "",
        "СЛУЧАИ",
    ]
    for r in sorted(rows, key=lambda r: r["case_id"]):
        tail = (
            f"машинное {r['machine']}, судья {r['judge']}, советник {r['advisor']}"
            if r["status"] == STATUS_SCORED
            else r["status"]
        )
        mark = " provisional" if r["provisional"] else ""
        lines.append(
            f"  {r['case_id']} #{r['task_id']} gen{r['generation']}"
            f" {r['expectation']}{mark}: {tail}"
        )
    return "\n".join(lines) + "\n"


# --- план платного перепрогона ---------------------------------------------


def plan_targets(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Цели перепрогона, дедуплицированные по (task, generation, sha)."""
    targets: dict[tuple[int, int, str], dict[str, Any]] = {}
    for case in cases:
        key = (
            int(case["task_id"]),
            int(case["submission_generation"]),
            str(case["submission_sha"]),
        )
        targets.setdefault(key, {"key": key, "cases": []})["cases"].append(
            case["case_id"]
        )
    return [targets[k] for k in sorted(targets)]


def read_target_cost(
    conn: sqlite3.Connection, task: int, gen: int
) -> tuple[str, int | None]:
    """Цена прогона ревьюера по этой сдаче: выгрузка (billed) важнее API.

    Источник и число; ``("none", None)`` — данных нет, это не ноль.
    """
    rows = conn.execute(
        "SELECT billed_tokens, provider_tokens FROM review_dispatches "
        "WHERE task_id=? AND submission_generation=? AND agent_id != '' "
        "ORDER BY id DESC",
        (task, gen),
    ).fetchall()
    for row in rows:
        if row["billed_tokens"] is not None:
            return "billed", int(row["billed_tokens"])
    for row in rows:
        if row["provider_tokens"] is not None:
            return "provider", int(row["provider_tokens"])
    return "none", None


def render_plan(
    conn: sqlite3.Connection, cases: list[dict[str, Any]], cap: int, skipped: int = 0
) -> str:
    targets = plan_targets(cases)
    chosen = targets[: max(cap, 0)]
    lines = ["", "ПЛАН ПЕРЕПРОГОНА РЕВЬЮЕРА (только печать, ничего не запускается)"]
    lines.append(
        f"  целей без дублей: {len(targets)}, в плане по потолку {cap}: {len(chosen)}"
    )
    if skipped:
        lines.append(
            f"  исключено из плана: {skipped} случаев со sha, не совпавшим с сдачей"
            " или без сдачи"
        )
    billed = provider = unknown = 0
    for t in chosen:
        task, gen, sha = t["key"]
        source, tokens = read_target_cost(conn, task, gen)
        billed += tokens or 0 if source == "billed" else 0
        provider += tokens or 0 if source == "provider" else 0
        unknown += source == "none"
        cost = {
            "billed": f"{tokens} токенов (выгрузка, billed_tokens)",
            "provider": f"{tokens} токенов (нижняя граница: provider_tokens)",
            "none": NO_DATA,
        }[source]
        lines.append(
            f"  #{task} gen{gen} {sha[:12]}: {cost}; случаи: {', '.join(t['cases'])}"
        )
    lines += _plan_totals(billed, provider, unknown, len(targets) - len(chosen))
    return "\n".join(lines) + "\n"


def _plan_totals(billed: int, provider: int, unknown: int, left_out: int) -> list[str]:
    total = billed + provider
    exact = not provider and not unknown
    mark = "" if exact else "≥ "
    if not total:  # ни одной известной цены: это «нет данных», не ноль
        lines = [f"  оценка цены: {NO_DATA}"]
    else:
        lines = [f"  оценка цены: {mark}{total} токенов"]
    if provider:
        lines.append(f"  из них по provider_tokens (нижняя граница): {provider}")
    if unknown:
        lines.append(f"  целей без данных о цене: {unknown} — в сумму не входят")
    if left_out:
        lines.append(f"  не вошло в план из-за потолка: {left_out}")
    return lines


# --- вход ------------------------------------------------------------------


def run(corpus_path: Path, db_path: str, plan_cap: int | None) -> tuple[int, str]:
    try:
        corpus = load_corpus(corpus_path)
    except (OSError, ValueError) as exc:
        return 1, f"корпус: не читается ({exc})\n"
    if not isinstance(corpus, dict):
        return 1, "корпус: верхний уровень не объект\n"
    errors = validate_corpus(corpus)
    if errors:
        return 1, "".join(f"корпус: {e}\n" for e in errors)
    conn = open_readonly(db_path)
    try:
        rows = [evaluate_case(conn, c) for c in corpus["cases"]]
        out = render_report(rows)
        if plan_cap is not None:
            ok = {r["case_id"] for r in rows if r["status"] == STATUS_SCORED}
            planned = [c for c in corpus["cases"] if c["case_id"] in ok]
            out += render_plan(
                conn, planned, plan_cap, len(corpus["cases"]) - len(planned)
            )
    finally:
        conn.close()
    return 0, out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--db", default=None, help="путь к базе хаба (читается только)")
    parser.add_argument(
        "--plan-cap",
        type=int,
        default=None,
        help="потолок числа прогонов; печатается план и цена, запуска нет",
    )
    args = parser.parse_args(argv)
    db_path = args.db
    if db_path is None:
        from hub.config import HUB_DB_PATH

        db_path = str(HUB_DB_PATH)
    try:
        code, out = run(args.corpus, db_path, args.plan_cap)
    except sqlite3.Error as exc:
        sys.stderr.write(f"база недоступна на чтение: {exc}\n")
        return 2
    sys.stdout.write(out)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
