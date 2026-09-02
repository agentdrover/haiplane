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
    ladder = ladder_refusal(packet)
    if ladder:
        out.append(ladder)
    return out
