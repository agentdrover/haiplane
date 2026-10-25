"""Бронь доработки #1991: один процесс на базу и отмена на COMMIT брони.

Восстановление по boot_id верно, пока на одной базе живёт один процесс хаба.
Второй экземпляр не должен ни сверять брони, ни заказывать доработку.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import select
import signal
import threading
import time
from typing import Any

import pytest
from fastapi import HTTPException

from hub import config
from hub import repository as repo
from hub import services
from hub.integrations.registry import plugins
from hub.models import TaskDecide
from tests.test_decision_brief import _task_in
from tests.test_rework_dispatch_once import REWORK, _crash_on_submit


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    path = tmp_path / "hub.db"
    monkeypatch.setattr(config, "HUB_DB_PATH", path)
    return path


def _other_process_holds(path):
    """Другой экземпляр хаба: свой open того же файла блокировки."""
    fd = open(f"{path}.owner.lock", "a+")  # noqa: SIM115
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


async def test_a_second_instance_neither_sweeps_nor_orders(db, monkeypatch, lock_path):
    from hub.services import rework_launch

    rework_launch.claim_process(lock_path, "test")  # приложение на этой базе
    task_id, jobs = await _crash_on_submit(db, monkeypatch, accepted=True)
    await db.execute(
        "UPDATE rework_launches SET boot_id='previous-boot' WHERE task_id=?",
        (task_id,),
    )
    await db.commit()
    rework_launch.release_all()  # другой процесс держит блокировку, не мы
    holder = _other_process_holds(lock_path)
    try:
        assert rework_launch.claim_process(lock_path) is False

        await rework_launch.sweep(db)
        rows = await db.execute_fetchall(
            "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
        )
        assert [r[0] for r in rows] == ["reserved"], "второй экземпляр сверил бронь"

        other = await _task_in(db, "needs_decision", submission_generation=1)
        with pytest.raises(HTTPException) as refused:
            await services.decide_task(db, other, TaskDecide(**REWORK))
        assert refused.value.status_code == 503
        assert len(jobs["calls"]) == 1
    finally:
        holder.close()

    # Первый экземпляр ушёл: право берётся при следующей попытке.
    await rework_launch.sweep(db)
    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["launched"]
    rework_launch.release_process(lock_path)


async def test_the_process_lock_is_taken_once_per_process(lock_path):
    from hub.services import rework_launch

    assert rework_launch.claim_process(lock_path) is True
    assert rework_launch.claim_process(lock_path) is True  # второе приложение
    rework_launch.release_process(lock_path)
    rework_launch.release_process(lock_path)
    holder = _other_process_holds(lock_path)
    try:
        assert rework_launch.claim_process(lock_path) is False
    finally:
        holder.close()
    rework_launch.release_process(lock_path)


async def test_a_cancel_on_the_booking_commit_frees_the_booking(db, monkeypatch):
    """Отмена на COMMIT брони: решение записано, до release_sender раньше не доходило."""
    from hub.services import rework_launch

    sent: list[str] = []

    async def submit(message: str, **_kwargs: Any) -> dict[str, Any]:
        sent.append(message)
        return {"job_id": f"job-{len(sent)}"}

    monkeypatch.setattr(plugins.dispatch, "submit_task", submit)
    monkeypatch.setattr(plugins.dispatch, "list_jobs", lambda limit=50: [])
    task_id = await _task_in(db, "needs_decision", submission_generation=1)
    loop = asyncio.get_running_loop()
    seen = {"booked": False, "task": None}

    def trace(statement: str) -> None:
        if statement.startswith("INSERT INTO rework_launches"):
            seen["booked"] = True
        elif statement.startswith("COMMIT") and seen["booked"] and seen["task"]:
            seen["booked"] = False
            loop.call_soon_threadsafe(seen["task"].cancel)

    await db.set_trace_callback(trace)
    attempt = asyncio.create_task(
        services.decide_task(db, task_id, TaskDecide(**REWORK))
    )
    seen["task"] = attempt
    with pytest.raises(asyncio.CancelledError):
        await attempt
    await db.set_trace_callback(None)

    assert sent == []
    await rework_launch.sweep(db)
    rows = await db.execute_fetchall(
        "SELECT state FROM rework_launches WHERE task_id=?", (task_id,)
    )
    assert [r[0] for r in rows] == ["needs_human"], "бронь осталась занятой"

    view = await services.decide_task(
        db, task_id, TaskDecide(**REWORK, confirm_no_job=True)
    )
    assert view.status.value == "fix_requested" and len(sent) == 1
    assert dict(await repo.get_task(db, task_id))["job_id"] == "job-1"


async def test_the_lock_creates_its_directory_and_never_grants_ownership_on_error(
    tmp_path,
):
    from hub.services import rework_launch

    fresh = tmp_path / "not-yet" / "hub.db"
    assert rework_launch.claim_process(fresh) is True
    rework_launch.release_process(fresh)

    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert rework_launch.claim_process(blocker / "hub.db") is False
    rework_launch.release_process(blocker / "hub.db")


async def test_an_app_that_never_held_the_lock_cannot_grant_ownership(lock_path):
    """Два приложения без блокировки; уход одного не делает оставшееся владельцем."""
    from hub.services import rework_launch

    holder = _other_process_holds(lock_path)
    try:
        first, second = object(), object()
        assert rework_launch.claim_process(lock_path, first) is False
        assert rework_launch.claim_process(lock_path, second) is False
        rework_launch.release_process(lock_path, first)

        assert rework_launch.owns_db() is False
    finally:
        holder.close()
    rework_launch.release_process(lock_path, second)


async def test_a_forked_child_inherits_neither_ownership_nor_boot_id(
    lock_path, monkeypatch
):
    from hub.services import rework_launch

    assert rework_launch.claim_process(lock_path) is True
    parent_boot = rework_launch.boot_id()
    # Родитель держит своё открытое описание файла (после fork их два).
    parent_copy = os.dup(rework_launch._OWNERS[str(lock_path)].handle.fileno())
    child_pid = os.getpid() + 100000
    monkeypatch.setattr(rework_launch.os, "getpid", lambda: child_pid)

    assert rework_launch.owns_db() is False, "потомок унаследовал владение"
    assert rework_launch.boot_id() != parent_boot, "потомок унаследовал boot id"
    monkeypatch.undo()
    os.close(parent_copy)
    rework_launch.release_all()


async def test_a_lazily_taken_lock_is_released_with_its_last_app(lock_path):
    """A ушёл после двух отказов B1 и B2; lock взят лениво и закрывается с B2."""
    from hub.services import rework_launch

    holder = _other_process_holds(lock_path)
    first, second = object(), object()
    assert rework_launch.claim_process(lock_path, first) is False
    assert rework_launch.claim_process(lock_path, second) is False
    rework_launch.release_process(lock_path, first)
    holder.close()  # A освободил блокировку

    assert rework_launch.owns_db() is True, "второе приложение вправе взять lock"
    rework_launch.release_process(lock_path, second)

    probe = _other_process_holds(lock_path)  # BlockingIOError, если дескриптор жив
    probe.close()


async def test_owns_db_takes_no_lock_without_a_registered_app(lock_path):
    from hub.services import rework_launch

    rework_launch.release_all()

    assert rework_launch.owns_db() is False
    probe = _other_process_holds(lock_path)
    probe.close()


def test_the_database_path_fixture_survives_a_monkeypatch_undo(monkeypatch):
    before = config.HUB_DB_PATH
    assert "hubdb" in str(before)

    monkeypatch.setattr(config, "HUB_DB_PATH", before.parent / "other.db")
    monkeypatch.undo()

    assert config.HUB_DB_PATH == before, "undo() вернул реальный путь базы"


async def test_a_symlinked_database_shares_one_lock(tmp_path, monkeypatch):
    from hub.services import rework_launch

    real = tmp_path / "real.db"
    link = tmp_path / "link.db"
    link.symlink_to(real)
    assert rework_launch.claim_process(real, "a") is True

    assert rework_launch.claim_process(link, "b") is True  # тот же процесс
    probe_path = f"{real}.owner.lock"
    with pytest.raises(BlockingIOError):
        fd = open(probe_path, "a+")  # noqa: SIM115
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            fd.close()
    assert not (tmp_path / "link.db.owner.lock").exists(), "второй lock-файл"
    rework_launch.release_all()


async def test_a_failed_startup_does_not_leak_its_registration(monkeypatch):
    from hub import app as app_mod
    from hub.services import rework_launch

    async def broken():
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(app_mod, "get_db", broken)
    registered = {k: list(v) for k, v in rework_launch._APPS.items()}

    with pytest.raises(RuntimeError, match="база недоступна"):
        async with app_mod.lifespan(app_mod.app):
            pass

    assert rework_launch._APPS == registered, "заявка приложения осталась"


# --- fork: потомок не наследует регистрации и дескрипторы родителя ---------------

_STEP_WAIT = 10.0


class _Chan:
    """Односторонние строки через pipe с ограниченным ожиданием."""

    def __init__(self, rfd: int, wfd: int) -> None:
        self.rfd, self.wfd, self.buf = rfd, wfd, b""

    def say(self, text: str) -> None:
        os.write(self.wfd, (text + "\n").encode())

    def hear(self) -> str:
        deadline = time.monotonic() + _STEP_WAIT
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.rfd], [], [], left)[0]:
                raise TimeoutError("шаг не пришёл за отведённое время")
            chunk = os.read(self.rfd, 4096)
            if not chunk:
                raise EOFError("собеседник закрыл канал")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode()

    def report(self) -> dict[str, Any]:
        line = self.hear()
        assert not line.startswith("error:"), line
        return json.loads(line)


def _run_forked(child_body):
    """Настоящий os.fork: потомок идёт по сигналам родителя, затем os._exit."""
    to_child_r, to_child_w = os.pipe()
    from_child_r, from_child_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(to_child_w)
            os.close(from_child_r)
            chan = _Chan(to_child_r, from_child_w)
            try:
                child_body(chan)
            except BaseException as exc:  # noqa: BLE001 - в потомке ловим всё
                chan.say(f"error: {type(exc).__name__}: {exc}")
                code = 1
        finally:
            os._exit(code)
    os.close(to_child_r)
    os.close(from_child_w)
    return pid, _Chan(from_child_r, to_child_w)


def _reap(pid: int, chan: _Chan) -> int:
    """Отпустить потомка и дождаться его выхода; зависшего убить."""
    try:
        chan.say("exit")
    except OSError:
        pass
    deadline = time.monotonic() + _STEP_WAIT
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            os.close(chan.rfd)
            os.close(chan.wfd)
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.02)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    os.close(chan.rfd)
    os.close(chan.wfd)
    raise AssertionError("потомок не завершился сам")


def _state(rework_launch, handles) -> dict[str, Any]:
    """Что потомок видит у себя: реестры и закрытость переданных дескрипторов."""
    return {
        "apps": {k: len(v) for k, v in rework_launch._APPS.items()},
        "owners": sorted(rework_launch._OWNERS),
        "closed": [h.closed for h in handles],
    }


def _can_take_lock(path) -> bool:
    """Независимый претендент (не потомок и не родитель) берёт lock?"""
    try:
        fd = _other_process_holds(path)
    except BlockingIOError:
        return False
    fd.close()
    return True


async def test_a_forked_child_drops_parent_registrations(lock_path):
    from hub.services import rework_launch

    key = rework_launch._key(lock_path)
    assert rework_launch.claim_process(lock_path, "A") is True
    inherited = [o.handle for o in rework_launch._OWNERS.values()]

    def child(chan: _Chan) -> None:
        chan.hear()  # go: очистка при первом входе потомка в модуль
        assert rework_launch.owns_db() is False
        chan.say(json.dumps(_state(rework_launch, inherited)))
        chan.hear()  # go: родитель отпустил A
        assert rework_launch.claim_process(lock_path, "B") is True
        taken = [o.handle for o in rework_launch._OWNERS.values()]
        chan.say(json.dumps(_state(rework_launch, inherited)))
        rework_launch.release_process(lock_path, "B")
        chan.say(json.dumps(_state(rework_launch, inherited + taken)))
        chan.hear()  # exit

    pid, chan = _run_forked(child)
    try:
        chan.say("go")
        cleaned = chan.report()
        assert cleaned["apps"] == {} and cleaned["owners"] == [], "чужие записи"
        assert all(cleaned["closed"]), "унаследованный дескриптор не закрыт"
        # Очистка потомка не снимает lock родителя: родитель всё ещё держит A.
        assert _can_take_lock(lock_path) is False, "очистка сняла lock родителя"

        rework_launch.release_process(lock_path, "A")
        chan.say("go")
        mine = chan.report()
        assert mine["apps"] == {key: 1} and mine["owners"] == [key]
        left = chan.report()
        assert left["apps"] == {} and left["owners"] == [], "осталась запись родителя"
        # Потомок жив и не звал release_all(): lock свободен, а закрытие - отдельно.
        assert all(left["closed"]), "дескриптор потомка не закрыт"
        assert _can_take_lock(lock_path) is True, "lock держится после ухода B"
    finally:
        assert _reap(pid, chan) == 0


async def test_a_forked_child_lazy_owner_releases_with_its_last_app(lock_path):
    from hub.services import rework_launch

    key = rework_launch._key(lock_path)
    assert rework_launch.claim_process(lock_path, "A") is True
    inherited = [o.handle for o in rework_launch._OWNERS.values()]

    def child_without_app(chan: _Chan) -> None:
        chan.hear()
        # Первый вход потомка - снятие заявки родителя: чужого он не освобождает.
        rework_launch.release_process(lock_path, "A")
        chan.say(json.dumps(_state(rework_launch, inherited)))
        assert rework_launch.owns_db() is False
        chan.say(json.dumps(_state(rework_launch, inherited)))
        chan.hear()  # родитель отпустил A
        assert rework_launch.owns_db() is False, "унаследованная регистрация"
        chan.say(json.dumps(_state(rework_launch, inherited)))
        chan.hear()

    pid, chan = _run_forked(child_without_app)
    try:
        chan.say("go")
        stray = chan.report()
        assert stray["apps"] == {} and stray["owners"] == [], "release не очистил"
        before = chan.report()
        assert before["apps"] == {} and before["owners"] == [] and all(before["closed"])
        rework_launch.release_process(lock_path, "A")
        chan.say("go")
        after = chan.report()
        assert after["apps"] == {} and after["owners"] == [] and all(after["closed"])
        assert _can_take_lock(lock_path) is True, "потомок взял lock без заявки"
    finally:
        assert _reap(pid, chan) == 0

    # (б) заявка B есть, но отказ: после ухода родителя владение берётся лениво.
    assert rework_launch.claim_process(lock_path, "A") is True
    inherited = [o.handle for o in rework_launch._OWNERS.values()]

    def child_refused(chan: _Chan) -> None:
        chan.hear()
        assert rework_launch.claim_process(lock_path, "B") is False
        chan.say(json.dumps(_state(rework_launch, inherited)))
        chan.hear()  # родитель отпустил A
        assert rework_launch.owns_db() is True, "B вправе взять lock"
        taken = [o.handle for o in rework_launch._OWNERS.values()]
        chan.say(json.dumps(_state(rework_launch, taken)))
        rework_launch.release_process(lock_path, "B")
        chan.say(json.dumps(_state(rework_launch, taken)))
        chan.hear()

    pid, chan = _run_forked(child_refused)
    try:
        chan.say("go")
        refused = chan.report()
        assert refused["apps"] == {key: 1}, "в потомке только B"
        rework_launch.release_process(lock_path, "A")
        chan.say("go")
        lazy = chan.report()
        assert lazy["owners"] == [key] and lazy["apps"] == {key: 1}
        gone = chan.report()
        assert gone["apps"] == {} and gone["owners"] == []
        assert all(gone["closed"]), "уход B не закрыл дескриптор"
        assert _can_take_lock(lock_path) is True, "претендент не допущен"
    finally:
        assert _reap(pid, chan) == 0


async def test_a_forked_child_release_all_keeps_the_parent_lock(lock_path):
    """Первый вход потомка - release_all(): дескриптор закрыт, lock родителя цел."""
    from hub.services import rework_launch

    assert rework_launch.claim_process(lock_path, "A") is True
    inherited = [o.handle for o in rework_launch._OWNERS.values()]

    def child(chan: _Chan) -> None:
        chan.hear()
        rework_launch.release_all()
        chan.say(json.dumps(_state(rework_launch, inherited)))
        chan.hear()

    pid, chan = _run_forked(child)
    try:
        chan.say("go")
        done = chan.report()
        assert done["apps"] == {} and done["owners"] == [] and all(done["closed"])
        assert _can_take_lock(lock_path) is False, "release_all потомка снял lock"
        rework_launch.release_process(lock_path, "A")
        assert _can_take_lock(lock_path) is True
    finally:
        assert _reap(pid, chan) == 0


# --- fork: переход PID сериализован, второй поток потомка его не обгоняет --------


class _PausingOwners(dict):
    """Реестр владений, обход которого останавливается в заданной точке."""

    def __init__(self, items, entered, resume, *, after_first=False):
        super().__init__(items)
        self.entered, self.resume, self.after_first = entered, resume, after_first
        self.overtaken: bool | None = None
        self.walkers: list[str] = []

    def _pause(self):
        name = threading.current_thread().name
        self.walkers.append(name)
        if name == "first":
            self.entered.set()
            self.overtaken = self.resume.wait(0.5)

    def values(self):
        live = super().values()
        if not self.after_first:
            self._pause()
            return live
        return self._paused(iter(live))

    def _paused(self, live):
        yield next(live)
        self._pause()
        yield from live


def _race_in_child(rework_launch, first_call, second_call, *, after_first):
    """Поток 1 входит в модуль первым и встаёт посреди очистки; поток 2 входит следом."""
    entered, resume = threading.Event(), threading.Event()
    rework_launch._OWNERS = _PausingOwners(
        rework_launch._OWNERS, entered, resume, after_first=after_first
    )
    out: dict[str, Any] = {}

    def run(name, call):
        try:
            out[name] = call()
        except BaseException as exc:  # noqa: BLE001 - ошибку потока отдаём тесту
            out[name] = f"{type(exc).__name__}: {exc}"
        if name == "second":
            resume.set()

    first = threading.Thread(target=run, args=("first", first_call), name="first")
    first.start()
    assert entered.wait(5), "поток 1 не дошёл до очистки"
    second = threading.Thread(target=run, args=("second", second_call), name="second")
    second.start()
    first.join(10)
    second.join(10)
    assert not first.is_alive() and not second.is_alive(), "поток завис"
    # Второй поток закончил, пока первый стоял посреди очистки, - он её обогнал.
    out["overtaken"] = rework_launch._OWNERS.overtaken
    out["walkers"] = rework_launch._OWNERS.walkers
    return out


async def test_a_second_thread_cannot_overtake_the_pid_transition(lock_path):
    from hub.services import rework_launch

    key = rework_launch._key(lock_path)
    assert rework_launch.claim_process(lock_path, "A") is True
    rework_launch.release_process(lock_path, "A")
    assert rework_launch.claim_process(lock_path, "A") is True
    inherited = [o.handle for o in rework_launch._OWNERS.values()]

    def child(chan: _Chan) -> None:
        chan.hear()
        out = _race_in_child(
            rework_launch,
            lambda: rework_launch.release_process(lock_path, "A"),
            lambda: rework_launch.claim_process(lock_path, "B"),
            after_first=False,
        )
        taken = [o.handle for o in rework_launch._OWNERS.values()]
        chan.say(
            json.dumps(
                {
                    **_state(rework_launch, taken),
                    "out": out,
                    "inherited_closed": [h.closed for h in inherited],
                    "owns": rework_launch.owns_db(),
                }
            )
        )
        chan.hear()

    pid, chan = _run_forked(child)
    try:
        rework_launch.release_process(lock_path, "A")  # родитель ушёл: lock свободен
        chan.say("go")
        done = chan.report()
        assert done["out"]["second"] is True, done["out"]
        assert done["owners"] == [key] and done["apps"] == {key: 1}, "B потеряно"
        assert done["closed"] == [False], "поток 1 закрыл дескриптор B"
        assert done["owns"] is True
        assert all(done["inherited_closed"])
    finally:
        assert _reap(pid, chan) == 0


async def test_a_new_key_during_the_pid_cleanup_does_not_break_the_walk(
    lock_path, tmp_path
):
    from hub.services import rework_launch

    other = tmp_path / "second" / "hub.db"
    other_key = rework_launch._key(other)
    assert rework_launch.claim_process(lock_path, "A") is True

    def child(chan: _Chan) -> None:
        chan.hear()
        out = _race_in_child(
            rework_launch,
            lambda: rework_launch.release_process(lock_path, "A"),
            lambda: rework_launch.claim_process(other, "B"),
            after_first=True,
        )
        chan.say(json.dumps({**_state(rework_launch, []), "out": out}))
        chan.hear()

    pid, chan = _run_forked(child)
    try:
        chan.say("go")
        done = chan.report()
        assert done["out"]["first"] is None, done["out"]  # обход не упал
        assert done["out"]["second"] is True, done["out"]
        assert done["owners"] == [other_key] and done["apps"] == {other_key: 1}
    finally:
        assert _reap(pid, chan) == 0
        rework_launch.release_process(lock_path, "A")


async def test_a_lock_held_at_fork_time_does_not_hang_the_child(lock_path):
    from hub.services import rework_launch

    assert rework_launch.claim_process(lock_path, "A") is True
    rework_launch.release_process(lock_path, "A")

    def child(chan: _Chan) -> None:
        chan.hear()
        done = threading.Event()
        threading.Thread(
            target=lambda: (rework_launch.claim_process(lock_path, "B"), done.set()),
            daemon=True,
        ).start()
        chan.say(json.dumps({"entered": done.wait(5)}))
        chan.hear()

    with rework_launch._LOCK:  # поток родителя держит замок в момент fork
        pid, chan = _run_forked(child)
    try:
        chan.say("go")
        assert chan.report()["entered"] is True, "потомок унаследовал занятый замок"
    finally:
        assert _reap(pid, chan) == 0


@pytest.mark.parametrize("second", ["owns_db", "release_all"])
async def test_every_entry_waits_for_the_pid_transition(lock_path, second):
    from hub.services import rework_launch

    assert rework_launch.claim_process(lock_path, "A") is True
    rework_launch.release_process(lock_path, "A")
    assert rework_launch.claim_process(lock_path, "A") is True

    def child(chan: _Chan) -> None:
        chan.hear()
        out = _race_in_child(
            rework_launch,
            lambda: rework_launch.release_process(lock_path, "A"),
            getattr(rework_launch, second),
            after_first=False,
        )
        chan.say(json.dumps({**_state(rework_launch, []), "out": out}))
        chan.hear()

    pid, chan = _run_forked(child)
    try:
        rework_launch.release_process(lock_path, "A")
        chan.say("go")
        done = chan.report()
        assert done["out"]["overtaken"] is False, "вход обогнал переход PID"
        assert done["out"]["walkers"] == ["first"], "очистку начал второй поток"
        assert done["out"]["second"] in (False, None)
        assert done["apps"] == {} and done["owners"] == []
    finally:
        assert _reap(pid, chan) == 0


async def test_a_failed_cleanup_is_retried_not_published_as_done(lock_path):
    from hub.services import rework_launch

    key = rework_launch._key(lock_path)
    assert rework_launch.claim_process(lock_path, "A") is True

    class _Flaky:
        closed = False

        def __init__(self, real):
            self.real, self.failed = real, False

        def close(self):
            if not self.failed:
                self.failed = True
                raise OSError("close failed")
            self.real.close()
            self.closed = True

    def child(chan: _Chan) -> None:
        chan.hear()
        owner = rework_launch._OWNERS[key]
        owner.handle = _Flaky(owner.handle)
        try:
            rework_launch.claim_process(lock_path, "B")
        except OSError:
            pass
        rework_launch.claim_process(lock_path, "C")
        chan.say(
            json.dumps({"apps": {k: list(v) for k, v in rework_launch._APPS.items()}})
        )
        chan.hear()

    pid, chan = _run_forked(child)
    try:
        chan.say("go")
        apps = chan.report()["apps"]
        assert apps.get(key) == ["C"], "родительские заявки пережили сбой очистки"
    finally:
        assert _reap(pid, chan) == 0
        rework_launch.release_process(lock_path, "A")
