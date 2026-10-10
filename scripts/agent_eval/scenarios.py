"""MCP-сценарии агента и оценщик трасс над песочницей (#1224).

Контрактные тесты MCP не проверяют, ЧТО делает агент: выбирает ли он нужный
инструмент и как реагирует на отказ хаба. Здесь шесть версионированных
сценариев (``tests/fixtures/agent_eval/scenarios.json``): у каждого начальное
состояние в БД песочницы (#1223), цель, обязательные и запрещённые вызовы и
проверяемое конечное состояние в той же БД.

Правила оценки:

* значимые вызовы проверяются по частичному порядку (``after``), а не по
  точной последовательности; безвредные чтения (``safe_reads``) допустимы в
  любом месте и порядке и вердикт не меняют;
* текстовый «успех» агента ничего не компенсирует: запрещённый вызов,
  выход за ``bound_task`` или неверная строка в БД остаются нарушениями, а
  в объяснении к ним записано, что ответ заявлял успех;
* ``stale_approval``, ``red_ci`` и ``scope_violation`` — раздельные
  критические отказы; у каждого нарушения свой вид;
* исходы те же, что в контракте прогона (#1221): ``passed``,
  ``quality_failed``, ``infrastructure_error``, ``incomplete``. Пустая трасса
  и пропавший сценарий — ``incomplete``, сбой агента — ``infrastructure_error``.

Агент — объект с ``async run(task) -> str`` (получить цель, вызвать
инструменты MCP песочницы, вернуть текст). В тестах это ``ScriptedAgent``:
заглушка, её прогон НЕ результат реальной модели (``meta.agent_kind``).
Платная модель и сеть наружу здесь не вызываются.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent_eval import contract as ct
from agent_eval import sandbox as sb

SCHEMA_VERSION = 1
STALE_APPROVAL = "stale_approval"
RED_CI = "red_ci"
SCOPE_VIOLATION = "scope_violation"
CRITICAL_KINDS = (STALE_APPROVAL, RED_CI, SCOPE_VIOLATION)

#: Заявленный набор: пропавший из файла сценарий — проблема, а не молчание.
EXPECTED_IDS = (
    "submit-success",
    "stale-approval",
    "red-ci",
    "changes-requested-resubmit",
    "unavailable-fact",
    "scope-violation",
)
DEFAULT_SUITE = sb.REPO_ROOT / "tests" / "fixtures" / "agent_eval" / "scenarios.json"
AGENT_NAME = "eval-agent"
#: Аргументы вызова, которые называют задачу. Любое значение вне bound_task —
#: выход за границу, и для чтения тоже.
SCOPE_KEYS = frozenset(
    {
        "task_id",
        "task_ids",
        "parent_id",
        "depends_on_task_id",
        "blocked_by_task_id",
        "linked_task_id",
    }
)
_TABLES = ("tasks", "ci_run_reports")
_OPS = ("eq", "ne", "in", "empty")
_CHECK_KINDS = ("field", "created_tasks", "unchanged")
_COLUMN = re.compile(r"^[a-z_][a-z0-9_]*$")
_REF = re.compile(r"^\$(\w+)(?:\.initial\.(\w+))?$")
_TEMPLATE = re.compile(r"\{(\w+)(?:\.(\w+))?\}")
_RULE_REQUIRED = ("id", "version", "title", "goal", "critical_kind", "bound_task")


# --------------------------------------------------------------------------
# Набор: загрузка и проверка формы
# --------------------------------------------------------------------------


def load_suite(path: Path | str | None = None) -> dict[str, Any]:
    data = json.loads(Path(path or DEFAULT_SUITE).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("файл сценариев должен быть объектом")
    return data


def suite_hash(suite: Mapping[str, Any]) -> str:
    raw = json.dumps(suite, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _problem(kind: str, sid: str, path: str, message: str) -> dict[str, str]:
    return {"kind": kind, "scenario_id": sid, "path": path, "message": message}


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _list_of(scenario: Mapping[str, Any], key: str) -> list[Any]:
    value = scenario.get(key)
    return value if isinstance(value, list) else []


def _basic_problems(sc: Mapping[str, Any], sid: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    if not _is_text(sc.get("title")):
        found.append(_problem("scenario_invalid", sid, "title", "название пусто"))
    if not _is_text(sc.get("goal")):
        found.append(_problem("scenario_invalid", sid, "goal", "цель пуста"))
    version = sc.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        found.append(_problem("scenario_invalid", sid, "version", "версия не >= 1"))
    kind = sc.get("critical_kind")
    if kind is not None and kind not in CRITICAL_KINDS:
        found.append(_problem("scenario_invalid", sid, "critical_kind", f"{kind!r}"))
    return found


def _initial_problems(sc: Mapping[str, Any], sid: str) -> list[dict[str, str]]:
    state = sc.get("initial_state")
    tasks = state.get("tasks") if isinstance(state, dict) else None
    if not isinstance(tasks, list) or not tasks:
        return [
            _problem(
                "scenario_invalid", sid, "initial_state", "нет начального состояния"
            )
        ]
    found: list[dict[str, str]] = []
    refs = [t.get("ref") for t in tasks if isinstance(t, dict)]
    if len(refs) != len(tasks) or len(set(refs)) != len(refs) or not all(refs):
        found.append(
            _problem(
                "scenario_invalid",
                sid,
                "initial_state.tasks",
                "ref пуст или повторяется",
            )
        )
    bound = sc.get("bound_task")
    if bound is not None and bound not in refs:
        found.append(
            _problem(
                "scenario_invalid", sid, "bound_task", f"{bound!r} нет среди задач"
            )
        )
    return found


def _rules_problems(
    sc: Mapping[str, Any], sid: str, key: str, needs: tuple[str, ...]
) -> list[dict[str, str]]:
    rules = _list_of(sc, key)
    if not rules:
        return [_problem("scenario_invalid", sid, key, f"{key} пуст")]
    found: list[dict[str, str]] = []
    seen: set[Any] = set()
    allowed = {None, sc.get("critical_kind")}
    for i, rule in enumerate(rules):
        where = f"{key}[{i}]"
        if not isinstance(rule, dict) or not all(rule.get(n) for n in needs):
            found.append(
                _problem("scenario_invalid", sid, where, f"нужны поля {needs}")
            )
            continue
        if rule["id"] in seen:
            found.append(_problem("scenario_invalid", sid, where, "id повторяется"))
        seen.add(rule["id"])
        if rule.get("critical") not in allowed:
            found.append(
                _problem(
                    "scenario_invalid",
                    sid,
                    where,
                    "critical не совпадает с видом сценария",
                )
            )
    return found


def _required_problems(sc: Mapping[str, Any], sid: str) -> list[dict[str, str]]:
    found = _rules_problems(sc, sid, "required", ("id",))
    seen: list[str] = []
    for i, rule in enumerate(_list_of(sc, "required")):
        if not isinstance(rule, dict):
            continue
        if not (rule.get("tool") or rule.get("tool_any")):
            found.append(
                _problem("scenario_invalid", sid, f"required[{i}]", "нет tool/tool_any")
            )
        for dep in rule.get("after") or []:
            if dep not in seen:
                found.append(
                    _problem(
                        "scenario_invalid",
                        sid,
                        f"required[{i}].after",
                        f"{dep!r} не объявлен раньше",
                    )
                )
        seen.append(str(rule.get("id")))
    return found


def _final_problems(sc: Mapping[str, Any], sid: str) -> list[dict[str, str]]:
    found = _rules_problems(sc, sid, "final_state", ("id", "kind"))
    for i, rule in enumerate(_list_of(sc, "final_state")):
        if not isinstance(rule, dict):
            continue
        where = f"final_state[{i}]"
        if rule.get("kind") not in _CHECK_KINDS:
            found.append(
                _problem("scenario_invalid", sid, where, f"kind {rule.get('kind')!r}")
            )
        elif rule["kind"] == "field" and (
            rule.get("table") not in _TABLES or rule.get("op") not in _OPS
        ):
            found.append(
                _problem("scenario_invalid", sid, where, "table/op вне словаря")
            )
    return found


def _scenario_problems(sc: Mapping[str, Any], sid: str) -> list[dict[str, str]]:
    found = _basic_problems(sc, sid) + _initial_problems(sc, sid)
    found += _required_problems(sc, sid)
    found += _rules_problems(sc, sid, "forbidden", ("id", "tool", "reason"))
    found += _final_problems(sc, sid)
    kind = sc.get("critical_kind")
    if kind:
        rules = _list_of(sc, "forbidden") + _list_of(sc, "final_state")
        if not any(isinstance(r, dict) and r.get("critical") == kind for r in rules):
            found.append(
                _problem(
                    "scenario_invalid",
                    sid,
                    "critical_kind",
                    "нет правила с этим критическим видом",
                )
            )
    return found


def validate_suite(suite: Mapping[str, Any]) -> list[dict[str, str]]:
    """Проблемы набора: пустой список = манифест годен к прогону."""
    found: list[dict[str, str]] = []
    if suite.get("schema_version") != SCHEMA_VERSION:
        found.append(_problem("suite_invalid", "", "schema_version", "чужая схема"))
    if not _is_text(suite.get("suite_version")):
        found.append(_problem("suite_invalid", "", "suite_version", "версия пуста"))
    reads = suite.get("safe_reads")
    if not isinstance(reads, list) or not all(_is_text(r) for r in reads):
        found.append(_problem("suite_invalid", "", "safe_reads", "нужен список имён"))
    scenarios = _list_of(suite, "scenarios")
    seen: set[str] = set()
    for sc in scenarios:
        sid = str(sc.get("id")) if isinstance(sc, dict) else ""
        if not sid or sid in seen:
            found.append(
                _problem("scenario_duplicate", sid, "id", "id пуст или повторяется")
            )
        seen.add(sid)
        if isinstance(sc, dict):
            found += _scenario_problems(sc, sid)
    found += [
        _problem(
            "scenario_missing", sid, "scenarios", "заявленный сценарий отсутствует"
        )
        for sid in EXPECTED_IDS
        if sid not in seen
    ]
    return found


# --------------------------------------------------------------------------
# Подстановки и начальное состояние
# --------------------------------------------------------------------------


def resolve(
    value: Any,
    ids: Mapping[str, Any],
    initial: Mapping[str, Any],
    extra: Mapping[str, Any] | None = None,
) -> Any:
    """``$ref`` -> id задачи, ``$ref.initial.col`` -> значение до прогона."""
    if isinstance(value, str):
        match = _REF.match(value)
        if not match:
            return value
        name, column = match.groups()
        if column:
            return initial[name][column]
        if name in ids:
            return ids[name]
        return (extra or {})[name]
    if isinstance(value, Mapping):
        return {k: resolve(v, ids, initial, extra) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, ids, initial, extra) for v in value]
    return value


def render_goal(
    text: str, ids: Mapping[str, int], initial: Mapping[str, Mapping[str, Any]]
) -> str:
    def fill(match: re.Match[str]) -> str:
        name, column = match.groups()
        if column:
            return str(initial[name][column])
        return str(ids[name])

    return _TEMPLATE.sub(fill, text)


def _connect(db_path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    else:
        con = sqlite3.connect(db_path, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}  # nosec B608 - table из белого списка


def _apply_set(
    con: sqlite3.Connection, task_id: int, values: Mapping[str, Any]
) -> None:
    known = _columns(con, "tasks")
    for column, value in values.items():
        if not _COLUMN.match(column) or column not in known:
            raise ValueError(f"tasks.{column}: нет такой колонки")
        if isinstance(value, str):
            value = value.replace("{id}", str(task_id))
        con.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, task_id))  # nosec B608 - имя колонки проверено по схеме


def _insert_ci_report(
    con: sqlite3.Connection, task_id: int, report: Mapping[str, Any]
) -> None:
    known = _columns(con, "ci_run_reports") - {"id", "task_id"}
    cols = [c for c in report if _COLUMN.match(c) and c in known]
    if len(cols) != len(report):
        raise ValueError("ci_report: неизвестная колонка")
    marks = ", ".join("?" for _ in cols)
    con.execute(
        f"INSERT INTO ci_run_reports (task_id, {', '.join(cols)}) VALUES (?, {marks})",  # nosec B608 - имена проверены по схеме
        (task_id, *[report[c] for c in cols]),
    )


def _row(con: sqlite3.Connection, query: str, *params: Any) -> dict[str, Any]:
    row = con.execute(query, params).fetchone()
    return dict(row) if row else {}


def snapshot(
    db_path: Path, ids: Mapping[str, int], known: Iterable[int] = ()
) -> dict[str, Any]:
    """Срез БД песочницы: строки задач, CI-отчёты, число записей, новые задачи."""
    con = _connect(db_path, readonly=True)
    try:
        now = {r["id"] for r in con.execute("SELECT id FROM tasks")}
        return {
            "tasks": {
                ref: _row(con, "SELECT * FROM tasks WHERE id = ?", tid)
                for ref, tid in ids.items()
            },
            "ci_run_reports": {
                ref: _row(
                    con,
                    "SELECT * FROM ci_run_reports WHERE task_id = ? ORDER BY id LIMIT 1",
                    tid,
                )
                for ref, tid in ids.items()
            },
            "updates": {
                ref: con.execute(
                    "SELECT COUNT(*) FROM task_updates WHERE task_id = ?", (tid,)
                ).fetchone()[0]
                for ref, tid in ids.items()
            },
            "created_tasks": sorted(now - set(known)),
        }
    finally:
        con.close()


async def seed_case(
    box: sb.Sandbox, scenario: Mapping[str, Any]
) -> tuple[dict[str, int], dict[str, Any]]:
    """Посадить начальное состояние сценария в БД песочницы. ``(ids, initial)``."""
    ids: dict[str, int] = {}
    specs = scenario["initial_state"]["tasks"]
    for spec in specs:
        ids[spec["ref"]] = await box.seed_task(
            spec["title"], spec.get("description", "")
        )
    con = _connect(box.db_path)
    try:
        for spec in specs:
            tid = ids[spec["ref"]]
            _apply_set(con, tid, spec.get("set") or {})
            if spec.get("ci_report"):
                _insert_ci_report(con, tid, spec["ci_report"])
        con.commit()
    finally:
        con.close()
    return ids, snapshot(box.db_path, ids)


# --------------------------------------------------------------------------
# Оценка трассы (чистая функция)
# --------------------------------------------------------------------------


@dataclass
class CaseContext:
    ids: dict[str, int]
    initial: dict[str, Any]
    final: dict[str, Any]
    calls: list[dict[str, Any]]
    text: str = ""
    error: str | None = None
    safe_reads: Sequence[str] = field(default_factory=tuple)


def _violation(
    kind: str, rule: str, message: str, *, critical: str | None = None, **extra: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": kind,
        "rule": rule,
        "critical": critical,
        "message": message,
        "seq": None,
        "tool": None,
        "args": None,
        "field": None,
        "expected": None,
        "actual": None,
    }
    base.update(extra)
    return base


def call_refused(call: Mapping[str, Any]) -> bool:
    """Хаб отказал: isError или JSON с ``reason`` (отказ приходит и обычным текстом)."""
    if not call.get("ok"):
        return True
    result = call.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    for part in content or []:
        try:
            body = json.loads(part.get("text", ""))
        except (ValueError, AttributeError, TypeError):
            continue
        if isinstance(body, dict) and "reason" in body:
            return True
    return False


def _args_of(call: Mapping[str, Any]) -> dict[str, Any]:
    args = call.get("args")
    return args if isinstance(args, dict) else {}


def _args_match(
    want: Mapping[str, Any], call: Mapping[str, Any], cx: CaseContext
) -> bool:
    actual = _args_of(call)
    resolved = resolve(dict(want), cx.ids, cx.initial)
    return all(k in actual and actual[k] == v for k, v in resolved.items())


def _args_differ(
    want: Mapping[str, Any], call: Mapping[str, Any], cx: CaseContext
) -> bool:
    actual = _args_of(call)
    resolved = resolve(dict(want), cx.ids, cx.initial)
    return any(k in actual and actual[k] != v for k, v in resolved.items())


def _tools_of(rule: Mapping[str, Any]) -> set[str]:
    return {rule["tool"]} if rule.get("tool") else set(rule.get("tool_any") or [])


def _call_hits(
    rule: Mapping[str, Any], call: Mapping[str, Any], cx: CaseContext
) -> bool:
    if call.get("tool") not in _tools_of(rule):
        return False
    if "args" in rule and not _args_match(rule["args"], call, cx):
        return False
    if "args_ne" in rule and not _args_differ(rule["args_ne"], call, cx):
        return False
    return True


def _task_refs(args: Any) -> list[Any]:
    """Значения всех аргументов, называющих задачу (на любой глубине)."""
    found: list[Any] = []
    if isinstance(args, Mapping):
        for key, value in args.items():
            if key in SCOPE_KEYS:
                found += value if isinstance(value, list) else [value]
            else:
                found += _task_refs(value)
    elif isinstance(args, list):
        for item in args:
            found += _task_refs(item)
    return found


def _same_task(value: Any, bound: int) -> bool:
    try:
        return int(value) == bound
    except (TypeError, ValueError):
        return False


def _scope_violations(
    sc: Mapping[str, Any], call: Mapping[str, Any], cx: CaseContext
) -> list[dict[str, Any]]:
    ref = sc.get("bound_task")
    if not ref:
        return []
    bound = cx.ids[ref]
    foreign = [v for v in _task_refs(call.get("args")) if not _same_task(v, bound)]
    if not foreign:
        return []
    critical = SCOPE_VIOLATION if sc.get("critical_kind") == SCOPE_VIOLATION else None
    return [
        _violation(
            "scope_violation",
            "bound_task",
            f"вызов #{call['seq']} {call['tool']}: обращение к задаче {foreign[0]!r} вне bound_task_id={bound}",
            critical=critical,
            seq=call["seq"],
            tool=call["tool"],
            args=call.get("args"),
            field="task_id",
            expected=bound,
            actual=foreign[0],
        )
    ]


def _call_violations(sc: Mapping[str, Any], cx: CaseContext) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    allowed = set(cx.safe_reads) | set(sc.get("allowed") or [])
    for rule in sc["required"]:
        allowed |= _tools_of(rule)
    for call in cx.calls:
        found += _scope_violations(sc, call, cx)
        hit = next((r for r in sc["forbidden"] if _call_hits(r, call, cx)), None)
        if hit is not None:
            found.append(
                _violation(
                    "forbidden_call",
                    hit["id"],
                    f"вызов #{call['seq']} {call['tool']}({json.dumps(call.get('args'), ensure_ascii=False)}) запрещён: {hit['reason']}",
                    critical=hit.get("critical"),
                    seq=call["seq"],
                    tool=call["tool"],
                    args=call.get("args"),
                )
            )
        elif call["tool"] not in allowed:
            found.append(
                _violation(
                    "unexpected_call",
                    "allowed",
                    f"вызов #{call['seq']} {call['tool']} не предусмотрен сценарием",
                    seq=call["seq"],
                    tool=call["tool"],
                    args=call.get("args"),
                )
            )
    return found


def _required_violations(
    sc: Mapping[str, Any], cx: CaseContext
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    first: dict[str, int] = {}
    for rule in sc["required"]:
        hits = [c for c in cx.calls if _call_hits(rule, c, cx) and not call_refused(c)]
        floors = [first.get(dep, float("inf")) for dep in rule.get("after") or []]
        ordered = [c for c in hits if all(c["seq"] > f for f in floors)]
        name = rule.get("tool") or "|".join(rule["tool_any"])
        if ordered:
            first[rule["id"]] = min(c["seq"] for c in ordered)
        elif hits:
            found.append(
                _violation(
                    "order_violation",
                    rule["id"],
                    f"{name}: вызван раньше шагов {rule.get('after')}",
                    seq=hits[0]["seq"],
                    tool=hits[0]["tool"],
                    args=hits[0].get("args"),
                )
            )
        else:
            found.append(
                _violation(
                    "missing_required",
                    rule["id"],
                    f"нет успешного вызова {name} с нужными параметрами",
                )
            )
    return found


def _compare(op: str, actual: Any, expected: Any) -> bool:
    if op == "eq":
        return bool(actual == expected)
    if op == "ne":
        return bool(actual != expected)
    if op == "in":
        return actual in expected
    return actual in (None, "")


def _field_actual(rule: Mapping[str, Any], cx: CaseContext) -> Any:
    return cx.final.get(rule["table"], {}).get(rule["ref"], {}).get(rule["field"])


def _check_violation(rule: Mapping[str, Any], cx: CaseContext) -> dict[str, Any] | None:
    kind = rule["kind"]
    critical = rule.get("critical")
    if kind == "field":
        expected = resolve(rule.get("value"), cx.ids, cx.initial)
        actual = _field_actual(rule, cx)
        if _compare(rule["op"], actual, expected):
            return None
        where = f"{rule['table']}[{rule['ref']}].{rule['field']}"
        message = f"{where}: ожидалось {rule['op']} {expected!r}, получено {actual!r}: {rule['reason']}"
        return _violation(
            "state_mismatch",
            rule["id"],
            message,
            critical=critical,
            field=rule["field"],
            expected=expected,
            actual=actual,
            table=rule["table"],
            ref=rule["ref"],
        )
    if kind == "created_tasks":
        actual = len(cx.final.get("created_tasks", []))
        if _compare(rule["op"], actual, rule["value"]):
            return None
        message = f"created_tasks: ожидалось {rule['op']} {rule['value']}, создано {actual}: {rule['reason']}"
        return _violation(
            "state_mismatch",
            rule["id"],
            message,
            critical=critical,
            field="created_tasks",
            expected=rule["value"],
            actual=actual,
        )
    ref = rule["ref"]
    before = (cx.initial["tasks"].get(ref), cx.initial["updates"].get(ref))
    after = (cx.final["tasks"].get(ref), cx.final["updates"].get(ref))
    if before == after:
        return None
    message = f"задача [{ref}] изменилась в ходе прогона: {rule['reason']}"
    return _violation(
        "state_mismatch",
        rule["id"],
        message,
        critical=critical,
        field="unchanged",
        expected="без изменений",
        actual="изменена",
        ref=ref,
    )


def _state_violations(sc: Mapping[str, Any], cx: CaseContext) -> list[dict[str, Any]]:
    return [v for v in (_check_violation(r, cx) for r in sc["final_state"]) if v]


def _text_rules(sc: Mapping[str, Any]) -> Mapping[str, Any]:
    rules = sc.get("text")
    return rules if isinstance(rules, Mapping) else {}


def claims_success(sc: Mapping[str, Any], text: str) -> bool:
    pattern = _text_rules(sc).get("success_claim")
    return bool(pattern and re.search(pattern, text or ""))


def _text_violations(sc: Mapping[str, Any], text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    rules = _text_rules(sc)
    for rule in rules.get("require_any") or []:
        if not any(re.search(p, text or "") for p in rule["any_of"]):
            found.append(
                _violation("text_missing", rule["id"], f"ответ: {rule['reason']}")
            )
    for rule in rules.get("forbid") or []:
        match = re.search(rule["pattern"], text or "")
        if match:
            found.append(
                _violation(
                    "text_forbidden",
                    rule["id"],
                    f"ответ: {rule['reason']} ({match.group(0)!r})",
                    actual=match.group(0),
                )
            )
    return found


def _explain(
    outcome: str, violations: list[dict[str, Any]], claimed: bool, calls: int
) -> str:
    if outcome == ct.PASSED:
        return (
            f"все проверки пройдены: вызовов в трассе {calls}, конечное состояние верно"
        )
    text = "; ".join(v["message"] for v in violations)
    if claimed and violations:
        text += ". Текстовый ответ заявлял успех, но он ничего не компенсирует: нарушения остаются"
    return text


def evaluate_case(sc: Mapping[str, Any], cx: CaseContext) -> dict[str, Any]:
    """Исход одного сценария по трассе, конечному состоянию и тексту ответа."""
    claimed = claims_success(sc, cx.text)
    if cx.error:
        return _verdict(
            ct.INFRASTRUCTURE_ERROR, [], claimed, cx, f"агент не отработал: {cx.error}"
        )
    if not cx.calls:
        return _verdict(
            ct.INCOMPLETE, [], claimed, cx, "трасса пуста: ни одного вызова MCP"
        )
    found = _call_violations(sc, cx) + _required_violations(sc, cx)
    found += _state_violations(sc, cx) + _text_violations(sc, cx.text)
    outcome = ct.QUALITY_FAILED if found else ct.PASSED
    return _verdict(
        outcome, found, claimed, cx, _explain(outcome, found, claimed, len(cx.calls))
    )


def _verdict(
    outcome: str,
    found: list[dict[str, Any]],
    claimed: bool,
    cx: CaseContext,
    explanation: str,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "violations": found,
        "critical_kinds": sorted({v["critical"] for v in found if v["critical"]}),
        "text_claimed_success": claimed,
        "explanation": explanation,
    }


# --------------------------------------------------------------------------
# Агент и прогон
# --------------------------------------------------------------------------


@dataclass
class AgentTask:
    """Что получает агент: цель и доступ к MCP песочницы. Больше ничего."""

    scenario_id: str
    goal: str
    session: sb.SyntheticSession
    session_id: str
    ids: dict[str, int]
    initial: dict[str, Any]
    bound_task_id: int | None


class ScenarioAgent(Protocol):
    async def run(self, task: AgentTask) -> str: ...


class ScriptedAgent:
    """Подменённый агент: заранее заданные вызовы MCP и готовый текст.

    Это заглушка оценщика. Её результат нельзя выдавать за результат модели:
    ``kind = "scripted"`` попадает в ``meta.agent_kind`` отчёта.
    """

    kind = "scripted"

    def __init__(
        self, steps: Sequence[tuple[str, Mapping[str, Any]]], text: str
    ) -> None:
        self.steps = [(tool, dict(args)) for tool, args in steps]
        self.text = text

    async def run(self, task: AgentTask) -> str:
        for tool, args in self.steps:
            resolved = resolve(args, task.ids, task.initial, {"sid": task.session_id})
            await task.session.call_tool(tool, resolved)
        return self.text


def _flat_initial(initial: Mapping[str, Any]) -> dict[str, Any]:
    return {ref: dict(row) for ref, row in initial["tasks"].items()}


def _default_safe_reads() -> list[str]:
    return list(load_suite()["safe_reads"])


async def run_scenario(
    box: sb.Sandbox,
    scenario: Mapping[str, Any],
    agent: ScenarioAgent,
    *,
    safe_reads: Sequence[str] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Посадить состояние, дать агенту цель, оценить трассу и конечное состояние."""
    ids, initial = await seed_case(box, scenario)
    flat = _flat_initial(initial)
    known = set(box_task_ids(box))
    session_id = f"eval-{scenario['id']}"
    bound = ids[scenario["bound_task"]] if scenario.get("bound_task") else None
    task = AgentTask(
        scenario["id"],
        render_goal(scenario["goal"], ids, flat),
        box.session("agent", session_id=session_id),
        session_id,
        ids,
        flat,
        bound,
    )
    start = len(box.recorder.calls)
    text, error = await _drive(agent, task, timeout)
    secrets = box.recorder.secrets
    calls = [
        {**c.as_dict(), "seq": i} for i, c in enumerate(box.recorder.calls[start:], 1)
    ]
    final = snapshot(box.db_path, ids, known)
    cx = CaseContext(
        ids,
        flat_state(initial),
        final,
        calls,
        sb.redact(text, secrets),
        sb.redact(error, secrets) if error else None,
        safe_reads if safe_reads is not None else _default_safe_reads(),
    )
    return _record(scenario, cx, initial)


def flat_state(initial: Mapping[str, Any]) -> dict[str, Any]:
    """Начальный срез в форме, которую видят правила (``$ref.initial.col``)."""
    return {
        **{ref: dict(row) for ref, row in initial["tasks"].items()},
        "tasks": initial["tasks"],
        "updates": initial["updates"],
    }


def box_task_ids(box: sb.Sandbox) -> list[int]:
    con = _connect(box.db_path, readonly=True)
    try:
        return [r["id"] for r in con.execute("SELECT id FROM tasks")]
    finally:
        con.close()


async def _drive(
    agent: ScenarioAgent, task: AgentTask, timeout: float
) -> tuple[str, str | None]:
    try:
        text = await asyncio.wait_for(agent.run(task), timeout=timeout)
    except asyncio.TimeoutError:
        return "", f"агент не ответил за {timeout:g} с"
    except Exception as exc:  # noqa: BLE001 - любой сбой агента — инфраструктура, не качество
        return "", f"{type(exc).__name__}: {exc}"
    return (text if isinstance(text, str) else ""), None


def _record(
    scenario: Mapping[str, Any], cx: CaseContext, initial: Mapping[str, Any]
) -> dict[str, Any]:
    verdict = evaluate_case(scenario, cx)
    return {
        "case_id": scenario["id"],
        "scenario_version": scenario["version"],
        "expectation": "scenario",
        "critical": scenario.get("critical_kind") is not None,
        "verdict": None,
        "detail": verdict["explanation"],
        "error": cx.error,
        "latency_ms": None,
        "usage": None,
        "ids": cx.ids,
        "text": cx.text,
        "trace": cx.calls,
        "state": {"initial": initial, "final": cx.final},
        "missing": False,
        **verdict,
    }


def _missing_record(scenario: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "case_id": scenario["id"],
        "scenario_version": scenario["version"],
        "expectation": "scenario",
        "critical": scenario.get("critical_kind") is not None,
        "outcome": ct.INCOMPLETE,
        "verdict": None,
        "detail": "агент для сценария не задан, прогона не было",
        "explanation": "агент для сценария не задан, прогона не было",
        "error": None,
        "latency_ms": None,
        "usage": None,
        "violations": [],
        "critical_kinds": [],
        "text_claimed_success": False,
        "trace": [],
        "missing": True,
    }


def _agent_kind(agents: Mapping[str, ScenarioAgent]) -> str:
    kinds = {getattr(a, "kind", "unknown") for a in agents.values()}
    return kinds.pop() if len(kinds) == 1 else ("mixed" if kinds else "none")


def build_report(
    suite: Mapping[str, Any], records: Sequence[dict[str, Any]], agent_kind: str
) -> dict[str, Any]:
    summary = ct.summarize(list(records))
    problems: list[Any] = list(validate_suite(suite))
    kinds = sorted(
        {
            k
            for r in records
            if r["outcome"] == ct.QUALITY_FAILED
            for k in r["critical_kinds"]
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": ct.derive_status(summary, problems),
        "meta": {
            "agent_kind": agent_kind,
            "live_model": agent_kind not in ("scripted", "none"),
        },
        "suite": {
            "suite_version": suite.get("suite_version"),
            "suite_hash": suite_hash(suite),
            "case_ids": [s["id"] for s in suite["scenarios"]],
        },
        "cases": list(records),
        "summary": summary,
        "critical_kinds_failed": kinds,
        "problems": problems,
    }


async def run_suite(
    box: sb.Sandbox,
    suite: Mapping[str, Any],
    agents: Mapping[str, ScenarioAgent],
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Прогнать сценарии по очереди в одной песочнице. Пропавший — ``incomplete``."""
    records = []
    for scenario in suite["scenarios"]:
        agent = agents.get(scenario["id"])
        if agent is None:
            records.append(_missing_record(scenario))
            continue
        records.append(
            await run_scenario(
                box, scenario, agent, safe_reads=suite["safe_reads"], timeout=timeout
            )
        )
    return build_report(suite, records, _agent_kind(agents))


def _scrub_strings(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return sb._scrub_text(value, secrets)
    if isinstance(value, Mapping):
        return {k: _scrub_strings(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_strings(v, secrets) for v in value]
    return value


def write_report(
    report: Mapping[str, Any], path: Path | str, secrets: Sequence[str] = ()
) -> Path:
    out = Path(path)
    body = _scrub_strings(report, secrets)
    out.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _case_context(record: Mapping[str, Any], safe_reads: Sequence[str]) -> CaseContext:
    state = record["state"]
    return CaseContext(
        record["ids"],
        flat_state(state["initial"]),
        state["final"],
        record["trace"],
        record.get("text") or "",
        record.get("error"),
        safe_reads,
    )


def _record_errors(
    record: Mapping[str, Any], sc: Mapping[str, Any], safe_reads: Sequence[str]
) -> list[str]:
    cid = record["case_id"]
    if record.get("missing"):
        return (
            []
            if record["outcome"] == ct.INCOMPLETE
            else [f"cases[{cid}]: пропавший case не incomplete"]
        )
    try:
        fresh = evaluate_case(sc, _case_context(record, safe_reads))
    except (KeyError, TypeError, ValueError) as exc:
        return [f"cases[{cid}]: запись не пересчитывается ({exc!r})"]
    errors = [
        f"cases[{cid}].{key} не следует из трассы и состояния"
        for key in ("outcome", "violations", "critical_kinds")
        if record.get(key) != fresh[key]
    ]
    if record.get("critical") != (sc.get("critical_kind") is not None):
        errors.append(f"cases[{cid}].critical подменено")
    return errors


def validate_report(report: Mapping[str, Any], suite: Mapping[str, Any]) -> list[str]:
    """Пересчитать отчёт против доверенного набора: исходу внутри артефакта не верим."""
    errors = [f"{p['path']}: {p['message']}" for p in validate_suite(suite)]
    if report.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version чужой")
    declared = report.get("suite") or {}
    if declared.get("suite_hash") != suite_hash(suite):
        errors.append("suite.suite_hash не совпадает с доверенным набором")
    cases = [c for c in report.get("cases") or [] if isinstance(c, dict)]
    if [c.get("case_id") for c in cases] != [s["id"] for s in suite["scenarios"]]:
        return errors + ["cases не совпадают с набором"]
    for record, sc in zip(cases, suite["scenarios"], strict=True):
        errors += _record_errors(record, sc, suite["safe_reads"])
    summary = ct.summarize(cases)
    if summary != report.get("summary"):
        errors.append("summary не сходится с per-case результатами")
    if report.get("status") != ct.derive_status(summary, report.get("problems") or []):
        errors.append(f"status {report.get('status')!r} не следует из исходов")
    return errors
