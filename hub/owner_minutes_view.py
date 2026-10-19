"""Контракт оценки минут владельца (#1874): один объект на все поверхности.

Карточка, REST (``TaskView.owner_minutes``), ``hub_task_status`` и ``hp-hub``
показывают ЭТОТ объект как есть: число не пересчитывается на стороне
читателя. Модуль чистый (pydantic), поэтому стоит в слое моделей.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: Подпись при любом показе числа: это оценка по нормативам, не замер.
ESTIMATE_LABEL = "оценка, с начала учёта"


class OwnerMinutesPrincipal(BaseModel):
    principal_id: int
    username: str = ""
    seconds: int
    accruals: int


class OwnerMinutesView(BaseModel):
    #: ok - есть начисления и пас догнал ленту; no_data - начислений нет, пас
    #: догнал; behind - пас отстаёт от ленты событий (число может быть неполным);
    #: not_started - учёт ещё не включён (пас ни разу не отработал).
    status: str
    total_seconds: int | None = None
    accruals: int = 0
    by_principal: list[OwnerMinutesPrincipal] = Field(default_factory=list)
    tracking_since: str | None = None
    processed_through_event_id: int = 0
    caught_up: bool = False
    norms_version: int = 0
    label: str = ESTIMATE_LABEL
    #: Применённый часовой пояс (IANA): по нему считается «день».
    timezone: str = ""
    #: Готовая строка для людей («7 мин (оценка, с начала учёта)»): карточка,
    #: MCP и CLI печатают её, а не пересчитывают число у себя.
    line: str = ""


class OwnerMinutesDayView(OwnerMinutesView):
    principal_id: int
    date: str
