"""Hub-dispatched cross-model reviews (#757): deploy_doc (split of the former single file, #1670)."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
import textwrap
import uuid
from pathlib import Path

import aiosqlite
from httpx import AsyncClient

from hub import config
from hub.integrations import local_reviewer
from hub.services.review_dispatch import (
    _delivery_block,
)
from tests.review_dispatch_support import (
    _DEPLOY_DOC,
    _ENV_EXAMPLE,
    _DispatchRecorder,
    _local_principal,
    _node,
    _own_host_budget,
    _stub_reviewer,
    _submitted,
    _wire,
)


def _documented_hub_deadline() -> int:
    """Хабский срок прогона, КАК ЕГО НАЗЫВАЕТ документ выката.

    Нужен там, где суждение стража зависит от ``LOCAL_REVIEW_TIMEOUT_SEC``:
    с 10.09.2026 контейнерный ``--timeout`` больше хабского отвергается, и
    тест, оставивший этот срок на волю окружения, был бы зелёным или красным
    по чужой переменной, а не по содержанию документа. Значение берётся ИЗ
    ФАЙЛА — переписанное сюда числом, оно проверяло бы тест, а не документ.
    """
    # Число берётся регулярным выражением, а не хвостом строки: то же имя со
    # значением стоит и внутри таблицы отказов, где за ним идёт разметка.
    named = re.compile(
        re.escape(config.brand.ENV_PREFIX + "LOCAL_REVIEW_TIMEOUT_SEC") + r"=(\d+)"
    )
    values = [
        found.group(1)
        for path in (_ENV_EXAMPLE, _DEPLOY_DOC)
        for line in path.read_text().splitlines()
        if (found := named.search(line))
    ]
    assert values, (
        f"в примере окружения и документе не назван {named.pattern} — "
        "проверка срока была бы привязана к переменной окружения, а не к "
        "документу"
    )
    assert len(set(values)) == 1, (
        f"документ называет хабский срок по-разному: {values}. Оператор "
        "скопирует одно из двух, и какое — неизвестно"
    )
    return int(values[0])


def _doc_limits_probe() -> str:
    """Фрагмент пробы лимитов ИЗ ДОКУМЕНТА — от шага 3 до конца блока.

    Берётся текстом из файла, а не переписывается сюда: переписанная проба
    проверяла бы тест, а не документ.
    """
    blocks = [
        b
        for b in re.findall(r"```bash\n(.*?)```", _DEPLOY_DOC.read_text(), re.S)
        if "memory.max" in b
    ]
    assert len(blocks) == 1, (
        f"в {_DEPLOY_DOC.name} ожидался ровно один bash-блок с пробой "
        f"memory.max, найдено {len(blocks)}"
    )
    lines = blocks[0].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# 3."))
    return "\n".join(lines[start:])


def test_the_doc_limits_probe_tells_failure_from_success(tmp_path) -> None:
    """Проба лимитов ОТЛИЧАЕТ отказ движка от успеха, а не читает молчание.

    Прежняя редакция запускала `haiplane-review:latest` — литерал, который
    абзац «Остальные расхождения» того же документа называет разошедшимся с
    настоящим именем образа (`localhost/haiplane-reviewer:1`). Образа с таким
    именем на хосте нет, podman отказывает и НИЧЕГО не печатает, а критерий
    был написан отрицанием: «не должно быть 'max'». Замерено::

        rc=125, stdout=[] -> слова 'max' нет -> оператор читает УСПЕХ

    То есть проба доказывала лимиты собственным отказом. Здесь она
    ИСПОЛНЯЕТСЯ с подставным движком, и проверяется ровно то, что чинит
    находка: четыре исхода — лимиты есть, лимитов нет, ответа нет, проба не
    запустилась — обязаны звучать ПО-РАЗНОМУ.
    """
    probe = _doc_limits_probe()
    # Судятся ИСПОЛНЯЕМЫЕ строки: комментарий рядом называет старый литерал
    # именно затем, чтобы его сюда не вернули, и запретом на слово это не
    # проверить.
    runnable = "\n".join(
        line for line in probe.splitlines() if not line.lstrip().startswith("#")
    )
    assert "haiplane-review:latest" not in runnable, (
        "проба запускает образ, который этот же документ называет выдуманным "
        "литералом прежней редакции"
    )
    assert "/etc/haiplane-review/image" in runnable, (
        "образ обязан читаться из того же файла, что читает враппер: литерал "
        "в пробе уже расходился с настоящим именем образа"
    )

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "sudo").write_text('#!/bin/sh\n[ "$1" = "-u" ] && shift 2\nexec "$@"\n')
    imagefile = tmp_path / "image"
    imagefile.write_text("localhost/haiplane-reviewer:1\n")
    argv_log = tmp_path / "argv.log"

    engines = {
        # Образа нет — ровно исход прежней редакции: rc 125, stdout пуст.
        "отказ движка": '#!/bin/sh\necho "Error: no such image" >&2\nexit 125\n',
        # Движок отработал, но лимитов нет.
        "лимитов нет": "#!/bin/sh\necho max\n",
        # Движок отработал и не напечатал ничего — то же молчание, что у
        # отказа, но с нулевым кодом.
        "пустой вывод": "#!/bin/sh\nexit 0\n",
        # Лимиты применены.
        "лимиты есть": "#!/bin/sh\necho 1572864000\n",
    }
    verdicts: dict[str, str] = {}
    for label, body in engines.items():
        podman = bindir / "podman"
        podman.write_text(
            body.replace(
                "#!/bin/sh\n",
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$ARGV_LOG"\n',
                1,
            )
        )
        for path in (bindir / "sudo", podman):
            path.chmod(0o755)
        done = subprocess.run(
            [
                "/bin/sh",
                "-c",
                probe.replace("/etc/haiplane-review/image", str(imagefile)),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "ARGV_LOG": str(argv_log),
            },
        )
        printed = done.stdout.strip()
        assert printed, (
            f"на исходе «{label}» проба не сказала НИЧЕГО: оператору нечего "
            f"прочитать (stderr: {done.stderr!r})"
        )
        # Приговор — то, что стоит ДО двоеточия. Сравнивать целые строки
        # значило бы различать исходы по подставленному числу: мутация,
        # печатающая на отказе движка «лимиты применены: memory.max=» с
        # пустым значением, отличалась бы от успеха одним лишь числом и
        # ВЫЖИЛА (замерено в мутационной серии, M2).
        verdicts[label] = printed.split(":", 1)[0].strip()

    assert len(set(verdicts.values())) == len(verdicts), (
        "исходы пробы неразличимы по приговору: оператор не сможет отличить "
        f"отказ движка от применённых лимитов, читая одно и то же — {verdicts}"
    )

    # И образ — тот, что лежит в файле, а не зашитый в пробу литерал.
    seen = argv_log.read_text()
    assert "localhost/haiplane-reviewer:1" in seen, (
        f"проба запустила не тот образ, что назван в файле: {seen!r}"
    )


def _doc_table(heading: str) -> list[list[str]]:
    """Строки таблицы под НАЗВАННЫМ заголовком документа, ячейками.

    Заголовок обязателен: таблиц в документе несколько, и брать «все строки,
    начинающиеся с | `» значило бы судить таблицу принятых форм правилами
    таблицы отказов. Пустая выборка — провал теста, а не зелёный прогон: она
    была бы зелёной при любом содержании документа.
    """
    lines = _DEPLOY_DOC.read_text().splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == heading.strip()), -1
    )
    assert start >= 0, f"в {_DEPLOY_DOC.name} нет заголовка «{heading}»"
    rows: list[list[str]] = []
    for line in lines[start:]:
        if line.startswith("## ") or line.startswith("### "):
            if rows:
                break
            continue
        if line.startswith("| `"):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    assert rows, f"под «{heading}» не нашлось таблицы — проверка была бы пустой"
    return rows


def _backticked(cell: str) -> str:
    assert cell.count("`") >= 2, f"в ячейке «{cell}» нет строки в обратных кавычках"
    return cell.split("`")[1]


def test_the_doc_names_the_closed_set_the_guard_accepts(monkeypatch) -> None:
    """AC-5/AC-6: набор форм в документе — это набор форм В КОДЕ, сверено машиной.

    Расхождение документа с кодом на этой задаче случалось ПЯТЬ раз и глазами
    не поймалось ни разу: документ рекомендовал `systemd-run --uid=`, который
    на проде не стартует; советовал `--timeout` «не меньше» хабского, тогда
    как страж отвергает «больше»; обещал, что незнакомое пропускается, тогда
    как незнакомый флаг давал отказ. Поэтому сверяется не глазами: имена форм,
    строки целиком и ОБА списка флагов берутся из таблицы документа и
    сравниваются с ``SANDBOX_SHAPES``, а каждая строка прогоняется через
    самого стража.
    """
    rows = _doc_table("### Что хаб принимает")
    shapes = {shape.name: shape for shape in local_reviewer.SANDBOX_SHAPES}
    documented = {_backticked(row[0]) for row in rows}
    assert documented == set(shapes), (
        f"документ называет формы {sorted(documented)}, а код принимает "
        f"{sorted(shapes)}. Форма, которой нет в одном из двух мест, — это "
        "либо обещание без кода, либо код без обещания"
    )

    for row in rows:
        shape = shapes[_backticked(row[0])]
        example = _backticked(row[1])
        assert example == shape.example, (
            f"документ печатает для формы «{shape.name}» строку «{example}», а "
            f"код держит «{shape.example}». Оператор скопирует первую"
        )
        required = {
            flag.strip()
            for cell in row[2].split(",")
            for flag in cell.replace("`", " ").split("/")
            if flag.strip().startswith("-")
        }
        assert required == {flag for group in shape.required for flag in group}, (
            f"обязательные флаги формы «{shape.name}»: документ называет "
            f"{sorted(required)}, код требует "
            f"{sorted(flag for group in shape.required for flag in group)}"
        )
        optional = {
            flag.strip()
            for flag in row[3].replace("`", " ").split(",")
            if flag.strip().startswith("-")
        }
        in_code = (shape.valueless | shape.valued) - {
            flag for group in shape.required for flag in group
        }
        assert optional == in_code, (
            f"необязательные флаги формы «{shape.name}»: документ называет "
            f"{sorted(optional)}, код принимает {sorted(in_code)}. Флаг, "
            "названный в документе и не принятый кодом, — это отказ на строке, "
            "которую документ рекомендует"
        )

        # И то же самое ИСПОЛНЕНИЕМ: строка из документа прогоняется через
        # стража и обязана подойти именно той форме, которой её назвали.
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", example)
        assert local_reviewer.sandbox_problem() == [], (
            f"документ печатает «{example}» как форму «{shape.name}», а хаб её "
            f"отвергает: {local_reviewer.sandbox_problem()}"
        )
        assert local_reviewer.sandbox_shape() == shape.name


def test_the_doc_table_of_refusals_is_what_the_guard_actually_refuses(
    monkeypatch,
) -> None:
    """AC-4, обратная сторона: строки из таблицы ОТКАЗОВ хаб и вправду отвергает.

    Пара к test_the_doc_recommends_only_sandboxes_the_hub_accepts. Документ,
    обещающий отказ там, где хаб молча пропускает, готовит ровно тот выкат, из
    которого выросла задача. Строки берутся из таблицы, а не переписываются
    сюда.

    В таблице стоят НАСТОЯЩИЕ входы, на которых страж когда-то ошибался, —
    включая те, что прежняя редакция документа обещала ПРИНИМАТЬ (`podman run
    --timeout 1800`). Смена правила не выбросила ни одного из них: они
    отвергаются тем более, и документ обязан говорить об этом ровно то же,
    что делает код.
    """
    refused = [_backticked(row[0]) for row in _doc_table("### Что хаб отвергает")]
    assert len(refused) >= 10, (
        f"таблица отказов слишком коротка ({len(refused)}): накопленные за "
        "девять кругов входы обязаны стоять в документе, а не только в тестах"
    )
    # Суждение о сроке зависело от хабского, и брать его из окружения значило
    # бы судить документ по чужой переменной. Приговор от него больше не
    # зависит, но число в документе и в примере окружения обязано быть одно —
    # это и проверяет _documented_hub_deadline.
    monkeypatch.setattr(config, "LOCAL_REVIEW_TIMEOUT_SEC", _documented_hub_deadline())

    for sandbox in refused:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert reasons, (
            f"документ обещает отказ на «{sandbox}», а хаб пропускает её "
            "молча: обещание в тексте, которого нет в коде, — это тот же "
            "выкат, ради которого писан весь документ"
        )
        assert local_reviewer.sandbox_uid() == "", (
            f"на отвергнутой «{sandbox}» страж назвал пользователя "
            f"«{local_reviewer.sandbox_uid()}»"
        )

    # Классы, за которыми документ обязан следить поимённо: контейнерный
    # запуск (отказ называет недостающий --timeout), отсоединённый запуск и
    # срок ДЛИННЕЕ хабского — все три страж когда-то пропускал молча.
    assert any("podman" in s and "--timeout" in s for s in refused), (
        "контейнерный запуск со сроком жизни в таблице не назван, а прежняя "
        "редакция документа его РЕКОМЕНДОВАЛА"
    )
    assert any("-d" in s or "--detach" in s for s in refused), (
        "отсоединённый запуск в таблице отказов не назван, а он проходил "
        "стража молча (воспроизведено на 5b7b813)"
    )
    assert any("3600" in s for s in refused), (
        "срок контейнера ДЛИННЕЕ хабского в таблице отказов не назван, а "
        "документ до 10.09.2026 такой срок прямо СОВЕТОВАЛ («не меньше»)"
    )
    assert any("-p80:80" in s for s in refused), (
        "строка с неизвестным флагом при ЖИВОМ сроке в таблице не названа, а "
        "именно на ней текст документа расходился с кодом"
    )


def test_the_doc_recommends_only_sandboxes_the_hub_accepts(monkeypatch) -> None:
    """AC-4: строки песочницы ИЗ ДОКУМЕНТА прогоняются через самих стражей.

    Документ, рекомендующий то, что хаб сам же отклонит, хуже отсутствующего.
    Строки берутся из файлов, а не переписываются сюда: переписанная строка
    проверяла бы тест, а не документ.
    """
    key = config.brand.ENV_PREFIX + "LOCAL_REVIEW_SANDBOX"
    # Значение — всё после первого '<ИМЯ>=' в строке.
    recommended = [
        line.split(key + "=", 1)[1]
        for path in (_DEPLOY_DOC, _ENV_EXAMPLE)
        for line in path.read_text().splitlines()
        if key + "=" in line
    ]
    assert len(recommended) >= 2, (
        "в документе и примере окружения не нашлось рекомендованных строк "
        f"{key}= — проверка была бы пустой и зелёной при любом их содержании"
    )

    for sandbox in recommended:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        assert local_reviewer.sandbox_problem() == [], (
            f"документ рекомендует «{sandbox}», а хаб её отвергает: "
            f"{local_reviewer.sandbox_problem()}"
        )
        assert local_reviewer.sandbox_uid(), (
            f"в рекомендованной строке «{sandbox}» страж не видит "
            "пользователя — значит проверка его членства в группе каталога "
            "прогонов на этой конфигурации молча не сработает вовсе, ровно "
            "как было до #1208"
        )


def test_the_doc_wrapper_carries_the_argv_the_hub_appends(
    monkeypatch, tmp_path
) -> None:
    """Скелет враппера ИЗ ДОКУМЕНТА доносит до движка argv, дописанный хабом.

    Хаб запускает ``shlex.split(SANDBOX) + shlex.split(CMD)``. Враппер без
    ``"$@"`` этот хвост молча выбрасывает: измерено на прежней редакции
    скелета — ``haiplane-review-run cursor-agent --print`` доходило до podman
    строкой БЕЗ ``cursor-agent``, код возврата 0, ни ошибки, ни следа.
    Запускалось бы то, что зашито в образ, а не то, что стоит в настройке —
    тот же класс отказа, который чинила задача: не ломает, а тихо подменяет
    (найдено машинным ревью 09.09.2026, находка 8d363dc407782056).

    Скелет берётся ИЗ ФАЙЛА и ИСПОЛНЯЕТСЯ, а не читается глазами: переписанный
    в тест, он проверял бы тест, а прочитанный — ничего.
    """
    # 1. Хаб действительно дописывает CMD к SANDBOX — это наблюдение, а не
    #    посылка: на ней держится всё остальное в этом тесте.
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "/usr/bin/sudo -n -u r /wrap")
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "cursor-agent --print")
    assert local_reviewer.argv() == [
        "/usr/bin/sudo",
        "-n",
        "-u",
        "r",
        "/wrap",
        "cursor-agent",
        "--print",
    ], "хаб склеивает песочницу и CMD — если это не так, весь тест ниже мимо"

    # 2. Скелет враппера из документа, с подменённым движком на заглушку,
    #    печатающую свой argv.
    blocks = [
        b
        for b in re.findall(r"```sh\n(.*?)```", _DEPLOY_DOC.read_text(), re.S)
        if "podman run" in b
    ]
    assert len(blocks) == 1, (
        f"в {_DEPLOY_DOC.name} ожидался ровно один sh-скелет враппера с "
        f"podman run, найдено {len(blocks)}"
    )
    script = textwrap.dedent(blocks[0])
    engine = next(
        tok
        for tok in shlex.split(script.replace("\\\n", " "))
        if tok.rsplit("/", 1)[-1] == "podman"
    )
    stub = tmp_path / "podman"
    stub.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
    stub.chmod(0o755)
    wrapper = tmp_path / "haiplane-review-run"
    wrapper.write_text(script.replace(engine, str(stub)))
    wrapper.chmod(0o755)

    cmd = ["cursor-agent", "--print", "--model", "grok-4.6"]
    done = subprocess.run(
        [str(wrapper), *cmd],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "HAIPLANE_REVIEW_CONF": str(_wrapper_conf(tmp_path, deadline=None)),
        },
    )
    assert done.returncode == 0, f"скелет враппера не запустился: {done.stderr}"
    seen = done.stdout.split("\n")
    assert seen[-1 - len(cmd) : -1] == cmd, (
        "скелет враппера из документа НЕ донёс до движка argv, который хаб к "
        f"нему дописал: движок получил {seen[:-1]}, а хвостом обязан был "
        f'стоять {cmd}. Молча: код возврата 0. Добавьте "$@" последним '
        "аргументом podman run"
    )


def test_the_doc_probe_runs_the_same_command_the_hub_will_run(monkeypatch) -> None:
    """Проба песочницы в документе гоняет тот же argv, что и живой запуск.

    Проба, которая запускает враппер голым, проходит и на скелете, молча
    выбрасывающем argv, — то есть доказывает не то, что проверяет.
    """
    key = config.brand.ENV_PREFIX + "LOCAL_REVIEW_SANDBOX"
    text = _DEPLOY_DOC.read_text()
    recommended = [
        line.split(key + "=", 1)[1] for line in text.splitlines() if key + "=" in line
    ]
    assert recommended, f"в {_DEPLOY_DOC.name} нет рекомендованной строки {key}="
    wrapper = shlex.split(recommended[0])[-1]

    probes = [
        line
        for block in re.findall(r"```bash\n(.*?)```", text, re.S)
        for line in block.splitlines()
        if wrapper in line
    ]
    assert probes, (
        f"в {_DEPLOY_DOC.name} нет пробы, запускающей враппер {wrapper} — "
        "проверка была бы пустой и зелёной при любом её содержании"
    )
    for line in probes:
        assert "$CMD" in line, (
            f"проба «{line.strip()}» запускает враппер БЕЗ дописанного argv "
            "CLI, а хаб запускает песочницу вместе с ним "
            f"({key[:-7]}CMD). Такая проба пройдёт там, где живой запуск "
            "пойдёт другой командой"
        )


# Флаги ``podman run``, которые встречаются в СКЕЛЕТЕ ВРАППЕРА из документа.
# Список живёт здесь, а не в хабе: с 10.09.2026 хаб строку podman не разбирает
# вовсе — контейнерного запуска нет в закрытом наборе форм песочницы (#1208),
# а внутрь враппера он не заглядывает по определению. Тесты документа читают
# скелет сами, и незнакомый флаг для них — ПРОВАЛ разбора, о котором тест
# кричит, а не угадывает: иначе проверка «--timeout доехал» была бы зелёной на
# строке, которую тест не понял.
_WRAPPER_VALUE_FLAGS = frozenset(
    {
        "--cpus",
        "--env-file",
        "-m",
        "--memory",
        "--name",
        "--net",
        "--network",
        "--pids-limit",
        "--timeout",
        "--userns",
        "-v",
        "--volume",
    }
)
_WRAPPER_VALUELESS_FLAGS = frozenset(
    {"--init", "-i", "-it", "--read-only", "--replace", "--rm", "-t"}
)


def _wrapper_run_flags(parts: list[str]) -> list[tuple[str, str | None]]:
    """Флаги самого ``podman run`` из скелета враппера — ДО образа.

    Всё, что стоит после образа, — команда внутри контейнера, и её флаги к
    запуску отношения не имеют.
    """
    assert "run" in parts, f"в строке запуска нет подкоманды run: {parts}"
    i = parts.index("run")
    assert parts[i - 1].rsplit("/", 1)[-1] in ("podman", "docker"), (
        f"«run» стоит не за движком: {parts}"
    )
    flags: list[tuple[str, str | None]] = []
    k = i + 1
    while k < len(parts) and parts[k].startswith("-"):
        name, sep, value = parts[k].partition("=")
        if sep:
            flags.append((name, value))
            k += 1
        elif name in _WRAPPER_VALUE_FLAGS:
            flags.append((name, parts[k + 1] if k + 1 < len(parts) else None))
            k += 2
        else:
            assert name in _WRAPPER_VALUELESS_FLAGS, (
                f"скелет враппера содержит флаг «{name}», о котором этот тест "
                f"не знает, берёт ли тот значение отдельным токеном: {parts}. "
                "Разбор недостоверен, и судить по нему нельзя"
            )
            flags.append((name, None))
            k += 1
    return flags


def _wrapper_flag(flags: list[tuple[str, str | None]], flag: str) -> str | None:
    """ДЕЙСТВУЮЩЕЕ значение флага — последнее вхождение, как берёт его pflag."""
    values = [value for name, value in flags if name == flag]
    return values[-1] if values else None


def _wrapper_conf(tmp_path, *, deadline: int | None) -> Path:
    """Каталог ``$CONF`` враппера: то, что оператор кладёт в /etc по шагу 3a.

    Скелет читает оттуда образ и срок — сам он их не зашивает, иначе срок ни
    за какой настройкой не следовал бы (находка 10.09.2026). Файл ``timeout``
    при ``deadline is None`` НЕ создаётся: это вход «оператор файла не писал»,
    на котором действует умолчание скелета.
    """
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    (conf / "image").write_text("localhost/haiplane-reviewer:1\n")
    (conf / "model.env").write_text("KEY=x\n")
    if deadline is None:
        (conf / "timeout").unlink(missing_ok=True)
    else:
        (conf / "timeout").write_text(f"{deadline}\n")
    return conf


def _doc_wrapper_argv(tmp_path, *, deadline: int | None) -> list[str]:
    """ВСЕ вызовы движка, которые сделал скелет враппера из документа.

    Скелет не читается глазами и не разбирается регулярками: он ИСПОЛНЯЕТСЯ с
    подменённым на заглушку движком, и наружу отдаётся то, что заглушка
    получила — каждый вызов отдельной строкой, в порядке вызова. Разница не
    косметическая — в скелете стоят переменные (``--timeout="$TIMEOUT"``,
    ``"$IMAGE"``), и текст «--timeout "$TIMEOUT"» выглядит убедительно ровно
    так же при пустом TIMEOUT, при опечатке в имени переменной и при её потере
    под ``set -u``. Проверять текст значило бы проверять намерение, а не
    команду.

    ``deadline`` — что лежит в ``$CONF/timeout``; ``None`` значит файла нет, и
    тогда действует умолчание самого скелета. Каталог настроек подставляется
    через ``HAIPLANE_REVIEW_CONF``: без него скелет читал бы боевой
    ``/etc/haiplane-review``, которого на машине проверки нет, а с зашитым в
    скелет литералом срок вообще не следовал бы ни за чем (находка 10.09.2026,
    находки 2 и 3 одного и того же числа).

    Возвращаются ВСЕ вызовы, а не только ``run``: то, что скелет делает ДО
    запуска, — часть рецепта, и именно там жил ``podman rm -af``, сносивший
    чужие контейнеры.
    """
    conf = _wrapper_conf(tmp_path, deadline=deadline)
    blocks = [
        b
        for b in re.findall(r"```sh\n(.*?)```", _DEPLOY_DOC.read_text(), re.S)
        if "podman run" in b
    ]
    assert len(blocks) == 1, (
        f"в {_DEPLOY_DOC.name} ожидался ровно один sh-скелет враппера с "
        f"podman run, найдено {len(blocks)}"
    )
    script = textwrap.dedent(blocks[0])
    engine = next(
        tok
        for tok in shlex.split(script.replace("\\\n", " "))
        if tok.rsplit("/", 1)[-1] == "podman"
    )
    # Свой файл на КАЖДЫЙ прогон: заглушка дописывает, и общий файл склеил бы
    # вызовы соседнего запуска скелета в том же tmp_path.
    log = tmp_path / f"argv-{uuid.uuid4().hex}.log"
    stub = tmp_path / "podman"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import os, shlex, sys\n"
        "open(os.environ['ARGV_LOG'], 'a').write(shlex.join(sys.argv[1:]) + '\\n')\n"
    )
    stub.chmod(0o755)
    wrapper = tmp_path / "haiplane-review-run"
    wrapper.write_text(script.replace(engine, str(stub)))
    wrapper.chmod(0o755)

    done = subprocess.run(
        [str(wrapper), "cursor-agent", "--print"],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "ARGV_LOG": str(log),
            "HAIPLANE_REVIEW_CONF": str(conf),
        },
    )
    assert done.returncode == 0, f"скелет враппера не запустился: {done.stderr}"
    seen = [line for line in log.read_text().splitlines() if line]
    log.unlink()
    return [shlex.join([engine, *shlex.split(line)]) for line in seen]


def _doc_wrapper_launch(tmp_path, *, deadline: int | None = None) -> str:
    """Единственная строка ``<engine> run`` скелета — то, что судит страж."""
    calls = _doc_wrapper_argv(tmp_path, deadline=deadline)
    runs = [c for c in calls if shlex.split(c)[1:2] == ["run"]]
    assert len(runs) == 1, (
        f"скелет враппера обязан один раз позвать «podman run»; движок получил {calls}"
    )
    return runs[0]


def test_the_doc_wrapper_skeleton_names_its_own_deadline(monkeypatch, tmp_path) -> None:
    """Срок жизни контейнера в скелете враппера держится тестом, а не текстом.

    Рекомендованная песочница — ``sudo … /usr/local/bin/haiplane-review-run``,
    и стражам хаба она непрозрачна: podman лежит ВНУТРИ скрипта. Значит на
    рекомендованном рецепте единственное место, где срок жизни вообще
    существует, — это скелет враппера в документе. Замер: вычеркнуть из него
    ``--timeout 1800`` — и весь набор оставался зелёным, включая AC-1..AC-4,
    тест argv и тест пробы (09.09.2026, находка машинного ревью
    b3a35600e66e971d). То есть флаг, ради которого писан страж, на
    рекомендованном пути не держался ничем.

    Проверяется не глазами и не подстрокой: строка запуска берётся ИЗ ФАЙЛА и
    ИСПОЛНЯЕТСЯ (переменные скелета подставляются оболочкой, а не читателем).

    ПОВОРОТ 10.09.2026. Прежде эту строку прогоняли через самого стража хаба.
    Теперь так нельзя, и не потому, что стало хуже: контейнерный запуск в
    закрытый набор форм песочницы не входит, и страж отверг бы ЛЮБУЮ строку
    podman — в том числе верную. Судить скелет ему больше нечем и незачем: он
    и на рекомендованном рецепте его не видел. Поэтому срок берётся из
    исполненного скелета и сравнивается с ХАБСКИМ СРОКОМ ИЗ ДОКУМЕНТА — с тем
    самым числом, которое оператор скопирует к себе.
    """
    launch = _doc_wrapper_launch(tmp_path)
    # Хвост ``cursor-agent --print`` — команда ВНУТРИ контейнера; флаги
    # запуска кончаются на образе, и до хвоста разбор не доходит.
    flags = _wrapper_run_flags(shlex.split(launch))
    lifetime = _wrapper_flag(flags, "--timeout")
    assert lifetime and lifetime.isdigit() and int(lifetime) > 0, (
        f"скелет враппера из документа запускает «{launch}» без собственного "
        "срока жизни. Контейнер переживёт снятие прогона — измерено "
        "08.09.2026: kill -KILL по группе, через 7 с контейнер «Up», — а хаб "
        "напишет в ленту, что снял"
    )
    # Срок контейнера не может быть БОЛЬШЕ хабского: он истёк бы уже после
    # того, как хаб закрыл прогон. Хабский берётся из документа, а не из
    # окружения, иначе скелет судился бы чужой переменной.
    hub_deadline = _documented_hub_deadline()
    assert int(lifetime) <= hub_deadline, (
        f"скелет даёт контейнеру {lifetime} с при хабских {hub_deadline}: "
        "контейнер переживёт снятие прогона ровно так же, как без --timeout "
        "вовсе, только тише"
    )


def test_the_doc_wrapper_deadline_follows_the_conf_file(monkeypatch, tmp_path) -> None:
    """Срок контейнера СЛЕДУЕТ за настройкой, а не зашит в скелет числом.

    Находка 10.09.2026 (ревьюер Codex, воспроизведено на ef2198fc): оператор
    ставит ``HAIPLANE_LOCAL_REVIEW_TIMEOUT_SEC=600`` и копирует скелет как
    есть — а в скелете стояло ``TIMEOUT=1800`` литералом, ни с чем не
    связанным. Контейнер переживал прогон втрое, и страж этого не видел вовсе:
    на рекомендованном рецепте ему виден только ``sudo …
    /haiplane-review-run``, podman лежит внутри скрипта.

    Проверяется исполнением: скелет запускается с подставленным каталогом
    настроек, и наружу берётся то число, которое ПОЛУЧИЛ движок. Замер на
    прежней редакции: файл ``timeout`` с любым содержимым не менял ничего —
    движку уходило 1800.
    """
    hub_deadline = _documented_hub_deadline()

    # 1. Умолчание скелета (файла нет) — это ХАБСКОЕ умолчание, а не «просто
    #    число»: разойдись они, рецепт по умолчанию был бы уже сломан.
    launch = _doc_wrapper_launch(tmp_path, deadline=None)
    flags = _wrapper_run_flags(shlex.split(launch))
    assert _wrapper_flag(flags, "--timeout") == str(hub_deadline), (
        f"без файла настроек скелет ставит контейнеру срок из «{launch}», а "
        f"хабское умолчание — {hub_deadline}. Умолчания обязаны совпадать: "
        "иначе рецепт ломается ещё до того, как оператор что-либо тронул"
    )

    # 2. Файл настроек ДЕЙСТВУЕТ: оператор укоротил хабский срок, вписал то же
    #    число в файл — и контейнер получил именно его. Это и есть то, чего в
    #    прежней редакции не было: связь между двумя местами.
    shortened = 600
    assert shortened != hub_deadline, "проверка беспредметна на равных числах"
    launch = _doc_wrapper_launch(tmp_path, deadline=shortened)
    flags = _wrapper_run_flags(shlex.split(launch))
    assert _wrapper_flag(flags, "--timeout") == str(shortened), (
        f"скелет не взял срок из $CONF/timeout: движку ушло «{launch}». "
        "Значит число в скелете зашито, и согласовать его с настройкой хаба "
        "оператору нечем"
    )
    # ПОВОРОТ 10.09.2026: прежде здесь стояла третья проверка — что строку
    # запуска враппера принимает сам страж хаба. Она потеряла предмет:
    # контейнерный запуск в закрытый набор форм песочницы не входит, и хаб
    # эту строку не увидит вовсе — ему видна только форма sudo, а podman
    # лежит внутри скрипта. Согласие двух чисел от этого не ослабло: оно и
    # есть то, что проверено выше, — и проверено ИСПОЛНЕНИЕМ скелета.
    monkeypatch.setattr(config, "LOCAL_REVIEW_TIMEOUT_SEC", shortened)
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/sudo -n -u haiplane-reviewer /usr/local/bin/haiplane-review-run",
    )
    assert local_reviewer.sandbox_problem() == [], (
        "хаб видит не строку podman, а форму sudo, и она обязана проходить "
        f"при любом сроке: {local_reviewer.sandbox_problem()}"
    )

    # 3. И РАСХОЖДЕНИЕ — это дыра, а не мелочь: не тронув файл, оператор
    #    получит на укороченном хабе контейнер с прежним умолчанием. Страж
    #    этого не увидит НИКОГДА — ни прежде, ни теперь: на рекомендованном
    #    рецепте podman лежит внутри скрипта. Поэтому файл из шага 3a и есть
    #    единственное место, где расхождение закрывается, и здесь измеряется
    #    именно оно.
    stale = _wrapper_flag(
        _wrapper_run_flags(shlex.split(_doc_wrapper_launch(tmp_path))), "--timeout"
    )
    assert stale == str(hub_deadline) and int(stale) > shortened, (
        f"без файла скелет даёт контейнеру {stale} с — и на хабских "
        f"{shortened} это переживший прогон контейнер. Если бы умолчание "
        "скелета следовало за настройкой само, шаг 3a был бы не нужен вовсе"
    )


def test_the_doc_wrapper_cleans_up_only_its_own_container(tmp_path) -> None:
    """Уборка хвоста адресована ОДНОМУ контейнеру, а не всем подряд.

    Находка 10.09.2026 (ревьюер Codex, подтверждена на хосте хаба:
    /usr/local/bin/haiplane-review-run, строка 22). Скелет содержал
    ``podman rm -af`` — снос ВСЕХ контейнеров пользователя-ревьюера.
    Основание было названо тут же в комментарии («два ревьюера разом хосту не
    по карману»), но ``rm -af`` такого ограничения не устанавливает: он не
    сдерживает второй прогон, а УБИВАЕТ первый — и заодно всё постороннее,
    что этот пользователь запустил. Одновременность держит хаб
    (``local_reviewer._HOST_BUDGET``, тест
    ``test_two_local_runs_never_overlap_on_the_host``), а здесь держится то,
    что уборка никого чужого не задевает.

    Судится не текст, а argv, которые движок ФАКТИЧЕСКИ получил.
    """
    calls = _doc_wrapper_argv(tmp_path, deadline=None)
    # ``--all``/``-a`` у любой снимающей подкоманды — это «все контейнеры
    # пользователя», то есть ровно та находка. Перечислены по имени: список
    # узкий и о подкомандах podman, а не догадка про CLI вообще.
    for call in calls:
        argv = shlex.split(call)[1:]
        if not argv or argv[0] not in ("rm", "stop", "kill", "pod"):
            continue
        wholesale = [
            tok
            for tok in argv[1:]
            if tok in ("-a", "--all")
            or (tok.startswith("-") and not tok.startswith("--") and "a" in tok[1:])
        ]
        assert not wholesale, (
            f"скелет враппера зовёт «{call}»: {wholesale} значит ВСЕ "
            "контейнеры пользователя-ревьюера, а не хвост своего прошлого "
            "прогона. Так уборка сносит живое чужое ревью, и в карточке это "
            "ляжет отказом прогона с ложной причиной"
        )
    # А адресат уборки обязан БЫТЬ: снести хвост всё-таки надо, и адресуется
    # он именем. Без имени ``--replace`` бессмыслен, а без ``--replace``
    # хвост пережил бы запуск и занял бы имя.
    launch = _doc_wrapper_launch(tmp_path, deadline=None)
    flags = _wrapper_run_flags(shlex.split(launch))
    name = _wrapper_flag(flags, "--name")
    assert isinstance(name, str) and name, (
        f"скелет не даёт контейнеру имени: {launch}. Тогда адресной уборки "
        "нет вовсе, и вернуться к «rm -af» — вопрос одного коммита"
    )
    assert any(flag == "--replace" for flag, _ in flags), (
        f"имя «{name}» есть, а --replace нет: {launch}. Хвост прошлого "
        "прогона займёт имя, и запуск упадёт «name already in use»"
    )


def test_the_doc_wrapper_leaves_the_network_the_hub_hands_the_run(tmp_path) -> None:
    """Скелет враппера НЕ отрезает контейнеру сеть, которой хаб его снабжает.

    Прежняя редакция документа рекомендовала ``--network none`` — четвёртый
    экземпляр того же дефекта, ради которого заведена #1208: документ учил
    настройке, на которой выкат останавливается. Настоящий враппер на хосте
    хаба флага ``--network`` не имеет вовсе (снят 10.09.2026).

    Требование не выдумано и не переписано в тест словами: оно берётся у
    САМОГО ХАБА. ``review_dispatch._delivery_block`` — та функция, что
    собирает промт, — кладёт в него ``curl`` на публичный адрес установки:
    обменять одноразовый код, прочитать постановку, СДАТЬ ОТЧЁТ. Контейнер
    без сети до этого адреса не дойдёт, и остаётся только запасной путь —
    блок в тексте (#1036), который хаб помечает как более слабый. Измерено на
    хосте хаба 10.09.2026 тем же образом: с сетью по умолчанию ``getent
    hosts`` внутри контейнера отвечает про agenthai.ru и api.cursor.com
    (rc 0), с ``--network none`` — rc 2 и ни одной строки.
    """
    hub_base = "https://hub.example"
    delivery = _delivery_block(1208, "CODE-1", hub_base)
    assert hub_base in delivery and "curl" in delivery, (
        "хаб перестал давать прогону сетевой адрес — тогда и требование "
        f"ниже беспредметно, и этот тест надо переписать: {delivery!r}"
    )

    launch = shlex.split(_doc_wrapper_launch(tmp_path))
    # Всё, что стоит ДО образа, — флаги запуска; сеть настраивается только там.
    flags = _wrapper_run_flags(launch)
    for name, value in flags:
        assert name not in ("--network", "--net"), (
            f"скелет враппера из документа задаёт «{name} {value}». Сеть "
            "контейнеру нужна В ДВЕ стороны: к поставщику (agentский CLI "
            "авторизуется ключом из --env-file и ходит в api.cursor.com) и К "
            "САМОМУ ХАБУ — отчёт по контракту сдаётся обычным HTTP на адрес "
            f"из промта ({hub_base} в примере выше). Отрезав её, выкат "
            "остановится молча: прогон кончится без отчёта. Если сеть надо "
            "сузить, сужайте до СПИСКА АДРЕСОВ, а не до нуля"
        )


def test_the_doc_probe_passes_the_cli_argv_as_arguments(monkeypatch) -> None:
    """В пробе ``$CMD`` — ПОЗИЦИОННЫЙ аргумент враппера, а не что попало.

    Прежняя проверка требовала лишь подстроки ``$CMD`` в строке. Замер: строка
    ``CMD=$CMD /usr/local/bin/haiplane-review-run`` — то есть враппер запущен
    ГОЛЫМ, ровно та регрессия, ради которой тест писан, — оставляла весь набор
    зелёным (09.09.2026, находка машинного ревью dc051e23e80f3cdd). Подстрока
    не отличает аргумент от присваивания перед командой и от ``<<<"$CMD"``.
    """
    key = config.brand.ENV_PREFIX + "LOCAL_REVIEW_SANDBOX"
    text = _DEPLOY_DOC.read_text()
    recommended = [
        line.split(key + "=", 1)[1] for line in text.splitlines() if key + "=" in line
    ]
    assert recommended, f"в {_DEPLOY_DOC.name} нет рекомендованной строки {key}="
    wrapper = shlex.split(recommended[0])[-1]

    probes = [
        line
        for block in re.findall(r"```bash\n(.*?)```", text, re.S)
        # Продолжения строк склеиваются: проба разнесена по строкам обратным
        # слешем, и разорванную строку токенами не разобрать.
        for line in block.replace("\\\n", " ").splitlines()
        if wrapper in line
    ]
    assert probes, (
        f"в {_DEPLOY_DOC.name} нет пробы, запускающей враппер {wrapper} — "
        "проверка была бы пустой и зелёной при любом её содержании"
    )
    for line in probes:
        tokens = shlex.split(line, comments=True)
        assert wrapper in tokens, (
            f"в пробе «{line.strip()}» {wrapper} — не отдельное слово команды: "
            "разобрать такую пробу нельзя, и что она запускает, неизвестно"
        )
        tail = tokens[tokens.index(wrapper) + 1 :]
        assert "$CMD" in tail, (
            f"проба «{line.strip()}» запускает враппер БЕЗ дописанного argv "
            f"CLI ПОЗИЦИОННЫМ аргументом, а хаб запускает песочницу именно "
            f"так ({key[:-7]}CMD дописывается хвостом argv). Присваивание "
            "перед командой или подача через heredoc сюда не годятся: "
            "враппер получит пустой argv, и проба пройдёт там, где живой "
            "запуск пойдёт другой командой"
        )


def test_the_recommended_sudo_recipe_still_checks_the_scratch_group(
    monkeypatch, tmp_path
) -> None:
    """Рецепт ИЗ ДОКУМЕНТА прогоняется через not_ready(), а не через страж.

    AC-2 проверяет sandbox_uid() прямым вызовом — и этого мало: решение о
    запуске принимает not_ready(), а его единственная интеграционная проверка
    кормили формой ``systemd-run --uid=``. Замер: вернуть в _uid_outside_group
    разбор ТОЛЬКО ``--uid`` — sandbox_uid() на sudo остаётся верным, AC-2 и
    AC-4 зелёные, весь набор зелёный (rc=0), а not_ready() на рецепте из
    документа возвращает [] и прогон упирается в EACCES внутри чужого
    процесса, где причину уже никто не назовёт (09.09.2026, находка машинного
    ревью 2bdf3c910e8d581c). Ровно то же расхождение, что и на #1155: дверь
    заперта по прямому вызову и открыта по дороге, которой ходят.

    Форма берётся ИЗ ДОКУМЕНТА; подменяется только имя пользователя — оно на
    машине проверки не заведено, а проверяется здесь не оно, а то, что
    проверка вообще СРАБАТЫВАЕТ на рекомендованной форме записи.
    """
    key = config.brand.ENV_PREFIX + "LOCAL_REVIEW_SANDBOX"
    recommended = [
        line.split(key + "=", 1)[1]
        for line in _DEPLOY_DOC.read_text().splitlines()
        if key + "=" in line
    ]
    assert recommended, f"в {_DEPLOY_DOC.name} нет рекомендованной строки {key}="

    import os

    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "/bin/true")
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o2770)
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(shared))

    for sandbox in recommended:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        named = local_reviewer.sandbox_uid()
        assert named, (
            f"в рекомендованной строке «{sandbox}» страж не видит пользователя"
        )
        # «nobody» есть на обеих системах, где это гоняется, и в группе
        # каталога не состоит — как и в соседней проверке 45971e09.
        probe = sandbox.replace(named, "nobody")
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", probe)
        assert any("не состоит в группе" in r for r in local_reviewer.not_ready()), (
            f"на рекомендованной форме «{probe}» not_ready() НЕ назвал "
            "проблему с группой каталога прогонов: значит проверка, заведённая "
            "ради 45971e09, на рецепте из документа молча не срабатывает — "
            f"ровно как было до #1208. Вернулось: {local_reviewer.not_ready()}"
        )


async def test_the_queued_local_run_starts_with_a_live_access_code(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Код доступа второго в очереди чеканится К ЕГО СТАРТУ, а не при сдаче.

    Находка ревьюера Codex 11.09.2026 на ff5b518. Код чеканится в
    ``prepare_review_order`` — то есть внутри HTTP-запроса автора, — а слот
    хоста (``local_reviewer._HOST_BUDGET``) берётся уже в фоне, и ждать его
    можно дольше, чем живёт код (``CHAT_PAIR_CODE_SECONDS``, на проде 300 с
    против ревью в десятки минут). Второй в очереди стартовал бы с мёртвым
    кодом: основной HTTP-канал отчёта ему не выкупить, и прогон сваливается
    в слабый путь через stdout либо теряет отчёт вовсе.

    ПОЧЕМУ ПРОВЕРКА ЗДЕСЬ НЕ СМОТРИТ НА ЧАСЫ (#1325). Прежняя редакция ставила
    ``CHAT_PAIR_CODE_SECONDS = 1``, держала слот 2.5 с и спрашивала у базы
    ``expires_at > datetime('now')``. Требование она держала, но измеряла его
    гонкой: между чеканкой и чтением на нагруженном бегунке проходило больше
    секунды, и тест краснел при ВЕРНОМ продукте — прогон CI 35785728735 на
    develop, где менялся один документ. Воспроизведено на месте: вставь в эту
    же проверку ``await asyncio.sleep(1.2)`` перед запросом — и на неизменном
    коде получишь ``[False, False]``.

    Измеряется теперь ПОРЯДОК СОБЫТИЙ, а не разница времён. Снимок всех кодов
    берётся в момент, когда хаб порождает процесс; код, с которым стартовал
    второй прогон, обязан в снимке ПЕРВОГО старта отсутствовать — то есть быть
    отчеканен позже, уже на своём слоте. Мутация, ради которой тест и стоит:
    ``_prompt_at_slot`` возвращает исходный промт (код выдан при постановке в
    очередь) — тогда второй стартует с кодом, который существовал уже на
    старте первого, и снимок это показывает. Живость кода проверяется тем же
    предикатом, что и у ``redeem_code`` (не redeem'ом: он потратил бы код), но
    без ``expires_at``: срок жизни здесь продовый, а гонку создавала именно
    его подмена. Первый прогон проверяется вместе со вторым — иначе починка
    очереди могла бы сломать нормальный путь.
    """
    import re

    from hub.services import chat_pair
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-nope"}, "run": {"id": "r-nope"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _own_host_budget(monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, "import sys; sys.stdin.read()")

    # Первый прогон держит слот, пока в очередь не встал второй. Держит СОБЫТИЕ,
    # а не сон: сон здесь и был гонкой — он задавал срок, который нагруженный
    # бегунок не обязан соблюдать, а событие задаёт ПОРЯДОК, и его бегунок
    # нарушить не может.
    both_queued = asyncio.Event()
    starts: list[dict[str, object]] = []
    queue_snapshot: set[str] = set()
    in_flight = 0
    spawn = local_reviewer._spawn

    async def _watching(prompt: str, workdir: str, limit: int, started: float):
        nonlocal in_flight
        found = re.search(r'"code":"([^"]+)"', prompt)
        assert found, "в промте нет кода доступа — тогда судить не о чем"
        if not starts:
            # Снимок берётся здесь, а не раньше: к этому моменту ОБА заказа
            # уже в очереди, то есть код, выданный «при постановке», в базе
            # непременно есть. Снимок до второй сдачи не доказывал бы ничего.
            await both_queued.wait()
            rows = await db.execute_fetchall("SELECT code_hash FROM chat_pair_codes")
            queue_snapshot.update(dict(r)["code_hash"] for r in rows)
        code_hash = chat_pair.hash_pair_code(
            chat_pair.normalize_pair_code(found.group(1))
        )
        mine = await db.execute_fetchall(
            "SELECT 1 FROM chat_pair_codes WHERE code_hash = ? AND redeemed_at IS NULL",
            (code_hash,),
        )
        in_flight += 1
        starts.append(
            {"hash": code_hash, "unredeemed": bool(mine), "at_once": in_flight}
        )
        try:
            return await spawn(prompt, workdir, limit, started)
        finally:
            in_flight -= 1

    monkeypatch.setattr(local_reviewer, "_spawn", _watching)

    for slug in ("queued-first", "queued-second"):
        await _submitted(
            client,
            db,
            slug,
            policy={"review": "dispatch"},
            repo_name="mrpda/snip-portal",
            forge="gitverse",
        )
    both_queued.set()
    await wait_for_local_runs()

    assert len(starts) == 2, f"оба прогона обязаны были стартовать: {starts}"
    first, second = starts
    assert [s["at_once"] for s in starts] == [1, 1], (
        "прогоны шли одновременно — очереди не было, и про «второй в очереди» "
        f"тест ничего не проверил: {starts}"
    )
    assert first["hash"] != second["hash"], (
        "оба прогона стартовали с ОДНИМ кодом: второй его уже не выкупит, "
        "потому что первый его потратит"
    )
    for n, start in enumerate(starts, 1):
        assert start["unredeemed"], (
            f"прогон #{n} стартовал с кодом, которого нет в chat_pair_codes "
            "или который уже потрачен: основной канал отчёта ему не выкупить"
        )
    assert queue_snapshot, "снимок очереди пуст — сверять не с чем"
    assert second["hash"] not in queue_snapshot, (
        "второй в очереди стартовал с кодом, который существовал уже тогда, "
        "когда очередь только собралась, — то есть код выдан ПРИ ПОСТАНОВКЕ В "
        "ОЧЕРЕДЬ, а не к старту. Ждать слота можно дольше, чем живёт код, и "
        "такой прогон свалится в слабый путь через stdout или потеряет отчёт "
        "вовсе"
    )


async def test_the_slot_prompt_mints_a_code_or_says_why_not(
    db: aiosqlite.Connection, db_dsn: str, monkeypatch, caplog
):
    """Промт на слоте: либо свежий код, либо названная в журнале причина.

    Журнал здесь единственный след того, что отчёт поедет слабым путём
    (блоком в тексте прогона, #1036), и цена ложной строки высока в обе
    стороны. «Кода не выдавали вовсе» — это открытый режим или отозванный
    токен, и говорить там «код не выписать» значит звать оператора искать то,
    чего не было. Молчать же о настоящей неудаче значит оставить слабый путь
    незамеченным.

    Заодно проверяется, что подмена доходит до базы обоими способами, как и
    на проде: своим соединением по пути базы и на переданном живом
    соединении.
    """
    import logging
    import re

    from hub.services import chat_pair
    from hub.services.review_dispatch import _prompt_at_slot, instance_base_url

    principal = await _local_principal(db, monkeypatch)
    # Задача настоящая: код привязывается к ней внешним ключом, и выдуманный
    # номер проверял бы отказ базы, а не поведение подмены.
    task_id = await _node(db, title="слот", task_type="task", parent_id=None)
    # Запись зафиксирована: второй путь до базы открывает СВОЁ соединение, и
    # незакрытая транзакция фикстуры отдала бы ему «database is locked» —
    # то есть тест мерил бы блокировку, а не подмену.
    # Задача СТОИТ НА РЕВЬЮ своего поколения: код вида reviewer годен только
    # против той сдачи, на которую выписан, и без этого «код выписан» нельзя
    # было бы отличить от «кодом можно воспользоваться».
    await db.execute(
        "UPDATE tasks SET status='review', submission_generation=1 WHERE id=?",
        (task_id,),
    )
    await db.commit()
    base = instance_base_url().rstrip("/")
    stale = "AH-11111111"
    prompt = "шапка\n" + _delivery_block(task_id, stale, base) + "хвост\n"

    async def _at_slot(db_path, live_db, **kw):
        return await _prompt_at_slot(
            db_path,
            live_db,
            prompt=kw.pop("prompt", prompt),
            code=kw.pop("code", stale),
            task_id=task_id,
            generation=1,
            principal_id=kw.pop("principal_id", principal),
        )

    with caplog.at_level(logging.WARNING, logger="hub.services.review_dispatch"):
        assert await _at_slot(db_dsn, None, code="") == prompt, (
            "промт без блока доставки трогать нечем"
        )
        assert await _at_slot("", None) == prompt, "базы нет — менять нечем"
        assert caplog.records == [], (
            "сказано о коде, которого не выдавали: "
            f"{[r.getMessage() for r in caplog.records]}"
        )

        assert await _at_slot(db_dsn, None, principal_id=None) == prompt, (
            "без принципала код не выписать — промт обязан остаться прежним"
        )
        assert await _at_slot(db_dsn, None, prompt="без блока") == "без блока", (
            "блока доставки в промте нет — подменять нечего"
        )

    said = [r.getMessage() for r in caplog.records]
    assert len(said) == 2, f"о каждой неудаче обязаны были сказать один раз: {said}"
    assert all("stdout" in message for message in said), (
        f"в журнале не названо следствие — отчёт поедет слабым путём: {said}"
    )

    for db_path, live in ((db_dsn, None), ("", db)):
        produced = await _at_slot(db_path, live)
        assert produced != prompt, (
            f"промт не переписан (путь к базе {db_path!r}) — прогон унёс бы "
            "код, который к этому времени может быть уже удалён сборщиком"
        )
        found = re.search(r'"code":"([^"]+)"', produced)
        assert found and found.group(1) != stale, f"код не сменился: {produced[:200]}"
        # Проверяется ВЫКУП, а не строка в таблице. Живая строка не значит
        # годный код: reviewer-код приколот к задаче И К ПОКОЛЕНИЮ сдачи, и
        # код, выписанный не на то поколение, лежит в базе живым, а на двери
        # получает отказ — мутация «generation + 1» на проверке строки
        # выживала, на выкупе гибнет.
        assert await chat_pair.redeem_code(db, found.group(1)) is not None, (
            "выписанный код не выкупается: ревьюер не войдёт в основной канал "
            f"отчёта (путь к базе {db_path!r})"
        )
        assert prompt.replace(
            _delivery_block(task_id, stale, base), ""
        ) == produced.replace(_delivery_block(task_id, found.group(1), base), ""), (
            "переписан не только блок доставки — промт обязан отличаться ровно кодом"
        )


async def test_a_purged_access_code_is_replaced_before_the_run_starts(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Код, который сборщик уже УДАЛИЛ, обязан быть заменён к старту прогона.

    Находка ревьюера Codex 11.09.2026 на c6af952: продления мало. Пока прогон
    стоит в очереди, по часовому кругу поллера отрабатывает ``purge_expired``,
    и протухшую строку она не щадит — ``DELETE FROM chat_pair_codes WHERE
    expires_at < datetime('now')``. Продлять тогда нечего, и второй в очереди
    всё равно стартует с мёртвым кодом: основной канал отчёта не выкуплен,
    прогон куплен впустую.

    Гонка здесь ВОСПРОИЗВОДИТСЯ БЕЗ ЧАСОВ, а не подгадывается спячками.
    Первый прогон, уже взявший слот, останавливается перед порождением
    процесса и ждёт, пока вторая сдача встанет в очередь; тогда он состаривает
    коды и гоняет сборщик — ровно тот порядок, что описан в находке, — и
    только после этого отпускает себя. Время в этом тесте не участвует
    вовсе, поэтому и флака в нём нет.
    """
    import re

    from hub.services import chat_pair
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-nope"}, "run": {"id": "r-nope"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _own_host_budget(monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, "import sys; sys.stdin.read()")

    queued = asyncio.Event()
    purged: list[int] = []
    alive: list[bool] = []
    spawn = local_reviewer._spawn

    async def _watching(prompt: str, workdir: str, limit: int, started: float):
        found = re.search(r'"code":"([^"]+)"', prompt)
        assert found, "в промте нет кода доступа — тогда судить не о чем"
        rows = await db.execute_fetchall(
            "SELECT 1 FROM chat_pair_codes WHERE code_hash = ? "
            "AND redeemed_at IS NULL AND expires_at > datetime('now')",
            (chat_pair.hash_pair_code(chat_pair.normalize_pair_code(found.group(1))),),
        )
        alive.append(bool(rows))
        if not purged:
            # Слот держит этот прогон, поэтому вторая сдача сейчас стоит в
            # очереди — и её код доживает здесь свой срок ровно так, как
            # дожил бы на проде.
            await queued.wait()
            await db.execute(
                # Строки ревьюера хранятся сутки после срока (#1694): чтобы
                # сборщик их удалил, состариваем за границу хранения.
                "UPDATE chat_pair_codes SET expires_at = datetime('now', '-25 hours')"
            )
            await db.commit()
            purged.append(await chat_pair.purge_expired(db))
        return await spawn(prompt, workdir, limit, started)

    monkeypatch.setattr(local_reviewer, "_spawn", _watching)

    for slug in ("purged-first", "purged-second"):
        await _submitted(
            client,
            db,
            slug,
            policy={"review": "dispatch"},
            repo_name="mrpda/snip-portal",
            forge="gitverse",
        )
    queued.set()
    await wait_for_local_runs()

    assert purged and purged[0] >= 1, (
        f"сборщик ничего не удалил ({purged}) — тогда тест не воспроизвёл "
        "находку и зелен не за то"
    )
    assert len(alive) == 2, f"оба прогона обязаны были стартовать: {alive}"
    assert alive == [True, True], (
        f"прогон стартовал с кодом, которого в базе уже нет: {alive}. "
        "Продления мало: сборщик протухшего УДАЛЯЕТ строку, и продлять "
        "становится нечего — код к старту обязан быть выписан заново"
    )
