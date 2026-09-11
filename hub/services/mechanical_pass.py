"""Боевой вход механического шага: кто его зовёт на настоящем отчёте (#1234).

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. :mod:`hub.services.steward_corridor` решает, ЧТО
означает исход шага, и не знает ни про базу, ни про подпроцессы: его решение
обязано проверяться без настоящего прогона набора. Здесь живёт вторая
половина — кто зовёт шаг на настоящем отчёте, над какой находкой и чем
гоняет набор.

ПОЧЕМУ ЭТОТ МОДУЛЬ ПОЯВИЛСЯ ОТДЕЛЬНОЙ ПРАВКОЙ. В первой сдаче #1234 шага на
боевом пути не было вовсе: ``mechanical_step`` вызывался ровно из тестов, и
анализатор вызовов самого хаба (``hub/services/call_sites.py``) назвал это
вслух — ``only_tests -> mechanical_step``. Объявленный механический шаг не
исполнялся НИ ДЛЯ ОДНОГО настоящего отчёта, а три критерия приёмки проверяли
недостижимый код. Мутационная серия этого не ловит и поймать не может: и
серия, и тесты зовут функцию напрямую.

ЧТО ЗДЕСЬ ЕСТЬ, А ЧЕГО НЕТ. Есть: разбор ``unresolved`` настоящего отчёта,
вызов шага на КАЖДОЙ находке, запись исхода событием и одной сводкой в
задачу. Нет: починки кода (``scope_out`` задачи), перевода находок в
подтверждённые списком (подтверждает только упавший тест) и работающего по
умолчанию прогона набора.

ПОЧЕМУ ЖИВОЙ ПРОГОН ВЫКЛЮЧЕН ПО УМОЛЧАНИЮ. Тем же правилом, которым выключен
локальный ревьюер (#1180): запуск чужого кода на хосте, где лежат secrets.env
и ключи, — не «настройка по умолчанию», а инцидент. Пустая
``MUTATION_PROBE_CMD`` означает «живого прогона нет», и шаг тогда честно
возвращает :data:`~hub.services.steward_corridor.STEP_NO_RUN`: находка едет
человеку с НАЗВАННОЙ мутацией, которую он применит сам. Это по-прежнему
ответ лучше вчерашнего — вчера у такой находки не было ни исхода, ни имени.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.services.finding_identity import unresolved_uids
from hub.services.steward_corridor import (
    MAX_RUNS_PER_FINDING,
    Mutation,
    STEP_ANSWERED,
    SuiteResult,
    derive_mutation,
    mechanical_step,
)

log = logging.getLogger("hub")

#: Событие исхода шага по ОДНОЙ находке. Метрика задачи — «доля неразрешённых
#: находок, получивших исход без участия человека» — считается по этим
#: записям, поэтому исход пишется на каждую находку отдельно, а не сводкой:
#: сводка отвечает человеку, событие отвечает счёту.
MECHANICAL_STEP_RECORDED = "mechanical_step_recorded"

#: Сколько прогонов набора шаг тратит на ОДИН отчёт. Прогон набора — минуты,
#: и отчёт с шестью неразрешёнными находками (#1158 такой был) съел бы час.
#: Находки сверх бюджета исход всё равно получают — но без прогона, с
#: названной мутацией.
MAX_RUNS_PER_REPORT = 3

#: Прогон набора: применяет мутацию в СВОЕЙ песочнице, гоняет, откатывает.
#: ``None`` — «прогнать не смог»; это не зелёный набор (см. STEP_NO_RUN).
Probe = Callable[[Mutation], Awaitable[SuiteResult | None]]

#: «3475 passed» в итоговой строке pytest. Число прошедших — то, что человек
#: увидит вместо тишины при пережившей мутации (AC-3).
_PASSED = re.compile(r"(\d+)\s+passed")


@dataclass
class PassResult:
    """Что механический проход сделал с отчётом."""

    ran: bool
    reason: str = ""
    outcomes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def answered(self) -> int:
        """Сколько находок получили исход, не требующий человека."""
        return sum(1 for o in self.outcomes if o["outcome"] in STEP_ANSWERED)


def _json_list(raw: Any) -> list[dict]:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "[]")
        except ValueError:
            return []
    else:
        parsed = raw or []
    return [item for item in parsed if isinstance(item, dict)]


async def _already_passed(db, task_id: int, generation: int) -> bool:
    """Был ли уже проход по этому поколению.

    Идемпотентность по ПОКОЛЕНИЮ, а не по отчёту: лестница добора (#879)
    кладёт в одно поколение два отчёта, и второй приход не должен гонять
    набор заново по тем же находкам.
    """
    rows = await fetchall(
        db,
        "SELECT payload FROM events WHERE kind=? AND task_id=?",
        (MECHANICAL_STEP_RECORDED, task_id),
    )
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        if int(payload.get("generation") or 0) == int(generation):
            return True
    return False


async def run_mechanical_pass(
    db,
    task_id: int,
    *,
    probe: Probe | None = None,
) -> PassResult:
    """Пройти механическим шагом по неразрешённым находкам отчёта.

    Вызывается с боевого пути — сразу после приёма отчёта машинного ревью,
    ПЕРЕД тем, как задача останется ждать человека. Ничего не решает за
    человека и ничего не чинит: ставит зонд и записывает наблюдение.

    ``probe`` инъектируется; ``None`` означает «живого прогона нет», и тогда
    каждая находка с выводимой мутацией получает
    :data:`~hub.services.steward_corridor.STEP_NO_RUN` — названную мутацию
    без наблюдения, а не выдуманный зелёный набор.
    """
    row = await repo.get_task(db, task_id)
    if row is None:
        return PassResult(False, "задачи нет")
    task = dict(row)
    generation = int(task.get("submission_generation") or 0)
    if generation <= 0:
        return PassResult(False, "сдачи не было")

    reports = await repo.machine_reviews_of_generation(db, task_id, generation)
    if not reports:
        return PassResult(False, "отчёта об этом поколении нет")
    report = dict(reports[-1])
    findings = _json_list(report.get("unresolved"))
    if not findings:
        # Ровно та ветка, по которой идёт подавляющее большинство отчётов.
        # Дёшево и молча: шаг существует для неразрешённых находок, и отчёт
        # без них ему не предмет.
        return PassResult(False, "неразрешённых находок нет")

    if await _already_passed(db, task_id, generation):
        return PassResult(False, "проход по этому поколению уже был")

    uids = unresolved_uids(findings)
    runs_left = MAX_RUNS_PER_REPORT
    outcomes: list[dict[str, Any]] = []

    for uid, finding in zip(uids, findings):
        # Мутация выводится здесь ВТОРОЙ раз (шаг выведет её сам) — ровно
        # затем, чтобы знать, положен ли этой находке прогон, и не занимать
        # бюджет находкой, которой мутировать нечего. Решает исход по-прежнему
        # только шаг: здесь не сравнивают ни строк, ни исходов.
        mutation = derive_mutation(finding)
        suite: SuiteResult | None = None
        if mutation is not None and probe is not None and runs_left > 0:
            runs_left -= MAX_RUNS_PER_FINDING
            try:
                suite = await probe(mutation)
            except Exception:  # noqa: BLE001 - отказ зонда не исход шага
                log.exception("mutation probe failed for task #%s", task_id)
                suite = None

        # Прогон уже состоялся (или не состоялся) ВЫШЕ, а шагу отдаётся
        # готовый ответ: ``mechanical_step`` синхронный, и делать его
        # асинхронным ради одного вызывающего значило бы переписать решающую
        # функцию под инфраструктуру, а не наоборот.
        def _answer(_m: Mutation, _suite: SuiteResult | None = suite):
            return _suite

        step = mechanical_step(
            finding,
            _answer,
            already_checked=(
                f"отчёт машинного ревью #{report.get('id')} по сдаче "
                f"#{generation} прочитан",
            ),
        )
        outcome = {
            "finding_uid": uid,
            "outcome": step.outcome,
            "observation": step.observation,
            "mutation": step.mutation.name if step.mutation else "",
            "needs_human": step.needs_human,
            "confirms": step.confirms,
            "generation": generation,
        }
        outcomes.append(outcome)
        await repo.insert_event(
            db,
            kind=MECHANICAL_STEP_RECORDED,
            task_id=task_id,
            actor="hub",
            payload=outcome,
        )

    result = PassResult(True, "", outcomes)
    await repo.add_task_update(db, task_id, "hub", "alert", _summary(result))
    await db.commit()
    return result


def _summary(result: PassResult) -> str:
    """Сводка прохода — то, что читает человек, которого позвали.

    Числом и словами сразу: «4 неразрешённых» человеку не говорит ничего, а
    имя упавшего теста и имя мутации он проверит сам. Находка, приехавшая без
    этого списка, заставляет начинать с нуля — ровно то, чего задача не
    хотела.
    """
    lines = [
        f"Механический шаг по неразрешённым находкам: {len(result.outcomes)} "
        f"находок, из них {result.answered} получили исход без человека."
    ]
    for item in result.outcomes:
        mark = "—" if item["needs_human"] else "+"
        lines.append(f"{mark} [{item['outcome']}] {item['observation']}")
    if result.answered < len(result.outcomes):
        lines.append(
            "Остальные едут человеку С ТЕМ, ЧТО УЖЕ ПРОВЕРЕНО: названная "
            "мутация применяется руками ровно там, где сказано."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Живой прогон: песочница, мутация, набор, уборка
# ---------------------------------------------------------------------------


async def _run(argv: list[str], cwd: str | None, timeout: int) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode or 0, out.decode(errors="replace")


def _apply(path: str, start: int, end: int) -> bool:
    """Закомментировать строки ``start..end``. False — мутация не вышла.

    Только Python и только комментированием: мутация, ломающая РАЗБОР файла,
    роняет не тест, а сбор набора, и «упало всё» выдало бы себя за
    подтверждение находки. Поэтому результат проверяется ``ast.parse``, и
    неразбираемый — это отказ прогона, а не его исход.
    """
    if not path.endswith(".py"):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return False
    if start < 1 or end > len(lines):
        return False
    mutated = list(lines)
    for i in range(start - 1, end):
        mutated[i] = "#" + mutated[i]
    source = "".join(mutated)
    try:
        ast.parse(source)
    except SyntaxError:
        return False
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(source)
    except OSError:
        return False
    return True


def parse_suite_output(rc: int, out: str) -> SuiteResult | None:
    """Что вернул прогон: имена упавших тестов и число прошедших.

    ``None`` — прогон ничего не сказал: набор не собрался, pytest не
    запустился, вывод не разобран. Ненулевой код возврата БЕЗ единого
    названного упавшего теста — это именно такой случай: ошибка сбора и
    упавший тест дают один и тот же ненулевой код, а подтверждать находку
    ошибкой сбора значит подтверждать её тем, что мутация синтаксически
    задела файл.
    """
    failed: list[str] = []
    passed = 0
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            nodeid = line.split(None, 1)[1].split(" - ", 1)[0].strip()
            if nodeid and nodeid not in failed:
                failed.append(nodeid)
            continue
        found = _PASSED.search(line)
        if found is not None:
            passed = max(passed, int(found.group(1)))
    if failed:
        return SuiteResult(failed=tuple(failed), passed=passed)
    if rc != 0:
        return None
    return SuiteResult(failed=(), passed=passed)


async def live_probe(
    mutation: Mutation,
    *,
    repo_path: str,
    sha: str,
) -> SuiteResult | None:
    """Применить мутацию в ОДНОРАЗОВОЙ песочнице и прогнать набор.

    Песочница — отдельное рабочее дерево на коммите сдачи, а не рабочая копия
    проекта. Так требование «дерево после шага чистое» выполняется не
    аккуратностью отката, а тем, что мутировать общую копию некуда: её здесь
    не открывают вовсе.
    """
    argv = shlex.split(config.MUTATION_PROBE_CMD or "")
    if not argv or not repo_path or not sha:
        return None
    base = config.MUTATION_PROBE_SCRATCH_DIR or None
    if base and not os.path.isdir(base):
        return None
    sandbox = tempfile.mkdtemp(prefix="haiplane-mutation-", dir=base)
    try:
        rc, _out = await _run(
            ["git", "-C", repo_path, "worktree", "add", "--detach", sandbox, sha],
            None,
            config.MUTATION_PROBE_TIMEOUT_SEC,
        )
        if rc != 0:
            return None
        target = os.path.join(sandbox, mutation.file)
        if not os.path.isfile(target):
            return None
        if not _apply(target, mutation.start_line, mutation.end_line):
            return None
        rc, out = await _run(argv, sandbox, config.MUTATION_PROBE_TIMEOUT_SEC)
        return parse_suite_output(rc, out)
    except (OSError, TimeoutError, asyncio.TimeoutError):
        log.warning("mutation probe could not run in %s", repo_path)
        return None
    finally:
        # Уборка безусловная и в два приёма: git снимает регистрацию дерева,
        # rmtree — то, что осталось, если git не смог. Песочница, пережившая
        # шаг, — это мусор на диске хаба, растущий с каждым отчётом.
        try:
            await _run(
                ["git", "-C", repo_path, "worktree", "remove", "--force", sandbox],
                None,
                60,
            )
        except (OSError, TimeoutError, asyncio.TimeoutError):
            log.warning("could not remove mutation sandbox %s", sandbox)
        shutil.rmtree(sandbox, ignore_errors=True)


async def configured_probe(db, task_id: int) -> Probe | None:
    """Зонд для этой задачи, или ``None``, если гонять негде.

    Три условия, и каждое — «нет», а не «попробуем»: настроенная команда
    набора, рабочая копия проекта и коммит сдачи. Отсутствие любого из них
    означает, что наблюдения не будет; выдумывать его вместо отказа — ровно
    та ошибка, из-за которой заведена задача.
    """
    if not (config.MUTATION_PROBE_CMD or "").strip():
        return None
    from hub.services.orchestration import project_git_context

    ctx = await project_git_context(db, task_id)
    repo_path = str(ctx.get("repo") or "")
    row = await repo.get_task(db, task_id)
    sha = str(dict(row).get("submission_sha") or "") if row is not None else ""
    if not repo_path or not sha:
        return None

    async def _probe(mutation: Mutation) -> SuiteResult | None:
        return await live_probe(mutation, repo_path=repo_path, sha=sha)

    return _probe


async def mechanical_pass_after_report(db, task_id: int) -> PassResult:
    """Боевой вход: механический шаг сразу после приёма отчёта.

    Стоит ПОСЛЕ автовердикта и добора и ПЕРЕД тем, как задача останется
    ждать человека, — то есть ровно там, где задача его и объявила: «прежде
    чем звать человека, стюард делает то, что умеет сам».
    """
    probe = await configured_probe(db, task_id)
    return await run_mechanical_pass(db, task_id, probe=probe)


__all__ = [
    "MAX_RUNS_PER_REPORT",
    "MECHANICAL_STEP_RECORDED",
    "PassResult",
    "configured_probe",
    "live_probe",
    "mechanical_pass_after_report",
    "parse_suite_output",
    "run_mechanical_pass",
]
