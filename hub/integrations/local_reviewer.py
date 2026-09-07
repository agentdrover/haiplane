"""Локальный ревьюер: агентский CLI под песочницей, на любом форже (#1180).

Зачем модуль существует. Хаб умеет звать ровно одного ревьюера — облачного
агента Cursor по HTTP, — и до GitVerse он не дотягивается: одинаковое тело
запроса, адрес gitverse.ru даёт 400, git@gitverse.ru — 500, контрольный
github.com — 201 (измерено 31.08.2026, #1119). Следствие наблюдалось на живой
задаче #1128: настоящий прогон харнесса с семью находками был сдан
принципалом автора, гейт его не засчитал при ``REVIEW_SELF_APPROVE=forbid``, и
задача простояла девять часов без вердикта.

Что этот модуль делает и чего НЕ делает. Он ЗАПУСКАЕТ процесс и отдаёт то, что
процесс написал. Ни промта, ни отчёта, ни политики он не знает: промт собирает
``review_dispatch`` — тот же самый, что уходит в облако, — а отчёт ревьюер
сдаёт сам, по HTTP, тем же контрактом (#1084). Поставщик здесь тоже не
зашит: ``cursor-agent`` — одна из реализаций, и вписывать её в код значило бы
переписывать код вместе с поставщиком.

РЕШЕНИЕ ОБ ИЗОЛЯЦИИ, принятое явно (постановка #1180 требовала назвать его
с причиной; разбор и порядок выката — deploy/LOCAL-REVIEW.md):

* **Отдельный пользователь и лимиты ресурсов, а не контейнер.** Ревьюер — это
  агентский CLI общего назначения: он умеет запускать команды и писать файлы.
  Промт харнесса запрещает ему лазить по репозиторию, но это ПРОСЬБА, а не
  песочница. От трёх видов вреда из четырёх (чтение секретов, порча рабочего
  клона, съедание CPU и памяти) защищают отдельный unix-пользователь и слайс с
  лимитами; контейнер добавляет к этому только изоляцию ЗАВИСИМОСТЕЙ — и
  делит с хабом ядро, диск и процессор, то есть «прод точно не пострадает» не
  обещает всё равно.
* **Песочница обязательна.** Пустая ``LOCAL_REVIEW_SANDBOX`` означает «пути
  нет», а не «запускай как есть»: настройка, о которой только заявлено,
  защитой не является, а хост несёт secrets.env, ключ Cursor и deploy key.
* **Клона ревьюеру не даётся вовсе.** Постановка предлагала одноразовый клон;
  чтение кода показало, что можно сильнее. Дифф уезжает ревьюеру инлайном в
  промте, а предмет ревью он читает по HTTP через ``review-brief``, который
  берёт данные из клона ХАБА и к форжу не ходит (проверено вызовом на
  GitVerse-задаче #1138). То, чего процессу не дали, испортить нельзя.
* **Окружение собирается белым списком.** ``os.environ.copy()`` унёс бы в
  чужой процесс ключ Cursor, токены хаба и путь к рабочему клону. Здесь
  наследуются четыре переменные окружения, и каждая названа поимённо.
* **Промт уходит в STDIN.** Аргументы процесса видны в ``ps`` любому
  пользователю хоста, а промт несёт одноразовый код доступа к хабу.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from hub import config
from hub.process_kill import kill_process_group

log = logging.getLogger(__name__)

# Сколько хвоста вывода мы вообще держим в памяти. Отчёт приходит по HTTP;
# вывод нужен на два случая — восстановить отчёт из текста, если контрактный
# путь не сработал (#1036), и назвать причину в ленте. Ни для того, ни для
# другого мегабайты не нужны, а неограниченное чтение чужого stdout — это
# способ уронить хаб по памяти.
OUTPUT_CAP = 200_000

# Тот же код, что у git и gh (integrations/proc.py): шелловая конвенция для
# снятого по таймауту, отличимая от любого кода, который вернул бы сам CLI.
TIMEOUT_RC = 124

# Переменные, которые процесс наследует, и ничего кроме. HOME и TMPDIR
# указывают в каталог-однодневку: агентские CLI пишут туда кэш и временные
# файлы, и без этого они пойдут писать в домашний каталог хаба.
_ENV_PASSTHROUGH: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "LC_CTYPE")


@dataclass(frozen=True)
class LocalRun:
    """Что вернул прогон. ``timed_out`` — это НЕ ``rc != 0``.

    Разница несущая: упавший ревьюер сказал о себе хоть что-то, снятый по
    таймауту не сказал ничего, и в ленте это две разные причины. Слить их в
    одну значило бы отправить человека читать пустой вывод в поисках ошибки,
    которой там нет.

    ``output`` — оба потока вместе, обрезанные ДО ``OUTPUT_CAP`` (найдено
    ревью, находка 30a65c79): раздельные stdout и stderr означали бы двух
    читателей и вдвое больше способов заблокировать ребёнка на полной трубе,
    а разделять их всё равно некому — обе стороны идут в одну и ту же ленту.
    ``dropped`` считает выброшенное, потому что «вывод кончился» и «вывод
    обрезан» — разные факты.
    """

    rc: int
    output: str
    dropped: int
    timed_out: bool
    duration_ms: int


def not_ready() -> list[str]:
    """Имена настроек, без которых локального пути нет. Пустой список = есть.

    Имена, никогда не значения (#1083): в карточку задачи уходит название
    настройки, которое и так написано открытым текстом в hub/config.py. Ни
    значения, ни префикса, ни длины — длина это суженная догадка.
    """
    return [
        name
        for name, value in (
            ("LOCAL_REVIEW_CMD (команда агентского CLI)", config.LOCAL_REVIEW_CMD),
            ("LOCAL_REVIEW_SANDBOX (песочница запуска)", config.LOCAL_REVIEW_SANDBOX),
            (
                "LOCAL_REVIEW_SCRATCH_DIR (каталог для одноразовых прогонов)",
                config.LOCAL_REVIEW_SCRATCH_DIR,
            ),
            (
                "LOCAL_REVIEWER_HUB_TOKEN (токен принципала ревьюера)",
                config.LOCAL_REVIEWER_HUB_TOKEN,
            ),
        )
        if not (value or "").strip()
    ] + detaching_sandbox()


# Песочница, которая ОТСОЕДИНЯЕТ полезную нагрузку от хаба (найдено ревью,
# находка a6aaffbc). ``systemd-run`` без ``--scope`` поднимает transient
# service: родителем CLI становится PID 1, наш процесс — всего лишь клиент,
# и SIGKILL по его группе юнит не останавливает. Хаб при этом честно
# напишет в ленту «процесс и вся его группа убиты» — то есть скажет
# неправду, а ревьюер продолжит жечь CPU и сможет прислать отчёт по уже
# закрытому прогону. Ровно класс #509/#544, только на чужом менеджере
# процессов.
#
# Проверка узкая и по имени: она знает ОДИН инструмент и ОДИН его флаг,
# потому что это факт про systemd-run, а не догадка про песочницы вообще.
# Всё, чего она не знает, она пропускает — и об этом сказано в документе
# выката прямым требованием к обёртке.
_DETACHING_HINT = (
    "LOCAL_REVIEW_SANDBOX: systemd-run без --scope запускает transient "
    "service — полезная нагрузка становится потомком PID 1, и снять её по "
    "таймауту хаб не сможет, хотя напишет в ленту, что снял. Добавьте "
    "--scope (тогда CLI остаётся прямым потомком хаба и наследует cwd, "
    "окружение и stdin) — см. deploy/LOCAL-REVIEW.md"
)


def detaching_sandbox() -> list[str]:
    """Названная причина, если обёртка запуска отсоединяет процесс."""
    parts = shlex.split(config.LOCAL_REVIEW_SANDBOX or "")
    runs_unit = any(part.rsplit("/", 1)[-1] == "systemd-run" for part in parts)
    if runs_unit and "--scope" not in parts:
        return [_DETACHING_HINT]
    return []


def is_configured() -> bool:
    return not not_ready()


def argv() -> list[str]:
    """Полная командная строка: песочница, затем сам CLI."""
    return shlex.split(config.LOCAL_REVIEW_SANDBOX) + shlex.split(
        config.LOCAL_REVIEW_CMD
    )


def _clean_env(workdir: str) -> dict[str, str]:
    env = {name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = workdir
    env["TMPDIR"] = workdir
    return env


async def run_review(prompt: str, *, timeout: int | None = None) -> LocalRun | None:
    """Прогнать ревьюера над готовым промтом. ``None`` — запуска не было.

    ``None`` возвращается ровно тогда, когда процесс не удалось породить: нет
    настроек, нет каталога, нет бинаря. Это не «ревью ничего не нашло» и не
    «ревью упало» — вызывающий обязан назвать это в ленте отдельной причиной,
    иначе «не запускалось» прочитается как «прочитано и чисто», а это и есть
    класс дефектов, ради которого всё здесь написано.
    """
    if not is_configured():
        return None
    limit = timeout if timeout is not None else config.LOCAL_REVIEW_TIMEOUT_SEC
    base = config.LOCAL_REVIEW_SCRATCH_DIR.strip()
    try:
        os.makedirs(base, exist_ok=True)
        workdir = tempfile.mkdtemp(prefix="haiplane-review-", dir=base)
        # Каталог создаёт ХАБ, а работать в нём чужому пользователю (найдено
        # ревью, находка 92eba4f8 — и это регрессия, которую открыл фикс
        # предыдущей). Пока песочница отсоединяла процесс, домом ревьюера был
        # его passwd-home и права этого каталога никого не задевали. С --scope
        # cwd, HOME и TMPDIR доезжают по-настоящему — а mkdtemp всегда даёт
        # 0700 владельца-создателя, то есть ревьюер получил бы EACCES на
        # собственный рабочий каталог и упал бы, не начав.
        #
        # 0770, а не 0777: доступ даётся ГРУППЕ, общей у хаба и ревьюера, —
        # setgid на родителе (2770) проставляет её сам. Права «всем» открыли
        # бы промт с одноразовым кодом любому пользователю хоста.
        os.chmod(workdir, 0o770)  # nosec B103 - права даны ГРУППЕ, не миру
    except OSError as exc:
        log.warning("local reviewer: no scratch dir under %s: %s", base, exc)
        return None
    started = time.monotonic()
    try:
        return await _spawn(prompt, workdir, limit, started)
    except (OSError, ValueError) as exc:
        # Нет бинаря, нет прав, пустая команда. Возврат None, а не исключение:
        # сорвавшийся ревьюер не имеет права ронять сдачу — это тот же
        # контракт деградации, которым живёт облачный клиент.
        log.warning("local reviewer could not start: %s", exc)
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _spawn(prompt: str, workdir: str, limit: int, started: float) -> LocalRun:
    proc = await asyncio.create_subprocess_exec(
        *argv(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        # Оба потока в одну трубу: читателя два — значит и способов
        # заблокировать ребёнка на полной трубе два, а разделять вывод
        # некому — обе стороны идут в одну ленту.
        stderr=asyncio.subprocess.STDOUT,
        cwd=workdir,
        env=_clean_env(workdir),
        # Своя сессия процесса — обязательное условие kill_process_group:
        # без неё группа у ребёнка общая с хабом, и убийство группы убило бы
        # сам хаб (#544).
        start_new_session=True,
    )
    try:
        out, dropped = await asyncio.wait_for(_pump(proc, prompt), timeout=limit)
    except asyncio.TimeoutError:
        await kill_process_group(proc)
        return LocalRun(
            rc=TIMEOUT_RC,
            output="",
            dropped=0,
            timed_out=True,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except asyncio.CancelledError:
        # Хаб останавливают или прогон отменяют (найдено ревью, находка
        # d478b896). CancelledError наследует BaseException и мимо
        # перехвата таймаута проходит насквозь — а ревьюер остаётся жить,
        # уже никем не досматриваемый. Снимаем группу и отдаём отмену
        # дальше: глотать её нельзя, это чужое решение остановиться.
        await kill_process_group(proc)
        raise
    return LocalRun(
        rc=proc.returncode or 0,
        output=out.decode(errors="replace"),
        dropped=dropped,
        timed_out=False,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def _pump(proc: Any, prompt: str) -> tuple[bytes, int]:
    """Скормить промт и вычитать вывод, не дав ребёнку встать на трубе.

    Одновременно, а не по очереди: промт больше буфера трубы (64 КБ), и
    последовательная запись встала бы до того, как ревьюер начнёт читать, —
    а он не начнёт, пока мы не дочитаем его вывод.
    """
    feeding = asyncio.create_task(_feed(proc, prompt))
    try:
        return await _collect(proc)
    finally:
        feeding.cancel()


async def _feed(proc: Any, prompt: str) -> None:
    """Отдать промт в stdin и закрыть его. Отказ ревьюера читать — не ошибка."""
    try:
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, AttributeError):
        pass
    finally:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            proc.stdin.close()


async def _collect(proc: Any) -> tuple[bytes, int]:
    """Читать вывод до ``OUTPUT_CAP``, остальное сливать и считать (#509).

    Именно СЛИВАТЬ, а не перестать читать: замолчавший читатель оставляет
    ребёнка стоять на полной трубе, и тот умрёт только по таймауту — то есть
    экономия памяти обернулась бы получасом впустую. Приём взят у
    validation_run._collect, где этот же дефект уже закрыт.
    """
    kept: list[bytes] = []
    size = 0
    dropped = 0
    while True:
        chunk = await proc.stdout.read(65536)
        if not chunk:
            break
        room = OUTPUT_CAP - size
        if room > 0:
            kept.append(chunk[:room])
            size += min(room, len(chunk))
        dropped += max(0, len(chunk) - max(room, 0))
    await proc.wait()
    return b"".join(kept), dropped
