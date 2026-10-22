"""Заявленный клиентом sha сдачи: формат и три входа (#1937).

Одна функция разбора на REST (поле модели), CLI и MCP: три разбора разошлись бы
ровно так, как расходятся поверхности. Значение в сообщения об ошибке не
попадает. Файл отдельный: models.py, cli.py и lifecycle.py стоят под потолком
размера, и поле с тремя входами в них не помещается. Здесь же пин отчёта
ревью (#1985): поколение и полный sha сдачи, которую смотрели, - тот же формат
и та же причина.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

EXPECTED_SHA_FORMAT = "ровно 40 шестнадцатеричных символов (полный sha коммита)"
_EXPECTED_SHA_RE = re.compile(r"[0-9a-fA-F]{40}", re.ASCII)


def normalize_expected_sha(value: Any) -> str | None:
    """None/"" — поля нет; иначе 40 hex в нижнем регистре, иначе ValueError."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or _EXPECTED_SHA_RE.fullmatch(value) is None:
        raise ValueError(EXPECTED_SHA_FORMAT)
    return value.lower()


class ExpectedShaMixin(BaseModel):
    """Поле expected_sha тела сдачи: заявление, которое хаб сверяет с наблюдением."""

    expected_sha: str | None = None

    @field_validator("expected_sha", mode="before")
    @classmethod
    def _expected_sha_is_a_full_sha(cls, value: Any) -> str | None:
        return normalize_expected_sha(value)


def put_expected_sha_in(body: dict[str, Any], value: str) -> dict[str, Any] | None:
    """MCP: положить в тело; при неверном формате вернуть отказ (до сети)."""
    try:
        if normalize_expected_sha(value) is not None:
            body["expected_sha"] = value
    except ValueError as exc:
        return {
            "reason": "expected_sha_invalid",
            "actor_hint": "agent",
            "retry_by_same_caller": False,
            "message": f"expected_sha: {exc}",
            "hint": "Pass the full 40-hex commit sha or omit the field.",
        }
    return None


def put_expected_sha_cli(body: dict[str, Any], args: argparse.Namespace) -> bool:
    """CLI: False — отказ напечатан, сети не было. Регистр нормализует хаб."""
    refusal = put_expected_sha_in(body, getattr(args, "expected_sha", "") or "")
    if refusal is not None:
        print(f"--expected-sha: {refusal['message']}", file=sys.stderr)
    return refusal is None


def add_expected_sha_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--expected-sha",
        dest="expected_sha",
        default="",
        help=(
            "Full 40-hex sha the critic checked (#1937): the hub observes the "
            "branch tip itself and refuses 409 head_moved/head_unverified "
            "when it differs"
        ),
    )


PIN_FIELDS = ("expected_generation", "reviewed_sha")


class ReviewPinSubmit(BaseModel):
    """Необязательный пин тела отчёта: оба поля или ни одного.

    Нет обоих - прежний приём без проверки. Оба - хаб сверит их с ``submissions``
    в транзакции записи. Одно поле - отказ: половина пина не доказывает ничего,
    а молча принятая выглядела бы проверенной.
    """

    expected_generation: int | None = Field(default=None, ge=1)
    reviewed_sha: str | None = None

    @field_validator("reviewed_sha", mode="before")
    @classmethod
    def _reviewed_sha_is_a_full_sha(cls, value: Any) -> str | None:
        return normalize_expected_sha(value)

    @model_validator(mode="after")
    def _pin_is_whole(self) -> "ReviewPinSubmit":
        if (self.expected_generation is None) != (self.reviewed_sha is None):
            raise ValueError(
                "пин отчёта задаётся парой: expected_generation и reviewed_sha "
                "вместе или ни одного"
            )
        return self

    @classmethod
    def from_run_text(cls, payload: dict[str, Any]) -> Any:
        """Отчёт из текста рана (#1036): пин из текста не принимается.

        Текст пишет сам ран, и пин в нём был бы словом проверяемого о себе. Поля
        отбрасываются до разбора, чтобы одно оставшееся поле не стоило отчёта.
        """
        return cls(**{k: v for k, v in payload.items() if k not in PIN_FIELDS})


class ReviewPinView(BaseModel):
    """Пин в показе: что отчёт заявил и сверил ли хаб это при записи."""

    #: Полный sha, который ревьюер заявил как проверенный; пусто - пина не было.
    reviewed_sha: str = ""
    #: True только если сверка с submissions прошла в транзакции записи отчёта.
    pin_verified: bool = False
