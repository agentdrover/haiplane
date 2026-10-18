"""Проектный контракт CI (#1848): по какому workflow и каким проверкам хаб подтверждает отчёт.

До этой задачи доверие к отчёту CI знало один контракт — хаба: workflow
``.github/workflows/ci.yml`` и четыре проверки lint/format/types/tests. Спутник
(Memo) гоняет свой ``tests.yml`` с одной проверкой ``tests``: его отчёт честен,
но по контракту хаба навсегда ``unverified(wrong_workflow)``.

Контракт — ключ ``ci_contract`` политики проекта ``{workflow_path,
required_checks}``. Его пишет только человек (PATCH проекта; отложенная правка и
проект default отказывают). Без ключа действует контракт хаба — поведение
до #1848 не меняется.

Отпечаток контракта (sha256 канонического JSON) — основание доверия: вердикт
проверки попытки хранит отпечаток, под которым он вынесен, и значит только
пока отпечаток совпадает с текущим отпечатком проекта. Смена контракта не
стирает строки, но делает их «требует повторной проверки».

Модуль чистый до раздела «база»: его зовёт и валидатор модели, и читатели.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CI_CONTRACT_KEY = "ci_contract"
#: Закрытый словарь имён проверок (тот же, что у отчёта хаба). Порядок — порядок
#: показа в сообщении «проверки не pass».
CHECK_VOCABULARY: tuple[str, ...] = ("lint", "format", "types", "tests")
#: Только файл прямо в каталоге workflow: подкаталог и ``..`` не пускаем.
_WORKFLOW_RE = re.compile(r"^\.github/workflows/[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml$")
_FIELDS = frozenset({"workflow_path", "required_checks"})
#: Проект default (репозиторий самого хаба): контракт хаба не ослабляется.
HUB_SLUG = "default"
#: Отпечаток отсутствует: претензия ещё не проверена (или контракт неизвестен).
NO_FINGERPRINT = ""


@dataclass(frozen=True)
class CiContract:
    """Workflow и обязательные проверки, по которым подтверждается отчёт проекта."""

    workflow_path: str
    required_checks: tuple[str, ...]

    @property
    def fingerprint(self) -> str:
        return fingerprint_of(self.workflow_path, self.required_checks)

    def ordered_checks(self) -> tuple[str, ...]:
        """Обязательные проверки в порядке словаря (так их называет сообщение)."""
        return tuple(n for n in CHECK_VOCABULARY if n in self.required_checks)


def fingerprint_of(workflow_path: str, required_checks: Any) -> str:
    """sha256 канонического JSON ``{workflow_path, required_checks (sorted)}``."""
    canonical = json.dumps(
        {"workflow_path": workflow_path, "required_checks": sorted(required_checks)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


HUB_CONTRACT = CiContract(".github/workflows/ci.yml", tuple(sorted(CHECK_VOCABULARY)))
HUB_FINGERPRINT = HUB_CONTRACT.fingerprint


def validated_ci_contract(value: Any) -> dict[str, Any]:
    """Привести ``ci_contract`` к каноническому виду или отказать (``ValueError``)."""
    if not isinstance(value, Mapping):
        raise ValueError(
            "gate_policy ci_contract must be an object "
            "{workflow_path, required_checks}, got: " + repr(value)[:80]
        )
    extra = set(value) - _FIELDS
    if extra:
        raise ValueError(
            f"gate_policy ci_contract has unknown fields: {sorted(extra)}; "
            f"allowed: {', '.join(sorted(_FIELDS))}"
        )
    path = value.get("workflow_path")
    if (
        not isinstance(path, str)
        or not _WORKFLOW_RE.match(path)
        or ".." in path.split("/")
    ):
        raise ValueError(
            "gate_policy ci_contract.workflow_path must be a file of "
            f".github/workflows/ (*.yml or *.yaml), got: {path!r}"
        )
    checks = value.get("required_checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError(
            "gate_policy ci_contract.required_checks must be a non-empty list "
            f"of {', '.join(CHECK_VOCABULARY)}"
        )
    bad = [c for c in checks if not isinstance(c, str) or c not in CHECK_VOCABULARY]
    if bad:
        raise ValueError(
            f"gate_policy ci_contract.required_checks names {bad!r}; "
            f"allowed: {', '.join(CHECK_VOCABULARY)}"
        )
    if len(set(checks)) != len(checks):
        raise ValueError("gate_policy ci_contract.required_checks names a check twice")
    return {"workflow_path": path, "required_checks": sorted(checks)}


def contract_from_raw(raw: Any, slug: str = "") -> CiContract:
    """Контракт проекта из сохранённого значения ключа; хаба — когда его нет.

    Читает значение ключа ``project_policy.ci_contract_from`` (единственный
    читатель политики). Нечитаемое значение (положено мимо API) читается как
    контракт хаба: он строже любого допустимого, и отчёт спутника при этом
    просто не подтвердится. Проект default всегда на контракте хаба, что бы ни
    лежало в его политике.
    """
    if slug == HUB_SLUG or raw is None:
        return HUB_CONTRACT
    try:
        ok = validated_ci_contract(raw)
    except ValueError:
        return HUB_CONTRACT
    return CiContract(ok["workflow_path"], tuple(ok["required_checks"]))


def is_hub(contract: CiContract) -> bool:
    return contract.fingerprint == HUB_FINGERPRINT


def path_matches(contract: CiContract, path: str) -> bool:
    """Путь прогона — workflow контракта (суффикс ``@<ref>`` документированно отбрасывается)."""
    return path.split("@", 1)[0] == contract.workflow_path


# ---- база --------------------------------------------------------------------


async def contract_for_task(db: Any, task_id: int) -> CiContract:
    """Контракт проекта задачи; контракт хаба, если проект не определился."""
    from hub import repository as repo
    from hub.services import project_policy

    try:
        project = await repo.resolve_project_for_task(db, task_id)
    except Exception:  # noqa: BLE001 - нечитаемое = самый строгий контракт
        return HUB_CONTRACT
    if project is None:
        return HUB_CONTRACT
    return project_policy.ci_contract_of(project)


async def fingerprint_for_task(db: Any, task_id: int) -> str:
    return (await contract_for_task(db, task_id)).fingerprint


def source_attempt(source: str) -> tuple[str, str] | None:
    """``(run_id, run_attempt)`` из источника ``ci:<run>:<attempt>``; иначе None."""
    parts = (source or "").split(":")
    if len(parts) != 3 or parts[0] != "ci":
        return None
    return parts[1], parts[2]


async def evidence_stale(
    db: Any, task_id: int, source: str, current_fp: str | None = None
) -> bool:
    """Доказательство CI (AC-результат, validation) выдано под контрактом, которого уже нет.

    ``source`` — ``ci:<run>:<attempt>``. Попытка с другим отпечатком (или без
    него) под текущим контрактом ничего не доказывает. Ручные источники и
    ``legacy`` — не из CI, их это правило не касается.
    """
    from hub.db import fetchall

    key = source_attempt(source)
    if key is None:
        return False
    rows = await fetchall(
        db,
        "SELECT contract_fp FROM ci_report_attempts "
        "WHERE task_id=? AND run_id=? AND run_attempt=? LIMIT 1",
        (task_id, key[0], key[1]),
    )
    if not rows:
        return False
    if current_fp is None:
        current_fp = await fingerprint_for_task(db, task_id)
    return str(rows[0][0] or "") != current_fp


async def drop_stale_results(db: Any, task_id: int, rows: Any) -> list[Any]:
    """The recorded results whose source is not a stale CI attempt (#1848)."""
    fp = await fingerprint_for_task(db, task_id)
    return [
        r
        for r in rows
        if not await evidence_stale(db, task_id, str(r["source"] or ""), fp)
    ]


async def attempts_to_recheck(db: Any, rows: list[Any]) -> list[Any]:
    """Из окончательно проверенных попыток те, чей отпечаток не равен текущему проекта."""
    seen: dict[int, str] = {}
    out = []
    for row in rows:
        task_id = int(row["task_id"])
        if task_id not in seen:
            seen[task_id] = await fingerprint_for_task(db, task_id)
        if str(row["contract_fp"] or "") != seen[task_id]:
            out.append(row)
    return out


async def settled_attempts(db: Any) -> list[Any]:
    """Окончательные попытки задач в работе: кандидаты на перепроверку после смены контракта."""
    from hub.db import fetchall

    return await fetchall(
        db,
        "SELECT a.* FROM ci_report_attempts a JOIN tasks t ON t.id=a.task_id "
        "WHERE a.trust_final=1 AND t.archived=0 "
        "AND t.status NOT IN ('completed','failed','rejected') "
        "ORDER BY a.id",
    )
