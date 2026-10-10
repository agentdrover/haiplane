"""Runner оценки реального стюарда на канарейках (#1222).

Платная модель здесь НЕ вызывается: транспорт везде тестовый. Эти тесты
доказывают поведение runner-а (рабочий промпт, оценщик #1108, лимиты, opt-in,
изоляцию), а не качество настоящей модели.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agent_eval import contract as ct  # noqa: E402
from agent_eval import runner as rn  # noqa: E402
from hub import config  # noqa: E402
from hub.integrations import cursor_cloud  # noqa: E402
from hub.services import steward_canaries as sc  # noqa: E402
from hub.services import steward_shadow  # noqa: E402

COMMIT = "b" * 40
MODEL = "test-judge-1"


@pytest.fixture(autouse=True)
def _eval_data_dir(tmp_path, monkeypatch):
    """Ledger по умолчанию живёт в каталоге данных eval — здесь это tmp."""
    monkeypatch.setenv("HAIPLANE_EVAL_DATA_DIR", str(tmp_path / "evaldata"))
    monkeypatch.delenv("HAIPLANE_EVAL_ALLOWED_REPOS", raising=False)


def _verdict_for(canary: sc.Canary) -> str:
    return "approve" if canary.expectation == sc.MUST_NOT_ESCALATE else "escalate"


def _good_text(verdict: str = "escalate", confidence: str = "high") -> str:
    return json.dumps({"verdict": verdict, "confidence": confidence})


def _right_text(request: rn.ProviderRequest) -> str:
    name = request.case_id.split(":", 1)[1]
    canary = next(c for c in sc.all_canaries() if c.name == name)
    return _good_text(_verdict_for(canary))


class FakeTransport:
    """Тестовый транспорт: ничего не отправляет, всё записывает."""

    def __init__(self, *, live: bool = False, script=None, usage=None, advance=None):
        self.live = live
        self.requests: list[rn.ProviderRequest] = []
        self._script = script
        self._usage = (
            {"input_tokens": 100, "output_tokens": 20} if usage is None else usage
        )
        self._advance = advance

    async def complete(self, request: rn.ProviderRequest) -> rn.ProviderReply:
        self.requests.append(request)
        if self._advance:
            self._advance()
        if self._script is not None:
            return await self._script(request, len(self.requests))
        return rn.ProviderReply(text=_right_text(request), usage=self._usage)


def _run(coro):
    return asyncio.run(coro)


def _canaries(n: int | None = None) -> list[sc.Canary]:
    items = sc.all_canaries()
    return items if n is None else items[:n]


def _eval(transport, **kw):
    kw.setdefault("model", MODEL)
    kw.setdefault("commit", COMMIT)
    kw.setdefault("task_id", 1222)
    kw.setdefault("ledger", _scratch_ledger())
    return _run(rn.run_eval(transport=transport, **kw))


def _scratch_ledger():
    import os
    import uuid

    return rn.RunLedger(
        Path(os.environ["HAIPLANE_EVAL_DATA_DIR"]) / f"scratch-{uuid.uuid4().hex}.json"
    )


def _fresh(tmp_path):
    """Чистый ledger на каждый сценарий: счёт токенов общий."""
    import uuid

    return rn.RunLedger(tmp_path / f"ledger-{uuid.uuid4().hex}.json")


def _all_cases(result):
    return [c for run in result.runs for c in run["cases"]]


def test_runner_uses_working_prompt():
    """AC-1: рабочий промпт + факты + модель уходят в транспорт; суд — оценщик #1108."""
    canaries = _canaries()
    transport = FakeTransport()
    result = _eval(transport, canaries=canaries)

    # ровно один вызов на case, никакого второго (судейского) вызова
    assert len(transport.requests) == len(canaries) > 0
    working = rn.working_prompt_text()
    assert working.startswith("Ты стюард гейта в Haiplane Hub.")
    assert working == steward_shadow._prompt(
        1,
        1,
        rn.EVAL_HUB_BASE,
        steward_shadow.delivery_block(1, "CODE", rn.EVAL_HUB_BASE),
    )
    # хэш в мета — хэш именно этого текста
    assert ct.working_prompt_hash() == ct._sha256(working)
    by_name = {c.name: c for c in canaries}
    for request in transport.requests:
        assert request.prompt.startswith(working)
        assert request.model == MODEL
        name = request.case_id.split(":", 1)[1]
        # весь промпт = фиксированный текст + факты канарейки, ничего третьего
        facts = json.dumps(by_name[name].facts, ensure_ascii=False, sort_keys=True)
        assert request.prompt == rn.fixed_prompt_text() + facts
    meta = result.runs[0]["meta"]
    # хэш покрывает весь фиксированный текст, уходящий модели (шаблон + вставка)
    assert meta["prompt_hash"] == ct._sha256(rn.fixed_prompt_text())
    assert meta["prompt_hash"] != ct.working_prompt_hash()
    assert rn.fixed_prompt_text().startswith(working) and len(
        rn.fixed_prompt_text()
    ) > len(working)
    assert meta["catalog_hash"] == _run(ct.working_catalog_hash())
    assert meta["model"] == MODEL and meta["commit"] == COMMIT

    # ответ уходит оценщику #1108: одобрение дефекта = quality_failed, и это
    # решает evaluate, а не вторая модель
    async def approve_all(request, n):
        return rn.ProviderReply(
            text=_good_text("approve"), usage={"input_tokens": 1, "output_tokens": 1}
        )

    approving = FakeTransport(script=approve_all)
    bad = _eval(approving, canaries=canaries)
    assert len(approving.requests) == len(canaries)  # судейских вызовов нет
    assert bad.status == ct.QUALITY_FAILED
    for case in _all_cases(bad):
        canary = by_name[case["case_id"].split(":", 1)[1]]
        expected = sc.evaluate(canary, {"verdict": "approve"}).caught
        assert (case["outcome"] == ct.PASSED) == expected
    assert bad.runs[0]["summary"]["false_approvals"] >= 1

    # судья, который ловит, где надо, и пропускает чистое, даёт passed
    async def right(request, n):
        canary = by_name[request.case_id.split(":", 1)[1]]
        return rn.ProviderReply(
            text=_good_text(_verdict_for(canary)),
            usage={"input_tokens": 1, "output_tokens": 1},
        )

    ok = _eval(FakeTransport(script=right), canaries=canaries)
    assert ok.status == ct.PASSED
    assert ct.validate_run(ok.runs[0]) == []

    # несколько разных вердиктов в ответе — не суждение (ambiguous_answer)
    two = '{"verdict": "escalate"} потом {"verdict": "approve"}'
    assert rn.parse_judgement(two) == {"verdict": rn.AMBIGUOUS}
    assert rn.parse_judgement(two + " " + two) == {"verdict": rn.AMBIGUOUS}
    same = '{"verdict": "escalate", "confidence": "high"} {"verdict": "escalate"}'
    assert rn.parse_judgement(same) == {"verdict": "escalate", "confidence": "high"}
    assert rn.parse_judgement("нет json") is None

    async def ambiguous(request, n):
        return rn.ProviderReply(text=two, usage={"input_tokens": 1, "output_tokens": 1})

    amb = _eval(FakeTransport(script=ambiguous), canaries=canaries[:2])
    assert amb.status == ct.INCOMPLETE
    assert all(c["outcome"] == ct.INCOMPLETE for c in _all_cases(amb))
    assert any(rn.AMBIGUOUS in p["message"] for p in amb.runs[0]["problems"])


async def _no_call(request, n):  # pragma: no cover - вызов = провал теста
    raise AssertionError("неразрешённый вызов")


def test_live_opt_in_and_limits(tmp_path):
    """AC-2: нет opt-in / исчерпан лимит / завис / ошибка — вызова нет, статус честный."""
    canaries = _canaries()
    n = len(canaries)

    # --- жёсткие потолки владельца ---
    assert (rn.HARD_MAX_RETRIES, rn.HARD_MAX_CALLS) == (2, 180)
    assert (rn.HARD_MAX_CASES, rn.HARD_MAX_RUNS_PER_TASK, rn.HARD_MAX_TOKENS) == (
        20,
        3,
        2_000_000,
    )
    default = rn.Limits()
    assert (default.max_cases, default.max_runs_per_task, default.max_tokens) == (
        20,
        3,
        2_000_000,
    )
    for bad in (
        {"max_cases": 21},
        {"max_runs_per_task": 4},
        {"max_tokens": 2_000_001},
        {"max_retries": 3},
        {"max_calls": 181},
        {"max_cases": 0},
        {"call_timeout_s": 0},
    ):
        with pytest.raises(ValueError):
            rn.Limits(**bad)

    # --- нет opt-in: живой транспорт не вызывается ни разу ---
    live = FakeTransport(live=True, script=_no_call)
    res = _eval(live, canaries=canaries, ledger=_fresh(tmp_path))
    assert live.requests == []
    assert res.calls == 0 and res.status == ct.INFRASTRUCTURE_ERROR
    assert all(c["outcome"] == ct.INFRASTRUCTURE_ERROR for c in _all_cases(res))
    assert all("live_opt_in" in c["error"] for c in _all_cases(res))
    assert any("live_opt_in" in note for note in res.notes)

    # транспорт без признака live считается живым
    class Unmarked:
        async def complete(self, request):  # pragma: no cover - вызов = провал
            raise AssertionError("неразрешённый вызов")

    assert _eval(Unmarked(), canaries=canaries[:1], ledger=_fresh(tmp_path)).calls == 0
    # с opt-in тот же транспорт работает
    live2 = FakeTransport(live=True)
    assert (
        len(
            _eval(
                live2, canaries=canaries, live_opt_in=True, ledger=_fresh(tmp_path)
            ).runs
        )
        == 1
    )
    assert len(live2.requests) == n

    # --- пустой набор не passed и не вызывает ---
    empty_t = FakeTransport(script=_no_call)
    empty = _eval(empty_t, canaries=[])
    assert empty_t.requests == [] and empty.status == ct.INCOMPLETE

    # --- лимит case: сверх лимита не вызывается, хвост incomplete ---
    t = FakeTransport()
    res = _eval(
        t, canaries=canaries, limits=rn.Limits(max_cases=2), ledger=_fresh(tmp_path)
    )
    assert len(t.requests) == 2
    assert res.status == ct.INCOMPLETE
    assert res.runs[0]["summary"]["incomplete"] == n - 2
    assert any("max_cases" in note for note in res.notes)

    # --- лимит прогонов на задачу: общий постоянный ledger ---
    ledger_path = tmp_path / "shared-ledger.json"
    t = FakeTransport()
    res = _eval(t, canaries=canaries[:2], repeats=4, ledger=rn.RunLedger(ledger_path))
    assert len(res.runs) == 3 and len(t.requests) == 6
    assert res.status == ct.INCOMPLETE
    assert any("max_runs_per_task" in note for note in res.notes)
    # новый объект (другой процесс) видит тот же счёт
    t2 = FakeTransport(script=_no_call)
    again = _eval(t2, canaries=canaries[:2], ledger=rn.RunLedger(ledger_path))
    assert t2.requests == [] and again.runs == [] and again.status == ct.INCOMPLETE
    other = _eval(
        FakeTransport(),
        canaries=canaries[:1],
        ledger=rn.RunLedger(ledger_path),
        task_id=999,
    )
    assert len(other.runs) == 1  # счёт по задаче, не общий
    # ledger по умолчанию тоже постоянный и не зависит от --out
    assert rn.RunLedger()._path == rn.default_ledger_path()
    assert rn.default_ledger_path().parent == tmp_path / "evaldata"
    for _ in range(4):
        _eval(FakeTransport(), canaries=canaries[:1], task_id=5, ledger=None)
    assert rn.RunLedger().runs_used(5) == 3
    assert rn.default_ledger_path().is_file()

    # параллельные резервы: больше трёх слотов не выдаётся никому
    race = rn.RunLedger(tmp_path / "race.json")
    with ThreadPoolExecutor(max_workers=12) as pool:
        got = list(pool.map(lambda _i: race.reserve_run(7, 3), range(12)))
    assert sum(got) == 3 and race.runs_used(7) == 3

    async def contenders():
        return await asyncio.gather(
            *(
                rn.run_eval(
                    transport=FakeTransport(),
                    model=MODEL,
                    commit=COMMIT,
                    task_id=8,
                    canaries=canaries[:1],
                    ledger=rn.RunLedger(tmp_path / "race2.json"),
                )
                for _ in range(6)
            )
        )

    outcomes = _run(contenders())
    assert sum(len(o.runs) for o in outcomes) == 3
    assert sum(o.truncated for o in outcomes) == 3

    # нечитаемый ledger — отказ, а не ноль
    broken_path = tmp_path / "broken.json"
    broken_path.write_text("{не json")
    t = FakeTransport(script=_no_call)
    res = _eval(t, canaries=canaries[:1], ledger=rn.RunLedger(broken_path))
    assert t.requests == [] and res.runs == [] and res.status == ct.INCOMPLETE
    # причина названа честно: не «лимит исчерпан» и не нулевой расход
    assert any(n.startswith("ledger_unreadable") for n in res.notes)
    assert not any("max_runs_per_task" in n for n in res.notes)
    assert rn.RunLedger(broken_path).runs_used(1222) is None
    assert rn.RunLedger(broken_path).tokens_used() is None

    # --- лимит токенов: бронь под вызов, новый вызов не начинается ---
    small = rn.Limits(max_tokens=50_000, call_token_reserve=20_000)
    t = FakeTransport(usage={"input_tokens": 15_000, "output_tokens": 5_000})
    res = _eval(t, canaries=canaries, limits=small, ledger=_fresh(tmp_path))
    assert len(t.requests) == 2 and res.tokens_used == 40_000  # 60 000 не допущено
    assert res.status == ct.INFRASTRUCTURE_ERROR
    tail = _all_cases(res)[2:]
    assert tail and all("max_tokens" in c["error"] for c in tail)

    # три запуска по 1 млн: суммарный лимит 2 млн держится между запусками
    shared = _fresh(tmp_path)
    million = {"input_tokens": 1_000_000, "output_tokens": 0}
    firsts = [
        _eval(FakeTransport(usage=million), canaries=canaries[:1], ledger=shared)
        for _ in range(2)
    ]
    assert all(len(r.runs) == 1 for r in firsts) and shared.tokens_used() == 2_000_000
    t = FakeTransport(usage=million, script=_no_call)
    third = _eval(t, canaries=canaries[:1], ledger=shared)
    assert t.requests == [] and third.status == ct.INFRASTRUCTURE_ERROR
    assert "max_tokens" in _all_cases(third)[0]["error"]

    # факт сверх лимита после ответа: не passed, причина budget_exceeded
    t = FakeTransport(usage={"input_tokens": 2_000_001, "output_tokens": 0})
    res = _eval(t, canaries=canaries, ledger=_fresh(tmp_path))
    assert len(t.requests) == 1  # дальше не идём
    assert res.status == ct.INCOMPLETE and res.runs[0]["status"] == ct.INCOMPLETE
    assert "budget_exceeded" in {p["kind"] for p in res.runs[0]["problems"]}
    assert ct.validate_run(res.runs[0]) == []

    # неизвестный расход: бронь списана, следующие case не запускаются
    async def silent(request, k):
        return rn.ProviderReply(text=_right_text(request), usage=None)

    t = FakeTransport(script=silent)
    led = _fresh(tmp_path)
    res = _eval(t, canaries=canaries, ledger=led)
    assert len(t.requests) == 1 and res.tokens_used == rn.Limits().call_token_reserve
    assert led.tokens_used() == res.tokens_used
    assert res.status == ct.INCOMPLETE
    assert "unknown_usage" in {p["kind"] for p in res.runs[0]["problems"]}

    # --- лимит вызовов ---
    t = FakeTransport()
    res = _eval(
        t, canaries=canaries, limits=rn.Limits(max_calls=4), ledger=_fresh(tmp_path)
    )
    assert len(t.requests) == 4 and res.calls == 4
    assert any("max_calls" in c["error"] for c in _all_cases(res)[4:])

    # --- общий срок ---
    clock = {"now": 0.0}
    t = FakeTransport(advance=lambda: clock.__setitem__("now", clock["now"] + 6.0))
    res = _eval(
        t,
        canaries=canaries,
        limits=rn.Limits(deadline_s=10.0),
        clock=lambda: clock["now"],
        ledger=_fresh(tmp_path),
    )
    assert len(t.requests) == 2
    assert any("deadline" in c["error"] for c in _all_cases(res)[2:])
    assert res.status == ct.INFRASTRUCTURE_ERROR

    # --- провайдер завис: таймаут, без повтора; расход неизвестен -> стоп ---
    async def hang(request, k):
        t.refs[request.session_id] = {"agent_id": "a9", "run_id": "r9"}
        await asyncio.sleep(5)

    t = FakeTransport(script=hang)
    t.refs = {}
    res = _eval(
        t,
        canaries=canaries[:2],
        limits=rn.Limits(call_timeout_s=0.05),
        ledger=_fresh(tmp_path),
    )
    assert len(t.requests) == 1 and t.requests[0].timeout_s == 0.05
    first = _all_cases(res)[0]
    assert first["outcome"] == ct.INFRASTRUCTURE_ERROR and "timeout" in first["error"]
    assert res.status == ct.INCOMPLETE  # второй case не запущен
    # прогон у провайдера не потерян: id остались в артефакте для ручной отмены
    kept = res.runs[0]["attempts"][first["case_id"]][0]["refs"]
    assert kept == {"agent_id": "a9", "run_id": "r9"}

    # --- ошибка провайдера: ограниченный повтор, не качество и не успех ---
    async def flaky(request, k):
        if k == 1:
            return rn.ProviderReply(error="http 503", retryable=True)
        return rn.ProviderReply(
            text=_right_text(request), usage={"input_tokens": 1, "output_tokens": 1}
        )

    t = FakeTransport(script=flaky)
    res = _eval(
        t,
        canaries=canaries[:1],
        limits=rn.Limits(max_retries=1),
        sleep=_instant,
        ledger=_fresh(tmp_path),
    )
    assert len(t.requests) == 2 and res.calls == 2
    assert [r.attempt for r in t.requests] == [1, 2]
    assert _all_cases(res)[0]["outcome"] != ct.INFRASTRUCTURE_ERROR
    recorded = res.runs[0]["attempts"][_all_cases(res)[0]["case_id"]]
    assert [a["attempt"] for a in recorded] == [1, 2]
    assert recorded[0]["error"] == "http 503" and recorded[1]["error"] is None

    async def broken(request, k):
        return rn.ProviderReply(error="http 503", retryable=True)

    t = FakeTransport(script=broken)
    res = _eval(
        t,
        canaries=canaries[:2],
        limits=rn.Limits(max_retries=2),
        sleep=_instant,
        ledger=_fresh(tmp_path),
    )
    assert len(t.requests) == 2 * (1 + 2)
    assert res.status == ct.INFRASTRUCTURE_ERROR
    assert all(c["outcome"] == ct.INFRASTRUCTURE_ERROR for c in _all_cases(res))

    async def fatal(request, k):
        return rn.ProviderReply(error="http 401", retryable=False, no_charge=True)

    t = FakeTransport(script=fatal)
    res = _eval(
        t,
        canaries=canaries[:2],
        limits=rn.Limits(max_retries=2),
        sleep=_instant,
        ledger=_fresh(tmp_path),
    )
    assert len(t.requests) == 2  # нерetryable не повторяется
    assert res.status == ct.INFRASTRUCTURE_ERROR

    # повтор не прячет неверный вердикт первой попытки, расход суммируется
    bad_canary = next(c for c in canaries if c.expectation == sc.MUST_NOT_APPROVE)

    async def hides(request, k):
        if k == 1:
            return rn.ProviderReply(
                text=_good_text("approve"),
                usage={"input_tokens": 10, "output_tokens": 0},
                error="http 503",
                retryable=True,
            )
        return rn.ProviderReply(
            text=_good_text("escalate"), usage={"input_tokens": 5, "output_tokens": 0}
        )

    led = _fresh(tmp_path)
    res = _eval(
        FakeTransport(script=hides),
        canaries=[bad_canary],
        sleep=_instant,
        ledger=led,
    )
    case = _all_cases(res)[0]
    assert case["outcome"] == ct.QUALITY_FAILED and res.status == ct.QUALITY_FAILED
    assert case["usage"] == {"input_tokens": 15, "output_tokens": 0}
    assert res.tokens_used == 15 and led.tokens_used() == 15

    # --- мусорный ответ: не успех ---
    async def garbage(request, k):
        return rn.ProviderReply(
            text="не json вовсе", usage={"input_tokens": 1, "output_tokens": 1}
        )

    res = _eval(
        FakeTransport(script=garbage), canaries=canaries[:2], ledger=_fresh(tmp_path)
    )
    assert res.status == ct.INCOMPLETE
    assert all(c["outcome"] == ct.INCOMPLETE for c in _all_cases(res))


async def _instant(_delay: float) -> None:
    return None


def test_case_sessions_cannot_mutate_production(tmp_path, monkeypatch):
    """AC-3: сессии независимы, прод не тронут, секретов в артефакте нет."""
    secret_key = "sk-secret-CURSOR-0123456789"  # pragma: allowlist secret
    secret_hub = "tok-secret-HUB-0123456789"  # pragma: allowlist secret
    monkeypatch.setattr(config, "CURSOR_API_KEY", secret_key)
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", secret_hub)
    monkeypatch.setenv("CURSOR_API_KEY", secret_key)
    # составная настройка: «имя:токен:роль» — секрет это ТОКЕН, а не вся строка
    composite, short = "TOKcomposite99", "shrt66"
    monkeypatch.setenv(
        "HAIPLANE_HUB_TOKENS", f"steward:{composite}:steward,bob:{short}"
    )
    known = cursor_cloud.known_secrets()
    assert composite in known and short in known and "steward" not in known

    # «прод»: файл БД и любой исходящий вызов — запрещены
    prod_db = tmp_path / "prod.db"
    prod_db.write_bytes(b"production-rows")
    before = hashlib.sha256(prod_db.read_bytes()).hexdigest()
    monkeypatch.setattr(config, "DB_PATH", str(prod_db), raising=False)

    def forbidden(*a, **k):
        raise AssertionError("обращение к провайдеру/хабу из тестового прогона")

    monkeypatch.setattr(cursor_cloud, "_attempt", forbidden)
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", forbidden)
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)

    canaries = _canaries(2)

    async def script(request, k):
        # эхо ключа в ошибке/ответе провайдера — частый способ утечки
        text = json.dumps(
            {"verdict": "escalate", "confidence": "high", "note": secret_key}
        )
        return rn.ProviderReply(
            text=text,
            usage={"input_tokens": 1, "output_tokens": 1},
            error=(
                f"warn: key {secret_key} / {secret_hub} / {composite} / {short}"
                if k == 1
                else None
            ),
        )

    transport = FakeTransport(script=script)
    result = _eval(transport, canaries=canaries, repeats=2)

    # независимые сессии: уникальные id на каждый case каждого прогона
    sessions = [r.session_id for r in transport.requests]
    assert len(sessions) == 4 and len(set(sessions)) == 4
    run_ids = {run["meta"]["run_id"] for run in result.runs}
    assert len(run_ids) == 2
    assert len({run["meta"]["session_id"] for run in result.runs}) == 2

    # запрос не несёт ни секретов, ни адреса прода, ни доступа к хабу
    request_fields = {f for f in rn.ProviderRequest.__dataclass_fields__}
    assert request_fields == {
        "case_id",
        "run_id",
        "session_id",
        "model",
        "prompt",
        "params",
        "attempt",
        "timeout_s",
    }
    for r in transport.requests:
        blob = json.dumps(r.__dict__, default=str)
        for needle in (secret_key, secret_hub, "agenthai", "STEWARD_HUB_TOKEN"):
            assert needle not in blob
        # соседние case не просачиваются в промпт друг друга
        others = [c.name for c in canaries if not r.case_id.endswith(":" + c.name)]
        assert all(('"' + o + '"') not in r.prompt for o in others)

    # прод не изменён
    assert hashlib.sha256(prod_db.read_bytes()).hexdigest() == before
    imported = set()
    tree = ast.parse(Path(rn.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not {
        m
        for m in imported
        if m.startswith(("hub.repository", "hub.db", "hub.web", "hub.app"))
    }

    # артефакт: записан, валиден, без секретов
    paths = rn.write_artifacts(result, tmp_path / "out")
    assert len(paths) == 2
    for path in paths:
        text = path.read_text()
        for leaked in (secret_key, secret_hub, composite, short):
            assert leaked not in text
        run = json.loads(text)
        assert ct.validate_run(run, ct.build_manifest(canaries)) == []
        assert run["meta"]["params"] == {} or secret_key not in json.dumps(
            run["meta"]["params"]
        )
    assert any("[redacted]" in p.read_text() for p in paths)

    # слой записи чистит секреты сам, даже если прогон пришёл нечищеным
    dirty = json.loads(json.dumps(result.runs[0]))
    dirty["cases"][0]["error"] = f"leak {secret_key}"
    dirty_path = rn.write_artifacts(rn.EvalResult(runs=[dirty]), tmp_path / "dirty")[0]
    assert secret_key not in dirty_path.read_text()
    assert secret_key not in json.dumps(result.runs)

    # CLI без --live не делает ни одного вызова: код 2 даже при полных аргументах
    full = ["--model", "m", "--repo-url", "org/eval", "--commit", "c", "--task-id", "1"]
    out = str(tmp_path / "cli")
    assert rn.main([*full, "--allowed-repo", "org/eval", "--out", out]) == 2
    assert rn.main([]) == 2
    assert not (tmp_path / "cli").exists()
    # --live без allowlist или с продовым репозиторием отказывает до любого вызова
    assert rn.main([*full, "--live", "--out", out]) == 2
    prod = ["--model", "m", "--repo-url", "agentdrover/haiplane", "--commit", "c"]
    prod += ["--task-id", "1", "--allowed-repo", "agentdrover/haiplane", "--live"]
    assert rn.main([*prod, "--out", out]) == 2
    assert not (tmp_path / "cli").exists()


import httpx  # noqa: E402

from agent_eval import cursor_adapter as ca  # noqa: E402

REPO = "org/eval-repo"


def _transport(**kw):
    kw.setdefault("poll_interval_s", 0.01)
    return ca.CursorTransport(repo_url=REPO, allowed_repos=[REPO], **kw)


def _request(**kw):
    kw.setdefault("timeout_s", 5.0)
    return rn.ProviderRequest("c", "r", "sess-1", MODEL, "prompt", {}, 1, **kw)


def test_cursor_adapter_http_request_is_isolated(monkeypatch):
    """Тело HTTP-запроса адаптера: рабочий промпт и модель, без MCP и токена хаба."""
    key, hub = "sk-live-ADAPTER-123456", "hubtok-ADAPTER-123456"
    monkeypatch.setattr(config, "CURSOR_API_KEY", key)
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", hub)
    sent: list[dict] = []

    async def fake(self, method, url, json=None, headers=None, **_):
        sent.append({"method": method, "url": url, "json": json, "headers": headers})
        if method == "POST":
            body = {"agent": {"id": "a1"}, "run": {"id": "r1"}}
        elif url.endswith("/usage?runId=r1"):
            body = {"totalUsage": {"totalTokens": 42}}
        else:
            body = {"status": "FINISHED", "result": _good_text()}
        return httpx.Response(200, json=body, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake)
    reply = _run(_transport().complete(_request()))
    assert reply.error is None and reply.usage == {
        "input_tokens": 42,
        "output_tokens": 0,
    }
    create = next(c for c in sent if c["method"] == "POST")
    body = create["json"]
    assert body["prompt"] == {"text": "prompt"} and body["model"] == {"id": MODEL}
    assert body["repos"] == [
        {"url": f"https://github.com/{REPO}", "startingRef": "HEAD"}
    ]
    assert body["name"] == "sess-1"
    assert "mcpServers" not in body  # доступа к хабу нет
    assert hub not in json.dumps(body)
    assert body["autoCreatePR"] is False
    assert create["headers"]["Authorization"] == f"Bearer {key}"


def test_cursor_adapter_repo_allowlist(monkeypatch):
    """Репозиторий только из явного allowlist; продовые agentdrover/* — никогда."""
    with pytest.raises(ValueError):  # allowlist пуст
        ca.CursorTransport(repo_url=REPO)
    with pytest.raises(ValueError):  # продовый, даже если его вписали
        ca.CursorTransport(
            repo_url="agentdrover/haiplane", allowed_repos=["agentdrover/haiplane"]
        )
    with pytest.raises(ValueError):
        ca.CursorTransport(
            repo_url="https://github.com/AgentDrover/other",
            allowed_repos=["agentdrover/other"],
        )
    with pytest.raises(ValueError):  # не из списка
        ca.CursorTransport(repo_url="org/another", allowed_repos=[REPO])
    with pytest.raises(ValueError):  # не GitHub-slug
        ca.CursorTransport(repo_url="http://evil.example/x", allowed_repos=[REPO])
    monkeypatch.setenv(ca.ALLOWED_REPOS_ENV, f"https://github.com/{REPO}.git")
    assert ca.CursorTransport(repo_url=REPO)._slug == REPO


def test_cursor_adapter_retry_only_after_proven_refusal(monkeypatch):
    """Повтор только после доказанного отказа до создания; иначе без повтора."""
    cases = [
        (cursor_cloud.Refusal(status=500), False),
        (cursor_cloud.Refusal(status=503), False),
        (cursor_cloud.Refusal(status=404), False),
        (cursor_cloud.Refusal(status=409), False),
        (cursor_cloud.Refusal(status=408), False),
        (cursor_cloud.Refusal(status=0, detail="ReadTimeout: "), False),
        (cursor_cloud.Refusal(status=0, detail="тело не объект"), False),
        (cursor_cloud.Refusal(status=400, code="usage_limit_exceeded"), True),
        (cursor_cloud.Refusal(status=429), True),
        (cursor_cloud.Refusal(status=0, detail="ConnectError: refused"), True),
    ]
    for denial, proven in cases:

        async def create(**_kw):
            return None, denial

        monkeypatch.setattr(cursor_cloud, "create_agent_attempt", create)
        reply = _run(_transport().complete(_request()))
        assert reply.retryable is proven and reply.no_charge is proven, denial
        assert reply.error.startswith("provider_refused")


def _run_state(monkeypatch):
    """Облачный прогон, который не кончается, пока его не отменят."""
    state = {"cancels": 0, "gets": 0}

    async def create(**_kw):
        return {"agent": {"id": "a1"}, "run": {"id": "r1"}}, None

    async def get_run(agent_id, run_id):
        state["gets"] += 1
        return {"status": "CANCELLED" if state["cancels"] else "RUNNING"}

    async def cancel_run(agent_id, run_id):
        state["cancels"] += 1
        return {}, None

    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", create)
    monkeypatch.setattr(cursor_cloud, "get_run", get_run)
    monkeypatch.setattr(cursor_cloud, "cancel_run", cancel_run)
    return state


def test_cursor_adapter_cancels_cloud_run_on_timeout(monkeypatch, tmp_path):
    """Таймаут и отмена: cancel_run ровно один раз, id прогона сохранены."""
    state = _run_state(monkeypatch)
    # таймаут адаптера (80% срока runner-а) срабатывает раньше runner-а
    reply = _run(_transport().complete(_request(timeout_s=0.2)))
    assert reply.error.startswith("timeout") and "cancel_unconfirmed" not in reply.error
    assert reply.refs == {"agent_id": "a1", "run_id": "r1"}
    assert state["cancels"] == 1

    # runner срезал вызов на ходу: finally всё равно останавливает агента
    state.update(cancels=0, gets=0)
    transport = _transport()

    async def cut():
        await asyncio.wait_for(transport.complete(_request(timeout_s=100.0)), 0.05)

    with pytest.raises(TimeoutError):
        _run(cut())
    assert state["cancels"] == 1
    assert transport.refs["sess-1"] == {"agent_id": "a1", "run_id": "r1"}

    # через run_eval: id попадают в артефакт, повторов нет, расход неизвестен -> стоп
    state.update(cancels=0, gets=0)
    live = _transport()
    res = _eval(
        live,
        canaries=_canaries(2),
        limits=rn.Limits(call_timeout_s=0.1),
        live_opt_in=True,
    )
    assert state["cancels"] == 1  # один case, дальше unknown_usage
    case_id = _all_cases(res)[0]["case_id"]
    attempt = res.runs[0]["attempts"][case_id][0]
    assert attempt["refs"] == {"agent_id": "a1", "run_id": "r1"}
    # адаптер уложился раньше runner-а и остановил агента сам
    assert "прогон провайдера" in attempt["error"] and res.status == ct.INCOMPLETE

    # отмена не подтвердилась — это видно в ошибке, а не молчит
    async def stuck(agent_id, run_id):
        return {"status": "RUNNING"}

    monkeypatch.setattr(cursor_cloud, "get_run", stuck)
    reply = _run(_transport().complete(_request(timeout_s=0.1)))
    assert "cancel_unconfirmed" in reply.error


def test_cursor_cloud_scrubs_secrets_from_logs(monkeypatch, caplog):
    """Эхо ключа в теле ошибки и в исключении не попадает ни в журнал, ни в Refusal."""
    key = "sk-live-LOGLEAK-123456"
    monkeypatch.setattr(config, "CURSOR_API_KEY", key)
    monkeypatch.setenv("HAIPLANE_HUB_TOKENS", "steward:LOGhubtok99:steward")
    leak = f"bad key {key} and LOGhubtok99"

    async def http_500(self, method, url, json=None, headers=None, **_):
        return httpx.Response(500, text=leak, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.AsyncClient, "request", http_500)
    with caplog.at_level(logging.DEBUG):
        body, denial = _run(cursor_cloud._attempt("GET", "/v1/x"))
    assert body is None and denial.status == 500
    assert key not in caplog.text and "LOGhubtok99" not in caplog.text
    assert key not in denial.detail and "LOGhubtok99" not in denial.detail
    assert "[redacted]" in denial.detail

    async def boom(self, method, url, json=None, headers=None, **_):
        raise RuntimeError(leak)

    monkeypatch.setattr(httpx.AsyncClient, "request", boom)
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        _, denial = _run(cursor_cloud._attempt("GET", "/v1/x"))
    assert key not in caplog.text and "LOGhubtok99" not in caplog.text
    assert key not in denial.detail and "LOGhubtok99" not in denial.detail
