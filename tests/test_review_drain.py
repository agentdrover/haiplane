"""deploy/review-drain.sh: деплой ждёт идущие локальные deep-ревью (#1588).

Скрипт — shell, поэтому тест гоняет настоящий bash на tmpdir: spool, задания
службы (``job-<16 hex>`` с job.json или claimed и без result.json), маркер
``draining`` и замок ``.drain.lock``. Ни root, ни сети, ни systemd не нужно.

``flock`` есть не везде (на macOS его нет, на CI он есть): когда системного нет,
в PATH кладётся заглушка на ``fcntl.flock`` — тот же вызов ядра, поэтому замок,
который держит сам тест, настоящий для обоих.
"""

from __future__ import annotations

import ast
import asyncio
import fcntl
import os
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy/review-drain.sh"

_FLOCK_SHIM = f"""#!{sys.executable}
import fcntl, sys, time

args = sys.argv[1:]
wait = None
while args and args[0].startswith("-"):
    flag = args.pop(0)
    if flag == "-w":
        wait = float(args.pop(0))
    elif flag == "-n":
        wait = 0.0
end = time.monotonic() + (wait if wait is not None else 1e9)
fd = int(args[0])
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except OSError:
        if time.monotonic() >= end:
            sys.exit(1)
        time.sleep(0.02)
"""

JOB = "job-0123456789abcdef"
JOB2 = "job-fedcba9876543210"


def install_flock(bin_dir: Path) -> None:
    """Положить в ``bin_dir`` заглушку flock, если системного flock нет."""
    bin_dir.mkdir(exist_ok=True)
    if shutil.which("flock"):
        return
    shim = bin_dir / "flock"
    shim.write_text(_FLOCK_SHIM)
    shim.chmod(0o755)


@pytest.fixture
def bin_dir(tmp_path) -> Path:
    path = tmp_path / "bin"
    install_flock(path)
    return path


@pytest.fixture
def spool(tmp_path) -> Path:
    path = tmp_path / "spool"
    path.mkdir(mode=0o770)
    return path


def _env(spool: Path, bin_dir: Path, **over: str) -> dict[str, str]:
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HAIPLANE_LOCAL_REVIEW_SPOOL_DIR": str(spool),
        "DRAIN_OWNER": "deploy-test",
        "DRAIN_BUDGET_SECONDS": "2",
        "DRAIN_POLL_SECONDS": "0.1",
        "DRAIN_PROGRESS_SECONDS": "1",
    }
    env.update(over)
    return env


def _run(cmd: list[str], spool: Path, bin_dir: Path, timeout: float = 30, **over: str):
    return subprocess.run(
        ["bash", str(SCRIPT), *cmd],
        env=_env(spool, bin_dir, **over),
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _popen(cmd: list[str], spool: Path, bin_dir: Path, **over: str):
    return subprocess.Popen(
        ["bash", str(SCRIPT), *cmd],
        env=_env(spool, bin_dir, **over),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _job(spool: Path, name: str = JOB, *, claimed: bool = False, result: bool = False):
    jobdir = spool / name
    jobdir.mkdir(mode=0o770)
    if claimed:
        (jobdir / "claimed").write_text("")
    else:
        (jobdir / "job.json").write_text("{}")
    if result:
        (jobdir / "result.json").write_text("{}")
    return jobdir


def _marker(spool: Path) -> dict[str, str]:
    text = (spool / "draining").read_text()
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def _wait_for(predicate, limit: float = 5.0) -> bool:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _await_output(proc, needle: str, limit: float = 10.0) -> str:
    """Ждать строку в выводе процесса, не блокируясь: событие, а не sleep.

    Читает ``proc.stdout`` через ``select`` и ``os.read`` — без буфера обёртки,
    поэтому пустой вывод не вешает тест. Вернёт всё прочитанное; нет строки за
    ``limit`` — пустая строка в ответе провалит assert вызывающего.
    """
    fd = proc.stdout.fileno()
    seen = ""
    end = time.monotonic() + limit
    while needle not in seen and time.monotonic() < end:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        seen += chunk.decode(errors="replace")
    return seen


def _close_stdin(proc) -> None:
    """Закрыть stdin (EOF) так, чтобы ``communicate`` после этого работал."""
    proc.stdin.close()
    proc.stdin = None


def _sleep_log(bin_dir: Path, log: Path) -> None:
    """Заглушка sleep: пишет строку в ``log`` и спит по-настоящему.

    Строка = «процесс дошёл до ожидания»: событие для теста вместо sleep.
    """
    shim = bin_dir / "sleep"
    shim.write_text(
        f'#!/bin/sh\necho "$$" >>{log}\nexec {shutil.which("sleep")} "$@"\n'
    )
    shim.chmod(0o755)


def _write_marker(spool: Path, owner: str, ahead: int) -> bytes:
    body = f"owner={owner}\nexpires={int(time.time()) + ahead}\n".encode()
    (spool / "draining").write_bytes(body)
    return body


# --------------------------------------------------------------------- AC-1


def test_waits_for_running_jobs_then_gives_up(spool, bin_dir) -> None:
    """AC-1: маркер с владельцем; завершилось — drain ok, срок вышел — timeout."""
    # Задания, которые «идут» (job.json ИЛИ claimed без result.json), и то, что
    # идущим не считается: с result.json, без job.json и claimed, чужое имя.
    running = _job(spool, JOB)
    claimed = _job(spool, JOB2, claimed=True)
    _job(spool, "job-aaaaaaaaaaaaaaaa", result=True)
    (spool / "job-bbbbbbbbbbbbbbbb").mkdir()
    _job(spool, "job-not-hex")

    proc = _popen(["acquire"], spool, bin_dir, DRAIN_BUDGET_SECONDS="20")
    assert _wait_for(lambda: (spool / "draining").exists()), "маркер не поставлен"
    marker = _marker(spool)
    assert marker["owner"] == "deploy-test"
    assert int(marker["expires"]) > time.time()
    assert (spool / "draining").stat().st_mode & 0o777 == 0o660
    time.sleep(0.5)
    assert proc.poll() is None, "идущие задания не дождались"

    # Прогон не трогаем — он продолжается; завершается он сам, и деплой это видит.
    (running / "result.json").write_text("{}")
    time.sleep(0.4)
    assert proc.poll() is None, "второе задание (claimed) ещё идёт"
    (claimed / "result.json").write_text("{}")
    out, _ = proc.communicate(timeout=15)
    assert proc.returncode == 0
    assert "drain ok" in out, out
    assert (spool / "draining").exists(), "маркер остаётся до конца выкладки"

    # Срок вышел: timeout со списком, exit 0, маркер на месте (снимет release).
    stuck = _job(spool, "job-cccccccccccccccc")
    started = time.monotonic()
    done = _run(["acquire"], spool, bin_dir, DRAIN_BUDGET_SECONDS="1")
    assert time.monotonic() - started < 6
    assert done.returncode == 0
    line = next(x for x in done.stdout.splitlines() if x.startswith("drain timeout"))
    assert stuck.name in line
    assert "job-aaaaaaaaaaaaaaaa" not in line
    assert JOB not in line and "job-not-hex" not in line
    assert (stuck / "job.json").exists(), "скрипт не трогает чужие прогоны"


# --------------------------------------------------------------------- AC-2


def test_no_active_jobs_or_no_spool_is_immediate(spool, bin_dir, tmp_path) -> None:
    """AC-2: пусто — ok сразу; нет spool, чужой маркер, занятый замок — exit 0."""
    started = time.monotonic()
    done = _run(["acquire"], spool, bin_dir, DRAIN_BUDGET_SECONDS="30")
    assert time.monotonic() - started < 3, "без заданий ждать нечего"
    assert done.returncode == 0 and "drain ok" in done.stdout, done.stdout
    lock = spool / ".drain.lock"
    assert lock.exists() and lock.stat().st_mode & 0o777 == 0o660
    inode = lock.stat().st_ino
    _run(["release"], spool, bin_dir)
    _run(["acquire"], spool, bin_dir)
    assert lock.stat().st_ino == inode, "замок — постоянный inode"
    _run(["release"], spool, bin_dir)
    assert not (spool / "draining").exists()

    # Нет каталога очереди: degraded с причиной, не падение.
    missing = tmp_path / "nowhere"
    done = _run(["acquire"], missing, bin_dir)
    assert done.returncode == 0
    assert "drain degraded" in done.stdout and str(missing) in done.stdout
    assert not missing.exists(), "скрипт не создаёт чужой каталог"

    # Недоступный каталог (под root права не работают — тогда пропуск шага).
    if os.geteuid() != 0:
        closed = tmp_path / "closed"
        closed.mkdir()
        closed.chmod(0)
        try:
            done = _run(["acquire"], closed, bin_dir)
        finally:
            closed.chmod(stat.S_IRWXU)
        assert done.returncode == 0
        assert "drain degraded" in done.stdout, done.stdout

    # Чужой свежий маркер: не перезаписан, не снят, ждём срок и говорим degraded.
    before = _write_marker(spool, "deploy-other", 600)
    started = time.monotonic()
    done = _run(["acquire"], spool, bin_dir, DRAIN_BUDGET_SECONDS="1")
    assert time.monotonic() - started < 6
    assert done.returncode == 0
    assert "drain degraded" in done.stdout and "deploy-other" in done.stdout
    assert (spool / "draining").read_bytes() == before
    _run(["release"], spool, bin_dir)
    _run(["recheck"], spool, bin_dir)
    assert (spool / "draining").read_bytes() == before, "чужой не снимается"

    # Просроченный чужой — не маркер: деплой занимает его место.
    _write_marker(spool, "deploy-old", -5)
    done = _run(["acquire"], spool, bin_dir)
    assert "drain ok" in done.stdout
    assert _marker(spool)["owner"] == "deploy-test"
    _run(["release"], spool, bin_dir)

    # Занятый замок: срок не превышен, маркер не записан, exit 0.
    fd = os.open(spool / ".drain.lock", os.O_RDWR | os.O_CREAT, 0o660)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        started = time.monotonic()
        done = _run(["acquire"], spool, bin_dir, DRAIN_BUDGET_SECONDS="2")
        elapsed = time.monotonic() - started
    finally:
        os.close(fd)
    assert done.returncode == 0
    assert "drain degraded" in done.stdout and "замок" in done.stdout, done.stdout
    assert elapsed < 2 + 3, f"замок занят, а срок превышен: {elapsed:.1f} с"
    assert not (spool / "draining").exists()


# ------------------------------------------------------------- extra guards


def test_a_signal_right_after_the_marker_write_takes_it_back(
    spool, bin_dir, tmp_path
) -> None:
    """AC-1 (#1595): TERM сразу после rename маркера не оставляет маркер.

    Шим ``mv`` — окно гонки, без sleep: настоящий mv, затем TERM основному
    процессу acquire (его PID тест присылает через FIFO) и сразу выход. Шим не
    ждёт acquire: он держит замок через fd 9, а acquire ждёт подстановку.
    Stdin acquire открыт до выхода процесса: EOF после TERM послал бы второй TERM и
    оборвал бы уборку первого — это другая причина, не та, что проверяется.
    """
    job = _job(spool)
    job_bytes = b'{"keep": "me"}'
    (job / "job.json").write_bytes(job_bytes)
    fifo = tmp_path / "pid.fifo"
    os.mkfifo(fifo)
    sent = tmp_path / "term-sent"
    shim = bin_dir / "mv"
    shim.write_text(
        f'#!/bin/sh\n{shutil.which("mv")} "$@" || exit $?\n'
        f'read -r pid <"{fifo}"\nkill -TERM "$pid"\n: >"{sent}"\n'
    )
    shim.chmod(0o755)
    proc = subprocess.Popen(
        ["bash", str(SCRIPT), "acquire"],
        env=_env(spool, bin_dir, DRAIN_STDIN_LIVENESS="1", DRAIN_BUDGET_SECONDS="60"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # O_RDWR: запись не блокируется, пока шим ещё не открыл FIFO на чтение.
    wfd = os.open(fifo, os.O_RDWR)
    try:
        os.write(wfd, f"{proc.pid}\n".encode())
        confirmed = _wait_for(sent.exists, limit=15)
        try:
            # TERM подтверждён концом процесса; stdin всё это время открыт, иначе
            # EOF пошлёт второй TERM и оборвёт уборку первого.
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        _close_stdin(proc)
        out, _ = proc.communicate(timeout=15)
    finally:
        os.close(wfd)
    assert confirmed, "шим не дошёл до отправки TERM"
    assert proc.returncode == 143, (proc.returncode, out)
    assert not (spool / "draining").exists(), "TERM в окне записи оставил маркер"
    assert (job / "job.json").read_bytes() == job_bytes


def test_an_early_stop_never_removes_a_foreign_marker(spool, bin_dir, tmp_path) -> None:
    """AC-2 (#1595): (а) чужой маркер цел при TERM/EOF; (б) degraded без ожидания."""
    log = tmp_path / "sleeps"
    _sleep_log(bin_dir, log)
    # (а) acquire ждёт чужой свежий маркер (poll-sleep = «ждёт»), его останавливают.
    for stop in ("TERM", "EOF"):
        log.write_text("")
        before = _write_marker(spool, "deploy-other", 600)
        proc = subprocess.Popen(
            ["bash", str(SCRIPT), "acquire"],
            env=_env(
                spool, bin_dir, DRAIN_STDIN_LIVENESS="1", DRAIN_BUDGET_SECONDS="60"
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            assert _wait_for(lambda: log.read_text() != "", limit=15), "не ждёт"
            if stop == "TERM":
                # Только TERM: ждём выхода ДО communicate, который закрыл бы
                # stdin и добавил к TERM второй сигнал (EOF).
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=15)
            else:
                _close_stdin(proc)
            proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        assert (spool / "draining").read_bytes() == before, f"{stop}: чужой снят"
        (spool / "draining").unlink()
    # (б) замок занят дольше срока, свой прежний маркер стоит: degraded за срок,
    # без ожидания CLEANUP_LOCK_WAIT, прежний маркер на месте.
    own = _write_marker(spool, "deploy-test", 600)
    fd = os.open(spool / ".drain.lock", os.O_RDWR | os.O_CREAT, 0o660)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        started = time.monotonic()
        try:
            done = _run(["acquire"], spool, bin_dir, timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail("cleanup ждёт замок: degraded-путь набрал лишнее ожидание")
        elapsed = time.monotonic() - started
    finally:
        os.close(fd)
    assert "drain degraded" in done.stdout and "замок" in done.stdout, done.stdout
    assert elapsed < 2 + 3, f"замок занят, а срок превышен: {elapsed:.1f} с"
    assert (spool / "draining").read_bytes() == own


def test_a_signal_during_the_wait_removes_only_our_marker(spool, bin_dir) -> None:
    _job(spool)
    proc = _popen(["acquire"], spool, bin_dir, DRAIN_BUDGET_SECONDS="30")
    assert "маркер" in _await_output(proc, "поставлен"), "acquire не дошёл до ожидания"
    assert (spool / "draining").exists()
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=10)
    assert not (spool / "draining").exists(), "убитый деплой оставил маркер"
    assert (spool / JOB / "job.json").exists(), "прогон продолжается"


def _renewer(spool: Path, bin_dir: Path, *args: str, **over: str):
    """Продлитель с каналом живучести: stdin — труба, EOF = деплой умер."""
    return subprocess.Popen(
        ["bash", str(SCRIPT), "renew-loop", *args],
        env=_env(spool, bin_dir, DRAIN_STDIN_LIVENESS="1", **over),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stop_renewer(proc, *, after_term=None) -> str:
    """Остановить продлитель через TERM и вернуть его вывод.

    Порядок: terminate -> wait -> communicate. ``communicate`` закрывает stdin, а
    EOF на stdin будит ``cat`` из start_liveness, тот шлёт процессу USR1.
    Пока bash ждёт foreground-потомка, ловушки отложены и идут по возрастанию
    номера сигнала (Linux: USR1=10 < TERM=15): ловушка USR1 снимет маркер раньше
    TERM. Поэтому stdin закрывается только после выхода процесса, а остановка
    не зависит от того, где продлитель был в момент TERM.

    ``after_term`` вызывается сразу после отправки TERM (до ожидания): точка
    для барьера регрессии AC-1. Процесс не вышел за 10 с - убить и провалить тест.
    """
    proc.terminate()
    if after_term is not None:
        after_term()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)  # потомок мог держать stdout
        except subprocess.TimeoutExpired:
            pass
        pytest.fail("продлитель не вышел за 10 с после TERM")
    out, _ = proc.communicate(timeout=10)
    return out


def _parent_dies(proc) -> str:
    """Родитель умер: его конец трубы закрылся. Возвращает вывод до выхода."""
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("EOF на stdin не остановил процесс")
    return proc.stdout.read()


def test_the_marker_is_renewed_beyond_its_ttl_independently(spool, bin_dir) -> None:
    """Продление живёт само по себе: acquire давно отработал, а маркер свеж.

    Числа заданы явно: TTL 4 с, раунд 0.5 с, ждём рост expires до 15 с, а
    свежесть проверяем через 2 с после исходного срока (BEYOND). ``now()`` —
    целые секунды, поэтому у живого продлителя запас в момент проверки не меньше
    TTL - 1 (усечение) - 0.5 (возраст раунда) = 2.5 с: одна задержка раунда в
    1-2 с его не роняет. Продлитель, умерший сразу после первого роста (expires
    = исходный + 1 с), к проверке уже просрочен.
    """
    ttl, renew, growth, beyond = 4, 0.5, 15.0, 2
    over = {"DRAIN_TTL_SECONDS": str(ttl), "DRAIN_RENEW_SECONDS": str(renew)}
    assert "drain ok" in _run(["acquire"], spool, bin_dir, **over).stdout
    first = int(_marker(spool)["expires"])
    renewer = _renewer(spool, bin_dir, **over)
    try:
        assert _wait_for(
            lambda: int(_marker(spool)["expires"]) > first, limit=growth
        ), "продлитель не продлил маркер"
        # Исходный срок давно позади: без продления маркер уже просрочен.
        assert _wait_for(lambda: time.time() > first + beyond, limit=ttl + growth)
        assert int(_marker(spool)["expires"]) > time.time(), "маркер просрочен"
    finally:
        out = _stop_renewer(renewer)
    assert "drain degraded" not in out
    assert (spool / "draining").exists(), "остановка продления маркер не снимает"


def test_the_renewer_never_takes_a_foreign_marker(spool, bin_dir) -> None:
    before = _write_marker(spool, "deploy-other", 600)
    renewer = _renewer(spool, bin_dir, DRAIN_RENEW_SECONDS="0.2")
    time.sleep(0.8)
    assert (spool / "draining").read_bytes() == before
    _stop_renewer(renewer)


@pytest.mark.skipif(os.geteuid() == 0, reason="под root kill -0 чужому uid проходит")
def test_liveness_is_not_judged_by_kill_across_a_uid_boundary(spool, bin_dir) -> None:
    """Деплой и продлитель живут под РАЗНЫМИ uid (прод: user1 и хаб).

    kill(2) чужому uid даёт EPERM — и проверка «жив ли родитель» через
    ``kill -0`` читает это как «умер» и снимает маркер в первую секунду. Здесь
    «родитель» — pid 1 (жив, чужой uid, kill -0 даёт EPERM): продлитель с
    живым каналом обязан держать маркер.
    """
    assert "drain ok" in _run(["acquire"], spool, bin_dir).stdout
    probe = subprocess.run(["bash", "-c", "kill -0 1"], capture_output=True)
    assert probe.returncode != 0, "предпосылка: kill -0 по pid 1 даёт EPERM"
    renewer = _renewer(spool, bin_dir, "1", DRAIN_RENEW_SECONDS="0.2")
    try:
        time.sleep(1.5)
        assert renewer.poll() is None, "продлитель вышел: EPERM прочтён как смерть"
        assert (spool / "draining").exists(), "маркер снят на старте деплоя"
    finally:
        _stop_renewer(renewer)


def test_a_dead_parent_is_seen_as_eof_and_the_marker_is_taken_back(
    spool, bin_dir
) -> None:
    assert "drain ok" in _run(["acquire"], spool, bin_dir).stdout
    log = bin_dir.parent / "sleeps"
    _sleep_log(bin_dir, log)
    renewer = _renewer(spool, bin_dir, DRAIN_RENEW_SECONDS="0.2")
    # Раунд продления сделан (продлитель дошёл до своего sleep) — тогда EOF.
    assert _wait_for(lambda: log.exists() and log.read_text() != "", limit=15)
    assert (spool / "draining").exists()
    out = _parent_dies(renewer)
    assert "stopped by parent EOF" in out, out
    assert not (spool / "draining").exists()
    # Чужой маркер при этом остаётся как был.
    before = _write_marker(spool, "deploy-other", 600)
    other = _renewer(spool, bin_dir, DRAIN_RENEW_SECONDS="0.2")
    _parent_dies(other)
    assert (spool / "draining").read_bytes() == before


def test_a_dead_parent_stops_a_waiting_acquire_and_frees_its_marker(
    spool, bin_dir
) -> None:
    _job(spool)
    proc = subprocess.Popen(
        ["bash", str(SCRIPT), "acquire"],
        env=_env(spool, bin_dir, DRAIN_STDIN_LIVENESS="1", DRAIN_BUDGET_SECONDS="60"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert _wait_for(lambda: (spool / "draining").exists())
    _parent_dies(proc)
    assert not (spool / "draining").exists(), "ждущий acquire пережил деплой"
    assert (spool / JOB / "job.json").exists()


def test_a_failed_renewal_is_named_degraded(spool, bin_dir) -> None:
    log = bin_dir.parent / "sleeps"
    _sleep_log(bin_dir, log)
    over = {
        "DRAIN_TTL_SECONDS": "2",
        "DRAIN_RENEW_SECONDS": "0.3",
        "DRAIN_RENEW_LOCK_WAIT": "0.5",
    }
    _run(["acquire"], spool, bin_dir, **over)
    renewer = _renewer(spool, bin_dir, **over)
    try:
        # Первое продление удалось: продлитель дошёл до sleep с маркером «своим».
        assert _wait_for(lambda: log.exists() and log.read_text() != "", limit=15)
        fd = os.open(spool / ".drain.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # замок заняли — продлить нельзя
            seen = _await_output(renewer, "не продлён", limit=15)
        finally:
            os.close(fd)
    finally:
        out = _stop_renewer(renewer)
    assert "drain degraded" in seen + out and "не продлён" in seen + out, seen + out


class _Barrier:
    """Барьер для foreground-потомка продлителя: подменённый flock держит его.

    Событие вместо sleep: flock-заглушка пишет в fifo ``entered`` и блокируется на
    чтении fifo ``go``; тест ждёт ``entered`` через select с таймаутом и
    отпускает потомка записью в ``go``. Обе fifo открыты O_RDWR: на Linux это
    не блокирует и не даёт ложного EOF. Заглушка ``cat`` пишет pid своей
    подоболочки (она пошлёт USR1 после EOF), чтобы тест увидел конец отправки.
    """

    def __init__(self, bin_dir: Path, tmp_path: Path) -> None:
        self.armed = tmp_path / "barrier.armed"
        self.catpid = tmp_path / "barrier.catpid"
        entered, go = tmp_path / "barrier.entered", tmp_path / "barrier.go"
        os.mkfifo(entered)
        os.mkfifo(go)
        self._entered = os.open(entered, os.O_RDWR | os.O_NONBLOCK)
        self._go = os.open(go, os.O_RDWR | os.O_NONBLOCK)
        real = bin_dir / "flock-real"
        old = bin_dir / "flock"
        if old.exists():  # питоновская заглушка: переносим, делегируем ей
            old.rename(real)
            delegate = str(real)
        else:
            delegate = shutil.which("flock") or ""
        assert delegate, "нет flock: нечего подменять"
        flock = bin_dir / "flock"
        flock.write_text(
            "#!/bin/bash\n"
            f"if [ -e {shlex.quote(str(self.armed))} ] "
            f"&& rm {shlex.quote(str(self.armed))} 2>/dev/null; then\n"
            f"  echo held >{shlex.quote(str(entered))}\n"
            f"  read -r _ <{shlex.quote(str(go))}\n"
            "fi\n"
            f'exec {shlex.quote(delegate)} "$@"\n'
        )
        flock.chmod(0o755)
        cat = bin_dir / "cat"
        cat.write_text(
            "#!/bin/bash\n"
            "if [ $# -eq 0 ]; then\n"
            f'  echo "$PPID" >{shlex.quote(str(self.catpid))}.tmp\n'
            f"  mv {shlex.quote(str(self.catpid))}.tmp {shlex.quote(str(self.catpid))}\n"
            "fi\n"
            f'exec {shlex.quote(shutil.which("cat") or "cat")} "$@"\n'
        )
        cat.chmod(0o755)

    def arm(self) -> None:
        self.armed.write_text("")

    def wait_held(self, limit: float = 15.0) -> None:
        """Потомок вошёл в flock-заглушку и стоит на барьере, иначе fail."""
        ready, _, _ = select.select([self._entered], [], [], limit)
        if not ready:
            pytest.fail("барьер: потомок продлителя не дошёл до flock")
        os.read(self._entered, 16)
        if not _wait_for(self.catpid.exists, limit=limit):
            pytest.fail("барьер: заглушка cat не запустилась")

    def release(self) -> None:
        os.write(self._go, b"go\n")

    def catpid_exit_fd(self) -> int:
        """pidfd подоболочки cat: читается, когда она вышла (USR1 отправлен)."""
        return os.pidfd_open(int(self.catpid.read_text()))

    def close(self) -> None:
        os.close(self._entered)
        os.close(self._go)


def _reap(proc, barrier: _Barrier) -> None:
    """Уборка на любом пути: барьер отпустить ДО сбора вывода, ожидание с таймаутом.

    Потомок на барьере держит stdout; kill + communicate без отпускания повисли
    бы навсегда.
    """
    barrier.release()
    if proc.poll() is None:
        proc.kill()
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        pytest.fail("уборка: процесс продлителя не завершился за 10 с")


def _stop_renewer_unsafely_for_ac1(proc, barrier: _Barrier) -> str:
    """Старый порядок остановки (TERM, затем EOF) - ТОЛЬКО для регрессии AC-1.

    Фиксирует механизм флаки: stdin закрыт, пока потомок держит барьер; USR1
    успел уйти, и после отпускания ловушка USR1 (меньший номер) идёт раньше TERM.
    """
    exited = barrier.catpid_exit_fd()
    try:
        proc.terminate()
        proc.stdin.close()
        proc.stdin = None
        ready, _, _ = select.select([exited], [], [], 15)
        if not ready:
            pytest.fail("барьер: USR1 не был отправлен после EOF")
    finally:
        os.close(exited)
    barrier.release()
    out, _ = proc.communicate(timeout=10)
    return out


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="порядок отложенных ловушек USR1 < TERM только на Linux (на macOS USR1=30)",
)
def test_term_then_eof_on_a_held_renewer_drops_the_marker_but_the_helper_keeps_it(
    spool, bin_dir, tmp_path
) -> None:
    """Регрессия #1627. Фиксирует механизм флаки и проверяет, что помощник его обходит.

    Продлитель стоит в foreground-потомке (flock на барьере), ловушки отложены.
    Старый порядок: TERM, затем закрытие stdin; USR1 успевает уйти, пока потомок
    держит барьер, и после отпускания ловушка USR1 (меньший номер) снимает маркер.
    Помощник ``_stop_renewer`` stdin не закрывает до выхода: идёт TERM-ловушка.
    Порядок вызовов помощника проверяет test_the_stop_helper_orders_its_calls.
    """
    assert signal.SIGUSR1 < signal.SIGTERM, "предпосылка: USR1 раньше TERM"
    assert "drain ok" in _run(["acquire"], spool, bin_dir).stdout
    barrier = _Barrier(bin_dir, tmp_path)
    try:
        barrier.arm()
        renewer = _renewer(spool, bin_dir)
        try:
            barrier.wait_held()
            out = _stop_renewer_unsafely_for_ac1(renewer, barrier)
        finally:
            _reap(renewer, barrier)
        assert "stopped by parent EOF" in out, out
        assert not (spool / "draining").exists(), "механизм: USR1 снял маркер"

        barrier.catpid.unlink()
        assert "drain ok" in _run(["acquire"], spool, bin_dir).stdout
        barrier.arm()
        renewer = _renewer(spool, bin_dir)
        try:
            barrier.wait_held()
            out = _stop_renewer(renewer, after_term=barrier.release)
        finally:
            _reap(renewer, barrier)
        assert renewer.returncode == 0
        assert "stopped by parent EOF" not in out, out
        assert (spool / "draining").exists(), "остановка через помощник сняла маркер"
    finally:
        barrier.close()


class _FakeProc:
    """Процесс-двойник: пишет порядок вызовов помощника остановки."""

    def __init__(self, hang: bool = False) -> None:
        self.calls: list[str] = []
        self.hang = hang

    def terminate(self) -> None:
        self.calls.append("terminate")

    def wait(self, timeout=None) -> int:
        self.calls.append("wait")
        if self.hang:
            raise subprocess.TimeoutExpired("fake", timeout)
        return 0

    def communicate(self, timeout=None):
        self.calls.append("communicate")
        return "out", None

    def kill(self) -> None:
        self.calls.append("kill")


def test_the_stop_helper_orders_its_calls() -> None:
    """``_stop_renewer``: terminate -> wait -> communicate; зависший процесс убит."""
    fake = _FakeProc()
    assert _stop_renewer(fake) == "out"
    assert fake.calls == ["terminate", "wait", "communicate"]
    hung = _FakeProc(hang=True)
    with pytest.raises(pytest.fail.Exception):
        _stop_renewer(hung)
    assert hung.calls[:3] == ["terminate", "wait", "kill"]
    assert "communicate" in hung.calls and hung.calls[-1] != "terminate"


_STOP_HELPER = "_stop_renewer"
_UNSAFE_HELPER = "_stop_renewer_unsafely_for_ac1"


def _terminate_counts(tree: ast.AST) -> dict[str, int]:
    """Число ``.terminate()`` в каждой функции файла (вне функций - ``<module>``)."""
    counts: dict[str, int] = {}

    def walk(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, ast.FunctionDef) else owner
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "terminate"
            ):
                counts[owner] = counts.get(owner, 0) + 1
            walk(child, name)

    walk(tree, "<module>")
    return counts


def _unconditional_calls(body: list[ast.stmt]) -> list[tuple[str, str]]:
    """(получатель, метод) вызовов, исполняемых всегда: тело функции и ``try``.

    Ветки ``if``/циклы/обработчики исключений не считаются: ``if False: p.wait()``
    безусловным не является.
    """
    found: list[tuple[str, str]] = []
    for stmt in body:
        if isinstance(stmt, ast.Try):
            found += _unconditional_calls(stmt.body)
        elif isinstance(stmt, ast.Expr | ast.Assign):
            for node in ast.walk(stmt.value):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    found.append((ast.unparse(node.func.value), node.func.attr))
    return found


def test_every_renewer_stop_waits_before_closing_stdin() -> None:
    """Страж (#1627): ``terminate()`` - только в двух помощниках, по одному разу.

    В ``_stop_renewer`` terminate, wait и communicate - безусловные вызовы одного
    получателя в этом порядке.
    """
    tree = ast.parse(Path(__file__).read_text())
    counts = _terminate_counts(tree)
    assert counts == {_STOP_HELPER: 1, _UNSAFE_HELPER: 1}, (
        f"terminate() мимо помощников или не по одному разу: {counts}; communicate "
        "закроет stdin раньше выхода и вернёт гонку TERM/USR1"
    )
    helper = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == _STOP_HELPER
    )
    calls = _unconditional_calls(helper.body)
    recv = {
        m: [r for r, a in calls if a == m] for m in ("terminate", "wait", "communicate")
    }
    assert all(len(v) == 1 for v in recv.values()), f"нужен ровно один вызов: {recv}"
    assert recv["terminate"] == recv["wait"] == recv["communicate"], recv
    order = [a for _, a in calls if a in recv]
    assert order == ["terminate", "wait", "communicate"], order


def test_release_removes_only_our_marker_and_recheck_uses_the_one_budget(
    spool, bin_dir
) -> None:
    assert "drain ok" in _run(["acquire"], spool, bin_dir).stdout
    job = _job(spool)
    # Один срок на acquire и recheck: он уже истёк — повторная проверка не
    # получает нового, а сразу называет оставшееся задание.
    started = time.monotonic()
    done = _run(
        ["recheck"],
        spool,
        bin_dir,
        DRAIN_DEADLINE=str(int(time.time()) - 1),
        DRAIN_BUDGET_SECONDS="600",
    )
    assert time.monotonic() - started < 4
    assert "drain timeout" in done.stdout and job.name in done.stdout
    assert "drain" in _run(["release"], spool, bin_dir).stdout
    assert not (spool / "draining").exists()
    # Чужой маркер release не трогает.
    before = _write_marker(spool, "deploy-other", 600)
    _run(["release"], spool, bin_dir)
    assert (spool / "draining").read_bytes() == before


def test_the_script_is_a_plain_bash_script() -> None:
    first = SCRIPT.read_text().splitlines()[0]
    assert first == "#!/usr/bin/env bash"
    check = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


async def test_an_advisor_job_obeys_the_drain_and_the_steward_cap(
    db, local_spool, local_identity, local_service, bin_dir, monkeypatch
) -> None:
    """#1649 AC-7: задание советника — под тем же замком выкладки, что и ревью.

    Свежий маркер: задание не публикуется, код не выпускается, заказ остаётся
    открытым (это не ошибка готовности и не отказ). Выложенное до маркера
    задание считает деплой. Суточная квота стюарда общая: исчерпана — отказ
    ``daily_cap`` с причиной, советник не заказывается.
    """
    import json as _json

    from hub import config
    from hub.db import fetchall
    from hub.integrations import local_reviewer
    from hub.services.steward_advisor import order_due_advisors, start_advisor_run
    from hub.services.steward_advisor_local import wait_for_local_advisors
    from hub.services.steward_dispatch import close_finished_runs  # noqa: F401
    from tests.test_steward_advisor import _advisor_row, _ordered_advisor

    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "reviewer-token")
    svc = local_service()
    task_id, order = await _ordered_advisor(db, "drain-advisor")
    spool = local_spool
    expires = int(time.time()) + 600
    (spool / "draining").write_text(f"owner=deploy-test\nexpires={expires}\n")

    assert await start_advisor_run(db, dict(order)) is False, "при дренаже не стартует"
    await wait_for_local_advisors()
    assert not list(spool.glob("job-*")) and svc.jobs == []
    assert local_identity == [], "код при дренаже не выпускается"
    row = await _advisor_row(db, task_id)
    assert row["status"] == "open" and row["agent_id"] == "", row
    events = [
        _json.loads(r["payload"])
        for r in await fetchall(
            db,
            "SELECT payload FROM events WHERE task_id=? AND kind='steward_run_refused'",
            (task_id,),
        )
    ]
    assert events and events[-1]["retryable"] is True
    assert "выкладка" in events[-1]["detail"]
    assert local_reviewer.not_ready() == [], "дренаж — не ошибка готовности"

    # Маркер снят — тот же заказ стартует; задание в очереди деплой считает.
    (spool / "draining").unlink()
    svc.hold = True
    assert await start_advisor_run(db, dict(order)) is True
    for _ in range(100):
        if svc.jobs:
            break
        await asyncio.sleep(0.02)
    jobs = list(spool.glob("job-*"))
    assert len(jobs) == 1 and svc.jobs == [
        {"version": 3, "timeout_sec": 900, "profile": "advisor", "model": "glm-5.3"}
    ]
    counted = _run(["acquire"], spool, bin_dir, DRAIN_BUDGET_SECONDS="1")
    assert "drain timeout" in counted.stdout and jobs[0].name in counted.stdout
    _run(["release"], spool, bin_dir)
    from hub.services.steward_advisor_local import cancel_local_advisors

    await cancel_local_advisors()

    # Маркер появился ПОСЛЕ последней проверки, прямо перед публикацией: замок
    # при публикации отказывает, заказ возвращается в очередь, задание не
    # опубликовано (код выписан, но не использован — он истечёт сам).
    task3, order3 = await _ordered_advisor(db, "drain-at-publish")
    published_before = len(svc.jobs)
    dirs_before = {p.name for p in spool.glob("job-*")}
    (spool / "draining").write_text(f"owner=deploy-test\nexpires={expires}\n")
    monkeypatch.setattr(local_reviewer, "drain_refusal", lambda: "")
    assert await start_advisor_run(db, dict(order3)) is True
    await wait_for_local_advisors()
    back = await _advisor_row(db, task3)
    assert back["status"] == "open" and back["agent_id"] == "", back
    assert not back["started_at"], "started_at снят вместе с возвратом"
    assert len(svc.jobs) == published_before
    assert {p.name for p in spool.glob("job-*")} == dirs_before, "новое задание"
    (spool / "draining").unlink()

    # Общая суточная квота: исчерпана — советник не заказывается, причина названа.
    from tests.test_steward_shadow import _judge, _judge_run, _project, _task

    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 1)
    project_id = await _project(db, "drain-cap")
    task2 = await _task(db, project_id)
    await _judge_run(db, task2, model="composer-2.5")
    await _judge(db, task2)
    assert await order_due_advisors(db) == 0
    refused = [
        _json.loads(r["payload"])
        for r in await fetchall(
            db,
            "SELECT payload FROM events WHERE task_id=? AND kind='steward_run_refused'",
            (task2,),
        )
    ]
    assert [e["reason"] for e in refused if e.get("kind") == "advisor"] == ["daily_cap"]
