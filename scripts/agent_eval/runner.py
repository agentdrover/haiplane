"""Runner оценки реального стюарда на канарейках (#1222).

Что делает: берёт манифест канареек (#1221), для каждого case собирает РАБОЧИЙ
промпт стюарда (тот же builder, что и в проде, см. ``contract.working_prompt_text``)
плюс синтетический пакет доказательств, отправляет его через транспорт и
отдаёт ответ тому же оценщику ``steward_canaries.evaluate`` (#1108) через
``contract.build_run``. Второго судьи — LLM или иного — здесь нет.

Чего не делает: не ходит в хаб и в БД, не получает доступа к хабу (агенту не
передаётся ни токен, ни MCP), не пишет в production. Живой транспорт включается
ТОЛЬКО явным ``live_opt_in=True``; без него вызова нет вообще.

Лимиты владельца (10.10) зашиты потолками и не поднимаются без правки кода:
не больше 20 канареек за прогон, 3 прогонов на задачу, 2 000 000 токенов.
Превышение — не вызов, а ``infrastructure_error``/``incomplete`` с причиной.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent_eval import contract as ct
from hub import config
from hub.services.steward_canaries import Canary

#: Адрес, который не резолвится (RFC 6761): промпт идёт строго по рабочему
#: шаблону, но попасть на настоящий хаб не может.
EVAL_HUB_BASE = "https://hub.invalid"

# Жёсткие потолки. Значения владельца 10.10; поднять можно только правкой кода.
HARD_MAX_CASES = 20
HARD_MAX_RUNS_PER_TASK = 3
HARD_MAX_TOKENS = 2_000_000
HARD_MAX_RETRIES = 2
HARD_MAX_CALLS = 180

#: Запас на ответ при оценке стоимости вызова, пока расход не известен.
TOKEN_RESERVE = 2_000

REDACTED = "[redacted]"
_SECRET_NAME_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")

_EVAL_SUFFIX = (
    "\n\n--- ОЦЕНОЧНЫЙ ПРОГОН (синтетический пакет) ---\n"
    "Хаб недоступен и обращаться к нему не нужно: инструментов, команд и "
    "сети не используй. Пакет доказательств ниже заменяет ответ "
    "steward-evidence. Верни ТОЛЬКО один JSON-объект "
    '{"verdict": "approve|changes_requested|escalate", '
    '"confidence": "low|medium|high"}.\n'
    "ПАКЕТ ДОКАЗАТЕЛЬСТВ:\n"
)


def _positive(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name}: не число")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name}: должно быть положительным")


@dataclass(frozen=True)
class Limits:
    """Лимиты прогона. Выше потолков владельца объект не создаётся."""

    max_cases: int = HARD_MAX_CASES
    max_runs_per_task: int = HARD_MAX_RUNS_PER_TASK
    max_tokens: int = HARD_MAX_TOKENS
    max_calls: int = 60
    max_retries: int = 1
    call_timeout_s: float = 300.0
    deadline_s: float = 3600.0
    retry_delay_s: float = 1.0

    def __post_init__(self) -> None:
        for name, ceiling in (
            ("max_cases", HARD_MAX_CASES),
            ("max_runs_per_task", HARD_MAX_RUNS_PER_TASK),
            ("max_tokens", HARD_MAX_TOKENS),
            ("max_calls", HARD_MAX_CALLS),
        ):
            value = getattr(self, name)
            _positive(name, value)
            if value > ceiling:
                raise ValueError(f"{name}={value} выше потолка владельца {ceiling}")
        _positive("call_timeout_s", self.call_timeout_s)
        _positive("deadline_s", self.deadline_s)
        if not 0 <= self.max_retries <= HARD_MAX_RETRIES:
            raise ValueError(f"max_retries вне 0..{HARD_MAX_RETRIES}")
        if self.retry_delay_s < 0 or not math.isfinite(self.retry_delay_s):
            raise ValueError("retry_delay_s: отрицательное или нечисло")


@dataclass(frozen=True)
class ProviderRequest:
    """Всё, что видит провайдер. Секретов и доступа к хабу здесь нет по построению."""

    case_id: str
    run_id: str
    session_id: str
    model: str
    prompt: str
    params: dict[str, Any]
    attempt: int


@dataclass(frozen=True)
class ProviderReply:
    text: str = ""
    usage: dict[str, Any] | None = None
    error: str | None = None
    #: Повторять можно только то, что заведомо не оставило живого агента.
    retryable: bool = False


class Transport(Protocol):
    #: Живой (платный) транспорт обязан выставить True; без opt-in не вызывается.
    live: bool

    async def complete(self, request: ProviderRequest) -> ProviderReply: ...


class RunLedger:
    """Счётчик прогонов на задачу; с ``path`` переживает процесс."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._counts: dict[str, int] = {}
        if path is not None and path.is_file():
            try:
                raw = json.loads(path.read_text())
                self._counts = {str(k): int(v) for k, v in raw.items()}
            except (ValueError, TypeError, AttributeError):
                # Нечитаемый счёт не равен нулю: лучше отказать, чем сбросить.
                self._counts = {"*": HARD_MAX_RUNS_PER_TASK}

    def used(self, task_id: int) -> int:
        return max(self._counts.get(str(task_id), 0), self._counts.get("*", 0))

    def record(self, task_id: int) -> None:
        self._counts[str(task_id)] = self._counts.get(str(task_id), 0) + 1
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._counts, sort_keys=True))


@dataclass
class EvalResult:
    runs: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    calls: int = 0
    tokens_used: int = 0
    #: Запрошенные прогоны не выполнены целиком (лимит прогонов на задачу).
    truncated: bool = False

    @property
    def status(self) -> str:
        statuses = [r["status"] for r in self.runs]
        if ct.QUALITY_FAILED in statuses:
            return ct.QUALITY_FAILED
        if not self.runs or self.truncated or ct.INCOMPLETE in statuses:
            return ct.INCOMPLETE
        if ct.INFRASTRUCTURE_ERROR in statuses:
            return ct.INFRASTRUCTURE_ERROR
        return ct.PASSED


def working_prompt_text() -> str:
    return ct.working_prompt_text()


def case_prompt(canary: Canary) -> str:
    facts = json.dumps(canary.facts, ensure_ascii=False, sort_keys=True)
    return working_prompt_text() + _EVAL_SUFFIX + facts


def estimate_tokens(prompt: str) -> int:
    return math.ceil(len(prompt) / 3) + TOKEN_RESERVE


def parse_judgement(text: str) -> dict[str, Any] | None:
    """Первый JSON-объект с ``verdict``; из него берутся только verdict/confidence."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            out = {"verdict": obj.get("verdict")}
            if "confidence" in obj:
                out["confidence"] = obj.get("confidence")
            return out
    return None


def _known_secrets() -> list[str]:
    found = {
        (config.CURSOR_API_KEY or "").strip(),
        (config.STEWARD_HUB_TOKEN or "").strip(),
    }
    for name, value in os.environ.items():
        if any(part in name.upper() for part in _SECRET_NAME_PARTS):
            found.add(value.strip())
    return sorted((s for s in found if len(s) >= 8), key=len, reverse=True)


def redact(value: Any, secrets: Iterable[str]) -> Any:
    """Глубокая замена известных секретов; ключи словарей тоже проверяются."""
    secrets = list(secrets)

    def clean(text: str) -> str:
        for secret in secrets:
            text = text.replace(secret, REDACTED)
        return text

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            return clean(item)
        if isinstance(item, dict):
            return {clean(str(k)): walk(v) for k, v in item.items()}
        if isinstance(item, list | tuple):
            return [walk(v) for v in item]
        return item

    return walk(value)


class _Budget:
    """Общий счёт всего вызова ``run_eval``: вызовы, токены, срок."""

    def __init__(self, limits: Limits, clock: Callable[[], float]) -> None:
        self.limits = limits
        self.clock = clock
        self.started = clock()
        self.calls = 0
        self.tokens = 0

    def refusal(self, estimate: int) -> str | None:
        if self.calls >= self.limits.max_calls:
            return f"max_calls: достигнут лимит {self.limits.max_calls} вызовов"
        if self.clock() - self.started >= self.limits.deadline_s:
            return f"deadline: вышел общий срок {self.limits.deadline_s} с"
        if self.tokens + estimate > self.limits.max_tokens:
            return (
                f"max_tokens: израсходовано {self.tokens}, следующий вызов "
                f"не укладывается в {self.limits.max_tokens}"
            )
        return None

    def charge(self, reply: ProviderReply, estimate: int) -> None:
        self.calls += 1
        self.tokens += _tokens_of(reply.usage) or estimate


def _tokens_of(usage: dict[str, Any] | None) -> int | None:
    if not isinstance(usage, dict):
        return None
    parts = [usage.get(k) for k in ct.USAGE_KEYS]
    if any(not isinstance(p, int) or isinstance(p, bool) or p < 0 for p in parts):
        return None
    return sum(parts) or None


async def _guarded(
    transport: Transport, request: ProviderRequest, timeout: float
) -> ProviderReply:
    try:
        return await asyncio.wait_for(transport.complete(request), timeout)
    except TimeoutError:
        return ProviderReply(error=f"timeout: провайдер не ответил за {timeout} с")
    except Exception as exc:  # noqa: BLE001 - любой сбой транспорта = не суждение
        # Только класс: текст исключения может нести ключ или адрес.
        return ProviderReply(error=f"transport_error: {type(exc).__name__}")


def _response(case_id: str, reply: ProviderReply, started: float, now: float):
    judgement = parse_judgement(reply.text)
    latency = max(0.0, (now - started) * 1000.0)
    return ct.CaseResponse(
        case_id=case_id,
        judgement=judgement,
        latency_ms=latency,
        usage=reply.usage if isinstance(reply.usage, dict) else None,
        error=reply.error or None,
    )


async def _call_case(
    *,
    transport: Transport,
    budget: _Budget,
    base: ProviderRequest,
    sleep: Callable[[float], Awaitable[None]],
) -> ct.CaseResponse:
    estimate = estimate_tokens(base.prompt)
    limits = budget.limits
    reply = ProviderReply(error="not_called")
    started = budget.clock()
    for attempt in range(1, limits.max_retries + 2):
        why = budget.refusal(estimate)
        if why:
            return ct.CaseResponse(case_id=base.case_id, error=why)
        request = ProviderRequest(**{**base.__dict__, "attempt": attempt})
        reply = await _guarded(transport, request, limits.call_timeout_s)
        budget.charge(reply, estimate)
        if not (reply.error and reply.retryable) or attempt > limits.max_retries:
            break
        await sleep(limits.retry_delay_s)
    return _response(base.case_id, reply, started, budget.clock())


def _meta(
    transport: Transport,
    *,
    model: str,
    commit: str,
    run_id: str,
    prompt_hash: str,
    catalog_hash: str,
) -> ct.RunMeta:
    return ct.RunMeta(
        commit=commit,
        provider=str(getattr(transport, "provider", "") or "test"),
        model=model,
        params=dict(getattr(transport, "params", None) or {}),
        prompt_hash=prompt_hash,
        catalog_hash=catalog_hash,
        run_id=run_id,
        session_id=f"{run_id}:session",
    )


async def _one_run(
    *,
    transport: Transport,
    manifest: ct.Manifest,
    meta: ct.RunMeta,
    budget: _Budget,
    live_opt_in: bool,
    sleep: Callable[[float], Awaitable[None]],
    notes: list[str],
) -> list[ct.CaseResponse]:
    responses: list[ct.CaseResponse] = []
    blocked = (
        "live_opt_in: живой транспорт без явного opt-in не вызывается"
        if getattr(transport, "live", True) and not live_opt_in
        else None
    )
    if blocked:
        notes.append(blocked)
    for index, (case, canary) in enumerate(
        zip(manifest.cases, manifest.canaries, strict=True)
    ):
        if index >= budget.limits.max_cases:
            notes.append(
                f"max_cases: {len(manifest.cases) - index} case не запущено "
                f"(лимит {budget.limits.max_cases})"
            )
            break
        if blocked:
            responses.append(ct.CaseResponse(case_id=case.case_id, error=blocked))
            continue
        base = ProviderRequest(
            case_id=case.case_id,
            run_id=meta.run_id,
            session_id=f"{meta.run_id}:{index}:{uuid.uuid4().hex}",
            model=meta.model,
            prompt=case_prompt(canary),
            params=dict(meta.params),
            attempt=1,
        )
        responses.append(
            await _call_case(transport=transport, budget=budget, base=base, sleep=sleep)
        )
    return responses


async def run_eval(
    *,
    transport: Transport,
    model: str,
    commit: str,
    task_id: int,
    canaries: list[Canary] | None = None,
    limits: Limits | None = None,
    live_opt_in: bool = False,
    repeats: int = 1,
    ledger: RunLedger | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> EvalResult:
    limits = limits or Limits()
    ledger = ledger if ledger is not None else RunLedger()
    manifest = ct.build_manifest(canaries)
    prompt_hash = ct.working_prompt_hash()
    catalog_hash = await ct.working_catalog_hash()
    budget = _Budget(limits, clock)
    result = EvalResult()
    secrets = _known_secrets()

    room = max(0, limits.max_runs_per_task - ledger.used(task_id))
    if repeats > room:
        result.truncated = True
        result.notes.append(
            f"max_runs_per_task: запрошено {repeats}, доступно {room} "
            f"(лимит {limits.max_runs_per_task} на задачу #{task_id})"
        )
    for _ in range(min(repeats, room)):
        run_id = uuid.uuid4().hex
        meta = _meta(
            transport,
            model=model,
            commit=commit,
            run_id=run_id,
            prompt_hash=prompt_hash,
            catalog_hash=catalog_hash,
        )
        calls_before = budget.calls
        responses = await _one_run(
            transport=transport,
            manifest=manifest,
            meta=meta,
            budget=budget,
            live_opt_in=live_opt_in,
            sleep=sleep,
            notes=result.notes,
        )
        if budget.calls > calls_before:
            ledger.record(task_id)
        result.runs.append(redact(ct.build_run(manifest, responses, meta), secrets))
    result.calls, result.tokens_used = budget.calls, budget.tokens
    result.notes = list(dict.fromkeys(result.notes))
    return result


def write_artifacts(result: EvalResult, out_dir: Path) -> list[Path]:
    """По файлу на прогон; секреты вычищаются ещё раз непосредственно перед записью."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    secrets = _known_secrets()
    paths: list[Path] = []
    for run in result.runs:
        path = out_dir / f"eval-run-{run['meta']['run_id']}.json"
        text = json.dumps(
            redact(run, secrets), ensure_ascii=False, indent=2, sort_keys=True
        )
        path.write_text(text + "\n")
        paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> int:
    """CLI. Без ``--live`` не делает ни одного вызова и завершается кодом 2."""
    parser = argparse.ArgumentParser(description="Оценка стюарда на канарейках (#1222)")
    parser.add_argument(
        "--live", action="store_true", help="явный opt-in на платные вызовы"
    )
    parser.add_argument("--model", default="")
    parser.add_argument("--repo-url", default="")
    parser.add_argument("--task-id", type=int, default=0)
    parser.add_argument("--commit", default="")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--out", default="eval-artifacts")
    args = parser.parse_args(argv)
    if not args.live:
        print(
            "live opt-in не дан (--live): платные вызовы не выполняются",
            file=sys.stderr,
        )
        return 2
    missing = [n for n in ("model", "repo_url", "commit") if not getattr(args, n)]
    if missing or args.task_id <= 0:
        print(f"не заданы: {missing or ['task-id']}", file=sys.stderr)
        return 2
    from agent_eval.cursor_adapter import CursorTransport

    result = asyncio.run(
        run_eval(
            transport=CursorTransport(repo_url=args.repo_url),
            model=args.model,
            commit=args.commit,
            task_id=args.task_id,
            live_opt_in=True,
            repeats=args.repeats,
            ledger=RunLedger(Path(args.out) / "ledger.json"),
        )
    )
    write_artifacts(result, Path(args.out))
    print(result.status)
    return 0 if result.status == ct.PASSED else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
