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


def configure(app_mod: Any, registry: Any, audit: Callable[[str], None]) -> None:
    """Подменить в приложении всё, что ходит наружу. Вынесено ради теста."""

    def register_denied() -> None:
        install_denied(registry, audit)

    def no_poller(app: Any) -> "asyncio.Task[None]":
        # Поллер — единственный источник фоновых проб наружу и автодействий.
        return asyncio.ensure_future(asyncio.sleep(10**9))

    app_mod._register_plugins = register_denied
    app_mod.start_poller = no_poller
    register_denied()


def main(argv: list[str]) -> int:
    fd, audit_path = int(argv[1]), argv[2]

    import uvicorn

    from hub import app as app_mod
    from hub.integrations.registry import plugins

    configure(app_mod, plugins, _file_audit(audit_path))
    uvicorn.run(app_mod.app, fd=fd, log_level="warning", lifespan="on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
