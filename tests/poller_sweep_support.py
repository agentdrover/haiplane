"""Оснастка и реестр многопроходных сценариев свипов поллера (#1859, мера Д1).

Класс дефектов «поллер: повтор события или заказ раньше условия» (#1289, #1290,
#1330, #1600) жил в одном свипе, ``steward_runs``. Каждый из них проявлялся не
на первом проходе, а на втором-пятом: первый проход честно пишет событие, а
второй обязан промолчать. Тест одного прохода такого не видит.

Здесь два предмета.

1. Оснастка ``run_passes``: N проходов настоящего ``Sweep`` из ``SWEEPS`` при
   неизменных внешних условиях (записи самого свипа между проходами
   сохраняются), с зафиксированными часами хаба и раздельным учётом четырёх
   каналов: события, уведомления в ленте задач, строки заказов и вызовы
   провайдера. Ожидания пишутся по сценарию и каналу, а не «ровно одно событие».
2. Реестр ``SWEEP_COVERAGE``: свип из ``SWEEPS`` → ссылки на сценарии либо
   мотивированное исключение с ``removed_by`` (формат ledger бюджета сложности).
   Проверка ``check_registry`` красит CI, если у свипа нет записи, запись
   ссылается на несуществующий тест или записан свип, которого больше нет.

Часы не спят и порогов по стенным часам нет: границу UTC-суток двигает
``HubClock``, а не ожидание.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import aiosqlite

from hub import config
from hub import repository as repo
from hub.db import fetchall

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Часы
# ---------------------------------------------------------------------------


class HubClock:
    """Часы хаба для границы UTC-суток: ``steward_cap.utcnow`` под контролем теста."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def set(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment


def freeze_hub_clock(monkeypatch: Any, moment: datetime) -> HubClock:
    """Подменить часы хаба; суточный потолок и метки заказов идут по ним."""
    from hub.services import steward_cap

    clock = HubClock(moment)
    monkeypatch.setattr(steward_cap, "utcnow", clock.now)
    return clock


def utc(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Каналы
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Channels:
    """Что появилось за период, раздельно по четырём каналам."""

    events: Counter = field(default_factory=Counter)  # вид события -> сколько
    notifications: int = 0  # записи в ленте задач (карточки, alert)
    orders: int = 0  # строки заказов steward_runs
    provider_calls: int = 0  # вызовы провайдера (создание облачного агента)

    def event(self, kind: str) -> int:
        return int(self.events.get(kind, 0))

    def __sub__(self, other: Channels) -> Channels:
        diff = Counter(self.events)
        diff.subtract(other.events)
        return Channels(
            +diff,
            self.notifications - other.notifications,
            self.orders - other.orders,
            self.provider_calls - other.provider_calls,
        )

    def __add__(self, other: Channels) -> Channels:
        return Channels(
            self.events + other.events,
            self.notifications + other.notifications,
            self.orders + other.orders,
            self.provider_calls + other.provider_calls,
        )


async def snapshot(db: aiosqlite.Connection, provider: AsyncMock | None) -> Channels:
    rows = await fetchall(db, "SELECT kind, COUNT(*) AS n FROM events GROUP BY kind")
    events = Counter({dict(r)["kind"]: int(dict(r)["n"]) for r in rows})
    updates = await fetchall(db, "SELECT COUNT(*) AS n FROM task_updates")
    orders = await fetchall(db, "SELECT COUNT(*) AS n FROM steward_runs")
    return Channels(
        events,
        int(dict(updates[0])["n"]),
        int(dict(orders[0])["n"]),
        provider.await_count if provider is not None else 0,
    )


@dataclass(frozen=True)
class PassLog:
    """Приращения по проходам и их сумма."""

    passes: tuple[Channels, ...]

    @property
    def total(self) -> Channels:
        out = Channels()
        for one in self.passes:
            out = out + one
        return out

    @property
    def after_first(self) -> Channels:
        """Всё, что появилось на втором и последующих проходах: повторы."""
        out = Channels()
        for one in self.passes[1:]:
            out = out + one
        return out


class _SweepErrorCatcher(logging.Handler):
    """Свип глотает исключение и пишет его в журнал; тест обязан это заметить."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def provider_probe(
    answer: Any = None,
) -> Iterator[AsyncMock]:
    """Провайдер облачных агентов: считает вызовы, по умолчанию агент создан."""
    created = {"agent": {"id": "agent-1"}, "run": {"id": "run-1"}}
    mock = AsyncMock(return_value=answer if answer is not None else (created, None))
    with patch("hub.integrations.cursor_cloud.create_agent_attempt", new=mock):
        yield mock


@contextmanager
def frozen_sql_date() -> Iterator[str]:
    """Дедуп steward_shadow видит ``date('now')`` как день, снятый при входе.

    Дедуп отказов отсекает прошлые сутки по ``date('now')`` SQLite, а не по
    часам хаба; тест, перешагнувший реальную полночь UTC между двумя вызовами,
    видел бы «вчерашний» отказ. SQLite не даёт подменить встроенную функцию,
    поэтому подменяется чтение запросов в steward_shadow: литерал заменяется
    на дату, снятую один раз. Код хаба не меняется.
    """
    from hub.services import steward_shadow

    day = datetime.now(UTC).strftime("%Y-%m-%d")
    original = steward_shadow.fetchall

    async def _fetchall(db: Any, sql: str, parameters: Any = ()) -> Any:
        return await original(db, sql.replace("date('now')", f"'{day}'"), parameters)

    with patch.object(steward_shadow, "fetchall", _fetchall):
        yield day


def sweep_named(name: str):
    from hub.poller import SWEEPS

    for sweep in SWEEPS:
        if sweep.name == name:
            return sweep
    raise AssertionError(f"в SWEEPS нет свипа {name!r}")


async def run_passes(
    db: aiosqlite.Connection,
    sweep_name: str,
    passes: int,
    provider: AsyncMock | None = None,
) -> PassLog:
    """N проходов именного свипа; внешние условия между проходами не трогаются."""
    sweep = sweep_named(sweep_name)
    catcher = _SweepErrorCatcher()
    root = logging.getLogger()
    root.addHandler(catcher)
    deltas: list[Channels] = []
    try:
        for _ in range(passes):
            before = await snapshot(db, provider)
            await sweep.run(db)
            deltas.append(await snapshot(db, provider) - before)
    finally:
        root.removeHandler(catcher)
    if catcher.records:
        raise AssertionError(
            f"свип {sweep_name} упал внутри прохода: "
            + "; ".join(r.getMessage() for r in catcher.records)
        )
    return PassLog(tuple(deltas))


def expect(
    label: str,
    actual: Channels,
    *,
    events: dict[str, int] | None = None,
    ignore_events: frozenset[str] = frozenset(),
    notifications: int = 0,
    orders: int = 0,
    provider_calls: int = 0,
) -> None:
    """Количества по каналам. События сравниваются ПОЛНЫМ набором видов: любой
    вид, которого нет в ``events``, обязан быть нулём. Фоновые виды, которые
    не есть предмет сценария, называются явно в ``ignore_events``."""
    want = Counter({k: v for k, v in (events or {}).items() if v})
    got = Counter({k: v for k, v in actual.events.items() if k not in ignore_events})
    assert got == want, (
        f"{label}: события {dict(sorted(got.items()))}, ожидалось {dict(sorted(want.items()))}"
    )
    assert actual.notifications == notifications, (
        f"{label}: записей в ленте {actual.notifications}, ожидалось {notifications}"
    )
    assert actual.orders == orders, (
        f"{label}: строк заказов {actual.orders}, ожидалось {orders}"
    )
    assert actual.provider_calls == provider_calls, (
        f"{label}: вызовов провайдера {actual.provider_calls}, "
        f"ожидалось {provider_calls}"
    )


# ---------------------------------------------------------------------------
# Данные сценариев стюарда
# ---------------------------------------------------------------------------

_DELIVERY = "код доступа: ABC-123"


def configure_steward_env(monkeypatch: Any) -> None:
    """Контур стюарда в тени, провайдер и канал доставки готовы к старту."""
    from hub.services import steward_ci_wait
    from hub.services import steward_shadow as sh

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 20)
    monkeypatch.setattr(config, "STEWARD_ADVISOR_DAILY_CAP", "")
    monkeypatch.setattr(config, "STEWARD_RUN_DEADLINE_MIN", 30)
    monkeypatch.setattr(config, "STEWARD_MODEL", "gpt-5.3-codex")
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", "steward-token")
    monkeypatch.setattr(config, "CURSOR_API_KEY", "cursor-key")
    # Потолок ожидания отчёта с запасом: сценарии не про него, а про ожидание.
    monkeypatch.setattr(config, "STEWARD_REVIEW_WAIT_MAX", 100_000)

    async def _delivery(_db, _task_id, _generation, _base_url, **_kw):
        return _DELIVERY

    async def _not_waiting(_db, _task):
        return steward_ci_wait.NOT_WAITING

    monkeypatch.setattr(sh, "identity_delivery", _delivery)
    monkeypatch.setattr(steward_ci_wait, "ci_wait_view", _not_waiting)


async def make_project(db: aiosqlite.Connection, slug: str) -> int:
    """Проект с verdict=steward и репозиторием: судья может дойти до провайдера."""
    project_id = await repo.create_project(
        db, slug=slug, name=slug, workspace_path="", status="active"
    )
    await db.execute(
        "UPDATE projects SET gate_policy=?, repo=? WHERE id=?",
        (json.dumps({"verdict": "steward"}), "agentdrover/haiplane", project_id),
    )
    await db.commit()
    return project_id


async def submit_task(
    db: aiosqlite.Connection, project_id: int, *, generation: int = 1
) -> int:
    """Сдача в review: ветка есть (диспетчер ревью заказать МОЖЕТ), модель названа."""
    task_id = await repo.create_task(
        db,
        title="сдача на суд",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="review",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(
        db,
        task_id,
        project_id=project_id,
        submission_generation=generation,
        submission_sha="a" * 40,
        submission_model="claude-opus-5",
        branch=f"task-{task_id}/work",
    )
    await db.commit()
    return task_id


async def review_dispatch(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    generation: int = 1,
    status: str = "active",
    channel: str = "cloud",
    model: str = "grok-4.6",
) -> None:
    """Строка заказа кросс-модельного ревью: active — ревью идёт, done — кончено."""
    await db.execute(
        "INSERT INTO review_dispatches "
        "(task_id, submission_generation, agent_id, run_id, model, status, channel) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, generation, "rev-agent", "run-7", model, status, channel),
    )
    await db.commit()


async def review_settled(
    db: aiosqlite.Connection, task_id: int, *, generation: int = 1
) -> None:
    """Ревью кончено, отчёт лёг: судье есть что читать."""
    await db.execute(
        "UPDATE review_dispatches SET status='done', channel='local' "
        "WHERE task_id=? AND submission_generation=?",
        (task_id, generation),
    )
    await db.execute(
        "INSERT INTO machine_reviews "
        "(task_id, submission_generation, model, submitted_by) "
        "VALUES (?, ?, ?, ?)",
        (task_id, generation, "grok-4.6", "rev-agent"),
    )
    await db.commit()


async def runs_of(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    rows = await fetchall(
        db, "SELECT * FROM steward_runs WHERE task_id=? ORDER BY id", (task_id,)
    )
    return [dict(r) for r in rows]


async def retire_scenario(db: aiosqlite.Connection) -> None:
    """Сценарий кончен: его задачи и открытые заказы не участвуют в следующем.

    Сценарии идут в одной базе; без уборки открытый заказ прошлого сценария
    получал бы повторы старта на проходах следующего и портил его счёт.
    """
    await db.execute("UPDATE tasks SET status='open' WHERE status='review'")
    await db.execute("UPDATE steward_runs SET status='superseded' WHERE status='open'")
    await db.commit()


# ---------------------------------------------------------------------------
# Реестр покрытия свипов
# ---------------------------------------------------------------------------

STEWARD_RUNS_SCENARIOS = (
    "tests/test_steward_runs_scenarios.py::test_steward_runs_counts_per_channel",
)

_START = "unclaimed — "
_NONE_YET = (
    _START + "многопроходного сценария нет: мера Д1 (#1859) начинает со свипов "
    "с историей дефектов; свип без дефектов в разборе 19.10 не покрывался"
)


@dataclass(frozen=True)
class Coverage:
    """Запись реестра: сценарии-ссылки ЛИБО исключение с ``removed_by``."""

    scenarios: tuple[str, ...] = ()
    removed_by: str = ""


#: Ключи — имена из ``hub.poller.SWEEPS``; перечень свипов копией не ведётся:
#: мета-тест сверяет этот словарь с настоящим ``SWEEPS`` в обе стороны.
SWEEP_COVERAGE: dict[str, Coverage] = {
    "policy_schedule": Coverage(removed_by=_NONE_YET),
    "running_dispatch": Coverage(removed_by=_NONE_YET),
    "ci_trust": Coverage(removed_by=_NONE_YET),
    "gate_observations": Coverage(removed_by=_NONE_YET),
    "ci_head_run": Coverage(removed_by=_NONE_YET),
    "review": Coverage(removed_by=_NONE_YET),
    "pair_delivery": Coverage(removed_by=_NONE_YET),
    "parent_rollup": Coverage(removed_by=_NONE_YET),
    "ci_check": Coverage(removed_by=_NONE_YET),
    "stale_running": Coverage(removed_by=_NONE_YET),
    "stale_statuses": Coverage(removed_by=_NONE_YET),
    "unrefined_drafts": Coverage(removed_by=_NONE_YET),
    "human_queue": Coverage(removed_by=_NONE_YET),
    "unjudged_findings": Coverage(removed_by=_NONE_YET),
    "reviewer_unavailable": Coverage(
        scenarios=(
            "tests/test_poller.py::"
            "test_reviewer_unavailable_is_one_hub_event_not_a_card_each",
        )
    ),
    "autopilot_digests": Coverage(removed_by=_NONE_YET),
    "delivery_discrepancies": Coverage(removed_by=_NONE_YET),
    "review_dispatches": Coverage(removed_by=_NONE_YET),
    "steward_runs": Coverage(scenarios=STEWARD_RUNS_SCENARIOS),
    "executor_runs": Coverage(removed_by=_NONE_YET),
    "executor_slots": Coverage(removed_by=_NONE_YET),
    "executor_orders": Coverage(removed_by=_NONE_YET),
    "rework_launches": Coverage(
        scenarios=(
            "tests/test_rework_dispatch_once.py::"
            "test_a_crash_after_acceptance_links_the_existing_job",
        )
    ),
    "critic_orders": Coverage(
        scenarios=(
            "tests/test_critic_orders_moment.py::test_the_critic_orders_sweep_is_idempotent",
        )
    ),
    "expired_claims": Coverage(removed_by=_NONE_YET),
    "machine_deadlines": Coverage(removed_by=_NONE_YET),
    "stale_arbiter": Coverage(removed_by=_NONE_YET),
    "owner_minutes": Coverage(removed_by=_NONE_YET),
    "role_orders": Coverage(
        scenarios=(
            "tests/test_role_orders.py::test_one_role_order_per_needs_decision_event",
        )
    ),
    "events_retention": Coverage(removed_by=_NONE_YET),
    "stale_worktrees": Coverage(removed_by=_NONE_YET),
    "sessions_retention": Coverage(removed_by=_NONE_YET),
    "release_policy": Coverage(removed_by=_NONE_YET),
    "release_deploy_watch": Coverage(
        scenarios=(
            "tests/test_release_deploy_watch.py::"
            "test_a_release_that_did_not_reach_prod_is_reported_once",
        )
    ),
    "orchestrator_queue": Coverage(removed_by=_NONE_YET),
    "messages_retention": Coverage(removed_by=_NONE_YET),
    "mcp_retention": Coverage(removed_by=_NONE_YET),
}


def validate_removed_by(text: object) -> str | None:
    """Ошибка формата ``removed_by`` или ``None``. Формат — как у complexity_budget:
    ``task:#N``, ``permanent — <причина>``, ``unclaimed — <причина>``."""
    raw = str(text or "").strip()
    kind, _, reason = (part.strip() for part in raw.partition("—"))
    if not raw:
        return "пусто"
    if kind.startswith("task:#"):
        return None if kind[6:].isdigit() else "task без номера"
    if kind in ("permanent", "unclaimed"):
        return None if reason else f"{kind} без причины"
    return f"неизвестный формат {raw!r}"


_COLLECTED: dict[tuple[str, str], frozenset[str]] = {}


def collected_tests(path_text: str, root: Path = REPO_ROOT) -> frozenset[str]:
    """Имена функций, которые pytest СОБИРАЕТ как тесты в файле (без параметров).

    Сбор настоящий (`--collect-only` в отдельном процессе), а не разбор имён:
    вспомогательная функция того же файла тестом не является. Результат
    кэшируется на файл: сбор test_poller.py стоит секунд.
    """
    key = (str(root), path_text)
    if key in _COLLECTED:
        return _COLLECTED[key]
    names: set[str] = set()
    if (root / path_text).is_file():
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                path_text,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        for line in run.stdout.splitlines():
            head, sep, name = line.strip().partition("::")
            if sep and head == path_text and "::" not in name:
                names.add(name.split("[", 1)[0])
    _COLLECTED[key] = frozenset(names)
    return _COLLECTED[key]


def scenario_exists(ref: str, root: Path = REPO_ROOT) -> bool:
    """Собирает ли pytest тест по ссылке ``путь::имя``."""
    path_text, sep, name = ref.partition("::")
    return bool(sep and name) and name in collected_tests(path_text, root)


def check_registry(
    sweep_names: list[str],
    registry: dict[str, Coverage],
    root: Path = REPO_ROOT,
) -> list[str]:
    """Нарушения реестра: пустой список — реестр сходится с ``SWEEPS``."""
    problems: list[str] = []
    for name in sweep_names:
        if name not in registry:
            problems.append(
                f"свип {name!r} не внесён в SWEEP_COVERAGE: добавьте сценарии "
                "или исключение с removed_by"
            )
    for name, entry in registry.items():
        if name not in sweep_names:
            problems.append(f"{name!r}: свипа больше нет в SWEEPS — удалите строку")
            continue
        if entry.scenarios and entry.removed_by:
            problems.append(f"{name!r}: сценарии и исключение вместе — выберите одно")
        if not entry.scenarios:
            error = validate_removed_by(entry.removed_by)
            if error:
                problems.append(f"{name!r}: removed_by не по формату — {error}")
        for ref in entry.scenarios:
            if not scenario_exists(ref, root):
                problems.append(f"{name!r}: сценария {ref} нет")
    return problems
