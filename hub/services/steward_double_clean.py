"""Исключение из потолка класса стюарда при «двойной чистоте» (#1986).

Потолок ``steward_approve_max_class`` остаётся правилом. Исключение узкое:
approve выше потолка, но не выше ``steward_approve_max_class_double_clean``,
применяется, только если СРАЗУ выполнено всё:

* пара отчётов ``machine_reviews`` с пином, проверенным хабом (#1985), на это
  поколение и этот sha; оба чисты (принципал известен, не саморевью, прогон
  полон, ни одной подтверждённой и неразрешённой находки, ни одного потерянного
  измерения); принципалы различны и ни один не исполнитель; дополнительный
  отчёт - от принципала из ``double_clean_reviewers``;
* все ПРОЧИЕ отчёты этого поколения тоже чисты: новый отрицательный отчёт
  отменяет исключение, новый чистый пару не меняет;
* CI на закреплённом sha подтверждён GitHub и зелёный, попытка не сменилась;
* советник согласен на том же пакете (правило не ослаблено).

ОДНО решение на два этапа. Ранний этап (``steward_apply.ceiling_refusals``)
решает по прочитанному до записи; транзакционный (``ceiling_gate`` внутри
записи вердикта) вызывает эту же функцию тем же соединением и сверяет итог с
ранним: исключение выдаёт только совпавшее решение. Транзакционный этап без
раннего решения не выдаёт ничего.

Пара фиксируется при ЗАКАЗЕ суждения (``freeze_pair`` пишет колонку
``steward_runs.double_clean_pair``): отчёт, пришедший после заказа, режим не
включает, а заказ без пары не получит её никогда в этом поколении.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.models import RiskClass
from hub.services import ci_trust, project_policy
from hub.services.orchestration import report_has_evidence

log = logging.getLogger(__name__)

MODE = "double_clean"
_VERDICT_KIND = "verdict"
_ORDER = list(RiskClass)


@dataclass(frozen=True)
class Decision:
    """Итог: включено ли исключение для случая, и что не выполнено.

    ``enabled`` ложно, когда ключи политики исключения не включают (нет ключей,
    нечитаемы, класс не выше обычного потолка): отказ тогда остаётся прежним,
    без единого слова об исключении.
    """

    enabled: bool
    unmet: tuple[str, ...] = ()
    audit: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.enabled and not self.unmet and self.audit is not None


OFF = Decision(enabled=False)


@dataclass(frozen=True)
class _Settings:
    ordinary: str
    clean: str
    reviewers: list[int]

    def audit(self) -> dict[str, Any]:
        return {
            project_policy.STEWARD_APPROVE_MAX_CLASS_KEY: self.ordinary,
            project_policy.STEWARD_CLEAN_CEILING_KEY: self.clean,
            project_policy.DOUBLE_CLEAN_REVIEWERS_KEY: list(self.reviewers),
        }


def settings_of(policy: dict) -> _Settings | None:
    """Ключи, включающие исключение, или None, если оно выключено.

    Выключено: нет обычного потолка (исключать не из чего) или он нечитаем, нет
    класса исключения, пуст список принципалов, класс исключения не выше обычного.
    """
    ordinary = project_policy.steward_approve_max_class_of(policy)
    clean = project_policy.steward_double_clean_ceiling_of(policy)
    reviewers = project_policy.double_clean_reviewers_of(policy)
    classes = project_policy.STEWARD_APPROVE_CLASSES
    if ordinary is None or ordinary not in classes or clean is None or not reviewers:
        return None
    if classes.index(clean) <= classes.index(ordinary):
        return None
    return _Settings(ordinary, clean, reviewers)


async def _settings(db: aiosqlite.Connection, task_id: int) -> _Settings | None:
    project = await repo.resolve_project_for_task(db, task_id)
    return settings_of(project_policy.gate_policy_of(project) if project else {})


def _items(raw: Any) -> list | None:
    """Список из JSON-колонки; None - нечитаемо или пусто в смысле «нет данных».

    ``NULL`` и пустая строка - не ``[]``: «хаб не смог прочитать» и «прочитал,
    там пусто» разные исходы, и только второй чист.
    """
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) else None


def _negatives(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Что делает отчёт нечистым: находки, потеря измерений, прерванный прогон.

    Незаявленная полнота (``incomplete`` пуст) - тоже нечисто: отчёт, не
    сказавший, что прогон полон, чистым не считается ни в паре, ни среди прочих.
    """
    out: list[tuple[str, str]] = []
    if row.get("incomplete") != 0:
        out.append(
            (
                "incomplete",
                "полнота прогона не заявлена"
                if row.get("incomplete") is None
                else "прогон ревью не завершён",
            )
        )
    for column, token in (
        ("findings_confirmed", "confirmed"),
        ("unresolved", "unresolved"),
        ("lost_dimensions", "lost_dimensions"),
    ):
        items = _items(row.get(column))
        if items is None:
            out.append((token, f"{column} не читается"))
        elif items:
            out.append((token, f"{len(items)} шт."))
    return out


def _unclean(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Единый предикат чистоты отчёта: изъяны, годные и для пары, и для прочих.

    Один на ВСЕ отчёты поколения (#1986, круг 2): определение «чисто» для пары
    и для прочих отчётов не разъезжается. Неизвестное не чисто: принципал,
    признак саморевью и полнота прогона обязаны быть заявлены, перенесённый
    отчёт - не прогон, пустой отчёт - не ревью.
    """
    out: list[tuple[str, str]] = []
    if row.get("carried_from_review_id") is not None:
        out.append(("carried", "отчёт перенесён с прежней сдачи, прогона не было"))
    elif not report_has_evidence(row):
        out.append(
            (
                "no_data",
                "отчёт без данных: ноль кандидатов, находок и токенов - "
                "исполнения не видно (#750)",
            )
        )
    if (row.get("raw_count") or 0) < 1 and report_has_evidence(row):
        # #2082: заявленное исполнение (число агентов) не заменяет биллинг;
        # пару составляют отчёты, в которых ревьюер показал кандидатов.
        out.append(
            (
                "no_data",
                "отчёт без кандидатов: исполнение заявлено, но не доказано "
                "биллингом провайдера - пару такой отчёт не составляет",
            )
        )
    if row.get("principal_id") is None:
        out.append(("identity", "принципал отчёта неизвестен"))
    if row.get("self_reviewed") != 0:
        out.append(("self_reviewed", "саморевью или признак не заявлен"))
    out.extend(_negatives(row))
    return out


def _flaws(row: dict[str, Any], sha: str) -> list[tuple[str, str]]:
    """Почему отчёт не годится в пару: пин поверх единого предиката чистоты."""
    out: list[tuple[str, str]] = []
    if (
        row.get("pin_verified") != 1
        or not sha
        or str(row.get("reviewed_sha") or "").lower() != sha.lower()
    ):
        out.append(("pin", "нет пина, проверенного хабом, на эту сдачу и sha"))
    return out + _unclean(row)


def _pick(
    rows: list[dict[str, Any]], sha: str, reviewers: list[int], implementer: int | None
) -> tuple[dict, dict] | None:
    """Детерминированная пара: (основной, дополнительный) или None.

    Засчитываемые - без изъянов и не от исполнителя. Дополнительный - ПОСЛЕДНИЙ
    засчитываемый от принципала из списка, основной - последний иной от другого
    принципала.
    """
    if implementer is None:
        return None
    ok = [
        r
        for r in rows
        if not _flaws(r, sha) and int(r["principal_id"]) != int(implementer)
    ]
    extras = [r for r in ok if int(r["principal_id"]) in reviewers]
    if not extras:
        return None
    extra = extras[-1]
    mains = [r for r in ok if r["principal_id"] != extra["principal_id"]]
    return (mains[-1], extra) if mains else None


async def _reports(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in await repo.machine_reviews_of_generation(db, task_id, generation)
    ]


async def freeze_pair(db: aiosqlite.Connection, task_id: int, generation: int) -> str:
    """Пара для заказа суждения: JSON ``{"main": id, "extra": id}`` или пусто.

    Пусто - исключения в этом поколении не будет. Не бросает: ошибка чтения
    значит «пары нет», а не отказ заказа.
    """
    try:
        settings = await _settings(db, task_id)
        if settings is None:
            return ""
        task = dict(await repo.get_task(db, task_id) or {})
        sha = str(task.get("submission_sha") or "").strip()
        picked = _pick(
            await _reports(db, task_id, generation),
            sha,
            settings.reviewers,
            task.get("implementer_principal_id"),
        )
    except Exception:  # noqa: BLE001 - fail closed: no pair, no exception
        log.warning("double_clean pair not frozen for #%s", task_id, exc_info=True)
        return ""
    if picked is None:
        return ""
    return json.dumps({"main": picked[0]["id"], "extra": picked[1]["id"]})


def parse_pair(raw: Any) -> tuple[int, int] | None:
    """(основной, дополнительный) из колонки заказа; None - пары нет или кривая."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return int(value["main"]), int(value["extra"])
    except (TypeError, ValueError, KeyError):
        return None


async def frozen_pair(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> tuple[int, int] | None:
    rows = await fetchall(
        db,
        "SELECT double_clean_pair FROM steward_runs "
        "WHERE task_id=? AND generation=? AND kind=?",
        (task_id, generation, _VERDICT_KIND),
    )
    return parse_pair(rows[0]["double_clean_pair"]) if rows else None


def _member(label: str, row: dict[str, Any] | None, sha: str) -> list[str]:
    if row is None:
        return [f"pair: отчёт пары ({label}) не найден среди отчётов поколения"]
    return [
        f"{token}: отчёт #{row['id']} ({label}): {text}"
        for token, text in _flaws(row, sha)
    ]


def _people(
    main: dict | None, extra: dict | None, settings: _Settings, implementer: Any
) -> list[str]:
    if main is None or extra is None:
        return []
    out: list[str] = []
    if implementer is None:
        out.append("identity: исполнитель задачи неизвестен")
    ids = (main.get("principal_id"), extra.get("principal_id"))
    if None not in ids and ids[0] == ids[1]:
        out.append(f"distinct: оба отчёта от принципала {ids[0]}")
    if implementer is not None and implementer in ids:
        out.append(f"implementer: отчёт пары написан исполнителем ({implementer})")
    if extra.get("principal_id") not in settings.reviewers:
        out.append(
            f"reviewers: принципал {extra.get('principal_id')} дополнительного "
            "отчёта не из double_clean_reviewers"
        )
    return out


def _others(rows: list[dict[str, Any]], pair: tuple[int, int] | None) -> list[str]:
    """Прочие отчёты поколения: любой отрицательный отменяет исключение."""
    out: list[str] = []
    for row in rows:
        if pair is not None and row["id"] in pair:
            continue
        out.extend(
            f"{token}: прочий отчёт #{row['id']}: {text}"
            for token, text in _unclean(row)
        )
    return out


def _why_no_pair(
    rows: list[dict[str, Any]], sha: str, settings: _Settings, implementer: Any
) -> list[str]:
    """Почему при заказе не набралась пара: изъяны каждого отчёта поимённо."""
    out = [
        "pair: пара отчётов не зафиксирована при заказе суждения "
        "(на заказе не было двух засчитываемых отчётов)"
    ]
    if implementer is None:
        out.append("identity: исполнитель задачи неизвестен")
    clean: list[dict[str, Any]] = []
    for row in rows:
        flaws = _flaws(row, sha)
        out += [f"{token}: отчёт #{row['id']}: {text}" for token, text in flaws]
        if not flaws:
            clean.append(row)
    own = [
        r for r in clean if implementer is not None and r["principal_id"] == implementer
    ]
    out += [f"implementer: отчёт #{r['id']} написан исполнителем" for r in own]
    usable = [r for r in clean if r not in own]
    if not any(r["principal_id"] in settings.reviewers for r in usable):
        out.append(
            "reviewers: нет чистого отчёта от принципала из double_clean_reviewers"
        )
    elif len({r["principal_id"] for r in usable}) < 2:
        out.append("distinct: нет чистого отчёта второго принципала")
    return out


async def _attempt(db: aiosqlite.Connection, task_id: int, sha: str) -> str:
    report = await repo.get_ci_run_report(db, task_id, sha)
    return str(dict(report).get("attempt_key") or "") if report else ""


async def _ci(
    db: aiosqlite.Connection, task_id: int, sha: str
) -> tuple[str, list[str]]:
    """Попытка CI, на которой стоит зелёное, или пусто и причина.

    Зелёное (``pass_gap``) и ключ попытки читаются разными запросами, поэтому
    ключ снимается ДО и ПОСЛЕ проверки: сменившаяся между ними попытка - не та,
    о которой сказали «зелёная», и доверия не получает.
    """
    if not sha:
        return "", ["ci: sha не закреплён"]
    before = await _attempt(db, task_id, sha)
    gap = await ci_trust.pass_gap(db, task_id, sha)
    after = await _attempt(db, task_id, sha)
    if gap:
        return "", [f"ci: {gap}"]
    if not after or before != after:
        return "", ["ci: попытка CI неизвестна или сменилась во время проверки"]
    return after, []


async def _advisor(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> list[str]:
    from hub.services import steward_advisor as adv

    state = await adv.advisor_state(db, task_id, generation)
    if state.state != adv.STATE_RECEIVED or state.answer != adv.ANSWER_CONCUR:
        return [f"advisor: советник {state.state} {state.answer}".rstrip()]
    if not state.judge_hash or state.judge_hash != state.advisor_hash:
        return ["advisor: согласие советника не на пакете судьи"]
    return []


async def statement_digest(db: aiosqlite.Connection, task_id: int) -> str:
    """Отпечаток постановки, о которой судили и согласились (только база).

    Те же поля, что хеш пакета берёт из брифа (``PACKET_HASH_BRIEF_FIELDS``).
    Полный хеш пакета в транзакции не пересчитать (дифф и вершина ветки - сеть),
    а согласие советника - о ТОЙ постановке: её правка между этапами снимает
    исключение.
    """
    import hashlib

    from hub.services.steward_evidence import PACKET_HASH_BRIEF_FIELDS

    task = dict(await repo.get_task(db, task_id) or {})
    body: dict[str, Any] = {
        k: task.get(k) for k in PACKET_HASH_BRIEF_FIELDS if k != "acceptance_criteria"
    }
    body["acceptance_criteria"] = [
        dict(r) for r in await repo.list_acceptance_criteria(db, task_id)
    ]
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _drift(early: dict[str, Any], late: dict[str, Any]) -> list[str]:
    """Что изменилось между ранним и транзакционным этапами."""
    out: list[str] = []
    for field in ("generation", "sha", "main", "extra", "class", "policy", "statement"):
        if early.get(field) != late.get(field):
            out.append(f"{field}: изменилось между проверками")
    if early.get("ci") != late.get("ci"):
        out.append("ci: попытка CI изменилась между проверками")
    return out


async def evaluate(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    worst: RiskClass,
    *,
    early: dict[str, Any] | None = None,
    late: bool = False,
    statement: str | None = None,
) -> Decision:
    """Решение по исключению. Читает только базу (без сети), тем же соединением.

    ``late`` - транзакционный этап: ему нужен аудит раннего решения, и итог
    обязан с ним совпасть.
    """
    settings = await _settings(db, task_id)
    if settings is None:
        return OFF
    task = dict(await repo.get_task(db, task_id) or {})
    sha = str(task.get("submission_sha") or "").strip()
    unmet: list[str] = []
    if _over(worst, settings):
        unmet.append(f"class: класс {worst.value} выше потолка двойной чистоты")
    if int(task.get("submission_generation") or 0) != generation:
        unmet.append("generation: поколение задачи уже другое")
    pair = await frozen_pair(db, task_id, generation)
    rows = await _reports(db, task_id, generation)
    by_id = {r["id"]: r for r in rows}
    main = by_id.get(pair[0]) if pair else None
    extra = by_id.get(pair[1]) if pair else None
    if pair is None:
        unmet += _why_no_pair(rows, sha, settings, task.get("implementer_principal_id"))
    else:
        unmet += _member("основной", main, sha) + _member("дополнительный", extra, sha)
        unmet += _people(main, extra, settings, task.get("implementer_principal_id"))
    unmet += _others(rows, pair)
    attempt, ci_unmet = await _ci(db, task_id, sha)
    unmet += ci_unmet + await _advisor(db, task_id, generation)
    audit = _audit(settings, worst, generation, sha, main, extra, attempt)
    # Ранний этап получает отпечаток, снятый ДО сборки пакета: правка после
    # сборки не станет «прежней» для обоих этапов сразу.
    audit["statement"] = statement or await statement_digest(db, task_id)
    if late:
        unmet += (
            _drift(early, audit)
            if early is not None
            else ["early: ранний этап исключения не выдавал"]
        )
    return Decision(True, tuple(unmet), audit)


def _over(worst: RiskClass, settings: _Settings) -> bool:
    return _ORDER.index(worst) > _ORDER.index(RiskClass(settings.clean.upper()))


def _audit(
    settings: _Settings,
    worst: RiskClass,
    generation: int,
    sha: str,
    main: dict | None,
    extra: dict | None,
    attempt: str,
) -> dict[str, Any]:
    def side(row: dict | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {"review_id": row["id"], "principal_id": row.get("principal_id")}

    return {
        "mode": MODE,
        "generation": generation,
        "sha": sha,
        "main": side(main),
        "extra": side(extra),
        "ci": {"attempt_key": attempt},
        "class": worst.value,
        "policy": settings.audit(),
    }


async def may_apply_to_class(
    db: aiosqlite.Connection, task_id: int, stored: Any
) -> bool:
    """Чтением: ключи включают исключение и сохранённый класс не выше его потолка.

    Не доказывает, что исключение сработает (пары, CI и советника чтением не
    проверить), - только что оно в принципе достижимо для этого класса.
    """
    settings = await _settings(db, task_id)
    try:
        cls = RiskClass(str(stored or "").strip())
    except ValueError:
        return False
    return settings is not None and not _over(cls, settings)


def line(audit: dict[str, Any]) -> str:
    """Строка карточки об одобрении по двойной чистоте, со ссылками на пару."""
    main, extra = audit["main"], audit["extra"]
    return (
        "Одобрено стюардом по двойной чистоте: "
        f"поколение {audit['generation']}, коммит {str(audit['sha'])[:12]}, "
        f"класс {audit['class']}; отчёты #{main['review_id']} "
        f"(принципал {main['principal_id']}) и #{extra['review_id']} "
        f"(принципал {extra['principal_id']}); CI {audit['ci']['attempt_key']}."
    )
