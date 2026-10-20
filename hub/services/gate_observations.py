"""Наблюдения хаба для путей показа (#1913).

Карточка перерисовывается каждые 15 с, бриф и hub_task_status читают постоянно.
Показ не ходит в сеть: он говорит то, что хаб УЖЕ видел, и называет возраст
этого наблюдения. Здесь лежат два наблюдения, у которых раньше не было своего
места: разойдётся ли PR с базой (``check_pr_mergeable``) и стоит ли ветка на
чужих коммитах (стыковка). Вершину ветки помнит ``lifecycle._observed_tips``,
стояние CI — база (его обновляет проход поллера ``ci_trust.sweep``).

Память живёт в процессе, как и у вершины: рестарт её забывает, и показ тогда
отвечает «не сверено», а не помнит за хаб то, чего тот не видел. Пишут сюда
явные операции (бриф стюарда, сдача) и проход поллера ``sweep`` — показ только
читает. Старше ``MAX_AGE_MINUTES`` наблюдение тоже «не сверено»: час назад
чистый мерж ничего не говорит о нынешней базе.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

#: Как давно наблюдение ещё можно назвать; старше — «не сверено».
MAX_AGE_MINUTES = 30

BASE_MERGE = "base_merge"
STACKING = "stacking"


@dataclass(frozen=True)
class Observation:
    """Что хаб видел: значение и возраст в минутах на момент чтения."""

    value: dict[str, Any]
    age_minutes: int


_seen: dict[tuple[str, int, str], tuple[dict[str, Any], datetime]] = {}


def record(kind: str, task_id: int, key: str, value: dict[str, Any]) -> None:
    """Запомнить наблюдение. Зовут только операции, которые САМИ ходили в сеть."""
    _seen[(kind, int(task_id), str(key))] = (dict(value), datetime.now(UTC))


def recall(kind: str, task_id: int, key: str) -> Observation | None:
    """Последнее наблюдение или ``None`` — не видели вовсе либо оно устарело."""
    seen = _seen.get((kind, int(task_id), str(key)))
    if seen is None:
        return None
    value, at = seen
    age = max(0, int((datetime.now(UTC) - at).total_seconds() // 60))
    if age > MAX_AGE_MINUTES:
        return None
    return Observation(value=value, age_minutes=age)


def forget_all() -> None:
    """Забыть всё (тесты; рестарт делает то же)."""
    _seen.clear()


def stacking_key(branch: str, sha: str) -> str:
    """Стыковка относится к ветке на сданном коммите, а не к имени ветки вообще."""
    return f"{branch}@{sha}"


def age_phrase(minutes: int) -> str:
    """Возраст словами для причины: «менее минуты назад» или «N мин назад»."""
    return "менее минуты назад" if minutes < 1 else f"{minutes} мин назад"


#: Как часто проход поллера освежает наблюдения одной задачи, и сколько задач за тик.
REFRESH_EVERY_MINUTES = 5
MAX_TASKS_PER_TICK = 3
TASK_DEADLINE_SECONDS = 45

_refreshed_at: dict[int, datetime] = {}


async def run_sweep(db: Any) -> None:
    """``sweep`` для тика поллера: сбой одного прохода не роняет остальные."""
    try:
        await sweep(db)
    except Exception:  # noqa: BLE001 - the sweep must not kill the loop
        log.exception("gate observations sweep failed")


async def _gate_tasks(db: Any) -> list[int]:
    """Задачи на гейте с веткой и закреплённым коммитом: их карточки читают люди."""
    from hub.db import fetchall

    rows = await fetchall(
        db,
        "SELECT id FROM tasks WHERE status IN ('review', 'fix_requested') "
        "AND COALESCE(branch, '') != '' AND COALESCE(submission_sha, '') != '' "
        "ORDER BY id",
    )
    return [int(r["id"]) for r in rows]


async def sweep(db: Any) -> None:
    """Проход поллера (#1913): освежить наблюдения задач на гейте, раз в 5 минут.

    Показ в сеть не ходит и потому видит только это. Здесь сеть разрешена:
    вершина ветки, расхождение PR с базой и стыковка — те же вызовы, что у
    гейтов. Бюджет мал и честен: не больше ``MAX_TASKS_PER_TICK`` задач за тик,
    у каждой не чаще ``REFRESH_EVERY_MINUTES`` и не дольше ``TASK_DEADLINE_SECONDS``;
    пока GitHub недавно не отвечал, проход не спрашивает его вовсе. Стояние CI
    освежает ``ci_trust.sweep`` — его здесь не дублируем.
    """
    from hub import repository as repo
    from hub import services
    from hub.services import ci_trust, lifecycle
    from hub.services.review_brief import base_merge_section

    if ci_trust.forge_recently_down():
        return
    now = datetime.now(UTC)
    ids = await _gate_tasks(db)
    for stale in set(_refreshed_at) - set(ids):
        del _refreshed_at[stale]
    done = 0
    for task_id in ids:
        last = _refreshed_at.get(task_id)
        if (
            last is not None
            and (now - last).total_seconds() < REFRESH_EVERY_MINUTES * 60
        ):
            continue
        if done >= MAX_TASKS_PER_TICK:
            return
        done += 1
        _refreshed_at[task_id] = now
        task_row = await repo.get_task(db, task_id)
        if task_row is None:
            continue
        view = services.row_to_task(task_row)
        branch = (view.branch or "").strip()
        try:
            async with asyncio.timeout(TASK_DEADLINE_SECONDS):
                await lifecycle.resolve_branch_tip(db, task_id, branch)
                await base_merge_section(db, view)
                await services.detect_branch_stacking(db, task_id, branch)
        except Exception:  # noqa: BLE001 - наблюдение не должно ронять проход
            log.exception("gate observation of task %s failed", task_id)
