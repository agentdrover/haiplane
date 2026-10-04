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
from hub.services import effective_policy, project_policy

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
