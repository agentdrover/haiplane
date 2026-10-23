"""Действия карточки решения: нижняя панель и листы (#1813).

Здесь нет ни одного правила решения и ни одного нового маршрута. Сборка
переводит состояние задачи в список кнопок; каждая кнопка открывает лист, а
форма листа ведёт на уже существующий человеческий маршрут (approve, reject,
review-verdict, decide, return-to-work). Чистая функция: ничего не читает и не
пишет, поэтому матрицу «состояние -> кнопки» можно проверить без сервера.

Что лист обещает, он обещает только текстом о самом действии. Результат хаб
называет сам, после ответа (``hub/services/card_sheet.py``).

Защита от устаревшего экрана — по своему поколению у каждого действия:
approve и reject - ``expected_statement_generation``; вердикт -
``expected_generation`` сдачи (её проверяет сервис вердикта, замена несёт
``supersedes_revision``); decide и return-to-work - ``expected_status`` и
``expected_submission_generation``.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from hub.services.decision_brief import ACCEPT, APPROVE_DRAFT, REWORK

#: Маршруты листов. Список замкнут: тест сверяет формы с ним.
ROUTES = (
    "web-approve",
    "web-reject",
    "web-review-verdict",
    "web-decide",
    "web-return-to-work",
)
_SEVERITIES = ("high", "medium", "low")
_RETURNABLE = ("review", "fix_requested")


def _route(task_id: int, name: str) -> str:
    return f"/tasks/{task_id}/{name}"


def finding_line(finding: Mapping[str, Any]) -> str:
    """Находка машинного ревью строкой формы «high: текст @ файл:строка»."""
    severity = str(finding.get("severity") or "")
    if severity not in _SEVERITIES:
        severity = "medium"
    line = f"{severity}: {str(finding.get('title') or '').strip()}"
    path = str(finding.get("file") or "").strip()
    if path:
        line += f" @ {path}"
        if finding.get("line"):
            line += f":{finding['line']}"
    return line


def _text(name: str, label: str, *, required: bool = False, help: str = "") -> dict:
    return {
        "type": "textarea",
        "name": name,
        "label": label,
        "required": required,
        "help": help,
    }


def _check(name: str, label: str, *, required: bool = False) -> dict:
    return {"type": "checkbox", "name": name, "label": label, "required": required}


def _sheet(
    sid: str,
    kind: str,
    title: str,
    intro: str,
    action: str,
    submit: str,
    *,
    hidden: dict[str, str],
    fields: list[dict],
    confirm: bool = False,
    tone: str = "primary",
) -> dict[str, Any]:
    return {
        "id": sid,
        "kind": kind,
        "title": title,
        "intro": intro,
        "action": action,
        "submit_label": submit,
        "submit_tone": tone,
        # c2_sheet: запрос пришёл с листа (даже без скрипта) - только человеку.
        "hidden": {**hidden, "c2_sheet": "1"},
        "fields": fields,
        "confirm": confirm,
    }


def _action(sheet: str, label: str, tone: str, recommended: bool) -> dict[str, Any]:
    return {"sheet": sheet, "label": label, "tone": tone, "recommended": recommended}


def _statement_sheets(t: dict[str, Any], recommend: str) -> tuple[list, list]:
    guard = {"expected_statement_generation": str(t.get("statement_generation") or 0)}
    tid = int(t["id"])
    approve = _sheet(
        "sh-approve",
        "approve",
        "Одобрить постановку",
        "Хаб проверит готовность постановки (DoR). Исполнитель этим действием "
        "не запускается: после ответа лист назовёт статус, который вернул хаб.",
        _route(tid, "web-approve"),
        "Одобрить",
        hidden={**guard, "runtime": "auto"},
        fields=[_text("comment", "Комментарий (необязательно)")],
    )
    reject = _sheet(
        "sh-reject",
        "reject",
        "Отклонить постановку",
        "Задача будет отклонена. Причина попадёт в ленту.",
        _route(tid, "web-reject"),
        "Отклонить",
        hidden=dict(guard),
        fields=[_text("comment", "Причина (необязательно)")],
        tone="danger",
    )
    actions = [
        _action("sh-approve", "Одобрить", "primary", recommend == APPROVE_DRAFT),
        _action("sh-reject", "Отклонить", "alt", False),
    ]
    return actions, [approve, reject]


def _verdict_sheets(
    t: dict[str, Any],
    recommend: str,
    risks: Sequence[Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]],
    stands: bool,
) -> tuple[list, list]:
    tid = int(t["id"])
    hidden = {"expected_generation": str(t.get("submission_generation") or 0)}
    if stands:
        hidden["supersedes_revision"] = str(t.get("review_verdict_revision") or 0)
    route = _route(tid, "web-review-verdict")
    replace = (
        " Это замена стоящего вердикта: нужна ревизия, которую вы видите."
        if stands
        else ""
    )
    again = [_check("acknowledge_repeat", "Повторяю прошлый вердикт намеренно")]
    options = [{"line": finding_line(f)} for f in findings]
    cr_fields: list[dict] = []
    if options:
        cr_fields.append(
            {"type": "findings", "label": "Находки ревью", "options": options}
        )
    cr_fields += [
        _text(
            "findings_text",
            "Находки (по строке: high: … / medium: … / low: …)",
        ),
        _text("comments", "Причина возврата", required=True),
        *(again if stands else []),
    ]
    cr = _sheet(
        "sh-cr",
        "changes_requested",
        "На доработку",
        "Вердикт «changes_requested» записывается на сдачу, которую вы видите."
        + replace,
        route,
        "Вернуть на доработку",
        hidden={**hidden, "verdict": "changes_requested"},
        fields=cr_fields,
        tone="warning",
    )
    high = [r for r in risks if str(r.get("severity")) == "high"]
    if high:
        ok = _sheet(
            "sh-ok-risk",
            "approved_high_risk",
            "Принять при высоком риске",
            "Справка называет высокий риск. Вердикт «approved» записывается "
            "на сдачу, которую вы видите; доставку проверяет и выполняет гейт."
            + replace,
            route,
            "Принять при высоком риске",
            hidden={**hidden, "verdict": "approved", "risk_ack": "required"},
            fields=[
                {
                    "type": "list",
                    "label": "Высокие риски",
                    # Тексты рисков уже напечатаны выше (факт - один раз): лист называет
                    # их число и отсылает к блокам.
                    "entries": [
                        f"Высоких рисков: {len(high)}. Их тексты - в блоках "
                        "«Рекомендую» и «Риски» карточки."
                    ],
                },
                _check(
                    "confirm_risk", "Я прочитал(а) риски и принимаю их", required=True
                ),
                _text("comments", "Почему риск приемлем", required=True),
                *(again if stands else []),
            ],
            confirm=True,
        )
        ok_id, ok_label = "sh-ok-risk", "Принять при высоком риске"
    else:
        ok = _sheet(
            "sh-ok",
            "approved",
            "Принять",
            "Вердикт «approved» записывается на сдачу, которую вы видите; "
            "доставку проверяет и выполняет гейт." + replace,
            route,
            "Принять",
            hidden={**hidden, "verdict": "approved"},
            fields=[
                _text("comments", "Комментарий (необязательно)"),
                *(again if stands else []),
            ],
            tone="success",
        )
        ok_id, ok_label = "sh-ok", "Принять"
    accept = _action(ok_id, ok_label, "alt", recommend == ACCEPT)
    rework = _action("sh-cr", "На доработку", "alt", recommend == REWORK)
    ordered = [accept, rework] if recommend == ACCEPT else [rework, accept]
    ordered[0]["tone"] = "primary"
    return ordered, [cr, ok]


def _decide_sheets(t: dict[str, Any], recommend: str) -> tuple[list, list]:
    tid = int(t["id"])
    guard = {
        "expected_status": "needs_decision",
        "expected_submission_generation": str(t.get("submission_generation") or 0),
    }
    route = _route(tid, "web-decide")
    accept = _sheet(
        "sh-accept",
        "decide_accept",
        "Принять решением",
        "Задача принимается как выполненная. Судьбу PR выбираете вы; что с ним "
        "стало, лист скажет после ответа хаба.",
        route,
        "Принять",
        hidden={**guard, "action": "accept"},
        fields=[
            _text("decision_summary", "Почему принимаете (необязательно)"),
            {"type": "pr_disposition", "label": "Судьба PR при принятии"},
        ],
        tone="success",
    )
    rework = _sheet(
        "sh-rework",
        "decide_rework",
        "На доработку",
        "Задача возвращается на доработку с вашими указаниями.",
        route,
        "Вернуть на доработку",
        hidden={**guard, "action": "rework"},
        fields=[
            _text("instructions", "Что исправить", required=True),
            _check(
                "confirm_no_job",
                "Прошлый заказ оборвался, задания в очереди диспетчера нет",
            ),
        ],
        tone="warning",
    )
    a_accept = _action("sh-accept", "Принять", "alt", recommend == ACCEPT)
    a_rework = _action("sh-rework", "На доработку", "alt", recommend == REWORK)
    ordered = [a_accept, a_rework] if recommend == ACCEPT else [a_rework, a_accept]
    ordered[0]["tone"] = "primary"
    return ordered, [accept, rework]


def _return_offered(t: dict[str, Any]) -> bool:
    """Те же условия, что у формы в ``partials/return_to_work.html``."""
    status = str(t.get("status") or "")
    commit = str(t.get("result_kind") or "") != "state"
    return (
        status in _RETURNABLE
        or (status == "running" and commit)
        or (not commit and status == "completed")
    )


def _return_sheet(t: dict[str, Any]) -> dict[str, Any]:
    status = str(t.get("status") or "")
    fields = [_text("reason", "Почему сдачу забирают у держателя", required=True)]
    has_job = t.get("job_id") or t.get("review_job_id")
    if has_job and status != "running":
        fields.append(_check("abandon_active_job", "Бросить активный job"))
    holder = f" (сейчас держит {t['claimed_by']})" if t.get("claimed_by") else ""
    return _sheet(
        "sh-return",
        "return_to_work",
        "Вернуть в работу",
        f"Задача вернётся в open{holder}, захват снимется, одобрение текущей "
        "сдачи закроется; ветка, PR и находки останутся. Если у задачи есть "
        "признак работы, хаб откажет и назовёт его.",
        _route(int(t["id"]), "web-return-to-work"),
        "Вернуть в работу",
        hidden={
            "expected_status": status,
            "expected_submission_generation": str(t.get("submission_generation") or 0),
        },
        fields=fields,
        tone="warning",
    )


def _verdict_stands(t: dict[str, Any]) -> bool:
    return bool(
        str(t.get("result_kind") or "") != "state"
        and str(t.get("status") or "") in ("review", "running")
        and t.get("review_verdict")
        and t.get("review_verdict_generation") == t.get("submission_generation")
    )


def build(
    task: dict[str, Any],
    *,
    recommend: str = "",
    risks: Sequence[Mapping[str, Any]] = (),
    findings: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Кнопки панели, вторичные кнопки, листы и метка живого состояния."""
    status = str(task.get("status") or "")
    actions: list[dict] = []
    sheets: list[dict] = []
    stands = _verdict_stands(task)
    board = status == "review" and not task.get("review_job_id")
    if status == "draft":
        actions, sheets = _statement_sheets(task, recommend)
    elif stands or board:
        actions, sheets = _verdict_sheets(task, recommend, risks, findings, stands)
    elif status == "needs_decision":
        actions, sheets = _decide_sheets(task, recommend)
    more: list[dict] = []
    if _return_offered(task):
        sheets.append(_return_sheet(task))
        more.append(_action("sh-return", "Вернуть в работу", "alt", False))
    live = {
        "status": status,
        "submission_generation": str(task.get("submission_generation") or 0),
        "statement_generation": str(task.get("statement_generation") or 0),
        "review_verdict_revision": str(task.get("review_verdict_revision") or 0),
    }
    for sheet in sheets:
        sheet["built"] = dict(live)
    return {"actions": actions, "more": more, "sheets": sheets, "live": live}


def _get(obj: Any, key: str) -> Any:
    value = obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)
    return getattr(value, "value", value)


def for_page(
    row: Mapping[str, Any],
    view: Mapping[str, Any],
    machine_review: Any,
    brief: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Панель и листы для страницы.

    Рекомендация - у вида C2. Риски для ВЫБОРА листа - исходные риски справки
    (``brief``): вид C2 сокращает повторы с «почему» и мог бы убрать высокий
    риск целиком, а лист принятия тогда оказался бы обычным. Сокращение - только
    для показа. Справки нет - риски вида (иного источника нет).
    """
    recommend = str((view.get("recommend") or {}).get("action") or "")
    raw = brief.get("risks") if brief is not None else view.get("risks")
    risks = raw or []
    # Находки прошлой сдачи листу не предлагаются: вердикт пишется на новую.
    current = bool(_get(machine_review, "is_current"))
    confirmed = (_get(machine_review, "findings_confirmed") or []) if current else []
    findings = [
        {k: _get(f, k) for k in ("severity", "title", "file", "line")}
        for f in confirmed
    ]
    return build(dict(row), recommend=recommend, risks=risks, findings=findings)
