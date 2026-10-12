"""Hub-dispatched cross-model reviews (#757): local_prompt (split of the former single file, #1670)."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub.integrations import cursor_cloud, local_reviewer
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import TaskRefine
from hub.services.review_dispatch import (
    DEEP,
    LITE,
    sweep_review_dispatches,
)
from tests.review_dispatch_support import (
    _DIFF_506,
    _LIMIT_REFUSAL,
    _LOCAL_FIRST,
    _LOCAL_REPORT,
    _QWEN,
    _TIP,
    _all_dispatches,
    _card_text,
    _DispatchRecorder,
    _docs_diff,
    _local_dispatches,
    _local_principal,
    _machine_report,
    _no_local_path,
    _node,
    _pinned_setup,
    _PinnedGitOps,
    _qwen_reviewer,
    _reporting_stub,
    _stub_reviewer,
    _submitted,
    _wire,
)


# --- #1582: локальный ревьюер читает дифф в промте, а не командой git diff ----


def _file_diff(path: str, body: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        f"-OLD_{body}\n"
        f"+NEW_{body}\n"
    )


_INLINE_DIFF = (
    _file_diff("hub/alpha.py", "ALPHA")
    + _file_diff("uv.lock", "LOCKED_NOISE")
    + _file_diff("hub/beta.py", "BETA")
)


async def _local_prompt(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch,
    tmp_path,
    slug: str,
    diff: str,
    transport: str = "direct",
) -> tuple[str, str, int]:
    """Локальный заказ на GitVerse: ``(промт у шва, промт у ревьюера, task_id)``.

    «У ревьюера» — то, что реально дошло до процесса: stdin при ``direct`` и
    ``prompt.txt`` задания при ``runner`` (#1571).
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-x"}, "run": {"id": "r-x"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    seen: list[str] = []
    real_run = local_reviewer.run_review

    async def _watch(prompt, *, timeout=None, prompt_at_slot=None):
        seen.append(prompt)
        return await real_run(prompt, timeout=timeout, prompt_at_slot=prompt_at_slot)

    monkeypatch.setattr(local_reviewer, "run_review", _watch)
    stdin_file = tmp_path / "stdin.txt"
    _stub_reviewer(
        monkeypatch,
        tmp_path,
        f"import sys\nopen({str(stdin_file)!r}, 'w').write(sys.stdin.read())\n"
        + _reporting_stub(),
    )
    if transport == "runner":
        spool = tmp_path / "spool"
        spool.mkdir()
        (spool / "heartbeat").write_text("{}")
        monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "runner")
        monkeypatch.setattr(config, "LOCAL_REVIEW_SPOOL_DIR", str(spool))
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "")
        monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "")
        monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", "")

        async def _the_service(jobdir, limit, started):
            stdin_file.write_text((Path(jobdir) / "prompt.txt").read_text())
            return local_reviewer.LocalRun(
                rc=1, output="", dropped=0, timed_out=False, duration_ms=1
            )

        monkeypatch.setattr(local_reviewer, "_await_result", _the_service)
    task_id = await _submitted(
        client,
        db,
        slug,
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
        diff=diff,
    )
    await wait_for_local_runs()
    await db.commit()
    assert recorder.calls == [], "облако на этом форже не зовут"
    assert len(seen) == 1, "локальный прогон один"
    return seen[0], stdin_file.read_text(), task_id


@pytest.mark.parametrize("transport", ("direct", "runner"))
async def test_a_local_review_prompt_carries_the_diff_inline(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch,
    tmp_path,
    transport: str,
):
    """AC-1: ревьюер без клона получает сам дифф, с закреплённого sha, без lock."""
    asked: list[str] = []
    real = _PinnedGitOps.branch_diff

    async def _recording(self, repo_, base, branch, context=0):
        asked.append(branch)
        return await real(self, repo_, base, branch)

    monkeypatch.setattr(_PinnedGitOps, "branch_diff", _recording)
    prompt, stdin_text, task_id = await _local_prompt(
        client, db, monkeypatch, tmp_path, "inline-diff", _INLINE_DIFF, transport
    )
    task = dict(await repo.get_task(db, task_id))
    for text in (prompt, stdin_text):
        assert "NEW_ALPHA" in text and "NEW_BETA" in text, (
            "текст диффа обоих файлов лежит в промте"
        )
        assert "OLD_ALPHA" in text, "минусовые строки тоже: это дифф, а не список"
        assert "LOCKED_NOISE" not in text, "сгенерированный файл не входит"
        assert "uv.lock" in text, "но исключение названо — молча ничего не пропадает"
        assert "КОМАНДОЙ ИЗ ПРЕДМЕТА" not in text, "и шаг порядка не шлёт к команде"
        assert "git diff" not in text, (
            "клона у ревьюера нет: велеть ему выполнить git diff значит "
            "получить отчёт «смотреть было нечем»"
        )
    assert task["submission_sha"], "сдача закрепила sha"
    assert task["submission_sha"] in asked, (
        "дифф снят на закреплённом sha (#824), а не по имени ветки"
    )


async def test_an_oversized_local_diff_is_cut_by_file_and_named(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-2: сверх потолка файлы целиком до него, остальные названы поимённо."""
    monkeypatch.setattr(config, "LOCAL_REVIEW_DIFF_CHAR_CEILING", 700)
    huge = "\n".join(f"+HUGE_LINE_{n}" for n in range(200))
    diff = (
        _file_diff("hub/small_one.py", "SMALL_ONE")
        + _file_diff("hub/huge.py", "HUGE")
        + huge
        + "\n"
        + _file_diff("hub/small_two.py", "SMALL_TWO")
    )
    prompt, stdin_text, _ = await _local_prompt(
        client, db, monkeypatch, tmp_path, "inline-cut", diff
    )
    for text in (prompt, stdin_text):
        assert "NEW_SMALL_ONE" in text and "NEW_SMALL_TWO" in text, (
            "файлы, что помещаются, включены целиком"
        )
        assert "HUGE_LINE_" not in text, "файл, что не вошёл, не режется посередине"
        assert re.search(r"НЕ ВОШЛИ[^\n]*hub/huge\.py", text), (
            "не вошедший файл назван поимённо, чтобы ревьюер поднял incomplete"
        )
        assert "incomplete=true" in text and "lost_dimensions" in text, (
            "ревьюеру сказано, что делать с непокрытым"
        )


async def test_the_cloud_prompt_keeps_the_diff_command(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3: у облачного заказа на ту же сдачу всё как до задачи — команда."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-c"}, "run": {"id": "r-c"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(
        client, db, "cloud-keeps", policy={"review": "dispatch"}, diff=_INLINE_DIFF
    )
    assert len(recorder.calls) == 1
    prompt = recorder.calls[0]["prompt_text"]
    task = dict(await repo.get_task(db, task_id))
    assert re.search(
        rf"git diff \S+\.\.\.{re.escape(task['branch'])} -- "
        r"':\(exclude\)uv\.lock'",
        prompt,
    ), "команда диффа с исключением сгенерированного на месте"
    assert "NEW_ALPHA" not in prompt and "OLD_ALPHA" not in prompt, (
        "облаку дифф не инлайнится: у агента есть клон"
    )


_SQL_HUNK_DIFF = (
    "diff --git a/db/q.sql b/db/q.sql\n"
    "--- a/db/q.sql\n"
    "+++ b/db/q.sql\n"
    "@@ -1,3 +1,3 @@\n"
    "--- OLD_SQL_COMMENT\n"
    "+++ NEW_PLUS_PLUS_LINE\n"
    "+AFTER_THE_LOOKALIKES\n" + _file_diff("hub/next.py", "NEXT")
)


def test_a_hunk_line_that_looks_like_a_file_header_does_not_split_the_file():
    """Находка 523e42c1: «--- …»/«+++ …» в теле хунка — строки правки, не заголовок."""
    from hub.services.review_dispatch import diff_file_sections

    sections = diff_file_sections(_SQL_HUNK_DIFF)
    assert [p for p, _ in sections] == ["db/q.sql", "hub/next.py"]
    body = dict(sections)["db/q.sql"]
    assert "OLD_SQL_COMMENT" in body and "NEW_PLUS_PLUS_LINE" in body
    assert "AFTER_THE_LOOKALIKES" in body, "хвост правки не пропал"


async def test_the_local_prompt_keeps_a_hunk_with_header_lookalikes(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    prompt, _, _ = await _local_prompt(
        client, db, monkeypatch, tmp_path, "inline-lookalike", _SQL_HUNK_DIFF
    )
    assert "AFTER_THE_LOOKALIKES" in prompt and "NEW_PLUS_PLUS_LINE" in prompt


async def test_the_local_diff_is_read_with_context_and_the_profile_without(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Без клона окружающий код ревьюеру взять негде: инлайн снимается с контекстом."""
    asked: list[tuple[str, int]] = []
    real = _PinnedGitOps.branch_diff

    async def _with_context(self, repo_, base, branch, context=0):
        asked.append((branch, context))
        got = await real(self, repo_, base, branch)
        if context:
            got = got.replace(
                "@@ -1 +1 @@\n", "@@ -1,3 +1,3 @@\n NEIGHBOUR_FUNCTION_LINE\n", 1
            )
        return got

    monkeypatch.setattr(_PinnedGitOps, "branch_diff", _with_context)
    prompt, _, task_id = await _local_prompt(
        client, db, monkeypatch, tmp_path, "inline-context", _INLINE_DIFF
    )
    task = dict(await repo.get_task(db, task_id))
    assert "NEIGHBOUR_FUNCTION_LINE" in prompt, "неизменённая строка рядом в промте"
    assert (task["submission_sha"], config.LOCAL_REVIEW_DIFF_CONTEXT_LINES) in asked
    assert config.LOCAL_REVIEW_DIFF_CONTEXT_LINES >= 10
    assert any(ctx == 0 for _, ctx in asked), "расчёт профиля остался на -U0"


# --- #1584: одна финальная сдача, без ранней пробной ---


def _matrix_prompt(profile: str, http: bool) -> str:
    from hub.services.review_dispatch import _delivery_block, _review_prompt

    delivery = _delivery_block(1584, "CODE123", "https://hub.example") if http else ""
    return _review_prompt(
        1584,
        "task-1584/x",
        "grok-4.6",
        profile,
        "RULES",
        "DIFF",
        "PREPASS",
        delivery_block=delivery,
    )


@pytest.mark.parametrize("http", [False, True])
@pytest.mark.parametrize("profile", [LITE, DEEP])
def test_review_prompt_demands_a_single_final_submission(
    profile: str, http: bool
) -> None:
    """AC-1: во всех клетках lite/deep x HTTP/MCP одна финальная сдача, запрет
    ранней и пробной, честный raw_count=0 разрешён, текстовый блок на месте."""
    from hub.services.review_dispatch import REPORT_FENCE

    prompt = _matrix_prompt(profile, http)
    assert "РОВНО ОДНА финальная сдача" in prompt
    assert "РАННЯЯ" in prompt and "пробная" in prompt, "ранняя сдача запрещена"
    assert "чтением брифа" in prompt, "доступ проверяется чтением, не сдачей"
    assert "raw_count=0" in prompt and "разрешён" in prompt, "честный ноль разрешён"
    assert f"```{REPORT_FENCE}" in prompt, "текстовый блок остаётся"
    if http:
        # Ни чтение брифа, ни сдача не велят MCP-вызов (скилл deep — вне задачи).
        assert "hub_get_review_brief" not in prompt
        assert "hub_submit_machine_review" not in prompt
        assert "/review-brief" in prompt and "/machine-review" in prompt
    else:
        assert "hub_get_review_brief" in prompt
        assert "hub_submit_machine_review" in prompt


# --- #1586: харнесс deep на HTTP-пути берётся из брифа ---


def test_deep_http_prompt_takes_the_harness_from_the_brief() -> None:
    """AC-3: HTTP deep не велит hub_get_skill, берёт харнесс из поля брифа и
    несёт harness_skill/harness_version в оба способа сдачи; MCP-шаг цел."""
    http = _matrix_prompt(DEEP, True)
    assert "hub_get_skill" not in http
    assert "harness_skill" in http and "harness_version" in http
    assert "поля harness_skill брифа" in http, "источник — поле брифа"
    assert "lost_dimensions" in http and "incomplete" in http
    # Метаданные и в HTTP-шаге сдачи, и в финальном текстовом блоке.
    step = http.rsplit("Порядок:", 1)[1]
    assert "harness_skill.name" in step and "harness_skill.version" in step, (
        "шаг сдачи по HTTP берёт имя и версию из брифа"
    )
    assert '"harness_version"' in http, "финальный текстовый блок"

    mcp = _matrix_prompt(DEEP, False)
    assert "hub_get_skill('multi-agent-review')" in mcp
    assert "поля harness_skill брифа" not in mcp

    # lite харнесса не требует и версию не выдумывает.
    lite = _matrix_prompt(LITE, True)
    assert "multi-agent-review" not in lite and "hub_get_skill" not in lite


# ---------------------------------------------------------------------------
# #1588: выкладка хаба — новые локальные прогоны не стартуют
# ---------------------------------------------------------------------------


def _runner_spool(monkeypatch, tmp_path) -> Path:
    """Локальный путь по транспорту runner, готовый целиком: служба жива."""
    spool = tmp_path / "drain-spool"
    spool.mkdir(mode=0o770)
    (spool / "heartbeat").write_text("{}")
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "runner")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SPOOL_DIR", str(spool))
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_MODEL", _QWEN)
    return spool


def _marker_body(ahead: int) -> bytes:
    import time

    return f"owner=deploy-1588\nexpires={int(time.time()) + ahead}\n".encode()


async def test_local_first_falls_back_to_cloud_while_draining(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-4 (#1588): во время выкладки local-first уходит в облако с причиной.

    Локальный путь настроен целиком, принципал действителен, облако разрешено;
    маркер выкладки свежий. Старт хаба маркер не снимает, правка проекта без
    облака не получает 422 (запрет не живёт в review_reach), а просроченный
    маркер заказу не мешает.
    """
    from hub.services.policy_change import PolicyRefused
    from hub.services.policy_change import (
        refuse_unrunnable_review as _refuse_unrunnable_review,
    )
    from hub.services.review_dispatch import review_reach, wait_for_local_runs
    from tests.test_diagnostics import _running_app

    recorder = _DispatchRecorder({"agent": {"id": "bc-dr"}, "run": {"id": "r-dr"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    spool = _runner_spool(monkeypatch, tmp_path)
    marker = spool / "draining"
    fresh = _marker_body(900)
    marker.write_bytes(fresh)

    # Старт хаба (настоящий lifespan) маркер не снимает и не правит.
    async with _running_app():
        assert marker.read_bytes() == fresh, "старт хаба тронул маркер выкладки"
        assert local_reviewer.drain_refusal(), "после старта запрет всё ещё действует"
    assert marker.read_bytes() == fresh

    # Достижимость не меняется: ни форма проекта, ни инвариант записи её не
    # знают. Локальный путь на gitverse остаётся способом, а правка проекта —
    # допустимой; на контроле без локального пути та же правка получает 422.
    reach = await review_reach(db, "gitverse")
    assert reach.runnable and not reach.local_missing, "выкладка не ломает готовность"
    pid = await repo.create_project(
        db, slug="drain-edit", name="D", repo_name="o/r", workspace_path="/tmp/ws"
    )
    await repo.update_project(
        db, pid, gate_policy=json.dumps({"review": "dispatch", "verdict": "auto"})
    )
    await db.commit()
    before = await repo.get_project(db, pid)
    await _refuse_unrunnable_review(db, before, {"forge": "gitverse"})
    await _refuse_unrunnable_review(
        db, before, {"gate_policy": json.dumps({"review": "dispatch"})}
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "direct")
    with pytest.raises(PolicyRefused) as refused:
        await _refuse_unrunnable_review(db, before, {"forge": "gitverse"})
    assert refused.value.status == 422, "контроль: без пути запись отказывает"
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", "runner")

    # local-first deep во время выкладки: облако, причина в карточке, локального
    # заказа нет.
    task_id = await _submitted(
        client, db, "lf-drain", policy=_LOCAL_FIRST, diff=_DIFF_506
    )
    await wait_for_local_runs()
    await db.commit()
    assert len(recorder.calls) == 1, "облако заказано вместо локального"
    rows = await _all_dispatches(db, task_id)
    assert [(r["channel"], r["profile"], r["status"]) for r in rows] == [
        ("cloud", "deep", "active")
    ], rows
    card = await _card_text(db, task_id)
    assert "идёт выкладка" in card, card
    assert "deep_reviewer" in card, "и сказано, что облако вместо локального"
    assert [p for p in spool.iterdir() if p.name.startswith("job-")] == [], (
        "ни одного задания службе"
    )
    assert marker.read_bytes() == fresh, "заказ чужой маркер не снял"

    # Просроченный маркер заказу не мешает: local-first идёт локально.
    marker.write_bytes(_marker_body(-30))

    async def _the_service(jobdir, limit, started):
        report = json.dumps(_LOCAL_REPORT, ensure_ascii=False)
        return local_reviewer.LocalRun(
            rc=0,
            output=f"```haiplane-review\n{report}\n```",
            dropped=0,
            timed_out=False,
            duration_ms=1,
        )

    monkeypatch.setattr(local_reviewer, "_await_result", _the_service)
    recorder.calls.clear()
    later = await _submitted(
        client, db, "lf-expired", policy=_LOCAL_FIRST, diff=_DIFF_506
    )
    await wait_for_local_runs()
    await db.commit()
    assert recorder.calls == [], "просроченный маркер увёл заказ в облако"
    assert [(r["channel"], r["profile"]) for r in await _all_dispatches(db, later)] == [
        ("local", "deep")
    ]


async def test_no_cloud_forge_names_the_deploy_instead_of_a_verdict_less_silence(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1588: форж без облака во время выкладки — вердикт человеку с причиной."""
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-gv"}, "run": {"id": "r-gv"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    spool = _runner_spool(monkeypatch, tmp_path)
    (spool / "draining").write_bytes(_marker_body(900))

    task_id = await _submitted(
        client,
        db,
        "gv-drain",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
        diff=_DIFF_506,
    )
    await wait_for_local_runs()
    await db.commit()
    assert recorder.calls == [], "на этом форже облако не зовут"
    assert await _all_dispatches(db, task_id) == []
    card = await _card_text(db, task_id)
    assert "идёт выкладка" in card and "Вердикт остаётся человеку" in card, card


# --- #1598: локальный промт называет реальные возможности ревьюера ----------

_CLOUD_PROMPT_FIXTURES = Path(__file__).parent / "fixtures" / "cloud_review_prompt"

# Команды, которые общий промт и харнесс велят исполнить, а у локального
# ревьюера исполнять нечем (#1180). Строки — именно предписания, а не слова.
_LOCAL_FORBIDDEN_ORDERS = (
    "uv run pytest",
    "python -m pytest",
    "pip install",
    "git fetch",
    "apt-get",
    "docker.io",
    "dockerd",
    "СНАЧАЛА ПОПРОБУЙ",
    "запускать проверки",
    "Поставить во временную машину",
    "Чтение кода вместо прогона тестов прогоном не называется",
    "нет чем запустить проверки",
)


def _local_matrix_prompt(profile: str, http: bool, container: bool) -> str:
    from hub.services.review_dispatch import _delivery_block, _review_prompt

    delivery = _delivery_block(1598, "CODE123", "https://hub.example") if http else ""
    return _review_prompt(
        1598,
        "task-1598/x",
        "qwen3.8-max",
        profile,
        "RULES",
        "DIFF",
        "PREPASS",
        delivery_block=delivery,
        needs_container=container,
        inline_diff=True,
    )


_DEMANDING_RULES = (
    "ПРАВИЛА ПРОЕКТА: обязательно прочитай исходники вне диффа; запусти "
    "`uv run pytest -q`; сделай `git fetch origin develop`; unknown запрещён."
)


def _realistic_local_prompt(profile: str, http: bool) -> str:
    """Локальный промт с настоящими блоками: правила, prepass, only_tests."""
    from hub.models import PrepassState
    from hub.services import call_sites, review_evidence
    from hub.services.review_dispatch import _delivery_block, _review_prompt

    symbol = call_sites.SymbolReport(
        symbol="registry_target",
        defined_in="hub/alpha.py",
        state=call_sites.ONLY_TESTS,
    )
    return _review_prompt(
        1598,
        "task-1598/x",
        "qwen3.8-max",
        profile,
        _DEMANDING_RULES,
        "ПРЕДМЕТ РЕВЬЮ — дифф ПРИЛОЖЕН НИЖЕ",
        review_evidence.prepass_block(PrepassState(state="unknown", reason="нет")),
        delivery_block=(
            _delivery_block(1598, "CODE123", "https://hub.example") if http else ""
        ),
        only_tests_block=call_sites.only_tests_block([symbol]),
        inline_diff=True,
    )


def _assert_capability_block_outranks_demands(prompt: str, where: str) -> None:
    caps = prompt.index("ВОЗМОЖНОСТИ ЛОКАЛЬНОГО РЕВЬЮЕРА")
    assert caps < prompt.index("обязательно прочитай исходники вне диффа"), where
    assert caps < prompt.index("ПРОВЕРЬ ДОСТИЖИМОСТЬ"), where
    assert "читать исходники вне диффа нельзя" in prompt, where
    assert "«не установлено» допустимый исход" in prompt, where
    assert "unresolved или lost_dimensions" in prompt, where
    assert "не знаешь" in prompt, where


async def test_a_local_review_prompt_states_its_real_capabilities():
    """AC-1 (#1598): во всех сборках локального промта есть блок возможностей.

    Сборки: lite и deep (харнесс из брифа, чей текст велит pytest, git fetch,
    установку и Docker), HTTP и stdout, с контейнерной задачей и без. Блок
    стоит ДО правил и сильнее их и харнесса; предписаний недоступного нет.
    """
    for profile in (LITE, DEEP):
        for http in (False, True):
            for container in (False, True):
                where = f"{profile} http={http} container={container}"
                prompt = _local_matrix_prompt(profile, http, container)
                assert "ВОЗМОЖНОСТИ ЛОКАЛЬНОГО РЕВЬЮЕРА" in prompt, where
                assert "СИЛЬНЕЕ" in prompt and "харнесс" in prompt, where
                assert prompt.index("ВОЗМОЖНОСТИ ЛОКАЛЬНОГО") < prompt.index("RULES"), (
                    f"блок раньше общих правил: {where}"
                )
                for fact in ("исходников", "git", "тесты", "Docker"):
                    assert fact in prompt.split("RULES")[0], f"{fact}: {where}"
                for order in _LOCAL_FORBIDDEN_ORDERS:
                    assert order not in prompt, f"{order!r} остался в {where}"
                assert "prepass" in prompt and "ci_run_report" in prompt, where
                assert "доказательств" in prompt, where
                # Настоящий отказ среды (#1238) остаётся названным путём.
                assert 'incomplete_reason="environment"' in prompt, where
                assert "бриф" in prompt.split("RULES")[0], where
    for http in (False, True):
        deep = _local_matrix_prompt(DEEP, http, False)
        assert "кроме того, что запрещено блоком ВОЗМОЖНОСТЕЙ" in deep, (
            f"харнесс читается ПОД блоком, а не вместо него (http={http})"
        )
    for profile in (LITE, DEEP):
        for http in (False, True):
            _assert_capability_block_outranks_demands(
                _realistic_local_prompt(profile, http), f"{profile} http={http}"
            )
    lite = _local_matrix_prompt(LITE, False, False)
    assert "доказательства тестов" in lite, "шаг брифа в lite называет доказательства"


@pytest.mark.parametrize("transport", ("direct", "runner"))
async def test_a_local_review_reaches_the_reviewer_with_the_capability_block(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch,
    tmp_path,
    transport: str,
):
    """AC-1 (#1598), сквозной: блок доезжает до процесса обоими транспортами."""
    prompt, stdin_text, _ = await _local_prompt(
        client, db, monkeypatch, tmp_path, f"caps-{transport}", _INLINE_DIFF, transport
    )
    for text in (prompt, stdin_text):
        assert "ВОЗМОЖНОСТИ ЛОКАЛЬНОГО РЕВЬЮЕРА" in text
        assert "uv run pytest" not in text


async def test_a_local_review_prompt_names_ci_evidence_by_real_fields():
    """AC-2 (#1598): состояния доказательств названы по ПОЛЯМ брифа.

    Имена из промта сверяются с настоящими моделями, чтобы промт не ссылался
    на поле, которого в брифе нет. ``current`` не выдан за pass, мутации и
    слова автора — непроверенные заявления, unknown — это lost_dimension.
    """
    from hub.models import (
        ACTestResultView,
        CIRunReportState,
        PrepassState,
        ReviewBrief,
    )

    for name in ("passed", "failed", "skipped", "state", "reason"):
        assert name in PrepassState.model_fields, name
    assert "state" in CIRunReportState.model_fields
    assert "is_current" in ACTestResultView.model_fields
    for name in ("prepass", "ci_run_report", "ac_test_results"):
        assert name in ReviewBrief.model_fields, name

    prompt = _local_matrix_prompt(LITE, False, False)
    head = prompt.split("RULES")[0]
    for field in (
        "prepass.passed",
        "prepass.failed",
        "prepass.skipped",
        "prepass.state",
        "prepass.reason",
        "ci_run_report.state",
        "ac_test_results",
        "is_current",
    ):
        assert field in head, f"поле {field} не названо"
    assert re.search(r"ci_run_report\.state=current[^\n]*НЕ pass", head), (
        "current — это наличие отчёта на sha, а не прохождение"
    )
    assert re.search(r"prepass\.state=unknown[^\n]*lost_dimensions", head)
    assert re.search(r"prepass\.state=unknown[^\n]*НЕ environment", head), (
        "нет доказательств — потерянное измерение с названием, не отказ среды"
    )
    assert re.search(r"[Мм]утации[^\n]*СЛОВО АВТОРА", head), (
        "мутации автора — заявления, хаб их не исполнял"
    )
    assert "ac_test_results" in head and "поколени" in head, (
        "ac_test_results — свидетельство текущего поколения, своего sha в нём нет"
    )


async def test_a_local_prompt_shows_every_prepass_state_beside_the_block():
    """AC-2 (#1598): passed, failed, skipped, unknown — каждое видно ревьюеру.

    Блок брифа (``prepass_block``) едет в промт, и локальный блок не стирает
    его: оба стоят рядом, и ни одно состояние не вырождается в «чисто».
    """
    from hub.models import PrepassState
    from hub.services import review_evidence
    from hub.services.review_dispatch import _review_prompt

    cases = {
        "passed": PrepassState(state="covered", passed=["lint", "tests"]),
        "failed": PrepassState(state="failed", failed=["tests"], reason="упали: tests"),
        "skipped": PrepassState(state="covered", passed=["lint"], skipped=["audit"]),
        "unknown": PrepassState(state="unknown", reason="CI не присылал отчёт"),
    }
    seen = {}
    for label, state in cases.items():
        prompt = _review_prompt(
            1598,
            "task-1598/x",
            "qwen3.8-max",
            LITE,
            "RULES",
            "DIFF",
            review_evidence.prepass_block(state),
            inline_diff=True,
        )
        assert "ВОЗМОЖНОСТИ ЛОКАЛЬНОГО РЕВЬЮЕРА" in prompt, label
        seen[label] = prompt
    assert "ПРОШЁЛ: lint (" in seen["passed"] and "tests (" in seen["passed"]
    assert "проверки УПАЛИ: tests" in seen["failed"]
    assert "Пропущены (ничего не доказывают): audit" in seen["skipped"]
    assert "данных нет — CI не присылал отчёт" in seen["unknown"]


def test_the_cloud_review_prompt_is_unchanged() -> None:
    """AC-3 (#1598): облачный промт байт в байт совпадает с эталоном до задачи.

    Эталоны сняты с develop до правки (tests/fixtures/cloud_review_prompt):
    lite и deep, MCP и HTTP, с контейнерной задачей и без. Регрессионная
    защита: зелёная и до, и после.
    """
    from hub.services.review_dispatch import _delivery_block, _review_prompt

    for profile in (LITE, DEEP):
        for http in (False, True):
            for container in (False, True):
                delivery = (
                    _delivery_block(1598, "CODE123", "https://hub.example")
                    if http
                    else ""
                )
                got = _review_prompt(
                    1598,
                    "task-1598/x",
                    "grok-4.6",
                    profile,
                    "RULES",
                    "DIFF",
                    "PREPASS",
                    delivery_block=delivery,
                    only_tests_block="ONLY_TESTS\n",
                    needs_container=container,
                )
                name = (
                    f"{profile}_{'http' if http else 'mcp'}_"
                    f"{'container' if container else 'plain'}"
                )
                expected = (_CLOUD_PROMPT_FIXTURES / f"{name}.txt").read_text(
                    encoding="utf-8"
                )
                assert got == expected, name
                assert "ВОЗМОЖНОСТИ ЛОКАЛЬНОГО" not in got, (
                    f"блок локального пути не течёт в облако: {name}"
                )


async def test_a_cloud_order_from_prepare_review_order_has_no_local_block(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1598), сквозной: заказ облака собирается без блока возможностей."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-c8"}, "run": {"id": "r-c8"}})
    _wire(monkeypatch, recorder)
    await _submitted(
        client, db, "cloud-no-caps", policy={"review": "dispatch"}, diff=_INLINE_DIFF
    )
    assert len(recorder.calls) == 1
    prompt = recorder.calls[0]["prompt_text"]
    assert "ВОЗМОЖНОСТИ ЛОКАЛЬНОГО" not in prompt
    assert "uv run pytest" in prompt, "у облака с клоном блок попыток #1238 на месте"


def test_a_local_only_tests_candidate_may_stay_unresolved():
    """Ревью P2 (#1598): блок only_tests не требует двоичного исхода локально.

    Вызов через реестр или getattr вне диффа статика не видит, а исходников у
    ревьюера нет: исход — unresolved и lost_dimensions, а недостижимость по
    одному статическому кандидату подтверждать нельзя. Облачный текст прежний
    (его защищает test_the_cloud_review_prompt_is_unchanged).
    """
    for profile in (LITE, DEEP):
        for http in (False, True):
            prompt = _realistic_local_prompt(profile, http)
            tail = prompt[prompt.index("ПРОВЕРЬ ДОСТИЖИМОСТЬ") :]
            assert "ДЛЯ ЛОКАЛЬНОГО РЕВЬЮЕРА" in tail, (profile, http)
            assert "unresolved" in tail and "lost_dimensions" in tail
            assert "НЕДОСТИЖИМОСТЬ по одному статическому кандидату НЕ подтверждай" in (
                tail.replace("\n", " ")
            )


# ================================================== снимок исходников (#1599)


async def _snapshot_order(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch,
    tmp_path,
    slug: str,
    archive,
    *,
    enabled: bool = True,
    transport: str = "runner",
    first: bool = False,
    blocker: str = "",
):
    """Локальный заказ со снимком: ``(промт, kwargs запуска, вызовы архива, task_id)``.

    Запуск ревьюера подменён: тест судит то, ЧТО хаб передал прогону (промт и
    снимок), а не само чтение — оно проверено в test_local_reviewer.
    """
    from hub.integrations.protocols import SnapshotArchive  # noqa: F401
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-x"}, "run": {"id": "r-x"}})
    _wire(monkeypatch, recorder)
    if first:
        # Принципал заводится один раз на базу; токен держится до конца теста.
        await _local_principal(db, monkeypatch)
    monkeypatch.setattr(config, "LOCAL_REVIEW_SNAPSHOT", enabled, raising=False)
    monkeypatch.setattr(config, "LOCAL_REVIEW_SNAPSHOT_MAX_BYTES", 12345, raising=False)
    monkeypatch.setattr(config, "LOCAL_REVIEW_TRANSPORT", transport)
    monkeypatch.setattr(local_reviewer.review_snapshot, "blocker", lambda _t: blocker)
    spool = tmp_path / "spool"
    spool.mkdir(parents=True)
    (spool / "heartbeat").write_text("{}")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SPOOL_DIR", str(spool))
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", "")

    taken: list[tuple] = []

    async def _take(self, repo, sha, max_bytes, ref=""):
        taken.append((repo, sha, max_bytes, ref))
        return archive(sha)

    monkeypatch.setattr(NoopGitOps, "snapshot_archive", _take, raising=False)
    seen: list[tuple[str, dict]] = []

    async def _run(prompt, **kwargs):
        seen.append((prompt, kwargs))
        return local_reviewer.LocalRun(
            rc=1, output="", dropped=0, timed_out=False, duration_ms=1
        )

    monkeypatch.setattr(local_reviewer, "run_review", _run)
    task_id = await _submitted(
        client,
        db,
        slug,
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()
    await db.commit()
    assert len(seen) == 1, "локальный прогон один"
    return seen[0][0], seen[0][1], taken, task_id


async def test_the_local_prompt_names_the_snapshot_state_and_the_path(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Состояние снимка в промте — то, что хаб передал прогону; три состояния."""
    from hub.integrations.protocols import SnapshotArchive
    from hub.services.review_dispatch import LOCAL_CAPABILITIES_BLOCK

    # ---- ok: путь-токен, имя состояния, поправка блока возможностей, архив на sha.
    ok = lambda sha: SnapshotArchive(  # noqa: E731
        sha=sha, data=b"TAR", note="исключено 2 ссылок"
    )
    prompt, kwargs, taken, task_id = await _snapshot_order(
        client, db, monkeypatch, tmp_path / "ok", "snap-ok", ok, first=True
    )
    row = dict(
        (await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,)))[0]
    )
    sha = row["submission_sha"]
    assert sha and taken[0][1] == sha, "архив снимается на ЗАКРЕПЛЁННОМ sha сдачи"
    assert taken[0][2] == 12345, "потолок берётся из настройки хаба"
    assert kwargs["snapshot"].state == "ok" and kwargs["snapshot"].data == b"TAR"
    assert "СНИМОК ИСХОДНИКОВ (только чтение)" in prompt
    marker = kwargs["snapshot"].placeholder
    assert marker.startswith("@@SNAPSHOT_DIR:") and marker in prompt, (
        "путь подставляет транспорт по маркеру ЗАКАЗА, а не по общему литералу"
    )
    assert sha[:12] in prompt and "исключено 2 ссылок" in prompt
    assert "рабочего клона репозитория" in prompt, (
        "блок возможностей поправлен: «исходников нет» при снимке было бы ложью"
    )
    assert "исходников репозитория (есть дифф в этом промте и бриф)" not in prompt
    assert "git" in prompt and "Docker" in prompt, "запрет git/тестов/Docker сохранён"
    assert "(рабочего клона нет; код вокруг диффа" in prompt
    assert "код вокруг него — в снимке исходников" in prompt, (
        "фраза предмета ревью «исходников на машине не ищи» при снимке неверна"
    )
    assert "Снимок исходников на" in "\n".join(
        str(u["content"]) for u in await _updates(db, task_id)
    ), "карточка называет снимок"

    # ---- absent: режим этапа 1 без изменений, причина названа.
    absent = lambda sha: SnapshotArchive(  # noqa: E731
        sha=sha, data=None, reason="архив больше потолка 12345 байт"
    )
    prompt, kwargs, _, task_id = await _snapshot_order(
        client, db, monkeypatch, tmp_path / "absent", "snap-absent", absent
    )
    assert "СНИМОК ИСХОДНИКОВ: не собран — архив больше потолка 12345 байт" in prompt
    assert LOCAL_CAPABILITIES_BLOCK in prompt, "блок возможностей этапа 1 не тронут"
    assert "@@SNAPSHOT_DIR" not in prompt
    assert kwargs["snapshot"].state == "absent"

    # ---- empty: отдельное состояние, не «не смонтирован».
    empty = lambda sha: SnapshotArchive(  # noqa: E731
        sha=sha, data=None, empty=True, reason="дерево пусто"
    )
    prompt, kwargs, _, _ = await _snapshot_order(
        client, db, monkeypatch, tmp_path / "empty", "snap-empty", empty
    )
    assert "пусто — читать в нём нечего" in prompt and "не сбой монтирования" in prompt
    assert "@@SNAPSHOT_DIR" not in prompt

    # ---- транспорт не может дать снимок (direct без защищённого распаковщика):
    #      архив не снимается, причина названа, режим этапа 1.
    prompt, kwargs, taken, _ = await _snapshot_order(
        client,
        db,
        monkeypatch,
        tmp_path / "blocked",
        "snap-blocked",
        ok,
        blocker="распаковщик не защищён: тест",
    )
    assert taken == [], "архив снят, хотя транспорт принять его не может"
    assert "не собран — распаковщик не защищён: тест" in prompt
    assert kwargs["snapshot"].state == "absent"

    # ---- настройка выключена: заказ ровно прежний, архив не снимался.
    prompt, kwargs, taken, _ = await _snapshot_order(
        client, db, monkeypatch, tmp_path / "off", "snap-off", ok, enabled=False
    )
    assert taken == [], "архив снят при выключенной настройке"
    assert "snapshot" not in kwargs, "прогон без снимка зовётся как до задачи"
    assert "СНИМОК ИСХОДНИКОВ" not in prompt and LOCAL_CAPABILITIES_BLOCK in prompt
    assert "исходников на машине не ищи" in prompt, "без снимка фраза прежняя"


async def _updates(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    rows = await db.execute_fetchall(
        "SELECT * FROM task_updates WHERE task_id=? ORDER BY id", (task_id,)
    )
    return [dict(r) for r in rows]


def test_the_snapshot_edits_of_the_capability_block_really_apply() -> None:
    """Обе замены блока возможностей находят свой текст: иначе поправка молчит."""
    from hub.integrations.protocols import SnapshotArchive
    from hub.services import review_dispatch as rd

    assert rd._CAP_NO_SOURCES in rd.LOCAL_CAPABILITIES_BLOCK
    assert rd._CAP_NO_OUTSIDE_READING in rd.LOCAL_CAPABILITIES_BLOCK
    changed = rd.local_capabilities_block(SnapshotArchive(sha="a" * 40, data=b"x"))
    assert changed != rd.LOCAL_CAPABILITIES_BLOCK
    assert rd._CAP_NO_SOURCES not in changed
    assert rd._CAP_NO_OUTSIDE_READING not in changed
    for state in (
        SnapshotArchive(sha="a" * 40, data=None, reason="x"),
        SnapshotArchive(sha="a" * 40, data=None, empty=True),
        None,
    ):
        assert rd.local_capabilities_block(state) == rd.LOCAL_CAPABILITIES_BLOCK


# ---- #1647: автоматика не трогает задачу-состояние ----


async def _state_with_git_residue(
    db: aiosqlite.Connection,
    slug: str,
    *,
    policy: dict,
    forge: str = "github",
    repo_name: str = "mrPDA/spike-repo",
) -> int:
    """State-задача в review С ОСТАТКАМИ git (ветка, sha) — как их оставил бы сбой.

    Остатки поставлены намеренно: у честной state-задачи ветки нет, и ранний
    возврат «нет ветки» отказал бы автоматике случайно. Здесь её может
    остановить только предикат result_kind. На коде до задачи result_kind
    молча отбрасывается, и задача читается как commit-задача, которой всё это
    положено, — тест на нём красный.
    """
    pid = await repo.create_project(
        db, slug=slug, name=slug.title(), repo_name=repo_name, workspace_path="/tmp/ws"
    )
    if forge != "github":
        await repo.update_project(db, pid, forge=forge)
    await repo.update_project(db, pid, gate_policy=json.dumps(policy))
    epic = await _node(db, title="epic", task_type="epic", parent_id=None)
    await repo.update_task(db, epic, project_id=pid)
    feature = await _node(db, title="feature", task_type="feature", parent_id=epic)
    task_id = await _node(db, title="state", task_type="task", parent_id=feature)
    areas = ["docs/notes.md"]
    await repo.update_task_structured(
        db,
        task_id,
        TaskRefine(affected_areas=areas, result_kind="state", rollback="откат"),
    )
    await repo.add_task_update(db, task_id, "dev", "status", "Plan: work")
    await db.execute(
        "UPDATE tasks SET status='review', branch=?, submission_sha=?, "
        "submission_generation=1, submission_model='claude-fable-5', "
        "assigned_agent='dev' WHERE id=?",
        (f"task-{task_id}/residue", _TIP, task_id),
    )
    await repo.record_submission(
        db, task_id=task_id, generation=1, sha=_TIP, base_branch="develop"
    )
    await db.commit()
    plugins.git_ops = _PinnedGitOps(_TIP, areas, None, {})
    return task_id


async def test_no_automation_touches_state_tasks(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-7: ни заказа ревью, ни старта стюарда, ни авто-APPROVED у state.

    Двери: облачный и локальный заказ ревью (в т.ч. deep-добор), заказы
    стюарда (судья, советник), свип due-заказов, ручной отчёт machine-review
    (auto_verdict не зовётся, добор не покупается), автовердикт и применение
    стюарда (запись вердикта не от человека отказана).
    """
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from hub.models import ReviewVerdict, TaskReviewVerdict
    from hub.services import auto_verdict, steward_advisor, steward_applied
    from hub.services import steward_dispatch
    from hub.services.review_dispatch import (
        DEEP,
        maybe_dispatch_review,
        wait_for_local_runs,
    )

    # --- облако: заказа нет, оплаченных прогонов нет ---
    recorder = _DispatchRecorder({"agent": {"id": "bc-state"}, "run": {"id": "r-st"}})
    _wire(monkeypatch, recorder)
    cloud_id = await _state_with_git_residue(
        db, "state-cloud", policy={"verdict": "auto", "steward_shadow": True}
    )
    assert not await maybe_dispatch_review(db, cloud_id)
    assert not await maybe_dispatch_review(db, cloud_id, force_profile=DEEP)
    assert recorder.calls == [], "облачный ревьюер заказан для state-задачи"
    assert not await db.execute_fetchall(
        "SELECT 1 FROM review_dispatches WHERE task_id=?", (cloud_id,)
    )

    # --- ручной отчёт machine-review: записан, автоматика молчит ---
    real_auto_verdict = auto_verdict.maybe_auto_verdict
    spy = AsyncMock(return_value=True)
    monkeypatch.setattr(auto_verdict, "maybe_auto_verdict", spy)
    await _machine_report(client, cloud_id, incomplete=True)
    assert len(await repo.machine_reviews_of_generation(db, cloud_id, 1)) == 1
    spy.assert_not_awaited()
    assert recorder.calls == [], "добор лестницы купил прогон для state"

    # --- локальный путь: форж без облака ---
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    local_id = await _state_with_git_residue(
        db,
        "state-local",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    assert not await maybe_dispatch_review(db, local_id)
    await wait_for_local_runs()
    assert await _local_dispatches(db, local_id) == []
    assert not await repo.machine_reviews_of_generation(db, local_id, 1)

    # --- стюард: заказы и свип ---
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "STEWARD_ADVISOR_MODELS", ("gpt-5.3-codex",))
    assert await steward_dispatch.order_run(db, cloud_id, 1) is None
    assert (
        await steward_dispatch.order_run(db, cloud_id, 1, steward_dispatch.KIND_ADVISOR)
        is None
    )
    assert not await steward_advisor._order_one(db, cloud_id, 1)
    assert await steward_dispatch.order_due_runs(db) == 0
    assert not await db.execute_fetchall(
        "SELECT 1 FROM steward_runs WHERE task_id IN (?, ?) AND status != 'refused'",
        (cloud_id, local_id),
    )

    # --- запись вердикта не от человека: автопилот и применение стюарда ---
    from hub.services.lifecycle import record_review_verdict

    for writer in (
        lambda: steward_applied._record(db, cloud_id, ReviewVerdict.approved, 1),
        lambda: record_review_verdict(
            db,
            cloud_id,
            TaskReviewVerdict(verdict=ReviewVerdict.approved, agent="policy"),
        ),
    ):
        with pytest.raises(HTTPException):
            await writer()
    stored = dict(await repo.get_task(db, cloud_id))
    assert stored["status"] == "review" and not stored["review_verdict"]
    assert not await real_auto_verdict(db, cloud_id)
    assert dict(await repo.get_task(db, cloud_id))["status"] == "review"


async def test_the_local_door_and_the_ladder_top_up_refuse_a_state_task(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1647: dispatch_local_review и maybe_top_up_incomplete — свои двери."""
    from hub.services.review_dispatch import (
        dispatch_local_review,
        maybe_top_up_incomplete,
        wait_for_local_runs,
    )

    recorder = _DispatchRecorder({"agent": {"id": "bc-st2"}, "run": {"id": "r-st2"}})
    _wire(monkeypatch, recorder)
    task_id = await _state_with_git_residue(
        db, "state-ladder", policy={"verdict": "auto"}
    )
    await _machine_report(client, task_id, incomplete=True)
    feed_before = len(await repo.get_task_updates(db, task_id))
    assert not await maybe_top_up_incomplete(db, task_id)
    assert len(await repo.get_task_updates(db, task_id)) == feed_before, (
        "добор лестницы не пишет по state-задаче даже отказов"
    )
    assert recorder.calls == []

    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    local_id = await _state_with_git_residue(
        db,
        "state-local-door",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    task = dict(await repo.get_task(db, local_id))
    assert not await dispatch_local_review(db, task, "gitverse", task["branch"], 1)
    await wait_for_local_runs()
    assert await _local_dispatches(db, local_id) == []


# ---------------------------------------------------------------------------
# #1653: запасной локальный ревьюер включается только согласием проекта
# (gate_policy.local_review_fallback, умолчание off)
# ---------------------------------------------------------------------------

_POLICY_REASON = "local_fallback_off_by_policy"
_POLICY_SENTENCE = (
    "Запасной локальный ревьюер выключен политикой проекта "
    "(local_review_fallback=off). Вердикт остаётся человеку."
)


def _fallback_policy(fallback: str | None, **extra) -> dict:
    policy: dict = {"review": "dispatch", **extra}
    if fallback is not None:
        policy["local_review_fallback"] = fallback
    return policy


async def _set_fallback(db: aiosqlite.Connection, task_id: int, value) -> None:
    project = await repo.resolve_project_for_task(db, task_id)
    policy = json.loads(dict(project)["gate_policy"] or "{}")
    if value is None:
        policy.pop("local_review_fallback", None)
    else:
        policy["local_review_fallback"] = value
    await repo.update_project(
        db, int(dict(project)["id"]), gate_policy=json.dumps(policy)
    )
    await db.commit()


def _count_local_launches(monkeypatch) -> list[str]:
    launched: list[str] = []

    async def _never(prompt):
        launched.append(prompt)
        raise AssertionError("локального прогона тут быть не должно")

    monkeypatch.setattr(local_reviewer, "run_review", _never)
    return launched


async def _policy_events(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    return [
        json.loads(r[0])
        for r in await db.execute_fetchall(
            "SELECT payload FROM events WHERE kind=? AND task_id=? ORDER BY id",
            (_POLICY_REASON, task_id),
        )
    ]


async def _policy_alerts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
        and "local_review_fallback=off" in dict(u)["content"]
    ]


async def _debts(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    return [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM review_dispatches WHERE task_id=? AND status='second_door'",
            (task_id,),
        )
    ]


async def _refuse_sync_submit(client, db, monkeypatch, tmp_path, slug, fallback):
    """Сдача при синхронном отказе облака и готовом локальном пути."""
    recorder = _DispatchRecorder(None, refusal=_LIMIT_REFUSAL)
    _wire(monkeypatch, recorder)
    pid = await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    task_id = await _submitted(
        client, db, slug, policy=_fallback_policy(fallback), diff=_DIFF_506
    )
    await db.commit()
    return recorder, task_id, pid


@pytest.mark.parametrize("fallback", [None, "off"], ids=["no-key", "explicit-off"])
@pytest.mark.parametrize(
    "entry",
    ["sync", "sweep", "ladder", "ask-again", "second-axis", "cloud-after-local"],
)
async def test_second_door_stays_closed_when_policy_is_off(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch,
    tmp_path,
    entry,
    fallback,
):
    """AC-1 (#1653): off — облако отказало, локального заказа нет ни на одном входе."""
    from hub.services.review_dispatch import (
        DEEP,
        maybe_dispatch_review,
        wait_for_local_runs,
    )

    from hub.services import review_dispatch as rd

    launched = _count_local_launches(monkeypatch)
    local_orders: list[int] = []
    real_order = rd.prepare_review_order

    async def _count_local_orders(*args, **kwargs):
        if kwargs.get("principal_id") is not None:
            local_orders.append(1)  # облачный заказ принципала не несёт
        return await real_order(*args, **kwargs)

    monkeypatch.setattr(rd, "prepare_review_order", _count_local_orders)
    slug = f"fb-off-{entry}-{fallback}"
    if entry == "sweep":
        recorder = _DispatchRecorder({"agent": {"id": "bc-off"}, "run": {"id": "r-1"}})
        _wire(monkeypatch, recorder)
        await _local_principal(db, monkeypatch)
        _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
        task_id = await _submitted(
            client, db, slug, policy=_fallback_policy(fallback), diff=_DIFF_506
        )
        await db.execute(
            "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
        )
        await db.commit()

        async def _errored(agent_id, run_id):
            return {"id": run_id, "status": "ERROR"}

        monkeypatch.setattr(cursor_cloud, "get_run", _errored)
        await sweep_review_dispatches(db)
        expected_status = "ERROR"
    else:
        recorder, task_id, _pid = await _refuse_sync_submit(
            client, db, monkeypatch, tmp_path, slug, fallback
        )
        expected_status = "usage_limit_exceeded"
        if entry != "sync":
            calls_before = len(recorder.calls)
            stub = (await _all_dispatches(db, task_id))[0]
            kwargs = {
                "ladder": {"force_profile": DEEP},
                "ask-again": {"replaces_dispatch_id": int(stub["id"])},
                "second-axis": {"force_model": "grok-4.6"},
                "cloud-after-local": {"cloud_after_local": True},
            }[entry]
            await maybe_dispatch_review(db, task_id, **kwargs)
            assert len(recorder.calls) > calls_before, (
                "предпосылка: вход дошёл до облака и получил отказ"
            )
    await wait_for_local_runs()
    await db.commit()

    assert launched == [], "прогон не запущен"
    assert local_orders == [], "локальный заказ даже не готовился: вход закрыт рано"
    assert await _local_dispatches(db, task_id) == [], "локального диспетча нет"
    assert await _debts(db, task_id) == [], "статус second_door не остаётся"
    rows = await _all_dispatches(db, task_id)
    assert rows and all(expected_status in (r["run_status"] or "") for r in rows[:1]), (
        "исходный run_status облачного отказа сохранён",
        rows,
    )
    assert await repo.machine_reviews_of_generation(db, task_id, 1) == []
    events = await _policy_events(db, task_id)
    assert events and all(e.get("reason") == _POLICY_REASON for e in events), events
    alerts = await _policy_alerts(db, task_id)
    assert alerts and all(_POLICY_SENTENCE in a for a in alerts), alerts


async def test_closed_second_door_survives_a_crash_without_duplicates(
    client: AsyncClient, db: aiosqlite.Connection, db_dsn, monkeypatch, tmp_path
):
    """AC-2 (#1653): сбой после записи причины, до закрытия долга — два свипа.

    Причина и закрытие — одна транзакция: упавший проход не оставляет ни
    полузаписи, ни потерянного долга; следующий проход закрывает его, а
    сообщение о политике одно на dispatch_id.
    """
    launched = _count_local_launches(monkeypatch)
    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-crash-off"}, "run": {"id": "r-1"}}
    )
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    task_id = await _submitted(
        client, db, "fb-crash", policy=_fallback_policy(None), diff=_DIFF_506
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    real_update = repo.add_task_update
    crashed = {"n": 0}

    async def _crash_after_the_reason(db_, tid, actor, kind, content, *a, **kw):
        # Причина и закрытие строки уже записаны в этой транзакции.
        if "local_review_fallback=off" in content and not crashed["n"]:
            crashed["n"] += 1
            raise RuntimeError("сбой после записи причины, до коммита")
        return await real_update(db_, tid, actor, kind, content, *a, **kw)

    monkeypatch.setattr(repo, "add_task_update", _crash_after_the_reason)
    try:
        await sweep_review_dispatches(db)
    except RuntimeError:
        pass  # ровно то, что поллер глотает и забывает
    assert crashed["n"] == 1, "предпосылка: сбой случился после записи причины"
    assert await _policy_events(db, task_id) == [], "полузаписи нет: всё откачено"
    assert await _policy_alerts(db, task_id) == []
    debt = (await _all_dispatches(db, task_id))[0]
    assert debt["status"] == "second_door" and not debt["second_door_reason"], (
        "долг остался видимым свипу, метка откатилась"
    )
    await sweep_review_dispatches(db)
    await sweep_review_dispatches(db)
    await db.commit()

    assert launched == [] and await _local_dispatches(db, task_id) == []
    assert await _debts(db, task_id) == [], "долг закрыт"
    cloud = (await _all_dispatches(db, task_id))[0]
    assert cloud["status"] == "failed" and cloud["run_status"] == "ERROR"
    events = await _policy_events(db, task_id)
    assert [e["dispatch_id"] for e in events] == [cloud["id"]], events
    assert len(await _policy_alerts(db, task_id)) == 1, "сообщение одно на dispatch_id"
    # Дедуп — метка в строке, а не событие: повтор после чистки событий
    # (их хранят 14 дней) и параллельное закрытие второго сообщения не дают.
    from hub.services import review_dispatch as rd

    assert cloud["second_door_reason"] == _POLICY_REASON
    await db.execute("DELETE FROM events WHERE kind=?", (_POLICY_REASON,))
    await db.commit()
    other = await aiosqlite.connect(db_dsn, uri=True)
    other.row_factory = aiosqlite.Row
    await other.execute("PRAGMA busy_timeout = 5000")
    try:
        await asyncio.gather(
            rd._close_second_door_by_policy(db, task_id, 1, cloud),
            rd._close_second_door_by_policy(other, task_id, 1, cloud),
        )
    finally:
        await other.close()
    assert await _policy_events(db, task_id) == []
    assert len(await _policy_alerts(db, task_id)) == 1
    # Метка уже стоит, а статус ещё открытый (сбой посреди закрытия другого
    # писателя): метка одна решает, второго сообщения нет.
    await db.execute(
        "UPDATE review_dispatches SET status='second_door' WHERE id=?", (cloud["id"],)
    )
    await db.commit()
    await rd._close_second_door_by_policy(db, task_id, 1, cloud)
    assert len(await _policy_alerts(db, task_id)) == 1
    # Строка, ещё не закрытая меткой: два параллельных закрытия — одно сообщение.
    await db.execute(
        "UPDATE review_dispatches SET status='second_door', second_door_reason='' "
        "WHERE id=?",
        (cloud["id"],),
    )
    await db.commit()
    other = await aiosqlite.connect(db_dsn, uri=True)
    other.row_factory = aiosqlite.Row
    await other.execute("PRAGMA busy_timeout = 5000")
    try:
        await asyncio.gather(
            rd._close_second_door_by_policy(db, task_id, 1, cloud),
            rd._close_second_door_by_policy(other, task_id, 1, cloud),
        )
    finally:
        await other.close()
    assert len(await _policy_events(db, task_id)) == 1
    assert len(await _policy_alerts(db, task_id)) == 2, "по одному на закрытие строки"


async def test_policy_turned_off_mid_dispatch_inserts_nothing(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-3 (#1653): on на входе, off во время подготовки — диспетча нет."""
    from hub.services import review_dispatch as rd
    from hub.services.review_dispatch import wait_for_local_runs

    launched = _count_local_launches(monkeypatch)
    _wire(monkeypatch, _DispatchRecorder(None, refusal=_LIMIT_REFUSAL))
    local_pid = await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    real_order = rd.prepare_review_order
    flipped = {"n": 0}

    async def _flip_after_the_first_check(*args, **kwargs):
        order = await real_order(*args, **kwargs)
        if kwargs.get("principal_id") == local_pid:
            flipped["n"] += 1
            await _set_fallback(db, int(args[1]["id"]), "off")
        return order

    monkeypatch.setattr(rd, "prepare_review_order", _flip_after_the_first_check)
    task_id = await _submitted(
        client, db, "fb-mid", policy=_fallback_policy("on"), diff=_DIFF_506
    )
    await wait_for_local_runs()
    await db.commit()

    assert flipped["n"] == 1, "предпосылка: вход прошёл при on и дошёл до подготовки"
    assert launched == []
    assert await _local_dispatches(db, task_id) == [], "диспетч не вставлен"
    assert await _debts(db, task_id) == [], "долг закрыт"
    events = await _policy_events(db, task_id)
    assert len(events) == 1 and events[0]["reason"] == _POLICY_REASON, events
    assert len(await _policy_alerts(db, task_id)) == 1


@pytest.mark.parametrize(
    "entry",
    ["sync", "sweep", "ladder", "ask-again", "second-axis", "cloud-after-local"],
)
async def test_second_door_opens_when_policy_is_on(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path, entry
):
    """AC-4 (#1653): явное on — локальный прогон ровно один раз, как раньше."""
    from hub.services.review_dispatch import (
        DEEP,
        maybe_dispatch_review,
        wait_for_local_runs,
    )

    slug = f"fb-on-{entry}"
    if entry == "sweep":
        recorder = _DispatchRecorder({"agent": {"id": "bc-on"}, "run": {"id": "r-1"}})
        _wire(monkeypatch, recorder)
        reviewer_pid = await _local_principal(db, monkeypatch)
        _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
        task_id = await _submitted(
            client, db, slug, policy=_fallback_policy("on"), diff=_DIFF_506
        )
        await db.execute(
            "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
        )
        await db.commit()

        async def _errored(agent_id, run_id):
            return {"id": run_id, "status": "ERROR"}

        monkeypatch.setattr(cursor_cloud, "get_run", _errored)
        await sweep_review_dispatches(db)
    elif entry == "sync":
        recorder = _DispatchRecorder(None, refusal=_LIMIT_REFUSAL)
        _wire(monkeypatch, recorder)
        reviewer_pid = await _local_principal(db, monkeypatch)
        _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
        task_id = await _submitted(
            client, db, slug, policy=_fallback_policy("on"), diff=_DIFF_506
        )
    else:
        # Вход идёт после отказа при off, затем проект включает запасной путь.
        recorder, task_id, reviewer_pid = await _refuse_sync_submit(
            client, db, monkeypatch, tmp_path, slug, "off"
        )
        await _set_fallback(db, task_id, "on")
        stub = (await _all_dispatches(db, task_id))[0]
        kwargs = {
            "ladder": {"force_profile": DEEP},
            "ask-again": {"replaces_dispatch_id": int(stub["id"])},
            "second-axis": {"force_model": "grok-4.6"},
            "cloud-after-local": {"cloud_after_local": True},
        }[entry]
        await maybe_dispatch_review(db, task_id, **kwargs)
    await wait_for_local_runs()
    await db.commit()

    locals_ = await _local_dispatches(db, task_id)
    assert len(locals_) == 1, (entry, locals_)
    assert locals_[0]["status"] == "done"
    assert locals_[0]["reviewer_principal_id"] == reviewer_pid
    reports = await repo.machine_reviews_of_generation(db, task_id, 1)
    assert len(reports) == 1
    assert await _policy_events(db, task_id) == [] or entry not in ("sync", "sweep")
    assert await _debts(db, task_id) == []


@pytest.mark.parametrize("fallback", [None, "on"], ids=["no-key", "explicit-on"])
async def test_old_second_door_debt_follows_the_policy(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path, fallback
):
    """AC-5 (#1653): долг, записанный до обновления: без ключа закрыт, on — восстановлен."""
    from hub.services.review_dispatch import wait_for_local_runs

    launched = _count_local_launches(monkeypatch) if fallback is None else []
    recorder = _DispatchRecorder({"agent": {"id": "bc-old"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    task_id = await _submitted(
        client,
        db,
        f"fb-old-{fallback}",
        policy=_fallback_policy(fallback),
        diff=_DIFF_506,
    )
    # Долг в том виде, в каком его оставила прежняя версия хаба.
    await db.execute(
        "UPDATE review_dispatches SET status='second_door', run_status='ERROR', "
        "created_at = datetime('now', '-60 minutes') WHERE task_id=?",
        (task_id,),
    )
    await db.commit()

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    assert await _debts(db, task_id) == []
    locals_ = await _local_dispatches(db, task_id)
    if fallback is None:
        assert launched == [] and locals_ == [], "без ключа заказа нет"
        assert len(await _policy_events(db, task_id)) == 1
    else:
        assert len(locals_) == 1 and locals_[0]["status"] == "done"
        assert await _policy_events(db, task_id) == []
    assert (await _all_dispatches(db, task_id))[0]["status"] == "failed"


async def test_fallback_key_leaves_other_local_paths_alone(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-6 (#1653): при off local-first, lite, форж без облака и поздний отчёт — как раньше."""
    from hub.services.review_dispatch import wait_for_local_runs

    # (a) deep_reviewer=local: deep заказан локально первым, облако не зовётся.
    recorder = _DispatchRecorder({"agent": {"id": "bc-keep"}, "run": {"id": "r-k"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _qwen_reviewer(monkeypatch, tmp_path)
    deep = await _submitted(
        client,
        db,
        "fb-keep-deep",
        policy={**_LOCAL_FIRST, "local_review_fallback": "off"},
        diff=_DIFF_506,
    )
    await wait_for_local_runs()
    await db.commit()
    assert recorder.calls == []
    assert [(r["channel"], r["profile"]) for r in await _all_dispatches(db, deep)] == [
        ("local", "deep")
    ]

    # (b) lite идёт в облако.
    lite = await _submitted(
        client,
        db,
        "fb-keep-lite",
        policy={**_LOCAL_FIRST, "local_review_fallback": "off"},
        diff=_docs_diff("docs/notes.md"),
    )
    assert len(recorder.calls) == 1
    assert [(r["channel"], r["profile"]) for r in await _all_dispatches(db, lite)] == [
        ("cloud", "lite")
    ]

    # (c) форж без облака — прямой локальный путь.
    gv = await _submitted(
        client,
        db,
        "fb-keep-gitverse",
        policy={"review": "dispatch", "local_review_fallback": "off"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
        diff=_DIFF_506,
    )
    await wait_for_local_runs()
    await db.commit()
    assert [r["channel"] for r in await _all_dispatches(db, gv)] == ["local"]
    assert await _policy_events(db, gv) == []

    # (d) советник стюарда не читает ключ: его путь отдельный.
    advisor_src = (
        Path(__file__).resolve().parent.parent / "hub/services/steward_advisor_local.py"
    ).read_text()
    assert "local_review_fallback" not in advisor_src

    # (e) поздний собственный отчёт облака закрывает диспетч в done.
    await _late_cloud_report_closes_as_done_when_off(client, db, monkeypatch, tmp_path)


async def _late_cloud_report_closes_as_done_when_off(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-6 (#1653): поздний собственный отчёт облака закрывает диспетч в done при off."""
    from hub.models import MachineReviewSubmit
    from hub.services.machine_review_intake import record_machine_review

    launched = _count_local_launches(monkeypatch)
    recorder = _DispatchRecorder({"agent": {"id": "bc-late-off"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    cloud_pid, _, _ = await _pinned_setup(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    task_id = await _submitted(
        client, db, "fb-late-off", policy=_fallback_policy(None), diff=_DIFF_506
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    submitted: list[int] = []

    async def _usage_and_a_late_report(agent_id, run_id=None):
        if submitted:
            return None
        submitted.append(1)
        payload = {**_LOCAL_REPORT}
        payload.pop("orchestrator", None)
        await record_machine_review(
            db,
            task_id,
            MachineReviewSubmit(**payload),
            principal_id=cloud_pid,
            username="cloud-reviewer",
        )
        await db.commit()
        return None

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    monkeypatch.setattr(cursor_cloud, "get_usage", _usage_and_a_late_report)
    await sweep_review_dispatches(db)
    await sweep_review_dispatches(db)
    await db.commit()

    assert launched == [] and await _local_dispatches(db, task_id) == []
    cloud = (await _all_dispatches(db, task_id))[0]
    assert cloud["status"] == "done"
    assert await _debts(db, task_id) == []

    # Само закрытие по политике (до разбора свипом) тоже видит собственный
    # отчёт: долг с лежащим отчётом закрывается в done одной транзакцией.
    from hub.services import review_dispatch as rd

    await db.execute(
        "UPDATE review_dispatches SET status='second_door' WHERE id=?", (cloud["id"],)
    )
    await db.commit()
    debt = (await _all_dispatches(db, task_id))[0]
    await rd._close_second_door_by_policy(db, task_id, 1, debt)
    assert (await _all_dispatches(db, task_id))[0]["status"] == "done"


async def test_policy_cannot_flip_between_the_last_check_and_the_insert(
    client: AsyncClient, db: aiosqlite.Connection, db_dsn, monkeypatch, tmp_path
):
    """#1653 P1: последняя проверка политики и INSERT — под одним write-локом.

    Второе соединение пытается закоммитить off ровно между проверкой и вставкой:
    лок держит его снаружи, и вставка законно видит on.
    """
    import sqlite3

    from hub.services import review_dispatch as rd
    from hub.services.review_dispatch import wait_for_local_runs

    _wire(monkeypatch, _DispatchRecorder(None, refusal=_LIMIT_REFUSAL))
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    other = await aiosqlite.connect(db_dsn, uri=True)
    await other.execute("PRAGMA busy_timeout = 100")
    raced: list[str] = []
    real = rd._local_fallback_allowed

    async def _race(db_, task_id):
        allowed = await real(db_, task_id)
        if allowed and db_.in_transaction:
            project = await repo.resolve_project_for_task(db_, task_id)
            try:
                await other.execute(
                    "UPDATE projects SET gate_policy=? WHERE id=?",
                    (json.dumps({"review": "dispatch"}), dict(project)["id"]),
                )
                await other.commit()
                raced.append("committed")
            except sqlite3.OperationalError as exc:
                raced.append(str(exc))
        return allowed

    monkeypatch.setattr(rd, "_local_fallback_allowed", _race)
    try:
        task_id = await _submitted(
            client, db, "fb-lock", policy=_fallback_policy("on"), diff=_DIFF_506
        )
        await wait_for_local_runs()
        await db.commit()
    finally:
        await other.close()

    assert len(raced) == 1 and "locked" in raced[0], raced
    assert len(await _local_dispatches(db, task_id)) == 1


async def test_a_late_cloud_report_after_the_policy_closure_settles_done(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1653 P2-a: отчёт ревьюера после закрытия по политике — строка в done."""
    from hub.models import MachineReviewSubmit
    from hub.services.machine_review_intake import record_machine_review

    launched = _count_local_launches(monkeypatch)
    _wire(
        monkeypatch,
        _DispatchRecorder({"agent": {"id": "bc-after-close"}, "run": {"id": "r-1"}}),
    )
    cloud_pid, _, _ = await _pinned_setup(db, monkeypatch)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    task_id = await _submitted(
        client, db, "fb-after-close", policy=_fallback_policy(None), diff=_DIFF_506
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    await sweep_review_dispatches(db)
    await db.commit()
    closed = (await _all_dispatches(db, task_id))[0]
    assert (
        closed["status"] == "failed" and closed["second_door_reason"] == _POLICY_REASON
    )

    payload = {**_LOCAL_REPORT}
    payload.pop("orchestrator", None)
    await record_machine_review(
        db,
        task_id,
        MachineReviewSubmit(**payload),
        principal_id=cloud_pid,
        username="cloud-reviewer",
    )
    await db.commit()
    await sweep_review_dispatches(db)
    assert launched == [] and await _local_dispatches(db, task_id) == []
    settled = (await _all_dispatches(db, task_id))[0]
    assert settled["status"] == "done", settled


async def test_off_without_a_local_path_still_names_the_policy(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1653 P2-c: политика проверяется ДО review_reach — причина пишется и без
    настроенного локального пути."""
    _wire(monkeypatch, _DispatchRecorder(None, refusal=_LIMIT_REFUSAL))
    _no_local_path(monkeypatch)
    task_id = await _submitted(
        client, db, "fb-off-nopath", policy=_fallback_policy(None), diff=_DIFF_506
    )
    await db.commit()

    assert await _debts(db, task_id) == []
    events = await _policy_events(db, task_id)
    assert len(events) == 1 and events[0]["reason"] == _POLICY_REASON
    assert len(await _policy_alerts(db, task_id)) == 1
    assert await _local_dispatches(db, task_id) == []


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_a_failed_second_door_insert_releases_the_write_lock(
    client: AsyncClient,
    db: aiosqlite.Connection,
    db_dsn,
    monkeypatch,
    tmp_path,
    failure,
):
    """#1653: исключение после BEGIN IMMEDIATE откатывает транзакцию (и CancelledError).

    Брошенная открытая транзакция держала бы write-лок всей базы («database is
    locked», #1428): второе соединение пишет сразу, строки диспетча нет.
    """
    from hub.services.review_dispatch import dispatch_local_review

    _wire(
        monkeypatch,
        _DispatchRecorder({"agent": {"id": "bc-lockfree"}, "run": {"id": "r-1"}}),
    )
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    task_id = await _submitted(
        client, db, f"fb-rollback-{failure.__name__}",
        policy=_fallback_policy("on"), diff=_DIFF_506,
    )  # fmt: skip
    task = dict(await repo.get_task(db, task_id))

    async def _boom(*args, **kwargs):
        raise failure()

    monkeypatch.setattr(repo, "create_review_dispatch", _boom)
    with pytest.raises(failure):
        await dispatch_local_review(
            db,
            task,
            "github",
            task["branch"],
            1,
            cloud_refusal="usage_limit_exceeded",
            second_door=True,
        )
    assert not db.in_transaction, "транзакция не осталась открытой"

    other = await aiosqlite.connect(db_dsn, uri=True)
    try:
        await other.execute("PRAGMA busy_timeout = 100")
        await other.execute("UPDATE projects SET name = name WHERE id = 1")
        await other.commit()
    finally:
        await other.close()
    assert await _local_dispatches(db, task_id) == []


@pytest.mark.parametrize(
    "b_replaces_a", [False, True], ids=["ladder-rung", "ask-again"]
)
async def test_a_report_settles_only_the_policy_closed_order_it_belongs_to(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, b_replaces_a
):
    """#1653: заказ A закрыт по политике, B активен в том же поколении.

    Без замещения (ступень лестницы) первый отчёт принадлежит A: A done, B
    active. С замещением (переспрос) отчёт принадлежит B: A не меняется.
    """
    from hub.models import MachineReviewSubmit
    from hub.services.machine_review_intake import record_machine_review

    _wire(
        monkeypatch,
        _DispatchRecorder({"agent": {"id": "bc-a"}, "run": {"id": "r-a"}}),
    )
    cloud_pid, _, _ = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(
        client, db, f"fb-owner-{b_replaces_a}", policy=_fallback_policy(None)
    )
    a = (await _all_dispatches(db, task_id))[0]
    await db.execute(
        "UPDATE review_dispatches SET status='failed', second_door_reason=? WHERE id=?",
        (_POLICY_REASON, a["id"]),
    )
    b_id = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="bc-b",
        run_id="r-b",
        model=a["model"],
        profile=a["profile"],
        reviewer_principal_id=cloud_pid,
        replaces_dispatch_id=int(a["id"]) if b_replaces_a else None,
    )
    await db.commit()

    payload = {**_LOCAL_REPORT}
    payload.pop("orchestrator", None)
    await record_machine_review(
        db,
        task_id,
        MachineReviewSubmit(**payload),
        principal_id=cloud_pid,
        username="cloud-reviewer",
    )
    await db.commit()
    rows = {r["id"]: r["status"] for r in await _all_dispatches(db, task_id)}
    assert rows[b_id] == "active", rows
    assert rows[a["id"]] == ("failed" if b_replaces_a else "done"), rows


async def test_a_policy_closed_order_without_a_principal_is_not_settled_by_any_report(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1653: NULL reviewer_principal_id не совпадает ни с каким принципалом."""
    from hub.models import MachineReviewSubmit
    from hub.services.machine_review_intake import record_machine_review

    _wire(
        monkeypatch,
        _DispatchRecorder({"agent": {"id": "bc-n"}, "run": {"id": "r-n"}}),
    )
    cloud_pid, _, _ = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(
        client, db, "fb-owner-null", policy=_fallback_policy(None)
    )
    a = (await _all_dispatches(db, task_id))[0]
    await db.execute(
        "UPDATE review_dispatches SET status='failed', second_door_reason=?, "
        "reviewer_principal_id=NULL WHERE id=?",
        (_POLICY_REASON, a["id"]),
    )
    await db.commit()
    payload = {**_LOCAL_REPORT}
    payload.pop("orchestrator", None)
    await record_machine_review(
        db,
        task_id,
        MachineReviewSubmit(**payload),
        principal_id=cloud_pid,
        username="cloud-reviewer",
    )
    await db.commit()
    assert (await _all_dispatches(db, task_id))[0]["status"] == "failed"


# --- #1681: ожидание локальных прогонов с пределом ---------------------------


def _register_fake_run(rd, dispatch_id: int, task_id: int, coro):
    """Прогон в реестре, как его кладёт _start_local_run: задача, ручка, pop."""
    task = asyncio.get_running_loop().create_task(coro)
    rd._LOCAL_RUNS[dispatch_id] = rd._LocalRunHandle(
        task=task, db_path="", dispatch_id=dispatch_id, task_id=task_id, generation=1
    )
    task.add_done_callback(lambda _t: rd._LOCAL_RUNS.pop(dispatch_id, None))
    return task


async def test_wait_for_local_runs_names_the_stuck_run():
    """AC-3 (#1681): зависший прогон назван ДО отмены, завершившийся ожидается."""
    from hub.services import review_dispatch as rd

    async def _never_finishes():
        await asyncio.Event().wait()

    async def _finishes_soon():
        await asyncio.sleep(0.05)

    before = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
    stuck = _register_fake_run(rd, 880001, 4242, _never_finishes())
    quick = _register_fake_run(rd, 880002, 4243, _finishes_soon())
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        with pytest.raises(rd.LocalRunsStuck) as caught:
            await rd.wait_for_local_runs(0.3, cancel_grace=0.5)
        elapsed = loop.time() - started
        text = str(caught.value)
        assert 0.3 <= elapsed < 0.3 + 0.5 + 1.0, "предел плюс ограниченная отмена"
        assert "task_id=4242" in text and "dispatch_id=880001" in text
        assert "task_id=4243" not in text, "завершившийся не назван зависшим"
        assert "_never_finishes" in text and "Task pending" in text, (
            "снимок снят ДО отмены: задача ещё pending"
        )
        assert quick.done() and not quick.cancelled(), "быстрый ожидается как прежде"
        assert stuck.cancelled(), "зависший отменён"
        assert not rd._LOCAL_RUNS, "реестр чист"
    finally:
        for dispatch_id in (880001, 880002):
            rd._LOCAL_RUNS.pop(dispatch_id, None)
        stuck.cancel()
        await asyncio.gather(stuck, quick, return_exceptions=True)
    leftover = {
        t for t in asyncio.all_tasks() if t is not asyncio.current_task()
    } - before
    assert not leftover, "после теста фоновых задач нет"


async def test_wait_for_local_runs_without_arguments_still_works():
    """AC-3 (#1681): вызов без аргументов дожидается естественного конца."""
    from hub.services import review_dispatch as rd

    async def _finishes_soon():
        await asyncio.sleep(0.05)

    task = _register_fake_run(rd, 880003, 4244, _finishes_soon())
    await rd.wait_for_local_runs()
    assert task.done() and not task.cancelled() and not rd._LOCAL_RUNS


async def test_wait_for_local_runs_lets_the_loop_drop_a_finished_run():
    """Причина #1681: готовый прогон в реестре не должен занимать цикл событий.

    Python 3.12 завершает gather над готовыми задачами сразу, без остановки;
    «while _LOCAL_RUNS: await gather(...)» тогда крутился без отдачи цикла, и
    pop-колбэк готового прогона никогда не доходил до очереди. Здесь снятие
    записи стоит в таймере — доходит только если ожидание отдаёт цикл.
    """
    from hub.services import review_dispatch as rd

    async def _done():
        return None

    loop = asyncio.get_running_loop()
    task = loop.create_task(_done())
    await task
    rd._LOCAL_RUNS[880004] = rd._LocalRunHandle(
        task=task, db_path="", dispatch_id=880004, task_id=4245, generation=1
    )
    loop.call_later(0.05, rd._LOCAL_RUNS.pop, 880004, None)
    try:
        await asyncio.wait_for(rd.wait_for_local_runs(), timeout=3)
        assert not rd._LOCAL_RUNS
    finally:
        rd._LOCAL_RUNS.pop(880004, None)


async def test_cancelling_the_waiter_cancels_the_runs():
    """Ревью P2 (#1681): отмена ожидания снимает прогоны и чистит реестр."""
    from hub.services import review_dispatch as rd

    async def _never_finishes():
        await asyncio.Event().wait()

    run = _register_fake_run(rd, 880005, 4246, _never_finishes())
    waiter = asyncio.get_running_loop().create_task(
        rd.wait_for_local_runs(30, cancel_grace=1)
    )
    try:
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert run.cancelled() and not rd._LOCAL_RUNS
    finally:
        rd._LOCAL_RUNS.pop(880005, None)
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)


async def test_a_run_that_ignores_cancellation_is_detached_and_named(caplog):
    """Ревью P2 (#1681): не слушающийся прогон отцеплен от реестра и назван."""
    from hub.services import review_dispatch as rd

    release = asyncio.Event()

    async def _stubborn():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    run = _register_fake_run(rd, 880006, 4247, _stubborn())
    try:
        with caplog.at_level("ERROR"):
            with pytest.raises(rd.LocalRunsStuck) as caught:
                await rd.wait_for_local_runs(0.1, cancel_grace=0.1)
        assert "не закончились: 1" in str(caught.value)
        assert not rd._LOCAL_RUNS, "брошенный прогон не держит реестр хаба"
        assert "task #4247" in caplog.text and "abandoned" in caplog.text
    finally:
        rd._LOCAL_RUNS.pop(880006, None)
        release.set()
        await asyncio.gather(run, return_exceptions=True)


async def test_the_deadline_also_names_a_finished_run_nobody_removed():
    """Ревью 912 (#1681): предел безусловен, готовая неснятая запись тоже виновна."""
    from hub.services import review_dispatch as rd

    async def _done():
        return None

    task = asyncio.get_running_loop().create_task(_done())
    await task
    rd._LOCAL_RUNS[880007] = rd._LocalRunHandle(
        task=task, db_path="", dispatch_id=880007, task_id=4248, generation=1
    )  # pop-колбэка нет: запись сама не уйдёт
    try:
        with pytest.raises(rd.LocalRunsStuck) as caught:
            await asyncio.wait_for(rd.wait_for_local_runs(0.2), timeout=5)
        assert "task_id=4248" in str(caught.value) and "state=done" in str(caught.value)
        assert not rd._LOCAL_RUNS, "неснятая запись убрана из реестра"
    finally:
        rd._LOCAL_RUNS.pop(880007, None)
