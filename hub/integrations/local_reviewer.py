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
        + sandbox_problem()
        + scratch_problem()
    )


# ЗАКРЫТЫЙ НАБОР ФОРМ ПЕСОЧНИЦЫ (решение владельца 10.09.2026, #1208).
#
# ЧТО ЗДЕСЬ БЫЛО РАНЬШЕ И ПОЧЕМУ СНЕСЕНО. Девять кругов ревью этот страж
# разбирал ПРОИЗВОЛЬНУЮ командную строку и судил её, перечисляя флаги ПО
# ИМЕНИ: знал ``--uid`` и ``-u``, знал ``podman run --timeout``, знал два
# десятка глобальных флагов движка. Частота находок по кругам — 4, 2, 4, 3,
# 3 — не падала, и каждый следующий читатель находил СЛЕДУЮЩУЮ форму записи:
# слипшийся пучок ``-itd``, неизвестный флаг со значением ``--blkio-weight``,
# терминатор ``--``, форму ``#UID`` у sudo, отсоединённый запуск, ``--scope``
# полезной нагрузки. Перечисление не догоняет генератор.
#
# РЕШАЮЩЕЕ. Все девять кругов дефекты шли В ОДНУ СТОРОНУ — ложный ПРОПУСК; ни
# одного ложного отказа. Страж, который знает названное и пропускает
# незнакомое, ошибается систематически в опасную сторону, а стоит он границей
# безопасности. Поэтому направление ошибки перевёрнуто НАМЕРЕННО: хаб
# принимает только те формы, которые умеет прочитать ЦЕЛИКОМ, а всё
# остальное отвергает с названной причиной и указанием на документ.
#
# ЧТО ДЕРЖИТ ИНВАРИАНТ. Хаб снимает зависший прогон, убивая группу СВОЕГО
# процесса, поэтому обёртка обязана оставить полезную нагрузку ПОТОМКОМ
# хаба. Обе формы набора это дают: ``sudo`` вызывает execve в том же
# процессе, ``systemd-run --scope`` оставляет CLI ребёнком клиента. Прежняя
# вторая половина правила — «либо СВОЙ срок жизни» — жила ради контейнеров,
# и вместе с ними уходит из переменной (см. _CONTAINER_HINT): контейнер
# потомком не бывает никогда, а его срок жизни теперь стоит внутри
# root-ового враппера, где зафиксирован вместе с остальными флагами podman.
#
# ЧЕГО НАБОР НЕ ОБЕЩАЕТ. Что делает САМА обёртка, страж по-прежнему не
# знает: путь к скрипту непрозрачен, и срок жизни того, что внутри, — на
# совести того, кто скрипт написал. Разница с прежним стражем не в этом
# знании, а в том, что теперь непрозрачен ровно ОДИН токен строки, а не
# произвольный её хвост, и все остальные прочитаны целиком.


class Shape(NamedTuple):
    """Одна форма закрытого набора, разобранная до последнего токена.

    ``example`` — не украшение: это строка, которую документ печатает в
    таблице форм, и тест сверяет её С ЭТИМ ПОЛЕМ, а потом прогоняет через
    самого стража. Расхождение документа с кодом на этой задаче случалось
    пять раз и глазами не ловилось ни разу.
    """

    name: str
    tool: str
    # Группы синонимов: форма не форма, пока из каждой группы не назван хотя
    # бы один флаг.
    required: tuple[tuple[str, ...], ...]
    valueless: frozenset[str]
    valued: frozenset[str]
    # Флаги, повтор которых НАКАПЛИВАЕТСЯ, а не перекрывает: у systemd-run
    # так устроены --property и --setenv, и запрещать их повтор значило бы
    # отвергать рабочую строку с двумя лимитами.
    repeatable: frozenset[str]
    user_flags: tuple[str, ...]
    example: str


SANDBOX_SHAPES: tuple[Shape, ...] = (
    Shape(
        name="sudo",
        tool="sudo",
        # ``-n`` обязателен не для красоты: без него sudo может спросить
        # пароль, а stdin у прогона занят ПРОМТОМ, который несёт одноразовый
        # код доступа к хабу. sudo прочитает его как пароль и отдаст в свой
        # лог неудачную попытку.
        required=(("-n",), ("-u", "--user")),
        valueless=frozenset({"-n"}),
        valued=frozenset({"-u", "--user"}),
        repeatable=frozenset(),
        user_flags=("-u", "--user"),
        example="/usr/bin/sudo -n -u haiplane-reviewer /usr/local/bin/haiplane-review-run",
    ),
    Shape(
        name="systemd-run --scope",
        tool="systemd-run",
        required=(("--scope",), ("--uid",)),
        valueless=frozenset({"--scope", "--quiet", "--collect"}),
        valued=frozenset(
            {
                "--uid",
                "--gid",
                "--slice",
                "--property",
                "--setenv",
                "--description",
                "--nice",
                "--working-directory",
                "--unit",
            }
        ),
        repeatable=frozenset({"--property", "--setenv"}),
        user_flags=("--uid",),
        example=(
            "/usr/bin/systemd-run --scope --uid=haiplane-reviewer "
            "/usr/local/bin/haiplane-review-run"
        ),
    ),
)
# Форма systemd-run ОСТАВЛЕНА в наборе осознанно, хотя на нашем проде она не
# стартует: смена uid у systemd-run идёт через polkit, и непривилегированному
# хабу отвечают «Interactive authentication required» (измерено 08.09.2026).
# Это факт про ХОСТ, а не про форму: там, где у пользователя хаба есть право
# (root или настроенное правило polkit), ``--scope`` — единственная форма,
# которая даёт лимиты ресурсов и потомка одновременно, без sudoers и без
# root-ового скрипта. Выкинуть её значило бы оставить в наборе ровно одну
# форму, то есть подменить проверку сравнением с зашитой строкой.

_DOC = "см. deploy/LOCAL-REVIEW.md"


def _shape_names() -> str:
    return ", ".join(f"«{shape.name}»" for shape in SANDBOX_SHAPES)


_UNKNOWN_TOOL_HINT = (
    "LOCAL_REVIEW_SANDBOX: «{tool}» не входит в закрытый набор форм "
    "песочницы. Хаб запускает только то, что умеет прочитать ЦЕЛИКОМ: "
    "{shapes}. Незнакомое больше не пропускается: за девять кругов ревью все "
    "найденные дефекты этого стража шли в одну сторону — ложного ПРОПУСКА, — "
    "и ни один в сторону ложного отказа. Спрячьте свою обёртку внутрь "
    "скрипта и назовите его формой sudo: "
    "sudo -n -u <пользователь> /абсолютный/путь — " + _DOC
)

# Контейнерам отказано ЦЕЛИКОМ, а не по недостающему флагу, и причина не в
# строгости. Контейнер не бывает потомком хаба: conmon отсоединён от клиента
# намеренно (измерено 08.09.2026 — kill -KILL по группе, через 7 с контейнер
# «Up»), поэтому единственной опорой оставался бы его собственный срок жизни
# — у podman ``--timeout``, у docker такого флага нет вовсе. Опора эта
# держалась на разборе чужой командной строки, и именно она дала шесть
# находок из пятнадцати. Рекомендованный путь ничего не теряет: podman
# по-прежнему запускается, но на уровень ниже — внутри root-ового враппера,
# где его флаги зафиксированы, а sudoers не допускает подстановок.
_CONTAINER_HINT = (
    "LOCAL_REVIEW_SANDBOX: «{tool}» — контейнерный движок, и в закрытый "
    "набор форм песочницы он не входит. Контейнер НИКОГДА не остаётся "
    "потомком хаба: conmon отсоединён от клиента намеренно (измерено "
    "08.09.2026: kill -KILL по группе, через 7 с контейнер «Up»), и снять "
    "его хаб может только чужим сроком жизни — у podman это --timeout "
    "<секунды>, а у docker штатного аналога нет вовсе. Судить о таком сроке "
    "значит разбирать всю строку podman до образа, и на этом разборе страж "
    "ошибался девять кругов подряд. Пропишите podman вместе с --timeout "
    "внутрь root-ового враппера, где флаги зафиксированы, а в песочнице "
    "назовите сам враппер: sudo -n -u <пользователь> /абсолютный/путь — " + _DOC
)

_REQUIRED_WHY: dict[str, str] = {
    "-n": (
        "без -n sudo может спросить пароль, а stdin прогона занят промтом "
        "ревью — sudo прочитает как пароль одноразовый код доступа к хабу"
    ),
    "-u": (
        "без имени пользователя запуск идёт от самого хаба, то есть изоляции "
        "нет вовсе, а проверка членства в группе каталога прогонов молча не "
        "срабатывает"
    ),
    "--scope": (
        "systemd-run без --scope поднимает transient service: родителем CLI "
        "становится PID 1, снять его по таймауту хаб не сможет, хотя напишет "
        "в ленту, что снял, — и ни cwd, ни белый список окружения до "
        "полезной нагрузки не доедут"
    ),
    "--uid": (
        "без имени пользователя запуск идёт от самого хаба, то есть изоляции "
        "нет вовсе, а проверка членства в группе каталога прогонов молча не "
        "срабатывает"
    ),
}

_MISSING_HINT = (
    "LOCAL_REVIEW_SANDBOX: форма «{shape}» требует {flags}, а строка их не "
    "называет: {why} — " + _DOC
)
_UNKNOWN_FLAG_HINT = (
    "LOCAL_REVIEW_SANDBOX: форма «{shape}» не знает флага «{flag}», а набор "
    "её флагов ЗАКРЫТ. Флаг, про который неизвестно даже, берёт ли он "
    "значение отдельным токеном, делает недостоверным разбор всей строки "
    "(измерено 10.09.2026: «--blkio-weight 500» съедало имя образа, и второй "
    "«--timeout 0» оставался невиден). Уберите флаг из строки песочницы либо "
    "спрячьте его внутрь враппера, где он зафиксирован, — " + _DOC
)
_BUNDLE_HINT = (
    "LOCAL_REVIEW_SANDBOX: «{flag}» — слипшийся короткий токен, и набор его "
    "не принимает: из одного токена не видно, где кончается флаг и "
    "начинается значение («-nu X», «-uX», «-pu X» читаются тремя разными "
    "способами, и страж выбирал не тот). Напишите каждый флаг отдельным "
    "токеном, а значение — следующим за ним: -n -u <пользователь> — " + _DOC
)
_SHORT_EQ_HINT = (
    "LOCAL_REVIEW_SANDBOX: «{flag}» — короткий флаг с «=», и набор такой "
    "записи не принимает: sudo возьмёт значением «={value}» вместе со "
    "знаком. Напишите значение отдельным токеном — " + _DOC
)
_VALUE_MISSING_HINT = (
    "LOCAL_REVIEW_SANDBOX: флаг «{flag}» формы «{shape}» берёт значение, а в "
    "строке за ним ничего нет — " + _DOC
)
_VALUE_EXTRA_HINT = (
    "LOCAL_REVIEW_SANDBOX: флаг «{flag}» формы «{shape}» значения не берёт, а "
    "ему написано «{value}» — " + _DOC
)
_REPEATED_HINT = (
    "LOCAL_REVIEW_SANDBOX: флаг «{flag}» назван в строке дважды. Инструменты "
    "берут ПОСЛЕДНЕЕ вхождение, читатель глазами — первое, и на этой разнице "
    "страж уже ошибался трижды. Напишите флаг один раз — " + _DOC
)
_TERMINATOR_HINT = (
    "LOCAL_REVIEW_SANDBOX: «--» в форме «{shape}» лишний — за флагами стоит "
    "ровно один аргумент, абсолютный путь к обёртке, и отделять от него "
    "нечего. Уберите «--» — " + _DOC
)
_NO_PAYLOAD_HINT = (
    "LOCAL_REVIEW_SANDBOX: форма «{shape}» кончается абсолютным путём к "
    "обёртке, а строка его не называет — " + _DOC
)
_RELATIVE_HINT = (
    "LOCAL_REVIEW_SANDBOX: «{path}» — не абсолютный путь. Хаб собирает PATH "
    "прогону сам, и относительное имя означает «какая-то программа», а не "
    "названная — " + _DOC
)
_TRAILING_HINT = (
    "LOCAL_REVIEW_SANDBOX: в форме «{shape}» после пути к обёртке аргументов "
    "быть не должно, а стоят: {rest}. Аргументы дописывает сам хаб — тем, "
    "что стоит в LOCAL_REVIEW_CMD, — " + _DOC
)


class _Scan(NamedTuple):
    """Строка песочницы, разобранная ДО ПОСЛЕДНЕГО ТОКЕНА под одну форму.

    ``bad`` — готовая причина по ПЕРВОМУ токену, который форма не принимает.
    Разбор при этом не обрывается: суждение «каких обязательных флагов не
    хватает» выносится по ВСЕЙ строке и называется первым, иначе оператор
    чинил бы по одному незнакомому флагу за круг, так и не узнав, что забыл
    ``--scope``.
    """

    flags: list[tuple[str, str | None]]
    positionals: list[str]
    bad: str


def _scan(shape: Shape, parts: list[str]) -> _Scan:
    known = shape.valueless | shape.valued
    flags: list[tuple[str, str | None]] = []
    positionals: list[str] = []
    terminator = False
    bad = ""
    i = 1
    while i < len(parts):
        token = parts[i]
        i += 1
        # Всё, что стоит ПОСЛЕ первого позиционного токена, — аргументы
        # ЧУЖОЙ программы, и флагами обёртки они не являются, как бы ни
        # выглядели. Без этого правила ``systemd-run --quiet --uid=x /wrap
        # --scope`` засчитывал ``--scope`` полезной нагрузки за собственный
        # флаг обёртки и проходил молча — то есть отказ уходил В СТОРОНУ
        # ПРОПУСКА (находка машинного ревью 3d5938a9a645c39d, воспроизведена
        # на первой редакции закрытого набора).
        if terminator or positionals or not token.startswith("-") or token == "-":
            positionals.append(token)
            continue
        if token == "--":
            terminator = True
            bad = bad or _TERMINATOR_HINT.format(shape=shape.name)
            continue
        long = token.startswith("--")
        name, sep, value = token.partition("=")
        if sep and not long:
            # ``-u=alice``: sudo возьмёт пользователем «=alice». Записать
            # флаг именем ``-u`` со значением ``alice`` значило бы прочитать
            # строку не так, как её прочитает сам инструмент.
            bad = bad or _SHORT_EQ_HINT.format(flag=token, value=value)
            continue
        if sep:
            flags.append((name, value))
            if name not in known:
                bad = bad or _UNKNOWN_FLAG_HINT.format(shape=shape.name, flag=name)
            elif name in shape.valueless:
                bad = bad or _VALUE_EXTRA_HINT.format(
                    shape=shape.name, flag=name, value=value
                )
            continue
        if not long and len(token) > 2:
            # Пучок разбирается только ради ИМЁН: назвать оператору
            # недостающий ``--scope`` важнее, чем сообщить про пучок, а сам
            # пучок набором не принимается в любом случае.
            letters = [f"-{ch}" for ch in token[1:]]
            if all(letter in known for letter in letters):
                for letter in letters[:-1]:
                    flags.append((letter, None))
                last = letters[-1]
                if last in shape.valued and i < len(parts):
                    flags.append((last, parts[i]))
                    i += 1
                else:
                    flags.append((last, None))
            bad = bad or _BUNDLE_HINT.format(flag=token)
            continue
        if token in shape.valued:
            if i < len(parts):
                flags.append((token, parts[i]))
                i += 1
            else:
                flags.append((token, None))
                bad = bad or _VALUE_MISSING_HINT.format(shape=shape.name, flag=token)
            continue
        flags.append((token, None))
        if token not in shape.valueless:
            bad = bad or _UNKNOWN_FLAG_HINT.format(shape=shape.name, flag=token)
    return _Scan(flags, positionals, bad)


def _canonical(shape: Shape, flag: str) -> str:
    """Имя-представитель группы синонимов: ``--user`` и ``-u`` — один флаг.

    Без этого повтор, записанный РАЗНЫМИ синонимами (``--user=alice -u bob``),
    проходил бы как два разных флага — то есть страж молчал бы ровно там, где
    инструмент возьмёт последнее вхождение, а читатель прочитает первое.
    """
    # Группы берутся и из ``required``, и из ``user_flags``: синоним, не
    # попавший в обязательные, иначе считался бы отдельным флагом, и повтор
    # им записанный прошёл бы молча.
    for group in shape.required + (shape.user_flags,):
        if flag in group:
            return group[0]
    return flag


def _shape_refusal(shape: Shape, scan: _Scan) -> str:
    """Первая названная причина отказа под уже выбранной формой; «» — годна."""
    named = [_canonical(shape, name) for name, _ in scan.flags]
    missing = [group for group in shape.required if group[0] not in named]
    if missing:
        flags = ", ".join("/".join(group) for group in missing)
        why = "; ".join(_REQUIRED_WHY[flag] for group in missing for flag in group[:1])
        return _MISSING_HINT.format(shape=shape.name, flags=flags, why=why)
    if scan.bad:
        return scan.bad
    for raw, _ in scan.flags:
        if named.count(_canonical(shape, raw)) > 1 and raw not in shape.repeatable:
            return _REPEATED_HINT.format(flag=raw)
    if not scan.positionals:
        return _NO_PAYLOAD_HINT.format(shape=shape.name)
    if not scan.positionals[0].startswith("/"):
        return _RELATIVE_HINT.format(path=scan.positionals[0])
    if len(scan.positionals) > 1:
        rest = " ".join(scan.positionals[1:])
        return _TRAILING_HINT.format(shape=shape.name, rest=rest)
    for flag in shape.user_flags:
        for name, value in scan.flags:
            if name == flag and not (value or "").strip():
                return _VALUE_MISSING_HINT.format(shape=shape.name, flag=flag)
    return ""


def _matched() -> tuple[Shape | None, str, str]:
    """(форма, пользователь ХОСТА, причина отказа). Форма есть — причины нет.

    Пользователь возвращается ТОЛЬКО у подошедшей формы. Раньше страж
    отвечал «кто» и про строку, которой не понял, и все девять кругов
    ошибался именно в этих ответах: имя бралось из аргументов полезной
    нагрузки, из значения соседнего флага, из первого вхождения повторённого
    флага. Форма, прочитанная целиком, называет пользователя однозначно;
    непрочитанная не называет его вовсе, и прогон не запускается.
    """
    parts = shlex.split(config.LOCAL_REVIEW_SANDBOX or "")
    if not parts:
        # Пустую настройку называет not_ready() отдельной строкой; вторая
        # причина про то же самое только шумит.
        return None, "", ""
    head = parts[0]
    tool = head.rsplit("/", 1)[-1]
    shape = next((s for s in SANDBOX_SHAPES if s.tool == tool), None)
    if shape is None or ("/" in head and not head.startswith("/")):
        if tool in ("podman", "docker"):
            return None, "", _CONTAINER_HINT.format(tool=tool)
        return None, "", _UNKNOWN_TOOL_HINT.format(tool=head, shapes=_shape_names())
    scan = _scan(shape, parts)
    reason = _shape_refusal(shape, scan)
    if reason:
        return None, "", reason
    user = ""
    for flag in shape.user_flags:
        for name, value in scan.flags:
            if name == flag:
                user = (value or "").strip()
    return shape, user, ""


def sandbox_problem() -> list[str]:
    """Названная причина, если песочница вне закрытого набора форм. [] — годна."""
    reason = _matched()[2]
    return [reason] if reason else []


def sandbox_shape() -> str:
    """Имя подошедшей формы или «». Им же названа строка формы в документе."""
    shape = _matched()[0]
    return shape.name if shape else ""


def _named_sandbox_user() -> tuple[str, str]:
    """ИНСТРУМЕНТ и пользователь ХОСТА подошедшей формы, или ("", "").

    Инструмент возвращается вместе с именем, потому что форма записи
    пользователя — свойство ИНСТРУМЕНТА, а не строки: ``#1000`` у sudo это
    числовой uid, а у systemd-run такой формы нет вовсе. Разрешать имя,
    забыв, кто его написал, значит либо отвергать годную настройку, либо
    принимать негодную (найдено ревьюером Codex 10.09.2026 на ff617db).

    Пустоту имени держит ``_matched`` и только он: второй такой же проверки
    здесь НЕТ намеренно. Найдено мутационной серией 10.09.2026 — двойной
    страж делал мутацию первого неубиваемой, то есть правило было записано в
    двух местах, а проверено ни в одном.
    """
    shape, user, _ = _matched()
    return (shape.tool if shape else "", user)


def sandbox_uid() -> str:
    """Пользователь ХОСТА, названный ПОДОШЕДШЕЙ формой, или "".

    Пустая строка теперь означает не «страж не разобрал», а «прогона не
    будет»: строку вне набора хаб не запускает вовсе, и судить о членстве в
    группе каталога прогонов там не о чем.
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


# Хост держит ОДИН прогон разом, и знать об этом может только хаб.
#
# Основание не выдумано: контейнеру ревьюера отмерено 1500 МБ и 1.5 CPU
# (deploy/LOCAL-REVIEW.md), и рабочий враппер на хосте хаба сам пишет об этом
# в комментарии — «два ревьюера разом хосту не по карману». Но САМ ВРАППЕР
# соблюсти это не может: он живёт одну команду и соседей не видит, и то, чем
# он это «соблюдал», — ``podman rm -af`` — соседа не сдерживает, а УБИВАЕТ,
# заодно снося все прочие контейнеры пользователя (найдено ревьюером Codex
# 10.09.2026 на ef2198fc и подтверждено на хосте: /usr/local/bin/
# haiplane-review-run, строка 22). Второй прогон, начавшийся близко по
# времени, сносил контейнер первого, и в карточке это легло бы отказом
# прогона с ЛОЖНОЙ причиной.
#
# Очередь, а не отказ: «хост занят» — это ожидание, а «ревью не состоялось» —
# исход, который карточка обязана нести человеку. Превращать первое во второе
# значило бы завести ровно тот класс молчаливой лжи, против которого написан
# весь этот модуль. Ждущий прогон остаётся в ``_LOCAL_RUNS``, поэтому свип
# потерянных строк его не трогает.
#
# Один процесс — та же зернистость, что у ``_LOCAL_RUNS``: прогоны
# досматривает только тот процесс хаба, который их завёл, и на большее этот
# замок не претендует.
_HOST_BUDGET = asyncio.Lock()


async def run_review(prompt: str, *, timeout: int | None = None) -> LocalRun | None:
    """Прогнать ревьюера над готовым промтом. ``None`` — запуска не было.

    ``None`` возвращается ровно тогда, когда прогона не было: нет настроек,
    нет каталога, нет бинаря — или песочница вне закрытого набора форм. Это
    не «ревью ничего не нашло» и не «ревью упало» — вызывающий обязан
    назвать это в ленте отдельной причиной, иначе «не запускалось»
    прочитается как «прочитано и чисто», а это и есть класс дефектов, ради
    которого всё здесь написано.

    Отдельной проверки песочницы ПО СРОКУ ЭТОГО ПРОГОНА здесь больше нет, и
    это не пропажа. Она сравнивала собственный срок жизни КОНТЕЙНЕРА с тем
    числом, по которому прогон снимут (``run_review(timeout=60)`` при
    ``--timeout 1800``). Контейнерных запусков в переменной больше не
    бывает: обе формы набора оставляют полезную нагрузку ПОТОМКОМ хаба, и
    убийство группы доходит до неё при любом сроке. Суждения, зависящего от
    ``timeout``, у стража не осталось — а ``is_configured()`` ниже спрашивает
    его на каждом прогоне, а не только на готовности.

    Прогон идёт ПО ОДНОМУ на хост: см. ``_HOST_BUDGET``. Ожидание очереди в
    отмеренный прогону срок не входит — срок начинают отсчитывать после того,
    как замок взят.
    """
    if not is_configured():
        return None
    limit = timeout if timeout is not None else config.LOCAL_REVIEW_TIMEOUT_SEC
    # Каталог прогона заводится ПОД ЗАМКОМ, а не до него: ждущий своей
    # очереди прогон иначе держал бы чужой каталог в scratch всё время
    # ожидания, а хаб обещает заводить его на прогон и сносить после.
    async with _HOST_BUDGET:
        base = config.LOCAL_REVIEW_SCRATCH_DIR.strip()
        try:
            workdir = tempfile.mkdtemp(prefix="haiplane-review-", dir=base)
            # Каталог создаёт ХАБ, а работать в нём чужому пользователю
            # (найдено ревью, находка 92eba4f8 — и это регрессия, которую
            # открыл фикс предыдущей). Пока песочница отсоединяла процесс,
            # домом ревьюера был его passwd-home и права этого каталога
            # никого не задевали. С --scope cwd, HOME и TMPDIR доезжают
            # по-настоящему — а mkdtemp всегда даёт 0700 владельца-создателя,
            # то есть ревьюер получил бы EACCES на собственный рабочий
            # каталог и упал бы, не начав.
            #
            # 0770, а не 0777: доступ даётся ГРУППЕ, общей у хаба и ревьюера,
            # — setgid на родителе (2770) проставляет её сам. Права «всем»
            # открыли бы промт с одноразовым кодом любому пользователю хоста.
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
