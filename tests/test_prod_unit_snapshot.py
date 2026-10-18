"""Снимок служб прода в deploy/systemd/prod (#1849, E0 спайка #1792).

Тест читает только файлы репозитория: без сети и без прода.

AC-1: в снимке виден только белый список имён со значениями, прошедшими
проверку своего типа; всё прочее - ровно ``<скрыто>``. Белый список и проверки
типов перенесены из скрипта снятия (e0-capture.py, v4) и служат здесь
независимой второй проверкой уже лежащих в репозитории файлов.

AC-2: ``CAPTURE.txt`` не тронут (sha256), а файлы снимка, таблица README и
разделы ``CAPTURE.txt`` описывают один и тот же набор источников.

Правило границ раздела ``CAPTURE.txt`` (закреплено ``parse_sections``):
раздел начинается строкой ``--- файл <путь>`` или ``--- EnvironmentFile
<путь> (ignore_errors=...)`` и идёт до следующей строки, начинающейся с
``--- ``, ``##########``, ``TimersCalendar=`` или строки проверки каталога
``/путь: mode=0o... uid=.. gid=..``, не включая её. Эти последние две строки
печатает сам скрипт снятия, в файл хоста они не входят и остаются только в
``CAPTURE.txt``. Строка заголовка в файл не входит. Строки делятся только по LF.
Хвост: юниты, таймеры и drop-in-ы скрипт печатает одним ``print`` (текст
файла уже кончается LF), поэтому к телу раздела добавлен лишний LF и файл
хоста равен телу без последнего LF; tmpfiles и EnvironmentFile скрипт
печатает построчно, там файл равен телу целиком (``host_file_text``).
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO_ROOT / "deploy" / "systemd" / "prod"
SNAPSHOT_PREFIX = "deploy/systemd/prod/"
CAPTURE_SHA256 = "c678952f49aa266c6d7ac4ac6ea398fdca7968ea3eb71bb0dc1de87f894ae128"  # pragma: allowlist secret

# Настоящее имя прода собрано, чтобы тест не ловил сам себя (tests/test_no_legacy_name.py).
PROD_NAME = "open" + "claw-hub"
DROP_IN_DIR = f"/{PROD_NAME}.service.d/"

# Источники, у которых в снимке нет файла-копии.
NAMES_ONLY_SOURCES = {f"/etc/{PROD_NAME}/secrets.env"}
CAPTURE_ONLY_PREFIXES = (
    "/etc/sudoers.d/",
)  # правила sudo остаются только в CAPTURE.txt
NON_SOURCE_FILES = {"README.md", "CAPTURE.txt"}

MASK = "<скрыто>"
MASKED_NAME = "<имя скрыто>"

# --- белый список и проверки типов (как в e0-capture.py, v4) ----------------
OPAQUE_RUN = re.compile(r"[A-Za-z0-9]{24,}")
ODD_SEPARATORS = re.compile("[\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
SECRET_WORD = re.compile(
    r"(?i)(token|secret|passw|bearer|authori[sz]ation|api[_-]?key|private|credential|cookie_sign)"
)
SECRET_WORD_OK = re.compile(r"(?i)token_ceiling")
KEY_SHAPE = re.compile(
    r"(sk-|crsr_|ochk_|ghp_|gho_|ghs_|github_pat_|glpat-|xox[abps]-|eyJ|-----BEGIN|://[^/\s]*@)"
)
WORD = re.compile(r"^[A-Za-z0-9_.:/@+,-]{1,200}$")
NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
BACKSLASH_WORD = re.compile(r"\S*\\\S*")


def has_secret_word(s: str) -> bool:
    return bool(SECRET_WORD.search(SECRET_WORD_OK.sub("", s)))


def plain_word(s: str) -> bool:
    return (
        bool(WORD.match(s))
        and not KEY_SHAPE.search(s)
        and not has_secret_word(s)
        and not OPAQUE_RUN.search(s)
    )


def t_enum(v: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9_-]{1,24}", v)) and not KEY_SHAPE.search(v)


def t_number(v: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{1,12}(\.[0-9]{1,6})?", v))


def t_path(v: str) -> bool:
    return bool(re.fullmatch(r"/[A-Za-z0-9_./-]{0,200}", v)) and plain_word(v)


def t_pathlist(v: str) -> bool:
    return all(t_path(p) for p in v.split(":")) if v else False


def t_model(v: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.:/-]{1,80}", v)) and plain_word(v)


def t_models(v: str) -> bool:
    return all(t_model(p.strip()) for p in v.split(",")) if v else False


def t_hosts(v: str) -> bool:
    if not v:
        return False
    return all(
        re.fullmatch(r"[a-z0-9.-]{1,100}(:[0-9]{1,5})?", p.strip())
        for p in v.split(",")
    )


def t_url(v: str) -> bool:
    return bool(
        re.fullmatch(r"https?://[a-z0-9.-]{1,100}(:[0-9]{1,5})?(/[A-Za-z0-9_./-]*)?", v)
    )


def t_repo(v: str) -> bool:
    return bool(
        re.fullmatch(r"[A-Za-z0-9_.-]{1,60}/[A-Za-z0-9_.-]{1,100}", v)
    ) or t_path(v)


EXEC_OK = {
    "/usr/bin/python3",
    f"/opt/{PROD_NAME}/venv/bin/haiplane-hub",
    "/opt/haiplane-hub/venv/bin/haiplane-hub",
    "/usr/local/sbin/haiplane-hub-backup",
    "sudo",
    "/usr/bin/sudo",
    "/usr/local/bin/haiplane-review-run",
}
FLAGS_NOARG = {"-I", "-n", "--yolo"}
FLAGS_ARG = {"-u", "--model"}
SUBCMD = {"daily", "hourly"}


def _command_words(words: list[str]) -> bool:
    if not words or words[0] not in EXEC_OK:
        return False
    i = 1
    while i < len(words):
        w = words[i]
        if w in FLAGS_NOARG or w in SUBCMD or w in EXEC_OK:
            i += 1
        elif (
            w in FLAGS_ARG
            and i + 1 < len(words)
            and t_model(words[i + 1])
            and ":" not in words[i + 1]
        ):
            i += 2
        elif w.startswith("/") and t_path(w):
            i += 1
        else:
            return False
    return True


def t_command(v: str) -> bool:
    try:
        words = shlex.split(v)
    except ValueError:
        return False
    return _command_words(words)


def t_names(v: str) -> bool:
    return bool(v.split()) and all(
        re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", w) and plain_word(w) for w in v.split()
    )


def _no_dupes(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def t_argv(v: str) -> bool:
    if "\\" in v:
        return False
    try:
        data = json.loads(v, object_pairs_hook=_no_dupes)
    except ValueError:
        return t_command(v)
    return (
        isinstance(data, list)
        and all(isinstance(w, str) for w in data)
        and _command_words(data)
    )


PARAM_KEYS = {
    "reasoning_effort",
    "effort",
    "fast",
    "temperature",
    "max_tokens",
    "thinking",
    "verbosity",
}


def t_params(v: str) -> bool:
    if "\\" in v:
        return False
    try:
        data = json.loads(v, object_pairs_hook=_no_dupes)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    for key, val in data.items():
        if key not in PARAM_KEYS:
            return False
        if not (
            isinstance(val, (bool, int, float))
            or (isinstance(val, str) and t_enum(val))
        ):
            return False
    return True


TYPE = {
    "HAIPLANE_AUTO_APPROVE_MAX_CLASS": t_enum,
    "HAIPLANE_CSRF_MODE": t_enum,
    "HAIPLANE_STEWARD_MODE": t_enum,
    "HAIPLANE_SDD_AC_LOCATOR": t_enum,
    "HAIPLANE_WORKTREE_PER_TASK": t_enum,
    "HAIPLANE_LOCAL_REVIEW_TRANSPORT": t_enum,
    "HAIPLANE_LOCAL_REVIEW_SNAPSHOT": t_enum,
    "HAIPLANE_LOCAL_REVIEW_SANDBOX": t_enum,
    "HAIPLANE_HUB_COOKIE_SECURE": t_enum,
    "HAIPLANE_LOCAL_REVIEW_TIMEOUT_SEC": t_number,
    "HAIPLANE_LOCAL_REVIEW_TOKEN_CEILING": t_number,
    "HAIPLANE_EXECUTOR_TOKEN_CEILING": t_number,
    "HAIPLANE_EXECUTOR_CENTS_CEILING": t_number,
    "HAIPLANE_REVIEW_RANDOM_DEEP_SHARE": t_number,
    "HAIPLANE_HUB_PORT": t_number,
    "HAIPLANE_REVIEW_RUNNER_MAX_TIMEOUT_SEC": t_number,
    "HAIPLANE_REVIEW_RUNNER_WRAPPER_TIMEOUT_SEC": t_number,
    "HAIPLANE_LOCAL_REVIEW_SCRATCH_DIR": t_path,
    "HAIPLANE_LOCAL_REVIEW_SPOOL_DIR": t_path,
    "HAIPLANE_HUB_DB": t_path,
    "HAIPLANE_HUB_HOME": t_path,
    "HAIPLANE_TRANSCRIPTS_DIR": t_path,
    "HAIPLANE_REVIEW_RUNNER_SCRATCH_DIR": t_path,
    "HAIPLANE_REVIEW_RUNNER_SPOOL_DIR": t_path,
    "HOME": t_path,
    "PATH": t_pathlist,
    "HAIPLANE_LOCAL_REVIEW_MODEL": t_model,
    "HAIPLANE_EXECUTOR_MODEL": t_model,
    "HAIPLANE_STEWARD_ADVISOR_LOCAL_MODEL": t_model,
    "HAIPLANE_STEWARD_ADVISOR_MODELS": t_models,
    "HAIPLANE_HUB_ALLOWED_HOSTS": t_hosts,
    "HAIPLANE_HUB_HOST": t_hosts,
    "HAIPLANE_HUB_URL": t_url,
    "HAIPLANE_HUB_REPO": t_repo,
    "HAIPLANE_WORKSPACE_REPO": t_repo,
    "HAIPLANE_LOCAL_REVIEW_CMD": t_command,
    "HAIPLANE_REVIEW_RUNNER_ARGV": t_argv,
    "HAIPLANE_REVIEW_RUNNER_ARGV_ADVISOR": t_argv,
    "HAIPLANE_EXECUTOR_MODEL_PARAMS": t_params,
}

_STDIO = r"(append|file):/[A-Za-z0-9_./-]+|journal|null|inherit"


def _t_calendar(v: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9 *:/,.~-]{1,60}", v))


DIRECTIVE = {
    "Description": lambda v: (
        bool(re.fullmatch(r"[A-Za-z0-9 ():,./+-]{0,120}", v)) and not has_secret_word(v)
    ),
    "After": t_names,
    "Wants": t_names,
    "Requires": t_names,
    "WantedBy": t_names,
    "PartOf": t_names,
    "Type": t_enum,
    "User": t_enum,
    "Group": t_enum,
    "SupplementaryGroups": t_names,
    "UMask": t_number,
    "Restart": t_enum,
    "RestartSec": t_number,
    "RestartPreventExitStatus": t_names,
    "TimeoutStartSec": t_number,
    "TimeoutStopSec": t_number,
    "WorkingDirectory": t_path,
    "EnvironmentFile": lambda v: t_path(v.lstrip("-")),
    "ExecStart": t_command,
    "ExecStartPre": t_command,
    "ExecStartPost": t_command,
    "ExecStop": t_command,
    "StandardOutput": lambda v: bool(re.fullmatch(_STDIO, v)),
    "StandardError": lambda v: bool(re.fullmatch(_STDIO, v)),
    "ReadWritePaths": lambda v: all(t_path(p.lstrip("-")) for p in v.split()),
    "ReadOnlyPaths": lambda v: all(t_path(p.lstrip("-")) for p in v.split()),
    "TemporaryFileSystem": lambda v: bool(
        re.fullmatch(r"/[A-Za-z0-9_./-]+(:[a-z0-9=,]+)?", v)
    ),
    "NoNewPrivileges": t_enum,
    "PrivateTmp": t_enum,
    "ProtectSystem": t_enum,
    "ProtectHome": t_enum,
    "ProtectKernelTunables": t_enum,
    "ProtectKernelModules": t_enum,
    "ProtectControlGroups": t_enum,
    "RestrictAddressFamilies": t_names,
    "RestrictNamespaces": t_enum,
    "LockPersonality": t_enum,
    "MemoryDenyWriteExecute": t_enum,
    "OnCalendar": _t_calendar,
    "Persistent": t_enum,
    "RandomizedDelaySec": t_number,
    "Unit": t_names,
    "OnBootSec": t_names,
    "OnUnitActiveSec": t_names,
}


def shown(name: str, value: str) -> str:
    """Что напечатало бы снятие для пары имя=значение (v4, функция show)."""
    check = TYPE.get(name)
    ok = (
        check is not None
        and "\n" not in value
        and "\\" not in value
        and len(value) <= 400
        and not KEY_SHAPE.search(value)
        and not OPAQUE_RUN.search(value)
        and check(value)
    )
    return f"{name}={value if ok else MASK}"


# --- проверки файлов снимка ---------------------------------------------------
def generic_line_problems(line: str) -> list[str]:
    """Проверки для каждой строки каждого файла снимка (включая README и CAPTURE)."""
    problems = []
    if KEY_SHAPE.search(line):
        problems.append("вид ключа")
    if BACKSLASH_WORD.search(line):
        problems.append("слово с обратной косой чертой")
    if OPAQUE_RUN.search(line):
        problems.append("сплошная буквенно-цифровая последовательность от 24 символов")
    if ODD_SEPARATORS.search(line):
        problems.append("необычный разделитель строк")
    return problems


def unit_line_problems(line: str) -> list[str]:
    if line == "" or re.fullmatch(r"\[[A-Za-z]+\]", line):
        return []
    if line != line.strip():
        return ["пробелы по краям строки"]
    if line.startswith(("#", ";")):
        return ["комментарий"]
    if re.fullmatch(re.escape(MASK) + r"=([0-9]+|" + re.escape(MASK) + ")", line):
        return []  # имя директивы скрыто последним фильтром снятия
    m = re.fullmatch(r"([A-Za-z]+)=(.*)", line)
    if not m:
        return ["строка не директива"]
    key, value = m.groups()
    if key == "Environment":
        return _environment_problems(value)
    if value == MASK:
        return []
    check = DIRECTIVE.get(key)
    if check is None:
        return [f"{key}: неизвестная директива с открытым значением"]
    if (
        "\\" in value
        or KEY_SHAPE.search(value)
        or OPAQUE_RUN.search(value)
        or not check(value)
    ):
        return [f"{key}: значение не прошло проверку типа"]
    return []


def _environment_problems(value: str) -> list[str]:
    if value == "":
        return []
    try:
        parts = shlex.split(value)
    except ValueError:
        return ["Environment: не разбирается"]
    if not parts:
        return ["Environment: пусто"]
    if " ".join(shlex.quote(p) for p in parts) != value:
        return ["Environment: нетипичное оформление кавычек"]
    problems = []
    for part in parts:
        m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", part, flags=re.S)
        if not m:
            problems.append("Environment: не присваивание")
            continue
        name, val = m.groups()
        if not NAME.match(name) or KEY_SHAPE.search(name):
            problems.append("Environment: имя вне формата")
        elif part != shown(name, val):
            problems.append(f"Environment: значение {name} вне белого списка или типа")
    return problems


RUNNER_ENV = "etc/haiplane-review-runner.env"
RUNNER_STOP_NAME = "HAIPLANE_REVIEW_RUNNER_ARGV"


def env_line_problems(line: str, bare_name_ok: bool = False) -> list[str]:
    if line == "":
        return ["пустая строка"]
    if line.startswith(("#", ";")):
        return ["комментарий"]
    if line != line.strip():
        return ["пробелы по краям строки"]
    if bare_name_ok and line == RUNNER_STOP_NAME:
        return []  # разбор файла остановлен на этом имени (раздел НЕПОЛНО)
    m = re.fullmatch(r"([A-Za-z_][A-Za-z_0-9]*)=(.*)", line)
    if not m:
        return ["строка не присваивание"]
    name, value = m.groups()
    if not NAME.match(name) or KEY_SHAPE.search(name):
        return ["имя переменной вне формата"]
    if line != shown(name, value):
        return [f"{name}: значение вне белого списка или типа"]
    return []


ASSIGNMENT = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]{2,})=")
SUDOERS_RULE = re.compile(r"\S+\s+ALL\s*=\s*\(\S+\)\s+(NOPASSWD:\s*)?/\S+.*")
SUDOERS_HEADER = "########## sudoers.d"


def _unit_problems(unit: str) -> list[str]:
    """Единица разметки целиком: NAME=VALUE ровно с начала, значение - весь остаток."""
    problems = []
    for m in ASSIGNMENT.finditer(unit):
        name, value = m.group(1), unit[m.end() :]
        if m.start() != 0 or f"{name}={value}" != shown(name, value):
            problems.append(f"{name}: значение вне белого списка или типа")
    return problems


def _raw_line_problems(line: str) -> list[str]:
    """Строка снятия: слова через пробел, оболочка Environment= и одиночные кавычки снимаются."""
    problems = []
    for token in line.split():
        token = token.removeprefix("Environment=")
        if token.startswith("'"):
            token = token[1:]
            token = token.removesuffix("'")
        problems += _unit_problems(token)
    return problems


def _markdown_line_problems(line: str) -> list[str]:
    problems = []
    for span in re.findall(r"`([^`]*)`", line):
        problems += _unit_problems(span)
    rest = re.sub(r"`[^`]*`", " ", line)
    for token in re.split(r"[\s|]+", rest):
        problems += _unit_problems(token)
    return problems


def prose_problems(rel: str, lines: list[str]) -> list[tuple[int, str]]:
    """README и CAPTURE: присваивания NAME=VALUE подчиняются белому списку и типам."""
    out: list[tuple[int, str]] = []
    in_fence = False
    in_sudoers = False
    for number, line in enumerate(lines, 1):
        if rel == "CAPTURE.txt":
            if line.startswith("##########"):
                in_sudoers = line.startswith(SUDOERS_HEADER)
            if in_sudoers and SUDOERS_RULE.fullmatch(line):
                continue  # правило sudoers: ``ALL=`` это синтаксис, не переменная
            found = _raw_line_problems(line)
        else:
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            found = (
                _raw_line_problems(line) if in_fence else _markdown_line_problems(line)
            )
        out.extend((number, p) for p in found)
    return out


def readme_table_problems(text: str) -> list[str]:
    """Ячейки таблицы переменных README, с обратными кавычками и без них."""
    problems = []
    for line in text.split("\n"):
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [c.strip().strip("`") for c in line.strip("|").split("|")]
        if len(cells) >= 4 and re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", cells[0]):
            if f"{cells[0]}={cells[2]}" != shown(cells[0], cells[2]):
                problems.append(
                    f"{cells[0]}: значение в таблице вне белого списка или типа"
                )
    return problems


def tmpfiles_line_problems(line: str) -> list[str]:
    if line == "":
        return []
    words = line.split(None, 6)
    if len(words) < 6 or line != line.strip():
        return ["запись tmpfiles разобрана не полностью"]
    head = words[:6]
    if not all(plain_word(w) or w in {"-", MASK} for w in head):
        return ["запись tmpfiles: слово вне формата"]
    if len(words) == 7 and words[6] not in {"-", MASK}:
        return ["запись tmpfiles: аргумент (содержимое) не скрыт"]
    return []


def snapshot_violations(root: Path) -> list[str]:
    """Все нарушения AC-1 в каталоге снимка ``root`` (пусто = чисто)."""
    out: list[str] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        for problem in generic_line_problems(rel):
            out.append(f"{rel}: имя файла: {problem}")
        if path.name == "secrets.env" or (
            "secret" in path.name.lower() and path.suffix == ".env"
        ):
            out.append(f"{rel}: файл-копия секретов недопустим")
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            out.append(f"{rel}: не UTF-8")
            continue
        if rel in NON_SOURCE_FILES:
            checker = None
            if rel == "README.md":
                out.extend(f"{rel}: {p}" for p in readme_table_problems(text))
        elif rel.startswith("etc/systemd/system/") and rel.endswith(
            (".service", ".timer", ".conf")
        ):
            checker = unit_line_problems
        elif rel.startswith("etc/tmpfiles.d/") and rel.endswith(".conf"):
            checker = tmpfiles_line_problems
        elif rel.startswith("etc/") and rel.endswith(".env"):
            checker = env_line_problems
        else:
            out.append(f"{rel}: файл вне известных видов")
            continue
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()  # завершающий LF
        if rel in NON_SOURCE_FILES:
            out.extend(f"{rel}:{n}: {p}" for n, p in prose_problems(rel, lines))
        for number, line in enumerate(lines, 1):
            problems = generic_line_problems(line)
            if checker is env_line_problems:
                last = rel == RUNNER_ENV and number == len(lines)
                problems += env_line_problems(line, bare_name_ok=last)
            elif checker is not None:
                problems += checker(line)
            out.extend(f"{rel}:{number}: {p}" for p in problems)
    return out


# --- разбор CAPTURE.txt -------------------------------------------------------
# Строка проверки каталога, которую печатает скрипт снятия после файла tmpfiles.
STAT_LINE = re.compile(r"/\S+: mode=0o[0-7]+ uid=\d+ gid=\d+\n?")
UNIT_DIR = "/etc/systemd/system/"


def host_file_text(host_path: str, body: str) -> str:
    """Файл хоста по телу раздела: без лишнего LF, добавленного ``print``."""
    if host_path.startswith(UNIT_DIR):
        assert body.endswith("\n\n"), host_path
        return body[:-1]
    return body


def parse_sections(text: str) -> list[tuple[str, str, str]]:
    """Разделы источников: (вид, путь, тело).

    Вид - ``file`` (``--- файл <путь>``) или ``env`` (``--- EnvironmentFile
    <путь> (ignore_errors=..)``). Тело - все строки до следующей строки,
    начинающейся с границы раздела (``BOUNDARY``; её в тело не входит), побайтно.
    """
    pieces = text.split("\n")
    lines = [p + "\n" for p in pieces[:-1]] + ([pieces[-1]] if pieces[-1] else [])
    sections: list[tuple[str, str, str]] = []
    current: tuple[str, str] | None = None
    body: list[str] = []

    def close() -> None:
        if current is not None:
            sections.append((current[0], current[1], "".join(body)))

    for line in lines:
        if line.startswith(
            ("--- ", "##########", "TimersCalendar=")
        ) or STAT_LINE.fullmatch(line):
            close()
            body = []
            current = None
            m = re.fullmatch(r"--- файл (\S+)\n", line)
            if m:
                current = ("file", m.group(1))
            m = re.fullmatch(
                r"--- EnvironmentFile (\S+) \(ignore_errors=(?:yes|no)\)\n", line
            )
            if m:
                current = ("env", m.group(1))
        elif current is not None:
            body.append(line)
    close()
    return sections


def source_sections(capture: str) -> dict[str, str]:
    """Путь на хосте -> тело раздела для источников, у которых в снимке есть файл."""
    bodies: dict[str, set[str]] = {}
    for _kind, host_path, body in parse_sections(capture):
        if host_path in NAMES_ONLY_SOURCES or host_path.startswith(
            CAPTURE_ONLY_PREFIXES
        ):
            continue
        bodies.setdefault(host_path, set()).add(body)
    differing = sorted(p for p, b in bodies.items() if len(b) != 1)
    assert not differing, f"один источник описан в CAPTURE.txt по-разному: {differing}"
    return {p: host_file_text(p, next(iter(b))) for p, b in bodies.items()}


def unit_blocks(capture: str) -> dict[str, dict[str, str]]:
    """Блоки ``########## <юнит>`` со свойствами UnitFileState, ActiveState, FragmentPath, DropInPaths."""
    blocks: dict[str, dict[str, str]] = {}
    name = ""
    for line in capture.split("\n"):
        if line.startswith("##########"):
            name = line[len("########## ") :]
            continue
        m = re.fullmatch(
            r"(UnitFileState|ActiveState|FragmentPath|DropInPaths)=(.*)", line
        )
        if m and name.endswith((".service", ".timer")):
            blocks.setdefault(name, {})[m.group(1)] = m.group(2)
    return blocks


def host_to_repo(host_path: str) -> str:
    return SNAPSHOT_PREFIX + host_path.lstrip("/")


def secrets_names(capture: str) -> list[list[str]]:
    result = []
    for kind, host_path, body in parse_sections(capture):
        if kind == "env" and host_path in NAMES_ONLY_SOURCES:
            result.append([ln.split("=", 1)[0] for ln in body.split("\n") if ln])
    return result


def readme_rows(readme: str) -> list[tuple[str, str, str]]:
    rows = re.findall(
        r"^\| `(deploy/systemd/prod/[^`]+)` \| `(/[^`]+)` \| ([^|]+?) \|$",
        readme,
        flags=re.M,
    )
    return [(a, b, c) for a, b, c in rows]


def readme_secret_names(readme: str) -> list[str]:
    lines = readme.split("\n")
    start = next(
        (
            i
            for i, ln in enumerate(lines)
            if ln.startswith("### ") and "secrets.env" in ln
        ),
        None,
    )
    assert start is not None, (
        "в README нет раздела '### ... secrets.env ...' с перечнем имён"
    )
    names = []
    for ln in lines[start + 1 :]:
        if ln.startswith("#"):
            break
        m = re.fullmatch(r"- `([A-Z][A-Z0-9_]*)`", ln)
        if m:
            names.append(m.group(1))
    return names


# --- AC-1 ---------------------------------------------------------------------
# Непрозрачное имя с разделителями (длина 39), собрано по частям.
OPAQUE_NAME = "_".join(["HAIPLANE", "ABCDEFGHIJ", "KLMNOPQRST", "UVWXYZ012"])
# Значения для отрицательных примеров собраны по частям (не литералами).
DEMO_VALUE = "demo" + "_value_" + "42"
NEGATIVE_EXAMPLES: dict[str, tuple[str, str]] = {
    "одиночные кавычки, значение вне типа": (
        "etc/systemd/system/x.service.d/a.conf",
        "[Service]\nEnvironment='HAIPLANE_HUB_URL=https://u:p@host.example'\n",
    ),
    "две пары, вторая вне белого списка": (
        "etc/systemd/system/x.service.d/a.conf",
        "[Service]\nEnvironment=HAIPLANE_CSRF_MODE=require FOO=bar\n",
    ),
    "две пары, вторая - ключ": (
        "etc/systemd/system/x.service.d/a.conf",
        "[Service]\nEnvironment=HAIPLANE_CSRF_MODE=require HAIPLANE_HUB_PORT=ghp_aaaa\n",
    ),
    "curl --user u:p в ExecStart": (
        "etc/systemd/system/x.service",
        "[Service]\nExecStart=/usr/bin/curl --user u:p https://host.example\n",
    ),
    "JSON с дублем ключа": (
        "etc/systemd/system/x.service.d/a.conf",
        '[Service]\nEnvironment=\'HAIPLANE_EXECUTOR_MODEL_PARAMS={"effort":"high","effort":"low"}\'\n',
    ),
    "JSON с \\uXXXX": (
        "etc/systemd/system/x.service.d/a.conf",
        '[Service]\nEnvironment=\'HAIPLANE_EXECUTOR_MODEL_PARAMS={"eff\\u006frt":"high"}\'\n',
    ),
    "JSON с ключом api_key": (
        "etc/systemd/system/x.service.d/a.conf",
        '[Service]\nEnvironment=\'HAIPLANE_EXECUTOR_MODEL_PARAMS={"api_key":"abc"}\'\n',
    ),
    "\\x67lpat- в Environment": (
        "etc/systemd/system/x.service.d/a.conf",
        "[Service]\nEnvironment=HAIPLANE_EXECUTOR_MODEL=\\x67lpat-abc\n",
    ),
    "\\147 в ExecStart": (
        "etc/systemd/system/x.service",
        "[Service]\nExecStart=/usr/bin/python3 \\147lpat-abc\n",
    ),
    "U+2028 внутри строки": (
        "etc/systemd/system/x.service.d/a.conf",
        "[Service]\nEnvironment=HAIPLANE_CSRF_MODE=require\u2028FOO=bar\n",
    ),
    "CR внутри строки": (
        "etc/systemd/system/x.service.d/a.conf",
        "[Service]\nEnvironment=HAIPLANE_CSRF_MODE=require\rFOO=bar\n",
    ),
    "сплошная последовательность в значении разрешённого имени": (
        "etc/systemd/system/x.service.d/a.conf",
        "[Service]\nEnvironment=HAIPLANE_EXECUTOR_MODEL=" + "a" * 30 + "\n",
    ),
    "комментарий юнита": (
        "etc/systemd/system/x.service",
        "[Service]\n# password is hunter2\nType=simple\n",
    ),
    "неизвестная директива с открытым значением": (
        "etc/systemd/system/x.service",
        "[Service]\nSomething=value\n",
    ),
    "Authorization: Bearer в Description": (
        "etc/systemd/system/x.service",
        "[Unit]\nDescription=Authorization: Bearer abc\n",
    ),
    "неверное имя в группе Environment": (
        "etc/systemd/system/x.service.d/a.conf",
        "[Service]\nEnvironment=HAIPLANE_CSRF_MODE=require 1BAD=x\n",
    ),
    "пробел до кавычки в Environment": (
        "etc/systemd/system/x.service.d/a.conf",
        '[Service]\nEnvironment=HAIPLANE_CSRF_MODE= "require"\n',
    ),
    "файл копии secrets.env": (
        f"etc/{PROD_NAME}/secrets.env",
        "CURSOR_API_KEY=<скрыто>\n",
    ),
    "EnvironmentFile: токен открыто": (
        f"etc/{PROD_NAME}/{PROD_NAME}.env",
        "HAIPLANE_HUB_TOKENS=abc\n",
    ),
    "EnvironmentFile: export": (
        f"etc/{PROD_NAME}/{PROD_NAME}.env",
        "export HAIPLANE_HUB_PORT=8080\n",
    ),
    "EnvironmentFile: пробел до кавычки": (
        f"etc/{PROD_NAME}/{PROD_NAME}.env",
        'HAIPLANE_HUB_PORT= "8080"\n',
    ),
    "EnvironmentFile: комментарий": (
        f"etc/{PROD_NAME}/{PROD_NAME}.env",
        "# HAIPLANE_HUB_TOKENS=abc\n",
    ),
    "EnvironmentFile: ключ в имени переменной": (
        f"etc/{PROD_NAME}/{PROD_NAME}.env",
        "HAIPLANE_ghp_abc=1\n",
    ),
    "EnvironmentFile: многострочное значение": (
        f"etc/{PROD_NAME}/{PROD_NAME}.env",
        f"HAIPLANE_HUB_HOME=/var/lib/{PROD_NAME}\nsecond-line-of-a-value\n",
    ),
    "README: ALL= вне sudoers": (
        "README.md",
        f"Пример: ALL={DEMO_VALUE}\n",
    ),
    "CAPTURE: ALL= вне раздела sudoers": (
        "CAPTURE.txt",
        f"\n########## x\nALL={DEMO_VALUE}\n",
    ),
    "CAPTURE: ALL= в sudoers, но не правило": (
        "CAPTURE.txt",
        f"\n########## sudoers.d (x)\nALL={DEMO_VALUE}\n",
    ),
    "README: хвост пунктуации после значения": (
        "README.md",
        "Пример: HAIPLANE_HUB_PORT=8080);;;\n",
    ),
    "README: хвост пунктуации после маски": (
        "README.md",
        "Пример: HAIPLANE_HUB_TOKENS=<скрыто>;;;\n",
    ),
    "CAPTURE: хвост пунктуации после маски": (
        "CAPTURE.txt",
        "HAIPLANE_HUB_TOKENS=<скрыто>;;;\n",
    ),
    "README: ячейка таблицы без кавычек": (
        "README.md",
        f"| HAIPLANE_HUB_TOKENS | x.env | {DEMO_VALUE} | действует |\n",
    ),
    "README: открытый токен": (
        "README.md",
        "Пример: HAIPLANE_HUB_TOKENS=abc\n",
    ),
    "README: токен в ячейке таблицы": (
        "README.md",
        f"| `HAIPLANE_HUB_TOKENS` | `{PROD_NAME}.env` | `abc` | действует |\n",
    ),
    "CAPTURE: открытый токен": (
        "CAPTURE.txt",
        "\n########## x\nHAIPLANE_HUB_TOKENS=abc\n",
    ),
    "CAPTURE: ключ в Environment": (
        "CAPTURE.txt",
        "Environment='HAIPLANE_HUB_PORT=ghp_aaaa'\n",
    ),
    "EnvironmentFile: непрозрачная строка-имя в чужом файле": (
        f"etc/{PROD_NAME}/{PROD_NAME}.env",
        OPAQUE_NAME + "\n",
    ),
    "EnvironmentFile runner: другое имя последней строкой": (
        RUNNER_ENV,
        "HAIPLANE_REVIEW_RUNNER_SPOOL_DIR=/var/lib/x\n" + OPAQUE_NAME + "\n",
    ),
    "EnvironmentFile runner: ARGV не последней строкой": (
        RUNNER_ENV,
        "HAIPLANE_REVIEW_RUNNER_ARGV\nHAIPLANE_REVIEW_RUNNER_SPOOL_DIR=/var/lib/x\n",
    ),
    "tmpfiles: запись f с содержимым": (
        "etc/tmpfiles.d/x.conf",
        "f /etc/x 0600 root root - hunter2\n",
    ),
    "ключ в имени файла": (
        "etc/systemd/system/ghp_abcdef.service",
        "[Service]\nType=simple\n",
    ),
    "файл вне известных видов": ("etc/other/notes.txt", "hello\n"),
}


def test_the_snapshot_shows_only_allowed_values(tmp_path: Path) -> None:
    assert SNAPSHOT_DIR.is_dir(), "нет каталога deploy/systemd/prod"
    assert snapshot_violations(SNAPSHOT_DIR) == []

    # Ключи белого списка: у каждой именованной переменной снимка есть проверка типа.
    clean = tmp_path / "clean"
    (clean / "etc/systemd/system/x.service.d").mkdir(parents=True)
    (clean / "etc/systemd/system/x.service.d/a.conf").write_text(
        "[Service]\nEnvironment=HAIPLANE_CSRF_MODE=require\n", encoding="utf-8"
    )
    assert snapshot_violations(clean) == [], "эталон без нарушений должен проходить"

    missed = []
    for index, (title, (rel, content)) in enumerate(NEGATIVE_EXAMPLES.items()):
        root = tmp_path / f"neg{index}"
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
        if not snapshot_violations(root):
            missed.append(title)
    assert not missed, f"отрицательные примеры не пойманы: {missed}"


# --- AC-2 ---------------------------------------------------------------------
def test_the_snapshot_holds_every_prod_source() -> None:
    raw = (SNAPSHOT_DIR / "CAPTURE.txt").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CAPTURE_SHA256
    capture = raw.decode("utf-8")
    readme = (SNAPSHOT_DIR / "README.md").read_text(encoding="utf-8")

    sections = source_sections(capture)
    # 5 юнитов, 3 таймера, 11 drop-in-ов, tmpfiles, 2 EnvironmentFile
    assert len(sections) == 22, sorted(sections)

    on_disk = {
        p.relative_to(REPO_ROOT).as_posix()
        for p in SNAPSHOT_DIR.rglob("*")
        if p.is_file()
        and p.relative_to(SNAPSHOT_DIR).as_posix() not in NON_SOURCE_FILES
    }
    from_capture = {host_to_repo(p) for p in sections}
    rows = readme_rows(readme)
    from_readme = {r[0] for r in rows}
    assert len(from_readme) == len(rows), "в таблице README повторяется путь"
    assert on_disk == from_capture == from_readme

    # содержимое каждого файла побайтно равно разделу
    for host_path, body in sections.items():
        assert (REPO_ROOT / host_to_repo(host_path)).read_bytes() == body.encode(
            "utf-8"
        ), host_path

    # путь на хосте в README = путь снимка без префикса
    for repo_path, host_path, _state in rows:
        assert host_path == "/" + repo_path[len(SNAPSHOT_PREFIX) :], repo_path

    # порядок drop-in-ов = DropInPaths в CAPTURE.txt = порядок строк таблицы README
    blocks = unit_blocks(capture)
    hub_drop_ins = {
        blocks[n]["DropInPaths"]
        for n in ("haiplane-hub.service", f"{PROD_NAME}.service")
    }
    assert len(hub_drop_ins) == 1
    drop_in_order = next(iter(hub_drop_ins)).split()
    assert len(drop_in_order) == 11
    assert drop_in_order == sorted(drop_in_order), "у systemd drop-in-ы идут по имени"
    assert [r[1] for r in rows if DROP_IN_DIR in r[1]] == drop_in_order
    assert [p for p in sections if DROP_IN_DIR in p] == drop_in_order

    # статус юнита в таблице README = UnitFileState/ActiveState из CAPTURE.txt
    state_by_fragment: dict[str, set[str]] = {}
    for props in blocks.values():
        if "FragmentPath" in props:
            state_by_fragment.setdefault(props["FragmentPath"], set()).add(
                f"{props['UnitFileState']}/{props['ActiveState']}"
            )
    for _repo_path, host_path, state in rows:
        if host_path in state_by_fragment:
            assert {state.strip("` ")} == state_by_fragment[host_path], host_path
        else:
            assert state.strip(), host_path

    # secrets.env: файла нет, перечень имён в README = раздел CAPTURE.txt
    assert not list(SNAPSHOT_DIR.rglob("secrets.env"))
    in_capture = secrets_names(capture)
    assert len(in_capture) == 2 and in_capture[0] == in_capture[1] and in_capture[0]
    assert readme_secret_names(readme) == in_capture[0]


def test_the_readme_variable_table_matches_the_sources() -> None:
    capture = (SNAPSHOT_DIR / "CAPTURE.txt").read_text(encoding="utf-8")
    readme = (SNAPSHOT_DIR / "README.md").read_text(encoding="utf-8")
    sections = source_sections(capture)
    table = re.findall(
        r"^\| `([A-Z][A-Z0-9_]*)` \| `([^`]+)` \| ([^|]+?) \| ([^|]+?) \|$",
        readme,
        flags=re.M,
    )
    assert table, "в README нет таблицы переменных"
    for name, source, value, _effective in table:
        host_path = next(
            (p for p in sections if p.endswith("/" + source) or p == source), None
        )
        assert host_path is not None, f"{name}: источник {source} не найден"
        body = sections[host_path]
        pattern = re.compile(
            r"^(?:Environment=)?'?" + re.escape(name) + r"=(.*?)'?$", flags=re.M
        )
        values = pattern.findall(body)
        assert len(values) == 1, f"{name} в {source}: {values}"
        assert values[0] == value.strip("` "), (
            f"{name} в {source}: README {value!r}, файл {values[0]!r}"
        )
    # у каждой переменной с несколькими источниками действует ровно один
    effective: dict[str, int] = {}
    for name, _source, _value, how in table:
        if how.strip().startswith("действует"):
            effective[name] = effective.get(name, 0) + 1
    assert all(count == 1 for count in effective.values()), effective
    names = {t[0] for t in table}
    assert names == set(effective), "у переменной нет действующего источника: " + str(
        names - set(effective)
    )


def test_the_parser_reads_the_section_boundary_rule() -> None:
    text = "\n##########x\n--- файл /a\nL1\n\n--- EnvironmentFile /b (ignore_errors=no)\nK=1\n##########y\nignored\n"
    assert parse_sections(text) == [
        ("file", "/a", "L1\n\n"),
        ("env", "/b", "K=1\n"),
    ]
    timer = "--- файл /t\n[Timer]\nOnCalendar=x\n\nTimersCalendar=x\n--- PATH\n"
    assert parse_sections(timer) == [("file", "/t", "[Timer]\nOnCalendar=x\n\n")]
    tmp = "--- файл /c\nd /x 2770 a b -\n/x: mode=0o2770 uid=998 gid=1001\n\n##########z\n"
    assert parse_sections(tmp) == [("file", "/c", "d /x 2770 a b -\n")]
    # слово TimersCalendar= или stat-строка внутри содержимого - только в начале строки
    assert parse_sections("--- файл /a\nx TimersCalendar=1\n") == [
        ("file", "/a", "x TimersCalendar=1\n")
    ]
    assert (
        host_file_text("/etc/systemd/system/a.service", "[Unit]\nX=1\n\n")
        == "[Unit]\nX=1\n"
    )
    assert (
        host_file_text("/etc/tmpfiles.d/a.conf", "d /x - - - -\n") == "d /x - - - -\n"
    )


@pytest.mark.parametrize("line", ["task-1849", "disk-usage", "risk-class"])
def test_key_shape_is_a_substring_rule(line: str) -> None:
    # "sk-" ловится внутри слов: README обязан обходить такие слова (проверка правила, не ошибка)
    assert generic_line_problems(line) == ["вид ключа"]
