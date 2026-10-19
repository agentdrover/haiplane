"""Годен ли отчёт машинного ревью для совета «Принять» (#1810).

Одна функция, белый список условий. Прежний подход искал плохие признаки по
одному (неполный, самоотчёт, unresolved, пустой…), и каждый круг ревью находил
следующий путь, который список не назвал. Здесь наоборот: отчёт годен, только
если ВЕРНО каждое из условий ниже; всё, что условие не назвало, годным не
считается.

Условия (все обязательны, порядок — порядок показа причин):

* исход отчёта — чисто (``outcome_names_clean``, ступень лестницы #1234);
* ``incomplete`` заявлен явно и равен false (None — «не заявлено», не «полный»);
* ``lost_dimensions`` пуст: потерянное измерение — непроверенное, а не чистое;
* нет unresolved вовсе: исход «неразрешённые» не чист, даже если автор уже
  ответил по каждому (ответ автора не доказывает, что дефекта нет);
  ``unanswered`` (по ledger исходов, «не рассудил» ответом не считается)
  нужен только для текста причины;
* ``provider_tokens`` > 0: работу ревьюера подтвердил провайдер. Не
  ``raw_count`` и не ``tokens_spent`` — их пишет сам отчёт о себе;
* происхождение не текстовое: отчёт сдан ревьюером по контракту, а не
  восстановлен хабом из текста прогона (#1587);
* ревьюер не автор: ``self_reviewed`` ложно и личность ревьюера
  (``principal_id``) установлена.

Совет «Принять» требует годности КАЖДОГО отчёта текущего поколения и хотя бы
одного отчёта; это решает вызывающий (``decision_brief``).
"""

from __future__ import annotations

from hub.models import MachineReviewView, text_origin_of
from hub.services.steward_corridor import OUTCOME_CONFIRMED

GAP_CONFIRMED = "confirmed"
GAP_OUTCOME = "outcome"
GAP_INCOMPLETE = "incomplete"
GAP_LOST = "lost_dimensions"
GAP_UNRESOLVED = "unresolved"
GAP_TOKENS = "provider_tokens"
GAP_ORIGIN = "origin"
GAP_SELF = "self_review"
GAP_IDENTITY = "identity"


def report_gaps(view: MachineReviewView, unanswered: int = 0) -> list[tuple[str, str]]:
    """Какие условия белого списка НЕ выполнены: ``[(код, текст)]``, пусто — годен."""
    gaps: list[tuple[str, str]] = []
    if view.outcome == OUTCOME_CONFIRMED:
        gaps.append(
            (
                GAP_CONFIRMED,
                f"Ревьюер подтвердил проблем: {len(view.findings_confirmed)}",
            )
        )
    elif not view.outcome_names_clean:
        gaps.append((GAP_OUTCOME, f"Исход отчёта не «чисто»: {view.outcome_label}"))
    if view.incomplete is not False:
        gaps.append((GAP_INCOMPLETE, "Отчёт ревью неполный или не сказал о полноте"))
    if view.lost_dimensions:
        gaps.append(
            (
                GAP_LOST,
                "Ревьюер потерял измерения проверки: "
                + ", ".join(view.lost_dimensions),
            )
        )
    if unanswered:
        gaps.append(
            (GAP_UNRESOLVED, f"Находок без проверки и без ответа: {unanswered}")
        )
    if (view.provider_tokens or 0) <= 0:
        gaps.append(
            (GAP_TOKENS, "Провайдер не подтвердил работу ревьюера (provider_tokens)")
        )
    if text_origin_of(view.orchestrator):
        gaps.append(
            (GAP_ORIGIN, "Отчёт восстановлен хабом из текста, а не сдан по контракту")
        )
    if view.self_reviewed:
        gaps.append((GAP_SELF, "Отчёт — не независимый, а самоотчёт"))
    elif view.principal_id is None:
        gaps.append((GAP_IDENTITY, "Независимость ревьюера не подтверждена"))
    return gaps


def report_fit_for_accept(view: MachineReviewView, unanswered: int = 0) -> bool:
    """Отчёт годен для «Принять»: ни одно условие белого списка не нарушено."""
    return not report_gaps(view, unanswered)
