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
import fcntl
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
from hub.integrations import cursor_cloud
from hub.services.steward_canaries import VALID_VERDICTS, Canary, evaluate

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
    #: Сколько токенов бронируется под вызов, пока факт не известен: бюджет
    #: делится на число case, а не на размер промпта (агент читает репозиторий).
    call_token_reserve: int = HARD_MAX_TOKENS // HARD_MAX_CASES

    def __post_init__(self) -> None:
        for name, ceiling in (
            ("max_cases", HARD_MAX_CASES),
            ("max_runs_per_task", HARD_MAX_RUNS_PER_TASK),
            ("max_tokens", HARD_MAX_TOKENS),
            ("max_calls", HARD_MAX_CALLS),
            ("call_token_reserve", HARD_MAX_TOKENS),
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
    #: Срок runner-а на вызов: транспорт обязан уложиться раньше и сам остановить
    #: облачного агента, иначе runner отменит его на ходу.
    timeout_s: float = 300.0


@dataclass(frozen=True)
class ProviderReply:
    text: str = ""
    usage: dict[str, Any] | None = None
    error: str | None = None
    #: Повторять можно только то, что ДОКАЗАННО не создало агента.
    retryable: bool = False
    #: Доказано, что ничего не списано (отказ до создания).
    no_charge: bool = False
    #: Идентификаторы у провайдера — для ручного восстановления.
    refs: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    #: Живой (платный) транспорт обязан выставить True; без opt-in не вызывается.
    live: bool

    async def complete(self, request: ProviderRequest) -> ProviderReply: ...


def default_ledger_path() -> Path:
    """Общий ledger eval: каталог данных, не зависящий от ``--out``."""
    base = os.environ.get("HAIPLANE_EVAL_DATA_DIR") or str(
        Path.home() / ".haiplane-eval"
    )
    return Path(base) / "ledger.json"


class RunLedger:
    """Постоянный счёт прогонов и токенов под файловой блокировкой.

    Брони делаются ДО платного вызова и под ``flock``, поэтому параллельные
    процессы и задачи не могут вместе превысить лимиты. Нечитаемый файл —
    отказ, а не ноль.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else default_ledger_path()

    def _transact(self, change: Callable[[dict[str, Any]], Any]) -> Any:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = self._load()
                answer = change(state)
                if not state.get("corrupt"):
                    tmp = self._path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(state, sort_keys=True))
                    os.replace(tmp, self._path)
                return answer
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {"runs": {}, "tokens": 0}
        try:
            raw = json.loads(self._path.read_text())
            return {
                "runs": {str(k): int(v) for k, v in raw["runs"].items()},
                "tokens": int(raw["tokens"]),
            }
        except (ValueError, TypeError, KeyError, AttributeError):
            return {"corrupt": True}

    def reserve_run(self, task_id: int, max_runs: int) -> bool:
        def change(state: dict[str, Any]) -> bool:
            if state.get("corrupt"):
                return False
            used = state["runs"].get(str(task_id), 0)
            if used >= max_runs:
                return False
            state["runs"][str(task_id)] = used + 1
            return True

        return self._transact(change)

    def reserve_tokens(self, amount: int, max_tokens: int) -> bool:
        def change(state: dict[str, Any]) -> bool:
            if state.get("corrupt") or state["tokens"] + amount > max_tokens:
                return False
            state["tokens"] += amount
            return True

        return self._transact(change)

    def settle_tokens(self, reserved: int, actual: int) -> int:
        """Бронь заменяется фактом; возвращает общий счёт."""

        def change(state: dict[str, Any]) -> int:
            if state.get("corrupt"):
                return HARD_MAX_TOKENS + 1
            state["tokens"] = max(0, state["tokens"] - reserved + actual)
            return int(state["tokens"])

        return self._transact(change)

    def runs_used(self, task_id: int) -> int:
        return int(self._transact(lambda s: s.get("runs", {}).get(str(task_id), 0)))

    def tokens_used(self) -> int:
        return int(self._transact(lambda s: s.get("tokens", 0)))


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


def fixed_prompt_text() -> str:
    """Всё фиксированное, что уходит модели: рабочий шаблон + оценочная вставка."""
    return working_prompt_text() + _EVAL_SUFFIX


def fixed_prompt_hash() -> str:
    return ct._sha256(fixed_prompt_text())


def case_prompt(canary: Canary) -> str:
    facts = json.dumps(canary.facts, ensure_ascii=False, sort_keys=True)
    return fixed_prompt_text() + facts


def estimate_tokens(prompt: str) -> int:
    return math.ceil(len(prompt) / 3) + TOKEN_RESERVE


AMBIGUOUS = "ambiguous_answer"


def _json_objects(text: str) -> list[dict[str, Any]]:
    """Объекты верхнего уровня в тексте; вложенные внутрь найденного не берутся."""
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    index = 0
    while (index := text.find("{", index)) != -1:
        try:
            obj, end = decoder.raw_decode(text[index:])
        except ValueError:
            index += 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        index += end
    return found


def parse_judgement(text: str) -> dict[str, Any] | None:
    """Суждение из ответа: один вердикт — он; разные — ``ambiguous_answer``."""
    objects = [o for o in _json_objects(text or "") if "verdict" in o]
    if not objects:
        return None
    verdicts = {json.dumps(o.get("verdict"), sort_keys=True) for o in objects}
    if len(verdicts) > 1:
        return {"verdict": AMBIGUOUS}
    out = {"verdict": objects[0].get("verdict")}
    if "confidence" in objects[0]:
        out["confidence"] = objects[0].get("confidence")
    return out


def _known_secrets() -> list[str]:
    return cursor_cloud.known_secrets()


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
    """Счёт одного вызова ``run_eval``: вызовы, срок, остановка; токены — в ledger."""

    def __init__(
        self, limits: Limits, clock: Callable[[], float], ledger: RunLedger
    ) -> None:
        self.limits = limits
        self.clock = clock
        self.ledger = ledger
        self.started = clock()
        self.calls = 0
        self.tokens = 0
        #: (вид, сообщение): после остановки новые платные вызовы не начинаются.
        self.halt: tuple[str, str] | None = None

    def refusal(self) -> str | None:
        if self.calls >= self.limits.max_calls:
            return f"max_calls: достигнут лимит {self.limits.max_calls} вызовов"
        if self.clock() - self.started >= self.limits.deadline_s:
            return f"deadline: вышел общий срок {self.limits.deadline_s} с"
        return None

    def reserve(self, amount: int) -> str | None:
        if self.ledger.reserve_tokens(amount, self.limits.max_tokens):
            return None
        return (
            f"max_tokens: бронь {amount} не укладывается в общий лимит "
            f"{self.limits.max_tokens} (учтено {self.ledger.tokens_used()})"
        )

    def settle(self, reply: ProviderReply, reserved: int) -> int:
        """Бронь -> факт. Неизвестный расход не бесплатен и останавливает прогон."""
        self.calls += 1
        actual = _tokens_of(reply.usage)
        proven_free = reply.no_charge or reply.retryable
        charged = actual if actual is not None else (0 if proven_free else reserved)
        total = self.ledger.settle_tokens(reserved, charged)
        self.tokens += charged
        if total > self.limits.max_tokens:
            self.halt = (
                "budget_exceeded",
                f"израсходовано {total} токенов при лимите {self.limits.max_tokens}",
            )
        elif actual is None and not proven_free:
            self.halt = (
                "unknown_usage",
                "провайдер не назвал расход: следующие case не запускаются",
            )
        return charged


def _tokens_of(usage: dict[str, Any] | None) -> int | None:
    if not isinstance(usage, dict):
        return None
    parts = [usage.get(k) for k in ct.USAGE_KEYS]
    if any(not isinstance(p, int) or isinstance(p, bool) or p < 0 for p in parts):
        return None
    return sum(parts)


async def _guarded(
    transport: Transport, request: ProviderRequest, timeout: float
) -> ProviderReply:
    refs = getattr(transport, "refs", None)

    def known() -> dict[str, str]:
        return dict(refs.get(request.session_id, {})) if isinstance(refs, dict) else {}

    try:
        return await asyncio.wait_for(transport.complete(request), timeout)
    except TimeoutError:
        return ProviderReply(
            error=f"timeout: провайдер не ответил за {timeout} с", refs=known()
        )
    except Exception as exc:  # noqa: BLE001 - любой сбой транспорта = не суждение
        # Только класс: текст исключения может нести ключ или адрес.
        return ProviderReply(
            error=f"transport_error: {type(exc).__name__}", refs=known()
        )


@dataclass
class _Attempt:
    number: int
    reply: ProviderReply
    charged: int
    latency_ms: float

    def judgement(self) -> dict[str, Any] | None:
        return parse_judgement(self.reply.text)

    def record(self) -> dict[str, Any]:
        j = self.judgement()
        return {
            "attempt": self.number,
            "error": self.reply.error,
            "verdict": (j or {}).get("verdict"),
            "tokens": self.charged,
            "refs": self.reply.refs,
            "no_charge": self.reply.no_charge,
        }


def _missed(canary: Canary, judgement: dict[str, Any] | None) -> bool:
    """Вердикт из словаря, который оценщик #1108 НЕ засчитывает за поимку."""
    if judgement is None or judgement.get("verdict") not in VALID_VERDICTS:
        return False
    return not evaluate(canary, judgement).caught


def _pick(canary: Canary, attempts: list[_Attempt]) -> _Attempt:
    """Попытка, которая решает case: критический провал не прячется повтором."""
    for attempt in attempts:
        if _missed(canary, attempt.judgement()):
            return attempt
    for attempt in reversed(attempts):
        if attempt.judgement() is not None:
            return attempt
    return attempts[-1]


def _sum_usage(attempts: list[_Attempt]) -> dict[str, Any] | None:
    totals = {k: 0 for k in ct.USAGE_KEYS}
    for attempt in attempts:
        usage = attempt.reply.usage
        if usage is None and attempt.reply.no_charge:
            continue
        if _tokens_of(usage) is None:
            return None
        for key in ct.USAGE_KEYS:
            totals[key] += usage[key]
    return totals


def _aggregate(
    case_id: str, canary: Canary, attempts: list[_Attempt]
) -> ct.CaseResponse:
    chosen = _pick(canary, attempts)
    return ct.CaseResponse(
        case_id=case_id,
        judgement=chosen.judgement(),
        latency_ms=sum(a.latency_ms for a in attempts),
        usage=_sum_usage(attempts),
        error=chosen.reply.error or None,
    )


async def _call_case(
    *,
    transport: Transport,
    budget: _Budget,
    base: ProviderRequest,
    canary: Canary,
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[ct.CaseResponse, list[dict[str, Any]]]:
    limits = budget.limits
    reserve = max(limits.call_token_reserve, estimate_tokens(base.prompt))
    attempts: list[_Attempt] = []
    for number in range(1, limits.max_retries + 2):
        why = budget.refusal() or budget.reserve(reserve)
        if why:
            if attempts:
                break
            return ct.CaseResponse(case_id=base.case_id, error=why), []
        request = ProviderRequest(
            **{**base.__dict__, "attempt": number, "timeout_s": limits.call_timeout_s}
        )
        started = budget.clock()
        reply = await _guarded(transport, request, limits.call_timeout_s)
        charged = budget.settle(reply, reserve)
        latency = max(0.0, (budget.clock() - started) * 1000.0)
        attempts.append(_Attempt(number, reply, charged, latency))
        if (
            budget.halt
            or not (reply.error and reply.retryable)
            or number > limits.max_retries
        ):
            break
        await sleep(limits.retry_delay_s)
    return _aggregate(base.case_id, canary, attempts), [a.record() for a in attempts]


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
    blocked: str | None,
    sleep: Callable[[float], Awaitable[None]],
    notes: list[str],
) -> tuple[list[ct.CaseResponse], dict[str, list[dict[str, Any]]]]:
    responses: list[ct.CaseResponse] = []
    attempts: dict[str, list[dict[str, Any]]] = {}
    for index, (case, canary) in enumerate(
        zip(manifest.cases, manifest.canaries, strict=True)
    ):
        if index >= budget.limits.max_cases:
            notes.append(
                f"max_cases: {len(manifest.cases) - index} case не запущено "
                f"(лимит {budget.limits.max_cases})"
            )
            break
        if budget.halt:
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
        response, log_ = await _call_case(
            transport=transport, budget=budget, base=base, canary=canary, sleep=sleep
        )
        responses.append(response)
        if log_:
            attempts[case.case_id] = log_
    return responses, attempts


def _seal(
    run: dict[str, Any],
    attempts: dict[str, list[dict[str, Any]]],
    halt: tuple[str, str] | None,
) -> dict[str, Any]:
    """Остановка по бюджету делает прогон неполным, а не зелёным."""
    run["attempts"] = attempts
    if halt:
        run["problems"].append(
            {"kind": halt[0], "case_id": "", "path": "budget", "message": halt[1]}
        )
        run["status"] = ct.derive_status(run["summary"], run["problems"])
    return run


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
    prompt_hash = fixed_prompt_hash()
    catalog_hash = await ct.working_catalog_hash()
    budget = _Budget(limits, clock, ledger)
    result = EvalResult()
    secrets = _known_secrets()
    blocked = (
        "live_opt_in: живой транспорт без явного opt-in не вызывается"
        if getattr(transport, "live", True) and not live_opt_in
        else None
    )
    if blocked:
        result.notes.append(blocked)

    for done in range(repeats):
        if budget.halt:
            result.truncated = True
            result.notes.append(f"{budget.halt[0]}: {budget.halt[1]}")
            break
        # Слот прогона бронируется ДО первого платного вызова.
        if (
            not blocked
            and manifest.cases
            and not ledger.reserve_run(task_id, limits.max_runs_per_task)
        ):
            result.truncated = True
            result.notes.append(
                f"max_runs_per_task: прогон {done + 1} из {repeats} не начат "
                f"(лимит {limits.max_runs_per_task} на задачу #{task_id})"
            )
            break
        run_id = uuid.uuid4().hex
        meta = _meta(
            transport,
            model=model,
            commit=commit,
            run_id=run_id,
            prompt_hash=prompt_hash,
            catalog_hash=catalog_hash,
        )
        responses, attempts = await _one_run(
            transport=transport,
            manifest=manifest,
            meta=meta,
            budget=budget,
            blocked=blocked,
            sleep=sleep,
            notes=result.notes,
        )
        run = _seal(ct.build_run(manifest, responses, meta), attempts, budget.halt)
        result.runs.append(redact(run, secrets))
    if budget.halt:
        result.notes.append(f"{budget.halt[0]}: {budget.halt[1]}")
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
    parser.add_argument(
        "--allowed-repo",
        action="append",
        default=[],
        help="разрешённый eval-репозиторий owner/name (также HAIPLANE_EVAL_ALLOWED_REPOS)",
    )
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

    try:
        transport = CursorTransport(
            repo_url=args.repo_url, allowed_repos=args.allowed_repo or None
        )
    except ValueError as exc:
        print(f"репозиторий отклонён: {exc}", file=sys.stderr)
        return 2
    result = asyncio.run(
        run_eval(
            transport=transport,
            model=args.model,
            commit=args.commit,
            task_id=args.task_id,
            live_opt_in=True,
            repeats=args.repeats,
            ledger=RunLedger(),
        )
    )
    write_artifacts(result, Path(args.out))
    print(result.status)
    return 0 if result.status == ct.PASSED else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
