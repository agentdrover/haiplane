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
import grp
import os
import pwd
import shlex
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from typing import Any, NamedTuple

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
    return (
        [
            name
            for name, value in (
                ("LOCAL_REVIEW_CMD (команда агентского CLI)", config.LOCAL_REVIEW_CMD),
                (
                    "LOCAL_REVIEW_SANDBOX (песочница запуска)",
                    config.LOCAL_REVIEW_SANDBOX,
                ),
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
        ]
        + detaching_sandbox()
        + scratch_problem()
    )


# Песочница, из-под которой хаб не может СНЯТЬ полезную нагрузку (найдено
# ревью, находка a6aaffbc; расширено выкатом 08.09.2026, #1208).
#
# Правило, которое здесь проверяется, звучит так: обёртка обязана ЛИБО
# оставлять CLI прямым потомком хаба, ЛИБО иметь СОБСТВЕННЫЙ срок жизни.
# Первая редакция требовала только потомка — и это требование недостижимо
# для контейнеров: ``podman run`` переживает убийство группы процессов
# (измерено на проде 08.09.2026: kill -KILL по группе, через 7 с контейнер
# «Up»), потому что conmon намеренно отсоединён от клиента podman. Ровно то
# же делает ``systemd-run`` без ``--scope``: родителем становится PID 1.
#
# Опасен здесь не сам факт отсоединения, а то, что хаб при этом напишет в
# ленту «процесс и вся его группа убиты» — то есть скажет неправду, а
# ревьюер продолжит жечь CPU и сможет прислать отчёт по уже закрытому
# прогону. Свой срок жизни закрывает дыру с другой стороны: podman с
# ``--timeout`` убивает контейнер сам (измерено: ``--timeout 5`` — через
# 12 с живых контейнеров нет, хотя убийство группы до него не дошло).
#
# Проверка узкая и ПО ИМЕНИ: она знает названные инструменты и названные
# флаги, потому что это факты про systemd-run, podman и docker, а не
# догадка про песочницы вообще. Всё, чего она не знает, она пропускает — и
# об этом сказано в документе выката прямым требованием к обёртке. В
# частности, обёртка-скрипт, внутри которой лежит podman, для этой проверки
# невидима, и срок жизни в ней — на совести того, кто её написал.
_SCOPE_HINT = (
    "LOCAL_REVIEW_SANDBOX: systemd-run без --scope запускает transient "
    "service — полезная нагрузка становится потомком PID 1, и снять её по "
    "таймауту хаб не сможет, хотя напишет в ленту, что снял. Добавьте "
    "--scope (тогда CLI остаётся прямым потомком хаба и наследует cwd, "
    "окружение и stdin) — см. deploy/LOCAL-REVIEW.md"
)
_PODMAN_HINT = (
    "LOCAL_REVIEW_SANDBOX: podman run без --timeout отсоединяет контейнер "
    "от хаба — conmon переживает убийство группы процессов (измерено "
    "08.09.2026: через 7 с контейнер «Up»), и снять прогон по таймауту хаб "
    "не сможет, хотя напишет в ленту, что снял. Добавьте --timeout "
    "<секунды> — с ним контейнер умирает сам — см. deploy/LOCAL-REVIEW.md"
)
_PODMAN_DEADLINE_HINT = (
    "LOCAL_REVIEW_SANDBOX: podman run --timeout «{value}» срока жизни "
    "контейнеру НЕ задаёт. Срок — целое число секунд больше нуля; ноль это "
    "подман-умолчание, а умолчание задокументировано словами «By default "
    "containers run until they exit or are stopped by podman stop» "
    "(podman-run(1)). Токен в строке есть, а снять прогон по таймауту хаб "
    "по-прежнему не сможет — тот же случай, что --timeout после образа. "
    "Поставьте --timeout <секунды> — см. deploy/LOCAL-REVIEW.md"
)
_PODMAN_UNKNOWN_FLAG_HINT = (
    "LOCAL_REVIEW_SANDBOX: в строке podman run стоит флаг «{flag}», про "
    "который страж не знает, берёт ли тот значение отдельным токеном. "
    "Разобрать строку до образа поэтому нельзя, а значит нельзя и сказать, "
    "есть ли у контейнера свой срок жизни: за проглоченным значением может "
    "стоять ещё один --timeout, и действующим будет он. Это НЕ разрешение "
    "работать — стражу «не знаю» и «всё в порядке» не одно и то же. "
    "Напишите флаг в форме {flag}=<значение> — она однозначна и от списков "
    "не зависит — либо уберите его из строки песочницы, спрятав podman "
    "внутрь враппера-скрипта, как советует deploy/LOCAL-REVIEW.md"
)
_DOCKER_HINT = (
    "LOCAL_REVIEW_SANDBOX: docker run отсоединяет контейнер от хаба так же, "
    "как podman, но флага собственного срока жизни (аналога podman "
    "--timeout) у него нет вовсе — недостающий флаг здесь дописать некуда. "
    "Возьмите podman run --timeout <секунды> либо обёртку, оставляющую CLI "
    "прямым потомком хаба — см. deploy/LOCAL-REVIEW.md"
)


# Глобальные флаги движка, которые берут значение ОТДЕЛЬНЫМ токеном. Без них
# ``podman --log-level debug run --rm -i img`` читается как подкоманда
# «debug», запуск контейнера стражу невидим и проходит без своего срока жизни
# (найдено машинным ревью 09.09.2026, находка 2e24a6068bbc3fa1). Список ПО
# ИМЕНИ, как и всё в этом модуле: глобальный флаг, которого здесь нет, съест
# подкоманду по-прежнему — это названный пропуск, он записан в
# deploy/LOCAL-REVIEW.md. Форма ``--flag=value`` от списка не зависит вовсе.
_ENGINE_GLOBAL_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "podman": frozenset(
        {
            "--cgroup-manager",
            "--conmon",
            "--connection",
            "-c",
            "--db-backend",
            "--events-backend",
            "--hooks-dir",
            "--identity",
            "--imagestore",
            "--log-level",
            "--module",
            "--namespace",
            "--network-cmd-path",
            "--root",
            "--runroot",
            "--runtime",
            "--runtime-flag",
            "--ssh",
            "--storage-driver",
            "--storage-opt",
            "--tmpdir",
            "--url",
            "--volumepath",
        }
    ),
    "docker": frozenset(
        {
            "--config",
            "--context",
            "-c",
            "--host",
            "-H",
            "--log-level",
            "-l",
            "--tlscacert",
            "--tlscert",
            "--tlskey",
        }
    ),
}

# Флаги самого ``<engine> run``, которые берут значение ОТДЕЛЬНЫМ токеном.
# Нужны, чтобы найти ОБРАЗ: всё, что стоит после образа, — команда полезной
# нагрузки, и её ``--timeout`` сроком жизни контейнера не является (найдено
# машинным ревью 09.09.2026, находка 2e24a6068bbc3fa1: ``podman run --rm -i
# img cursor-agent --timeout 60`` проходил как запуск со своим сроком).
#
# Прежняя редакция этого комментария утверждала, что неизвестный флаг можно
# считать булевым, потому что «ошибка тогда идёт в сторону отказа». Это
# НЕВЕРНО, и измерено 10.09.2026 на ``podman run --timeout 60 --blkio-weight
# 500 --timeout 0 img``: ``--blkio-weight`` в списке не назван, ``500``
# принято за образ, разбор кончился — и ВТОРОЙ ``--timeout 0`` не увиден, а
# у podman действует последний. Страж пропустил контейнер без срока жизни,
# то есть ошибся ровно В СТОРОНУ ПРОПУСКА. Поэтому неизвестный флаг больше
# не угадывается: см. _RUN_VALUELESS_FLAGS и поле ``unknown`` у _RunFlags.
_RUN_VALUE_FLAGS: frozenset[str] = frozenset(
    {
        "--add-host",
        "--annotation",
        "--arch",
        "--cap-add",
        "--cap-drop",
        "--cgroup-parent",
        "--cgroups",
        "--cidfile",
        "--cpu-shares",
        "--cpus",
        "--cpuset-cpus",
        "--cpuset-mems",
        "--device",
        "--dns",
        "--entrypoint",
        "--env",
        "-e",
        "--env-file",
        "--gidmap",
        "--health-cmd",
        "--hostname",
        "-h",
        "--ipc",
        "--label",
        "-l",
        "--log-driver",
        "--log-opt",
        "--memory",
        "-m",
        "--memory-swap",
        "--name",
        "--network",
        "--os",
        "--pid",
        "--pids-limit",
        "--platform",
        "--publish",
        "-p",
        "--pull",
        "--restart",
        "--secret",
        "--security-opt",
        "--shm-size",
        "--stop-signal",
        "--stop-timeout",
        "--sysctl",
        "--timeout",
        "--tmpfs",
        "--tz",
        "--uidmap",
        "--ulimit",
        "--umask",
        "--user",
        "-u",
        "--userns",
        "--variant",
        "--volume",
        "-v",
        "--workdir",
        "-w",
    }
)


# Флаги ``<engine> run``, которые значения НЕ берут. Список нужен не ради
# полноты, а ради РАЗЛИЧЕНИЯ: «флаг известен и он булев» против «флаг мне не
# известен». Без него разбор угадывал, и угадывал в сторону пропуска (см.
# комментарий к _RUN_VALUE_FLAGS). Булевы флаги обоих движков разбираются
# spf13/pflag, а он значение отдельным токеном у булевого флага НЕ берёт —
# только форму ``--flag=value``; значит принять их за беззначные безопасно.
_RUN_VALUELESS_FLAGS: frozenset[str] = frozenset(
    {
        "--detach",
        "-d",
        "--help",
        "--http-proxy",
        "--init",
        "--interactive",
        "-i",
        "--no-healthcheck",
        "--no-hosts",
        "--oom-kill-disable",
        "--passwd",
        "--privileged",
        "--publish-all",
        "-P",
        "--quiet",
        "-q",
        "--read-only",
        "--replace",
        "--rm",
        "--rmi",
        "--sig-proxy",
        "--tls-verify",
        "--tty",
        "-t",
        "--unsetenv-all",
    }
)
# Буквы слипшегося короткого пучка (``-it``, ``-ti``, ``-itd``), значения не
# берущие. Пучок с чужой буквой известным не считается.
_RUN_VALUELESS_SHORTS = frozenset("Pdiqt")


def _is_valueless_run_flag(token: str) -> bool:
    """Флаг ``run``, о котором ТОЧНО известно, что значения он не берёт."""
    if token in _RUN_VALUELESS_FLAGS:
        return True
    if token.startswith("--") or not token.startswith("-"):
        return False
    body = token[1:]
    return bool(body) and all(ch in _RUN_VALUELESS_SHORTS for ch in body)


class _RunFlags(NamedTuple):
    """Окно флагов ``<engine> run`` и признак того, что разбор не достоверен.

    ``unknown`` — имя первого флага, про который неизвестно, берёт ли он
    значение отдельным токеном. Пока он пуст, окну можно верить; как только
    он назван, судить по этому окну о сроке жизни контейнера НЕЛЬЗЯ: за
    проглоченным значением может стоять ещё один ``--timeout``, и именно он
    будет действующим.
    """

    flags: list[tuple[str, str | None]]
    unknown: str


def _container_run_flags(parts: list[str], engine: str) -> _RunFlags | None:
    """Флаги самого ``<engine> run`` — до образа. ``None``, если это не run.

    Возвращается именно окно флагов запуска, а не вся строка: судить о сроке
    жизни контейнера по токенам ПОСЛЕ образа нельзя, там уже команда внутри
    контейнера.

    Каждый флаг отдаётся ВМЕСТЕ СО ЗНАЧЕНИЕМ (``None``, если значения нет):
    у ``--timeout`` вопрос не «есть ли флаг», а «названо ли им время», и без
    значения на него не ответить (найдено машинным ревью 09.09.2026, находка
    fee582c0e918a6a6).
    """
    global_value_flags = _ENGINE_GLOBAL_VALUE_FLAGS.get(engine, frozenset())
    for i, part in enumerate(parts):
        if part.rsplit("/", 1)[-1] != engine:
            continue
        j = i + 1
        while j < len(parts) and parts[j].startswith("-"):
            j += 2 if parts[j] in global_value_flags else 1
        if j >= len(parts) or parts[j] != "run":
            # Не «движка в строке нет», а «ЭТОТ вызов — не run»: подкоманд у
            # движка много, и первая же не-run (``podman ps``) обрывала поиск
            # вовсе, оставляя настоящий ``podman run`` правее невидимым — то
            # есть отказ уходил В СТОРОНУ ПРОПУСКА (найдено машинным ревью
            # 10.09.2026, находка 96144e6317c1d7ac).
            continue
        own: list[tuple[str, str | None]] = []
        unknown = ""
        k = j + 1
        while k < len(parts) and parts[k].startswith("-"):
            if parts[k] == "--":
                # ``--`` кончает опции САМОГО run: следующий за ним токен —
                # образ, чего бы он ни напоминал. Прежде ``--`` числился
                # среди беззначных флагов, и разбор шёл дальше: на
                # ``podman run -- --timeout 1800 img`` страж видел срок
                # жизни, тогда как для podman образ здесь — ``--timeout``, а
                # срока нет вовсе. Ошибка шла В СТОРОНУ ПРОПУСКА (найдено
                # машинным ревью 10.09.2026, неразрешённая a58d77e268b2d709;
                # воспроизведено на ff617db и на HEAD 149cd825).
                break
            if parts[k] in _RUN_VALUE_FLAGS:
                own.append((parts[k], parts[k + 1] if k + 1 < len(parts) else None))
                k += 2
            else:
                name, sep, value = parts[k].partition("=")
                own.append((name, value if sep else None))
                # Форма ``--flag=value`` однозначна и от списков не зависит.
                # А вот голый флаг, которого нет НИ в одном из двух списков,
                # разбору не по зубам: следующий токен может быть и его
                # значением, и образом, и от выбора зависит, увидим ли мы
                # флаги правее. Запоминаем ПЕРВЫЙ такой — он и называется в
                # отказе.
                if not sep and not _is_valueless_run_flag(parts[k]):
                    unknown = unknown or parts[k]
                k += 1
        return _RunFlags(own, unknown)
    return None


_FLAG_ABSENT = object()


def _effective(occurrences: list[Any]) -> Any:
    """ДЕЙСТВУЮЩЕЕ вхождение повторённого флага — ПОСЛЕДНЕЕ. Нет — _FLAG_ABSENT.

    Правило одно на весь модуль, потому что одна и та же ошибка дала здесь
    уже три разных дефекта: страж судил ПЕРВОЕ вхождение, а инструменты
    берут ПОСЛЕДНЕЕ, и всё, что дописано правее, страж не видел вовсе.

    ЗАМЕРЕНО 10.09.2026 на машине разработки — оба семейства разборщиков:
    — spf13/pflag (podman, docker): ``docker context ls --format '{{.Name}}'
      --format 'LAST_WINS'`` печатает LAST_WINS, а обратный порядок печатает
      имена контекстов, то есть повторный Set перезаписывает значение;
    — getopt_long (sudo(8) parse_args.c, systemd-run): программа на C с
      ``case 'u': user = optarg`` даёт на ``-n -u alice -u bob`` — bob, на
      ``--user alice --user bob`` — bob, на ``-u bob -nu alice`` — alice.

    Живого podman и systemd-run на машине нет, и повтор флага у них САМИХ не
    замерялся: замерены их РАЗБОРЩИКИ, названные поимённо.
    """
    return occurrences[-1] if occurrences else _FLAG_ABSENT


def _flag_value(flags: list[tuple[str, str | None]], flag: str) -> Any:
    """Значение флага среди флагов run; ``_FLAG_ABSENT``, если флага нет.

    «Флага нет» и «флаг есть, а времени в нём нет» — разные отказы, и путать
    их нельзя: первый чинится дописыванием флага, второй — исправлением его
    значения, и подсказка обязана называть именно то, что делать.

    Повторённый флаг судится по ПОСЛЕДНЕМУ вхождению (_effective): на
    ``--timeout 1800 --timeout 0`` действует ноль, то есть срока жизни нет,
    а страж по первому вхождению пропускал такой запуск молча (найдено
    машинным ревью 09.09.2026, находка 04d3d4b0f7febee0).
    """
    return _effective([value for name, value in flags if name == flag])


def _names_a_deadline(value: str | None) -> bool:
    """Значение ``--timeout`` действительно задаёт срок жизни контейнера.

    Срок — целое число секунд БОЛЬШЕ НУЛЯ. Ноль сроком не является: это
    подман-умолчание, а умолчание задокументировано словами «By default
    containers run until they exit or are stopped by ``podman stop``»
    (podman-run(1)). То есть ``--timeout 0`` — тот же класс «токен в строке
    есть, срока жизни нет», что и ``--timeout`` после образа, и пропускать
    его значит вернуть ровно ту дыру, ради которой страж написан.
    """
    return value is not None and value.isdigit() and int(value) > 0


def detaching_sandbox() -> list[str]:
    """Названные причины, если обёртка запуска ни потомок, ни со своим сроком."""
    parts = shlex.split(config.LOCAL_REVIEW_SANDBOX or "")
    reasons: list[str] = []
    # ``--scope`` засчитывается только среди СОБСТВЕННЫХ аргументов
    # systemd-run: за ``--`` стоит полезная нагрузка, и её ``--scope``
    # transient service в scope не превращает. Взгляд на всю строку давал
    # отказ В СТОРОНУ ПРОПУСКА — ``systemd-run --quiet --pipe --uid=x --
    # /wrap --scope`` проходил молча (найдено машинным ревью 10.09.2026,
    # находка 3d5938a9a645c39d; воспроизведено на HEAD 21629cde).
    if any(
        window.tool == "systemd-run"
        # Именно среди ФЛАГОВ окна, а не среди его токенов: ``--scope``,
        # съеденный как значение соседа, флагом не является (неразрешённая
        # 5a59af8d620685d0).
        and not any(name == "--scope" for name, _ in window.flags)
        for window in _wrapper_windows(parts)
    ):
        reasons.append(_SCOPE_HINT)
    podman_flags = _container_run_flags(parts, "podman")
    if podman_flags is not None and podman_flags.unknown:
        # Неизвестный флаг со значением отменяет ЛЮБОЕ суждение о сроке, а не
        # только положительное: он мог проглотить и сам --timeout, и тогда
        # «допишите --timeout» — совет починить то, что уже написано. Честный
        # отказ называет флаг, который сорвал разбор (измерено 10.09.2026 на
        # ``podman run --timeout 60 --blkio-weight 500 --timeout 0 img``:
        # страж пропускал строку, где действующий срок — ноль).
        reasons.append(_PODMAN_UNKNOWN_FLAG_HINT.format(flag=podman_flags.unknown))
    elif podman_flags is not None:
        deadline = _flag_value(podman_flags.flags, "--timeout")
        if deadline is _FLAG_ABSENT:
            reasons.append(_PODMAN_HINT)
        elif not _names_a_deadline(deadline):
            # Значения нет вовсе (флаг последним токеном) — так и сказать, а
            # не показать оператору «None», которого он в строке не писал.
            shown = "<значения нет>" if deadline is None else deadline
            reasons.append(_PODMAN_DEADLINE_HINT.format(value=shown))
    if _container_run_flags(parts, "docker") is not None:
        reasons.append(_DOCKER_HINT)
    return reasons


# Пользователь песочницы записывается РАЗНЫМИ инструментами по-разному, и
# знание одной формы означает, что на другой конфигурации проверка группы
# каталога прогонов молча не срабатывает вовсе (найдено выкатом 08.09.2026,
# #1208: рабочий рецепт использует ``sudo -n -u``, а страж знал только
# ``--uid=`` от systemd-run).
#
# Карта по имени инструмента, а не список флагов вообще: ``--user`` у podman
# и docker означает пользователя ВНУТРИ контейнера, а не на хосте, и принять
# его за хостового значило бы проверить членство в группе не того. Поэтому
# контейнерные движки названы здесь ЯВНО и с пустым списком флагов: они
# гасят чужой ``--user``, а не остаются неизвестными.
_USER_FLAGS: dict[str, tuple[str, ...]] = {
    "systemd-run": ("--uid",),
    "sudo": ("-u", "--user"),
    "podman": (),
    "docker": (),
}


# Короткие флаги sudo, НЕ берущие значения: только из таких букв может
# состоять слипшийся пучок перед ``-u``. ``sudo -nu X`` — обычная запись тех
# же ``-n`` и ``-u``, и на ней страж возвращал пустую строку, то есть молча
# снимал проверку членства в группе (найдено машинным ревью 09.09.2026,
# находка f4286f03c34368d3). А вот в ``sudo -pu X`` буква ``u`` — уже
# значение ``-p``, а не флаг, и такой пучок здесь НЕ признаётся: угадать тут
# значит проверить группу не того пользователя.
_SUDO_VALUELESS_SHORT = frozenset("AbEeHiKklnPSsVv")


def _bundled_short_value(token: str, letter: str, following: str | None) -> str | None:
    """Значение слипшегося короткого флага: ``-nu X``, ``-uX``, ``-nuX``.

    ``following`` — значение, которое окно уже отдало ЭТОМУ флагу отдельным
    токеном (``None``, если флаг значения не берёт). Раньше сюда передавался
    весь список окна и индекс, и «следующим» считался соседний токен, кем бы
    он ни был; теперь сосед приходит только тогда, когда он и вправду
    значение этого флага.
    """
    if not token.startswith("-") or token.startswith("--"):
        return None
    body = token[1:]
    pos = body.find(letter)
    if pos < 0 or any(ch not in _SUDO_VALUELESS_SHORT for ch in body[:pos]):
        return None
    rest = body[pos + 1 :]
    if rest:
        return rest
    return following


# Флаги СОБСТВЕННЫХ аргументов обёртки, чьё значение стоит ОТДЕЛЬНЫМ
# токеном. Нужны, чтобы отличить значение флага от первого позиционного
# аргумента: в ``sudo -n -u haiplane-reviewer /wrap`` имя пользователя — это
# значение ``-u``, а ``/wrap`` уже полезная нагрузка, и всё правее неё —
# аргументы ЧУЖОЙ программы. Списки ПО ИМЕНИ, как и всё в этом модуле;
# незнакомый флаг со значением отдельным токеном закончит окно раньше
# времени, и пользователь останется НЕ НАЗВАН — это пропуск, а не подмена.
_TOOL_VALUE_FLAGS: dict[str, frozenset[str]] = {
    # sudo(8): опции, берущие аргумент.
    "sudo": frozenset(
        {
            "-C",
            "--close-from",
            "-D",
            "--chdir",
            "-g",
            "--group",
            "-h",
            "--host",
            "-p",
            "--prompt",
            "-R",
            "--chroot",
            "-r",
            "--role",
            "-t",
            "--type",
            "-T",
            "--command-timeout",
            "-U",
            "--other-user",
            "-u",
            "--user",
        }
    ),
    # systemd-run(1): опции, берущие аргумент. ``--user`` здесь значения НЕ
    # берёт — это выбор менеджера, а не имя пользователя, и путать их нельзя.
    "systemd-run": frozenset(
        {
            "-u",
            "--unit",
            "-p",
            "--property",
            "--description",
            "--slice",
            "--uid",
            "--gid",
            "--nice",
            "-E",
            "--setenv",
            "--service-type",
            "--working-directory",
            "--on-active",
            "--on-boot",
            "--on-startup",
            "--on-unit-active",
            "--on-unit-inactive",
            "--on-calendar",
            "--timer-property",
            "--path-property",
            "--socket-property",
            "-M",
            "--machine",
            "-H",
            "--host",
        }
    ),
    # У движков собственные аргументы — это глобальные флаги ДО подкоманды,
    # и они уже названы поимённо выше.
    "podman": _ENGINE_GLOBAL_VALUE_FLAGS["podman"],
    "docker": _ENGINE_GLOBAL_VALUE_FLAGS["docker"],
}


# Короткие флаги, берущие значение, и короткие флаги без значения — по
# инструменту. Слипшийся пучок ``-nu`` берёт следующий токен, а ``-n`` — нет,
# и без этого различия окно собственных аргументов обрывалось бы на имени
# пользователя (``sudo -n -u alice -nu bob``).
_TOOL_VALUE_SHORTS: dict[str, str] = {
    "sudo": "CDghpRrtTUu",
    "systemd-run": "upEMH",
}
_TOOL_VALUELESS_SHORTS: dict[str, str] = {
    "sudo": "".join(sorted(_SUDO_VALUELESS_SHORT)),
    "systemd-run": "dGPqrSt",
}


def _consumes_next(tool: str, arg: str) -> bool:
    """Флаг инструмента, чьё значение стоит СЛЕДУЮЩИМ отдельным токеном."""
    if arg in _TOOL_VALUE_FLAGS.get(tool, frozenset()):
        return True
    if arg.startswith("--") or not arg.startswith("-"):
        return False
    body = arg[1:]
    if not body:
        return False
    valueless = _TOOL_VALUELESS_SHORTS.get(tool, "")
    return body[-1] in _TOOL_VALUE_SHORTS.get(tool, "") and all(
        ch in valueless for ch in body[:-1]
    )


class _ToolWindow(NamedTuple):
    """Окно собственных аргументов инструмента, разобранное НА ФЛАГИ.

    Окно отдаётся парами ``(флаг, значение отдельным токеном или None)``, а
    не плоским списком токенов, потому что плоский список не отличал ФЛАГ от
    ЗНАЧЕНИЯ ЧУЖОГО ФЛАГА — и на этом страж выносил ПОЛОЖИТЕЛЬНОЕ суждение о
    строке, которой в ней нет. Измерено на ff617db и на HEAD 149cd825:
    ``systemd-run --description --scope --uid=haiplane-reviewer /wrap`` давало
    окно ``['--description', '--scope', '--uid=…']``, проверка ``'--scope' in
    own`` находила своё слово — и отказ снимался. Но ``--scope`` здесь съеден
    как ЗНАЧЕНИЕ ``--description``: настоящий systemd-run поднимет transient
    service, то есть ровно то, ради чего отказ и написан. Ошибка шла В
    СТОРОНУ ПРОПУСКА (найдено машинным ревью 10.09.2026, неразрешённая
    5a59af8d620685d0).
    """

    tool: str
    flags: list[tuple[str, str | None]]


def _wrapper_windows(parts: list[str]) -> list[_ToolWindow]:
    """Окна СОБСТВЕННЫХ аргументов названных инструментов, слева направо.

    Окно инструмента кончается там же, где кончает его читать он сам: на
    ``--`` либо на первом позиционном токене (имени полезной нагрузки).
    Дальше идут аргументы ЧУЖОЙ программы, и принимать их за флаги обёртки
    нельзя: страж возвращал тогда постороннее имя.

    ЗАМЕРЕНО на HEAD 21629cde до починки — обе формы давали чужой ответ:
    ``sudo -n -u haiplane-reviewer /usr/bin/env -u HOME /wrap`` давало
    ``HOME`` (находка 64e89a8683b01db1), а
    ``systemd-run --scope --uid=alice -- /bin/true --uid=bob`` — ``bob``
    (находка d228b0eb3310a9cc). Это не «пропустили незнакомое», о чём
    говорит объявленное ограничение задачи, а РАЗОБРАЛИ ЗНАКОМОЕ НЕВЕРНО:
    имя постороннее, и членство в группе каталога прогонов проверялось у
    того, кого в строке нет.

    Поиск инструментов продолжается и ЗА окном: вложенная обёртка
    (``sudo -u X podman run …``) остаётся видимой, и последнее названное имя
    по-прежнему действующее.
    """
    windows: list[_ToolWindow] = []
    i = 0
    while i < len(parts):
        tool = parts[i].rsplit("/", 1)[-1]
        if tool not in _USER_FLAGS:
            i += 1
            continue
        own: list[tuple[str, str | None]] = []
        j = i + 1
        while j < len(parts):
            arg = parts[j]
            if arg == "--":
                j += 1
                break
            if not arg.startswith("-") or arg == "-":
                break
            if _consumes_next(tool, arg) and j + 1 < len(parts):
                own.append((arg, parts[j + 1]))
                j += 2
            else:
                own.append((arg, None))
                j += 1
        windows.append(_ToolWindow(tool, own))
        i = max(j, i + 1)
    return windows


def _named_sandbox_user() -> tuple[str, str]:
    """ИНСТРУМЕНТ и пользователь ХОСТА, названные в песочнице, или ("", "").

    Инструмент возвращается вместе с именем, потому что форма записи
    пользователя — свойство ИНСТРУМЕНТА, а не строки: ``#1000`` у sudo это
    числовой uid, а у systemd-run такой формы нет вовсе. Разрешать имя,
    забыв, кто его написал, значит либо отвергать годную настройку, либо
    принимать негодную (найдено ревьюером Codex 10.09.2026 на ff617db).
    """
    parts = shlex.split(config.LOCAL_REVIEW_SANDBOX or "")
    named: list[tuple[str, str]] = []
    for window in _wrapper_windows(parts):
        flags = _USER_FLAGS[window.tool]
        for part, value in window.flags:
            for flag in flags:
                if part.startswith(flag + "="):
                    named.append((window.tool, part.split("=", 1)[1]))
                    break
                if part == flag and value is not None:
                    named.append((window.tool, value))
                    break
                if len(flag) == 2 and not flag.startswith("--"):
                    bundled = _bundled_short_value(part, flag[1], value)
                    if bundled:
                        named.append((window.tool, bundled))
                        break
    entry = _effective(named)
    return ("", "") if entry is _FLAG_ABSENT else entry


def sandbox_uid() -> str:
    """Пользователь ХОСТА, названный в песочнице, или "".

    Разбор идёт слева направо, и флаги ищутся те, что принадлежат ПОСЛЕДНЕМУ
    названному инструменту: в ``sudo -u X podman run --user 1000`` хостовый
    пользователь — X, а ``--user`` за podman относится к контейнеру.

    При ПОВТОРЕ флага действует ПОСЛЕДНЕЕ вхождение — то же правило и тот же
    _effective, что судит срок жизни контейнера. По первому вхождению страж
    на ``sudo -u alice -u bob`` называл alice, тогда как запуск идёт под bob,
    и членство в группе каталога прогонов проверялось НЕ У ТОГО пользователя
    — то есть проверка, заведённая ради 45971e09, отвечала не про ту
    конфигурацию (найдено при работе над #1208, ревьюер этого не называл).
    """
    return _named_sandbox_user()[1]


def scratch_problem() -> list[str]:
    """Названная причина, если каталог прогонов не даст ревьюеру работать.

    Проверка родилась из ревью (неразрешённая 45971e09) и закрывает его
    возражение по существу: режим 0770, который хаб ставит каталогу прогона,
    даёт доступ ГРУППЕ — и если группа не та, ревьюер всё равно получит
    EACCES, а тест на права пройдёт, потому что владелец проходит сам.

    Группа берётся у РОДИТЕЛЯ через setgid, поэтому проверяется именно он:
    без setgid подкаталог унаследует основную группу хаба, в которой ревьюера
    заведомо нет — так и задумано (deploy/LOCAL-REVIEW.md, шаг 1).

    Каталог, которого нет, — тоже причина, а не мелочь: созданный хабом «на
    лету» он получит группу хаба, то есть ровно ту, которая ревьюеру
    недоступна. Раньше хаб создавал его молча и тем самым готовил отказ,
    который проявлялся уже внутри чужого процесса.
    """
    base = (config.LOCAL_REVIEW_SCRATCH_DIR or "").strip()
    if not base:
        return []  # отсутствие настройки уже названо в not_ready()
    try:
        st = os.stat(base)
    except OSError:
        return [
            f"LOCAL_REVIEW_SCRATCH_DIR: каталога {base} нет. Создайте его "
            "заранее, с setgid и группой, общей с пользователем ревьюера: "
            "созданный хабом на лету, он получит группу хаба, куда ревьюеру "
            "хода нет — см. deploy/LOCAL-REVIEW.md"
        ]
    if not st.st_mode & stat.S_ISGID:
        return [
            f"LOCAL_REVIEW_SCRATCH_DIR: на {base} нет setgid (нужен режим "
            "2770). Без него каталог прогона унаследует основную группу хаба, "
            "и ревьюер получит отказ на собственный рабочий каталог"
        ]
    return _uid_outside_group(base, st.st_gid)


def _resolve_user(user: str, tool: str = "") -> Any | None:
    """Пользователь песочницы: ИМЯ или ЧИСЛО. Не разрешился — None.

    Числовая форма — не экзотика: ``--uid`` у systemd-run принимает и её, а
    ``getpwnam("65534")`` бросает KeyError, то есть проверка группы на таком
    значении молча ничего не проверяла (найдено ревью, неразрешённая
    534e16e4).

    Форма ``#1000`` — тоже не экзотика, а НАСТОЯЩИЙ синтаксис sudo(8): «The
    user may be either a user name or a numeric user ID (UID) prefixed with
    the '#' character». Решётка у sudo как раз и снимает неоднозначность
    между ИМЕНЕМ ``1000`` и uid ``1000`` — обе записи законны и означают
    разное. Без неё ``'#1000'.isdigit()`` ложно, разбор уходил в
    ``getpwnam('#1000')`` и падал ВСЕГДА: годная настройка отвергалась, а
    оператору называлась неверная причина — «пользователь не разрешается в
    системе» вместо «страж не знает этой записи» (найдено ревьюером Codex
    10.09.2026, воспроизведено на ff617db).

    ``tool`` обязателен именно потому, что форма принадлежит инструменту, и
    правило это двустороннее:

    * у systemd-run записи ``--uid=#1000`` не существует, и принять её там
      значило бы разрешить строку, которую сам systemd-run отвергнет;
    * у sudo, наоборот, ГОЛОЕ ЧИСЛО — это ИМЯ, а не uid, и разрешать его
      через ``getpwuid`` значит проверить членство в группе НЕ У ТОГО, а
      заодно одобрить строку, на которой sudo и не запустится.

    Второе не выведено из мана, а ЗАМЕРЕНО 10.09.2026 на машине разработки,
    где uid 501 существует и принадлежит вызывающему::

        sudo -n -u 501 true    -> «sudo: unknown user 501», rc 1
        sudo -n -u '#501' true -> rc 0

    То есть ``getpwuid(501)`` для строки ``sudo -u 501`` отвечает про
    пользователя, которого sudo в ней НЕ ВИДИТ. Ошибка шла в сторону
    ПРОПУСКА: страж говорил «настроено» про песочницу, которая падает на
    первом же запуске (доделано преемником на #1208; ревьюер называл только
    форму с решёткой).
    """
    try:
        if tool == "sudo":
            if user.startswith("#"):
                return pwd.getpwuid(int(user[1:]))
            # Голое число у sudo — имя. Никакого getpwuid здесь быть не может.
            return pwd.getpwnam(user)
        return pwd.getpwuid(int(user)) if user.isdigit() else pwd.getpwnam(user)
    except (KeyError, ValueError, OverflowError):
        return None


def _uid_outside_group(base: str, gid: int) -> list[str]:
    """Ревьюер из песочницы не состоит в группе каталога — назвать это.

    Неудача проверки — тоже причина, а не разрешение (найдено ревью,
    неразрешённая 36a63d6b). Пустой список из ``not_ready()`` означает
    «настроено», и вернуть его, ничего не проверив, значит пообещать
    работу там, где ревьюер упрётся в EACCES уже внутри чужого процесса —
    а в карточке будет «завершилось без отчёта» вместо названной причины,
    которую документ обещает.
    """
    tool, user = _named_sandbox_user()
    if not user:
        return []  # песочница не называет пользователя — судить не о чем
    entry = _resolve_user(user, tool)
    try:
        group = grp.getgrgid(gid)
    except (KeyError, OSError):
        group = None
    if entry is None or group is None:
        unknown = f"пользователь «{user}»" if entry is None else f"группа {gid}"
        return [
            f"LOCAL_REVIEW_SCRATCH_DIR: {unknown} не разрешается в системе, "
            f"поэтому проверить доступ ревьюера к {base} нельзя. Запуск "
            "вслепую кончится отказом внутри чужого процесса, где причину "
            "уже никто не назовёт"
        ]
    if entry.pw_name in group.gr_mem or entry.pw_gid == gid:
        return []
    return [
        f"LOCAL_REVIEW_SCRATCH_DIR: пользователь «{entry.pw_name}» из "
        f"песочницы не состоит в группе «{group.gr_name}», которой "
        f"принадлежит {base}. Права 0770 на каталог прогона даются именно "
        "группе — иначе ревьюер получит отказ на свой рабочий каталог"
    ]


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
