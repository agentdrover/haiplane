"""Одноразовый Hub для eval и запись синтетических MCP-трасс (#1223).

Зачем. Реальные вызовы lifecycle-инструментов нельзя оценивать на production
задачах, а штатная телеметрия (``mcp_call_events``) намеренно не хранит
payload. Здесь поднимается отдельный процесс хаба: временная БД, loopback
endpoint, свои тестовые principals, синтетические задачи. Внешние адаптеры в
нём — заглушки (``sandbox_server``), процесс не получает ни production URL, ни
токенов, ни даже PATH с настоящими ``git``/``gh``.

Чего нет намеренно: сбора production-данных, миграций, таблиц под трассу.
Трасса живёт в памяти recorder-а и по запросу пишется в файл, который выбирает
вызывающий; в БД песочницы она не попадает.

Порядок отказов (deny-by-default):

1. ``check_environment`` — ДО создания каталога и процесса. production URL
   (не loopback) или любой hub/forge/Cursor-токен в окружении = отказ запуска,
   а не использование и не молчаливое игнорирование;
2. ``SyntheticSession`` отказывается говорить с адресом не из loopback;
3. дочернему процессу окружение собирается с нуля, из белого списка.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import weakref
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from hub import brand
from hub.integrations import cursor_cloud

REDACTED = "[redacted]"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Имена вне префиксов хаба, наличие которых запрещает запуск (токены форжей
#: и облачного агента). Совпадение по имени, значение не читается.
_FORBIDDEN_EXACT = frozenset(
    {
        "GITVERSE_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "CURSOR_API_KEY",
        "CURSOR_REVIEWER_HUB_TOKEN",
    }
)
#: Любое имя, оканчивающееся на HUB_TOKEN(S), под любым префиксом.
_FORBIDDEN_HUB_TOKEN = re.compile(r"(^|_)HUB_TOKENS?$")
#: Последний сегмент имени настройки хаба, означающий секрет: например
#: HUB_BOOTSTRAP_ADMIN_TOKEN, HUB_CSRF_SECRET, STEWARD_HUB_TOKEN. Бюджеты вида
#: REVIEW_TOKEN_BUDGET под правило не попадают: секрет стоит в конце имени.
_SECRET_LAST_SEGMENT = frozenset({"TOKEN", "TOKENS", "SECRET", "PASSWORD", "KEY"})

#: Префиксы настроек хаба: действующий и снятые с поддержки (#964). Снятый
#: префикс хаб сам не читает, но оператор мог оставить его drop-in'ом, и
#: он выдаёт production-доступ так же, как действующий.
_HUB_PREFIXES: tuple[str, ...] = (brand.ENV_PREFIX, *brand.RETIRED_ENV_PREFIXES)

#: Ключи JSON, значения которых вычищаются независимо от содержимого.
_SECRET_KEY = re.compile(
    r"(token|secret|password|passwd|authorization|api[_-]?key|bearer|cookie)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")

_START_TIMEOUT = 90.0
_TERM_GRACE = 5.0


class SandboxRefused(RuntimeError):
    """Песочница не запускается или не говорит с адресом: небезопасно."""


class SandboxStartError(RuntimeError):
    """Хаб песочницы не поднялся. Всё уже освобождено."""


def _is_loopback_host(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_loopback_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and _is_loopback_host(parts.hostname or "")


def _forbidden_reason(name: str, value: str) -> str | None:
    up = name.upper()
    for prefix in _HUB_PREFIXES:
        if up == prefix + "HUB_URL":
            if not _is_loopback_url(value):
                return f"{name} указывает не на loopback"
            return None
    if up in _FORBIDDEN_EXACT or _FORBIDDEN_HUB_TOKEN.search(up):
        return f"{name} содержит токен"
    for prefix in _HUB_PREFIXES:
        if up.startswith(prefix) and up.rsplit("_", 1)[-1] in _SECRET_LAST_SEGMENT:
            return f"{name} содержит секрет"
    return None


def check_environment(env: Mapping[str, str], *, allow: Iterable[str] = ()) -> None:
    """Отказать, если окружение выглядит как production. Значения не читаются.

    ``allow`` — явный список ИМЁН, которые вызывающий разрешает оставить в
    окружении (по умолчанию пуст). Разрешённое имя в окружение хаба-ребёнка
    всё равно не попадает: оно только не мешает запуску.
    """
    allowed = {a.upper() for a in allow}
    problems: list[str] = []
    for name, value in env.items():
        value = str(value).strip()
        if not value or name.upper() in allowed:
            continue
        reason = _forbidden_reason(name, value)
        if reason:
            problems.append(reason)
    if problems:
        raise SandboxRefused(
            "sandbox не запускается в окружении с production-доступом: "
            + "; ".join(sorted(problems))
        )


# --------------------------------------------------------------------------
# Recorder
# --------------------------------------------------------------------------


def _scrub_text(text: str, secrets_: Iterable[str]) -> str:
    for secret in sorted({s for s in secrets_ if s}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = cursor_cloud.scrub_secrets(text)
    return _BEARER.sub(REDACTED, text)


def redact(value: Any, secrets_: Iterable[str]) -> Any:
    """Копия JSON-значения без секретов: по значению и по имени ключа."""
    pool = list(secrets_)
    if isinstance(value, str):
        return _scrub_text(value, pool)
    if isinstance(value, Mapping):
        return {
            _scrub_text(str(k), pool): (
                REDACTED if _SECRET_KEY.search(str(k)) else redact(v, pool)
            )
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v, pool) for v in value]
    return value


@dataclass(frozen=True)
class TraceCall:
    seq: int
    session_id: str
    tool: str
    args: Any
    result: Any
    ok: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "session_id": self.session_id,
            "tool": self.tool,
            "args": self.args,
            "result": self.result,
            "ok": self.ok,
        }


@dataclass
class TraceRecorder:
    """Трасса синтетической сессии. Секреты вычищаются при записи и при выдаче."""

    secrets: list[str] = field(default_factory=list)
    calls: list[TraceCall] = field(default_factory=list)

    def add_secret(self, value: str) -> None:
        if value and value not in self.secrets:
            self.secrets.append(value)

    def record(
        self, *, session_id: str, tool: str, args: Any, result: Any, ok: bool
    ) -> TraceCall:
        call = TraceCall(
            seq=len(self.calls) + 1,
            session_id=redact(session_id, self.secrets),
            tool=redact(tool, self.secrets),
            args=redact(args, self.secrets),
            result=redact(result, self.secrets),
            ok=ok,
        )
        self.calls.append(call)
        return call

    def to_json(self) -> str:
        # Второй проход: секрет, объявленный ПОСЛЕ записи вызова, тоже уйдёт.
        body = {"calls": [c.as_dict() for c in self.calls]}
        return _scrub_text(
            json.dumps(redact(body, self.secrets), ensure_ascii=False, indent=2),
            self.secrets,
        )


def _parse_mcp_body(resp: httpx.Response) -> dict[str, Any]:
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"error": {"message": "unreadable MCP response"}}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


class SyntheticSession:
    """MCP-клиент одной синтетической сессии; каждый tools/call пишется в трассу."""

    def __init__(
        self, base_url: str, token: str, session_id: str, recorder: TraceRecorder
    ) -> None:
        if not _is_loopback_url(base_url):
            raise SandboxRefused("SyntheticSession говорит только с loopback")
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self._token = token
        self._recorder = recorder
        recorder.add_secret(token)
        self._next_id = 0

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        args = dict(arguments or {})
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
            "X-Eval-Session": self.session_id,
        }
        ok, result = False, None
        try:
            async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
                resp = await client.post(
                    f"{self.base_url}/mcp", json=payload, headers=headers
                )
            if resp.status_code != 200:
                result = {"http_status": resp.status_code}
            else:
                body = _parse_mcp_body(resp)
                if "error" in body:
                    result = {"error": body["error"]}
                else:
                    result = body.get("result")
                    ok = not (isinstance(result, dict) and result.get("isError"))
        finally:
            self._recorder.record(
                session_id=self.session_id,
                tool=name,
                args=args,
                result=result,
                ok=ok,
            )
        return result


# --------------------------------------------------------------------------
# Sandbox
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    token: str


def _descendants(root_pid: int) -> list[int]:
    """PID всех потомков процесса (любой глубины, любых сессий), без самого root.

    Читается таблица процессов целиком: внук в отдельной сессии (setsid) не
    состоит в группе ребёнка, и убийство группы его не достанет.
    """
    try:
        out = subprocess.run(  # nosec B603 - fixed argv
            ["/bin/ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: list[int] = []
    stack = [root_pid]
    while stack:
        for kid in children.get(stack.pop(), []):
            if kid not in found:
                found.append(kid)
                stack.append(kid)
    return found


def _kill(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _release(proc: subprocess.Popen[bytes] | None, root: Path | None) -> None:
    """Остановить процесс, его группу и ВСЕХ потомков; снести каталог. Идемпотентно."""
    if proc is not None:
        # Снимок потомков до остановки ребёнка: после его смерти они осиротеют
        # и по дереву уже не найдутся.
        offspring = _descendants(proc.pid) if proc.poll() is None else []
        if proc.poll() is None:
            for sig, wait in ((signal.SIGTERM, _TERM_GRACE), (signal.SIGKILL, 5.0)):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    proc.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
        for pid in reversed(offspring):
            _kill(pid, signal.SIGKILL)
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # остатки группы
        except (ProcessLookupError, PermissionError):
            pass
        proc.poll()
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)


class Sandbox:
    """``async with Sandbox(...) as box`` — одноразовый хаб на loopback."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        allow_env: Iterable[str] = (),
        base_dir: Path | str | None = None,
        extra_secrets: Iterable[str] = (),
        start_timeout: float = _START_TIMEOUT,
        server_script: Path | str | None = None,
    ) -> None:
        self._server_script = Path(
            server_script or Path(__file__).with_name("sandbox_server.py")
        )
        #: Дополнительные переменные ДЛЯ ХАБА-РЕБЁНКА. На проверку окружения
        #: родителя не влияют: проверяется фактический os.environ.
        self._extra_env = dict(env or {})
        self._allow_env = tuple(allow_env)
        self._base_dir = Path(base_dir) if base_dir is not None else None
        self._start_timeout = start_timeout
        self.recorder = TraceRecorder(secrets=[s for s in extra_secrets if s])
        self.principals: dict[str, Principal] = {}
        self.root: Path = Path()
        self.db_path: Path = Path()
        self.url = ""
        self.pid = 0
        self.closed = False
        self._proc: subprocess.Popen[bytes] | None = None
        self._finalizer: weakref.finalize | None = None
        self._child_env: dict[str, str] = {}
        self._audit_path: Path = Path()

    # -- жизненный цикл ---------------------------------------------------

    async def __aenter__(self) -> "Sandbox":
        # Фактическое окружение процесса В МОМЕНТ запуска (не при создании
        # объекта) плюс то, что вызывающий хочет дать ребёнку: словарь env не
        # может скрыть production из os.environ.
        check_environment(
            {**os.environ, **self._extra_env}, allow=self._allow_env
        )  # до любого побочного эффекта
        try:
            await self._start()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Освободить процесс, порт и каталог. Безопасно при отмене и повторе."""
        if self.closed:
            return
        self.closed = True
        if self._finalizer is not None:
            self._finalizer.detach()
        _release(self._proc, self.root if self.root != Path() else None)
        for principal in self.principals.values():
            self.recorder.add_secret(principal.token)

    async def _start(self) -> None:
        if self._base_dir is not None:
            self._base_dir.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="agent-eval-", dir=self._base_dir))
        self.db_path = self.root / "hub.db"
        (self.root / "home").mkdir()
        (self.root / "bin").mkdir()  # пустой PATH: настоящих git/gh у процесса нет
        self._audit_path = self.root / "denied-effects.jsonl"
        self._audit_path.touch()

        self.principals = {
            role: Principal(
                name=f"eval-{role}", role=role, token=secrets.token_urlsafe(24)
            )
            for role in ("human", "agent")
        }
        for principal in self.principals.values():
            self.recorder.add_secret(principal.token)

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_out = (self.root / "server.out").open("wb")
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(64)
            listener.set_inheritable(True)
            port = listener.getsockname()[1]
            self.url = f"http://127.0.0.1:{port}"
            self._child_env = self._build_child_env(port)
            self._proc = subprocess.Popen(
                [
                    sys.executable,
                    str(self._server_script),
                    str(listener.fileno()),
                    str(self._audit_path),
                ],
                env=self._child_env,
                cwd=self.root,
                pass_fds=(listener.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=server_out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            listener.close()  # сокет остался у ребёнка
            server_out.close()
        self.pid = self._proc.pid
        self._finalizer = weakref.finalize(self, _release, self._proc, self.root)
        await self._wait_ready()

    def child_environment(self) -> dict[str, str]:
        return dict(self._child_env)

    def _build_child_env(self, port: int) -> dict[str, str]:
        """Окружение процесса хаба — с нуля. Из родителя не берётся ничего."""
        tokens = ",".join(
            f"{p.name}:{p.token}:{p.role}" for p in self.principals.values()
        )
        prefix = brand.ENV_PREFIX
        return {
            **{
                k: v
                for k, v in self._extra_env.items()
                if k.upper() not in {a.upper() for a in self._allow_env}
            },
            "PATH": str(self.root / "bin"),
            "HOME": str(self.root / "home"),
            "LANG": "C.UTF-8",
            "PYTHONPATH": os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "scripts")]),
            "PYTHONDONTWRITEBYTECODE": "1",
            prefix + "HUB_HOME": str(self.root / "home"),
            prefix + "HUB_DB": str(self.db_path),
            prefix + "HUB_HOST": "127.0.0.1",
            prefix + "HUB_PORT": str(port),
            prefix + "HUB_URL": self.url,
            prefix + "HUB_TOKENS": tokens,
            prefix + "TRANSCRIPTS_DIR": str(self.root / "transcripts"),
        }

    async def _wait_ready(self) -> None:
        assert self._proc is not None
        deadline = time.monotonic() + self._start_timeout
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    raise SandboxStartError(
                        f"hub песочницы завершился с кодом {self._proc.returncode}: "
                        + self._server_output()
                    )
                try:
                    resp = await client.get(f"{self.url}/healthz")
                    if resp.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
        raise SandboxStartError(
            f"hub песочницы не ответил за {self._start_timeout:g} с: "
            + self._server_output()
        )

    def _server_output(self) -> str:
        try:
            text = (self.root / "server.out").read_text(errors="replace")[-1500:]
        except OSError:
            return ""
        return _scrub_text(text, self.recorder.secrets)

    # -- работа -----------------------------------------------------------

    def session(self, role: str, *, session_id: str) -> SyntheticSession:
        principal = self.principals[role]
        return SyntheticSession(self.url, principal.token, session_id, self.recorder)

    async def seed_task(self, title: str, description: str = "") -> int:
        """Синтетическая задача от тестового человека. Возвращает её id."""
        human = self.principals["human"]
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            resp = await client.post(
                f"{self.url}/api/tasks",
                json={"title": title, "description": description},
                headers={"Authorization": f"Bearer {human.token}"},
            )
        if resp.status_code not in (200, 201):
            raise SandboxStartError(
                f"synthetic task refused: HTTP {resp.status_code} "
                + _scrub_text(resp.text[:300], self.recorder.secrets)
            )
        return int(resp.json()["id"])

    async def request(
        self, role: str, method: str, path: str, json_body: Any = None
    ) -> httpx.Response:
        """REST-вызов песочницы от тестового принципала (для проверок границ)."""
        principal = self.principals[role]
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            return await client.request(
                method,
                f"{self.url}{path}",
                json=json_body,
                headers={"Authorization": f"Bearer {principal.token}"},
            )

    def denied_effects(self) -> list[str]:
        """Какие внешние адаптеры пытались вызвать (только имена методов)."""
        try:
            lines = self._audit_path.read_text().splitlines()
        except OSError:
            return []
        return [json.loads(line)["effect"] for line in lines if line.strip()]

    def write_trace(self, path: Path | str) -> Path:
        out = Path(path)
        out.write_text(self.recorder.to_json(), encoding="utf-8")
        return out
