"""Hub-dispatched cross-model reviews (#757): local_reviewer (split of the former single file, #1670)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import aiosqlite
from httpx import AsyncClient

from hub import config, services
from hub import repository as repo
from hub.integrations import cursor_cloud, local_reviewer
from hub.models import MachineReviewView, TaskSubmitReview
from hub.services.review_dispatch import (
    ENVIRONMENT_REFUSAL_NOTE,
    _ladder_cause_note,
    count_environment_refusals,
    is_environment_refusal,
    maybe_dispatch_review,
)
from tests.review_dispatch_support import (
    _LOCAL_REPORT,
    _LOST_ANSWER,
    _REAL_REFUSAL,
    _DispatchRecorder,
    _Listing,
    _local_principal,
    _own_host_budget,
    _report_on_current,
    _reporting_stub,
    _scratch,
    _seed_report,
    _stub_reviewer,
    _submitted,
    _sudo_sandbox,
    _wire,
)


# --- Находки ревью по сдаче #1 (отчёт #258) ----------------------------------
#
# Шесть подтверждённых находок про этот же локальный путь. Тесты ниже названы
# по дефекту, а не по фиксу: каждый обязан падать на коде ДО правки, иначе он
# не ловит класс, ради которого написан.


async def test_the_local_top_up_keeps_the_profile_it_was_ordered_with(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находка 7ed386a8: добор лестницы на не-GitHub снова покупал lite.

    maybe_dispatch_review получал force_profile=DEEP, а до prepare_review_order
    он не доезжал — локальный путь выбирал профиль заново и заказывал второй
    однопроходный прогон вместо харнесса. Молча и за деньги: в карточке стоит
    «профиль lite», как будто так и заказывали.
    """
    from hub.services.review_dispatch import (
        DEEP,
        maybe_dispatch_review,
        wait_for_local_runs,
    )

    recorder = _DispatchRecorder({"agent": {"id": "bc-tu"}, "run": {"id": "r-tu"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(
        monkeypatch, tmp_path, _reporting_stub({**_LOCAL_REPORT, "tokens_spent": 1000})
    )

    task_id = await _submitted(
        client,
        db,
        "spike-local-topup",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()
    await db.commit()

    assert await maybe_dispatch_review(db, task_id, force_profile=DEEP)
    await wait_for_local_runs()
    await db.commit()

    rows = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT profile, channel FROM review_dispatches WHERE task_id=? "
            "ORDER BY id",
            (task_id,),
        )
    ]
    assert len(rows) == 2 and rows[1]["channel"] == "local"
    assert rows[1]["profile"] == DEEP, (
        "добор заказан deep — локальный путь обязан исполнить заказанное, а не "
        f"выбрать профиль заново: {rows}"
    )


async def test_the_timed_out_reviewer_is_actually_dead(monkeypatch, tmp_path):
    """Находка aa620d54: AC-3 проверял ТЕКСТ в ленте, а не смерть процесса.

    kill_process_group никогда не бросает, поэтому прежний тест оставался
    зелёным и при живом ревьюере — он читал только слова хаба о самом себе.
    Здесь доказательство внешнее: полезная нагрузка пишет маркер ПОСЛЕ
    таймаута, и файла быть не должно.

    Хвост `; :` не украшение (#544): одна простая команда была бы заменена
    шеллом через exec, полезная нагрузка стала бы тем самым pid, который мы
    сигналим, и тест прошёл бы на macOS при живом процессе на Linux.
    Составная команда заставляет sh форкнуться — до внука дотягивается только
    убийство группы.
    """
    import shlex
    import sys

    marker = tmp_path / "still_alive"
    payload = (
        f"{sys.executable} -c "
        + shlex.quote(
            "import time, pathlib, sys; sys.stdin.read(); time.sleep(2.5); "
            f"pathlib.Path({str(marker)!r}).write_text('x')"
        )
        + "; :"
    )
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, "/bin/sh")
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", shlex.join(["-c", payload]))
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    run = await local_reviewer.run_review("промт", timeout=1)

    assert run is not None and run.timed_out
    await asyncio.sleep(3.0)
    assert not marker.exists(), (
        "ревьюер пережил собственный таймаут: хаб написал в ленту, что снял "
        "процесс, и это было бы неправдой"
    )


async def test_the_run_guard_judges_the_deadline_the_run_will_get(
    monkeypatch, tmp_path
):
    """Стража спрашивают НА КАЖДОМ ПРОГОНЕ, а не только на готовности.

    Находка 10.09.2026 (ревьюер Codex, воспроизведено на ef2198fc): проверка
    сравнивала срок контейнера с ``LOCAL_REVIEW_TIMEOUT_SEC`` даже там, где
    прогону отмерили меньше. ``podman run --timeout 1800`` проходил готовность
    при умолчании 1800, а ``run_review(timeout=1)`` убивал только клиента
    podman — контейнер жил оставшиеся почти полчаса, жёг CPU и мог прислать
    отчёт по закрытому прогону.

    ПОВОРОТ 10.09.2026. Половина про СРОК потеряла предмет: контейнерных
    запусков в песочнице больше не бывает (закрытый набор форм, #1208), а обе
    формы набора оставляют полезную нагрузку потомком хаба — убийство группы
    доходит до неё при любом сроке, и суждения, зависящего от ``timeout``, у
    стража не осталось. Вход не выброшен: та же строка стоит здесь же и
    проверяется на отказ. Вторая половина осталась целиком и она несущая:
    отказ — это не мнение, а незапуск, иначе страж был бы суждением, которое
    некому применить.
    """
    sandbox = "/usr/bin/podman run --rm -i --timeout 1800 img"
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
    monkeypatch.setattr(config, "LOCAL_REVIEW_TIMEOUT_SEC", 1800)
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "cursor-agent --print")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    assert local_reviewer.sandbox_problem(), (
        "контейнерный запуск в набор форм не входит — иначе проверка ниже беспредметна"
    )

    # И это не только суждение: прогон НЕ ЗАПУСКАЕТСЯ. Иначе страж остался бы
    # мнением, которое некому применить, — весь класс дефектов #1208 именно
    # об этом.
    spawned: list[str] = []
    monkeypatch.setattr(
        local_reviewer,
        "_spawn",
        lambda *a, **k: spawned.append("да"),  # noqa: ARG005
    )
    assert await local_reviewer.run_review("промт", timeout=1) is None, (
        "прогон с песочницей вне закрытого набора форм запускать нельзя"
    )
    assert not spawned, (
        "страж высказался, а хаб всё равно породил процесс: контейнер "
        f"пережил бы прогон на 1799 с, {spawned}"
    )


async def test_two_local_runs_never_overlap_on_the_host(monkeypatch, tmp_path):
    """Хост держит один прогон разом, и это держит ХАБ, а не скрипт враппера.

    Находка 10.09.2026 (ревьюер Codex, подтверждена на хосте хаба). Рабочий
    враппер снимал «хвосты» строкой ``podman rm -af``, называя основанием
    «два ревьюера разом хосту не по карману». Хаб такого ограничения не знал:
    ``_LOCAL_RUNS`` — обычный ``dict`` по идентификатору заказа, ни очереди,
    ни сериализации. Два ревью, начавшихся близко по времени, сносили друг
    друга, и в карточке это ложилось отказом прогона с ЛОЖНОЙ причиной.

    Доказательство внешнее: полезная нагрузка отмечает вход и выход в общем
    файле. Пересечение читается из ПОРЯДКА отметок, а не из времени — по
    времени тест был бы флаким на загруженной машине.
    """
    import shlex

    _own_host_budget(monkeypatch)
    marks = tmp_path / "marks"
    payload = (
        f"printf 'in\n' >> {shlex.quote(str(marks))}; "
        "sleep 0.3; "
        f"printf 'out\n' >> {shlex.quote(str(marks))}"
    )
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, "/bin/sh")
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", shlex.join(["-c", payload]))
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    runs = await asyncio.gather(
        *[local_reviewer.run_review("промт", timeout=30) for _ in range(3)]
    )
    assert all(run is not None and not run.timed_out for run in runs), (
        f"прогоны не состоялись — тогда о пересечении судить не по чему: {runs}"
    )
    seen = marks.read_text().split()
    assert seen == ["in", "out"] * 3, (
        f"прогоны шли внахлёст: {seen}. На хосте это значит второй контейнер "
        "при отмеренных первому 1500 МБ и 1.5 CPU — и уборку враппера, "
        "которая сносит живой контейнер соседа"
    )


async def test_stopping_the_hub_kills_the_local_reviewer(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находка d478b896: отмена проходила мимо перехвата и оставляла сироту.

    CancelledError наследует BaseException, поэтому except на таймаут его не
    видел, а wait_for_local_runs, объявленная «для остановки хаба», из
    lifespan никем не звалась. Проверяется тем же внешним маркером.
    """
    from hub.services.review_dispatch import cancel_local_runs

    import shlex
    import sys

    marker = tmp_path / "outlived_the_hub"
    payload = (
        f"{sys.executable} -c "
        + shlex.quote(
            "import time, pathlib, sys; sys.stdin.read(); time.sleep(2.5); "
            f"pathlib.Path({str(marker)!r}).write_text('x')"
        )
        + "; :"
    )
    recorder = _DispatchRecorder({"agent": {"id": "bc-st"}, "run": {"id": "r-st"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, "/bin/sh")
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", shlex.join(["-c", payload]))
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))

    await _submitted(
        client,
        db,
        "spike-local-stop",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await asyncio.sleep(0.3)

    await cancel_local_runs()

    await asyncio.sleep(3.0)
    assert not marker.exists(), (
        "хаб остановился, а ревьюер продолжил работать сиротой — и мог бы ещё "
        "прислать отчёт по прогону, за которым больше некому смотреть"
    )


async def test_a_chatty_reviewer_does_not_grow_the_hub(monkeypatch, tmp_path):
    """Находка 30a65c79: лимит применялся ПОСЛЕ чтения всего вывода в память.

    communicate() читает оба потока до EOF, и OUTPUT_CAP резал уже собранную
    строку — то есть не ограничивал ничего. Лимит памяти при этом стоит на
    слайсе ревьюера, а росла память ХАБА.
    """
    import shlex
    import sys

    monkeypatch.setattr(local_reviewer, "OUTPUT_CAP", 1000)
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, sys.executable)
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_CMD",
        shlex.join(["-c", "import sys; sys.stdin.read(); print('x' * 500000)"]),
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    run = await local_reviewer.run_review("промт", timeout=30)

    assert run is not None and not run.timed_out
    assert len(run.output) <= 1000, "в памяти остаётся только хвост под лимитом"
    assert run.dropped > 400_000, (
        "выброшенное считается: «вывод кончился» и «вывод обрезан» — разные "
        "факты, и второй должен быть виден в ленте"
    )


async def test_a_detaching_sandbox_is_refused_by_name(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находки a6aaffbc и 7897d52c: песочница из документа отсоединяла процесс.

    systemd-run без --scope поднимает transient service: родитель CLI — PID 1,
    таймаут его не снимет, а cwd и белый список окружения осядут на клиенте.
    Хаб не имеет права запускать прогон, снять который он не сможет, — и
    обязан назвать причину, а не промолчать.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-ds"}, "run": {"id": "r-ds"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    runs: list[str] = []

    async def _never(prompt, *, timeout=None, prompt_at_slot=None):
        runs.append(prompt)
        return None

    monkeypatch.setattr(local_reviewer, "run_review", _never)
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --quiet --pipe --uid=haiplane-reviewer --",
    )

    task_id = await _submitted(
        client,
        db,
        "spike-local-detach",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()

    assert runs == [], "прогон, который нельзя снять, не запускается вовсе"
    assert not await db.execute_fetchall(
        "SELECT 1 FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert any("--scope" in a for a in alerts), (
        f"отказ обязан назвать недостающий флаг, а не «песочница неверна»: {alerts}"
    )


# --- Находки ревью по сдаче #3 (отчёт #265) ----------------------------------
#
# Первая из них — регрессия, которую открыл фикс предыдущей: пока песочница
# отсоединяла процесс, права каталога-однодневки никого не задевали, а с
# --scope они стали решающими. Ровно то, что харнесс называет
# fix-induced-regression.


async def test_the_scratch_dir_is_writable_by_the_reviewer(monkeypatch, tmp_path):
    """Находка 92eba4f8: mkdtemp даёт 0700, и ревьюеру закрыт его же каталог.

    С --scope cwd, HOME и TMPDIR доезжают до CLI по-настоящему — значит
    каталог, созданный ХАБОМ, должен быть доступен ЧУЖОМУ пользователю по
    общей группе. 0700 владельца-создателя означал бы EACCES на собственный
    рабочий каталог: CLI упал бы, не начав, а в ленте стояло бы «завершилось
    без отчёта» вместо успешного прогона.

    Проверяется правами, снятыми САМИМ процессом со своего cwd, а не
    вычислением по коду: второго unix-пользователя на машине разработчика нет,
    но режим каталога — это ровно то, что решает исход.
    """
    import shlex
    import sys

    probe = tmp_path / "mode.txt"
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, sys.executable)
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_CMD",
        shlex.join(
            [
                "-c",
                "import os, sys; sys.stdin.read(); "
                f"open({str(probe)!r}, 'w').write(oct(os.stat(os.getcwd()).st_mode & 0o777))",
            ]
        ),
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    run = await local_reviewer.run_review("промт", timeout=30)

    assert run is not None and not run.timed_out
    assert probe.read_text() == "0o770", (
        "каталог прогона обязан быть доступен группе, общей у хаба и ревьюера: "
        f"получено {probe.read_text()}"
    )


async def test_stopping_the_hub_closes_the_dispatch_row(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находка 30e3ac32: процесс убивали, а строку прогона не закрывали.

    _supervise_local_run ждал run_review ДО try, поэтому CancelledError
    проходил мимо except Exception и _settle_local_run не вызывался. Итог:
    процесс мёртв, а карточка в настоящем времени говорит «Машинное ревью
    запущено ЛОКАЛЬНО», и свип назовёт это потерей только минут через сорок
    пять. «Ещё идёт» и «снято при остановке» — разные факты.
    """
    from hub.services.review_dispatch import cancel_local_runs

    import shlex
    import sys

    recorder = _DispatchRecorder({"agent": {"id": "bc-sd"}, "run": {"id": "r-sd"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, sys.executable)
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_CMD",
        shlex.join(["-c", "import time, sys; sys.stdin.read(); time.sleep(30)"]),
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))

    task_id = await _submitted(
        client,
        db,
        "spike-local-shutdown",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await asyncio.sleep(0.3)

    await cancel_local_runs()
    await db.commit()

    rows = await db.execute_fetchall(
        "SELECT status FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    assert dict(rows[-1])["status"] == "failed", (
        "снятый прогон не имеет права остаться активным: активная строка "
        "означает «ревью идёт»"
    )
    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert any("остановке хаба" in a for a in alerts), (
        f"причина обязана быть названа, а не выведена из тишины: {alerts}"
    )


def test_the_lifespan_cancels_local_runs_on_shutdown():
    """Находка 6e4d7e6b: тест снятия звал функцию, а не путь, которым она живёт.

    Сними две строки из finally в lifespan — и прогоны снова переживут хаб,
    а тест, зовущий cancel_local_runs напрямую, останется зелёным. Здесь
    проверяется именно ВЫЗОВ из lifespan.

    Читается исходник, а не поведение, и это осознанный размен: настоящий
    подъём lifespan открывает боевую базу по HUB_DB_PATH и поднимает
    MCP-транспорт, то есть тест трогал бы установку разработчика. Приём в
    репозитории принятый — так же сверяются с исходником страж имени бренда
    и проверка читателя политики.
    """
    import ast
    import pathlib

    source = pathlib.Path(__file__).resolve().parent.parent / "hub" / "app.py"
    tree = ast.parse(source.read_text())
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    finalizers = [
        n for node in ast.walk(fn) if isinstance(node, ast.Try) for n in node.finalbody
    ]
    called = {
        n.func.id
        for block in finalizers
        for n in ast.walk(block)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "cancel_local_runs" in called, (
        "остановка хаба обязана снимать локальные прогоны: без вызова в finally "
        "агентский CLI переживёт процесс, который его породил"
    )


async def test_the_collector_keeps_reading_past_the_cap(monkeypatch):
    """Находка cc48162a: тест лимита не отличал чтение чанками от communicate().

    dropped = len(всё) - cap считается и после полного чтения в память, так
    что прежний тест был зелёным и для той реализации, которую находка 30a65c79
    просила убрать. Здесь проверяется САМО поведение читателя: он берёт поток
    по кускам и, перебрав лимит, ПРОДОЛЖАЕТ читать — иначе ребёнок встанет на
    полной трубе и умрёт только по таймауту.
    """

    class _Stream:
        def __init__(self, chunks: list[bytes]):
            self.chunks = chunks
            self.reads = 0

        async def read(self, _n: int) -> bytes:
            self.reads += 1
            return self.chunks.pop(0) if self.chunks else b""

    class _Proc:
        def __init__(self, stream):
            self.stdout = stream
            self.waited = False

        async def wait(self):
            self.waited = True

    monkeypatch.setattr(local_reviewer, "OUTPUT_CAP", 500)
    stream = _Stream([b"a" * 400, b"b" * 400, b"c" * 400])
    proc = _Proc(stream)

    kept, dropped = await local_reviewer._collect(proc)

    assert len(kept) == 500 and kept.endswith(b"b"), "в памяти остаётся ровно лимит"
    assert dropped == 700, f"выброшенное считается поштучно, а не на глаз: {dropped}"
    assert stream.reads >= 4, (
        "читатель обязан вычитать поток до конца кусками, а не остановиться "
        f"на лимите: замолчавший читатель оставляет ребёнка на полной трубе "
        f"(чтений {stream.reads})"
    )
    assert proc.waited, "процесс должен быть дождан, иначе останется зомби"


# --- Находки ревью по сдаче #4 (отчёт #267) ----------------------------------


def test_a_scratch_dir_that_cannot_be_shared_is_refused_by_name(monkeypatch, tmp_path):
    """Неразрешённая 45971e09: 0770 без верной группы — всё тот же отказ.

    Валидатор прав в существе: хаб ставит каталогу прогона 0770, то есть даёт
    доступ ГРУППЕ, а если группа не та, ревьюер получит EACCES — и тест на
    режим этого не увидит, потому что владелец проходит сам. Проверять
    членство чужого пользователя в группе на машине разработчика нельзя, но
    можно проверить то, ОТ ЧЕГО оно зависит, и отказать заранее с названной
    причиной. Три дороги в одно и то же состояние, и все три названы.
    """
    import os

    import pwd

    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "/bin/true")
    # Песочница обязана быть ФОРМОЙ ИЗ НАБОРА, иначе not_ready() назовёт её, а
    # не каталог, и тест судил бы другое (#1208, поворот 10.09.2026: прежде
    # здесь стоял «/usr/bin/env», который набор не принимает). Пользователь —
    # сам вызывающий: он владеет каталогом, и по группе проходит.
    me = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", f"/usr/bin/sudo -n -u {me} /usr/local/bin/wrap"
    )
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(tmp_path / "missing"))
    assert any("каталога" in r for r in local_reviewer.not_ready()), (
        "каталог, созданный хабом на лету, получит группу хаба — то есть ту, "
        "куда ревьюеру хода нет; это причина, а не мелочь"
    )

    plain = tmp_path / "plain"
    plain.mkdir()
    os.chmod(plain, 0o770)
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(plain))
    assert any("setgid" in r for r in local_reviewer.not_ready()), (
        "без setgid подкаталог унаследует основную группу хаба, и права 0770 "
        "достанутся не тому"
    )

    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o2770)
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(shared))
    assert local_reviewer.not_ready() == [], "правильный каталог претензий не вызывает"

    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=nobody /usr/local/bin/wrap",
    )
    assert any("не состоит в группе" in r for r in local_reviewer.not_ready()), (
        "пользователь песочницы вне группы каталога — тот же EACCES, только "
        "предсказуемый заранее"
    )

    # ЧИСЛОВОЙ --uid: systemd-run принимает и его, а getpwnam("1") бросает
    # KeyError — то есть проверка на таком значении молча ничего не проверяла
    # (найдено ревью, неразрешённая 534e16e4). uid 1 есть на обеих системах,
    # где это гоняется, и в группе каталога он не состоит.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=1 /usr/local/bin/wrap",
    )
    assert any("не состоит в группе" in r for r in local_reviewer.not_ready()), (
        "числовая форма --uid обязана проверяться так же, как именная: "
        "иначе один синтаксис обходит проверку целиком"
    )

    # Свой uid числом — доступ есть, претензий нет: проверка не должна
    # отказывать всем подряд, иначе она не проверка, а запрет.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        f"/usr/bin/systemd-run --scope --uid={os.getuid()} /usr/local/bin/wrap",
    )
    assert local_reviewer.not_ready() == [], "владелец каталога проходит по группе"

    # Пользователь, которого в системе нет: «не смогли проверить» — это тоже
    # причина, а не разрешение (неразрешённая 36a63d6b). Пустой not_ready()
    # означает «настроено», и вернуть его, ничего не проверив, значит
    # пообещать работу там, где ревьюер упрётся в EACCES.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=no-such-user-1180 /usr/local/bin/wrap",
    )
    assert any("не разрешается в системе" in r for r in local_reviewer.not_ready()), (
        "неудача проверки не имеет права читаться как «настроено»"
    )


async def test_the_chatty_reviewer_output_never_lands_in_memory(monkeypatch, tmp_path):
    """Неразрешённая e021e4bf: прежний тест был зелёным и для communicate().

    Валидатор прав: len(output) и dropped сходятся и после полного чтения в
    память, поэтому старая проверка не отличала чтение чанками от «прочитали
    всё и обрезали», а unit-тест на _collect этого пути не видел вовсе.

    Здесь измеряется то, ради чего правка и делалась: пиковая память САМОГО
    процесса хаба. communicate() собрал бы весь вывод одним объектом, чтение
    кусками держит в памяти только лимит.
    """
    import shlex
    import sys
    import tracemalloc

    monkeypatch.setattr(local_reviewer, "OUTPUT_CAP", 1000)
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, sys.executable)
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_CMD",
        shlex.join(
            [
                "-c",
                "import sys; sys.stdin.read(); sys.stdout.write('x' * 8_000_000)",
            ]
        ),
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    tracemalloc.start()
    try:
        run = await local_reviewer.run_review("промт", timeout=60)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert run is not None and not run.timed_out
    assert run.dropped > 7_000_000, "почти весь вывод обязан быть выброшен"
    assert peak < 2_000_000, (
        f"вывод ревьюера не имеет права оседать в памяти хаба целиком: пик "
        f"{peak} байт при выводе 8 МБ и лимите 1000"
    )


async def test_an_agent_created_behind_a_lost_answer_is_adopted(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1199 AC-1: агент подбирается, а второй НЕ покупается.

    Проверяется не только исход, но и число обращений к созданию: подбор
    обязан заменить второй POST, а не сопровождать его.
    """
    recorder = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, recorder)
    marker_seen: list[str] = []

    async def _listing(limit=50, cursor=""):
        # Имя берём из того же запроса, который «не ответил» — так его
        # увидел бы и провайдер.
        marker = recorder.calls[0]["name"]
        marker_seen.append(marker)
        return {"items": [{"id": "bc-adopted", "name": marker}], "nextCursor": ""}

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)

    task_id = await _submitted(client, db, "spike-adopt")

    assert len(recorder.calls) == 1, "второй POST — это второй оплаченный агент"
    assert marker_seen and marker_seen[0] == cursor_cloud.agent_marker(
        "review", task_id, 1
    )
    rows = await repo.list_active_review_dispatches(db)
    assert len(rows) == 1 and dict(rows[0])["agent_id"] == "bc-adopted", (
        "подобранный агент записан как исполнитель этого диспетча"
    )


async def test_the_adoption_is_named_not_silent(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1199: подбор виден человеку и надзору.

    Тихий подбор прячет оплаченный прогон ровно так же, как его прятал
    потерянный идентификатор.
    """
    recorder = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, recorder)

    async def _listing(limit=50, cursor=""):
        return {
            "items": [{"id": "bc-adopted", "name": recorder.calls[0]["name"]}],
            "nextCursor": "",
        }

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    task_id = await _submitted(client, db, "spike-adopt-visible")

    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='status'",
        (task_id,),
    )
    assert any("подобран по метке" in dict(r)["content"] for r in rows), (
        "карточка обязана назвать подбор"
    )
    events = await db.execute_fetchall(
        "SELECT payload FROM events WHERE task_id=? AND kind='review_dispatched'",
        (task_id,),
    )
    assert any(json.loads(dict(e)["payload"]).get("adopted") for e in events), (
        "надзор считает подборы по событию, а не по тексту карточки"
    )


async def test_a_real_refusal_is_neither_reconciled_nor_retried(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1199 AC-3: ответ БЫЛ и он отрицательный — спрашивать нечего.

    Сверка на настоящем отказе — лишний запрос, а повтор — лишние деньги
    на заведомо тот же исход.
    """
    recorder = _DispatchRecorder(None, refusal=_REAL_REFUSAL)
    _wire(monkeypatch, recorder)
    listing = _Listing([[{"id": "bc-x", "name": "чужой"}]])
    monkeypatch.setattr(cursor_cloud, "list_agents", listing)

    task_id = await _submitted(client, db, "spike-real-refusal")

    assert listing.calls == 0, "на настоящем отказе провайдера сверка не нужна"
    assert len(recorder.calls) == 1, "повтор на 400 покупает тот же отказ"
    assert not await repo.list_active_review_dispatches(db)
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    alerts = " ".join(dict(r)["content"] for r in rows)
    assert "провайдер отказал" in alerts and "HTTP 400" in alerts
    assert "не принял запрос (бета" not in alerts, (
        "прежний текст утверждал причину, которой не было"
    )


class _ParamsRefusingProvider:
    """Провайдер, отвергающий заказ с params 400-м, а голый — по сценарию."""

    def __init__(self, bare_refusal=None, code="invalid_argument"):
        self.bare_refusal = bare_refusal
        self.code = code
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("model_params"):
            return None, cursor_cloud.Refusal(
                status=400, code=self.code, detail="Unknown param fast"
            )
        if self.bare_refusal is not None:
            return None, self.bare_refusal
        return {"agent": {"id": "bc-bare"}, "run": {"id": "run-bare"}}, None


async def _alerts(db: aiosqlite.Connection, task_id: int) -> str:
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    return " ".join(dict(r)["content"] for r in rows)


async def test_rejected_model_params_fall_back_once_and_alert(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1417 AC-3: 400 на заказ с params — один повтор без них, и alert.

    Параметр fast=false может не поддерживаться моделью или называться
    иначе. Ревью от этого пропасть не должно: повтор без params проходит,
    причина названа в карточке, а оплачен ровно один прогон.
    """
    provider = _ParamsRefusingProvider()
    _wire(monkeypatch, _DispatchRecorder(None))
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", provider)
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL_PARAMS", "fast=false")

    task_id = await _submitted(client, db, "spike-params-refused")

    assert len(provider.calls) == 2
    assert provider.calls[0]["model_params"] == [{"id": "fast", "value": "false"}]
    assert not provider.calls[1]["model_params"]
    assert provider.calls[0]["name"] == provider.calls[1]["name"]
    active = await repo.list_active_review_dispatches(db)
    assert len(active) == 1, "заказано ровно один раз"
    assert dict(active[0])["agent_id"] == "bc-bare"
    assert dict(active[0])["model"] == "grok-4.6", "семейство (#758) — по id"
    alerts = await _alerts(db, task_id)
    assert "fast=false" in alerts and "HTTP 400" in alerts
    assert "invalid_argument" in alerts


def _recorded_catalog() -> dict:
    """Выдержка GET /v1/models, та же, что сверяет test_cursor_cloud (#1423)."""
    path = Path(__file__).parent / "fixtures" / "cursor_models_catalog.json"
    return json.loads(path.read_text())


async def test_invalid_model_on_params_falls_back_to_bare_order(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1423 AC-1: 400 invalid_model на заказ с params — один заказ без них.

    Ровно так Cursor ответил на ``[{fast:false}]`` для grok-4.6 25.09 (заказы
    449–457), а запасной путь #1417 invalid_model пропускал как «не про
    параметры» — и ревью встало для всех. Params здесь — из каталога, как
    на проде по умолчанию: даже верно подобранный вариант может оказаться
    отвергнут, и ревью от этого теряться не должно.
    """
    provider = _ParamsRefusingProvider(code="invalid_model")
    _wire(monkeypatch, _DispatchRecorder(None))
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", provider)
    catalog = _recorded_catalog()

    async def _catalog():
        return catalog

    monkeypatch.setattr(cursor_cloud, "list_models", _catalog)

    task_id = await _submitted(client, db, "spike-invalid-model-params")

    assert len(provider.calls) == 2, "ровно два запроса: с params и без"
    assert provider.calls[0]["model_params"] == [
        {"id": "effort", "value": "high"},
        {"id": "fast", "value": "false"},
    ]
    assert not provider.calls[1]["model_params"]
    active = await repo.list_active_review_dispatches(db)
    assert len(active) == 1
    assert dict(active[0])["agent_id"] == "bc-bare"
    alerts = await _alerts(db, task_id)
    assert "grok-4.6 effort=high,fast=false" in alerts
    assert "HTTP 400, invalid_model" in alerts
    assert "Кросс-модельное ревью НЕ вызвано" not in alerts


async def test_quota_refusal_with_params_is_not_retried(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1423 AC-2: лимит счёта — не про параметры, повтор купил бы тот же отказ."""
    provider = _ParamsRefusingProvider(code="usage_limit_exceeded")
    _wire(monkeypatch, _DispatchRecorder(None))
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", provider)
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL_PARAMS", "effort=high,fast=false")

    task_id = await _submitted(client, db, "spike-quota-params")

    assert len(provider.calls) == 1, "на лимит счёта повтора без params нет"
    assert not await repo.list_active_review_dispatches(db)
    alerts = await _alerts(db, task_id)
    assert "провайдер отказал: HTTP 400, usage_limit_exceeded" in alerts
    assert "Повтор без параметров" not in alerts


async def test_a_bare_retry_refused_too_is_not_blamed_on_params(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1417: если голый заказ тоже отвергнут — причина не в параметрах.

    Повтор ровно один, и alert о параметрах не пишется: он назвал бы чужую
    причину. Карточка получает обычный отказ провайдера.
    """
    provider = _ParamsRefusingProvider(bare_refusal=_REAL_REFUSAL)
    _wire(monkeypatch, _DispatchRecorder(None))
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", provider)
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL_PARAMS", "fast=false")

    task_id = await _submitted(client, db, "spike-params-bare-refused")

    assert len(provider.calls) == 2
    assert not await repo.list_active_review_dispatches(db)
    alerts = await _alerts(db, task_id)
    assert "провайдер отказал: HTTP 400, invalid_model" in alerts
    assert "fast=false" not in alerts


async def test_the_dispatched_notice_names_the_variant(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1417: запись «ревью вызвано» называет фактический вариант модели."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL_PARAMS", "fast=false")

    task_id = await _submitted(client, db, "spike-variant-named")

    assert recorder.calls[0]["model_params"] == [{"id": "fast", "value": "false"}]
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='status' "
        "AND content LIKE 'Кросс-модельное ревью вызвано%'",
        (task_id,),
    )
    assert rows
    assert "модель grok-4.6 fast=false " in dict(rows[-1])["content"]
    assert "Повтор без параметров" not in await _alerts(db, task_id)


async def test_an_unreadable_reconciliation_never_guesses(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1199 AC-4: «не смогли спросить» — это не «агента нет».

    Сверка сама оборвалась. Подобрать нельзя: любой похожий агент был бы
    угадан. Состояние называется человеку как есть — правило #762 в его
    исходной форме, отсутствие данных не есть значение.
    """
    recorder = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, recorder)
    listing = _Listing(None)  # провайдер не ответил и на список
    monkeypatch.setattr(cursor_cloud, "list_agents", listing)

    task_id = await _submitted(client, db, "spike-blind")

    assert listing.calls == 1, "спросить обязаны"
    assert not await repo.list_active_review_dispatches(db), (
        "неопознанный агент хуже пропущенного: записывать нечего"
    )
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    alerts = " ".join(dict(r)["content"] for r in rows)
    assert "ответ провайдера не дошёл" in alerts, "причина названа наблюдённая"
    assert "ReadTimeout" in alerts, "класс исключения доезжает до человека"


async def test_a_similar_agent_is_not_close_enough(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1199: сравнение имени на РАВЕНСТВО, а не на вхождение.

    Метка соседнего поколения отличается одним символом. Подобрать её
    значило бы отдать сдаче чужого судью — хуже, чем не подобрать никого.
    """
    recorder = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, recorder)

    async def _listing(limit=50, cursor=""):
        mine = recorder.calls[0]["name"]
        return {
            "items": [
                {"id": "bc-other-gen", "name": mine + "9"},
                {"id": "bc-prefix", "name": mine[:-1]},
            ],
            "nextCursor": "",
        }

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    task_id = await _submitted(client, db, "spike-similar")

    assert not await repo.list_active_review_dispatches(db)
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    assert any("не нашлось" in dict(r)["content"] for r in rows)


class _FlakyThenFine:
    """Первый вызов обрывается, второй проходит — самый частый исход."""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return None, _LOST_ANSWER
        return {"agent": {"id": "bc-second"}, "run": {"id": "run-2"}}, None


async def test_a_lost_answer_without_an_agent_is_retried_within_a_cap(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1199 AC-2: сверка сказала «агента нет» — повторяем, а не сдаёмся.

    Вердикт не уходит к человеку с первого же обрыва. Повтор безопасен
    ИМЕННО потому, что сверка прошла и показала пустоту: без неё он купил
    бы второго агента.
    """
    flaky = _FlakyThenFine()
    _wire(monkeypatch, flaky)
    listing = _Listing([[]])  # спросили, ответ пустой: агента нет
    monkeypatch.setattr(cursor_cloud, "list_agents", listing)

    task_id = await _submitted(client, db, "spike-retry")

    assert len(flaky.calls) == 2, "ровно один повтор после подтверждённой пустоты"
    assert listing.calls == 1, "спрашиваем перед повтором, а не после"
    assert flaky.calls[0]["name"] == flaky.calls[1]["name"], (
        "повтор идёт под ТОЙ ЖЕ меткой — иначе подобрать его потом нечем"
    )
    rows = await repo.list_active_review_dispatches(db)
    assert len(rows) == 1 and dict(rows[0])["agent_id"] == "bc-second"
    alerts = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    assert not any("НЕ вызвано" in dict(r)["content"] for r in alerts), (
        "успешный повтор не оставляет жалобы"
    )


async def test_the_retry_stops_at_the_cap(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1199: потолок держит. Иначе это семнадцать минут одной неудачи."""
    always_lost = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, always_lost)
    listing = _Listing([[]])
    monkeypatch.setattr(cursor_cloud, "list_agents", listing)

    task_id = await _submitted(client, db, "spike-cap")

    assert len(always_lost.calls) == 2, "две попытки, не больше"
    assert not await repo.list_active_review_dispatches(db)
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    assert any("не нашлось за попыток: 2" in dict(r)["content"] for r in rows)


# --- находки ревью №269 ---------------------------------------------------


async def test_a_top_up_does_not_adopt_the_first_reviewer(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#269 high: добор той же генерации — другой заказ, и метка это знает.

    Лестница (#879) заказывает второго ревьюера на ТУ ЖЕ генерацию. Метка
    без номера попытки совпала бы с первой, и сверка после оборвавшегося
    добора подобрала бы агента дешёвого прогона: второго POST нет, но
    судья чужой, а настоящий остался бы сиротой.
    """
    first = _DispatchRecorder({"agent": {"id": "bc-first"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, first)
    task_id = await _submitted(client, db, "spike-topup-marker")
    assert len(first.calls) == 1
    first_marker = first.calls[0]["name"]

    # Добор: POST обрывается, а в выдаче стоит агент ПЕРВОГО заказа.
    lost = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", lost)

    async def _listing(limit=50, cursor=""):
        return {
            "items": [{"id": "bc-first", "name": first_marker, "latestRunId": "run-1"}],
            "nextCursor": "",
        }

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    await maybe_dispatch_review(db, task_id, force_profile="deep")

    assert lost.calls, "добор вообще пробовал создать агента"
    assert lost.calls[0]["name"] != first_marker, (
        "метка добора обязана отличаться от метки первого заказа"
    )
    rows = await repo.list_active_review_dispatches(db)
    agents = {dict(r)["agent_id"] for r in rows}
    assert agents == {"bc-first"}, (
        "агент первого прогона не должен стать исполнителем добора"
    )


async def test_the_adopted_agent_brings_its_run_id(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#269: подбор без run_id — половина потери.

    Без него свип бьёт в /v1/agents/{id}/runs/ с пустым хвостом: не видит
    статус, не восстанавливает отчёт из текста прогона и не ставит расход.
    """
    recorder = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, recorder)

    async def _listing(limit=50, cursor=""):
        return {
            "items": [
                {
                    "id": "bc-adopted",
                    "name": recorder.calls[0]["name"],
                    "latestRunId": "run-adopted",
                }
            ],
            "nextCursor": "",
        }

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    await _submitted(client, db, "spike-adopt-runid")

    row = dict((await repo.list_active_review_dispatches(db))[0])
    assert row["agent_id"] == "bc-adopted"
    assert row["run_id"] == "run-adopted", "идентификатор прогона едет с агентом"


async def test_a_body_of_the_wrong_shape_is_not_an_empty_answer(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#269: чужая форма ответа — «не смог спросить», а не «агента нет».

    page.get('items') or [] на теле без items давал пустой обход и в конце
    подтверждённую пустоту, которая разрешает купить второго агента. Это
    ровно #762 в новом месте: отсутствие данных снова стало бы значением.
    """
    recorder = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, recorder)

    async def _listing(limit=50, cursor=""):
        return {"agents": [], "next": ""}  # схема беты изменилась

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    task_id = await _submitted(client, db, "spike-wrong-shape")

    assert len(recorder.calls) == 1, "на нечитаемой сверке второго POST быть не должно"
    assert not await repo.list_active_review_dispatches(db)
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    alerts = " ".join(dict(r)["content"] for r in rows)
    assert "СПРОСИТЬ" in alerts, "состояние названо как есть, а не как пустота"


# ---------------------------------------------------------------------------
# #1216 — тихий отказ по политике называет себя в карточке
#
# Измерено на #1202 (snip-portal, форж gitverse, gate_policy без ключа review):
# две сдачи подряд встали в review, и в карточке НЕТ НИ ОДНОЙ записи про
# диспетч ревью. Порядок проверок в maybe_dispatch_review отказывает по
# политике раньше, чем управление доходит до развилки по форжу, где стоит
# именованный отказ #1180. Второго читателя не будет ни при каких условиях, а
# карточка об этом молчит — и молчание читается как «ревью не потребовалось».
# ---------------------------------------------------------------------------


#: Собственный словарь диспетчера: чем он открывает вызов и чем — отказ.
#: Фильтровать по одному слову «ревью» нельзя: сдача пишет в карточку и своё
#: («ревью валидно и без PR»), и тогда счётчик считал бы чужие строки.
_DISPATCH_SAYS = (
    "ревью вызвано хабом",
    "ревью НЕ вызвано",
    "ревью запущено ЛОКАЛЬНО",
)


async def _dispatch_notices(db: aiosqlite.Connection, task_id: int) -> list[str]:
    """Всё, что карточка говорит о диспетче ревью: и вызовы, и отказы."""
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if any(phrase in dict(u)["content"] for phrase in _DISPATCH_SAYS)
    ]


async def test_a_submission_without_a_reviewer_says_so_on_the_card(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1216): политика не просит диспетча — карточка называет это.

    Политика snip-portal снята с прода 09.09.2026 как есть: dor и verdict
    человеку, ключа review нет вовсе. До правки этот путь не писал ничего, и
    задача стояла в review без второго читателя молча.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1216"}, "run": {"id": "r-1216"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(
        client,
        db,
        "spike-1216-silent",
        policy={"dor": "human", "verdict": "human"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )

    assert recorder.calls == [], "политика не просила диспетча — звать некого"
    notices = await _dispatch_notices(db, task_id)
    assert notices, (
        "карточка обязана сказать, что второго читателя не будет: тишина на "
        "этом месте читается как «ревью не потребовалось» (#1202)"
    )
    said = " ".join(notices)
    assert "политика проекта не просит" in said, (
        "причина называется по существу, а не общими словами: отказала именно "
        "политика проекта"
    )
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "review", "запись не должна ломать сдачу"


async def test_the_refusal_names_the_missing_settings(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1216): имена недостающих настроек берутся у review_reach.

    Второго источника правды не заводим: тот же текст человек видит в форме
    проекта и в отказе на записи политики (#1188). Сверяем ПОДСТРОКОЙ, а не
    похожестью — расхождение в одном слове означало бы вторую копию знания.
    """
    from hub.services.review_dispatch import review_reach

    recorder = _DispatchRecorder({"agent": {"id": "bc-r"}, "run": {"id": "r-r"}})
    _wire(monkeypatch, recorder)
    # Локальный путь не настроен — ровно состояние прода на 09.09.2026.
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", "")
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "")

    task_id = await _submitted(
        client,
        db,
        "spike-1216-names",
        policy={"dor": "human", "verdict": "human"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )

    said = " ".join(await _dispatch_notices(db, task_id))
    reach = await review_reach(db, "gitverse")
    assert not reach.runnable, "предпосылка теста: добыть ревью нечем"
    assert reach.reason in said, (
        "текст про форж и настройки обязан быть ТЕМ ЖЕ, что отдаёт "
        "review_reach: иначе три места объясняют одно состояние тремя словами"
    )
    assert "LOCAL_REVIEW_CMD" in said and "LOCAL_REVIEWER_HUB_TOKEN" in said, (
        "настройки названы по именам (#1083)"
    )
    assert "deploy/LOCAL-REVIEW.md" in said, "назван порядок включения"


async def test_the_notice_is_written_once_per_generation(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1216): свип проходит по сдаче снова — записи не прибавляется.

    Отказ по политике не меняет ни статуса, ни review_job_id, поэтому каждый
    следующий проход входит в ту же ветку. Без дедупа карточка засорялась бы
    одинаковыми алертами до самого вердикта.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-d"}, "run": {"id": "r-d"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(
        client,
        db,
        "spike-1216-dedup",
        policy={"dor": "human", "verdict": "human"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    first = len(await _dispatch_notices(db, task_id))
    assert first == 1, "первая сдача оставляет ровно одну запись"

    assert await maybe_dispatch_review(db, task_id) is False
    assert await maybe_dispatch_review(db, task_id) is False

    assert len(await _dispatch_notices(db, task_id)) == 1, (
        "повторные проходы по той же сдаче второй записи не создают"
    )


async def test_a_dispatched_project_gets_no_extra_notice(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-4 (#1216): на проекте с review=dispatch не изменилось ничего.

    Контроль к трём предыдущим: они доказывают появление записи, и без этого
    теста «запись появилась» было бы совместимо с «запись появляется всегда»,
    то есть с засорением карточек работающих проектов.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-ok"}, "run": {"id": "r-ok"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(
        client, db, "spike-1216-working", policy={"review": "dispatch"}
    )

    assert len(recorder.calls) == 1, "ревью вызывается как раньше"
    notices = await _dispatch_notices(db, task_id)
    assert len(notices) == 1 and "вызвано хабом" in notices[0], (
        "единственная запись про ревью — та самая, что была до правки: "
        "новой записи на рабочем пути не появилось"
    )

    # Второй тихий отказ той же функции — проверка новизны (#1152). Он
    # случается на проекте с ВКЛЮЧЁННЫМ диспетчем, то есть проходит ровно
    # через новый помощник, и накрыть его записью «политика не просит
    # диспетча» значило бы сказать неправду: политика как раз просила.
    # Первая редакция этого теста мутацию «снять условие, отделяющее тихий
    # путь от рабочего» ПЕРЕЖИЛА — до сюда она не доставала.
    review_id = await _report_on_current(db, task_id)
    await db.commit()
    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )

    assert len(recorder.calls) == 1, "второй прогон на том же коде не покупается"
    said = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "политика проекта не просит" not in said, (
        "отказала проверка новизны, а не политика: чужая причина в карточке "
        "хуже отсутствующей — по ней пойдут править gate_policy"
    )
    assert f"отчёт #{review_id}" in said, (
        "своя причина у этого отказа осталась на месте (#1152)"
    )
    assert len(await _dispatch_notices(db, task_id)) == 1, (
        "и записей про диспетч по-прежнему одна"
    )


async def test_an_environment_refusal_is_not_an_exhausted_profile(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1238): причина названа, и она отличается от исчерпания профиля.

    Проверяется не хранение ради хранения, а то, что человек прочитает в
    карточке: отказ среды говорит про настройку окружения, исчерпание
    профиля — нет. Мутация «считать любую неполноту отказом среды» роняет
    вторую половину теста, мутация «не различать причину вовсе» — первую.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-e1"}, "run": {"id": "run-e1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-env-refusal")

    report = {
        "harness_skill": "multi-agent-review",
        "raw_count": 0,
        "findings_confirmed": [],
        "findings_rejected": [],
        "incomplete": True,
        "incomplete_reason": "environment",
        "unresolved": [],
        "lost_dimensions": ["прогон тестов: в среде нет uv/pytest"],
        "agent": "cursor-cloud-reviewer",
        "model": "grok-4.6",
    }
    resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=report)
    assert resp.status_code == 200, resp.text

    stored = dict(await repo.get_latest_machine_review(db, task_id))
    assert stored["incomplete_reason"] == "environment"
    assert is_environment_refusal(stored) is True

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    refusal = [u for u in updates if "ОТКАЗУ СРЕДЫ" in u["content"]]
    assert len(refusal) == 1, [u["content"] for u in updates]
    text = refusal[0]["content"]
    # Карточка обязана сказать не только ЧТО, но и что с этим делать: второй
    # прогон в той же среде даст тот же отказ.
    assert "второй прогон" in text and "настройкой" in text
    assert "в среде нет uv/pytest" in text
    # Тот же вывод — в алерте лестницы (#879): её перечень исходов не
    # меняется, меняется только то, что человек в нём прочитает.
    assert ENVIRONMENT_REFUSAL_NOTE in _ladder_cause_note(stored)

    # Тот же отчёт, но с исчерпанием профиля, такой строки не порождает:
    # это ровно тот случай, который лечит добор.
    other_id = await _submitted(client, db, "spike-profile-exhausted")
    report["incomplete_reason"] = "profile"
    report["lost_dimensions"] = ["20 файлов не дочитаны"]
    resp = await client.post(f"/api/tasks/{other_id}/machine-review", json=report)
    assert resp.status_code == 200, resp.text
    other_stored = dict(await repo.get_latest_machine_review(db, other_id))
    assert other_stored["incomplete_reason"] == "profile"
    assert is_environment_refusal(other_stored) is False
    other_updates = [dict(u) for u in await repo.get_task_updates(db, other_id)]
    assert not [u for u in other_updates if "ОТКАЗУ СРЕДЫ" in u["content"]]
    assert _ladder_cause_note(other_stored) == ""

    # И причина, которую хаб не знает, не становится отказом среды по
    # похожести слов: «нет pytest» — это проза, а поле заведено ровно затем,
    # чтобы прозу не разбирать. Отчёт при этом не теряется — только причина
    # остаётся незаявленной.
    third_id = await _submitted(client, db, "spike-unknown-reason")
    report["incomplete_reason"] = "в среде нет pytest"
    resp = await client.post(f"/api/tasks/{third_id}/machine-review", json=report)
    assert resp.status_code == 200, resp.text
    third_stored = dict(await repo.get_latest_machine_review(db, third_id))
    assert third_stored["incomplete_reason"] == ""
    assert is_environment_refusal(third_stored) is False
    third_updates = [dict(u) for u in await repo.get_task_updates(db, third_id)]
    assert not [u for u in third_updates if "ОТКАЗУ СРЕДЫ" in u["content"]]


async def test_environment_refusals_are_counted_with_their_sample_size(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1238): счёт повторяемости, и рядом — размер выборки.

    Два случая за 09.09.2026 заметил человек, читавший карточки подряд.
    Механизма, который считает такие отчёты, не было вовсе; здесь он есть, и
    он не имеет права печатать долю, не сказав, из чего она взята.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-e2"}, "run": {"id": "run-e2"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-refusal-count")

    # Пока причину не назвал никто, доли нет — есть слово «недобор».
    await _seed_report(db, task_id, incomplete=True, reason=None)
    await _seed_report(db, task_id, incomplete=False, reason=None)
    empty = await count_environment_refusals(db, since_days=30)
    assert empty["environment_refusals"] == 0
    assert empty["reason_declared"] == 0
    assert empty["environment_share"] is None
    assert "недобор" in empty["share_note"]

    await _seed_report(db, task_id, incomplete=True, reason="environment")
    await _seed_report(db, task_id, incomplete=True, reason="environment")
    await _seed_report(db, task_id, incomplete=True, reason="profile")
    counted = await count_environment_refusals(db, since_days=30)

    assert counted["environment_refusals"] == 2
    assert counted["profile_exhausted"] == 1
    assert counted["reason_declared"] == 3
    assert counted["reason_unstated"] == 1
    assert counted["incomplete_total"] == 4
    assert counted["reports_total"] == 5
    assert counted["environment_share"] == round(2 / 3, 3)
    # Размер выборки идёт вместе с числом, а не отдельной строкой ниже
    # (#1153): «2» без «из 3 назвавших причину» решения не выдерживает.
    note = counted["share_note"]
    assert "2 из 3" in note
    assert "4" in note and "5" in note
    assert "недобор" not in note


async def test_reports_without_the_field_are_not_counted_as_refusals(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1238): старые отчёты читаются и НЕ становятся отказом среды.

    МУТАЦИЯ, которую обязан ловить этот тест: трактовать пустую причину как
    отказ среды. Тогда каждый неполный отчёт, написанный до появления поля,
    задним числом получил бы показание, которого никто не давал, — та же
    подмена «не измерено» на «измерено», ради которой всё это заведено.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-e3"}, "run": {"id": "run-e3"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-old-reports")

    for _ in range(3):
        await _seed_report(db, task_id, incomplete=True, reason=None)

    stats = await count_environment_refusals(db, since_days=30)
    assert stats["incomplete_total"] == 3
    assert stats["environment_refusals"] == 0
    assert stats["profile_exhausted"] == 0
    assert stats["reason_unstated"] == 3
    assert stats["environment_share"] is None
    assert "недобор" in stats["share_note"]

    # Чтение таких строк не ломается: причина пустая, а не отсутствующая.
    row = dict(await repo.get_latest_machine_review(db, task_id))
    assert row["incomplete_reason"] == ""
    assert is_environment_refusal(row) is False
    view = MachineReviewView(**row)
    assert view.incomplete_reason == ""
    assert view.incomplete is True


async def test_the_mcp_path_carries_the_environment_refusal_to_storage(
    client: AsyncClient, db: aiosqlite.Connection
):
    """#1238 (ревью, P2): отчёт, сданный через MCP, доносит причину.

    До этой правки аргумента не было вовсе: вызов проходил успешно, лишний
    аргумент молча отбрасывался, и в строке оставалось умолчание — «причина
    не заявлена». Отказ среды, приехавший этим путём, выпадал и из алерта, и
    из счётчика, ради которых поле заведено.

    Проверяется ОПУБЛИКОВАННЫМ входом — ``mcp.call_tool`` поверх настоящего
    приёма, — потому что питонова функция приняла бы аргумент и тогда, когда
    в схеме его нет: вызов .fn ничего не доказывает.
    """
    from hub.mcp_server import mcp

    task_id = await _submitted(client, db, "spike-mcp-refusal")

    async def _post(path: str, body: dict | None = None, **_kw):
        resp = await client.post(path, json=body or {})
        resp.raise_for_status()
        return resp.json()

    with patch("hub.mcp_server._api_post", side_effect=_post):
        await mcp.call_tool(
            "hub_submit_machine_review",
            {
                "task_id": task_id,
                "raw_count": 0,
                "incomplete": True,
                "incomplete_reason": "environment",
                "lost_dimensions": ["прогон тестов: в среде нет uv/pytest"],
                "agent": "cursor-cloud-reviewer",
                "model": "grok-4.6",
            },
        )

    stored = dict(await repo.get_latest_machine_review(db, task_id))
    assert stored["incomplete_reason"] == "environment"
    assert is_environment_refusal(stored) is True

    # И карточка получает тот же алерт, что и на HTTP-пути: паритет — это про
    # поведение, а не про то, что аргумент принят.
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert [u for u in updates if "ОТКАЗУ СРЕДЫ" in u["content"]]


async def test_a_report_sent_through_mcp_without_a_reason_is_not_a_refusal(
    client: AsyncClient, db: aiosqlite.Connection
):
    """AC-3 на MCP-пути (#1238): умолчание аргумента — «не заявлена».

    У опубликованного аргумента есть ДЕФОЛТ, и это отдельное место, где
    можно ошибиться: дефолт `environment` записал бы отказом среды каждый
    неполный отчёт, никем этого не заявивший, — ровно ту подмену, против
    которой поле и заведено. Сидящие в базе отчёты старого образца такую
    мутацию не ловят: они приходят мимо инструмента.
    """
    from hub.mcp_server import mcp

    task_id = await _submitted(client, db, "spike-mcp-no-reason")

    async def _post(path: str, body: dict | None = None, **_kw):
        resp = await client.post(path, json=body or {})
        resp.raise_for_status()
        return resp.json()

    with patch("hub.mcp_server._api_post", side_effect=_post):
        await mcp.call_tool(
            "hub_submit_machine_review",
            {
                "task_id": task_id,
                "raw_count": 0,
                "incomplete": True,
                "lost_dimensions": ["20 файлов не дочитаны"],
                "agent": "cursor-cloud-reviewer",
                "model": "grok-4.6",
            },
        )

    stored = dict(await repo.get_latest_machine_review(db, task_id))
    assert stored["incomplete_reason"] == ""
    assert is_environment_refusal(stored) is False
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert not [u for u in updates if "ОТКАЗУ СРЕДЫ" in u["content"]]
