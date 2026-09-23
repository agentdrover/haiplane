"""Run a task's declared validation_commands and record the result (#509).

validation_commands were declared as "the commands that prove the change works"
but the hub never ran them. This executes them in the project workspace and
records pass/fail stamped with the submission_generation, so #510 can gate
completion on them. The runner is injectable for testing. Secret env vars are
stripped from the child environment so a command cannot read and echo them into
the recorded log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, Awaitable, Callable

from hub import repository as repo
from hub.services.orchestration import project_git_context
from hub.process_kill import kill_process_group

log = logging.getLogger("hub")

# A validation outcome, not a credential — bandit's B105 matches the name alone.
PASS = "pass"  # nosec B105
FAIL = "fail"
SKIPPED = "skipped"
UNKNOWN = "unknown"

_RUN_TIMEOUT = 300
_LOG_TAIL = 4000
# Bytes kept in memory per command. The tail is 4000 chars, but the whole
# stream used to be buffered before truncation — a chatty or looping command
# can emit gigabytes inside the timeout window and OOM the whole hub process,
# not just its own task. Read up to the cap, drain the rest without keeping it.
_MAX_OUTPUT = 1_000_000

# runner(commands, repo_path) -> (returncode, log_tail) or None if it could not run.
ValidationRunner = Callable[[list[str], str | None], Awaitable[tuple[int, str] | None]]

_SECRET_HINTS = ("TOKEN", "SECRET", "PASSWORD", "KEY", "CREDENTIAL")


def _safe_env() -> dict[str, str]:
    """Child env with obvious secrets stripped (#509 AC-2, best-effort)."""
    return {
        k: v
        for k, v in os.environ.items()
        if not any(hint in k.upper() for hint in _SECRET_HINTS)
    }


async def _collect(proc: Any) -> tuple[bytes, int]:
    """Read the child's output up to ``_MAX_OUTPUT``, then reap it (#509).

    Everything past the cap is read and discarded rather than buffered: we must
    keep draining, otherwise the child blocks on a full pipe and only dies at
    the timeout. Returns the kept bytes and how many were dropped.
    """
    kept: list[bytes] = []
    size = 0
    dropped = 0
    while True:
        chunk = await proc.stdout.read(65536)
        if not chunk:
            break
        room = _MAX_OUTPUT - size
        if room > 0:
            kept.append(chunk[:room])
            size += min(room, len(chunk))
        dropped += max(0, len(chunk) - max(room, 0))
    await proc.wait()
    return b"".join(kept), dropped


async def default_validation_runner(
    commands: list[str], repo_path: str | None
) -> tuple[int, str] | None:
    """Run ``commands`` in ``repo_path`` in order, stop at the first failure."""
    if not commands or not repo_path:
        return None
    env = _safe_env()
    logs: list[str] = []
    rc = 0
    for cmd in commands:
        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                # Own session, so the timeout path can signal the whole group
                # rather than just the shell we spawned (#544).
                start_new_session=True,
            )
            out, dropped = await asyncio.wait_for(_collect(proc), timeout=_RUN_TIMEOUT)
        except (OSError, TimeoutError, asyncio.TimeoutError):
            # wait_for cancels only the await — the command keeps running in the
            # workspace, and every retry leaks another one. Kill the group: the
            # shell may not have exec'd the payload, in which case killing its
            # pid alone leaves the real command alive (#544).
            await kill_process_group(proc)
            log.warning("validation command failed to run in %s", repo_path)
            return None
        tail = f"\n[... {dropped} bytes dropped]" if dropped else ""
        logs.append(f"$ {cmd}\n{out.decode(errors='replace')}{tail}")
        rc = proc.returncode or 0
        if rc != 0:
            break
    return rc, "\n".join(logs)[-_LOG_TAIL:]


# ---- #1332: фиксированный профиль хоста для проверки автомержа ----
#
# validation_commands пишет автор задачи под машину разработчика («uv run
# pytest», «make check»). На проде uv нет, а в venv сервиса нет ни pytest, ни
# ruff: 23.09.2026 первая же попытка автомержа (#1242) упала кодом 127, и
# человек прочёл «валидация упала». Решение владельца — вариант (б): хост
# проверяет сложенное тем, что у него ЗАВЕДОМО есть, а поведение слитого кода
# проверяет CI на новой вершине (гейт ждёт его следующим циклом).

# Код возврата shell для «команда не найдена». Профиль отдаёт его и тогда,
# когда exec не нашёл бинарь (OSError): для читателя это одно и то же.
COMMAND_NOT_FOUND_RC = 127
PROFILE_TIMEOUT_RC = 124
_PROFILE_TIMEOUT = 120
_PROFILE_LOG = 1500

# Компиляция в памяти: compile() не пишет .pyc, а -B страхует от импорта с
# записью. Список файлов идёт через stdin — у слияния с большой базой он может
# не влезть в argv. Выход 1 и строки «путь:строка: сообщение» — сломанный файл.
_COMPILE_SNIPPET = """
import sys
bad = []
for rel in sys.stdin.read().splitlines():
    if not rel:
        continue
    try:
        with open(rel, "rb") as fh:
            compile(fh.read(), rel, "exec", dont_inherit=True)
    except SyntaxError as exc:
        bad.append(f"{rel}:{exc.lineno}: {exc.msg}")
    except (OSError, ValueError) as exc:
        bad.append(f"{rel}: {exc}")
print("\\n".join(bad))
sys.exit(1 if bad else 0)
"""


def _profile_python() -> str:
    """Python для компиляции — тот, на котором работает сам хаб (#1332).

    Он есть везде, где работает гейт (на проде — python из venv сервиса),
    и это та же версия языка, под которой код будет исполняться. Искать
    «python3» по PATH значило бы проверять другим интерпретатором, чем запускать.
    """
    return sys.executable


async def _profile_exec(
    argv: list[str], cwd: str, stdin: bytes = b""
) -> tuple[int, str]:
    """Запустить инструмент профиля без shell; «не найден» — отдельный код.

    Без shell нет и второго слоя подстановок: имя, которое не нашлось, — это
    ровно ``argv[0]``, и отказ называет его. Окружение — ``_safe_env``: секреты
    в проверку не попадают, как и в #509.
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_safe_env(),
            start_new_session=True,
        )
        out, _ = await asyncio.wait_for(
            proc.communicate(stdin), timeout=_PROFILE_TIMEOUT
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        if exc.filename not in (None, argv[0]):
            # Не найдено ДЕРЕВО, а не команда: exec винит cwd тем же
            # исключением, и назвать это средой значило бы соврать.
            return 1, f"{argv[0]} не запущен: {exc.filename} ({exc.strerror})"
        return COMMAND_NOT_FOUND_RC, f"команда не найдена: {argv[0]}"
    except (TimeoutError, asyncio.TimeoutError):
        await kill_process_group(proc)
        return PROFILE_TIMEOUT_RC, (
            f"{argv[0]} не уложился в {_PROFILE_TIMEOUT} с и остановлен"
        )
    except OSError as exc:
        await kill_process_group(proc)
        return COMMAND_NOT_FOUND_RC, f"команда не запустилась: {argv[0]} ({exc})"
    except BaseException:
        # Отмена тика поллера на остановке сервиса приходит CancelledError и
        # мимо веток выше пролетала: дочерний процесс доживал сиротой. Группу
        # гасим, отмену пробрасываем дальше — как #544 (находка
        # 2ce63622f499a563).
        await asyncio.shield(kill_process_group(proc))
        raise
    rc = proc.returncode or 0
    text = (out or b"")[:_MAX_OUTPUT].decode(errors="replace").strip()
    if rc == COMMAND_NOT_FOUND_RC and not text:
        text = f"команда не найдена: {argv[0]}"
    return rc, text


async def _merge_produced_paths(path: str) -> tuple[list[str] | None, int, str]:
    """Пути, которых нет ни у одной из сторон мержа — их сложил сам гейт.

    Файл, совпадающий с одной из сторон, уже проверен CI этой стороны; новым
    здесь может быть только то, что отличается от ОБЕИХ: разрешённые хвосты и
    файлы, которые git слил сам. Дерево — незавершённый мерж: HEAD — коммит
    сдачи, MERGE_HEAD — база, индекс — сложенное.
    """
    sides: list[set[str]] = []
    for parent in ("HEAD", "MERGE_HEAD"):
        rc, out = await _profile_exec(
            ["git", "diff", "--cached", "--name-only", "-z", "--no-renames", parent],
            path,
        )
        if rc != 0:
            return None, rc, out
        sides.append({p for p in out.split("\0") if p})
    return sorted(sides[0] & sides[1]), 0, ""


async def host_profile_runner(repo_path: str | None) -> tuple[int, str] | None:
    """Проверить сложенное дерево автомержа ФИКСИРОВАННЫМ профилем хоста (#1332).

    Две проверки, обе — средствами, которые есть везде, где работает гейт:
    ``git diff --check`` (маркеры конфликта, ошибки пробелов) и синтаксическая
    компиляция изменённых .py интерпретатором самого хаба. Судим по коду
    возврата; лог — первое, что пойдёт в отказ, поэтому в нём имя файла или
    имя ненайденной команды. Поведение этот профиль НЕ проверяет — это делает
    CI на новой вершине, и гейт ждёт его зелёным следующим циклом.
    """
    if not repo_path or not os.path.isdir(repo_path):
        return None
    paths, rc, why = await _merge_produced_paths(repo_path)
    if paths is None:
        # Код возврата git уходит наверх как есть: 127 — это «git не найден»,
        # и отказ назовёт его дефектом среды, а не сломанным деревом.
        return rc or 1, f"сложенные файлы не перечислены: {why}"[:_PROFILE_LOG]
    if not paths:
        return 0, "сложенных файлов нет — проверять нечего"
    rc, out = await _profile_exec(
        ["git", "diff", "--cached", "--check", "HEAD", "--", *paths], repo_path
    )
    if rc != 0:
        if rc not in (COMMAND_NOT_FOUND_RC, PROFILE_TIMEOUT_RC):
            out = f"git diff --check: {out}"
        return rc, out[:_PROFILE_LOG]
    present = [p for p in paths if os.path.isfile(os.path.join(repo_path, p))]
    sources = [p for p in present if p.endswith(".py")]
    if not sources:
        return 0, f"git diff --check чисто ({len(paths)} ф.); .py среди сложенного нет"
    python = _profile_python()
    rc, out = await _profile_exec(
        [python, "-I", "-B", "-c", _COMPILE_SNIPPET],
        repo_path,
        stdin="\n".join(sources).encode(),
    )
    if rc != 0:
        if rc not in (COMMAND_NOT_FOUND_RC, PROFILE_TIMEOUT_RC):
            out = f"компиляция .py не прошла: {out}"
        return rc, out[:_PROFILE_LOG]
    return 0, (
        f"git diff --check чисто, компиляция {len(sources)} .py ({python}) чисто"
    )


async def record_validation_result(
    db: Any,
    task_id: int,
    *,
    generation: int,
    status: str,
    log_tail: str = "",
) -> dict:
    """Write the validation outcome for ``generation``. Does NOT commit (#546).

    The one write path for validation results, shared by the local runner
    (#509) and the CI report intake (#546). Callers own the transaction so the
    submission path can land these fields inside its own write lock.
    """
    await repo.update_task(
        db,
        task_id,
        validation_generation=generation,
        validation_status=status,
        validation_log=log_tail,
    )
    return {"status": status, "generation": generation}


async def run_validation_commands(
    db: Any,
    task_id: int,
    *,
    runner: ValidationRunner | None = None,
) -> dict:
    """Run a task's validation_commands and record the result (#509).

    Records pass/fail (or unknown when it could not run) stamped with the
    current submission_generation. When there are no commands the step is
    skipped and nothing is recorded (AC-3).
    """
    runner = runner or default_validation_runner
    row = await repo.get_task(db, task_id)
    if not row:
        return {"status": SKIPPED, "generation": 0}
    task = dict(row)
    commands = json.loads(task.get("validation_commands") or "[]")
    generation = task.get("submission_generation") or 0
    if not commands:
        return {"status": SKIPPED, "generation": generation}

    ctx = await project_git_context(db, task_id)
    result = await runner(commands, ctx.get("repo"))
    if result is None:
        status, log_tail = UNKNOWN, ""
    else:
        rc, log_tail = result
        status = PASS if rc == 0 else FAIL
    out = await record_validation_result(
        db, task_id, generation=generation, status=status, log_tail=log_tail
    )
    await db.commit()
    return out


def validation_gap(task: dict) -> str | None:
    """None when the current validation result is green; else a reason (#510).

    A gap exists when the task has validation_commands but the recorded result
    is missing / stale (wrong generation) / not ``pass``. No commands ⇒ no gap.
    """
    commands = task.get("validation_commands")
    if isinstance(commands, str):
        commands = json.loads(commands or "[]")
    if not commands:
        return None
    generation = task.get("submission_generation") or 0
    if task.get("validation_generation") != generation:
        return "validation_commands не прогонялись для текущего поколения"
    if task.get("validation_status") != PASS:
        return f"validation_commands не прошли: статус {task.get('validation_status')}"
    return None
