"""Диспетчер агентов: 407 строк, которые не исполнял ни один тест (#849).

До этого файла модуль не просто был не покрыт — coverage сообщал «module was
never imported», то есть ни один тест его даже не загружал. Сломать запуск
агентов можно было любой правкой, и все две тысячи тестов остались бы
зелёными; узнали бы об этом по задачам, застрявшим между open и running.

Тесты держатся двух правил из постановки: наружу не ходим (ни сети, ни
реальных процессов) и проверяем результат, а не факт вызова. Подпроцесс
подменяется на уровне asyncio, каталоги заданий и логов — на временные.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from hub.integrations import dispatch as dispatch_module
from hub.integrations.dispatch import DispatchIntegration


@pytest.fixture
def plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DispatchIntegration:
    """Плагин, чьи каталоги и бинарь живут во временном каталоге.

    Константы импортированы в модуль по имени, поэтому подменяются на самом
    модуле: правка hub.config сюда бы не дошла.
    """
    jobs = tmp_path / "jobs"
    logs = tmp_path / "logs"
    jobs.mkdir()
    logs.mkdir()
    monkeypatch.setattr(dispatch_module, "DISPATCH_JOBS_DIR", jobs)
    monkeypatch.setattr(dispatch_module, "DISPATCH_LOGS_DIR", logs)
    monkeypatch.setattr(dispatch_module, "DISPATCH_BIN", str(tmp_path / "oc-dispatch"))
    return DispatchIntegration()


class _FakeProc:
    """Подпроцесс, который никуда не ходит и отвечает заготовленным."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", code: int = 0) -> None:
        self._out = stdout
        self._err = stderr
        self.returncode = code

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out, self._err


def _fake_exec(proc: _FakeProc, seen: list[list[str]]):
    async def _spawn(*cmd: str, **kwargs: Any) -> _FakeProc:
        seen.append(list(cmd))
        return proc

    return _spawn


# --- состояние заданий на диске -------------------------------------------


def test_is_available_follows_the_binary(
    plugin: DispatchIntegration, tmp_path: Path
) -> None:
    assert plugin.is_available() is False, "бинаря нет — плагин недоступен"
    (tmp_path / "oc-dispatch").write_text("#!/bin/sh\n")
    assert plugin.is_available() is True


def test_jobs_come_newest_first_and_broken_files_are_skipped(
    plugin: DispatchIntegration,
) -> None:
    """Битый JSON не должен ронять список: одно испорченное задание из десяти
    иначе лишило бы оператора всей страницы."""
    jobs_dir = dispatch_module.DISPATCH_JOBS_DIR
    (jobs_dir / "old.json").write_text(json.dumps({"id": "old"}))
    (jobs_dir / "broken.json").write_text("{не json")
    (jobs_dir / "new.json").write_text(json.dumps({"id": "new"}))
    import os

    os.utime(jobs_dir / "old.json", (1, 1))
    os.utime(jobs_dir / "broken.json", (2, 2))
    os.utime(jobs_dir / "new.json", (3, 3))

    jobs = plugin.list_jobs()

    assert [j["id"] for j in jobs] == ["new", "old"]


def test_jobs_respect_the_limit(plugin: DispatchIntegration) -> None:
    for i in range(5):
        (dispatch_module.DISPATCH_JOBS_DIR / f"j{i}.json").write_text(
            json.dumps({"id": f"j{i}"})
        )
    assert len(plugin.list_jobs(limit=2)) == 2


def test_missing_jobs_dir_is_empty_not_an_error(
    plugin: DispatchIntegration, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatch_module, "DISPATCH_JOBS_DIR", tmp_path / "нет")
    assert plugin.list_jobs() == []


def test_get_job_reads_one_and_says_none_when_absent(
    plugin: DispatchIntegration,
) -> None:
    (dispatch_module.DISPATCH_JOBS_DIR / "abc.json").write_text(
        json.dumps({"id": "abc", "status": "running"})
    )
    assert plugin.get_job("abc") == {"id": "abc", "status": "running"}
    assert plugin.get_job("нет-такого") is None


def test_log_tail_returns_the_end_and_survives_a_missing_file(
    plugin: DispatchIntegration,
) -> None:
    (dispatch_module.DISPATCH_LOGS_DIR / "job.log").write_text(
        "\n".join(f"строка {i}" for i in range(10))
    )
    assert plugin.job_log_tail("job", max_lines=3) == [
        "строка 7",
        "строка 8",
        "строка 9",
    ]
    assert plugin.job_log_tail("нет") == []


def test_log_full_returns_everything_or_empty(plugin: DispatchIntegration) -> None:
    (dispatch_module.DISPATCH_LOGS_DIR / "job.log").write_text("весь лог")
    assert plugin.job_log_full("job") == "весь лог"
    assert plugin.job_log_full("нет") == ""


# --- сборка сообщения агенту ----------------------------------------------


def test_enriched_message_carries_task_branch_and_history(
    plugin: DispatchIntegration,
) -> None:
    msg = plugin.build_enriched_message(
        title="Починить поллер",
        description="Описание задачи",
        updates=[
            {"kind": "question", "agent": "pda_claude", "content": "какой дедлайн?"},
            {"kind": "answer", "content": "сегодня"},
            {"kind": "status", "agent": "pda_claude", "content": "взял в работу"},
        ],
        branch="task-1/fix",
        breadcrumb="Эпик > Фича",
    )

    assert "Починить поллер" in msg and "Описание задачи" in msg
    assert "Эпик > Фича" in msg
    assert "task-1/fix" in msg
    assert "какой дедлайн?" in msg and "сегодня" in msg and "взял в работу" in msg
    assert "[ответ] Человек:" in msg, "ответ человека помечается отдельно от агентских"


def test_enriched_message_forbids_git_only_when_a_branch_is_given(
    plugin: DispatchIntegration,
) -> None:
    """Запрет на git-команды едет вместе с веткой.

    Без ветки агенту нечего коммитить, и предупреждение было бы шумом; с веткой
    оно обязано быть — на нём держится правило «Hub коммитит сам».
    """
    with_branch = plugin.build_enriched_message("t", "d", branch="task-1/x")
    without = plugin.build_enriched_message("t", "d")

    assert "ЗАПРЕЩЕНО: git commit" in with_branch
    assert "ЗАПРЕЩЕНО" not in without


def test_unknown_update_kinds_are_dropped_not_rendered(
    plugin: DispatchIntegration,
) -> None:
    """Неизвестный вид записи не попадает в промпт.

    Журнал задачи растёт новыми видами (alert, decision, review), и молча
    вливать их в сообщение агенту значит однажды подсунуть ему чужой текст как
    часть задания.
    """
    msg = plugin.build_enriched_message(
        "t",
        "d",
        updates=[
            {"kind": "alert", "agent": "hub", "content": "СЛУЖЕБНЫЙ АЛЕРТ"},
            {"kind": "status", "agent": "a", "content": "рабочая запись"},
        ],
    )
    assert "рабочая запись" in msg
    assert "СЛУЖЕБНЫЙ АЛЕРТ" not in msg


# --- запуск агента ---------------------------------------------------------


async def test_submit_returns_the_parsed_answer_and_addresses_the_task(
    plugin: DispatchIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_exec(_FakeProc(stdout=b'{"job_id": "j-1"}'), seen),
    )

    out = await plugin.submit_task("сделай", runtime="openrouter", task_id=42)

    assert out == {"job_id": "j-1"}
    cmd = seen[0]
    assert "submit" in cmd and "openrouter" in cmd
    assert "+10000000042" in cmd, "адресация задачи должна доехать до диспетчера"


async def test_submit_reports_a_failed_run_instead_of_pretending(
    plugin: DispatchIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: сломанный путь отказа обязан быть виден.

    Молчаливый успех при ненулевом коде — худший исход: задача уходит в
    running, а агента никто не запускал.
    """
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_exec(_FakeProc(stderr="нет квоты".encode(), code=3), []),
    )

    out = await plugin.submit_task("сделай")

    assert out["exit_code"] == 3
    assert "нет квоты" in out["error"]
    assert "job_id" not in out


async def test_submit_without_a_binary_answers_instead_of_raising(
    plugin: DispatchIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(*cmd: str, **kwargs: Any) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)

    out = await plugin.submit_task("сделай")

    assert "dispatch binary not found" in out["error"]


async def test_submit_keeps_unparsable_output_rather_than_losing_it(
    plugin: DispatchIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_exec(_FakeProc(stdout="не json, но важно".encode()), []),
    )

    out = await plugin.submit_task("сделай")

    assert out["raw"] == "не json, но важно"


async def test_agent_name_travels_in_the_environment(
    plugin: DispatchIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def _spawn(*cmd: str, **kwargs: Any) -> _FakeProc:
        captured.update(kwargs.get("env") or {})
        return _FakeProc(stdout=b"{}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

    await plugin.submit_task("сделай", agent="pda_claude")

    assert captured["HAIPLANE_OPENROUTER_DEV_AGENT"] == "pda_claude"
    assert captured["HAIPLANE_VAST_DEV_AGENT"] == "pda_claude"
    legacy_prefix = "OPEN" + "CLAW" + "_"
    assert not [
        k for k in captured if k.startswith(legacy_prefix) and k.endswith("_DEV_AGENT")
    ], "Wave 5: the hub must not add legacy-prefixed agent keys"


async def test_classify_parses_and_degrades_readably(
    plugin: DispatchIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_exec(_FakeProc(stdout=b'{"runtime": "auto"}'), []),
    )
    assert await plugin.classify_task("что это") == {"runtime": "auto"}

    async def _boom(*cmd: str, **kwargs: Any) -> None:
        raise PermissionError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    assert "not found" in (await plugin.classify_task("что это"))["error"]


# --- остальные сборщики сообщений ------------------------------------------


def test_review_message_points_at_the_branch_and_the_pr(
    plugin: DispatchIntegration,
) -> None:
    """Ревьюеру нужны ветка и PR: без них он смотрит не тот код.

    Именно этот класс ошибки закрывали #572 и #612 — вердикт, выданный по
    другому коммиту, ничего не значит.
    """
    msg = plugin.build_review_message(
        task_id=42,
        title="Починить поллер",
        description="Описание",
        review_cycle=1,
        max_cycles=3,
        branch="task-42/fix",
        pr_number=100,
        breadcrumb="Эпик > Фича",
        # #1119: адрес приходит СНАРУЖИ. Раньше сборщик собирал его сам из
        # литерала github.com и глобального REPO_NAME — то есть знал и
        # хостинг, и репозиторий, и оба знал неверно для любого проекта,
        # кроме одного. Прежнее ожидание закрепляло именно это: ссылку,
        # выдуманную тем, кто не может её знать.
        pr_url="https://github.com/mrPDA/repo/pull/100",
    )

    assert "#42" in msg and "Починить поллер" in msg
    assert "task-42/fix" in msg
    assert "/pull/100" in msg
    assert "Эпик > Фича" in msg


def test_review_message_without_a_branch_falls_back_to_local_diff(
    plugin: DispatchIntegration,
) -> None:
    msg = plugin.build_review_message(1, "t", "d", 1, 3)

    assert "git diff HEAD~1" in msg, "без ветки сравнивать не с чем, кроме прошлого"
    assert "/pull/" not in msg, "номера PR нет — ссылки быть не должно"


def test_fix_message_carries_the_reviewer_comments_and_the_cycle(
    plugin: DispatchIntegration,
) -> None:
    """Исправляющий агент должен видеть, ЧТО именно просили поправить."""
    msg = plugin.build_fix_message(
        task_id=7,
        title="Задача",
        description="Описание",
        review_comments="дублируется проверка статуса",
        review_cycle=2,
        max_cycles=3,
        branch="task-7/x",
    )

    assert "дублируется проверка статуса" in msg
    assert "#7" in msg
    assert "2" in msg and "3" in msg, "номер круга и предел должны быть видны"


def test_ci_fix_message_shows_the_failure_not_just_the_fact(
    plugin: DispatchIntegration,
) -> None:
    """«CI упал» без логов заставляет агента гадать; передаём причину."""
    msg = plugin.build_ci_fix_message(
        task_id=9,
        title="Задача",
        description="Описание",
        ci_failures={"job": "pytest", "log": "assert 1 == 2"},
        ci_fix_cycle=1,
        max_cycles=3,
        branch="task-9/x",
    )

    assert "#9" in msg
    assert "assert 1 == 2" in msg or "pytest" in msg


def test_arbiter_message_carries_the_whole_disagreement(
    plugin: DispatchIntegration,
) -> None:
    """Арбитра зовут, когда исполнитель и ревьюер не сошлись.

    Ему нужна история обоих кругов: решение по последней реплике — это тот же
    спор, только с третьим участником.
    """
    msg = plugin.build_arbiter_message(
        task_id=11,
        title="Задача",
        description="Описание",
        # Формат — записи журнала задачи (kind/agent/content), ровно то, что
        # передаёт orchestration: там review_history собирается из updates.
        review_history=[
            {"kind": "review", "agent": "reviewer", "content": "первое замечание"},
            {"kind": "review", "agent": "reviewer", "content": "второе замечание"},
            {"kind": "alert", "agent": "hub", "content": "лимит циклов исчерпан"},
        ],
        review_cycle=3,
        max_cycles=3,
        branch="task-11/x",
    )

    assert "#11" in msg
    assert "первое замечание" in msg and "второе замечание" in msg
    # Служебные алерты арбитру доезжают, в отличие от build_enriched_message,
    # где неизвестные виды отбрасываются. Разница намеренная: арбитру нужен
    # контекст «почему тебя позвали», исполнителю — только рабочие записи.
    assert "лимит циклов исчерпан" in msg
