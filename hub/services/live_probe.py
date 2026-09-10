"""Живой зонд: хаб сам смотрит, изменилось ли поведение после доставки (#1236).

09.09.2026 у ВСЕХ одиннадцати сдач дня живая проверка стояла в ``unknown`` с
одной и той же причиной: поведение в проде никто не наблюдал. Это не лень
исполнителей. Исполнитель #1206 дошёл до конца доступного ему пути — ssh
работает, каталог службы читается, unit с env-файлом виден, читающий скрипт
написан, — и остановился там, где начинается ключ провайдера: подать его
процессу агент не может, а обходить запрещено. Два критерия приёмки остались
незакрытыми, и гейт завёл на это дефект-драфт #1230.

Правило, которое здесь оплачено: **чтобы судить о поведении системы, надо
вызвать ЕЁ код в ЕЁ окружении**. Ручной запуск от другого пользователя с
другими переменными измеряет соседнюю систему, похожую на нужную. Единственный,
кто уже стоит в настоящем окружении, — сам хаб.

ГРАНИЦА, БЕЗ КОТОРОЙ ЭТОГО НЕЛЬЗЯ ДЕЛАТЬ ВООБЩЕ. Постановку пишет агент, а
исполнять объявленное в ней будет служба с ключами. Произвольная строка,
исполняемая на проде, — это не проверка, а дыра, и никакая формулировка задачи
её не оправдывает. Поэтому объявление зонда — НЕ команда и не текст вызова, а
ОДНО ИМЯ из закрытого реестра ниже. Ни аргументов, ни параметров: всё, что
зонду нужно знать о задаче, он берёт из собственных записей хаба, а не из
текста карточки. Текст постановки остаётся данными и никогда не становится
командой (то же правило, что #1076 провёл для входов ревью).

Второй виток той же границы — на исполнении. Запись реестра называет методы,
которыми зонду разрешено ходить наружу, и ``probe_refusal`` спрашивается на
ОБОИХ входах: при объявлении и перед каждым запуском. Второй раз — не паранойя:
реестр правят люди, колонку можно заполнить мимо объявления (миграцией, ручным
UPDATE, старой записью, пережившей сужение реестра), и проверка только на входе
означала бы, что запись в базе сильнее правила.

ЧЕГО ЗОНД НЕ ДЕЛАЕТ. Не меняет состояние — ни у нас, ни у провайдера. Не
получает и не печатает секретов: запись реестра носит ИМЯ настройки
(``CURSOR_API_KEY``), а значение берётся процессом службы из окружения и не
попадает ни в карточку, ни в лог, ни в аргументы. Не решает судьбу задачи:
зонд снимается ПОСЛЕ доставки и отвечает на вопрос «поведение изменилось», а не
«код годится», — его отказ не откатывает доставку и не трогает вердикт (AC-3).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub.mcp_envelope import enrich_error_payload

log = logging.getLogger("hub")

#: Имя зонда — один скучный токен. Не путь, не URL, не команда: всё, что
#: сложнее имени, пришлось бы разбирать, а разбор текста из карточки и есть та
#: поверхность, которую эта задача обязана закрыть.
PROBE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

#: Единственные методы, которыми зонду разрешено ходить наружу.
READ_ONLY_METHODS = frozenset({"GET", "HEAD"})

UNKNOWN_PROBE = "unknown_live_probe"
MUTATING_PROBE = "mutating_live_probe"
MALFORMED_PROBE = "malformed_live_probe"


class MutatingProbeRefused(RuntimeError):
    """Зонд, который меняет состояние, до провода не доходит."""


@dataclass(frozen=True)
class ProbeOutcome:
    """Чем кончился один запуск зонда.

    ``observed=False`` — это НЕ «поведение не то». Это «ответа, на который можно
    опереться, хаб не получил»: сеть, лимит провайдера, ненастроенное окружение.
    Разница именно та, которую условие пересмотра этой задачи и ловит, поэтому
    она названа полем, а не выведена из пустой строки.
    """

    observed: bool
    observation: str = ""
    reason: str = ""


ProbeRunner = Callable[[aiosqlite.Connection, dict[str, Any]], Awaitable[ProbeOutcome]]


@dataclass(frozen=True)
class ProbeSpec:
    """Одна разрешённая проба: что спрашивается и чем это ограничено."""

    #: Имя, которым зонд объявляется в постановке.
    name: str
    #: Одна фраза для человека в карточке.
    summary: str
    #: Что именно спрашивается — попадает в поле ``probe`` наблюдения.
    call: str
    #: Методы, которыми зонду разрешено ходить наружу.
    methods: frozenset[str]
    runner: ProbeRunner
    #: ИМЯ настройки, из которой служба берёт ключ. Никогда не значение и
    #: никогда не его длина: длина — тоже сведение о секрете (AC-4). Поле
    #: названо ``setting_name``, а не ``secret_*``: bandit (B106) читает
    #: аргумент с таким именем как захардкоженный пароль — и по-своему прав,
    #: потому что отличить имя от значения по строке нельзя. Имя, которое не
    #: обещает секрета, честнее подавленной проверки.
    setting_name: str = ""

    @property
    def mutating(self) -> bool:
        return not self.methods <= READ_ONLY_METHODS


async def _review_agent_name_roundtrip(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> ProbeOutcome:
    """Сверить имя заказанного ревьюера у провайдера с меткой хаба (#1206).

    Первый живой случай, ради которого всё это заводится. Хаб называет своего
    агента меткой ``haiplane:review:t<id>:g<поколение>:a<попытка>`` — иначе
    провайдер придумывает имя из промта, и узнать СВОЙ заказ после оборвавшегося
    вызова нечем. Утверждение «метка доезжает до провайдера в точности» до
    сегодняшнего дня было выведено из кода, а не наблюдено.

    Читающий запрос: ``GET /v1/agents``. Ничего не создаёт, ничего не удаляет,
    новых агентов ради замера не покупает. Сравнение — на РАВЕНСТВО, тем же
    ``find_agent_by_name``, которым живёт подбор: соседнее поколение отличается
    одним символом, и вхождение отдало бы сдаче чужого судью.
    """
    from hub.db import fetchall
    from hub.integrations import cursor_cloud

    if not cursor_cloud.is_configured():
        return ProbeOutcome(
            False,
            reason=(
                "у службы не задана настройка CURSOR_API_KEY — спросить "
                "провайдера нечем; это факт об окружении, а не о поведении"
            ),
        )

    task_id = int(task["id"])
    rows = await fetchall(
        db,
        "SELECT submission_generation, agent_id FROM review_dispatches "
        "WHERE task_id = ? ORDER BY submission_generation, id",
        (task_id,),
    )
    if not rows:
        return ProbeOutcome(
            False,
            reason=(
                "хаб не заказывал этой задаче облачного ревьюера — сверять "
                "нечего, и «совпало» здесь было бы утверждением о пустоте"
            ),
        )

    ordinal: dict[int, int] = {}
    lines: list[str] = []
    matched = 0
    for row in rows:
        data = dict(row)
        generation = int(data.get("submission_generation") or 0)
        ordinal[generation] = ordinal.get(generation, 0) + 1
        marker = cursor_cloud.agent_marker(
            "review", task_id, generation, ordinal[generation]
        )
        seen = await cursor_cloud.find_agent_by_name(marker)
        if not seen.asked:
            # «Не смогли спросить» — не «имени нет». Половина сверки, выданная
            # за целую, хуже отсутствия сверки: она закрыла бы критерий.
            return ProbeOutcome(
                False,
                reason=(
                    f"список агентов у провайдера прочитать не удалось — метка "
                    f"«{marker}» ни подтверждена, ни опровергнута"
                ),
            )
        recorded = str(data.get("agent_id") or "").strip()
        if seen.agent_id and seen.agent_id == recorded:
            matched += 1
            lines.append(f"«{marker}» → агент {seen.agent_id}: имя совпало")
        elif seen.agent_id:
            lines.append(
                f"«{marker}» → агент {seen.agent_id}, а хаб записал "
                f"{recorded or '(пусто)'}"
            )
        else:
            lines.append(f"«{marker}» → агента с таким именем у провайдера нет")

    return ProbeOutcome(
        observed=True,
        observation=(
            f"GET /v1/agents: сверено меток {len(lines)}, совпало посимвольно "
            f"{matched}. " + "; ".join(lines)
        ),
    )


#: Закрытый набор. Всё, что хаб умеет спросить сам, и ничего сверх: реестр —
#: это и есть граница безопасности, а не подсказка к ней. Пополняется правкой
#: этого файла, то есть через ревью, а не текстом карточки.
PROBES: dict[str, ProbeSpec] = {
    "review_agent_name_roundtrip": ProbeSpec(
        name="review_agent_name_roundtrip",
        summary=(
            "Сверить имя заказанного облачного ревьюера у провайдера с меткой "
            "хаба посимвольно"
        ),
        call="GET /v1/agents (cursor cloud), сверка имени с меткой хаба",
        methods=frozenset({"GET"}),
        runner=_review_agent_name_roundtrip,
        setting_name="CURSOR_API_KEY",
    ),
}


def probe_refusal(name: str) -> tuple[str, str]:
    """``(причина, объяснение)`` или ``("", "")``, если зонд допустим.

    Одна функция на оба входа — объявление и запуск. Два списка условий
    разошлись бы, и слабейший стал бы настоящим (#519).
    """
    probe = (name or "").strip()
    if not probe:
        return "", ""
    if not PROBE_NAME_RE.match(probe):
        return (
            MALFORMED_PROBE,
            "зонд объявляется ИМЕНЕМ из реестра, а не строкой вызова: "
            f"«{probe[:80]}» на имя не похоже",
        )
    spec = PROBES.get(probe)
    if spec is None:
        return (
            UNKNOWN_PROBE,
            f"зонда «{probe}» в реестре нет; хаб исполняет только то, что "
            f"умеет спросить сам: {', '.join(sorted(PROBES)) or '(реестр пуст)'}",
        )
    if spec.mutating:
        return (
            MUTATING_PROBE,
            f"зонд «{probe}» ходит методами "
            f"{', '.join(sorted(spec.methods - READ_ONLY_METHODS))} — он меняет "
            "состояние, а живой зонд только читает",
        )
    return "", ""


def validate_declared_probe(name: str) -> None:
    """Отказать в объявлении недопустимого зонда, назвав причину."""
    reason, message = probe_refusal(name)
    if not reason:
        return
    raise HTTPException(
        422,
        detail=enrich_error_payload(
            {
                "reason": reason,
                "actor_hint": "agent",
                "message": message,
                "hint": (
                    "live_probe — одно имя из закрытого реестра читающих проб "
                    "(hub/services/live_probe.py). Параметров у него нет: всё, "
                    "что зонду нужно о задаче, он берёт из записей хаба. Нужна "
                    "другая проба — её добавляют правкой реестра через ревью."
                ),
            }
        ),
    )


async def run_declared_probe(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    agent: str = "hub",
) -> dict[str, Any] | None:
    """Исполнить объявленный задачей зонд и положить исход в карточку.

    Возвращает записанное наблюдение, либо ``None``, если зонд не объявлен —
    отсутствие зонда не отказ и молчит (обязательной живая проверка этой
    задачей не делается).

    Вызывается ПОСЛЕ доставки, из окружения службы. Ничего не поднимает наружу:
    доставка уже состоялась, и упавшая бухгалтерия не имеет права её отменять.
    """
    from hub import repository as repo

    row = await repo.get_task(db, task_id)
    if row is None:
        return None
    task = dict(row)
    declared = (task.get("live_probe") or "").strip()
    if not declared:
        return None

    reason, message = probe_refusal(declared)
    if reason:
        # Объявление могло пройти до того, как реестр сузили, — или мимо
        # объявления вовсе. Отказ называется и остаётся виден.
        return await _record_failure(
            db,
            task_id,
            probe=f"объявленный зонд «{declared}»",
            reason=message,
            agent=agent,
        )

    spec = PROBES[declared]
    try:
        outcome = await spec.runner(db, task)
    except MutatingProbeRefused as exc:
        return await _record_failure(
            db,
            task_id,
            probe=spec.call,
            reason=f"зонд попытался изменить состояние и был остановлен: {exc}",
            agent=agent,
        )
    except Exception as exc:  # noqa: BLE001 - деградация здесь и есть контракт
        log.exception("живой зонд %s задачи #%s упал", spec.name, task_id)
        return await _record_failure(
            db,
            task_id,
            probe=spec.call,
            reason=f"зонд не отработал: {type(exc).__name__}: {exc}"[:500],
            agent=agent,
        )

    if not outcome.observed:
        return await _record_failure(
            db, task_id, probe=spec.call, reason=outcome.reason, agent=agent
        )
    return await _record_observation(
        db, task_id, probe=spec.call, observation=outcome.observation, agent=agent
    )


async def _record_observation(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    probe: str,
    observation: str,
    agent: str,
) -> dict[str, Any]:
    """Наблюдение — через ту же запись, которой пользуются люди.

    Второй путь записи означал бы вторые правила: сегодня он обошёл бы
    единственную проверку, ради которой всё держится, — свидетельство о коде,
    которого в проде нет, выглядит сильнее всех прочих блоков и при этом ложно
    (#837). Отказ этой проверки не теряется: он становится названным провалом
    зонда, а не тишиной.
    """
    from hub.models import LiveCheckRecord
    from hub.services import live_check as live_check_service

    try:
        view = await live_check_service.record_live_check(
            db,
            task_id,
            LiveCheckRecord(
                outcome=live_check_service.DONE, probe=probe, observation=observation
            ),
            agent=agent,
            principal_id=None,
        )
    except HTTPException as exc:
        detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
        return await _record_failure(
            db,
            task_id,
            probe=probe,
            reason=str(detail.get("message") or exc.detail),
            agent=agent,
        )
    return view.model_dump()


async def _record_failure(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    probe: str,
    reason: str,
    agent: str,
) -> dict[str, Any]:
    from hub.models import LiveCheckRecord
    from hub.services import live_check as live_check_service

    view = await live_check_service.record_live_check(
        db,
        task_id,
        LiveCheckRecord(
            outcome=live_check_service.FAILED,
            probe=probe,
            reason=reason or "зонд не назвал причину",
        ),
        agent=agent,
        principal_id=None,
    )
    return view.model_dump()
