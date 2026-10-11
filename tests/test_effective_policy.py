"""Действующая политика проекта одной сводкой (#1457).

Сводка не толкует политику сама: каждое значение она берёт у того же читателя,
которым пользуется решатель. Поэтому тесты проверяют три вещи: что каждый ключ
виден со значением и источником на REST, CLI и MCP одинаково, что сервер
показывает запрошенный и эффективный режим стюарда и не выдаёт секретов, и что
новый ключ политики без записи в сводке роняет тест, называя ключ.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import cli, config, mcp_server
from hub import repository as repo
from hub.models import GATE_POLICY_KEYS
from hub.services import auto_approve, effective_policy, project_policy
from tests.test_auto_approve import (
    _draft_in_project,
    _project as _auto_project,
    _refine_to_dor,
)

SECRET = "SECRET-MARKER-1457-do-not-print"  # pragma: allowlist secret


async def _project(db: aiosqlite.Connection, slug: str, policy: dict) -> int:
    pid = await repo.create_project(db, slug=slug, name=slug)
    await repo.update_project(db, pid, gate_policy=json.dumps(policy))
    await db.commit()
    return pid


def _message(result) -> str:
    """Человекочитаемая часть MCP-ответа: текст приходит эхом {"message": ...}."""
    return json.loads(result.content[0].text)["message"]


def _by_key(data: dict) -> dict[str, dict]:
    return {row["key"]: row for row in data["keys"]}


async def test_every_policy_key_shown_with_source_on_all_surfaces(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, capsys
):
    # AC-1: каждый известный ключ со значением и источником, время и автор
    # последней правки; REST, CLI и MCP говорят одно и то же.
    policy = {
        "verdict": "human",
        "submission_contract": "require",
        "executor_launch": "manual",
        "mystery_key": 1,
    }
    pid = await _project(db, "spike", policy)
    await repo.insert_event(
        db,
        kind="project_gate_policy_changed",
        project_id=pid,
        actor="human",
        payload={
            "slug": "spike",
            "changed": ["submission_contract"],
            "removed": [],
            "by": "denis",
        },
    )
    await db.commit()

    resp = await client.get("/api/projects/spike/effective-policy")
    assert resp.status_code == 200
    rest = resp.json()
    rows = _by_key(rest)
    assert sorted(rows) == sorted(GATE_POLICY_KEYS), "каждый ключ — ровно одна строка"
    assert rows["verdict"]["value"] == "human"
    assert rows["verdict"]["source"] == "project"
    assert rows["submission_contract"]["value"] == "require"
    assert rows["submission_contract"]["source"] == "project"
    assert rows["executor_launch"]["value"] == "manual"
    assert rows["executor_launch"]["source"] == "project"
    # Нет ключа в проекте: значение даёт читатель, источник — умолчание.
    assert rows["claim_area_check"]["source"] == "default"
    assert rows["claim_area_check"]["value"] == "off"
    assert rows["claim_area_check"]["default"] == "off"
    # Умолчание из серверной настройки называется сервером, а не умолчанием кода.
    assert rows["executor_task_token_ceiling"]["source"] == "server"
    assert rest["unknown_keys"] == {"mystery_key": 1}
    change = rest["last_change"]
    assert change["changed"] == ["submission_contract"]
    assert change["by"] == "denis"
    assert change["actor"] == "human"
    assert change["at"]

    # MCP: те же данные, что отдал REST.
    async def _via_client(path: str, **_: object) -> object:
        return (await client.get(path)).json()

    monkeypatch.setattr(mcp_server, "_api_get", _via_client)
    out = await mcp_server.hub_effective_policy("spike")
    assert out.structuredContent["keys"] == rest["keys"]
    assert out.structuredContent["last_change"] == rest["last_change"]
    mcp_text = _message(out)
    for key in GATE_POLICY_KEYS:
        assert key in mcp_text, key
    assert "submission_contract = require [project]" in mcp_text
    assert "claim_area_check = off [default]" in mcp_text
    assert "denis" in mcp_text

    # CLI: тот же ответ, тот же текст.
    argv = ["oc-hub", "effective-policy", "spike"]
    with (
        patch.object(sys, "argv", argv),
        patch.object(cli, "_api", return_value=rest) as api,
    ):
        assert cli.main() in (0, None)
    assert api.call_args.args[:2] == ("GET", "/api/projects/spike/effective-policy")
    assert capsys.readouterr().out.strip() == mcp_text.strip()
    with (
        patch.object(sys, "argv", argv + ["--json"]),
        patch.object(cli, "_api", return_value=rest),
    ):
        cli.main()
    assert json.loads(capsys.readouterr().out) == rest


async def test_effective_policy_unknown_project_is_404(client: AsyncClient):
    resp = await client.get("/api/projects/no-such-project/effective-policy")
    assert resp.status_code == 404


async def test_my_context_carries_the_policy_block(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # hub_my_context: задача называет проект, а блок политики идёт следом.
    await _project(db, "spike", {"verdict": "human", "claim_area_check": "warn"})

    async def _fake_get(path: str, **_: object) -> object:
        if path.startswith("/api/tasks/7/context"):
            return {
                "context_text": "Task #7",
                "task": {"project": {"id": 2, "slug": "spike"}},
            }
        return (await client.get(path)).json()

    monkeypatch.setattr(mcp_server, "_api_get", _fake_get)
    out = await mcp_server.hub_my_context(task_id=7)
    text = _message(out)
    assert "Policy of project spike" in text
    assert "claim_area_check = warn [project]" in text
    assert "hub_effective_policy" in text


async def test_server_steward_mode_and_locks_without_secrets(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, capsys
):
    # AC-2: act запрошен, замеры его не дают — виден и запрошенный, и
    # эффективный режим с причинами отказа; замок #743 на default; секретов нет.
    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", SECRET)
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", SECRET)
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", SECRET)
    monkeypatch.setattr(config, "EXECUTOR_MODEL", "composer-test-model")
    await _project(db, "spike", {})
    await _project(db, "default", {"deep_daily_cap": 4})

    rest = (await client.get("/api/projects/default/effective-policy")).json()
    steward = rest["steward"]
    assert steward["requested"] == "act"
    assert steward["effective"] == "shadow"
    assert steward["act_refusals"], "причины отказа названы"
    assert all(r["code"] and r["detail"] for r in steward["act_refusals"])
    locks = {lock["id"]: lock for lock in rest["locks"]}
    assert locks["#743"]["applies"] is True
    assert sorted(locks["#743"]["gates"]) == ["dor", "verdict"]
    assert rest["server"]["executor_model"] == "composer-test-model"
    assert rest["server"]["steward_model"] == config.STEWARD_MODEL
    assert rest["server"]["secrets_configured"]["steward_hub_token"] is True

    other = (await client.get("/api/projects/spike/effective-policy")).json()
    assert {lock["id"]: lock for lock in other["locks"]}["#743"]["applies"] is False

    # Чтение сводки не пишет в ленту: GET не должен оставлять следов.
    refused = await repo.list_events(db, kinds=["steward_act_refused"])
    assert refused == []

    async def _via_client(path: str, **_: object) -> object:
        return (await client.get(path)).json()

    monkeypatch.setattr(mcp_server, "_api_get", _via_client)
    out = await mcp_server.hub_effective_policy("default")
    with (
        patch.object(sys, "argv", ["oc-hub", "effective-policy", "default"]),
        patch.object(cli, "_api", return_value=rest),
    ):
        cli.main()
    surfaces = [
        json.dumps(rest, ensure_ascii=False),
        json.dumps(other, ensure_ascii=False),
        out.content[0].text,
        _message(out),
        json.dumps(out.structuredContent, ensure_ascii=False, default=str),
        capsys.readouterr().out,
    ]
    for blob in surfaces:
        assert SECRET not in blob
    text = _message(out)
    assert "requested act" in text and "effective shadow" in text
    assert "#743" in text


def test_new_policy_key_without_summary_entry_fails(monkeypatch):
    # AC-3: сводка не отстаёт от правил. Перечень берётся из project_policy и
    # GATE_POLICY_KEYS, поэтому новый *_KEY без записи роняет тест и назван.
    assert effective_policy.unsummarised_keys() == [], (
        f"ключи политики без записи в сводке: {effective_policy.unsummarised_keys()}"
    )

    monkeypatch.setattr(
        project_policy, "BRAND_NEW_THING_KEY", "brand_new_policy_key", raising=False
    )
    assert effective_policy.unsummarised_keys() == ["brand_new_policy_key"]

    # Ключ, что умеет принять запись, но не умеет показать сводка.
    monkeypatch.undo()
    monkeypatch.setattr(
        "hub.models.GATE_POLICY_KEYS", (*GATE_POLICY_KEYS, "another_new_key")
    )
    assert effective_policy.unsummarised_keys() == ["another_new_key"]

    # И обратное: запись в сводке, которой нет в правилах, — тоже расхождение.
    monkeypatch.undo()
    monkeypatch.setitem(
        effective_policy.REGISTRY,
        "ghost_key",
        effective_policy.PolicyEntry(lambda policy: None, "nowhere"),
    )
    assert effective_policy.unsummarised_keys() == ["ghost_key"]
    with pytest.raises(AssertionError, match="ghost_key"):
        effective_policy.assert_summary_complete()


async def test_a_policy_patch_shows_up_in_the_summary_with_time_and_author(
    client: AsyncClient,
):
    # Правка через PATCH пишет событие с автором, и сводка называет время правки.
    resp = await client.post("/api/projects", json={"slug": "spike", "name": "Spike"})
    pid = resp.json()["id"]
    patched = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"claim_area_check": "warn"}}
    )
    assert patched.status_code == 200, patched.text
    data = (await client.get("/api/projects/spike/effective-policy")).json()
    change = data["last_change"]
    assert change["changed"] == ["claim_area_check"]
    assert change["at"]
    assert change["by"], "автор правки назван именем из identity"
    assert _by_key(data)["claim_area_check"]["value"] == "warn"


async def test_review_derived_from_a_delegated_verdict_names_its_source(
    client: AsyncClient, db: aiosqlite.Connection
):
    # Находка 5197fd78dd0e3efb: review=dispatch, выведенный из verdict=auto,
    # не должен читаться как умолчание или как сохранённое значение проекта.
    await _project(db, "derived-a", {"verdict": "auto"})
    await _project(db, "derived-b", {"verdict": "auto", "review": "off"})
    await _project(db, "derived-c", {"verdict": "auto", "review": "dispatch"})
    await _project(db, "derived-d", {"verdict": "human"})
    rows = {}
    for slug in ("derived-a", "derived-b", "derived-c", "derived-d"):
        data = (await client.get(f"/api/projects/{slug}/effective-policy")).json()
        rows[slug] = (_by_key(data)["review"], data)
    a, _ = rows["derived-a"]
    assert (a["value"], a["source"], a["derived_from"]) == (
        "dispatch",
        "derived",
        "verdict=auto",
    )
    assert a["default"] == "off"
    b, data_b = rows["derived-b"]
    assert (b["value"], b["source"], b["stored"]) == ("dispatch", "derived", "off")
    assert b["derived_from"] == "verdict=auto"
    text = "\n".join(effective_policy.format_effective_policy(data_b))
    assert "review = dispatch [derived from verdict=auto] (stored off)" in text
    c, _ = rows["derived-c"]
    assert (c["source"], "derived_from" in c) == ("project", False)
    d, _ = rows["derived-d"]
    assert (d["value"], d["source"]) == ("off", "default")
    # Других ключей с выводом из соседа нет: источник derived только у review.
    for _slug, (_row, data) in rows.items():
        derived = [r["key"] for r in data["keys"] if r["source"] == "derived"]
        assert derived in ([], ["review"])


async def test_my_context_names_an_unreadable_policy_instead_of_dropping_it(
    monkeypatch,
):
    # Находка e17305020f78121a: ошибка чтения не должна убирать блок молча.
    async def _fake_get(path: str, **_: object) -> object:
        if path.startswith("/api/tasks/7/context"):
            return {
                "context_text": "Task #7",
                "task": {"project": {"id": 2, "slug": "spike"}},
            }
        raise mcp_server.HubApiError({"message": "policy backend exploded"})

    monkeypatch.setattr(mcp_server, "_api_get", _fake_get)
    out = await mcp_server.hub_my_context(task_id=7)
    text = _message(out)
    assert "политика проекта не прочитана" in text
    assert "policy backend exploded" in text


def test_deep_reviewer_key_is_validated_and_defaults_to_cloud():
    """#1561: ключ принимает cloud | local, нечитаемое не читается как local."""
    import pytest

    from hub.models import ProjectPatch

    assert "deep_reviewer" in GATE_POLICY_KEYS
    assert project_policy.deep_reviewer_of({}) == "cloud"
    assert project_policy.deep_reviewer_of({"deep_reviewer": "local"}) == "local"
    assert project_policy.deep_reviewer_of({"deep_reviewer": "LOCAL"}) == "cloud"
    assert project_policy.deep_reviewer_of({"deep_reviewer": 1}) == "cloud"
    ProjectPatch(gate_policy={"deep_reviewer": "local"})
    with pytest.raises(ValueError, match="deep_reviewer"):
        ProjectPatch(gate_policy={"deep_reviewer": "qwen"})


def test_summary_covers_merge_is_delivery():
    """#1572: the key is accepted by the write, read by one reader, and summarised."""
    import pytest

    from hub.models import ProjectPatch

    assert "merge_is_delivery" in GATE_POLICY_KEYS
    assert "merge_is_delivery" in effective_policy.REGISTRY
    assert effective_policy.unsummarised_keys() == []
    assert project_policy.merge_is_delivery_of({}) is False
    assert project_policy.merge_is_delivery_of({"merge_is_delivery": True}) is True
    assert project_policy.merge_is_delivery_of({"merge_is_delivery": "true"}) is False
    assert project_policy.merge_is_delivery_of({"merge_is_delivery": 1}) is False
    ProjectPatch(gate_policy={"merge_is_delivery": True})
    ProjectPatch(gate_policy={"merge_is_delivery": False})
    with pytest.raises(ValueError, match="merge_is_delivery"):
        ProjectPatch(gate_policy={"merge_is_delivery": "yes"})


async def test_merge_is_delivery_is_shown_with_its_source(db: aiosqlite.Connection):
    """The summary asks the same reader: stored true shows as true, from the project."""
    pid = await _project(db, "local-app", {"merge_is_delivery": True})
    other = await _project(db, "plain-app", {})

    async def _row(project_id: int) -> dict:
        project = await repo.get_project(db, project_id)
        data = await effective_policy.effective_policy(db, project)
        return _by_key(data)["merge_is_delivery"]

    on, off = await _row(pid), await _row(other)

    assert on["value"] is True and off["value"] is False
    assert on["source"] != off["source"]


# --- #1558: решатель читает ключ политики тем же читателем, что и сводка -----

_SERVICES_DIR = Path(__file__).resolve().parents[1] / "hub" / "services"

#: Приёмник ``.get`` похож на политику: имя с «policy» или ``gate_policy_of(...)``.
_POLICY_RECEIVER = re.compile(
    r"^(\w*policy\w*|gate_policy_of\(.*\)|\(policy or \{\}\))$"
)

#: Прямые чтения вне читателя из REGISTRY. Каждая строка — своя причина; шаблона
#: «весь файл» здесь нет, и решатель сюда не вписывается: тест ниже проверяет,
#: что перечень не растёт молча (число строк и ключ закреплены).
_DIRECT_READ_ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "project_policy.py",
        "_stored_gate_values",
        "dor",
    ): "внутренность читателей gate_value_of/gate_form_value: сырое значение по литералу на ключ, "
    "его читает сверка ключей tests/test_branch_policy_validation.py",
    (
        "project_policy.py",
        "_stored_gate_values",
        "verdict",
    ): "то же: сырое значение verdict внутри читателей",
    (
        "effective_policy.py",
        "_review_derived_from",
        "review",
    ): "сама сводка: подпись «derived from» сравнивает сырой review, ничего не решает",
    (
        "effective_policy.py",
        "_review_derived_from",
        "verdict",
    ): "сама сводка: называет сырой verdict в подписи «derived from»",
    (
        "steward_apply.py",
        "policy_refusal",
        "verdict",
    ): "показ: текст отказа называет СОХРАНЁННОЕ значение, решение уже принято читателем "
    "verdict_delegated_to_steward строкой выше",
}


def _declared_readers() -> set[tuple[str, str]]:
    """Пары (файл, функция) из второго поля PolicyEntry: «модуль.функция»."""
    pairs = set()
    for entry in effective_policy.REGISTRY.values():
        module, _, func = entry.reader.partition(".")
        pairs.add((f"{module}.py", func))
    return pairs


def _string_constants() -> dict[str, str]:
    """Имя -> строка для верхнеуровневых констант hub/services (``*_KEY``)."""
    consts: dict[str, str] = {}
    for path in _SERVICES_DIR.glob("*.py"):
        for node in ast.parse(path.read_text()).body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = node.value.value
    return consts


def direct_policy_reads(
    source: str, filename: str, keys: set[str], consts: dict[str, str]
) -> list[tuple[str, int, str, str]]:
    """(файл, строка, функция, ключ) для каждого ``policy.get(<ключ REGISTRY>)``.

    Ключ — строка, имя константы или переменная: ``.get(gate)`` читает ЛЮБОЙ
    ключ, и страж называет его «<dynamic>», а не пропускает.
    """
    found: list[tuple[str, int, str, str]] = []

    def visit(node: ast.AST, func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and _POLICY_RECEIVER.match(ast.unparse(node.func.value))
        ):
            arg = node.args[0]
            key = None
            if isinstance(arg, ast.Constant) and arg.value in keys:
                key = arg.value
            elif isinstance(arg, ast.Name):
                value = consts.get(arg.id)
                key = value if value in keys else (None if value else "<dynamic>")
            elif isinstance(arg, ast.Attribute) and consts.get(arg.attr) in keys:
                key = consts[arg.attr]
            if key is not None:
                found.append((filename, node.lineno, func, key))
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    visit(ast.parse(source), "<module>")
    return found


def _offenders(
    sources: dict[str, str], allowed: set[tuple[str, str]] | None = None
) -> list[str]:
    keys = set(effective_policy.REGISTRY)
    consts = _string_constants()
    readers = _declared_readers() if allowed is None else allowed
    out = []
    for name, source in sorted(sources.items()):
        for file, line, func, key in direct_policy_reads(source, name, keys, consts):
            if (file, func) in readers or (file, func, key) in _DIRECT_READ_ALLOWLIST:
                continue
            out.append(f"{file}:{line} читает ключ {key} в {func}() мимо читателя")
    return out


def test_every_solver_reads_policy_through_the_summary_reader():
    """AC-1 (#1558): прямое чтение ключа REGISTRY вне читателя роняет тест.

    Падение называет файл:строку и ключ. Страж обходит ast, а не греп: ключ
    может прийти константой ``*_KEY`` или переменной.
    """
    sources = {p.name: p.read_text() for p in _SERVICES_DIR.glob("*.py")}
    assert _offenders(sources) == []

    # Страж не немой: посторонний решатель в литерале и в константе назван.
    planted = {
        "fake.py": (
            "def decide(policy):\n"
            "    if policy.get('dor') != 'auto':\n"
            "        return 1\n"
            "    return policy.get(WIP_LIMIT_KEY)\n"
        )
    }
    named = _offenders(planted)
    assert any("fake.py:2" in m and "dor" in m for m in named), named
    assert any("fake.py:4" in m and "wip_limit" in m for m in named), named

    # Перечень исключений не прячет решатель: каждая запись существует в коде
    # и несёт причину, а читателя-решателя среди них нет.
    keys = set(effective_policy.REGISTRY)
    consts = _string_constants()
    live = {
        (f, fn, k)
        for name, src in sources.items()
        for f, _line, fn, k in direct_policy_reads(src, name, keys, consts)
    }
    assert set(_DIRECT_READ_ALLOWLIST) <= live, "запись белого списка без чтения"
    assert all(len(reason) > 20 for reason in _DIRECT_READ_ALLOWLIST.values())


@pytest.mark.parametrize("gate", ["dor", "verdict"])
@pytest.mark.parametrize("value", ["human", "auto", "steward", "", "AUTO", "stewrad"])
def test_gate_value_reader_accepts_what_the_write_accepts(gate, value):
    expected = value if value in {"human", "auto", "steward"} else "human"
    assert project_policy.gate_value_of({gate: value}, gate) == expected


def test_gate_value_reader_reads_garbage_as_human():
    for policy in ({}, None, [], "auto", {"dor": None}, {"dor": 1}, {"dor": ["auto"]}):
        assert project_policy.gate_value_of(policy, "dor") == "human"  # type: ignore[arg-type]
    assert project_policy.gate_value_of({"dor": "auto"}, "nonsense") == "human"


async def test_a_dor_delegated_to_the_steward_shows_in_the_summary(
    db: aiosqlite.Connection,
):
    """Расхождение #1558: запись принимает dor=steward, диспетчер его исполняет.

    Сводка читала форменного читателя и писала human, пока диспетчер DoR-стюарда
    заказывал прогон. Теперь оба спрашивают gate_value_of.
    """
    from hub.services.steward_dispatch import _policy_wants_steward

    pid = await _project(db, "dor-steward", {"dor": "steward"})
    project = await repo.get_project(db, pid)
    row = _by_key(await effective_policy.effective_policy(db, project))["dor"]

    assert row["value"] == "steward"
    assert row["reader"] == "project_policy.gate_value_of"
    assert _policy_wants_steward(project, gate="dor") is True
    assert _policy_wants_steward(project, gate="verdict") is False


def test_summary_and_gate_readers_are_one_function(monkeypatch):
    """Сводка и решатели зовут ОДНУ функцию: подмена одной сдвигает все."""
    from hub.services import auto_approve, steward_dispatch

    assert auto_approve.gate_value_of is project_policy.gate_value_of
    assert steward_dispatch.gate_value_of is project_policy.gate_value_of
    for gate in ("dor", "verdict"):
        assert effective_policy.REGISTRY[gate].reader == "project_policy.gate_value_of"
        seen = []
        monkeypatch.setattr(
            project_policy, "gate_value_of", lambda p, g: seen.append(g) or "probe"
        )
        assert effective_policy.REGISTRY[gate].read({}) == "probe"
        assert seen == [gate]
        monkeypatch.undo()
    monkeypatch.setattr(project_policy, "gate_value_of", lambda p, g: "human")
    assert project_policy.verdict_is_delegated({"verdict": "steward"}) is False


@pytest.mark.parametrize(
    "value", ["human", "auto", "steward", "AUTO", "stewrad", "", 1, None, ["steward"]]
)
def test_solvers_answer_as_the_raw_comparison_did(value):
    """Таблица: переведённые решатели дают тот же ответ, что и сырое сравнение."""
    from hub.services.steward_dispatch import _policy_wants_steward

    for gate in ("dor", "verdict"):
        policy = {gate: value}
        project = {"gate_policy": json.dumps(policy)}
        assert _policy_wants_steward(project, gate=gate, shadow=False) is (
            value == "steward"
        ), (gate, value)
    delegated = isinstance(value, str) and value in {"auto", "steward"}
    assert project_policy.verdict_is_delegated({"verdict": value}) is delegated
    assert project_policy.verdict_is_delegated({}) is False
    assert project_policy.verdict_is_delegated("junk") is False  # type: ignore[arg-type]


async def test_auto_approve_decides_dor_with_the_summary_reader(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # #1558 AC-2: ONE reader answers for the gate and for the summary.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    pid = await _auto_project(db, "dor-one-reader", {"dor": "auto"})
    project = await repo.get_project(db, pid)
    summary = effective_policy.REGISTRY["dor"]
    assert summary.read({"dor": "auto"}) == "auto"

    # Replace the reader everywhere it is looked up: both sides follow it.
    monkeypatch.setattr(project_policy, "gate_value_of", lambda p, g: "probe")
    monkeypatch.setattr(auto_approve, "gate_value_of", project_policy.gate_value_of)
    assert summary.read(project_policy.gate_policy_of(project)) == "probe"
    task_id = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(client, task_id, ["docs/notes.md"])
    assert body["status"] == "draft", "the gate follows the same reader"


# --- #1589: ключ path_notices ------------------------------------------------


async def test_path_notices_policy_is_validated_and_summarised(
    client: AsyncClient, db: aiosqlite.Connection
):
    """#1589 AC-3: корректная запись видна в сводке с читателем; плохие — 422."""
    pid = await repo.create_project(db, slug="pn-policy", name="pn-policy")
    await db.commit()
    url = f"/api/projects/{pid}"
    good = [
        {"pattern": "deploy/remote-deploy.sh", "text": "  обновить копию  "},
        {"pattern": "deploy/**/*.service", "text": "подтвердить unit"},
    ]

    resp = await client.patch(url, json={"gate_policy": {"path_notices": good}})
    assert resp.status_code == 200, resp.text
    stored = resp.json()["gate_policy"]["path_notices"]
    assert stored[0]["text"] == "обновить копию", "края текста срезаются"

    summary = (await client.get("/api/projects/pn-policy/effective-policy")).json()
    row = _by_key(summary)["path_notices"]
    assert row["reader"] == "project_policy.path_notices_of"
    assert row["source"] == "project"
    assert [r["pattern"] for r in row["value"]] == [g["pattern"] for g in good]
    assert row["default"] == []
    assert "path_notices" not in summary["unknown_keys"]

    bad = {
        "empty text": [{"pattern": "a/**", "text": "   "}],
        "empty pattern": [{"pattern": "", "text": "t"}],
        "wrong type": "deploy/**",
        "wrong item type": ["deploy/**"],
        "missing text": [{"pattern": "a/**"}],
        "extra key": [{"pattern": "a/**", "text": "t", "level": "high"}],
        "non-string text": [{"pattern": "a/**", "text": 5}],
        "too many rules": [{"pattern": f"d{i}/**", "text": "t"} for i in range(51)],
        "long pattern": [{"pattern": "a" * 201, "text": "t"}],
        "long text": [{"pattern": "a/**", "text": "т" * 501}],
    }
    for name, value in bad.items():
        resp = await client.patch(url, json={"gate_policy": {"path_notices": value}})
        assert resp.status_code == 422, (name, resp.status_code, resp.text)
        assert "path_notices" in resp.text, (name, resp.text)
    # Границы допустимы: ровно 50 правил, 200 символов шаблона, 500 текста.
    edge = [{"pattern": f"d{i}/**", "text": "t"} for i in range(49)] + [
        {"pattern": "a" * 200, "text": "т" * 500}
    ]
    resp = await client.patch(url, json={"gate_policy": {"path_notices": edge}})
    assert resp.status_code == 200, resp.text
    # Отказы ничего не испортили.
    assert (
        len(
            project_policy.path_notices_of(
                project_policy.gate_policy_of(await repo.get_project(db, pid))
            )
        )
        == 50
    )

    # Пустой список допустим, PATCH null удаляет ключ штатно.
    resp = await client.patch(url, json={"gate_policy": {"path_notices": []}})
    assert resp.status_code == 200, resp.text
    resp = await client.patch(url, json={"gate_policy": {"path_notices": None}})
    assert resp.status_code == 200, resp.text
    assert "path_notices" not in resp.json()["gate_policy"]
    row = _by_key(
        (await client.get("/api/projects/pn-policy/effective-policy")).json()
    )["path_notices"]
    assert row["source"] == "default" and row["value"] == []


def test_path_notices_pattern_semantics():
    """#1589: * не пересекает /, ** — любая глубина, включая ноль."""
    from hub.services.path_notices import match_rules, path_matches

    assert path_matches("deploy/**/*.service", "deploy/x.service")
    assert path_matches("deploy/**/*.service", "deploy/a/b/x.service")
    assert not path_matches("deploy/*.service", "deploy/a/x.service")
    assert path_matches("deploy/*.service", "deploy/x.service")
    assert path_matches("deploy/review-runner/**", "deploy/review-runner/a/b.py")
    assert not path_matches("deploy/review-runner/**", "deploy/review-runnerX/a.py")
    assert not path_matches("deploy/review-runner/**", "deploy/review-runner")
    assert path_matches("**/x.py", "x.py") and path_matches("**/x.py", "a/b/x.py")
    assert path_matches("deploy/remote-deploy.sh", "deploy/remote-deploy.sh")
    assert not path_matches("deploy/remote-deploy.sh", "xdeploy/remote-deploy.sh")
    assert not path_matches("deploy/remote-deploy.sh", "deploy/remote-deploy.sh.bak")
    assert path_matches("a.b", "a.b") and not path_matches("a.b", "aXb")
    assert path_matches("hub/db?.py", "hub/db1.py")
    # Пересечения: все совпавшие, текст без дублей.
    rules = [
        {"pattern": "deploy/**", "text": "один"},
        {"pattern": "deploy/*.sh", "text": "один"},
        {"pattern": "deploy/x.sh", "text": "два"},
    ]
    got = match_rules(rules, ["deploy/x.sh", "docs/a.md"])
    assert [n["text"] for n in got] == ["один", "два"]
    assert got[0]["patterns"] == ["deploy/**", "deploy/*.sh"]
    assert got[0]["paths"] == ["deploy/x.sh"]


# --- #1591: ключ release_artifacts -------------------------------------------


async def test_release_artifacts_policy_is_validated_and_confined(
    client: AsyncClient, db: aiosqlite.Connection, tmp_path, monkeypatch
):
    """#1591 AC-3: корректная запись видна; плохие — 422; symlink и чужие пути не читаются."""
    import os

    from hub.services import release_artifacts as ra

    srv = tmp_path / "srv"
    srv.mkdir()
    outside = tmp_path / "outside.env"
    outside.write_text("SECRET=1\n")
    monkeypatch.setattr(config, "RELEASE_ARTIFACT_DIRS", ("/usr/local/sbin", str(srv)))
    pid = await repo.create_project(db, slug="ra-policy", name="ra-policy")
    await db.commit()
    url = f"/api/projects/{pid}"

    def entry(server_path: str, repo_path: str = "deploy/x.sh") -> list[dict]:
        return [
            {
                "repo_path": repo_path,
                "server_path": server_path,
                "update_hint": "обновить",
            }
        ]

    good = entry("/usr/local/sbin/svc-remote-deploy.sh")
    resp = await client.patch(url, json={"gate_policy": {"release_artifacts": good}})
    assert resp.status_code == 200, resp.text
    summary = (await client.get("/api/projects/ra-policy/effective-policy")).json()
    row = _by_key(summary)["release_artifacts"]
    assert row["reader"] == "project_policy.release_artifacts_of"
    assert row["source"] == "project" and row["value"] == good
    assert row["default"] == []
    assert "release_artifacts" not in summary["unknown_keys"]

    bad = {
        "relative": entry("usr/local/sbin/x"),
        "dotdot": entry("/usr/local/sbin/../../etc/passwd"),
        "outside": entry("/etc/hub-conf/secrets.env"),
        "prefix without boundary": entry("/usr/local/sbinX/x.sh"),
        "the directory itself": entry("/usr/local/sbin"),
        "absolute repo path": entry("/usr/local/sbin/x", "/deploy/x.sh"),
        "repo path with ..": entry("/usr/local/sbin/x", "../x.sh"),
        "no hint": [{"repo_path": "a", "server_path": "/usr/local/sbin/x"}],
        "not a list": "x",
    }
    for name, value in bad.items():
        resp = await client.patch(
            url, json={"gate_policy": {"release_artifacts": value}}
        )
        assert resp.status_code == 422, (name, resp.status_code, resp.text)
        assert "release_artifacts" in resp.text, (name, resp.text)

    # Stored policy, allowlist changed afterwards: reading is confined anyway,
    # and a file outside the allowlist is never opened.
    opened: list[str] = []
    real_open = os.open

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    digest, why = ra.server_sha256("/etc/hub-conf/secrets.env", ra.allowed_dirs())
    assert digest is None and why
    digest, why = ra.server_sha256(str(outside), ra.allowed_dirs())
    assert digest is None and why
    assert not any("secrets.env" in p or "outside.env" in p for p in opened)

    # A symlink inside the allowed directory to a file outside it is refused,
    # and so is a symlinked directory in the path; a regular file is read.
    link = srv / "link.sh"
    link.symlink_to(outside)
    digest, why = ra.server_sha256(str(link), ra.allowed_dirs())
    assert digest is None and "symlink" in why
    assert not any("outside.env" in p for p in opened)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "f.sh").write_text("x")
    (srv / "dirlink").symlink_to(elsewhere)
    digest, why = ra.server_sha256(str(srv / "dirlink" / "f.sh"), ra.allowed_dirs())
    assert digest is None and "symlink" in why
    regular = srv / "ok.sh"
    regular.write_bytes(b"abc\n")
    digest, why = ra.server_sha256(str(regular), ra.allowed_dirs())
    import hashlib

    assert digest == hashlib.sha256(b"abc\n").hexdigest() and why == ""
    # Not a regular file.
    fifo_dir = srv / "d"
    fifo_dir.mkdir()
    digest, why = ra.server_sha256(str(fifo_dir), ra.allowed_dirs())
    assert digest is None and why

    resp = await client.patch(url, json={"gate_policy": {"release_artifacts": None}})
    assert resp.status_code == 200, resp.text
    assert "release_artifacts" not in resp.json()["gate_policy"]
    row = _by_key(
        (await client.get("/api/projects/ra-policy/effective-policy")).json()
    )["release_artifacts"]
    assert row["source"] == "default" and row["value"] == []


async def test_summary_shows_scheduled_change_and_its_executor(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, capsys
):
    """#1593: «2 → 4 с даты» рядом с ключом; исполненную правку делает актор schedule."""
    from datetime import UTC, datetime, timedelta

    from hub.services import policy_change

    base = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    clock = {"now": base}
    monkeypatch.setattr(policy_change, "utcnow", lambda: clock["now"])
    await _project(db, "sched-sum", {"deep_daily_cap": 2})
    at = base + timedelta(days=7)
    for value in (4, 5):
        resp = await client.post(
            "/api/projects/sched-sum/policy-schedule",
            json={
                "at": (at + timedelta(hours=value)).isoformat(),
                "patch": {"deep_daily_cap": value},
            },
        )
        assert resp.status_code == 201, resp.text

    before = (await client.get("/api/projects/sched-sum/effective-policy")).json()
    cap = next(r for r in before["keys"] if r["key"] == "deep_daily_cap")
    assert cap["value"] == 2
    assert [p["value"] for p in cap["scheduled"]] == [4, 5], "цепочка по порядку"
    text = "\n".join(effective_policy.format_effective_policy(before))
    assert "→ 4 с 2026-10-13" in text and "отложенная правка #1" in text

    # Чтение списка: CLI и MCP — только чтение, тот же текст.
    listed = (await client.get("/api/projects/sched-sum/policy-schedule")).json()

    async def _via_client(path: str, **_: object) -> object:
        return (await client.get(path)).json()

    monkeypatch.setattr(mcp_server, "_api_get", _via_client)
    out = await mcp_server.hub_policy_schedule("sched-sum")
    assert "#1 pending" in _message(out) and "#2 pending" in _message(out)
    with (
        patch.object(sys, "argv", ["oc-hub", "policy-schedule", "sched-sum"]),
        patch.object(cli, "_api", return_value=listed) as api,
    ):
        assert cli.main() in (0, None)
    assert api.call_args.args[:2] == (
        "GET",
        "/api/projects/sched-sum/policy-schedule",
    )
    assert capsys.readouterr().out.strip() == _message(out).strip()

    clock["now"] = at + timedelta(days=1)
    await policy_change.run_due(db)
    after = (await client.get("/api/projects/sched-sum/effective-policy")).json()
    assert after["scheduled"] == []
    change = after["last_change"]
    assert change["actor"] == "schedule" and change["schedule_id"] == 2
    assert change["changes"] == {"deep_daily_cap": {"was": 4, "now": 5}}
    assert "executed from schedule #2" in "\n".join(
        effective_policy.format_effective_policy(after)
    )


async def _patch_policy(client: AsyncClient, pid: int, policy: dict):
    return await client.patch(f"/api/projects/{pid}", json={"gate_policy": policy})


async def test_freeze_policy_is_validated_and_visible_to_agents(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, capsys
):
    # AC-3 (#1594): запись проверяется тем же валидатором, сводка и контекст
    # агента показывают срок и разрешённые типы, снятие убирает ключ.
    pid = await _project(db, "frozen-view", {})
    good = {
        "until": "2026-10-26T00:00:00+00:00",
        "allow_work_types": ["bug", "chore"],
        "note": "до MS-A2",
    }
    ok = await _patch_policy(client, pid, {"freeze": good})
    assert ok.status_code == 200, ok.text
    assert ok.json()["gate_policy"]["freeze"] == good

    # дата без времени — 00:00 UTC этого дня, смещение приводится к UTC
    day = await _patch_policy(
        client, pid, {"freeze": {"until": "2026-10-26", "allow_work_types": []}}
    )
    assert day.status_code == 200, day.text
    assert day.json()["gate_policy"]["freeze"]["until"] == "2026-10-26T00:00:00+00:00"
    shifted = await _patch_policy(
        client,
        pid,
        {"freeze": {"until": "2026-10-26T03:00:00+03:00", "allow_work_types": ["bug"]}},
    )
    assert (
        shifted.json()["gate_policy"]["freeze"]["until"] == "2026-10-26T00:00:00+00:00"
    )

    refusals = {
        "unknown work type": {"allow_work_types": ["bug", "magic"]},
        "garbled date": {"until": "когда-нибудь", "allow_work_types": ["bug"]},
        "time without zone": {"until": "2026-10-26T12:00:00", "allow_work_types": []},
        "no list": {"until": None},
        "types not a list": {"allow_work_types": "bug"},
        "stray key": {"allow_work_types": ["bug"], "types": ["bug"]},
        "note not text": {"allow_work_types": [], "note": 5},
    }
    stored_before = (await repo.get_project(db, pid))["gate_policy"]
    for label, freeze in refusals.items():
        resp = await _patch_policy(client, pid, {"freeze": freeze})
        assert resp.status_code == 422, f"{label}: {resp.text}"
        assert "freeze" in resp.text, label
    unknown = await _patch_policy(
        client, pid, {"freeze": {"allow_work_types": ["magic"]}}
    )
    assert "magic" in unknown.text, "неизвестный тип назван в причине"
    assert (await repo.get_project(db, pid))["gate_policy"] == stored_before

    # прошедший срок — запись валидна и значит «не действует»
    past = {"until": "2020-01-01", "allow_work_types": ["bug"], "note": ""}
    assert (await _patch_policy(client, pid, {"freeze": past})).status_code == 200
    data = (await client.get("/api/projects/frozen-view/effective-policy")).json()
    row = _by_key(data)["freeze"]
    assert row["value"]["active"] is False and row["source"] == "project"
    assert "не действует" in row["shown"]
    # действующая заморозка: срок и типы читаются словами, а не «N rule(s)»
    await _patch_policy(client, pid, {"freeze": dict(good, until=None)})
    data = (await client.get("/api/projects/frozen-view/effective-policy")).json()
    row = _by_key(data)["freeze"]
    assert row["value"] == {
        "until": None,
        "allow_work_types": ["bug", "chore"],
        "note": "до MS-A2",
        "active": True,
    }
    lines = "\n".join(effective_policy.format_effective_policy(data))
    assert "freeze = до снятия (действует); разрешены: bug, chore; до MS-A2" in lines
    assert "rule(s)" not in lines.split("freeze =")[1].splitlines()[0]

    # контекст агента: по слагу, с наследованием из задачи и по default
    async def _fake_get(path: str, **_: object) -> object:
        resp = await client.get(path)
        if resp.status_code >= 400:
            raise mcp_server.HubApiError(
                mcp_server._parse_api_error(resp, resp.status_code)
            )
        return resp.json()

    monkeypatch.setattr(mcp_server, "_api_get", _fake_get)
    named = _message(await mcp_server.hub_my_context(project="frozen-view"))
    assert "Policy of project frozen-view" in named
    assert "freeze = до снятия (действует); разрешены: bug, chore" in named

    default_id = await _project(db, "default", {})
    assert (
        await _patch_policy(
            client, default_id, {"freeze": {"allow_work_types": ["docs"]}}
        )
    ).status_code == 200
    fallback = _message(await mcp_server.hub_my_context())
    assert "Policy of project default" in fallback
    assert "разрешены: docs" in fallback, "без project — политика default"

    both = await mcp_server.hub_my_context(task_id=7, project="frozen-view")
    assert "mutually exclusive" in _message(both)
    missing = _message(await mcp_server.hub_my_context(project="no-such-project"))
    assert "не прочитана" in missing

    # снятие убирает ключ
    gone = await _patch_policy(client, pid, {"freeze": None})
    assert gone.status_code == 200, gone.text
    assert "freeze" not in gone.json()["gate_policy"]
    data = (await client.get("/api/projects/frozen-view/effective-policy")).json()
    assert _by_key(data)["freeze"]["source"] == "default"
    assert _by_key(data)["freeze"]["value"] is None


async def test_unparsable_policy_brief_is_logged_and_still_answers(monkeypatch, caplog):
    # #1594: ответ агенту прежний, но причина больше не пропадает молча.
    import logging

    async def _junk(path: str, **_: object) -> object:
        return {"tasks": []}

    monkeypatch.setattr(mcp_server, "_api_get", _junk)
    with caplog.at_level(logging.WARNING, logger="hub.mcp_server"):
        text = await mcp_server._policy_brief_for_slug("spike")
    assert "не прочитана" in text
    assert any("KeyError" in r.getMessage() for r in caplog.records)


async def test_summary_names_partial_lock_and_counts_actual_steward_verdicts(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, capsys
):
    """#1602 AC-4: замок частичный, фактическое отдельно, считаются ТОЛЬКО вердикты."""
    pid = await _project(db, "default", {"verdict": "human"})
    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"verdict": "steward"}}
    )
    assert resp.status_code == 200, resp.text
    spike = await _project(db, "spike", {})

    async def task(project_id: int | None) -> int:
        task_id = await repo.create_task(
            db,
            title="t",
            description="",
            runtime="auto",
            source="human",
            assigned_agent="",
            rationale="",
            status="review",
            auto_review=False,
            task_type="task",
            parent_id=None,
            priority="medium",
        )
        if project_id is not None:
            await repo.update_task(db, task_id, project_id=project_id)
        return task_id

    async def event(kind: str, task_id: int, actor: str, **payload) -> int:
        return await repo.insert_event(
            db, kind=kind, task_id=task_id, actor=actor, payload=payload
        )

    first, second, shadow = await task(None), await task(None), await task(None)
    foreign = await task(spike)
    for task_id, gen in ((first, 1), (second, 2)):
        await event(
            "review_verdict_recorded",
            task_id,
            "steward",
            verdict="approved",
            submission_generation=gen,
            source="steward_applied",
        )
    # Повтор того же поколения — одно поколение, один вердикт.
    await event(
        "review_verdict_recorded",
        first,
        "steward",
        verdict="approved",
        submission_generation=1,
        source="steward_applied",
    )
    # #1602: имя steward в теле запроса — не вердикт стюарда: метки применения нет.
    await event(
        "review_verdict_recorded",
        shadow,
        "steward",
        verdict="approved",
        submission_generation=3,
    )
    # Человеческий вердикт и вердикт стюарда на чужом проекте не считаются.
    await event(
        "review_verdict_recorded",
        shadow,
        "denis",
        verdict="approved",
        submission_generation=1,
    )
    await event(
        "review_verdict_recorded",
        foreign,
        "steward",
        verdict="approved",
        submission_generation=1,
        source="steward_applied",
    )
    # Теневые суждения (5) и DoR-суждение (1): steward_applied вердиктом не является.
    for _ in range(5):
        await event("steward_applied", shadow, "steward", verdict="approve")
    await event("steward_applied", shadow, "steward", gate="dor")
    # Вердикт стюарда за окном.
    old = await event(
        "review_verdict_recorded",
        shadow,
        "steward",
        verdict="approved",
        submission_generation=9,
        source="steward_applied",
    )
    await db.execute(
        "UPDATE events SET created_at=datetime('now','-200 days') WHERE id=?", (old,)
    )
    await db.commit()

    data = (await client.get("/api/projects/default/effective-policy")).json()
    lock = {item["id"]: item for item in data["locks"]}["#743"]
    assert lock["applies"] is True
    assert lock["allowed"] == ["verdict=steward"]
    assert lock["allowed_note"]["verdict=steward"] == "решение владельца от 06.10.2026"
    assert set(lock["refused"]) == {"dor=auto", "dor=steward", "verdict=auto"}
    # Фактическое значение и время правки — отдельно от решения владельца.
    assert lock["actual"]["verdict"] == "steward"
    assert lock["actual"]["changed_at"]
    assert data["steward_verdicts"]["count"] == 2
    assert data["steward_verdicts"]["window_days"] == 14

    other = (await client.get("/api/projects/spike/effective-policy")).json()
    assert other["steward_verdicts"]["count"] == 1
    assert {i["id"]: i for i in other["locks"]}["#743"]["applies"] is False

    # Та же цифра в practice_metrics.
    from hub.services import orchestration

    metrics = await orchestration._steward_shadow_metrics(db)
    assert metrics["actual_verdicts"]["by_project"] == {"default": 2, "spike": 1}
    assert metrics["actual_verdicts"]["total"] == 3
    assert metrics["actual_verdicts"]["window_days"] == 14

    text = "\n".join(effective_policy.format_effective_policy(data))
    assert "verdict=steward" in text and "06.10.2026" in text
    assert "Steward verdicts: 2" in text


async def test_ci_before_submit_is_registered_and_validated(
    client: AsyncClient, db: aiosqlite.Connection
):
    """AC-4 (#1629): три режима приняты и видны в сводке, чужое значение — 422."""
    from hub.services import lifecycle

    pid = await _project(db, "cbs-policy", {})
    for mode in ("require", "warn", "off"):
        ok = await _patch_policy(client, pid, {"ci_before_submit": mode})
        assert ok.status_code == 200, ok.text
        assert ok.json()["gate_policy"]["ci_before_submit"] == mode
        row = _by_key(
            (await client.get("/api/projects/cbs-policy/effective-policy")).json()
        )["ci_before_submit"]
        assert row["value"] == mode and row["source"] == "project"
        assert row["reader"] == "project_policy.ci_before_submit_of"
    bad = await _patch_policy(client, pid, {"ci_before_submit": "maybe"})
    assert bad.status_code == 422, bad.text

    step = next(s for s in lifecycle.HEADLESS_STEPS if s.name == "ci_before_submit")
    assert not step.active and "#1122" in step.inactive_reason


def test_ci_before_submit_reader_modes():
    from hub.services.project_policy import ci_before_submit_of

    assert ci_before_submit_of({}) == "off"
    assert ci_before_submit_of({"ci_before_submit": "require"}) == "require"
    assert ci_before_submit_of({"ci_before_submit": "requier"}) == "warn"


async def test_local_review_fallback_is_reported_on_every_surface(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, capsys
):
    """AC-7 (#1653): ключ принимает off|on, null снимает, REST, CLI и MCP говорят одно."""
    assert "local_review_fallback" in GATE_POLICY_KEYS
    pid = await _project(db, "lrf-surfaces", {"verdict": "human"})
    slug = "lrf-surfaces"

    async def _via_client(path: str, **_: object) -> object:
        return (await client.get(path)).json()

    monkeypatch.setattr(mcp_server, "_api_get", _via_client)

    async def _surfaces(value: str, source: str) -> None:
        resp = await client.get(f"/api/projects/{slug}/effective-policy")
        rest = resp.json()
        row = _by_key(rest)["local_review_fallback"]
        assert (row["value"], row["source"]) == (value, source), row
        assert row["default"] == "off"
        out = await mcp_server.hub_effective_policy(slug)
        mcp_row = _by_key(out.structuredContent)["local_review_fallback"]
        assert (mcp_row["value"], mcp_row["source"]) == (value, source)
        assert f"local_review_fallback = {value} [{source}]" in _message(out)
        argv = ["oc-hub", "effective-policy", slug, "--json"]
        with (
            patch.object(sys, "argv", argv),
            patch.object(cli, "_api", return_value=rest),
        ):
            cli.main()
        cli_row = _by_key(json.loads(capsys.readouterr().out))["local_review_fallback"]
        assert (cli_row["value"], cli_row["source"]) == (value, source)

    await _surfaces("off", "default")

    bad = await _patch_policy(client, pid, {"local_review_fallback": "maybe"})
    assert bad.status_code == 422, bad.text
    stored = (await repo.get_project(db, pid))["gate_policy"]
    assert "local_review_fallback" not in json.loads(stored), "БД не изменилась"
    await _surfaces("off", "default")

    ok = await _patch_policy(client, pid, {"local_review_fallback": "on"})
    assert ok.status_code == 200, ok.text
    await _surfaces("on", "project")

    # Пропущенный ключ сохраняется.
    other = await _patch_policy(client, pid, {"ci_before_submit": "warn"})
    assert other.status_code == 200, other.text
    assert other.json()["gate_policy"]["local_review_fallback"] == "on"

    cleared = await _patch_policy(client, pid, {"local_review_fallback": None})
    assert cleared.status_code == 200, cleared.text
    assert "local_review_fallback" not in cleared.json()["gate_policy"]
    await _surfaces("off", "default")


def test_local_review_fallback_reader_defaults_to_off():
    """AC-7 (#1653): нет ключа и нечитаемое значение — off; читатель один."""
    reader = project_policy.local_review_fallback_of
    assert reader({}) == "off"
    assert reader({"local_review_fallback": "on"}) == "on"
    assert reader({"local_review_fallback": "ON"}) == "off"
    assert reader({"local_review_fallback": True}) == "off"
    assert reader({"local_review_fallback": 1}) == "off"
    assert reader(None) == "off"  # type: ignore[arg-type]
