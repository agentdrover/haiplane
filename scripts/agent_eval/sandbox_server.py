"""Процесс одноразового хаба для eval (#1223). Запускается только sandbox.py.

Это настоящее приложение ``hub.app`` с двумя заменами, которых в проде нет:

* внешние адаптеры (git, форж, dispatch, заметки, vast, транскрипты) —
  ``DeniedAdapter``: поведение Noop-заглушки плюс запись имени вызванного
  метода в журнал попыток (только имя: аргументов в журнале нет);
* фоновый поллер не стартует: ни проб наружу, ни автомерж, ни рассылок.

Слушающий сокет приходит готовым (fd от родителя): порт выбирает родитель и
гарантированно держит его, гонки «порт занят» нет.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from collections.abc import Callable
from typing import Any

#: Поля реестра плагинов и их Noop-заглушки. Имя поля — префикс в журнале.
_ADAPTERS = (
    ("dispatch", "NoopDispatch"),
    ("git_ops", "NoopGitOps"),
    ("forge", "NoopForge"),
    ("github", "NoopGitHub"),
    ("notes", "NoopNotes"),
    ("vast", "NoopVast"),
    ("transcripts", "NoopTranscripts"),
)


class DeniedAdapter:
    """Заглушка внешнего адаптера: делегирует Noop и пишет имя метода."""

    def __init__(self, name: str, inner: Any, audit: Callable[[str], None]) -> None:
        self._name = name
        self._inner = inner
        self._audit = audit

    def __getattr__(self, attr: str) -> Any:
        target = getattr(self._inner, attr)  # AttributeError пробрасывается как есть
        if attr.startswith("_") or not callable(target):
            return target
        label = f"{self._name}.{attr}"
        audit = self._audit
        if inspect.isasyncgenfunction(target):

            async def agen(*args: Any, **kwargs: Any) -> Any:
                audit(label)
                async for item in target(*args, **kwargs):
                    yield item

            return agen
        if inspect.iscoroutinefunction(target):

            async def coro(*args: Any, **kwargs: Any) -> Any:
                audit(label)
                return await target(*args, **kwargs)

            return coro

        def plain(*args: Any, **kwargs: Any) -> Any:
            audit(label)
            return target(*args, **kwargs)

        return plain


def install_denied(registry: Any, audit: Callable[[str], None]) -> None:
    """Заменить ВСЕ адаптеры реестра на DeniedAdapter поверх Noop."""
    from hub.integrations import noop

    for field, noop_name in _ADAPTERS:
        setattr(
            registry,
            field,
            DeniedAdapter(field, getattr(noop, noop_name)(), audit),
        )


def _file_audit(path: str) -> Callable[[str], None]:
    def write(label: str) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"effect": label}) + "\n")

    return write


def _denied_runner(audit: Callable[[str], None], label: str) -> Callable[..., Any]:
    async def refuse(*args: Any, **kwargs: Any) -> Any:
        audit(label)
        raise PermissionError(f"{label}: запуск процессов запрещён в eval-песочнице")

    return refuse


def deny_execution(audit: Callable[[str], None]) -> Callable[[], None]:
    """Запретить хабу-ребёнку порождать процессы вообще. Возвращает откат.

    Пустой PATH не запрещает запуск: ``/usr/bin/git`` по абсолютному пути его
    обходит. Поэтому закрыт сам вход: ``Popen.__init__`` (через него идут
    ``subprocess.run`` и все ``asyncio.create_subprocess_*``), ``os.system``,
    ``os.posix_spawn*``, ``os.fork*``. Попытка пишется в журнал и отклоняется
    ДО системного вызова: процесс не рождается.
    """
    import subprocess

    saved: list[tuple[Any, str, Any]] = []

    def swap(owner: Any, name: str, label: str) -> None:
        if not hasattr(owner, name):
            return
        saved.append((owner, name, getattr(owner, name)))

        def refuse(*args: Any, **kwargs: Any) -> Any:
            audit(label)
            raise PermissionError(
                f"{label}: запуск процессов запрещён в eval-песочнице"
            )

        setattr(owner, name, refuse)

    swap(subprocess.Popen, "__init__", "exec.subprocess.Popen")
    for name in ("system", "posix_spawn", "posix_spawnp", "fork", "forkpty"):
        swap(os, name, f"exec.os.{name}")

    def restore() -> None:
        for owner, name, original in reversed(saved):
            setattr(owner, name, original)
        saved.clear()

    return restore


def configure(app_mod: Any, registry: Any, audit: Callable[[str], None]) -> None:
    """Подменить в приложении всё, что ходит наружу. Вынесено ради теста."""
    from fastapi import HTTPException

    from hub.services import ac_tests, validation_run

    def register_denied() -> None:
        install_denied(registry, audit)

    def no_poller(app: Any) -> "asyncio.Task[None]":
        # Поллер — единственный источник фоновых проб наружу и автодействий.
        return asyncio.ensure_future(asyncio.sleep(10**9))

    def refuse_route(label: str) -> Callable[..., Any]:
        async def refuse(*args: Any, **kwargs: Any) -> Any:
            audit(label)
            raise HTTPException(
                403, detail=f"{label}: запуск проверок запрещён в eval-песочнице"
            )

        return refuse

    app_mod._register_plugins = register_denied
    app_mod.start_poller = no_poller
    # Маршруты, запускающие код на хосте хаба (доступны тестовому human):
    # заглушка пишет попытку и отказывает. Раннеры по умолчанию закрыты так же —
    # на случай вызова не через маршрут.
    app_mod.run_validation_commands = refuse_route(
        "validation_run.run_validation_commands"
    )
    app_mod.run_ac_tests = refuse_route("ac_tests.run_ac_tests")
    validation_run.default_validation_runner = _denied_runner(
        audit, "validation_run.default_validation_runner"
    )
    ac_tests.default_test_runner = _denied_runner(audit, "ac_tests.default_test_runner")
    register_denied()


def main(argv: list[str]) -> int:
    fd, audit_path = int(argv[1]), argv[2]

    import uvicorn

    from hub import app as app_mod
    from hub.integrations.registry import plugins

    audit = _file_audit(audit_path)
    configure(app_mod, plugins, audit)
    deny_execution(audit)
    uvicorn.run(app_mod.app, fd=fd, log_level="warning", lifespan="on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
