"""#1933: у файла выше потолка тесты AC названы первыми, непрочитанный тест AC — строкой.

Машинное ревью файла с диффом больше REVIEW_FILE_LINE_CAP раньше говорило
«прочитай сколько успеешь», и непрочитанным оказывался хвост тестов, которые
доказывают AC (#1874, #1862). Теперь хаб называет их диапазоны строк раньше
команды и текста диффа, а то, что ревьюер не прочитал, читается из его
lost_dimensions точным совпадением со списком test_ref задачи.
"""

from __future__ import annotations

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.services.review_dispatch import (
    REVIEW_FILE_LINE_CAP,
    _inline_diff_lines,
    diff_plan,
)
from tests.review_dispatch_support import (
    _DispatchRecorder,
    _submitted,
    _wire,
)

BIG = "tests/test_big.py"
ALPHA = f"{BIG}::test_alpha"
METHOD = f"{BIG}::TestKit::test_method"
VITEST = "web/src/panel.test.ts::still toggles"
PREFIX = "ac_test_unread: "

# Раскладка файла на 1200 строк: функции AC лежат в строках 900–1100.
ALPHA_AT, ALPHA_TO = 900, 959  # def test_alpha ... последняя строка тела
CLASS_AT = 1000
METHOD_AT, METHOD_TO = 1001, 1100


def _big_lines() -> list[str]:
    lines = [f"# filler {n}" for n in range(1, ALPHA_AT)]
    lines.append("def test_alpha():")
    lines += [f"    assert ALPHA_BODY_{n}" for n in range(ALPHA_AT + 1, ALPHA_TO + 1)]
    lines += [""] * (CLASS_AT - ALPHA_TO - 1)
    lines.append("class TestKit:")
    lines.append("    def test_method(self):")
    lines += [
        f"        assert METHOD_BODY_{n}" for n in range(METHOD_AT + 1, METHOD_TO)
    ]
    lines.append("        assert METHOD_LAST")
    lines += [f"# tail {n}" for n in range(METHOD_TO + 1, 1201)]
    assert len(lines) == 1200
    assert lines[ALPHA_AT - 1] == "def test_alpha():"
    assert lines[METHOD_AT - 1] == "    def test_method(self):"
    return lines


SOURCE = "\n".join(_big_lines()) + "\n"  # файл теста на закреплённом sha
SRC = {BIG: SOURCE}


def _new_file(path: str, lines: list[str]) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n" + body
    )


def _small_file(path: str) -> str:
    return _new_file(path, ["def helper():", "    return SMALL_FILE_MARK"])


@pytest.fixture
def big_diff() -> str:
    return _new_file(BIG, _big_lines()) + _small_file("hub/small.py")


def _first_block(prompt: str) -> list[str]:
    return [ln for ln in prompt.splitlines() if ln.startswith("ЧИТАТЬ ПЕРВЫМИ:")]


def _assert_first_block(prompt: str, *, before: str) -> None:
    lines = _first_block(prompt)
    assert (f"ЧИТАТЬ ПЕРВЫМИ: {BIG}:{ALPHA_AT}–{ALPHA_TO} ({ALPHA})") in lines, lines
    assert (f"ЧИТАТЬ ПЕРВЫМИ: {BIG}:{METHOD_AT}–{METHOD_TO} ({METHOD})") in lines, lines
    assert any(VITEST in ln and "не найден в диффе" in ln for ln in lines), lines
    assert prompt.index("ЧИТАТЬ ПЕРВЫМИ") < prompt.index(before), (
        "блок стоит раньше команды и текста диффа"
    )
    assert "ПЕРВЫМИ" in prompt and "остаток" in prompt, "порядок чтения назван"


_REFS = [ALPHA, METHOD, VITEST]


async def test_ac_tests_are_named_first_in_an_oversized_file(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, big_diff: str
):
    """AC-1: блок «ЧИТАТЬ ПЕРВЫМИ» раньше диффа; облако, локальный путь и заказ."""
    # Облачный путь: функция диффа напрямую, команда git diff — после блока.
    block, _ = diff_plan(
        big_diff, "develop", "task-1/x", ac_test_refs=_REFS, ac_sources=SRC
    )
    _assert_first_block(block, before="git diff")

    # Номера строк облачного блока — строки файла на закреплённом sha сдачи.
    moved = _new_file(BIG, ["# сдвиг ветки"] + _big_lines())
    pinned, _ = diff_plan(
        moved,
        "develop",
        "task-1/x",
        ac_test_refs=_REFS,
        ac_sources=SRC,
        pinned_diff=big_diff,
    )
    assert f"{BIG}:{ALPHA_AT}–{ALPHA_TO} ({ALPHA})" in pinned, pinned

    # Локальный путь: файл целиком не влез в потолок, функции вложены вырезкой.
    ceiling = 9000
    assert len(big_diff) > ceiling
    local, _ = diff_plan(
        big_diff,
        "develop",
        "task-1/x",
        ac_test_refs=_REFS,
        ac_sources=SRC,
        inline_ceiling=ceiling,
    )
    _assert_first_block(local, before="=== ДИФФ")
    assert "ALPHA_BODY_950" in local and "METHOD_LAST" in local, (
        "функции test_ref вложены, хотя файл целиком не влез"
    )
    assert "filler 5\n" not in local, "вырезка — функции, а не файл"
    assert "SMALL_FILE_MARK" in local, "малый файл читается как обычно"
    assert local.index("ALPHA_BODY_950") < local.index("=== ДИФФ"), (
        "вырезка стоит раньше остального диффа"
    )

    # Заказ: AC берутся из карточки задачи на месте вызова diff_plan.
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    acs = [
        {
            "id": f"AC-{n}",
            "given": "g",
            "when": "w",
            "then": "t",
            "verifiable_by": "test",
            "test_ref": ref,
        }
        for n, ref in enumerate(_REFS, 1)
    ]
    await _submitted(client, db, "oversized-order", diff=big_diff, acs=acs, files=SRC)
    prompt = recorder.calls[0]["prompt_text"]
    _assert_first_block(prompt, before="git diff")


async def test_the_order_without_ac_tests_has_no_first_block(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, big_diff: str
):
    """Нет test_ref у задачи — прежний промт: ни блока, ни строки «не найден»."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-2"}, "run": {"id": "r-2"}})
    _wire(monkeypatch, recorder)
    await _submitted(client, db, "oversized-no-ac", diff=big_diff)
    prompt = recorder.calls[0]["prompt_text"]
    assert "ЧИТАТЬ ПЕРВЫМИ" not in prompt
    assert "НЕ ПОМЕСТЯТСЯ В ОДИН ПРОХОД" in prompt, "потолок назван, как раньше"


def test_a_diff_without_an_oversized_file_has_no_first_block():
    small = _small_file("tests/test_small.py")
    block, _ = diff_plan(small, "develop", "b", ac_test_refs=[ALPHA])
    assert "ЧИТАТЬ ПЕРВЫМИ" not in block
    assert block == diff_plan(small, "develop", "b")[0], "и промт прежний"


def test_a_parameter_id_is_dropped_and_a_missing_function_is_named():
    diff = _new_file(BIG, _big_lines())
    refs = [f"{ALPHA}[case-1]", f"{BIG}::test_gone"]
    block, _ = diff_plan(diff, "develop", "b", ac_test_refs=refs, ac_sources=SRC)
    lines = _first_block(block)
    assert f"ЧИТАТЬ ПЕРВЫМИ: {BIG}:{ALPHA_AT}–{ALPHA_TO} ({refs[0]})" in lines
    gone = [ln for ln in lines if refs[1] in ln]
    assert gone and "не найден в диффе" in gone[0]


def test_a_delta_generation_looks_for_the_function_in_the_full_diff():
    # delta_paths не называет большой файл, но oversized считается по полному
    # диффу — и функции AC ищутся там же, как и сам oversized.
    diff = _new_file(BIG, _big_lines()) + _small_file("hub/small.py")
    block, _ = diff_plan(
        diff,
        "develop",
        "b",
        delta_paths=["hub/small.py"],
        delta_note="дельта",
        ac_test_refs=[ALPHA],
        ac_sources=SRC,
    )
    assert f"{BIG}:{ALPHA_AT}–{ALPHA_TO} ({ALPHA})" in block


def test_a_cutout_that_does_not_fit_is_named_unread():
    diff = _new_file(BIG, _big_lines())
    lines, omitted = _inline_diff_lines(diff, [], None, 200)
    assert omitted == [BIG] and "ac_test_unread" not in "\n".join(lines)
    block, _ = diff_plan(
        diff, "develop", "b", ac_test_refs=[ALPHA], ac_sources=SRC, inline_ceiling=200
    )
    assert "ALPHA_BODY_950" not in block
    assert f"{PREFIX}{ALPHA}" in block, "не влезший тест назван непрочитанным"


def test_the_prompt_asks_for_the_unread_line_and_keeps_the_ceiling_wording():
    block, note = diff_plan(
        _new_file(BIG, _big_lines()),
        "develop",
        "b",
        ac_test_refs=[ALPHA],
        ac_sources=SRC,
    )
    assert f"НЕ ПОМЕСТЯТСЯ В ОДИН ПРОХОД (потолок {REVIEW_FILE_LINE_CAP}" in block
    assert PREFIX.strip() in block and "lost_dimensions" in block
    assert "incomplete=true" in block
    assert "крупных файлов: 1" in note


# --- AC-2: непрочитанный тест AC в brief -----------------------------------


async def _task_with_report(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch,
    lost: list[str],
    *,
    incomplete: bool = True,
    slug: str = "unread",
) -> tuple[int, dict]:
    recorder = _DispatchRecorder({"agent": {"id": "bc-3"}, "run": {"id": "r-3"}})
    _wire(monkeypatch, recorder)
    acs = [
        {
            "id": f"AC-{n}",
            "given": "g",
            "when": "w",
            "then": "t",
            "verifiable_by": "test",
            "test_ref": ref,
        }
        for n, ref in enumerate([ALPHA, METHOD], 1)
    ]
    task_id = await _submitted(client, db, f"{slug}-{len(lost)}", acs=acs)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "agent_count": 1,
            "tokens_spent": 12000,
            "model": "grok-4.6",
            "raw_count": 1,
            "findings_confirmed": [],
            "findings_rejected": [],
            "incomplete": incomplete,
            "unresolved": [],
            "lost_dimensions": lost,
            "agent": "cursor-cloud-reviewer",
        },
    )
    assert resp.status_code == 200, resp.text
    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    return task_id, brief["machine_review"]


async def test_an_unread_ac_test_is_named_by_exact_match(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2: «ac_test_unread: <test_ref>» названо полем, посторонний текст — нет."""
    lost = [
        f"{PREFIX}{ALPHA}",
        f"не дочитал {METHOD}, там много строк",  # то же имя без префикса
        METHOD,  # голое имя без префикса
        f"{PREFIX}{METHOD} и ещё что-то",  # префикс, но не точное совпадение
        f"{PREFIX}{BIG}::test_unknown",  # не test_ref этой задачи
        f"AC_TEST_UNREAD: {METHOD}",  # другой регистр префикса
    ]
    _, review = await _task_with_report(client, db, monkeypatch, lost)
    assert review["ac_tests_unread"] == [ALPHA]
    assert review["incomplete"] is True, "отчёт остаётся неполным"
    assert review["lost_dimensions"] == lost, "сами строки не потеряны"


async def test_a_report_without_the_line_names_nothing_unread(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    _, review = await _task_with_report(
        client, db, monkeypatch, ["hub/services/big.py"]
    )
    assert review["ac_tests_unread"] == []


async def test_the_unread_line_forces_the_report_incomplete(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Ревьюер назвал непрочитанный тест AC, но сказал incomplete=false."""
    task_id, review = await _task_with_report(
        client, db, monkeypatch, [f"{PREFIX}{METHOD}"], incomplete=False
    )
    assert review["incomplete"] is True
    assert review["ac_tests_unread"] == [METHOD]
    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["incomplete"] == 1


async def test_the_default_mcp_answer_lists_the_unread_tests(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    import json

    from hub.services import review_brief_projection as proj

    task_id, _ = await _task_with_report(client, db, monkeypatch, [f"{PREFIX}{ALPHA}"])
    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    result = proj.brief_result(brief)
    answer = result.structuredContent["brief"]
    assert answer["machine_review"]["ac_tests_unread"] == [ALPHA]
    assert ALPHA in json.loads(result.content[0].text)["message"], "и в тексте"


async def test_the_task_card_shows_the_unread_ac_test(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    task_id, _ = await _task_with_report(client, db, monkeypatch, [f"{PREFIX}{METHOD}"])
    page = (await client.get(f"/tasks/{task_id}")).text
    assert f"тест AC не прочитан: {METHOD}" in page
    quiet, _ = await _task_with_report(
        client, db, monkeypatch, ["hub/x.py"], slug="quiet"
    )
    assert "тест AC не прочитан" not in (await client.get(f"/tasks/{quiet}")).text
