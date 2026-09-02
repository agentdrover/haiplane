"""Привратник применения: что стоит между суждением и изменением исхода (#1147).

До F4 стюард только советует, и цена ошибки — лишняя строка в карточке.
После — он меняет исход, и цена ошибки становится дефектом, доставленным в
develop без человека. Этот модуль и есть та разница: он не применяет
ничего, он отвечает на один вопрос — есть ли право применять.

Отвечает ПЕРЕЧИСЛЕНИЕМ, а не примером. Три группы отказов, и у каждой своя
природа:

предусловия
    факты пакета доказательств (#1074): CI на закреплённом sha, вершина
    ветки, дифф против заявленных областей, класс риска, отчёт этой
    генерации. Все под одним кодом ``precondition_failed`` — вопрос у них
    общий: описывает ли пакет тот код, который судят;
громкие основания
    те же пять, при которых отказывается сам автовердикт (#745), каждое со
    своим кодом. Живут в hub/services/gate_grounds.py, чтобы список был
    один: стюард не получает прав, которых нет у автопилота;
ladder
    поверхности, меняющие правила самих гейтов — по ФАКТИЧЕСКОМУ диффу.
    Заявленная область здесь не годится: задача, объявившая «hub/services/»,
    проходит проверку по декларации и меняет hub/auth.py.

ПАКЕТ — ЕДИНСТВЕННЫЙ ВХОД, и это не аккуратность, а правило #1075. Все
факты читаются из пакета, который хаб собрал сам; ни одного пересчёта
рядом. Второй способ узнать тот же факт означает второй ответ на один
вопрос, и расходиться они начнут молча.

Новых кодов эскалации не заводится: словарь #1022 закрыт, и всё, что здесь
нужно, в нём уже есть.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from hub import config
from hub.services import gate_grounds as grounds
from hub.services.auto_approve import ladder_hits
from hub.services.steward_evidence import EvidencePacket, build_evidence_packet

log = logging.getLogger(__name__)

REFUSED_PRECONDITION = "precondition_failed"
REFUSED_LADDER = "ladder_surface"
REFUSED_UNCLOSED = "unclosed_finding"

# Типы закрытия из закрытого словаря #1022, и рядом — ЧЕМ каждый
# подтверждается. Соответствие публичное, потому что его полноту проверяет
# тест: тип, добавленный в словарь и забытый здесь, тихо стал бы закрытием
# без доказательства — то есть словом вместо факта.
CLOSURE_EVIDENCE: dict[str, str] = {
    "fixed": "коммит после отчёта тронул строки находки",
    "human_disposition": "человек вынес диспозицию по этой находке",
    "out_of_scope_linked": "находка вынесена в существующую связанную задачу",
}

# Факты пакета, которые обязаны быть и обязаны быть чистыми, чтобы approve
# можно было применить. Перечень публичный: полноту проверяет тест, а не
# внимательность читателя — тот же приём, которым закреплён пин генерации
# (#1120) и недостижимость act мимо порогов (#1107).
#
# ac_locator, red_base и dependency_state в перечень НЕ входят намеренно:
# они описывают постановку и окружение, а не сдаваемый код, и человеческий
# гейт их сегодня тоже не считает препятствием для approve. Отказывать по
# ним значило бы ужесточить правило, не объявив этого.
PRECONDITION_FACTS: tuple[str, ...] = (
    "machine_review_report",
    "ci_pinned_sha",
    "branch_tip",
    "diff_vs_areas",
    "risk_class",
)


def _fact_refusal(source: str, fact: Any) -> str | None:
    """Почему этот факт не пускает approve, или None, если пускает.

    Отсутствие факта — тоже отказ, и это главное правило пакета (#762):
    «хаб не смог посмотреть» и «хаб посмотрел, там чисто» — разные
    состояния, и подставлять второе вместо первого значит одобрять по
    незнанию.
    """
    if fact is None:
        return f"{source}: факта нет в пакете вовсе"
    if fact.state != "present":
        return f"{source}: {fact.detail}"
    value = fact.value or {}
    if source == "machine_review_report":
        # Незавершённый харнесс — не чистый отчёт. Находки как таковые
        # проверяет соседняя задача (#1148): здесь только полнота прогона.
        if value.get("incomplete"):
            return "machine_review_report: отчёт неполон (incomplete)"
        return None
    if source == "ci_pinned_sha":
        return None if value.get("passed") else f"ci_pinned_sha: {fact.detail}"
    if source == "branch_tip":
        return "branch_tip: вершина уехала после сдачи" if value.get("moved") else None
    if source == "diff_vs_areas":
        if value.get("within_declared"):
            return None
        return f"diff_vs_areas: {fact.detail}"
    if source == "risk_class":
        return (
            "risk_class: дифф поднял класс выше заявленного"
            if value.get("raised")
            else None
        )
    # Незнакомый источник — отказ, а не пропуск: перечень выше и этот
    # разбор обязаны совпадать, и расхождение должно стоить отказа, а не
    # тишины.
    return f"{source}: источник не разобран привратником"


def precondition_refusals(packet: EvidencePacket) -> list[tuple[str, str]]:
    """Отказы по фактам пакета — все, а не первый попавшийся.

    Перечисляются полностью, потому что человеку, читающему фид, нужна
    причина, а не первая из причин: остановиться на одной значит заставить
    его чинить по одной и возвращаться.
    """
    out: list[tuple[str, str]] = []
    for source in PRECONDITION_FACTS:
        refusal = _fact_refusal(source, packet.facts.get(source))
        if refusal:
            out.append((REFUSED_PRECONDITION, refusal))
    return out


async def loud_refusals(
    db: aiosqlite.Connection, task: dict[str, Any], packet: EvidencePacket
) -> list[tuple[str, str]]:
    """Те же пять оснований, при которых отказывается автовердикт.

    Читаются из общего источника (#1147), а не переписываются: правило,
    добавленное туда и забытое здесь, тихо расширило бы автономию.
    """
    report = packet.facts.get("machine_review_report")
    if report is None or report.state != "present":
        # Без отчёта громкие основания не вычислимы, и это уже сказано
        # предусловием. Молчать здесь честнее, чем объявлять чистоту.
        return []
    from hub.services.steward_shadow import reviewer_model as read_reviewer_model

    value = report.value or {}
    review_id = value.get("review_id")
    # Модель ревьюера берётся из записи хаба о запуске, а не из декларации
    # отчёта: хаб САМ запускал ревьюера, и это факт, которым он владеет,
    # тогда как модель, названная отчётом, — утверждение отчёта о себе
    # (#1008).
    reviewer = await read_reviewer_model(db, task["id"], packet.generation)

    pairs = [
        (
            "report_security_finding",
            grounds.security_ground(
                value.get("confirmed") or [],
                value.get("rejected") or [],
                value.get("unresolved") or [],
            ),
        ),
        (
            "report_token_budget",
            grounds.token_budget_ground(
                value.get("tokens_spent"), config.REVIEW_TOKEN_BUDGET
            ),
        ),
        (
            "report_sibling_mismatch",
            await grounds.sibling_mismatch_ground(
                db, task["id"], packet.generation, int(review_id or 0)
            ),
        ),
        (
            "self_authored",
            grounds.self_review_ground(
                bool(value.get("self_reviewed")),
                config.REVIEW_SELF_APPROVE == "allow",
            ),
        ),
        (
            "same_family_as_reviewer",
            grounds.monoculture_ground(
                (task.get("submission_model") or "").strip(), reviewer
            ),
        ),
    ]
    return [(code, detail) for code, detail in pairs if detail]


async def closure_refusals(
    db: aiosqlite.Connection, task_id: int, packet: EvidencePacket
) -> list[tuple[str, str]]:
    """Каждая подтверждённая находка закрыта, и КАЖДОЕ закрытие проверено.

    Грязный путь существует ради случая, когда ревью что-то нашло, а работа
    всё равно годна. Но если approve проходит, не отчитавшись по каждой
    находке, находки перестают влиять на исход — и отчёт превращается в
    текст, который никто не обязан читать.

    Факт закрытия считает ХАБ. «Исправлено» нельзя принимать на слово:
    модель, которая судит, и модель, которая пишет, ошибаются одинаково
    охотно, а правку хаб умеет увидеть сам. Три типа — три разных источника
    факта, и ни одного, который сообщал бы сам себя:

    fixed
        коммит после отчёта тронул диапазон строк находки. Считается тем
        же расчётом, что и доказательство в карточке (#1039), и ДО
        закреплённого sha, а не до вершины ветки: решение о сдаче читает
        то, что сдача закрепила (урок #1150);
    human_disposition
        по находке уже есть запись человека (#1038). Ссылка на решение, а
        не пересказ его;
    out_of_scope_linked
        находка вынесена в СУЩЕСТВУЮЩУЮ задачу: linked_task_id в записи
        исхода, и эта задача проверяется на существование. Обещание завести
        её потом закрытием не является.

    Возвращает по одному отказу на каждую незакрытую находку, а не первый:
    человеку, который будет разбирать, нужен список, а не первая строка из
    него.
    """
    from hub.services.finding_evidence import evidence_for_report
    from hub.services.finding_identity import finding_uids

    report = packet.facts.get("machine_review_report")
    if report is None or report.state != "present":
        # Отчёта нет — предусловие уже отказало. Молчать здесь честнее, чем
        # объявлять, что закрывать нечего.
        return []
    value = report.value or {}
    confirmed = [f for f in (value.get("confirmed") or []) if isinstance(f, dict)]
    if not confirmed:
        return []

    claimed = await _claimed_closures(db, task_id, packet.generation)
    uids = finding_uids(confirmed)

    touched: dict[str, str] = {}
    if any(claimed.get(uid) == "fixed" for uid in uids):
        # Один обход ветки на все находки: вызов на каждую сделал бы
        # привратника непригодным на живом размере отчёта (#1042).
        #
        # Считаем ДО закреплённого sha, а не до вершины ветки. Вершина —
        # движущаяся цель: к моменту вопроса она может стоять не там, где
        # стояла сдача, и «исправлено» подтверждалось бы коммитом, которого
        # в сдаче нет (урок #1150).
        from hub import repository as repo

        row = await repo.get_task(db, task_id)
        pinned = ((dict(row).get("submission_sha") if row else "") or "").strip()
        evidence = await evidence_for_report(
            db, task_id, confirmed, generation=packet.generation, head=pinned
        )
        touched = {
            uid: str(blob.get("outcome") or "") for uid, blob in evidence.items()
        }

    out: list[tuple[str, str]] = []
    for uid, finding in zip(uids, confirmed, strict=True):
        title = str(finding.get("title") or uid)[:80]
        kind = claimed.get(uid)
        if kind is None:
            out.append(
                (REFUSED_UNCLOSED, f"находка «{title}» ({uid}) не закрыта ничем")
            )
            continue
        if kind not in CLOSURE_EVIDENCE:
            out.append(
                (
                    REFUSED_UNCLOSED,
                    f"находка «{title}» ({uid}): закрытие типа {kind!r} вне "
                    f"словаря {sorted(CLOSURE_EVIDENCE)}",
                )
            )
            continue
        proven = await _closure_is_proven(db, task_id, packet, uid, kind, touched)
        if not proven:
            out.append(
                (
                    REFUSED_UNCLOSED,
                    f"находка «{title}» ({uid}): закрытие {kind} объявлено, но "
                    f"хаб его не подтверждает — ожидалось, что "
                    f"{CLOSURE_EVIDENCE[kind]}",
                )
            )
    return out


async def _claimed_closures(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> dict[str, str]:
    """Что стюард ЗАЯВИЛ про каждую находку — по uid, а не по позиции (#1007)."""
    import json

    from hub import repository as repo

    row = await repo.get_steward_judgement(db, task_id, generation, "verdict")
    if row is None:
        return {}
    try:
        entries = json.loads(dict(row).get("closures") or "[]")
    except (TypeError, ValueError):
        return {}
    if not isinstance(entries, list):
        return {}
    return {
        str(e.get("finding_uid")): str(e.get("type"))
        for e in entries
        if isinstance(e, dict) and e.get("finding_uid")
    }


async def _closure_is_proven(
    db: aiosqlite.Connection,
    task_id: int,
    packet: EvidencePacket,
    uid: str,
    kind: str,
    touched: dict[str, str],
) -> bool:
    """Подтверждает ли ХАБ это закрытие. Незнание — не подтверждение (#762)."""
    from hub import repository as repo
    from hub.services.finding_evidence import OUTCOME_TOUCHED

    if kind == "fixed":
        return touched.get(uid) == OUTCOME_TOUCHED
    report = packet.facts.get("machine_review_report")
    review_id = int(((report.value if report else None) or {}).get("review_id") or 0)
    if kind == "human_disposition":
        rows = await repo.list_finding_dispositions(db, review_id)
        return any(
            str(dict(r).get("finding_uid") or "") == uid
            and str(dict(r).get("decided_by") or "").strip()
            for r in rows
        )
    if kind == "out_of_scope_linked":
        for row in await repo.list_finding_outcomes(db, review_id):
            entry = dict(row)
            if str(entry.get("finding_uid") or "") != uid:
                continue
            linked = entry.get("linked_task_id")
            if not linked:
                return False
            # Задача обязана СУЩЕСТВОВАТЬ: ссылка на несозданное — обещание,
            # а не вынос.
            return await repo.get_task(db, int(linked)) is not None
        return False
    return False


def ladder_refusal(packet: EvidencePacket) -> tuple[str, str] | None:
    """Ladder — по фактическому диффу, а не по заявленным областям.

    Дифф лежит в пакете (``diff_vs_areas.paths``), собранный тем же обходом
    ветки, которым хаб считает выход за области. Если прочитать его не
    удалось, факт отсутствует — и отказ уже дан предусловием: нечитаемый
    дифф не есть безопасный дифф.
    """
    fact = packet.facts.get("diff_vs_areas")
    if fact is None or fact.state != "present":
        return None
    hits = ladder_hits(list((fact.value or {}).get("paths") or []))
    if not hits:
        return None
    return (
        REFUSED_LADDER,
        "дифф трогает поверхности, меняющие правила гейтов: "
        + ", ".join(hits)
        + " — такое решение остаётся человеку независимо от класса",
    )


def mode_refusal() -> tuple[str, str] | None:
    """Выключенный или неузнанный режим — отказ до всякой проверки фактов.

    Нераспознанное значение читается как off (#835): опечатка в drop-in не
    имеет права быть тем, что включает контур. Читается синхронным
    читателем, который никогда не отдаёт act (#1107) — здесь достаточно
    знать, что контур вообще открыт.
    """
    from hub.services.steward_dispatch import requested_mode

    if requested_mode() != "off":
        return None
    return (
        REFUSED_PRECONDITION,
        f"STEWARD_MODE={config.STEWARD_MODE!r} — контур закрыт, применять нечего",
    )


async def apply_refusals(
    db: aiosqlite.Connection, task_id: int, generation: int | None = None
) -> list[tuple[str, str]]:
    """Все причины, по которым approve этой сдачи применять нельзя.

    Пустой список означает «привратник не возражает» — не «применяй». Что
    делать дальше, решают соседние задачи F4: закрытия находок (#1148) и
    сами переходы (#1149). Разделение намеренное: «можно ли» и «что именно
    произойдёт» ошибаются по-разному, и функция, отвечающая на оба вопроса
    сразу, не проверяется по половине.
    """
    mode = mode_refusal()
    if mode:
        return [mode]
    packet = await build_evidence_packet(db, task_id, generation)
    if packet is None:
        return [(REFUSED_PRECONDITION, f"задачи #{task_id} нет")]
    from hub import repository as repo

    row = await repo.get_task(db, task_id)
    task = dict(row) if row is not None else {"id": task_id}

    out = precondition_refusals(packet)
    out.extend(await loud_refusals(db, task, packet))
    out.extend(await closure_refusals(db, task_id, packet))
    ladder = ladder_refusal(packet)
    if ladder:
        out.append(ladder)
    return out
