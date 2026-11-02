"""Снимок дельты поведения на сдаче (#2218).

На сдаче хаб фиксирует записи, поколение и head_sha. Дальше эта строка
читается как была: правка черновика и новый коммит её не переписывают.
Хеш записи — sha256 канонического блока. У removed блок берётся из индекса
на ``base_spec_revision``, а не из файла на head_sha: на вершине этого id
уже нет.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.db import deserialize_str_list
from hub.behavior_delta import BehaviorDeltaRecord
from hub.models import IndexedRequirement, SpecRequirement, SpecScenario
from hub.services.spec_format import requirement_revision
from hub.services.spec_index import index_state, indexed_requirements

_GATED_TYPES = frozenset({"task", "subtask"})
_COMPARE_KEYS = (
    "requirement_id",
    "op",
    "base_spec_revision",
    "new_requirement_id",
    "obligation",
    "scenarios",
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parse_delta(value: Any) -> tuple[str, list[BehaviorDeltaRecord]]:
    """``unspecified`` | ``delta`` | ``no_delta`` | ``records`` и сами записи."""
    raw = _as_record_list(value)
    if raw is None:
        return _mark(value), []
    if not raw:
        return "unspecified", []
    return "records", [BehaviorDeltaRecord.model_validate(item) for item in raw]


def requirement_ids_of(value: Any) -> list[str]:
    """Id из записей дельты, по порядку, без повторов. Колонка списка не читается."""
    kind, records = parse_delta(value)
    if kind != "records":
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for rec in records:
        for ident in (rec.requirement_id, rec.new_requirement_id):
            if ident and ident not in seen:
                seen.add(ident)
                ids.append(ident)
    return ids


async def prepare(db: aiosqlite.Connection, task: dict[str, Any], head_sha: str) -> str:
    """JSON снимка без поколения, либо ``""`` когда снимок не создаётся.

    «Не сказано» отказывает только внутри границы: task или subtask, у
    проекта уже был успешный индекс. Вне границы сдача идёт дальше и
    снимка нет. Явное ``no_delta`` — снимок без записей.
    """
    kind, records = parse_delta(task.get("behavior_delta"))
    if kind == "unspecified":
        if await _inside_gate(db, task):
            raise _unspecified()
        return ""
    if kind == "delta":
        return ""
    areas = deserialize_str_list(task.get("affected_areas"))
    if kind == "no_delta":
        return _payload(head_sha, [], True, areas)
    hashed = await _hashed_records(db, task, records)
    return _payload(head_sha, hashed, False, areas)


def bind_generation(payload: str, generation: int) -> str:
    """Поколение известно только после bump, поэтому дописывается здесь."""
    if not payload:
        return ""
    data = json.loads(payload)
    data["generation"] = generation
    return _canonical(data)


async def for_brief(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> dict[str, Any] | None:
    """Зафиксированный снимок поколения. ``areas`` — ключ сверки, не пункт брифа."""
    raw = await load_raw(db, task_id, generation)
    if not raw:
        return None
    data = json.loads(raw)
    data.pop("areas", None)
    return data


async def void_if_diverged(db: aiosqlite.Connection, task_id: int) -> None:
    """Черновик разошёлся со снимком текущего поколения — одобрение не завершает.

    Сама строка снимка не пишется. Совпадение черновика со снимком флаг снимает.
    """
    row = await repo.get_task(db, task_id)
    if row is None:
        return
    task = dict(row)
    generation = int(task.get("submission_generation") or 0)
    if generation <= 0:
        return
    raw = await load_raw(db, task_id, generation)
    flag = 1 if raw and _draft_token(task) != _frozen_token(raw) else 0
    if int(task.get("behavior_approval_void") or 0) == flag:
        return
    await repo.update_task(db, task_id, behavior_approval_void=flag)


async def refusal_for_moved_head(
    db: aiosqlite.Connection, task: dict[str, Any], current_tip: str, pr_num: Any
) -> str:
    """Новый head не переписывает снимок и не даёт слить сдачу по старому одобрению."""
    generation = int(task.get("submission_generation") or 0)
    if generation <= 0 or not current_tip:
        return ""
    raw = await load_raw(db, int(task["id"]), generation)
    if not raw:
        return ""
    head = str(json.loads(raw).get("head_sha") or "")
    if not head or head == current_tip:
        return ""
    return (
        f"stale_approval: снимок сдачи на {head[:12]}, ветка на "
        f"{current_tip[:12]} — снимок не меняется, одобрение недействительно, "
        f"слияние блокируется до новой сдачи (PR #{pr_num})."
    )


async def load_raw(db: aiosqlite.Connection, task_id: int, generation: int) -> str:
    cur = await db.execute(
        "SELECT behavior_snapshot FROM submissions "
        "WHERE task_id = ? AND generation = ?",
        (task_id, generation),
    )
    row = await cur.fetchone()
    if row is None:
        return ""
    return str(row["behavior_snapshot"] or "")


def _as_record_list(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip().startswith("["):
        return _json_list(value)
    return None


def _json_list(value: str) -> list[Any] | None:
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, list) else None


def _mark(value: Any) -> str:
    text = "" if value is None else str(getattr(value, "value", value)).strip()
    if text in ("", "unspecified"):
        return "unspecified"
    if text == "no_delta":
        return "no_delta"
    if text == "delta":
        return "delta"
    return "unspecified"


def _unspecified() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "reason": "behavior_unspecified",
            "message": (
                "behavior_unspecified: сдача внутри границы индекса не говорит, "
                "меняется ли поведение"
            ),
            "hint": "Укажите записи behavior_delta или явное no_delta",
            "actor_hint": "agent",
        },
    )


def _base_missing(requirement_id: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "reason": "behavior_delta_base_missing",
            "message": (
                "behavior_delta_base_missing: "
                f"{requirement_id} нет в индексе на base_spec_revision"
            ),
            "hint": "removed хешируется по блоку индекса, не по файлу на head_sha",
            "actor_hint": "agent",
        },
    )


async def _inside_gate(db: aiosqlite.Connection, task: dict[str, Any]) -> bool:
    if str(task.get("task_type") or "task") not in _GATED_TYPES:
        return False
    project = await repo.resolve_project_for_task(db, int(task["id"]))
    if project is None:
        return False
    return await index_state(db, int(project["id"])) is not None


def _payload(
    head_sha: str, records: list[dict[str, Any]], explicit: bool, areas: list[str]
) -> str:
    return _canonical(
        {
            "head_sha": head_sha,
            "records": records,
            "explicit_no_delta": explicit,
            "areas": areas,
        }
    )


async def _hashed_records(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    records: list[BehaviorDeltaRecord],
) -> list[dict[str, Any]]:
    project = await repo.resolve_project_for_task(db, int(task["id"]))
    indexed: dict[str, IndexedRequirement] = {}
    if project is not None:
        found = await indexed_requirements(db, int(project["id"]))
        indexed = {item.id: item for item in found}
    return [_with_hash(rec, indexed) for rec in records]


def _with_hash(
    rec: BehaviorDeltaRecord, indexed: dict[str, IndexedRequirement]
) -> dict[str, Any]:
    if rec.op == "removed":
        return _public_record(rec, _removed_hash(rec, indexed))
    ident = rec.new_requirement_id if rec.op == "renamed" else rec.requirement_id
    digest = requirement_revision(_spec_block(ident, rec))
    return _public_record(rec, digest)


def _removed_hash(
    rec: BehaviorDeltaRecord, indexed: dict[str, IndexedRequirement]
) -> str:
    block = indexed.get(rec.requirement_id)
    if block is None:
        raise _base_missing(rec.requirement_id)
    digest = requirement_revision(
        SpecRequirement(
            id=block.id, obligation=block.obligation, scenarios=block.scenarios
        )
    )
    if digest != rec.base_spec_revision:
        raise _base_missing(rec.requirement_id)
    return digest


def _spec_block(ident: str, rec: BehaviorDeltaRecord) -> SpecRequirement:
    return SpecRequirement(
        id=ident,
        obligation=rec.obligation,
        scenarios=[SpecScenario(**item.model_dump()) for item in rec.scenarios],
    )


def _public_record(rec: BehaviorDeltaRecord, digest: str) -> dict[str, Any]:
    return {
        "requirement_id": rec.requirement_id,
        "op": rec.op,
        "base_spec_revision": rec.base_spec_revision,
        "new_requirement_id": rec.new_requirement_id,
        "obligation": rec.obligation,
        "scenarios": [item.model_dump() for item in rec.scenarios],
        "content_hash": digest,
    }


def _draft_token(task: dict[str, Any]) -> str:
    kind, records = parse_delta(task.get("behavior_delta"))
    body = (
        [_record_view(rec.model_dump()) for rec in records] if kind == "records" else []
    )
    return _canonical(
        {
            "explicit_no_delta": kind == "no_delta",
            "areas": deserialize_str_list(task.get("affected_areas")),
            "records": body,
        }
    )


def _frozen_token(raw: str) -> str:
    data = json.loads(raw)
    records = [_record_view(item) for item in data.get("records") or []]
    return _canonical(
        {
            "explicit_no_delta": bool(data.get("explicit_no_delta")),
            "areas": data.get("areas") or [],
            "records": records,
        }
    )


def _record_view(rec: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {}
    for key in _COMPARE_KEYS:
        if key == "scenarios":
            view[key] = rec.get(key) or []
        else:
            view[key] = rec.get(key) or ""
    return view
