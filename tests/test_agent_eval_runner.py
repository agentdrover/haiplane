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
import sys
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
    return _run(rn.run_eval(transport=transport, **kw))


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
        # фиксированные факты канарейки лежат в промпте дословно
        facts = json.dumps(by_name[name].facts, ensure_ascii=False, sort_keys=True)
        assert facts in request.prompt
    meta = result.runs[0]["meta"]
    assert meta["prompt_hash"] == ct.working_prompt_hash()
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


async def _no_call(request, n):  # pragma: no cover - вызов = провал теста
    raise AssertionError("неразрешённый вызов")


def test_live_opt_in_and_limits():
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
    res = _eval(live, canaries=canaries)
    assert live.requests == []
    assert res.calls == 0 and res.status == ct.INFRASTRUCTURE_ERROR
    assert all(c["outcome"] == ct.INFRASTRUCTURE_ERROR for c in _all_cases(res))
    assert all("live_opt_in" in c["error"] for c in _all_cases(res))
    assert any("live_opt_in" in note for note in res.notes)

    # транспорт без признака live считается живым
    class Unmarked:
        async def complete(self, request):  # pragma: no cover - вызов = провал
            raise AssertionError("неразрешённый вызов")

    assert _eval(Unmarked(), canaries=canaries[:1]).calls == 0
    # с opt-in тот же транспорт работает
    live2 = FakeTransport(live=True)
    assert len(_eval(live2, canaries=canaries, live_opt_in=True).runs) == 1
    assert len(live2.requests) == n

    # --- пустой набор не passed и не вызывает ---
    empty_t = FakeTransport(script=_no_call)
    empty = _eval(empty_t, canaries=[])
    assert empty_t.requests == [] and empty.status == ct.INCOMPLETE

    # --- лимит case: сверх лимита не вызывается, хвост incomplete ---
    t = FakeTransport()
    res = _eval(t, canaries=canaries, limits=rn.Limits(max_cases=2))
    assert len(t.requests) == 2
    assert res.status == ct.INCOMPLETE
    assert res.runs[0]["summary"]["incomplete"] == n - 2
    assert any("max_cases" in note for note in res.notes)

    # --- лимит прогонов на задачу, в том числе между вызовами ---
    t = FakeTransport()
    ledger = rn.RunLedger()
    res = _eval(t, canaries=canaries[:2], repeats=4, ledger=ledger)
    assert len(res.runs) == 3 and len(t.requests) == 6
    assert res.status == ct.INCOMPLETE
    assert any("max_runs_per_task" in note for note in res.notes)
    t2 = FakeTransport(script=_no_call)
    again = _eval(t2, canaries=canaries[:2], ledger=ledger)
    assert t2.requests == [] and again.runs == [] and again.status == ct.INCOMPLETE
    other = _eval(FakeTransport(), canaries=canaries[:1], ledger=ledger, task_id=999)
    assert len(other.runs) == 1  # счёт по задаче, не общий

    # --- лимит токенов: новый вызов не начинается, когда бюджета не хватит ---
    t = FakeTransport(usage={"input_tokens": 15_000, "output_tokens": 5_000})
    res = _eval(t, canaries=canaries, limits=rn.Limits(max_tokens=50_000))
    assert len(t.requests) == 3 and res.tokens_used == 60_000
    assert res.status == ct.INFRASTRUCTURE_ERROR
    tail = _all_cases(res)[3:]
    assert tail and all("max_tokens" in c["error"] for c in tail)

    # неизвестный расход не бесплатен
    async def silent(request, k):
        return rn.ProviderReply(text=_right_text(request), usage=None)

    t = FakeTransport(script=silent)
    res = _eval(t, canaries=canaries, limits=rn.Limits(max_tokens=10_000))
    assert 1 <= len(t.requests) < n and res.tokens_used > 0

    # --- лимит вызовов ---
    t = FakeTransport()
    res = _eval(t, canaries=canaries, limits=rn.Limits(max_calls=4))
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
    )
    assert len(t.requests) == 2
    assert any("deadline" in c["error"] for c in _all_cases(res)[2:])
    assert res.status == ct.INFRASTRUCTURE_ERROR

    # --- провайдер завис: таймаут, без повтора ---
    async def hang(request, k):
        await asyncio.sleep(5)

    t = FakeTransport(script=hang)
    res = _eval(t, canaries=canaries[:2], limits=rn.Limits(call_timeout_s=0.05))
    assert len(t.requests) == 2  # по одному на case, повтора на зависание нет
    assert all("timeout" in c["error"] for c in _all_cases(res))
    assert res.status == ct.INFRASTRUCTURE_ERROR

    # --- ошибка провайдера: ограниченный повтор, не качество и не успех ---
    async def flaky(request, k):
        if k == 1:
            return rn.ProviderReply(error="http 503", retryable=True)
        return rn.ProviderReply(
            text=_right_text(request), usage={"input_tokens": 1, "output_tokens": 1}
        )

    t = FakeTransport(script=flaky)
    res = _eval(
        t, canaries=canaries[:1], limits=rn.Limits(max_retries=1), sleep=_instant
    )
    assert len(t.requests) == 2 and res.calls == 2
    assert [r.attempt for r in t.requests] == [1, 2]
    assert _all_cases(res)[0]["outcome"] != ct.INFRASTRUCTURE_ERROR

    async def broken(request, k):
        return rn.ProviderReply(error="http 503", retryable=True)

    t = FakeTransport(script=broken)
    res = _eval(
        t, canaries=canaries[:2], limits=rn.Limits(max_retries=2), sleep=_instant
    )
    assert len(t.requests) == 2 * (1 + 2)
    assert res.status == ct.INFRASTRUCTURE_ERROR
    assert all(c["outcome"] == ct.INFRASTRUCTURE_ERROR for c in _all_cases(res))

    async def fatal(request, k):
        return rn.ProviderReply(error="http 401", retryable=False)

    t = FakeTransport(script=fatal)
    res = _eval(
        t, canaries=canaries[:2], limits=rn.Limits(max_retries=2), sleep=_instant
    )
    assert len(t.requests) == 2  # нерetryable не повторяется
    assert res.status == ct.INFRASTRUCTURE_ERROR

    # --- мусорный ответ: не успех ---
    async def garbage(request, k):
        return rn.ProviderReply(
            text="не json вовсе", usage={"input_tokens": 1, "output_tokens": 1}
        )

    res = _eval(FakeTransport(script=garbage), canaries=canaries[:2])
    assert res.status == ct.INCOMPLETE
    assert all(c["outcome"] == ct.INCOMPLETE for c in _all_cases(res))


async def _instant(_delay: float) -> None:
    return None


def test_case_sessions_cannot_mutate_production(tmp_path, monkeypatch):
    """AC-3: сессии независимы, прод не тронут, секретов в артефакте нет."""
    secret_key = "sk-secret-CURSOR-0123456789"
    secret_hub = "tok-secret-HUB-0123456789"
    monkeypatch.setattr(config, "CURSOR_API_KEY", secret_key)
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", secret_hub)
    monkeypatch.setenv("CURSOR_API_KEY", secret_key)

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
            error=f"warn: key {secret_key} / {secret_hub}" if k == 1 else None,
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
        assert secret_key not in text and secret_hub not in text
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
    full = ["--model", "m", "--repo-url", "u", "--commit", "c", "--task-id", "1"]
    assert rn.main([*full, "--out", str(tmp_path / "cli")]) == 2
    assert rn.main([]) == 2
    assert not (tmp_path / "cli").exists()


def test_cursor_adapter_gives_no_hub_access(monkeypatch):
    """Адаптер зовёт шов стюарда без MCP и токена хаба; сеть подменена."""
    from agent_eval.cursor_adapter import CursorTransport

    seen: dict = {}

    async def create(**kwargs):
        seen.update(kwargs)
        return {"agent": {"id": "a1"}, "run": {"id": "r1"}}, None

    async def get_run(agent_id, run_id):
        return {"status": "FINISHED", "result": _good_text()}

    async def get_usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 42}}

    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", create)
    monkeypatch.setattr(cursor_cloud, "get_run", get_run)
    monkeypatch.setattr(cursor_cloud, "get_usage", get_usage)
    request = rn.ProviderRequest("c", "r", "s", MODEL, "prompt", {}, 1)
    reply = _run(
        CursorTransport(repo_url="https://example.invalid/r").complete(request)
    )
    assert reply.error is None and reply.usage == {
        "input_tokens": 42,
        "output_tokens": 0,
    }
    assert not seen.get("hub_mcp_url") and not seen.get("reviewer_token")
    assert seen["model_id"] == MODEL and seen["prompt_text"] == "prompt"
