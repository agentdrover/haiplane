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
from hub.process_kill import kill_process_group
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


async def mechanical_outcomes(db, task_id: int, generation: int) -> dict[str, str]:
    """Исход механического шага по КАЖДОЙ разобранной находке: uid -> исход.

    Ключ — uid находки, а не поколение. Сначала здесь стояло «был ли проход по
    этому поколению», и это был дефект в ту же сторону, что и весь #1234:
    лестница добора (#879) кладёт в ОДНО поколение два отчёта, и у второго
    неразрешённые находки СВОИ. Событие первого отчёта заставляло пропустить
    второй целиком — новые находки не получали ни исхода, ни имени мутации,
    то есть снова становились тишиной.

    Дважды одну и ту же находку не разбирают (прогон набора — минуты), а
    новую разбирают всегда, в каком бы по счёту отчёте поколения она ни
    приехала.
    """
    rows = await fetchall(
        db,
        "SELECT payload FROM events WHERE kind=? AND task_id=?",
        (MECHANICAL_STEP_RECORDED, task_id),
    )
    answered: dict[str, str] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        if int(payload.get("generation") or 0) != int(generation):
            continue
        uid = str(payload.get("finding_uid") or "")
        if uid:
            answered[uid] = str(payload.get("outcome") or "")
    return answered


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

    # Пропускается не ОТЧЁТ, а разобранная НАХОДКА: второй отчёт поколения
    # приносит свои неразрешённые записи, и им исход положен так же.
    answered_before = await mechanical_outcomes(db, task_id, generation)
    uids = unresolved_uids(findings)
    pending = [(uid, f) for uid, f in zip(uids, findings) if uid not in answered_before]
    if not pending:
        return PassResult(False, "все находки этого поколения уже разобраны")

    runs_left = MAX_RUNS_PER_REPORT
    outcomes: list[dict[str, Any]] = []

    for uid, finding in pending:
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
    """Запустить и дождаться, а по потолку времени — СНЯТЬ ГРУППУ ПРОЦЕССОВ.

    Отмена ``communicate()`` не завершает ни подпроцесс, ни его потомков:
    ``wait_for`` снимает ожидание, а прогон продолжает жить с окружением и
    правами пользователя хаба — уже после того, как зовущий вернул «прогнать
    не смог» и снёс песочницу. Наблюдено опытом: команда, снятая по потолку в
    1 секунду, дописала свой файл через 4 секунды.

    ``start_new_session=True`` + ``kill_process_group`` — тот же приём, что у
    прогона валидации (#544): ``proc.kill()`` сигналит только тому pid,
    который породил хаб, а работу делает не он.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        await kill_process_group(proc)
        raise
    return proc.returncode or 0, out.decode(errors="replace")


def resolve_in_sandbox(sandbox: str, relative: str) -> str | None:
    """Настоящий путь внутри песочницы — или ``None``.

    ЗАЧЕМ. ``mutation.file`` приходит из ТЕКСТА НАХОДКИ, который пишет
    ревьюер, то есть это недоверенный вход. Пока путь только склеивали через
    ``os.path.join``, находка с текстом «гейт читает ../../opt/haiplane-hub/
    src/hub/config.py:1» уводила запись наружу: наблюдено опытом — файл вне
    песочницы получил закомментированную первую строку и НЕ был восстановлен
    уборкой рабочего дерева, потому что к дереву он не принадлежал. Это
    произвольная запись в файлы по недоверенному входу, а не мутация.

    ЧТО ИМЕННО ЗАКРЫВАЕТ. Путь разрешается целиком (``realpath`` проходит и
    по симлинкам, поэтому ссылка ВНУТРИ песочницы, указывающая наружу, тоже
    не проходит), и результат обязан лежать строго под корнем песочницы,
    быть обычным файлом и не быть симлинком. Абсолютный путь отвергается до
    склейки: ``os.path.join`` с абсолютным вторым аргументом молча выбрасывает
    первый.

    Отказ здесь — не авария, а штатный исход: находка едет человеку.
    """
    if not relative or os.path.isabs(relative) or "\x00" in relative:
        return None
    root = os.path.realpath(sandbox)
    target = os.path.realpath(os.path.join(root, relative))
    if target == root:
        return None
    try:
        if os.path.commonpath([root, target]) != root:
            return None
    except ValueError:
        # Разные тома — общего корня нет вовсе.
        return None
    # ``islink`` по УЖЕ разрешённому пути ловит только битую ссылку, но
    # проверяется и он: ``isfile`` на битой ссылке даёт False, и порядок
    # утверждений тут читается как одно правило — обычный файл, и ничего
    # кроме.
    if not os.path.isfile(target) or os.path.islink(target):
        return None
    return target


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

    ``ERROR`` СЧИТАЕТСЯ ОТКАЗОМ, А НЕ ПАДЕНИЕМ. Сначала строки ``ERROR ...``
    складывались в упавшие тесты вместе с ``FAILED ...`` — то есть ровно то,
    что абзац выше запрещает, делалось на строку ниже. ``ERROR`` печатается
    на ошибке сбора и на упавшем фикстуре: тест при этом не исполнялся, и
    сказать, изменила ли мутация поведение, нечем.
    """
    failed: list[str] = []
    passed = 0
    errored = False
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("ERROR "):
            errored = True
            continue
        if line.startswith("FAILED "):
            nodeid = line.split(None, 1)[1].split(" - ", 1)[0].strip()
            if nodeid and nodeid not in failed:
                failed.append(nodeid)
            continue
        found = _PASSED.search(line)
        if found is not None:
            passed = max(passed, int(found.group(1)))
    if errored:
        return None
    if failed:
        return SuiteResult(failed=tuple(failed), passed=passed)
    if rc != 0:
        return None
    return SuiteResult(failed=(), passed=passed)


async def run_suite_in_sandbox(
    *,
    repo_path: str,
    sha: str,
    mutation: Mutation | None = None,
) -> SuiteResult | None:
    """Прогнать набор в ОДНОРАЗОВОЙ песочнице, при мутации — под ней.

    Песочница — отдельное рабочее дерево на коммите сдачи, а не рабочая копия
    проекта. Так требование «дерево после шага чистое» выполняется не
    аккуратностью отката, а тем, что мутировать общую копию некуда: её здесь
    не открывают вовсе.

    ``mutation=None`` — БАЗОВЫЙ прогон: тот же коммит, тот же набор, ничего не
    мутировано. Без него «упал тест» ничего не доказывает (см.
    :class:`SandboxProbe`).
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
        if mutation is not None:
            # Путь из ТЕКСТА НАХОДКИ разрешается и запирается в песочнице —
            # см. resolve_in_sandbox. Отказ здесь штатный: находка едет
            # человеку, а не переписывает файл, до которого дотянулась.
            target = resolve_in_sandbox(sandbox, mutation.file)
            if target is None:
                log.warning(
                    "mutation probe refused a path outside the sandbox: %r",
                    mutation.file,
                )
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


class SandboxProbe:
    """Зонд с БАЗОВЫМ прогоном: убийством считается только НОВОЕ падение.

    ПОЧЕМУ БЕЗ БАЗОВОГО ПРОГОНА ШАГ ЛЖЁТ. Если на коммите сдачи уже есть
    падающий (или плавающий) тест, то полный прогон под ЛЮБОЙ мутацией
    покажет это падение, и его имя уедет в наблюдение как доказательство:
    подтвердится КАЖДАЯ неразрешённая находка подряд. Воспроизведено опытом —
    мутация строки, которую не зовёт ни один тест, вернулась с
    ``failed=('test_broken.py::test_already_red',)`` при красном наборе.

    Правило, оплаченное раньше и записанное отдельно: на красном наборе
    мутация «убита» ни о чём. Поэтому база снимается ОДИН РАЗ на отчёт (не на
    находку — прогон стоит минуты), а из падений мутированного прогона
    вычитаются те, что падали и без неё.

    База не снялась — зонд не отвечает вовсе: судить не о чем.
    """

    def __init__(self, repo_path: str, sha: str) -> None:
        self.repo_path = repo_path
        self.sha = sha
        self.baseline: SuiteResult | None = None
        self.baseline_taken = False

    async def _ensure_baseline(self) -> None:
        if self.baseline_taken:
            return
        self.baseline_taken = True
        self.baseline = await run_suite_in_sandbox(
            repo_path=self.repo_path, sha=self.sha
        )
        if self.baseline is None:
            log.warning("mutation probe: baseline run did not say anything")
        elif self.baseline.failed:
            log.warning(
                "mutation probe: baseline is red (%d failing) — those names "
                "cannot prove anything about a mutation",
                len(self.baseline.failed),
            )

    async def __call__(self, mutation: Mutation) -> SuiteResult | None:
        await self._ensure_baseline()
        if self.baseline is None:
            return None
        mutated = await run_suite_in_sandbox(
            repo_path=self.repo_path, sha=self.sha, mutation=mutation
        )
        if mutated is None:
            return None
        fresh = tuple(t for t in mutated.failed if t not in self.baseline.failed)
        return SuiteResult(failed=fresh, passed=mutated.passed)


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
    return SandboxProbe(repo_path, sha)


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
    "SandboxProbe",
    "configured_probe",
    "mechanical_outcomes",
    "mechanical_pass_after_report",
    "parse_suite_output",
    "resolve_in_sandbox",
    "run_mechanical_pass",
    "run_suite_in_sandbox",
]
