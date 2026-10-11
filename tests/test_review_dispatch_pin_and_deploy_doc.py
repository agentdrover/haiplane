"""Hub-dispatched cross-model reviews (#757): pin_and_deploy_doc (split of the former single file, #1670)."""

from __future__ import annotations

import ast
import json
import os
import re
import stat
from pathlib import Path

import aiosqlite
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub.integrations import cursor_cloud, local_reviewer
from hub.services.review_dispatch import (
    sweep_review_dispatches,
)
from tests.review_dispatch_support import (
    _DEPLOY_DOC,
    _ENV_EXAMPLE,
    _HUB_ROOT,
    _LOCAL_REPORT,
    _LOST_ANSWER,
    _DispatchRecorder,
    _expire_grace,
    _finished_run_with,
    _Listing,
    _local_principal,
    _pinned_setup,
    _report_block,
    _stub_reviewer,
    _submitted,
    _wire,
)


# --- Закрепление сдачи на отчёте, восстановленном из текста (#1260) ----------
#
# record_machine_review умеет отвергать отчёт, выписанный на другое поколение
# (#1084), но путь восстановления из текста прогона закрепления не передавал:
# прогон судил сдачу №1, работу пересдали, и отчёт ложился на №2 — отчёт о
# прежнем диффе читался как отчёт о новом. _store_report одна на облачный и
# локальный каналы, поэтому оба проверяются поимённо.


async def _cloud_report_on_moved_submission(
    client: AsyncClient, db: aiosqlite.Connection, slug: str
) -> int:
    """Облачный заказ на поколении 1; пока прогон шёл, работу пересдали."""
    task_id = await _submitted(
        client, db, slug, policy={"review": "dispatch", "local_review_fallback": "on"}
    )
    await db.execute(
        "UPDATE tasks SET submission_generation = 2 WHERE id = ?", (task_id,)
    )
    await db.commit()
    return task_id


def _gated_reporting_stub(marker) -> str:
    """Локальная заглушка, которая сдаёт отчёт только после сигнала теста.

    Сигнал — файл: тест пересдаёт работу ПОКА прогон идёт, и лишь потом
    отпускает его. Без шлюза исход зависел бы от того, кто успеет первым.
    """
    payload = json.dumps(_LOCAL_REPORT, ensure_ascii=False)
    return (
        "import os, sys, time\n"
        "sys.stdin.read()\n"
        "for _ in range(400):\n"
        f"    if os.path.exists({str(marker)!r}):\n"
        "        break\n"
        "    time.sleep(0.05)\n"
        "print('```haiplane-review')\n"
        f"print({payload!r})\n"
        "print('```')\n"
    )


async def _local_report_on_moved_submission(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch,
    tmp_path,
    slug: str,
    *,
    move: bool = True,
) -> int:
    from hub.services.review_dispatch import wait_for_local_runs

    marker = tmp_path / f"{slug}.go"
    _stub_reviewer(monkeypatch, tmp_path, _gated_reporting_stub(marker))
    task_id = await _submitted(
        client,
        db,
        slug,
        policy={"review": "dispatch", "local_review_fallback": "on"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    if move:
        await db.execute(
            "UPDATE tasks SET submission_generation = 2 WHERE id = ?", (task_id,)
        )
        await db.commit()
    marker.write_text("go")
    await wait_for_local_runs()
    await db.commit()
    return task_id


async def _reports_by_generation(db: aiosqlite.Connection, task_id: int) -> list[int]:
    return [
        len(await repo.machine_reviews_of_generation(db, task_id, generation))
        for generation in (1, 2)
    ]


def _moved_refusals(updates: list[dict]) -> list[str]:
    return [
        u["content"]
        for u in updates
        if u["kind"] == "alert"
        and "выписан на сдачу #1" in u["content"]
        and "текущая — #2" in u["content"]
    ]


async def test_a_report_from_a_superseded_run_is_not_stamped_on_the_new_submission(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#1260): проверяется СОСТОЯНИЕМ — строк отчётов на обоих
    # поколениях столько же, сколько было до свипа. И свип после отказа
    # продолжает работу: соседний заказ того же прохода записывается.
    recorder = _DispatchRecorder({"agent": {"id": "bc-mv"}, "run": {"id": "run-mv"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    moved = await _cloud_report_on_moved_submission(client, db, "spike-moved")
    steady = await _submitted(
        client,
        db,
        "spike-steady",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await _expire_grace(db)
    await _finished_run_with(monkeypatch, _report_block())
    before = await _reports_by_generation(db, moved)
    dispatches_before = len(
        await db.execute_fetchall("SELECT id FROM review_dispatches")
    )

    await sweep_review_dispatches(db)

    assert await _reports_by_generation(db, moved) == before == [0, 0], (
        "отчёт о прежнем диффе не ложится ни на прежнюю, ни на новую сдачу"
    )
    assert len(await repo.machine_reviews_of_generation(db, steady, 1)) == 1, (
        "свип обязан пережить отказ и записать соседний отчёт"
    )
    rows = await db.execute_fetchall(
        "SELECT status FROM review_dispatches WHERE task_id = ?", (moved,)
    )
    assert [dict(r)["status"] for r in rows] == ["failed"], (
        "строка прогона закрыта, а не висит активной"
    )
    assert (
        len(await db.execute_fetchall("SELECT id FROM review_dispatches"))
        == dispatches_before
    ), "за прогон по уехавшей сдаче вторая дверь не покупается"


async def test_the_card_names_why_the_recovered_report_was_refused(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#1260): отказ виден в карточке названной причиной. Молчание — и
    # ложное «отчёт НЕ сдан» — критерий не закрывают: отчёт был, но о другом.
    recorder = _DispatchRecorder({"agent": {"id": "bc-nm"}, "run": {"id": "run-nm"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    task_id = await _cloud_report_on_moved_submission(client, db, "spike-named")
    await _expire_grace(db)
    await _finished_run_with(monkeypatch, _report_block())

    await sweep_review_dispatches(db)

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert len(_moved_refusals(updates)) == 1, (
        f"причина отказа обязана быть в карточке ровно раз: {updates}"
    )
    assert not [u for u in updates if "отчёт НЕ сдан" in u["content"]], (
        "отчёт был сдан — о прежней сдаче; «не сдан» подменило бы причину"
    )
    assert not [u for u in updates if "восстановлен из текста" in u["content"]]


async def test_both_report_paths_carry_the_submission_pin(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    # AC-3 (#1260): два зовущих _store_report поимённо — облачное
    # восстановление из текста рана и локальный отчёт из вывода прогона.
    # Один потребитель правила не доказывает второго.
    recorder = _DispatchRecorder({"agent": {"id": "bc-bt"}, "run": {"id": "run-bt"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    await _local_principal(db, monkeypatch)

    cloud = await _cloud_report_on_moved_submission(client, db, "spike-pin-cloud")
    await _expire_grace(db)
    await _finished_run_with(monkeypatch, _report_block())
    await sweep_review_dispatches(db)

    local = await _local_report_on_moved_submission(
        client, db, monkeypatch, tmp_path, "spike-pin-local"
    )

    for name, task_id in (("облачный", cloud), ("локальный", local)):
        assert await _reports_by_generation(db, task_id) == [0, 0], (
            f"{name} путь записал отчёт о прежней сдаче"
        )
        updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
        assert len(_moved_refusals(updates)) == 1, (
            f"{name} путь не назвал отказ в карточке: {updates}"
        )
        assert not [
            u
            for u in updates
            if "отчёт НЕ сдан" in u["content"]
            or "завершилось без отчёта" in u["content"]
        ], f"{name} путь выдал отказ по поколению за отсутствие отчёта"
        rows = await db.execute_fetchall(
            "SELECT status FROM review_dispatches WHERE task_id = ?", (task_id,)
        )
        assert [dict(r)["status"] for r in rows] == ["failed"], name
    local_updates = [dict(u) for u in await repo.get_task_updates(db, local)]
    assert not [u for u in local_updates if "восстановлен из вывода" in u["content"]], (
        "локальный путь не выдаёт отказ за восстановление"
    )


async def test_a_matching_generation_is_recorded_on_both_channels(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    # AC-4 (#1260): совпавшее поколение ложится как раньше — с владельцем и
    # происхождением. Сторож, отказывающий всегда, неотличим от работающего.
    from hub.services.machine_review_intake import ORIGIN_LOCAL_TEXT, ORIGIN_RUN_TEXT

    recorder = _DispatchRecorder({"agent": {"id": "bc-ok"}, "run": {"id": "run-ok"}})
    _wire(monkeypatch, recorder)
    cloud_pid, _, _ = await _pinned_setup(db, monkeypatch)
    local_pid = await _local_principal(db, monkeypatch)

    cloud = await _submitted(
        client,
        db,
        "spike-same-cloud",
        policy={"review": "dispatch", "local_review_fallback": "on"},
    )
    await _expire_grace(db)
    await _finished_run_with(monkeypatch, _report_block())
    await sweep_review_dispatches(db)

    local = await _local_report_on_moved_submission(
        client, db, monkeypatch, tmp_path, "spike-same-local", move=False
    )

    for task_id, owner, origin in (
        (cloud, cloud_pid, ORIGIN_RUN_TEXT),
        (local, local_pid, ORIGIN_LOCAL_TEXT),
    ):
        rows = [
            dict(r) for r in await repo.machine_reviews_of_generation(db, task_id, 1)
        ]
        assert len(rows) == 1, f"{origin}: отчёт совпавшего поколения не записан"
        assert rows[0]["principal_id"] == owner, origin
        assert rows[0]["orchestrator"].startswith(origin), rows[0]["orchestrator"]
        updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
        assert not _moved_refusals(updates), origin
        status = await db.execute_fetchall(
            "SELECT status FROM review_dispatches WHERE task_id = ?", (task_id,)
        )
        assert [dict(r)["status"] for r in status] == ["done"], origin


# Названные исключения из правила «вызывающий record_machine_review передаёт
# закрепление». Ключ — (файл относительно корня, функция-владелец вызова),
# значение — причина. Пусто сознательно: сегодня исключений нет, и новое
# обязано прийти сюда с объяснением, а не проскочить молча.
_INTAKE_PIN_EXCEPTIONS: dict[tuple[str, str], str] = {}


def _intake_callers() -> list[tuple[str, str, bool]]:
    """Все вызовы record_machine_review в hub/, собранные разбором AST.

    Функция-владелец — ближайшая объемлющая def: оба сегодняшних вызова
    импортируют функцию ЛОКАЛЬНО, из тела, и сторож по импортам верхнего
    уровня их бы не увидел. Поэтому ищутся сами вызовы, а не импорты.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    found: list[tuple[str, str, bool]] = []

    def _walk(node: ast.AST, owner: str, rel: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _walk(child, child.name, rel)
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else ""
                )
                if name == "record_machine_review":
                    pinned = any(k.arg == "expected_generation" for k in child.keywords)
                    found.append((rel, owner, pinned))
            _walk(child, owner, rel)

    for path in sorted((root / "hub").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        _walk(ast.parse(path.read_text(encoding="utf-8")), "<module>", rel)
    return found


def test_every_intake_caller_pins_the_submission_or_is_a_named_exception():
    # AC-5 (#1260): вызывающие перечисляются разбором исходника, а не списком
    # из головы. Новый вызывающий роняет тест, а не стареет молча.
    callers = _intake_callers()
    owners = {(rel, owner) for rel, owner, _ in callers}
    assert ("hub/services/review_dispatch.py", "_store_report") in owners, (
        f"сторож не видит вызова из _store_report — он слеп: {callers}"
    )
    assert any(rel == "hub/app.py" for rel, _ in owners), (
        f"сторож не видит приёма по контракту в hub/app.py: {callers}"
    )
    unpinned = [
        (rel, owner)
        for rel, owner, pinned in callers
        if not pinned and (rel, owner) not in _INTAKE_PIN_EXCEPTIONS
    ]
    assert not unpinned, (
        "вызывающий record_machine_review без expected_generation и без "
        f"названной причины: {unpinned}"
    )
    stale = set(_INTAKE_PIN_EXCEPTIONS) - owners
    assert not stale, f"исключение названо для вызова, которого больше нет: {stale}"


# --- #1206: список без поля name -----------------------------------------


async def test_a_listing_without_names_stops_instead_of_buying_a_second_agent(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1206: потребитель читает «поля нет» как blind, а не как пустоту.

    Если провайдер перестанет возвращать метку, сверка не найдёт агента
    никогда. Прежде это доезжало до _create_or_adopt как подтверждённая
    пустота и разрешало повторный POST — то есть механизм против двойной
    покупки сам покупал бы второго на каждом обрыве.
    """
    recorder = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, recorder)
    # Агент у провайдера ЕСТЬ и он наш — просто имени в ответе нет.
    listing = _Listing([[{"id": "bc-paid", "latestRunId": "run-paid"}]])
    monkeypatch.setattr(cursor_cloud, "list_agents", listing)

    task_id = await _submitted(client, db, "spike-nameless")

    assert listing.calls == 1, "спросить обязаны"
    assert len(recorder.calls) == 1, (
        "второй POST — это второй оплаченный агент поверх уже созданного"
    )
    assert not await repo.list_active_review_dispatches(db), (
        "подобрать нечем: любой агент из такого ответа был бы угадан"
    )
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    alerts = " ".join(dict(r)["content"] for r in rows)
    assert "СПРОСИТЬ" in alerts, "состояние названо как есть, а не как пустота"


# --- #1206: имена есть, но ни одно не наше --------------------------------


async def test_foreign_names_stop_instead_of_buying_a_second_agent(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1206: наблюдённая форма — имя ЕСТЬ, просто чужое.

    06.09 у осиротевших агентов поле name было заполнено автоименем из
    промта. На такой странице страж «ключа name нет ни у кого» молчит,
    сверка на равенство не находит метку — и до _create_or_adopt приезжает
    подтверждённая пустота, разрешающая купить второго оплаченного агента
    поверх уже созданного. Потребитель обязан увидеть blind.
    """
    recorder = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, recorder)
    # Агент у провайдера ЕСТЬ и он наш — но имя ему придумал провайдер.
    listing = _Listing(
        [
            [
                {"id": "bc-paid", "name": "Код-ревью задачи haiplane"},
                {"id": "bc-other", "name": "Суждение стюарда гейта"},
            ]
        ]
    )
    monkeypatch.setattr(cursor_cloud, "list_agents", listing)

    task_id = await _submitted(client, db, "spike-foreign-names")

    assert listing.calls == 1, "спросить обязаны"
    assert len(recorder.calls) == 1, (
        "второй POST — это второй оплаченный агент поверх уже созданного"
    )
    assert not await repo.list_active_review_dispatches(db), (
        "подобрать нечем: любой агент из такого ответа был бы угадан"
    )
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    alerts = " ".join(dict(r)["content"] for r in rows)
    assert "СПРОСИТЬ" in alerts, "состояние названо как есть, а не как пустота"


_CONFIG_SOURCE = _HUB_ROOT / "hub/config.py"


# Строка вида ``Environment=ИМЯ=…``, ``# ИМЯ=…`` или просто ``ИМЯ=…`` — то, что
# оператор КОПИРУЕТ к себе. Именно она и разошлась с кодом.
_ASSIGNED = re.compile(r"^\s*(?:#\s*)?(?:Environment=)?([A-Z][A-Z0-9_]*)=")
# Имя настройки локального ревьюера БЕЗ префикса: \b не срабатывает внутри
# HAIPLANE_LOCAL_REVIEW…, поэтому лишний lookbehind не нужен.
_BARE_NAME = re.compile(r"\bLOCAL_REVIEW[A-Z0-9_]*")


def _suffixes_read_by_config() -> set[str]:
    """Суффиксы env_get(...) из ИСХОДНИКА hub/config.py, а не из списка в тесте.

    Список имён, переписанный в тест, — третье описание тех же имён, и оно
    разойдётся следующим ровно так же, как разошёлся документ.
    """
    tree = ast.parse(_CONFIG_SOURCE.read_text())
    found = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "env_get" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            found.add(arg.value)
    return {name for name in found if name.startswith("LOCAL_REVIEW")}


def _names_assigned_in(path: Path) -> set[str]:
    return {
        m.group(1)
        for line in path.read_text().splitlines()
        if (m := _ASSIGNED.match(line)) and "LOCAL_REVIEW" in m.group(1)
    }


def test_the_deploy_doc_names_the_settings_the_code_reads() -> None:
    """AC-1: имена в документе сверяются С ИСХОДНИКОМ машиной, а не глазами.

    Расхождение уже случилось один раз и было незаметным ровно потому, что
    глазами оно не ловится: HAIPLANE_HUB_HOST двумя строками выше в том же
    файле выглядит так же убедительно, как LOCAL_REVIEW_CMD без префикса.
    """
    read_by_code = _suffixes_read_by_config()
    assert read_by_code, (
        "разбор hub/config.py не нашёл ни одного env_get с именем "
        "LOCAL_REVIEW* — сверка была бы пустой и зелёной на любом документе"
    )
    expected = {config.brand.ENV_PREFIX + suffix for suffix in read_by_code}

    for path in (_DEPLOY_DOC, _ENV_EXAMPLE):
        named = _names_assigned_in(path)
        assert named == expected, (
            f"{path.name} называет настройки локального ревьюера как "
            f"{sorted(named)}, а код читает {sorted(expected)}. Разница в "
            "префиксе не ломает запуск, а МОЛЧА выключает локальный путь: в "
            "карточке встанет «локальный не настроен» при верной во всём "
            "остальном конфигурации (найдено выкатом 08.09.2026, #1208)"
        )

    # Ни одного упоминания без префикса — включая прозу: оператор, который
    # грепает по имени из текста, обязан найти то же самое имя.
    for path in (_DEPLOY_DOC, _ENV_EXAMPLE):
        bare = _BARE_NAME.findall(path.read_text())
        assert not bare, (
            f"{path.name} упоминает {sorted(set(bare))} без префикса "
            f"{config.brand.ENV_PREFIX} — а config.env_get читает только с ним"
        )


# ПЕРЕСМОТР 10.09.2026: закрытый набор форм вместо перечисления флагов.
#
# Тесты ниже НЕ выброшены и не ослаблены. Каждый из них кодирует настоящий
# вход, на котором страж когда-то ошибался, и все девять кругов ошибка шла в
# одну сторону — ложного ПРОПУСКА. Новое правило отвергает эти входы тем
# более, поэтому у большинства тестов входы остались прежними, а изменился
# ПРИГОВОР: там, где прежде проверялось «отвергнут по такому-то флагу»,
# теперь проверяется «отвергнут, и причина названа». Каждый такой поворот
# назван в докстроке своего теста поимённо — молча не перевёрнут ни один.


def test_every_blessed_sandbox_shape_still_passes(monkeypatch) -> None:
    """AC-6: рабочая строка прода и обе формы набора проходят стража.

    Строгое правило, отвергающее рабочий выкат, хуже прежнего мягкого,
    поэтому эта половина проверяется ТЕМ ЖЕ набором тестов, что и AC-5: иначе
    строгость чинилась бы ценой поломки, и поломка вскрылась бы на проде.

    Строки берутся из ``SANDBOX_SHAPES``, а не переписываются сюда: список,
    переписанный в тест, — третье описание тех же форм, и оно разойдётся
    следующим ровно так же, как пять раз расходился документ.
    """
    prod = "/usr/bin/sudo -n -u haiplane-reviewer /usr/local/bin/haiplane-review-run"
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", prod)
    assert local_reviewer.sandbox_problem() == [], (
        f"рабочая строка прода отвергнута: {local_reviewer.sandbox_problem()}"
    )
    assert local_reviewer.sandbox_uid() == "haiplane-reviewer"
    assert local_reviewer.sandbox_shape() == "sudo"

    for shape in local_reviewer.SANDBOX_SHAPES:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", shape.example)
        assert local_reviewer.sandbox_problem() == [], (
            f"форма «{shape.name}» отвергает собственный пример "
            f"«{shape.example}»: {local_reviewer.sandbox_problem()}"
        )
        assert local_reviewer.sandbox_shape() == shape.name
        assert local_reviewer.sandbox_uid() == "haiplane-reviewer", (
            f"в примере формы «{shape.name}» страж не видит пользователя — "
            "значит проверка его членства в группе каталога прогонов на этой "
            "конфигурации молча не сработает вовсе, ровно как было до #1208"
        )

    # Необязательные флаги формы — тоже часть обещания: названный в документе
    # флаг обязан проходить, иначе документ рекомендует то, что хаб отвергнет.
    for sandbox in (
        "/usr/bin/systemd-run --scope --quiet --uid=haiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/systemd-run --scope --uid=haiplane-reviewer --slice=review.slice "
        "--property=MemoryMax=1500M --property=CPUQuota=150% /usr/local/bin/wrap",
        "/usr/bin/systemd-run --scope --uid haiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -n --user haiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -n --user=haiplane-reviewer /usr/local/bin/wrap",
        # Имя инструмента без пути: PATH прогону собирает сам хаб.
        "sudo -n -u haiplane-reviewer /usr/local/bin/wrap",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        assert local_reviewer.sandbox_problem() == [], (
            f"«{sandbox}» состоит из флагов, названных в наборе, и отвергаться "
            f"не должна: {local_reviewer.sandbox_problem()}"
        )
        assert local_reviewer.sandbox_uid() == "haiplane-reviewer"

    # Повтор НАКАПЛИВАЮЩЕГОСЯ флага — не тот повтор, что перекрывает: две
    # --property у systemd-run складываются, и запрещать их значило бы
    # отвергать строку с двумя лимитами, то есть рабочий рецепт.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=r --setenv=A=1 --setenv=B=2 /usr/local/bin/wrap",
    )
    assert local_reviewer.sandbox_problem() == [], (
        f"--setenv накапливается, а не перекрывает: {local_reviewer.sandbox_problem()}"
    )


def test_a_sandbox_outside_the_closed_set_is_refused(monkeypatch) -> None:
    """AC-5: строка вне закрытого набора отвергается с названной причиной.

    Здесь собраны входы, каждый из которых КОГДА-ТО ПРОХОДИЛ стража, а
    обязан был быть отвергнут, — и входы, которые страж отвергал по частному
    флагу, а теперь отвергает по правилу. Ни один не выброшен: это
    накопленное за девять кругов доказательство, и оно переживает смену
    правила.

    Проверяется ДВА следствия разом. Первое: отказ есть и он назван. Второе:
    ``sandbox_uid()`` на такой строке пуст — раньше страж отвечал «кто» и про
    строку, которой не понял, и именно в этих ответах ошибался (имя из
    аргументов полезной нагрузки, из значения соседнего флага, из первого
    вхождения повторённого флага).
    """
    outside = [
        # Инструмент, о котором набор не знает вовсе. Прежнее ограничение
        # «чего не знаем — пропускаем» отменено решением владельца 10.09.2026.
        "/usr/bin/env",
        "/usr/bin/env -u HOME /usr/local/bin/wrap",
        "/bin/sh -c /usr/local/bin/wrap",
        # Относительное имя со слэшем: «какая-то программа», а не названная.
        "./sudo -n -u haiplane-reviewer /usr/local/bin/wrap",
        # Контейнерные движки — целиком, любой подкомандой и с любым сроком.
        "/usr/bin/podman run --rm -i img",
        "/usr/bin/podman run --rm -i --timeout 1800 img",
        "/usr/bin/podman run --rm -i --timeout=1800 img",
        "/usr/bin/podman ps",
        "/usr/bin/docker run --rm -i img",
        "sudo -n -u haiplane-reviewer podman run --timeout 60 img",
        # Слипшийся короткий токен во всех записях, что находили читатели.
        "/usr/bin/sudo -nu haiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -nuhaiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -uhaiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -niu haiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -pu haiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -n -u=haiplane-reviewer /usr/local/bin/wrap",
        # Повтор перекрывающего флага: инструмент возьмёт последнее вхождение,
        # читатель глазами — первое, и на этой разнице страж уже ошибался.
        "/usr/bin/sudo -n -u alice -u bob /usr/local/bin/wrap",
        "/usr/bin/sudo -n --user alice --user bob /usr/local/bin/wrap",
        "/usr/bin/sudo -n --user=alice -u bob /usr/local/bin/wrap",
        "/usr/bin/systemd-run --scope --uid=alice --uid=bob /usr/local/bin/wrap",
        # Флаг, которого в форме нет.
        "/usr/bin/sudo -n -u haiplane-reviewer -g haiplane /usr/local/bin/wrap",
        "/usr/bin/systemd-run --scope --pipe --uid=r /usr/local/bin/wrap",
        # Терминатор и аргументы полезной нагрузки в самой песочнице.
        "/usr/bin/systemd-run --scope --uid=alice -- /usr/local/bin/wrap",
        "/usr/bin/sudo -n -u alice -- -u bob",
        "/usr/bin/systemd-run --scope --uid=alice /wrap --uid=bob",
        "/usr/bin/sudo -n -u haiplane-reviewer /usr/bin/env -u HOME /wrap",
        # Пути к обёртке нет вовсе либо он относительный.
        "/usr/bin/sudo -n -u haiplane-reviewer",
        "/usr/bin/sudo -n -u haiplane-reviewer wrap",
        # Обязательного флага нет: без -n sudo может спросить пароль, а stdin
        # занят промтом с одноразовым кодом доступа к хабу.
        "/usr/bin/sudo -u 1234 /usr/local/bin/wrap",
        "/usr/bin/sudo /usr/local/bin/wrap",
        # Пользователь не назван вовсе — запуск шёл бы от самого хаба.
        "/usr/bin/sudo -n /usr/local/bin/wrap",
        "/usr/bin/systemd-run --scope /usr/local/bin/wrap",
        "/usr/bin/sudo -n -u  /usr/local/bin/wrap",
        # Флаг есть, значения нет: «флага нет» и «флаг пуст» — разные отказы,
        # и чинятся они по-разному.
        "/usr/bin/systemd-run --scope --uid= /usr/local/bin/wrap",
        "/usr/bin/sudo -n -u",
        # И наоборот: значение написано флагу, который его не берёт.
        "/usr/bin/systemd-run --scope=1 --uid=r /usr/local/bin/wrap",
        # Одинокий дефис позиционным путём не является.
        "/usr/bin/sudo -n -u r -",
    ]
    for sandbox in outside:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert reasons, (
            f"«{sandbox}» не совпадает ни с одной формой закрытого набора, а "
            "хаб пропустил её молча. Пустой список здесь означает «посмотрел "
            "и одобрил» — то есть ровно ту ошибку в сторону пропуска, из-за "
            "которой правило и переписано"
        )
        assert all(r.startswith("LOCAL_REVIEW_SANDBOX:") for r in reasons), (
            f"отказ обязан называть НАСТРОЙКУ, которую чинить: {reasons}"
        )
        assert all("deploy/LOCAL-REVIEW.md" in r for r in reasons), (
            f"отказ обязан указывать на документ, где набор форм назван: {reasons}"
        )
        assert local_reviewer.sandbox_uid() == "", (
            f"на отвергнутой строке «{sandbox}» страж назвал пользователя "
            f"«{local_reviewer.sandbox_uid()}». Прогона не будет, и судить о "
            "членстве в группе каталога прогонов не о чем — а имя, названное "
            "по непрочитанной строке, и есть источник всех девяти кругов"
        )
        assert local_reviewer.sandbox_shape() == ""

    # Пустая настройка второй причиной не шумит: её называет not_ready()
    # отдельной строкой, и повторять то же самое другими словами — это
    # два разных имени одной поломки в одной карточке.
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "")
    assert local_reviewer.sandbox_problem() == []


def test_the_guard_reads_the_sudo_form_of_the_sandbox_user(monkeypatch) -> None:
    """AC-2: форма sudo видна стражу так же, как --uid= от systemd-run.

    Рабочий рецепт выката использует ``sudo -n -u haiplane-reviewer``, а
    страж знал только ``--uid``. Он возвращал пустую строку, и проверка
    членства в группе каталога прогонов (заведённая ради находки ревью
    45971e09) на этой конфигурации не срабатывала ВОВСЕ — то есть создавала
    впечатление проверки там, где её нет.

    ПОВОРОТ 10.09.2026. Прежняя редакция этого теста держала таблицу «строка
    → имя пользователя» и на строках ВНЕ набора: ``sudo -nu X`` давало X,
    ``sudo -u alice -u bob`` — bob. Теперь такие строки не запускаются вовсе,
    и страж на них молчит; сами строки переехали в
    test_a_sandbox_outside_the_closed_set_is_refused, где проверяется их
    отказ. Здесь остались формы, которые набор ПРИНИМАЕТ, — и на них ответ
    обязан быть точным, потому что по нему судится доступ к каталогу.
    """
    cases = {
        # Три формы записи пользователя у sudo, названные в AC-2.
        "/usr/bin/sudo -n -u haiplane-reviewer /usr/local/bin/haiplane-review-run": (
            "haiplane-reviewer"
        ),
        "/usr/bin/sudo -n --user haiplane-reviewer /usr/local/bin/wrap": (
            "haiplane-reviewer"
        ),
        "/usr/bin/sudo -n --user=haiplane-reviewer /usr/local/bin/wrap": (
            "haiplane-reviewer"
        ),
        "/usr/bin/sudo -n -u 1234 /usr/local/bin/wrap": "1234",
        # Старая форма НЕ сломана — иначе починено одно ценой другого.
        "/usr/bin/systemd-run --scope --uid=haiplane-reviewer /usr/local/bin/wrap": (
            "haiplane-reviewer"
        ),
        "/usr/bin/systemd-run --scope --uid haiplane-reviewer /usr/local/bin/wrap": (
            "haiplane-reviewer"
        ),
        "/usr/bin/systemd-run --scope --uid=65534 /usr/local/bin/wrap": "65534",
    }
    for sandbox, expected in cases.items():
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        assert local_reviewer.sandbox_problem() == [], (
            f"«{sandbox}» — форма из набора, и отвергаться она не должна: "
            f"{local_reviewer.sandbox_problem()}"
        )
        assert local_reviewer.sandbox_uid() == expected, (
            f"песочница «{sandbox}» называет пользователя «{expected}», а "
            f"страж вернул «{local_reviewer.sandbox_uid()}»: пустая строка "
            "здесь означает, что проверка группы каталога прогонов молча не "
            "сработает вовсе"
        )


def test_the_user_guard_reads_only_the_wrappers_own_arguments(monkeypatch) -> None:
    """Имя пользователя не берётся из аргументов полезной нагрузки.

    Воспроизведено на HEAD 21629cde до починки — обе формы давали чужой
    ответ: ``sudo -n -u haiplane-reviewer /usr/bin/env -u HOME /wrap`` давало
    ``HOME`` (находка 64e89a8683b01db1), а ``systemd-run --scope --uid=alice
    -- /bin/true --uid=bob`` — ``bob`` (находка d228b0eb3310a9cc). Следствие
    названо: ``scratch_problem`` судил членство в группе каталога прогонов у
    пользователя, которого в строке нет вовсе.

    ПОВОРОТ 10.09.2026. Прежде тест требовал, чтобы на таких строках страж
    называл ПРАВИЛЬНОЕ имя. Теперь у песочницы аргументов полезной нагрузки
    не бывает вовсе: их дописывает сам хаб из LOCAL_REVIEW_CMD, а форма
    кончается путём к обёртке. Поэтому те же входы обязаны быть ОТВЕРГНУТЫ, а
    имя — не называться вообще: ответ по строке, которую хаб не запустит,
    никому не нужен, а именно такие ответы девять кругов и были неверны.
    """
    for sandbox in (
        "/usr/bin/sudo -n -u haiplane-reviewer /usr/bin/env -u HOME /wrap",
        "/usr/bin/sudo -n -u haiplane-reviewer /usr/bin/env -u HOME -u PATH /wrap",
        "/usr/bin/systemd-run --scope --uid=alice -- /bin/true --uid=bob",
        "/usr/bin/systemd-run --scope --uid alice -- /wrap --uid bob",
        "/usr/bin/systemd-run --scope --uid=alice /wrap --uid=bob",
        "/usr/bin/sudo -n -u alice -- -u bob",
        "/usr/bin/systemd-run --scope --uid=alice -- --uid=bob",
        "/usr/bin/sudo -n /wrap --uid=bob",
        "/usr/bin/sudo -n /usr/bin/env -u HOME /wrap",
        "/usr/bin/sudo -n -u alice -u bob /wrap --user carol",
        "/usr/bin/sudo -n /wrap podman run --timeout 60 --user 1000 img",
        "/usr/bin/sudo -nu haiplane-reviewer /usr/bin/env -u HOME /wrap",
        # --user за podman/docker — пользователь ВНУТРИ контейнера, а не на
        # хосте: принять его за хостового значило бы проверить членство в
        # группе каталога совсем не того пользователя.
        "/usr/bin/podman run --rm -i --timeout 60 --user 1000 img",
        "/usr/bin/docker run --user haiplane-reviewer img",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        assert local_reviewer.sandbox_problem(), (
            f"«{sandbox}» несёт аргументы полезной нагрузки прямо в песочнице "
            "— форма набора кончается путём к обёртке, и разбирать чужие "
            "флаги хаб не обязан и не берётся"
        )
        assert local_reviewer.sandbox_uid() == "", (
            f"в отвергнутой строке «{sandbox}» страж назвал "
            f"«{local_reviewer.sandbox_uid()}»: имя из аргументов полезной "
            "нагрузки означает, что членство в группе каталога прогонов "
            "проверяется НЕ У ТОГО пользователя"
        )


def test_the_scope_flag_is_read_only_from_systemd_runs_own_arguments(
    monkeypatch,
) -> None:
    """``--scope`` засчитывается только как СОБСТВЕННЫЙ флаг обёртки.

    Отказ уходил В СТОРОНУ ПРОПУСКА: страж искал ``--scope`` во всей строке,
    и ``--scope`` полезной нагрузки снимал отказ, хотя transient service от
    этого в scope не превращается. Воспроизведено на HEAD 21629cde: строка
    ``systemd-run --quiet --pipe --uid=x -- /wrap --scope`` возвращала ``[]``
    (находка 3d5938a9a645c39d).

    Отказ по НЕДОСТАЮЩЕМУ ОБЯЗАТЕЛЬНОМУ флагу идёт первым — раньше любого
    другого: оператору важнее узнать, что он забыл ``--scope``, чем что хаб
    не принимает ``--pipe``. Иначе он чинил бы по одному незнакомому флагу за
    круг, так и не увидев главного.
    """
    for hidden in (
        "/usr/bin/systemd-run --quiet --pipe --uid=x -- /wrap --scope",
        "/usr/bin/systemd-run --quiet --uid=x /wrap --scope",
        "/usr/bin/systemd-run --uid=x -- /usr/bin/env SCOPE=1 /wrap --scope",
        "/usr/bin/systemd-run --quiet --pipe --uid=haiplane-reviewer --",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", hidden)
        reasons = local_reviewer.sandbox_problem()
        # Подстроки «--scope» мало: она стоит и в ИМЕНИ формы, поэтому её
        # находит любой другой отказ этой же формы. Найдено мутацией
        # 10.09.2026: «if missing:» -> «if False:» оставляла тест зелёным,
        # потому что отказ по терминатору называет форму «systemd-run
        # --scope». Отказ обязан называть ПРИЧИНУ, а она одна — transient
        # service, переживающий снятие прогона.
        assert reasons and all(
            "--scope" in r and "transient service" in r for r in reasons
        ), (
            f"«{hidden}» — systemd-run БЕЗ --scope: ``--scope`` здесь стоит "
            "среди аргументов полезной нагрузки и transient service в scope "
            f"не превращает, а страж пропустил запуск молча: {reasons}"
        )

    # Собственный ``--scope`` обёртки по-прежнему снимает этот отказ — иначе
    # рабочая форма из #1180 оказалась бы сломана.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=x /usr/local/bin/wrap",
    )
    assert local_reviewer.sandbox_problem() == [], (
        "«systemd-run --scope --uid=x /usr/local/bin/wrap» называет --scope "
        f"собственным флагом: {local_reviewer.sandbox_problem()}"
    )


def test_a_non_run_engine_call_does_not_hide_a_later_run(monkeypatch) -> None:
    """Движок отвергается любой подкомандой, а не только на ``run``.

    Отказ уходил В СТОРОНУ ПРОПУСКА: страж возвращал «движка нет» на первом
    же ``podman``, за которым не стоит ``run``, и настоящий запуск правее
    оставался невидимым. Воспроизведено на HEAD 21629cde:
    ``podman ps /usr/bin/podman run --timeout 0 img`` давало ``[]``, тогда
    как одиночный ``podman run --timeout 0`` отвергался (находка
    96144e6317c1d7ac).

    ПОВОРОТ 10.09.2026: искать подкоманду больше не нужно вовсе. Движок в
    закрытый набор форм не входит, и ``podman ps`` отвергается ровно так же,
    как ``podman run``, — вопрос «а не спрятан ли запуск правее» просто
    перестал существовать. Прежняя редакция теста ждала на ``podman ps``
    пустого списка, то есть ОДОБРЕНИЯ; это и есть поворот, и он назван.
    """
    for sandbox in (
        "/usr/bin/podman ps /usr/bin/podman run --timeout 0 img",
        "/usr/bin/podman version /usr/bin/podman run --rm -i img",
        "/usr/bin/docker ps /usr/bin/docker run --rm -i img",
        "/usr/bin/podman ps",
        "/usr/bin/podman ps /usr/bin/podman version",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        assert local_reviewer.sandbox_problem(), (
            f"«{sandbox}» начинается с контейнерного движка, а он в набор "
            "форм не входит ни одной подкомандой: пропустить его значит "
            "разрешить прогон, который снять нельзя"
        )


def test_a_container_launch_without_its_own_deadline_is_refused(monkeypatch) -> None:
    """AC-3: контейнерный запуск отвергнут, и отказ называет НЕДОСТАЮЩИЙ ФЛАГ.

    Требование «прямой потомок хаба» для контейнеров недостижимо: измерено
    08.09.2026 — podman run переживает kill -KILL по группе, через 7 с
    контейнер «Up», потому что conmon отсоединён от клиента намеренно. Зато
    с --timeout 5 контейнер умирает сам (через 12 с живых нет).

    ПОВОРОТ 10.09.2026. Прежде отсюда следовало «потомок ИЛИ свой срок», и
    хаб разбирал строку podman до образа, чтобы этот срок найти. Разбор и дал
    шесть находок из пятнадцати, все — в сторону пропуска. Теперь
    контейнерный запуск отвергается ЦЕЛИКОМ, а срок жизни переехал внутрь
    root-ового враппера, где флаги зафиксированы и sudoers не допускает
    подстановок. Отказ по-прежнему обязан называть НЕДОСТАЮЩИЙ ФЛАГ, а не
    «песочница неверна»: оператору нужно знать, что во враппере обязан стоять
    ``--timeout``, и что у docker такого флага нет вовсе.

    Строки со сроком жизни (``--timeout 1800``), которые прежняя редакция
    теста требовала ПРИНИМАТЬ, теперь отвергаются вместе с остальными — и
    это тот самый поворот. Ни одна из них не выброшена: они стоят здесь же,
    ниже, и проверяются на отказ.
    """
    containers = [
        "/usr/bin/podman run --rm -i img",
        "/usr/bin/podman run --rm -i --timeout 1800 img",
        "/usr/bin/podman run --rm -i --timeout=1800 img",
        "/usr/bin/podman run --rm -i -m 1500m --cpus 1.5 --pids-limit 512 "
        "--env-file /etc/haiplane-review/env --network none --timeout 1800 img",
        "/usr/bin/podman --log-level debug run --rm -i --timeout 1800 img",
        "/usr/bin/podman --url unix:///run/x run --rm -i --timeout 1800 img",
        "/usr/bin/podman run --rm -i img cursor-agent --timeout 60",
        "/usr/bin/podman run --rm -i img agent --timeout=60",
        "/usr/bin/podman --log-level debug run --rm -i img",
        "/usr/bin/podman -c remote run --rm -i img",
        "/usr/bin/podman run --rm -i --timeout 0 img",
        "/usr/bin/podman run --rm -i --timeout=0 img",
        "/usr/bin/podman run --rm -i --timeout 00 img",
        "/usr/bin/podman run --rm -i --timeout abc img",
        "/usr/bin/podman run --rm -i --timeout",
        "/usr/bin/podman run --rm -i --timeout 1 img",
        "/usr/bin/podman run --rm -i --timeout 1800 --timeout 0 img",
        "/usr/bin/podman run --rm -i --timeout=1800 --timeout=0 img",
        "/usr/bin/podman run --rm -i --timeout 1800 --timeout abc img",
        "/usr/bin/podman run --rm -i --timeout 1800 --timeout 5 --timeout 0 img",
        "/usr/bin/podman run --rm -i --timeout 0 --timeout 1800 img",
        "/usr/bin/podman run --rm -i --timeout abc --timeout=1800 img",
    ]
    for sandbox in containers:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert reasons and all("--timeout" in r for r in reasons), (
            f"«{sandbox}» — контейнерный запуск: отказ обязан назвать флаг, "
            f"без которого контейнер переживёт снятие прогона: {reasons}"
        )

    # У docker штатного аналога --timeout нет вовсе, и отказ обязан сказать,
    # ЧЕМ его заменить, а не просто «нельзя».
    for sandbox in (
        "/usr/bin/docker run --rm -i --timeout 60 img",
        "/usr/bin/docker run --rm -i img",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert reasons and any("docker" in r and "podman" in r for r in reasons), (
            f"docker run обязан быть отвергнут с названной заменой: {reasons}"
        )

    # Старое требование не ослаблено: systemd-run без --scope — прежний отказ,
    # и назван по-прежнему недостающий флаг.
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", "/usr/bin/systemd-run --quiet --pipe --uid=x --"
    )
    reasons = local_reviewer.sandbox_problem()
    assert reasons and all(
        "--scope" in r and "transient service" in r for r in reasons
    ), f"systemd-run без --scope остаётся отказом: {reasons}"
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=x /usr/local/bin/wrap",
    )
    assert local_reviewer.sandbox_problem() == []


def test_a_detached_container_launch_is_refused_by_flag(monkeypatch) -> None:
    """Отсоединённый запуск — отказ, и ``--timeout`` его не снимал никогда.

    Найдено ревьюером Codex 10.09.2026, воспроизведено на 5b7b813::

        podman run -d --timeout 1800 img        -> []       # пропуск
        podman run --detach --timeout=1800 img  -> []       # пропуск
        podman run --rm -i img                  -> ОТКАЗ    # правильно

    ``-d`` заставляет клиента podman вернуть управление НЕМЕДЛЕННО: хаб
    видит завершившийся процесс, закрывает прогон как законченный и может
    снести его каталог, а ревьюер внутри контейнера продолжает работать и
    способен прислать отчёт по уже закрытому прогону.

    ПОВОРОТ 10.09.2026. Прежде тест держал две половины: отсоединённые строки
    отвергнуть, «передний план» (``--detach=false``, ``-d=false``, ``-it``)
    принять — иначе починка была бы оплачена ложным отказом. Теперь
    отвергаются ОБЕ половины, потому что отвергается движок целиком, и
    ложного отказа тут нет: строка не «плоха», она вне набора, и починка
    названа — тот же podman внутри враппера. Важное осталось: отказ НЕ
    ВЫДУМЫВАЕТ отсоединения там, где его нет. ``--detach=false`` — это
    передний план, и обвинять строку в ``-d`` было бы ложью о ней.
    """
    detached = [
        "/usr/bin/podman run -d --timeout 1800 img",
        "/usr/bin/podman run --detach --timeout=1800 img",
        "/usr/bin/podman run --rm -i -d --timeout 1800 img",
        "/usr/bin/podman run -itd --timeout 1800 img",
        "/usr/bin/podman run -dt --read-only --timeout 1800 img",
        "/usr/bin/podman run --rm -i --detach=true --timeout 1800 img",
        "/usr/bin/podman run --rm -i --detach=false -d --timeout 1800 img",
    ]
    foreground = [
        "/usr/bin/podman run --rm -i --timeout 1800 img",
        "/usr/bin/podman run --rm -it --timeout 1800 img",
        "/usr/bin/podman run --rm -i --detach=false --timeout 1800 img",
        "/usr/bin/podman run --rm -i -d=false --timeout 1800 img",
        "/usr/bin/podman run --rm -i -d --detach=false --timeout 1800 img",
        "/usr/bin/podman run --rm -i --timeout 1800 img agent -d",
    ]
    for sandbox in detached + foreground:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        assert local_reviewer.sandbox_problem(), (
            f"«{sandbox}» — контейнерный запуск, и в набор форм он не входит"
        )

    for sandbox in foreground:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert not any("отсоедин" in r and "«-d»" in r for r in reasons), (
            f"«{sandbox}» идёт на переднем плане, и обвинять её в -d значит "
            f"назвать оператору причину, которой в строке нет: {reasons}"
        )


def test_a_container_deadline_may_not_outlive_the_hubs(monkeypatch) -> None:
    """Срок контейнера в песочнице не судится вовсе — контейнера там нет.

    Найдено ревьюером Codex 10.09.2026, воспроизведено на 5b7b813:
    ``podman run --rm -i --timeout 3600 img`` при хабских 1800 проходило
    молча, хотя контейнер переживает снятие прогона ровно так же, как без
    ``--timeout`` вовсе, — только тише.

    ПОВОРОТ 10.09.2026. Правило «не больше хабского» держалось на разборе
    чужой командной строки и на сравнении с числом, по которому прогон
    снимут. Обе опоры ушли вместе с контейнерами: приговор этому классу
    строк теперь ОДИН и от ``LOCAL_REVIEW_TIMEOUT_SEC`` не зависит вовсе.
    Это и проверяется — иначе поворот был бы заявлен, а не измерен. Сам срок
    жизни контейнера никуда не делся: он стоит во враппере, равен хабскому и
    держится тестами документа (test_the_doc_wrapper_deadline_follows_the_conf_file).
    """
    verdicts = set()
    for deadline in (1800, 7200):
        monkeypatch.setattr(config, "LOCAL_REVIEW_TIMEOUT_SEC", deadline)
        for sandbox in (
            "/usr/bin/podman run --rm -i --timeout 3600 img",
            "/usr/bin/podman run --rm -i --timeout=1801 img",
            "/usr/bin/podman run --rm -i --timeout 1800 --timeout 7200 img",
            "/usr/bin/podman run --rm -i --timeout 1800 img",
            "/usr/bin/podman run --rm -i --timeout 60 img",
            "/usr/bin/podman run --rm -i --timeout 7200 --timeout 900 img",
            "/usr/bin/podman run --rm -i --timeout 7201 img",
        ):
            monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
            reasons = local_reviewer.sandbox_problem()
            assert reasons, f"«{sandbox}» — контейнерный запуск, и он отвергнут"
            verdicts.add((sandbox, tuple(reasons)))

    assert len({sandbox for sandbox, _ in verdicts}) == len(verdicts), (
        "приговор одной и той же строке разошёлся на разном хабском сроке — "
        "значит суждение всё ещё зависит от LOCAL_REVIEW_TIMEOUT_SEC, а "
        "зависеть ему больше не от чего"
    )


def test_the_run_guard_refuses_the_string_it_cannot_parse(monkeypatch) -> None:
    """Строка, которую страж не может прочитать целиком, — отказ, а не «ок».

    Воспроизведено на ff617db (найдено ревьюером Codex 10.09.2026)::

        podman run --timeout 60 --blkio-weight 500 --timeout 0 img
        detaching_sandbox() -> []            # пропуск

    ``--blkio-weight`` не был назван в списке флагов со значением, поэтому
    ``500`` принято за имя образа, разбор кончился, и ВТОРОЙ ``--timeout 0``
    остался невиден. У podman действующим будет последний, то есть ноль, то
    есть срока жизни нет.

    ПОВОРОТ 10.09.2026: списка флагов podman у хаба больше нет, и угадывать
    нечего — движок не входит в набор форм. Правило, ради которого тест
    написан, стало ШИРЕ: страж отвергает всё, что не прочитал целиком, а не
    только то, о чём успел вынести суждение. Поэтому здесь же проверяются
    строки, которые прежняя редакция ПРИНИМАЛА как «однозначные»
    (``--blkio-weight=500``, ``-itq``): они тоже вне набора.
    """
    for sandbox in (
        "/usr/bin/podman run --timeout 60 --blkio-weight 500 --timeout 0 img",
        "/usr/bin/podman run --blkio-weight 500 --timeout 1800 img",
        "/usr/bin/podman run --rm -i --blkio-weight 500 img",
        "/usr/bin/podman run --rm -i --blkio-weight=500 --timeout=1800 img",
        "/usr/bin/podman run --rm -it --privileged --timeout 1800 img",
        "/usr/bin/podman run -itq --read-only --timeout 1800 img",
        "/usr/bin/podman run --rm -i --timeout 1800 img cursor-agent --blkio-weight 5",
        # Тот же класс в форме, которую набор ЗНАЕТ по инструменту: флаг, о
        # котором форма не договаривалась, отвергается по имени.
        "/usr/bin/systemd-run --scope --uid=r --blkio-weight=500 /usr/local/bin/wrap",
        "/usr/bin/sudo -n -u r --blkio-weight=500 /usr/local/bin/wrap",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert reasons, (
            f"«{sandbox}» страж прочитать целиком не может. Пустой список "
            "здесь означает «посмотрел и одобрил» — то есть контейнер без "
            "срока жизни пройдёт молча"
        )

    # Флаг, которого форма не знает, назван в отказе ПО ИМЕНИ: иначе
    # оператору не видно, что убирать.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=r --blkio-weight=500 /usr/local/bin/wrap",
    )
    assert any("--blkio-weight" in r for r in local_reviewer.sandbox_problem()), (
        "отказ обязан НАЗВАТЬ флаг, которого форма не знает: "
        f"{local_reviewer.sandbox_problem()}"
    )


def test_the_unknown_flag_refusal_advises_a_form_podman_has(monkeypatch) -> None:
    """Отказ советует починку, КОТОРАЯ РАБОТАЕТ, — и это проверяется запуском.

    Один шаблон на обе формы флага советовал невозможное: на коротком токене
    он предлагал ``-p80:80=<значение>`` — записи, которой у podman нет вовсе
    (pflag разберёт ``-p80:80=x`` как значение ``80:80=x`` у ``-p``), и
    оператор, сделавший ровно то, что сказано, получал тот же отказ.

    ПОВОРОТ 10.09.2026: советовать форму записи флага podman хаб больше не
    берётся — он не разбирает podman вовсе. Обещание осталось прежним и стало
    проверяемым строже: КАЖДЫЙ отказ называет починку, и починка приводит к
    строке, которую хаб принимает. Здесь это проверяется исполнением, а не
    чтением: сделали, как сказано, — прогнали через стража.
    """
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/podman run --rm -p80:80 --timeout 1800 img",
    )
    reasons = local_reviewer.sandbox_problem()
    assert reasons, "неизвестный короткий флаг обязан давать отказ"
    assert not any("-p80:80=<значение>" in r for r in reasons), (
        "отказ советует дописать «=<значение>» к целому короткому токену — "
        f"записи, которой у podman нет: {reasons}"
    )
    assert all("sudo -n -u" in r for r in reasons), (
        f"отказ обязан назвать форму, к которой оператору идти: {reasons}"
    )

    # Починка, названная в отказе, ИСПОЛНЯЕТСЯ и приводит к принятой строке.
    fixed = "/usr/bin/sudo -n -u haiplane-reviewer /usr/local/bin/haiplane-review-run"
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", fixed)
    assert local_reviewer.sandbox_problem() == [], (
        f"страж советует форму, которую сам же отвергает: "
        f"{local_reviewer.sandbox_problem()}"
    )

    # То же обещание на слипшемся токене: сказано «напишите отдельными
    # токенами» — делаем так и получаем принятую строку.
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", "/usr/bin/sudo -nu r /usr/local/bin/wrap"
    )
    bundled = local_reviewer.sandbox_problem()
    assert bundled and all("отдельным токеном" in r for r in bundled), (
        f"отказ на слипшемся токене обязан назвать починку: {bundled}"
    )
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", "/usr/bin/sudo -n -u r /usr/local/bin/wrap"
    )
    assert local_reviewer.sandbox_problem() == [], (
        f"починка, названная в отказе, не работает: {local_reviewer.sandbox_problem()}"
    )


def test_a_consumed_value_does_not_pass_for_the_scope_flag(monkeypatch) -> None:
    """``--scope``, съеденный как ЗНАЧЕНИЕ соседа, флагом ``--scope`` не является.

    Неразрешённая 5a59af8d620685d0 машинного ревью #371. Воспроизведено на
    ff617db и на HEAD 149cd825::

        systemd-run --description --scope --uid=haiplane-reviewer /wrap
        окно -> ['--description', '--scope', '--uid=haiplane-reviewer']
        detaching_sandbox() -> []            # пропуск

    Настоящий systemd-run возьмёт ``--scope`` описанием и поднимет transient
    SERVICE, то есть ровно то, ради чего отказ и написан. Ошибка шла В
    СТОРОНУ ПРОПУСКА.

    Возражение опровергателя («рекомендованный рецепт — sudo-враппер, а не
    systemd-run») отклонено и здесь: страж существует не для рекомендованной
    строки, а для той, которую напишет оператор, — рекомендованную проверять
    было бы незачем. И systemd-run --scope остаётся формой набора.
    """
    for sandbox in (
        "/usr/bin/systemd-run --description --scope --uid=r /wrap",
        "/usr/bin/systemd-run --unit --scope --uid=r /wrap",
        "/usr/bin/systemd-run --slice --scope -- /wrap",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert reasons and all(
            "--scope" in r and "transient service" in r for r in reasons
        ), (
            f"в «{sandbox}» слово «--scope» стоит ЗНАЧЕНИЕМ соседнего флага, "
            "а не флагом. systemd-run поднимет transient service, который "
            f"переживёт снятие прогона: {reasons}"
        )

    # А рабочие формы, где --scope настоящий, приниматься не перестали:
    # починка не оплачена ложным отказом (возражение опровергателя учтено).
    for fine in (
        "/usr/bin/systemd-run --description=review --scope --uid=r /usr/local/bin/wrap",
        "/usr/bin/systemd-run --scope --description review --uid=r /usr/local/bin/wrap",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", fine)
        assert local_reviewer.sandbox_problem() == [], (
            f"«{fine}» называет --scope собственным флагом systemd-run: "
            f"{local_reviewer.sandbox_problem()}"
        )

    # Та же поправка на пользователе: имя берётся у флага, а не у соседа.
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", "/usr/bin/sudo --prompt -u alice /wrap"
    )
    assert local_reviewer.sandbox_uid() == "", (
        "«-u» здесь съедено значением --prompt, и пользователя строка не "
        f"называет вовсе; страж назвал «{local_reviewer.sandbox_uid()}»"
    )


def test_the_run_options_end_at_the_double_dash(monkeypatch) -> None:
    """``--`` в песочнице не принимается — ни у движка, ни у формы набора.

    Неразрешённая a58d77e268b2d709 машинного ревью #371. Воспроизведено на
    ff617db и на HEAD 149cd825::

        podman run -- --timeout 1800 img
        окно флагов -> [('--', None), ('--timeout', '1800')]
        detaching_sandbox() -> []            # пропуск

    Для podman образ здесь — ``--timeout``, а ``1800`` и ``img`` уже команда
    внутри контейнера: собственного срока жизни у запуска НЕТ. Страж же
    находил ``--timeout 1800`` и засчитывал срок, ошибаясь В СТОРОНУ
    ПРОПУСКА. Хуже того, прошлый круг числил ``--`` среди беззначных флагов,
    то есть отказ по неизвестному флагу на этой строке заведомо не срабатывал.

    ПОВОРОТ 10.09.2026: терминатор не принимается ни в одной форме набора —
    за флагами стоит ровно один аргумент, путь к обёртке, и отделять от него
    нечего. Класс дефектов «что считается за ``--``» закрыт целиком, а не
    разобран правильнее.
    """
    for sandbox in (
        "/usr/bin/podman run -- --timeout 1800 img",
        "/usr/bin/podman run --rm -i -- --timeout 1800 img",
        "/usr/bin/podman run -- --rm -i img",
        "/usr/bin/podman run --rm -i --timeout 1800 -- img",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        assert local_reviewer.sandbox_problem(), (
            f"в «{sandbox}» всё, что стоит за «--», — это образ и команда "
            "внутри контейнера. Своего срока жизни у запуска нет, и "
            "контейнер переживёт снятие прогона"
        )

    for sandbox in (
        "/usr/bin/sudo -n -u haiplane-reviewer -- /usr/local/bin/wrap",
        "/usr/bin/systemd-run --scope --uid=r -- /usr/local/bin/wrap",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert reasons and all("--»" in r for r in reasons), (
            f"«{sandbox}» пишет терминатор там, где отделять нечего, и отказ "
            f"обязан назвать именно его: {reasons}"
        )


def test_sudo_names_a_numeric_uid_with_a_hash(monkeypatch, tmp_path) -> None:
    """``sudo -u '#1000'`` — числовой uid, а не имя, которого нет в системе.

    sudo(8): «The user may be either a user name or a numeric user ID (UID)
    prefixed with the '#' character». Воспроизведено на ff617db (найдено
    ревьюером Codex 10.09.2026)::

        sudo -n -u #1000 /usr/local/bin/wrap
        sandbox_uid() -> '#1000'   _resolve_user('#1000') -> None

    ``'#1000'.isdigit()`` ложно, разбор уходил в ``getpwnam('#1000')`` и падал
    ВСЕГДА. Отказ шёл в безопасную сторону, но причину называл неверную:
    оператор читал «пользователь не разрешается в системе» про пользователя,
    который в системе есть.

    Решётка снимает неоднозначность именно У SUDO: там ``1000`` — это ИМЯ, а
    ``#1000`` — uid, и обе записи законны. У systemd-run такой формы нет, и
    принимать её там значит разрешить строку, которую сам systemd-run
    отвергнет, — поэтому тест держит и это.
    """
    import pwd

    me = pwd.getpwuid(os.getuid())
    uid = me.pw_uid

    # Слипшаяся запись той же решётки в набор форм не входит с 10.09.2026 —
    # но вход не выброшен: он проверяется на ОТКАЗ, а не на разбор.
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", f"/usr/bin/sudo -nu#{uid} /usr/local/bin/wrap"
    )
    assert local_reviewer.sandbox_problem(), (
        "слипшийся токен «-nu#N» набор форм не принимает: из одного токена не "
        "видно, где кончается флаг и начинается значение"
    )

    # Форма sudo с решёткой разрешается в НАСТОЯЩЕГО пользователя.
    for sandbox in (
        f"/usr/bin/sudo -n -u #{uid} /usr/local/bin/wrap",
        f"/usr/bin/sudo -n --user=#{uid} /usr/local/bin/wrap",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        tool, user = local_reviewer._named_sandbox_user()
        entry = local_reviewer._resolve_user(user, tool)
        assert entry is not None and entry.pw_uid == uid, (
            f"«{sandbox}» называет uid {uid} синтаксисом самого sudo, а страж "
            f"его не разрешил ({tool!r}, {user!r} -> {entry}): годная "
            "настройка отвергается, и человеку называется ложная причина"
        )

    # Различение сохранено: у systemd-run решётки нет, и придумывать её там
    # нельзя — а голое число он принимает как uid по-прежнему.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        f"/usr/bin/systemd-run --scope --uid=#{uid} /usr/local/bin/wrap",
    )
    tool, user = local_reviewer._named_sandbox_user()
    assert local_reviewer._resolve_user(user, tool) is None, (
        "у systemd-run записи --uid=#N не существует; принять её значило бы "
        "разрешить строку, на которой запуск упадёт уже внутри systemd-run"
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        f"/usr/bin/systemd-run --scope --uid={uid} /usr/local/bin/wrap",
    )
    tool, user = local_reviewer._named_sandbox_user()
    entry = local_reviewer._resolve_user(user, tool)
    assert entry is not None and entry.pw_uid == uid, (
        "числовая форма systemd-run (неразрешённая 534e16e4) не сломана"
    )

    # И обратная сторона того же правила: у sudo ГОЛОЕ число — это ИМЯ.
    # Замерено 10.09.2026 на машине разработки, где uid 501 существует и
    # принадлежит вызывающему::
    #
    #     sudo -n -u 501 true    -> «sudo: unknown user 501», rc 1
    #     sudo -n -u '#501' true -> rc 0
    #
    # Значит ``getpwuid`` на такой записи отвечает про пользователя, которого
    # sudo в строке НЕ ВИДИТ, — и страж одобрял бы песочницу, падающую на
    # первом запуске. Ревьюер называл только форму с решёткой; эта половина
    # доделана преемником.
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", f"/usr/bin/sudo -n -u {uid} /usr/local/bin/wrap"
    )
    tool, user = local_reviewer._named_sandbox_user()
    assert (tool, user) == ("sudo", str(uid)), (
        f"пользователь из «sudo -u {uid}» не извлёкся ({tool!r}, {user!r}) — "
        "всё, что ниже, судило бы не тот вход"
    )
    bare = local_reviewer._resolve_user(user, tool)
    try:
        by_name = pwd.getpwnam(str(uid))
    except KeyError:
        by_name = None
    # РАВЕНСТВО, а не «None или имя из цифр». Прежнее утверждение
    # «bare is None or bare.pw_name == str(uid)» getpwnam от getpwuid не
    # отличало: на машине, где учётка названа цифрами собственного uid
    # (обычная запись в образах и в LDAP), обе ветки дают одну и ту же
    # запись, pw_name == str(uid), и регресс в getpwuid проходил бы
    # зелёным (неразрешённая ревью #373; проверено на struct_passwd
    # ('1000', uid 1000) — утверждение True на обеих ветках).
    assert bare == by_name, (
        f"«sudo -u {uid}» — это ИМЯ «{uid}», и страж обязан ответить ровно "
        f"то же, что getpwnam('{uid}') ({by_name}); он ответил {bare}. "
        "Разрешать голое число через getpwuid значит проверить членство в "
        "группе у постороннего и объявить настроенной песочницу, на которой "
        "sudo скажет «unknown user»"
    )

    # Та же строка, доведённая до САМОГО стража каталога прогонов: прежде эта
    # половина не проверялась вовсе, и «страж одобрял бы песочницу, падающую
    # на первом запуске» держалось одним лишь _resolve_user.
    scratch = tmp_path / "runs"
    scratch.mkdir()
    # chown ДО chmod: непривилегированный chown снимает setgid, и каталог
    # увёл бы scratch_problem() в ветку «нет setgid», где про пользователя не
    # спрашивают вовсе.
    os.chown(scratch, -1, me.pw_gid)
    scratch.chmod(0o2770)
    assert os.stat(scratch).st_mode & stat.S_ISGID, "каталог без setgid судит другое"
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(scratch))
    problems = local_reviewer.scratch_problem()
    if by_name is None:
        assert problems and all("не разрешается в системе" in p for p in problems), (
            f"«sudo -u {uid}» называет ИМЯ «{uid}», которого в системе нет. "
            "Страж обязан назвать это причиной, а не промолчать: молчание "
            f"здесь — обещание работы, которой не будет ({problems})"
        )
        assert any(f"«{uid}»" in p for p in problems), (
            f"отказ обязан назвать неразрешённого пользователя: {problems}"
        )
    else:  # pragma: no cover — учётка, названная цифрами uid, на CI не заводится
        assert not any("не разрешается в системе" in p for p in problems), (
            f"пользователь «{uid}» в системе ЕСТЬ, и отказ по разрешению "
            f"имени был бы ложным: {problems}"
        )
    # А у systemd-run голое число uid-ом БЫТЬ ОБЯЗАНО — иначе доделка выше
    # сломала бы неразрешённую 534e16e4, ради которой числовая форма и заведена.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        f"/usr/bin/systemd-run --scope --uid={uid} /usr/local/bin/wrap",
    )
    tool, user = local_reviewer._named_sandbox_user()
    entry = local_reviewer._resolve_user(user, tool)
    assert entry is not None and entry.pw_uid == uid

    # Имя пользователя по-прежнему разрешается по имени, а мусор с решёткой —
    # по-прежнему нет: отказ остаётся отказом.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        f"/usr/bin/sudo -n -u {me.pw_name} /usr/local/bin/wrap",
    )
    tool, user = local_reviewer._named_sandbox_user()
    assert local_reviewer._resolve_user(user, tool) is not None
    for junk in ("#", "#abc", "#-1"):
        monkeypatch.setattr(
            config,
            "LOCAL_REVIEW_SANDBOX",
            f"/usr/bin/sudo -n -u '{junk}' /usr/local/bin/wrap",
        )
        tool, user = local_reviewer._named_sandbox_user()
        assert local_reviewer._resolve_user(user, tool) is None, (
            f"«{junk}» числовым uid не является, и разрешаться не должен"
        )


def test_the_scratch_check_reaches_the_group_through_a_hash_uid(
    monkeypatch, tmp_path
) -> None:
    """Проверка группы каталога прогонов ДОХОДИТ до существа на форме ``#UID``.

    Отдельный тест, а не хвост предыдущего, по двум причинам. Первая — так
    видно, ЧТО именно ломается: прямые вызовы ``_resolve_user`` рядом падают
    первыми и накрывают собой эту проверку. Вторая важнее: прошлая редакция
    ставила здесь ``LOCAL_REVIEW_SCRATCH_DIR = ""`` и ждала ``[]`` — а
    ``scratch_problem()`` на пустой настройке возвращает ``[]`` ПЕРВОЙ ЖЕ
    СТРОКОЙ, не дойдя ни до ``_uid_outside_group``, ни до ``_resolve_user``.
    Утверждение было верным при любом состоянии кода, то есть не держало
    ничего; комментарий при нём обещал ровно обратное («scratch_problem зовёт
    его сам»). Поэтому каталог здесь настоящий.

    Существо: пользователь РАЗРЕШЁН, значит «не смогли проверить» звучать не
    должно.

    Утверждениями ПОЛОЖИТЕЛЬНЫМИ, а не запретом фразы. Прошлая редакция
    запрещала здесь только слова «не разрешается в системе» — и оставалась
    зелёной, когда извлечение пользователя ломалось совсем: при пустом
    ``user`` ``_uid_outside_group`` возвращает ``[]`` первой же строкой, до
    ``_resolve_user`` дело не доходит, и запрещать в пустом списке нечего.
    Замерено на HEAD 684bab0b: подменить ``_named_sandbox_user`` на
    ``("", "")`` — ``scratch_problem()`` даёт ``[]``, утверждение True
    (неразрешённая ревью #373). Поэтому тест теперь называет и извлечение, и
    ОБА исхода сравнения с группой.
    """
    import grp
    import pwd

    me = pwd.getpwuid(os.getuid())
    uid = me.pw_uid
    scratch = tmp_path / "runs"
    scratch.mkdir()
    # chown ДО chmod: непривилегированный chown снимает setgid.
    os.chown(scratch, -1, me.pw_gid)
    scratch.chmod(0o2770)
    st = os.stat(scratch)
    assert st.st_mode & stat.S_ISGID, (
        "каталог без setgid уводит scratch_problem() в ДРУГУЮ ветку, и тест "
        "снова ничего не проверит — как это уже было с пустой настройкой"
    )
    assert st.st_gid == me.pw_gid, "группа каталога не та, о которой судит тест"

    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(scratch))
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", f"/usr/bin/sudo -n -u #{uid} /wrap"
    )

    # 1. Пользователь ИЗВЛЁКСЯ. Без этого всё ниже вырождается в проверку
    #    пустого списка, а вырожденный тест хуже отсутствующего: он показывает
    #    покрытие там, где его нет.
    assert local_reviewer._named_sandbox_user() == ("sudo", f"#{uid}"), (
        "форма «-u #UID» не извлеклась из песочницы: "
        f"{local_reviewer._named_sandbox_user()}"
    )

    # 2. Группа СВОЯ — путь доходит до сравнения и разрешает. Пустой список
    #    здесь означает «проверил и допустил», и держит его пункт 1.
    problems = local_reviewer.scratch_problem()
    assert problems == [], (
        f"«sudo -u #{uid}» называет существующего пользователя синтаксисом "
        "самого sudo, каталог принадлежит его же основной группе — а хаб "
        f"всё равно нашёл причину: {problems}"
    )

    # 3. Группа ЧУЖАЯ — тот же путь доходит до сравнения и НАЗЫВАЕТ имена.
    #    Это и есть утверждение, которого не хватало: если ``#UID`` перестанет
    #    разрешаться, здесь встанет «не разрешается в системе», а если
    #    извлечение сломается — пустой список. Оба падают.
    mine = set(os.getgroups()) | {me.pw_gid}
    foreign = next(
        (
            g
            for g in grp.getgrall()
            if g.gr_gid not in mine and me.pw_name not in g.gr_mem
        ),
        None,
    )
    assert foreign is not None, "на машине не нашлось группы, в которой мы не состоим"
    outside = local_reviewer._uid_outside_group(str(scratch), foreign.gr_gid)
    assert len(outside) == 1, (
        f"членство в чужой группе «{foreign.gr_name}» обязано быть названо "
        f"причиной, а страж вернул {outside}"
    )
    assert "не разрешается в системе" not in outside[0], (
        f"форма «#{uid}» снова не разрешилась, и оператору называется ложная "
        f"причина: {outside[0]}"
    )
    assert me.pw_name in outside[0] and foreign.gr_name in outside[0], (
        "отказ обязан назвать И пользователя, И группу — иначе чинить нечего: "
        f"{outside[0]}"
    )
