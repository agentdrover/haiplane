"""Причина отказа применения одной строкой для карточки: закрытый словарь (#2040).

Текст исключения в карточку не попадает НИКОГДА: в нём бывают слова моделей,
значения из отчёта CI, путь, токен. Показывается фиксированный текст хаба по
КОДУ причины; код, которого в словаре нет, называется только номером отказа.

Код берётся из трёх мест, от надёжного к ненадёжному: атрибут ``code`` у
``CodedRefusal`` (писатель сам назвал причину), ``reason`` словаря ``detail``
(ворота вердикта), слово до первого двоеточия строки ``detail``. Ненадёжные
источники безопасны потому, что код проверяется по словарю и от него остаётся
только фиксированный текст.
"""

from __future__ import annotations

from fastapi import HTTPException

#: Код отказа ворот транзакции записи при снятом допуске ``act`` (#2040). Не код
#: эскалации судьи: словарь #1022 закрыт, поэтому и назван не ``REFUSED_*``.
ACT_REVOKED = "act_revoked"
WHERE = "подробности в журнале хаба"

#: Код причины -> фиксированный текст хаба. Новый код - строка здесь.
REASON_TEXTS: dict[str, str] = {
    ACT_REVOKED: "автономия act больше не выдана: стюарда выключили или критерии выхода перестали выполняться",
    "policy_not_delegated": "вердикт проекта не отдан стюарду",
    "advisor_not_concurred": "нет согласия советника на этом пакете фактов, либо сдача передана человеку из-за потолка",
    "ci_not_green": "CI на закреплённом коммите не зелёный",
    "ci_integrity": "дифф меняет входы CI или не прочитан",
    "class_above_policy_ceiling": "класс сдачи выше потолка политики проекта",
    "precondition_failed": "не выполнено предусловие применения",
    "verdict_already_stands": "на эту сдачу вердикт уже стоит",
    "verdict_generation_moved": "поколение сдачи уже другое",
    "verdict_not_written": "вердикт не записан",
    "generation_moved": "поколение сдачи уже другое",
    "submission_moved": "суждение относится к прошлой сдаче",
    "ac_tests_not_green": "тесты критериев приёмки не зелёные",
    "machine_review_required": "обязательное машинное ревью отсутствует или не удовлетворяет политике",
    "verdict_window_closed": "одобрение этой сдачи закрыто решением человека, нужна новая сдача",
    "verdict_repeats_previous": "вердикт повторяет предыдущий дословно",
    "verdict_taken": "на эту сдачу вердикт уже записан",
}


class CodedRefusal(HTTPException):
    """Отказ с кодом причины, названным самим писателем (ключ ``REASON_TEXTS``)."""

    def __init__(self, status_code: int, detail: str, *, code: str) -> None:
        super().__init__(status_code, detail=detail)
        self.code = code


def _code_of(exc: HTTPException) -> str:
    coded = getattr(exc, "code", None)
    if isinstance(coded, str):
        return coded
    if isinstance(exc.detail, dict):
        reason = exc.detail.get("reason") or exc.detail.get("code")
        return reason if isinstance(reason, str) else ""
    return str(exc.detail).split(":", 1)[0].strip()


def reason_line(exc: Exception) -> str:
    """Причина неудачного применения: фиксированный текст по коду, без текста исключения."""
    if not isinstance(exc, HTTPException):
        return f"внутренняя ошибка применения ({type(exc).__name__}), {WHERE}"
    code = _code_of(exc)
    if code in REASON_TEXTS:
        return f"{code}: {REASON_TEXTS[code]}"
    return f"отказ {exc.status_code}, {WHERE}"
