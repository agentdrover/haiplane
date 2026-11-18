"""Active library snapshot for a task implementer's slot order."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Final

import aiosqlite

from hub.db import fetchall

SKILL_NAMES: Final = (
    "executor-pair-discipline",
    "hub-operator",
    "hub-review-cycle",
    "hub-submit-task",
    "hub-review-report-reading",
    "mcp-write-resilience",
)
PAIR_CODE_PLACEHOLDER: Final = "{{PAIR_CODE}}"
MAX_BUNDLE_BYTES: Final = 128 * 1024
_MARKER_PREFIX: Final = "<<<HAIPLANE_SKILL_"

TASK_IMPLEMENTER_RULES: Final = """Маршрут этого заказа — task implementer (исполнитель задачи).
После redeem прочитай hub-operator. При сдаче и доработке используй hub-review-cycle.
Все шесть обязательных текстов уже вложены: ссылки hub_get_skill на них означают
прочитать соответствующий блок ниже. Не запрашивай /api/skills.

Без MCP пользуйся сессией из redeem и разрешёнными HTTP-маршрутами:
- POST /api/sessions/register и POST /api/sessions/{session_id}/heartbeat;
- GET /api/tasks/{task_id}/context для рабочих правил, GET /api/tasks/{task_id}
  для статуса, GET /api/tasks/{task_id}/review-brief для результатов своей задачи;
- POST /api/tasks/{task_id}/claim и POST /api/tasks/{task_id}/pair-start;
- POST /api/tasks/{task_id}/submit-review или POST /api/tasks/{task_id}/question.

Действуют актуальные working_rules и политика проекта. Маршруты местной
разработки Hub, независимого ревьюера и решения человека к этому заказу
не применяются. После принятой сдачи заверши прогон. Ожидание и продолжение
обслуживает Hub; результаты доступны следующему запуску."""


@dataclass(frozen=True)
class SkillBundle:
    text: str
    discipline_version: int


class SkillBundleError(Exception):
    """A safe refusal to return in the existing slot launch result."""


async def read_active_bundle(db: aiosqlite.Connection) -> SkillBundle:
    """Read all six versions in one SQLite snapshot inside the issue transaction."""
    try:
        rows = await fetchall(
            db,
            "SELECT name, content, version FROM skills WHERE (name, version) IN ("
            "SELECT name, MAX(version) FROM skills WHERE status = 'active' "
            "AND name IN (?, ?, ?, ?, ?, ?) GROUP BY name)",
            SKILL_NAMES,
        )
    except aiosqlite.Error as exc:
        raise SkillBundleError(
            "executor_skill_bundle_unreadable: не удалось прочитать активные навыки"
        ) from exc
    by_name = {row["name"]: row for row in rows}
    missing = [name for name in SKILL_NAMES if name not in by_name]
    if missing:
        raise SkillBundleError(
            f"executor_skill_bundle_missing: нет активных версий {', '.join(missing)}"
        )

    sections = [_skill_section(name, by_name[name]) for name in SKILL_NAMES]
    text = "\n".join(
        [
            f"{_MARKER_PREFIX}BUNDLE_BEGIN>>>",
            *sections,
            f"{_MARKER_PREFIX}BUNDLE_END>>>",
        ]
    )
    if len(text.encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise SkillBundleError(
            f"executor_skill_bundle_oversize: пакет навыков больше {MAX_BUNDLE_BYTES} байт"
        )
    return SkillBundle(text, int(by_name[SKILL_NAMES[0]]["version"]))


def _skill_section(name: str, row: aiosqlite.Row) -> str:
    body = row["content"]
    if not isinstance(body, str) or not body.strip():
        raise SkillBundleError(f"executor_skill_bundle_invalid: пустой текст {name}")
    if any(marker in body for marker in ("\0", PAIR_CODE_PLACEHOLDER, _MARKER_PREFIX)):
        raise SkillBundleError(
            f"executor_skill_bundle_invalid: зарезервированный маркер в {name}"
        )
    digest = sha256(body.encode("utf-8")).hexdigest()
    return (
        f"{_MARKER_PREFIX}BEGIN slug={name} version={row['version']} sha256={digest}>>>\n"
        f"{body}\n{_MARKER_PREFIX}END>>>"
    )
