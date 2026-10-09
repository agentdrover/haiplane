#!/usr/bin/env python3
"""Служба-исполнитель локального ревьюера (#1571).

Зачем она есть. Юнит хаба идёт с ProtectSystem=strict и NoNewPrivileges=yes
(аудит 14.08, ослаблять нельзя): хаб не может ни создать каталог прогона вне
своих путей, ни выполнить sudo к пользователю ревьюера. Поэтому запуск вынесен
сюда — в отдельную службу ВНЕ изоляции хаба. Хаб ничего не запускает: он кладёт
задание в spool-каталог и ждёт result.json.

Чем служба защищена. Хаб открыт в интернет, а spool он пишет сам, поэтому всё,
что лежит в задании, считается чужим вводом:

* команду запускает ТОЛЬКО служба, и берёт её из СВОЕГО конфига
  (HAIPLANE_REVIEW_RUNNER_ARGV), а не из задания. Задание — закрытый список
  полей (JOB_FIELDS); любое лишнее поле отвергает задание целиком, ничего не
  запуская;
* конфиг обязан держать форму ``sudo -n -u <не root> /абсолютный/путь``:
  служба запускает врапер под пользователем ревьюера, а не что угодно;
* промт читается один раз и тут же удаляется с диска; дальше он живёт в памяти
  и в stdin процесса (в argv его нет: аргументы видны в ps любому);
* файлы spool открываются с O_NOFOLLOW, имена заданий — по строгому шаблону;
* служба работает от пользователя хаба (у него уже есть право на sudo к
  ревьюеру) и ничего не получает сверх этого. NoNewPrivileges в её юните
  НЕТ намеренно: с ним sudo не работает, и вся причина существования службы
  пропала бы.

Снимок исходников (#1599). Задание version=2 несёт ``src.tar``. Его проверяет
и распаковывает отдельный файл ``snapshot_unpack.py``, установленный РЯДОМ со
службой в каталог root (хаб на запись не пишет): служба грузит его по явному
пути и отказывается, если файл или каталог доступны на запись не root-у.
Враждебный tar отвергается до запуска модели; ``src`` и ``home`` раскладываются
в рабочем каталоге прогона (``<workdir>/src`` только для чтения). Проверка
установки без hub: ``haiplane-review-runner.py --check-install``.

Профиль advisor (#1649). Задание version=3 с ``profile="advisor"`` запускает
ВТОРУЮ команду службы (HAIPLANE_REVIEW_RUNNER_ARGV_ADVISOR) — врапер
``haiplane-advisor-run`` со своими образом, ключом z.ai, сроком и закреплённым
argv CLI. Профиль — закрытое перечисление, а не путь и не команда: v3 несёт
ровно ``version``, ``timeout_sec``, ``profile``, снимок ``src.tar`` в нём
запрещён, v1/v2 поля ``profile`` не знают. МОДЕЛЬ и СРОК советника служба берёт
из тех же root-овых файлов, что читает врапер (``advisor-model``,
``advisor-timeout`` в ФИКСИРОВАННОМ каталоге /etc/haiplane-review: он не
настраивается, иначе служба и врапер читали бы разные файлы), поэтому
объявленное в heartbeat не может разойтись с тем, что запустит врапер.
Heartbeat служба пишет JSON с блоком ``capabilities``: версии заданий,
профили, закреплённая модель советника и сроки. Это ПОДСКАЗКА готовности, а
не доказательство: spool пишет хаб, тем же uid. Доказательство — поле
``model`` задания v3: служба сверяет его с ``advisor-model`` из защищённого
источника ДО запуска, и отказывает при несовпадении.

Только стандартная библиотека: служба ставится на хост двумя файлами (сама и
``snapshot_unpack.py``), без пакета hub.

Протокол (хаб и служба держат одни имена; тест сверяет их):

    <spool>/job-<16 hex>/prompt.txt   промт (0660, удаляется службой сразу)
    <spool>/job-<16 hex>/src.tar      снимок исходников (#1599), только при
                                      version=2; пишется ДО job.json
    <spool>/job-<16 hex>/job.json     {"version": 1|2, "timeout_sec": N}; пишется
                                      ПОСЛЕДНИМ и атомарно — это и есть «готово».
                                      version=2 — задание со снимком; старая
                                      служба принимает только version=1 и
                                      отклоняет его ДО запуска модели.
                                      version=3 — {"version": 3, "timeout_sec": N,
                                      "profile": "advisor"} (#1649), без src.tar
    <job>/claimed                     служба взяла задание
    <job>/cancel                      хаб просит снять прогон (таймаут)
    <job>/result.json                 итог; пишется атомарно
    <spool>/heartbeat                 служба жива (mtime обновляется)

Снятие: файл cancel, исчезновение job.json (хаб остановлен и отозвал задание)
или исчезновение самого каталога. Служба посылает группе процесса SIGTERM —
его sudo пересылает своему потомку — и ждёт grace. SIGKILL после этого
отправляется, но на форме sudo он НЕ ДОХОДИТ: служба идёт от пользователя
хаба, потомок sudo — от другого uid, а sudo SIGKILL не пересылает (его
перехватить нельзя). Поэтому последний рубеж — НЕ служба, а собственный
``--timeout`` контейнера во враппере; чтобы хаб не объявил снятие раньше, чем
контейнер умрёт, срок задания обязан быть не меньше этого ``--timeout``
(HAIPLANE_REVIEW_RUNNER_WRAPPER_TIMEOUT_SEC, проверка в parse_job).
Результат снятия служба называет честно: ``cancelled`` / ``timed_out`` значат
«сигнал отправлен», а не «процесс мёртв».
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pwd
import re
import shlex
import shutil
import signal
import stat
import sys
import tempfile
import time
import types
from collections.abc import Mapping
from dataclasses import dataclass, field

SPOOL_PROMPT = "prompt.txt"
SPOOL_JOB = "job.json"
SPOOL_CLAIMED = "claimed"
SPOOL_CANCEL = "cancel"
SPOOL_RESULT = "result.json"
SPOOL_HEARTBEAT = "heartbeat"
SPOOL_SNAPSHOT = "src.tar"
UNPACKER_FILE = "snapshot_unpack.py"

JOB_VERSION = 1
# Версия задания со снимком исходников (#1599): старая служба знает только 1.
JOB_VERSION_SNAPSHOT = 2
# Версия задания профиля advisor (#1649): только советник, без снимка. Старые
# службы (v1, v2) её не знают и отклоняют ДО запуска модели.
JOB_VERSION_ADVISOR = 3
JOB_VERSIONS = (JOB_VERSION, JOB_VERSION_SNAPSHOT, JOB_VERSION_ADVISOR)
# Закрытый список: ни команды, ни путей, ни окружения задание нести не может.
JOB_FIELDS = ("version", "timeout_sec")
# У v3 к ним добавляется закрытое перечисление профиля.
JOB_FIELDS_ADVISOR = ("version", "timeout_sec", "profile", "model")
PROFILE_ADVISOR = "advisor"
PROFILE_REVIEW = "review"
# Файлы доверенной конфигурации врапера advisor (root:root, без записи группе и
# миру): их читает и врапер, и служба — единственный источник модели и срока.
ADVISOR_MODEL_FILE = "advisor-model"
ADVISOR_TIMEOUT_FILE = "advisor-timeout"
ADVISOR_ENV_FILE = "advisor.env"
REVIEW_ENV_FILE = "model.env"
DEFAULT_ADVISOR_CONF = "/etc/haiplane-review"
CAPABILITIES_PROTOCOL = 1
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")

OUTPUT_CAP = 200_000
PROMPT_CAP = 4 * 1024 * 1024
JOB_CAP = 4096
TIMEOUT_RC = 124

_JOB_NAME = re.compile(r"^job-[0-9a-f]{16}$")
_ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "LC_CTYPE")

log = logging.getLogger("haiplane-review-runner")


class ConfigError(Exception):
    """Конфиг службы негоден: служба не стартует, а не запускает «примерно»."""


class JobRejected(Exception):
    """Задание отвергнуто: причина уходит хабу в result.json."""


@dataclass(frozen=True)
class Config:
    spool: str
    argv: tuple[str, ...]
    scratch: str
    max_timeout: int = 1800
    poll: float = 1.0
    heartbeat_every: float = 5.0
    term_grace: float = 10.0
    wrapper_timeout: int = 0
    stale_sec: float = 3600.0
    output_cap: int = field(default=OUTPUT_CAP)
    # Снимок исходников (#1599). Потолки распаковки; распаковщик — файл рядом
    # со службой, если путь не задан. Доверенный владелец распаковщика — root
    # и из окружения не настраивается: хаб не должен иметь способа его сменить.
    snapshot_max_bytes: int = 64 * 1024 * 1024
    snapshot_max_entries: int = 50_000
    snapshot_max_depth: int = 32
    snapshot_unpacker: str = ""
    unpacker_trusted_uid: int = 0
    # Пользователь ревьюера из argv (sudo -n -u <имя>): нужен, чтобы убедиться,
    # что каталог прогонов не принадлежит ему (иначе sticky-бит не защищает).
    reviewer_user: str = ""
    # Профиль advisor (#1649). Пустой advisor_argv — профиля нет, v3 отвергается.
    advisor_argv: tuple[str, ...] = ()
    advisor_conf: str = ""
    # Доверенный владелец файлов advisor-model/advisor-timeout — root; из
    # окружения не настраивается (как unpacker_trusted_uid).
    conf_trusted_uid: int = 0


def _advisor_argv_problem(argv: list[str]) -> str:
    """Форма advisor строже: ровно ``sudo -n -u <не root> /абсолютный/путь``.

    Аргументов после врапера нет вовсе: argv CLI закреплён внутри врапера, а
    sudoers разрешает путь только с пустым списком аргументов.
    """
    problem = _argv_problem(argv)
    if problem:
        return problem
    if len(argv) != 5:
        return (
            "после пути к враперу advisor аргументов быть не должно: argv CLI "
            "закреплён внутри врапера"
        )
    return ""


def _argv_problem(argv: list[str]) -> str:
    """Название изъяна формы ``sudo -n -u <user> /abs/wrapper [args]`` или ""."""
    if len(argv) < 5:
        return "команда должна быть вида: sudo -n -u <пользователь> /путь/к/враперу"
    if os.path.basename(argv[0]) != "sudo":
        return "команда должна начинаться с sudo"
    if argv[1] != "-n":
        return "нужен -n: без него sudo может спросить пароль у промта в stdin"
    if argv[2] != "-u" or not argv[3] or argv[3].startswith(("-", "#")):
        return "после -n нужен -u <имя пользователя ревьюера>"
    if argv[3] == "root":
        return "запуск под root не изоляция; нужен пользователь ревьюера"
    if not argv[4].startswith("/"):
        return "путь к враперу обязан быть абсолютным"
    return ""


def load_config(env: Mapping[str, str]) -> Config:
    """Собрать конфиг из окружения; негодный — ConfigError с названной причиной."""

    def need(name: str) -> str:
        value = (env.get(name) or "").strip()
        if not value:
            raise ConfigError(f"{name} не задана")
        return value

    spool = need("HAIPLANE_REVIEW_RUNNER_SPOOL_DIR")
    scratch = need("HAIPLANE_REVIEW_RUNNER_SCRATCH_DIR")
    for name, path in (("SPOOL_DIR", spool), ("SCRATCH_DIR", scratch)):
        if not os.path.isabs(path):
            raise ConfigError(f"HAIPLANE_REVIEW_RUNNER_{name} должен быть абсолютным")
    try:
        argv = shlex.split((env.get("HAIPLANE_REVIEW_RUNNER_ARGV") or "").strip())
    except ValueError as exc:
        raise ConfigError(f"HAIPLANE_REVIEW_RUNNER_ARGV не разобран: {exc}") from exc
    problem = _argv_problem(argv)
    if problem:
        raise ConfigError(f"HAIPLANE_REVIEW_RUNNER_ARGV: {problem}")
    raw = (env.get("HAIPLANE_REVIEW_RUNNER_MAX_TIMEOUT_SEC") or "").strip()
    try:
        cap = int(raw) if raw else 1800
    except ValueError as exc:
        raise ConfigError("HAIPLANE_REVIEW_RUNNER_MAX_TIMEOUT_SEC не число") from exc
    if cap <= 0:
        raise ConfigError("HAIPLANE_REVIEW_RUNNER_MAX_TIMEOUT_SEC должен быть > 0")
    raw = (env.get("HAIPLANE_REVIEW_RUNNER_WRAPPER_TIMEOUT_SEC") or "").strip()
    try:
        wrapper = int(raw) if raw else 0
    except ValueError as exc:
        raise ConfigError(
            "HAIPLANE_REVIEW_RUNNER_WRAPPER_TIMEOUT_SEC не число"
        ) from exc
    if wrapper < 0 or wrapper > cap:
        raise ConfigError(
            "HAIPLANE_REVIEW_RUNNER_WRAPPER_TIMEOUT_SEC не может быть больше "
            "HAIPLANE_REVIEW_RUNNER_MAX_TIMEOUT_SEC: потолок службы убил бы "
            "прогон раньше, чем контейнер отсчитает свой --timeout"
        )
    try:
        advisor = shlex.split(
            (env.get("HAIPLANE_REVIEW_RUNNER_ARGV_ADVISOR") or "").strip()
        )
    except ValueError as exc:
        raise ConfigError(
            f"HAIPLANE_REVIEW_RUNNER_ARGV_ADVISOR не разобран: {exc}"
        ) from exc
    if advisor:
        problem = _advisor_argv_problem(advisor)
        if problem:
            raise ConfigError(f"HAIPLANE_REVIEW_RUNNER_ARGV_ADVISOR: {problem}")
        if advisor[3] != argv[3]:
            raise ConfigError(
                "HAIPLANE_REVIEW_RUNNER_ARGV_ADVISOR: пользователь врапера advisor "
                "должен совпадать с пользователем ревью (отдельные uid — вне MVP)"
            )
    return Config(
        spool=spool,
        argv=tuple(argv),
        scratch=scratch,
        max_timeout=cap,
        wrapper_timeout=wrapper,
        reviewer_user=argv[3],
        advisor_argv=tuple(advisor),
        snapshot_max_bytes=_positive(
            env, "HAIPLANE_REVIEW_RUNNER_SNAPSHOT_MAX_BYTES", 64 * 1024 * 1024
        ),
        snapshot_max_entries=_positive(
            env, "HAIPLANE_REVIEW_RUNNER_SNAPSHOT_MAX_ENTRIES", 50_000
        ),
        snapshot_max_depth=_positive(
            env, "HAIPLANE_REVIEW_RUNNER_SNAPSHOT_MAX_DEPTH", 32
        ),
    )


def _positive(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ConfigError(f"{name} не число") from exc
    if value <= 0:
        raise ConfigError(f"{name} должен быть > 0")
    return value


def parse_job(raw: bytes, max_timeout: int, wrapper_timeout: int = 0) -> int:
    """Срок прогона из задания, или JobRejected с причиной. Команды не читает."""
    return parse_job_ex(raw, max_timeout, wrapper_timeout)[0]


def parse_job_ex(
    raw: bytes,
    max_timeout: int,
    wrapper_timeout: int = 0,
    advisor_timeout: int = 0,
    advisor_model: str = "",
) -> tuple[int, int]:
    """``(срок, версия)`` задания, или JobRejected. Команды не читает.

    ``wrapper_timeout`` — ``--timeout`` врапера ревью, ``advisor_timeout`` —
    врапера advisor (#1649): срок задания не может быть короче того, что у
    контейнера, иначе хаб объявил бы снятие раньше, чем контейнер умрёт.
    """
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise JobRejected("job.json не разобран как JSON") from exc
    if not isinstance(data, dict):
        raise JobRejected("job.json должен быть объектом")
    advisor_job = data.get("version") == JOB_VERSION_ADVISOR and not isinstance(
        data.get("version"), bool
    )
    allowed = JOB_FIELDS_ADVISOR if advisor_job else JOB_FIELDS
    extra = sorted(str(key) for key in set(data) - set(allowed))
    if extra:
        raise JobRejected(
            "в задании неизвестные поля: "
            + ", ".join(extra)[:200]
            + "; задание несёт только "
            + ", ".join(allowed)
            + ", а команду службы задаёт её конфиг"
        )
    version = data.get("version")
    if isinstance(version, bool) or version not in JOB_VERSIONS:
        raise JobRejected(
            "версия задания не поддерживается (нужна "
            + " или ".join(str(v) for v in JOB_VERSIONS)
            + ")"
        )
    if version == JOB_VERSION_ADVISOR and data.get("profile") != PROFILE_ADVISOR:
        raise JobRejected(
            "версия задания 3 принимается только с profile=advisor (поле profile "
            f"— закрытое перечисление, получено {data.get('profile')!r})"
        )
    if version == JOB_VERSION_ADVISOR:
        asked = data.get("model")
        if not isinstance(asked, str) or not _MODEL_NAME.match(asked):
            raise JobRejected(
                "задание advisor обязано назвать model (имя модели, не путь)"
            )
        if advisor_model and asked != advisor_model:
            raise JobRejected(
                f"model задания ({asked}) не совпадает с advisor-model службы "
                f"({advisor_model}): подмены модели нет, запуск отказан до модели"
            )
    timeout = data.get("timeout_sec")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise JobRejected("timeout_sec должен быть целым числом больше нуля")
    floor = advisor_timeout if version == JOB_VERSION_ADVISOR else wrapper_timeout
    if timeout < floor:
        raise JobRejected(
            f"срок задания {timeout} с меньше --timeout враппера "
            f"({floor} с): хаб объявил бы снятие раньше, чем "
            "контейнер умрёт. Выровняйте LOCAL_REVIEW_TIMEOUT_SEC хаба с "
            "файлом timeout враппера"
        )
    return min(timeout, max_timeout), int(version)


def _open_chain(path: str, trusted_uid: int) -> tuple[list[int], int, str, str]:
    """Цепочка каталогов до родителя файла: ``(fds, fd родителя, имя, причина)``.

    Каждый каталог открывается от корня ``O_NOFOLLOW|O_DIRECTORY`` от
    дескриптора предка и судится по ``fstat`` ЭТОГО дескриптора (владелец —
    root или доверенный, запись группе и миру — только с sticky-битом). Ссылка
    в любом компоненте — отказ. Вызывающий закрывает ``fds``. Как загрузчик
    распаковщика (#1599): проверка и чтение идут одним проходом.
    """
    if not os.path.isabs(path) or any(
        p in ("", ".", "..") for p in path.split("/")[1:]
    ):
        return (
            [],
            -1,
            "",
            f"{path}: нужен абсолютный путь без «.», «..» и пустых компонентов",
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_DIRECTORY
    parts = [p for p in path.split("/") if p]
    fds: list[int] = []
    try:
        current = os.open("/", flags)
        fds.append(current)
        walked = ""
        for name in parts[:-1]:
            walked += "/" + name
            try:
                current = os.open(name, flags, dir_fd=current)
            except OSError as exc:
                return (
                    fds,
                    -1,
                    "",
                    (
                        f"каталог {walked} не открыт без следования по ссылке: {exc.strerror}"
                    ),
                )
            fds.append(current)
            dinfo = os.fstat(current)
            if dinfo.st_uid not in (0, trusted_uid):
                return (
                    fds,
                    -1,
                    "",
                    (
                        f"каталог {walked} принадлежит uid {dinfo.st_uid}, а должен "
                        f"принадлежать root (uid {trusted_uid})"
                    ),
                )
            if dinfo.st_mode & 0o022 and not dinfo.st_mode & stat.S_ISVTX:
                return (
                    fds,
                    -1,
                    "",
                    f"каталог {walked} доступен на запись группе или всем",
                )
        return fds, current, parts[-1], ""
    except OSError as exc:
        return fds, -1, "", f"{path} не открыт: {exc.strerror}"


def _trusted_read(path: str, trusted_uid: int, cap: int = 256) -> tuple[str, str]:
    """Текст файла конфигурации: ``(текст, причина)``; проверка и чтение — из ОДНОГО дескриптора."""
    fds, parent, name, problem = _open_chain(path, trusted_uid)
    try:
        if problem:
            return "", problem
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=parent,
            )
        except OSError as exc:
            return "", f"{path}: не открыт без следования по ссылке ({exc.strerror})"
        fds.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return "", f"{path}: не обычный файл"
        if info.st_uid != trusted_uid:
            return "", (
                f"{path} принадлежит uid {info.st_uid}, а должен принадлежать "
                f"uid {trusted_uid} (root): иначе хаб мог бы подменить модель или срок"
            )
        if info.st_mode & 0o022:
            return "", f"{path} доступен на запись группе или всем"
        data = os.read(fd, cap + 1)
        if len(data) > cap:
            return "", f"{path} больше {cap} байт"
        return data.decode("utf-8", "replace").strip(), ""
    except OSError as exc:
        return "", f"{path} не прочитан: {exc.strerror}"
    finally:
        for fd in fds:
            os.close(fd)


def _secret_file_facts(
    path: str, trusted_uid: int
) -> tuple[tuple[int, int] | None, str]:
    """Метаданные файла ключа БЕЗ чтения содержимого: ``((dev, ino), причина)``.

    Файл ключа читает только контейнер; службе (пользователь хаба) читать его
    незачем, поэтому судим по ``lstat`` от дескриптора родителя: обычный файл
    (не ссылка), владелец доверенный, записи группе и миру нет, миру недоступен.
    """
    fds, parent, name, problem = _open_chain(path, trusted_uid)
    try:
        if problem:
            return None, problem
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            return None, f"{path}: {exc.strerror}"
        if not stat.S_ISREG(info.st_mode):
            return None, f"{path}: нужен обычный файл, не ссылка"
        if info.st_uid != trusted_uid:
            return (
                None,
                f"{path} принадлежит uid {info.st_uid}, а должен uid {trusted_uid}",
            )
        if info.st_mode & 0o022:
            return None, f"{path} доступен на запись группе или всем"
        if info.st_mode & 0o007:
            return None, f"{path} доступен всем: ключ не должен быть читаем миру"
        return (info.st_dev, info.st_ino), ""
    finally:
        for fd in fds:
            os.close(fd)


def advisor_settings(cfg: Config) -> tuple[str, int, str]:
    """``(модель, срок враппера, причина)`` профиля advisor; причина пуста — профиль годен.

    Модель и срок читаются из ТЕХ ЖЕ root-овых файлов фиксированного каталога,
    что и враппер ``haiplane-advisor-run``. Цепочка каталогов открывается по
    компонентам с ``O_NOFOLLOW`` и читается из проверенного дескриптора.
    Файл ключа ``advisor.env`` проверяется по метаданным и не может быть тем
    же файлом, что ``model.env`` (иначе контейнер советника получил бы ключ
    ревью).
    """
    if not cfg.advisor_argv:
        return (
            "",
            0,
            "профиль advisor не настроен (HAIPLANE_REVIEW_RUNNER_ARGV_ADVISOR пуст)",
        )
    conf = cfg.advisor_conf or DEFAULT_ADVISOR_CONF
    values: dict[str, str] = {}
    for name in (ADVISOR_MODEL_FILE, ADVISOR_TIMEOUT_FILE):
        text, problem = _trusted_read(os.path.join(conf, name), cfg.conf_trusted_uid)
        if problem:
            return "", 0, problem
        values[name] = text
    env_facts, problem = _secret_file_facts(
        os.path.join(conf, ADVISOR_ENV_FILE), cfg.conf_trusted_uid
    )
    if problem:
        return "", 0, problem
    review_facts, review_problem = _secret_file_facts(
        os.path.join(conf, REVIEW_ENV_FILE), cfg.conf_trusted_uid
    )
    if not review_problem and review_facts == env_facts:
        return (
            "",
            0,
            (
                f"{ADVISOR_ENV_FILE} и {REVIEW_ENV_FILE} — один и тот же файл: ключ "
                "ревью не должен попадать в контейнер советника"
            ),
        )
    model = values[ADVISOR_MODEL_FILE]
    if not _MODEL_NAME.match(model):
        return "", 0, f"{ADVISOR_MODEL_FILE}: имя модели недопустимо"
    raw = values[ADVISOR_TIMEOUT_FILE]
    if not raw.isdigit() or int(raw) <= 0:
        return "", 0, f"{ADVISOR_TIMEOUT_FILE}: нужно целое число секунд больше нуля"
    timeout = int(raw)
    if timeout > cfg.max_timeout:
        return (
            "",
            0,
            (
                f"{ADVISOR_TIMEOUT_FILE} ({timeout} с) больше потолка службы "
                f"({cfg.max_timeout} с): потолок убил бы прогон раньше контейнера"
            ),
        )
    return model, timeout, ""


def capabilities(cfg: Config) -> dict[str, object]:
    """Что служба умеет, — для хаба (блок ``capabilities`` в heartbeat, #1649)."""
    model, timeout, problem = advisor_settings(cfg)
    ready = not problem
    caps: dict[str, object] = {
        "protocol": CAPABILITIES_PROTOCOL,
        "job_versions": [v for v in JOB_VERSIONS if v != JOB_VERSION_ADVISOR or ready],
        "profiles": [PROFILE_REVIEW] + ([PROFILE_ADVISOR] if ready else []),
        "advisor_model": model,
        "timeouts": {
            "max_sec": cfg.max_timeout,
            "review_sec": cfg.wrapper_timeout,
            "advisor_sec": timeout,
        },
    }
    if cfg.advisor_argv and problem:
        caps["advisor_problem"] = problem[:300]
    return caps


def heartbeat_payload(cfg: Config) -> bytes:
    """Тело heartbeat: жизнь (pid, время) и возможности. mtime файла — признак жизни."""
    return json.dumps(
        {"pid": os.getpid(), "time": time.time(), "capabilities": capabilities(cfg)}
    ).encode()


class _TooBig(Exception):
    pass


def _read_nofollow(path: str, cap: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("не обычный файл")
        data = handle.read(cap + 1)
    if len(data) > cap:
        raise _TooBig(f"файл больше {cap} байт")
    return data


def _write_atomic(directory: str, name: str, data: bytes) -> None:
    tmp = os.path.join(directory, name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o660)
    try:
        os.fchmod(fd, 0o660)  # nosec B103 - группе хаба и службы, не миру
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, os.path.join(directory, name))


def _unlink(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


def _claim(jobdir: str) -> bool:
    try:
        fd = os.open(
            os.path.join(jobdir, SPOOL_CLAIMED),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o660,
        )
    except OSError:
        return False
    os.close(fd)
    return True


def _withdrawn(jobdir: str) -> bool:
    """Хаб отозвал задание: удалил job.json или весь каталог."""
    return not os.path.exists(os.path.join(jobdir, SPOOL_JOB))


def _cancelled(jobdir: str) -> bool:
    return _withdrawn(jobdir) or os.path.exists(os.path.join(jobdir, SPOOL_CANCEL))


def _clean_env(workdir: str, home: str = "") -> dict[str, str]:
    env = {name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    # Со снимком рабочий каталог закрыт на запись (в нём нельзя подменить src),
    # поэтому писать ревьюеру разрешено только в его ``home`` внутри.
    env["HOME"] = home or workdir
    env["TMPDIR"] = home or workdir
    return env


def default_unpacker_path() -> str:
    """Распаковщик — файл рядом со службой (ни cwd, ни spool, ни sys.path)."""
    here = os.path.dirname(os.path.realpath(__file__))
    return os.path.join(here, UNPACKER_FILE)


def read_trusted_source(path: str, trusted_uid: int) -> tuple[bytes, str]:
    """Прочитать файл распаковщика ТЕМ ЖЕ проходом, что и проверка: ``(байты, причина)``.

    Раньше проверялась цепочка после ``realpath``, а загрузка шла по исходному
    пути: ссылка-алиас в пути переключалась между проверкой и загрузкой
    (находка Codex, TOCTOU). Теперь путь абсолютный и без ссылок: каждый
    каталог открывается от корня ``O_NOFOLLOW|O_DIRECTORY`` от дескриптора
    предка, владелец и режим судятся по ``fstat`` ЭТОГО дескриптора, файл
    открывается так же и читается из него. Исполняется прочитанное, а не путь
    повторно. Любая ссылка в пути — отказ. Доверяем только владельцу
    ``trusted_uid`` (root): хаб и служба — один пользователь, и «владеет
    служба» ничего не защищает; каталог с записью группе или всем допустим
    только с sticky-битом (/tmp).
    """
    if not os.path.isabs(path) or any(
        p in ("", ".", "..") for p in path.split("/")[1:]
    ):
        return (
            b"",
            f"распаковщик {path}: нужен абсолютный путь без «.», «..» и пустых компонентов",
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    parts = [p for p in path.split("/") if p]
    fds: list[int] = []
    try:
        current = os.open("/", flags | os.O_DIRECTORY)
        fds.append(current)
        walked = ""
        for name in parts[:-1]:
            walked += "/" + name
            try:
                current = os.open(name, flags | os.O_DIRECTORY, dir_fd=current)
            except OSError as exc:
                return (
                    b"",
                    f"каталог {walked} в пути распаковщика не открыт без следования по ссылке: {exc.strerror}",
                )
            fds.append(current)
            dinfo = os.fstat(current)
            if dinfo.st_uid not in (0, trusted_uid):
                return (
                    b"",
                    (
                        f"каталог {walked} над распаковщиком принадлежит uid "
                        f"{dinfo.st_uid}, а должен принадлежать root (uid {trusted_uid})"
                    ),
                )
            if dinfo.st_mode & 0o022 and not dinfo.st_mode & stat.S_ISVTX:
                return b"", f"каталог {walked} над распаковщиком доступен на запись"
        try:
            fd = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=current)
        except FileNotFoundError:
            return b"", f"распаковщик {path} не найден"
        except OSError as exc:
            return b"", f"распаковщик {path} не обычный файл (ссылка?): {exc.strerror}"
        fds.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return b"", f"распаковщик {path} не обычный файл (ссылка?)"
        if info.st_uid != trusted_uid:
            return b"", (
                f"распаковщик {path} принадлежит uid {info.st_uid}, а должен "
                f"принадлежать root (uid {trusted_uid}): иначе хаб, работающий "
                "под тем же пользователем, мог бы его заменить"
            )
        if info.st_mode & 0o022:
            return b"", f"распаковщик {path} доступен на запись группе или всем"
        if info.st_size > 1 << 20:
            return b"", f"распаковщик {path} подозрительно велик"
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks), ""
    except OSError as exc:
        return b"", f"распаковщик {path} не прочитан: {exc.strerror}"
    finally:
        for fd in fds:
            os.close(fd)


def load_unpacker(path: str, trusted_uid: int = 0):  # noqa: ANN201 - модуль по пути
    """Загрузить распаковщик ПО ЯВНОМУ ПУТИ: исполняется то, что проверено."""
    source, problem = read_trusted_source(path, trusted_uid)
    if problem:
        raise ConfigError(problem)
    module = types.ModuleType("haiplane_snapshot_unpack")
    module.__file__ = path
    sys.modules["haiplane_snapshot_unpack"] = module
    try:
        exec(compile(source, path, "exec"), module.__dict__)  # noqa: S102 - проверенный root-овый код
    except Exception as exc:  # noqa: BLE001 - любой отказ загрузки — отказ снимку
        raise ConfigError(f"распаковщик {path} не загружен: {exc}") from exc
    return module


def _outcome(status: str, **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "status": status,
        "rc": 0,
        "output": "",
        "dropped": 0,
        "timed_out": False,
        "cancelled": False,
        "duration_ms": 0,
        "reason": "",
    }
    base.update(over)
    return base


class _Sink:
    """Хвост вывода с потолком; остальное сливается и считается."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.kept: list[bytes] = []
        self.size = 0
        self.dropped = 0

    def add(self, chunk: bytes) -> None:
        room = self.cap - self.size
        if room > 0:
            self.kept.append(chunk[:room])
            self.size += min(room, len(chunk))
        self.dropped += max(0, len(chunk) - max(room, 0))

    def text(self) -> str:
        return b"".join(self.kept).decode(errors="replace")


async def _pump(proc: asyncio.subprocess.Process, prompt: bytes, sink: _Sink) -> None:
    async def feed() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                assert proc.stdin is not None
                proc.stdin.close()

    feeding = asyncio.create_task(feed())
    try:
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(65536):
            sink.add(chunk)
        await proc.wait()
    finally:
        feeding.cancel()


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.send_signal(sig)


async def _terminate(proc: asyncio.subprocess.Process, grace: float) -> None:
    """SIGTERM группе (sudo перешлёт его потомку), после grace — SIGKILL.

    SIGKILL здесь последняя попытка и ГАРАНТИИ НЕ ДАЁТ: на форме sudo потомок
    идёт от другого uid, и сигнал до него не доходит. Последний рубеж там —
    ``--timeout`` контейнера во враппере (см. docstring модуля).
    """
    if proc.returncode is None:
        _signal_group(proc, signal.SIGTERM)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), grace)
    _signal_group(proc, signal.SIGKILL)
    with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError, OSError):
        await asyncio.wait_for(proc.wait(), 5)


async def _watch(
    cfg: Config, jobdir: str, pump: asyncio.Task[None], timeout: int
) -> str:
    """Ждать конца прогона: ``done``, ``timeout`` или ``cancelled``."""
    end = time.monotonic() + timeout
    while True:
        done, _ = await asyncio.wait({pump}, timeout=cfg.poll)
        if done:
            return "done"
        if _cancelled(jobdir):
            return "cancelled"
        if time.monotonic() >= end:
            return "timeout"


def _lay_out_snapshot(cfg: Config, unpacker, workdir: str, snapshot: str) -> str:  # noqa: ANN001
    """Разложить снимок в ``workdir``. Непустая строка — причина отказа."""
    limits = unpacker.Limits(
        max_bytes=cfg.snapshot_max_bytes,
        max_entries=cfg.snapshot_max_entries,
        max_depth=cfg.snapshot_max_depth,
    )
    try:
        reviewer_uid = pwd.getpwnam(cfg.reviewer_user).pw_uid
    except (KeyError, ValueError):
        return (
            "снимок отвергнут: пользователь ревьюера "
            f"«{cfg.reviewer_user}» не разрешается в системе, проверить каталог "
            "прогонов нельзя"
        )
    try:
        gid = os.stat(cfg.scratch).st_gid
        unpacker.prepare_workdir(workdir, snapshot, limits, gid, reviewer_uid)
    except unpacker.SnapshotRefused as exc:
        return f"снимок отвергнут: {exc.reason}"
    except OSError as exc:
        return f"снимок отвергнут: распаковка не удалась: {exc.strerror or exc}"
    return ""


def _remove_workdir(workdir: str, unpacker) -> None:  # noqa: ANN001
    """Снять каталог прогона; со снимком — без следования ссылкам, с правами."""
    if unpacker is not None and unpacker.remove_tree(workdir):
        return
    if unpacker is not None:
        log.warning("workdir %s removed with leftovers", workdir)
    shutil.rmtree(workdir, ignore_errors=True)


async def _run(
    cfg: Config,
    jobdir: str,
    prompt: bytes,
    timeout: int,
    snapshot: str | None = None,
    unpacker=None,  # noqa: ANN001
    argv: tuple[str, ...] | None = None,
) -> tuple[dict[str, object], bool]:
    """Прогнать команду службы. Второе значение — «хаб отозвал, убрать за ним».

    ``argv`` — команда профиля (advisor, #1649); без него — команда ревью.
    """
    started = time.monotonic()
    workdir = tempfile.mkdtemp(prefix="haiplane-review-", dir=cfg.scratch)
    proc: asyncio.subprocess.Process | None = None
    try:
        home = workdir
        if snapshot is not None:
            # Приватный workdir (0700) до конца проверки; после неё он закрыт на
            # запись, а писать ревьюеру можно только в ``home`` внутри.
            refusal = _lay_out_snapshot(cfg, unpacker, workdir, snapshot)
            _unlink(snapshot)
            if refusal:
                return _outcome("rejected", reason=refusal), False
            home = os.path.join(workdir, "home")
        else:
            # Работает в каталоге чужой пользователь (ревьюер): доступ группе.
            os.chmod(workdir, 0o770)  # nosec B103 - группе, не миру
        proc = await asyncio.create_subprocess_exec(
            *(argv or cfg.argv),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=workdir,
            env=_clean_env(workdir, home if snapshot is not None else ""),
            start_new_session=True,
        )
        sink = _Sink(cfg.output_cap)
        pump = asyncio.create_task(_pump(proc, prompt, sink))
        try:
            verdict = await _watch(cfg, jobdir, pump, timeout)
        except asyncio.CancelledError:
            await _terminate(proc, min(cfg.term_grace, 2.0))
            raise
        if verdict != "done":
            await _terminate(proc, cfg.term_grace)
            await asyncio.wait({pump}, timeout=2)
            pump.cancel()
        elapsed = int((time.monotonic() - started) * 1000)
        if verdict == "timeout":
            return _outcome(
                "ok", rc=TIMEOUT_RC, timed_out=True, duration_ms=elapsed
            ), False
        done = verdict == "done"
        return (
            _outcome(
                "ok",
                rc=(proc.returncode or 0) if done else TIMEOUT_RC,
                output=sink.text() if done else "",
                dropped=sink.dropped if done else 0,
                cancelled=not done,
                duration_ms=elapsed,
                reason="" if done else "прогон снят по просьбе хаба",
            ),
            verdict == "cancelled" and _withdrawn(jobdir),
        )
    finally:
        _remove_workdir(workdir, unpacker if snapshot is not None else None)


async def _execute(cfg: Config, jobdir: str) -> tuple[dict[str, object], bool]:
    snapshot_path = os.path.join(jobdir, SPOOL_SNAPSHOT)
    try:
        return await _execute_inner(cfg, jobdir, snapshot_path)
    finally:
        # На ЛЮБОМ исходе, включая ранние отказы (версия, JSON, промт): архив
        # хаба в задании не остаётся (находка Codex).
        _unlink(snapshot_path)


async def _execute_inner(
    cfg: Config, jobdir: str, snapshot_path: str
) -> tuple[dict[str, object], bool]:
    prompt_path = os.path.join(jobdir, SPOOL_PROMPT)
    try:
        a_model, a_timeout, _a_problem = advisor_settings(cfg)
        timeout, version = parse_job_ex(
            _read_nofollow(os.path.join(jobdir, SPOOL_JOB), JOB_CAP),
            cfg.max_timeout,
            cfg.wrapper_timeout,
            a_timeout,
            a_model,
        )
        prompt = _read_nofollow(prompt_path, PROMPT_CAP)
    except JobRejected as exc:
        return _outcome("rejected", reason=str(exc)), False
    except (OSError, _TooBig) as exc:
        return _outcome(
            "error", reason=f"задание не прочитано: {exc}"[:300]
        ), _withdrawn(jobdir)
    finally:
        # Промт с одноразовым кодом живёт на диске только до чтения.
        _unlink(prompt_path)
    try:
        return await _execute_job(cfg, jobdir, prompt, timeout, version, snapshot_path)
    except OSError as exc:
        return _outcome(
            "error", reason=f"команда службы не запустилась: {exc}"[:300]
        ), False


async def _execute_job(
    cfg: Config,
    jobdir: str,
    prompt: bytes,
    timeout: int,
    version: int,
    snapshot_path: str,
) -> tuple[dict[str, object], bool]:
    """Прогон после разбора задания: версия 1 — как раньше, 2 — со снимком."""
    has_tar = os.path.lexists(snapshot_path)
    if version == JOB_VERSION_ADVISOR:
        if has_tar:
            return _outcome(
                "rejected",
                reason=f"в задании advisor лежит {SPOOL_SNAPSHOT}: у профиля advisor "
                "снимка исходников нет",
            ), False
        _model, _timeout, problem = advisor_settings(cfg)
        if problem:
            return _outcome(
                "rejected", reason=f"профиль advisor недоступен: {problem}"[:300]
            ), False
        return await _run(cfg, jobdir, prompt, timeout, argv=cfg.advisor_argv)
    if version == JOB_VERSION:
        if has_tar:
            return _outcome(
                "rejected",
                reason=f"в задании version=1 лежит {SPOOL_SNAPSHOT}: снимок "
                "возможен только в version=2",
            ), False
        return await _run(cfg, jobdir, prompt, timeout)
    if not has_tar:
        return _outcome(
            "rejected", reason=f"version=2 требует {SPOOL_SNAPSHOT}, его нет"
        ), False
    try:
        unpacker = load_unpacker(
            cfg.snapshot_unpacker or default_unpacker_path(), cfg.unpacker_trusted_uid
        )
    except ConfigError as exc:
        return _outcome("rejected", reason=f"снимок принять нечем: {exc}"[:300]), False
    return await _run(cfg, jobdir, prompt, timeout, snapshot_path, unpacker)


async def process_job(cfg: Config, name: str) -> None:
    """Взять задание, прогнать, записать результат, убрать за собой."""
    jobdir = os.path.join(cfg.spool, name)
    if not _claim(jobdir):
        return
    try:
        outcome, abandoned = await _execute(cfg, jobdir)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - одно задание не роняет службу
        log.exception("job %s failed", name)
        outcome, abandoned = (
            _outcome("error", reason=f"{type(exc).__name__}: {exc}"[:300]),
            False,
        )
    with contextlib.suppress(OSError):
        _write_atomic(jobdir, SPOOL_RESULT, json.dumps(outcome).encode())
    if abandoned or not os.path.isdir(jobdir):
        shutil.rmtree(jobdir, ignore_errors=True)


def _job_dirs(spool: str) -> list[os.DirEntry[str]]:
    with os.scandir(spool) as entries:
        return [
            e
            for e in entries
            if _JOB_NAME.match(e.name) and e.is_dir(follow_symlinks=False)
        ]


def _pending(cfg: Config) -> list[str]:
    ready: list[tuple[float, str]] = []
    for entry in _job_dirs(cfg.spool):
        job = os.path.join(entry.path, SPOOL_JOB)
        if os.path.exists(os.path.join(entry.path, SPOOL_CLAIMED)):
            continue
        with contextlib.suppress(OSError):
            ready.append((os.lstat(job).st_mtime, entry.name))
    return [name for _, name in sorted(ready)]


async def run_pending(cfg: Config) -> int:
    """Обработать все ожидающие задания, по одному. Сколько обработано."""
    handled = 0
    for name in _pending(cfg):
        await process_job(cfg, name)
        handled += 1
    return handled


def _sweep(cfg: Config) -> None:
    """Убрать то, за чем некому прийти: хаб упал, не забрав результат."""
    now = time.time()
    for entry in _job_dirs(cfg.spool):
        with contextlib.suppress(OSError):
            if now - entry.stat(follow_symlinks=False).st_mtime > (
                cfg.stale_sec + cfg.max_timeout
            ):
                shutil.rmtree(entry.path, ignore_errors=True)


async def _heartbeat(cfg: Config) -> None:
    while True:
        try:
            payload = heartbeat_payload(cfg)
        except Exception as exc:  # noqa: BLE001 - жизнь важнее возможностей
            log.warning("capabilities not computed: %s", exc)
            payload = json.dumps({"pid": os.getpid(), "time": time.time()}).encode()
        try:
            _write_atomic(cfg.spool, SPOOL_HEARTBEAT, payload)
        except OSError as exc:
            log.warning("heartbeat not written: %s", exc)
        await asyncio.sleep(cfg.heartbeat_every)


def recover_orphans(cfg: Config) -> int:
    """Закрыть задания, взятые ПРОШЛЫМ процессом службы и брошенные на полпути.

    Выбран вариант «закрыть с названной причиной», а не «подобрать и
    перезапустить»: промт после чтения удалён, а повторный прогон одного и
    того же задания тратит платное ревью дважды. Хаб, если ещё ждёт, получит
    result.json с причиной сразу; если не ждёт — каталог уберёт sweep.
    """
    closed = 0
    for entry in _job_dirs(cfg.spool):
        claimed = os.path.join(entry.path, SPOOL_CLAIMED)
        result = os.path.join(entry.path, SPOOL_RESULT)
        if not os.path.exists(claimed) or os.path.exists(result):
            continue
        outcome = _outcome(
            "error",
            reason="служба-исполнитель была перезапущена во время прогона: "
            "прогон потерян, повтор не делается",
        )
        with contextlib.suppress(OSError):
            _write_atomic(entry.path, SPOOL_RESULT, json.dumps(outcome).encode())
            closed += 1
    return closed


async def serve(cfg: Config) -> None:
    """Главный цикл: heartbeat, задания, уборка. Возвращается только отменой."""
    with contextlib.suppress(OSError):
        recover_orphans(cfg)
    beat = asyncio.create_task(_heartbeat(cfg))
    try:
        while True:
            try:
                await run_pending(cfg)
                _sweep(cfg)
            except OSError as exc:
                log.warning("spool not readable: %s", exc)
            await asyncio.sleep(cfg.poll)
    finally:
        beat.cancel()


def check_install(argv: list[str]) -> int:
    """Проверка установки: распаковщик грузится по пути и hub не нужен.

    Печатает JSON и возвращает 0, либо причину в stderr и 3. Ни конфиг, ни
    spool не читаются: проверяется только то, что установлено ФАЙЛАМИ.
    """
    trusted = 0
    if "--trusted-uid" in argv:
        trusted = int(argv[argv.index("--trusted-uid") + 1])
    path = default_unpacker_path()
    try:
        module = load_unpacker(path, trusted)
    except ConfigError as exc:
        print(f"haiplane-review-runner: {exc}", file=sys.stderr)
        return 3
    print(
        json.dumps(
            {
                "unpacker": path,
                "hub_imported": any(
                    name == "hub" or name.startswith("hub.") for name in sys.modules
                ),
                "limits": module.Limits().max_bytes,
            }
        )
    )
    return 0


def main() -> int:
    if "--check-install" in sys.argv[1:]:
        return check_install(sys.argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        cfg = load_config(os.environ)
    except ConfigError as exc:
        print(f"haiplane-review-runner: {exc}", file=sys.stderr)
        return 2

    async def _main() -> None:
        task = asyncio.current_task()
        assert task is not None
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
        await serve(cfg)

    with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
        asyncio.run(_main())
    return 0


if __name__ == "__main__":
    sys.exit(main())
