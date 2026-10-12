"""Shared helpers of the review-dispatch tests (#1670).

Moved out of tests/test_review_dispatch.py unchanged; the thematic
tests/test_review_dispatch_*.py files import them from here.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import aiosqlite
from httpx import AsyncClient

from hub import auth as hub_auth
from hub import config, services
from hub import repository as repo
from hub.integrations import cursor_cloud, local_reviewer
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import TaskRefine, TaskSubmitReview
from hub.services.review_dispatch import (
    maybe_dispatch_review,
)

_TIP = "c" * 40


# Ordinary code with no process surface: the class and the risks decide. It
# must NOT be documentation — a docs-only diff buys lite before the class or
# the risks are read (#1415), and a lite assertion on it would witness nothing.
_HARMLESS_DIFF = "+++ b/app/notes.py\n+NOTE = 'одна строка'\n"


class _PinnedGitOps(NoopGitOps):
    def __init__(
        self,
        tip: str,
        paths: list[str],
        diff: str | None = None,
        rules: dict[str, str] | None = None,
    ) -> None:
        self._tip = tip
        self._paths = paths
        # #820: the profile is decided against the diff, so the double must
        # serve one. None means "could not be read", which buys deep.
        self._diff = _HARMLESS_DIFF if diff is None else diff
        # #873: the repository's review rules, keyed by path. Absent path =
        # no such file, exactly as `git show base:path` behaves.
        self._rules = rules or {}

    async def branch_diff(self, repo, base, branch, context=0):
        if branch != self._tip and re.fullmatch(r"[0-9a-f]{40}", branch or ""):
            # #1361: a pinned commit other than the tip is ANOTHER submission,
            # and by default another author edit — every fixture here models
            # resubmissions as new code. One diff for every sha would read as
            # "only the base was merged" and carry the report over.
            return self._diff + f"+{branch}\n"
        return self._diff

    async def file_at_ref(self, repo, ref, path):
        return self._rules.get(path)

    async def fetch_base(self, repo: str, base: str):
        return True, ""

    async def head_sha(self, repo: str, base: str) -> str:
        return self._tip

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        return self._paths


async def _node(
    db: aiosqlite.Connection, *, title: str, task_type: str, parent_id: int | None
) -> int:
    return await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type=task_type,
        parent_id=parent_id,
        priority="medium",
    )


class _DispatchRecorder:
    """Подставка провайдера: помнит вызовы, отдаёт заготовленный исход.

    Возвращает ПАРУ (тело, отказ) — шов сместился на create_agent_attempt
    в #1199, потому что путь ревью обязан отличать обрыв связи от отказа
    провайдера. Подставка, оставшаяся на старом шве, молча пропускала бы
    вызовы в настоящий Cursor.
    """

    def __init__(self, result, refusal=None):
        self.result = result
        self.refusal = refusal
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result, self.refusal


def _wire(monkeypatch, recorder: _DispatchRecorder) -> None:
    monkeypatch.setattr(config, "CURSOR_API_KEY", "test-key")
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", "reviewer-token")
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", recorder)

    async def _no_usage(agent_id, run_id=None):
        # Default: the API did not answer. Sweep must not hit the network
        # just because _wire set a fake key (#1026 stamps usage on every
        # terminal close). Tests that care override this.
        return None

    monkeypatch.setattr(cursor_cloud, "get_usage", _no_usage)

    async def _no_runs(agent_id):
        # #1413: счёт агента падает на сумму по его прогонам, когда /usage без
        # runId молчит — список прогонов тоже не должен уходить в сеть.
        return None

    monkeypatch.setattr(cursor_cloud, "list_runs", _no_runs)

    async def _no_catalog():
        # Умолчание params — подбор по каталогу (#1423), а ключ выше фальшивый:
        # без подставки заказ пошёл бы за каталогом в настоящий Cursor.
        # «Не ответил» — заказ без params; каталог дают тесты, которым он нужен.
        return None

    monkeypatch.setattr(cursor_cloud, "list_models", _no_catalog)


async def _any_dispatch_row(db: aiosqlite.Connection, task_id: int) -> dict:
    rows = await db.execute_fetchall(
        "SELECT * FROM review_dispatches WHERE task_id=? ORDER BY id", (task_id,)
    )
    assert rows, "no dispatch recorded for the task"
    return dict(rows[-1])


async def _submitted(
    client: AsyncClient,
    db: aiosqlite.Connection,
    slug: str,
    *,
    verdict_auto: bool = True,
    policy: dict | None = None,
    repo_name: str = "mrPDA/spike-repo",
    areas: list[str] | None = None,
    risks: list[dict] | None = None,
    clear_risk_class: bool = False,
    diff: str | None = None,
    rules: dict[str, str] | None = None,
    forge: str = "github",
    validation_commands: list[str] | None = None,
    ci_reports: list[dict] | None = None,
    override: str = "",
) -> int:
    areas = ["docs/notes.md"] if areas is None else areas
    pid = await repo.create_project(
        db,
        slug=slug,
        name=slug.title(),
        repo_name=repo_name,
        workspace_path="/tmp/ws",
    )
    if forge != "github":
        # Форж выставляется ДО сдачи намеренно: диспатч случается внутри
        # submit_for_review, и проект, переключённый после, проверял бы не тот
        # момент (#1119).
        await repo.update_project(db, pid, forge=forge)
    if policy is not None:
        await repo.update_project(db, pid, gate_policy=json.dumps(policy))
    elif verdict_auto:
        await repo.update_project(db, pid, gate_policy=json.dumps({"verdict": "auto"}))
    epic = await _node(db, title="epic", task_type="epic", parent_id=None)
    await repo.update_task(db, epic, project_id=pid)
    feature = await _node(db, title="feature", task_type="feature", parent_id=epic)
    task_id = await _node(db, title="probe", task_type="task", parent_id=feature)
    await repo.add_task_update(db, task_id, "dev", "status", "Plan: work")
    await repo.update_task_structured(
        db,
        task_id,
        TaskRefine(
            affected_areas=areas,
            risks=risks,
            validation_commands=validation_commands,
        ),
    )
    if clear_risk_class:
        # A task whose class was never computed: the state #582 calls
        # "not computed", which must never be read as low risk. NULL is that
        # state in the column; the empty string is not a valid class.
        await db.execute("UPDATE tasks SET risk_class = NULL WHERE id = ?", (task_id,))
    for report in ci_reports or []:
        # #1405: CI раньше сдачи — обычный порядок; отчёт ложится до сдачи и
        # подхватывается ею (adopt_ci_run_report).
        await _ci_report(db, task_id, **report)
    if override:
        await repo.update_task(db, task_id, machine_review_override=override)
    await db.commit()

    plugins.git_ops = _PinnedGitOps(_TIP, areas, diff, rules)
    started = await services.pair_start_task(db, task_id, caller="dev-agent")
    assert started.status.value == "running"
    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )
    assert view.status.value == "review"
    return task_id


async def _ci_report(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    head_sha: str = _TIP,
    validation_status: str = "pass",
    checks: dict[str, str] | None = None,
) -> None:
    await repo.upsert_ci_run_report(
        db,
        task_id=task_id,
        head_sha=head_sha,
        ac_results="{}",
        validation_status=validation_status,
        validation_log="",
        reason="",
        reported_by="github-actions",
        checks=json.dumps(checks or {}),
        trust="verified",
        trust_reason="",
    )


# --- Review profiles (#807) --------------------------------------------------
#
# The profile answers "how much was this run allowed to spend", and it is
# decided by the hub before the run starts. Every kind of ignorance —
# unknown class, unreadable class, a human explicitly asking — resolves
# toward deep: cheap is the default only where the facts say it is safe.


async def _dispatch_row(db: aiosqlite.Connection, task_id: int) -> dict:
    rows = await repo.list_active_review_dispatches(db)
    mine = [dict(r) for r in rows if r["task_id"] == task_id]
    assert mine, "no dispatch recorded for the task"
    return mine[-1]


# --- Process surfaces buy the expensive profile (#820) -----------------------
#
# Measured, not assumed: the lite-vs-deep comparison of 21.08.2026 found the
# cheap profile caught 2 of 7 confirmed findings, and both misses were process
# defects — an orphaned collector and a collection run against the wrong
# branch. Neither is visible in the diff text; both live on surfaces that can
# be named.

# Real added lines from the branches where those defects were found. Written
# out rather than generated, so the regression is against what actually
# happened rather than against a shape invented to pass.
_DIFF_506 = (
    "+++ b/hub/services/test_existence.py\n"
    "+        proc = await asyncio.create_subprocess_exec(\n"
    '+            "uv",\n'
    "+            stdout=asyncio.subprocess.PIPE,\n"
    "+        )\n"
    "+        out, _ = await asyncio.wait_for("
    "proc.communicate(), timeout=_COLLECT_TIMEOUT)\n"
)


def _docs_diff(*paths: str) -> str:
    return "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n+строка\n" for p in paths
    )


# --- The ladder: buy deep when cheap said it did not finish (#879) ----------
#
# The trigger is the reviewer's own declaration of what it did not read. #893
# removed the budget guard because a run that burned 1.5M while reporting 36k
# sailed through as complete; the declaration, unlike the number, is checkable
# against the diff.


async def _machine_report(
    client: AsyncClient,
    task_id: int,
    *,
    incomplete: bool,
    confirmed: list | None = None,
) -> None:
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "agent_count": 1,
            "tokens_spent": 12000,
            "model": "grok-4.6",
            "raw_count": 1,
            "findings_confirmed": confirmed or [],
            "findings_rejected": [],
            "incomplete": incomplete,
            "unresolved": [],
            "lost_dimensions": ["hub/services/big.py"] if incomplete else [],
            "agent": "cursor-cloud-reviewer",
        },
    )
    assert resp.status_code == 200, resp.text


# --- Incremental review: pay for the fixes, not the branch (#880) ------------
#
# A report is pinned to a generation, so a resubmission makes it stale — right
# in substance, expensive in practice: the next run re-read the whole branch,
# including code the previous generation had already read and called clean.


class _AncestryGitOps(_PinnedGitOps):
    """Git that answers the three questions the delta stands on."""

    def __init__(
        self,
        *args,
        ancestor: bool | None = True,
        delta: str = "",
        own: str | None = "",
        **kw,
    ):
        super().__init__(*args, **kw)
        self._ancestor = ancestor
        self._delta = delta
        # #1249: the author's OWN commits inside the delta. None is the answer
        # a clone gives when reachability cannot be established at all.
        self._own = own

    async def is_ancestor(self, repo, ancestor, descendant):
        return self._ancestor

    async def branch_diff(self, repo, base, branch, context=0):
        # The delta call passes the previous SHA as `base`; anything else is
        # the ordinary branch diff.
        if base == _PREV_SHA:
            return self._delta
        return await super().branch_diff(repo, base, branch)

    async def delta_without_base(self, repo, base, prev, current):
        return self._own


_PREV_SHA = "b" * 40

# --- Whose change is this? (#1249) ------------------------------------------
#
# The previous submission is an ancestor of the current tip, so the plain
# prev..current diff also carries everything a merge of the base branch
# brought in. Measured on the live resubmission of #1172 (generation 2,
# 10.09.2026): 25 files in that diff, 2 written by the author, 9 of the 11
# commits in the range already reachable from origin/develop. The marker that
# bought the deep harness — workspace_path — appeared 0 times in the author's
# own commit and twice in the delta as a whole.
#
# The doubles below are that shape in miniature: one file the author wrote,
# one file that arrived with the base and carries the process-surface marker.
_AUTHOR_FILE = "hub/services/fixed.py"


async def _second_generation(
    client: AsyncClient,
    db: aiosqlite.Connection,
    slug: str,
    *,
    ancestor: bool | None = True,
    base_branch: str = "develop",
    record_previous: bool = True,
    reviewed: bool = True,
    **submitted: object,
) -> int:
    """A task on its SECOND submission, with the first one in the ledger.

    ``reviewed`` records a report on generation 1: since #1400 the delta is
    taken only from a generation somebody's review actually covered.
    ``submitted`` goes to :func:`_submitted` as is (risks, policy, ...).
    """
    task_id = await _submitted(client, db, slug, **submitted)  # type: ignore[arg-type]
    if reviewed:
        await repo.insert_machine_review(
            db, task_id=task_id, submission_generation=1, incomplete=False
        )
    if record_previous:
        # submit_for_review already wrote this row; the upsert pins the sha and
        # base the test wants to reason about.
        await repo.record_submission(
            db,
            task_id=task_id,
            generation=1,
            sha=_PREV_SHA,
            base_branch=base_branch,
        )
    else:
        # A task already in flight when the ledger appeared has no row at all.
        await db.execute("DELETE FROM submissions WHERE task_id = ?", (task_id,))
    # Bump to generation 2 the way a resubmission would, and pin a new tip.
    await db.execute(
        "UPDATE tasks SET submission_generation = 2, submission_sha = ? WHERE id = ?",
        ("c" * 40, task_id),
    )
    await db.commit()
    return task_id


async def _agent_key(db, username: str) -> tuple[int, str]:
    """A db-backed agent principal and its plaintext API key."""
    from hub.services import admin as admin_svc

    principal = await admin_svc.create_principal(db, kind="agent", username=username)
    key = await admin_svc.create_api_key(db, principal["id"], name=username)
    return principal["id"], key["plaintext_key"]


async def _pinned_setup(db, monkeypatch) -> tuple[int, str, str]:
    """Real auth + a reviewer principal the dispatcher will pin.

    Returns (reviewer_principal_id, reviewer_token, author_token). Call after
    _wire and before _submitted — the pin resolves at dispatch creation.
    """
    from hub import auth as hub_auth

    monkeypatch.setattr(hub_auth, "_is_open_mode", lambda: False)
    reviewer_pid, reviewer_token = await _agent_key(db, "cloud-reviewer")
    _, author_token = await _agent_key(db, "parallel-author")
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", reviewer_token)
    return reviewer_pid, reviewer_token, author_token


# --- Report delivered in the run's own text (#1036) --------------------------
#
# Since 22.08 Cursor stops delivering the hub's MCP into cloud runs: verified
# on grok-4.6, grok-4.5, composer-2.5 and default, and through GetDynamicTools,
# whose catalog does not list the server at all. The reviewers still work — the
# findings just die in the final text. So the text becomes a second, weaker
# delivery path, and its weakness is recorded rather than smoothed over.


def _report_block(**over) -> str:
    payload = {
        "raw_count": 2,
        "incomplete": False,
        "findings_confirmed": [
            {
                "title": "race on retry",
                "severity": "medium",
                "category": "correctness",
                "locator": "lines",
                "file": "hub/a.py",
                "start_line": 10,
            }
        ],
        "findings_rejected": [],
        "unresolved": [],
        "lost_dimensions": [],
        "harness_skill": "lite-diff-review",
        "tokens_spent": 40000,
        "model": "grok-4.6",
        "orchestrator": "cursor-cloud",
    }
    payload.update(over)
    return (
        "Прочитал дифф, ревью выполнено.\n\n"
        "```haiplane-review\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    )


async def _finished_run_with(monkeypatch, text: str) -> None:
    async def _finished(agent_id, run_id):
        return {"id": run_id, "status": "FINISHED", "result": text}

    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 900_000}}

    monkeypatch.setattr(cursor_cloud, "get_run", _finished)
    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)


async def _expire_grace(db) -> None:
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()


# ---------------------------------------------------------------------------
# #1152 — второй прогон на том же коде не покупается
# ---------------------------------------------------------------------------


async def _report_on_current(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    incomplete: bool = False,
    self_reviewed: bool = False,
) -> int:
    """Отчёт машинного ревью на текущую генерацию задачи."""
    task = dict(await repo.get_task(db, task_id))
    return await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=int(task["submission_generation"] or 0),
        harness_skill="lite-diff-review",
        model="grok-4.6",
        raw_count=3,
        findings_confirmed=json.dumps([]),
        incomplete=incomplete,
        self_reviewed=self_reviewed,
        submitted_by="cursor-cloud-reviewer",
    )


# --- Локальный ревьюер: второй способ добыть отчёт (#1180) --------------------
#
# Облачный агент Cursor принимает только GitHub (измерено 31.08.2026), поэтому
# на GitVerse независимого машинного ревью не бывает вовсе: отчёт может подать
# только тот, кто делал работу, и гейт его не засчитывает при
# REVIEW_SELF_APPROVE=forbid. Наблюдено на живой задаче #1128 — настоящий
# прогон с семью находками не был засчитан, задача простояла девять часов.
#
# Здесь проверяется ПОВЕДЕНИЕ, а не форма настроек: вместо агентского CLI
# запускается python-заглушка, и всё, что тесты утверждают, они утверждают по
# строкам в базе, по ленте задачи и по тому, что заглушка увидела о себе сама.

_LOCAL_REPORT = {
    "harness_skill": "lite-diff-review",
    "harness_version": 8,
    "raw_count": 3,
    "findings_confirmed": [],
    "findings_rejected": [],
    "incomplete": False,
    "unresolved": [],
    "lost_dimensions": [],
    "agent_count": 4,
    "tokens_spent": 1_500_000,
    "duration_ms": 61_000,
    "orchestrator": "local-stub",
    "model": "grok-4.6",
}


def _scratch(tmp_path) -> str:
    """Каталог прогонов, как его требует выкат: существует и setgid.

    Тесты обязаны описывать прод, а не удобство: на проде каталог создаёт
    оператор с группой, общей у хаба и ревьюера, и хаб отказывается работать
    без setgid — без него подкаталог унаследовал бы основную группу хаба.
    """
    import os

    path = tmp_path / "scratch"
    path.mkdir(exist_ok=True)
    os.chmod(path, 0o2770)
    return str(path)


def _fake_sudo(tmp_path) -> str:
    """Исполняемый скрипт по имени ``sudo`` — ради ФОРМЫ, а не ради изоляции.

    С 10.09.2026 хаб принимает закрытый набор форм строки песочницы (#1208), и
    системный ``env``, которым эти тесты пользовались раньше, в набор не
    входит: строка вне набора не запускается вовсе, и прогон, который тест
    хочет измерить, просто не состоялся бы. Второго unix-пользователя на
    машине разработчика не завести, поэтому здесь стоит скрипт, который
    ничего не изолирует, а лишь съедает свои три токена и запускает остальное.

    Смысл тот же, что был у ``env``: доказать, что префикс ДЕЙСТВИТЕЛЬНО
    применяется к командной строке — заглушка видит себя запущенной через
    него. Настоящая изоляция остаётся свойством выката
    (deploy/LOCAL-REVIEW.md), и её проверка попыткой названа в AC-2 ручной
    честно, а не подменена этим тестом.
    """
    path = tmp_path / "sudo"
    if not path.exists():
        path.write_text(
            '#!/bin/sh\n# sudo -n -u <пользователь> <команда...>\nshift 3\nexec "$@"\n'
        )
        path.chmod(0o755)
    return str(path)


def _sudo_sandbox(tmp_path, wrapper: str) -> str:
    """Строка песочницы формы ``sudo`` — той самой, что стоит на проде.

    Пользователь — сам вызывающий: чужого на машине разработчика нет, а страж
    проверяет его членство в группе каталога прогонов по-настоящему.
    """
    import os
    import pwd

    return f"{_fake_sudo(tmp_path)} -n -u {pwd.getpwuid(os.getuid()).pw_name} {wrapper}"


def _stub_reviewer(monkeypatch, tmp_path, script: str) -> None:
    """Локальный ревьюер = python-заглушка под настоящим префиксом."""
    import shlex
    import sys

    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, sys.executable)
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", shlex.join(["-c", script]))
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))


async def _local_principal(db, monkeypatch) -> int:
    """Принципал локального ревьюера — тот, чей токен подпишет отчёт."""
    monkeypatch.setattr(hub_auth, "_is_open_mode", lambda: False)
    pid, token = await _agent_key(db, "local-reviewer")
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", token)
    return pid


def _reporting_stub(report: dict | None = None) -> str:
    """Заглушка, оставляющая отчёт блоком в собственном выводе."""
    payload = json.dumps(report or _LOCAL_REPORT, ensure_ascii=False)
    return (
        "import sys\n"
        "sys.stdin.read()\n"
        "print('```haiplane-review')\n"
        f"print({payload!r})\n"
        "print('```')\n"
    )


def _own_host_budget(monkeypatch) -> None:
    """Свой замок слота на этот тест — ради ЦИКЛА СОБЫТИЙ, а не ради смысла.

    ``asyncio.Lock`` привязывается к циклу при ПЕРВОЙ же конкуренции за него
    (``_LoopBoundMixin._get_loop``), а ``_HOST_BUDGET`` живёт в модуле, то
    есть переживает тест. Второй тест, который дождётся очереди, получил бы
    «bound to a different event loop» вместо своего измерения — и падал бы
    только В КОМПАНИИ первого, что читается как флак, а не как правило. У
    хаба цикл один, поэтому на проде вопроса нет вовсе.

    Замок остаётся настоящим и остаётся глобалью модуля: ``run_review``
    читает его по имени на каждом вызове, так что очередь тест измеряет ту
    же самую.
    """
    monkeypatch.setattr(local_reviewer, "_HOST_BUDGET", asyncio.Lock())


# --- #1199: ответ не дошёл, но агент создан -------------------------------
#
# Измерено 06.09.2026: три «отказа» подряд, и по каждому у провайдера нашёлся
# живой оплаченный агент (08:49:06Z, 08:50:53Z, 11:28:05Z). Хаб при этом
# писал «Cloud Agents API не принял запрос» и уводил вердикт к человеку.
# Наивный повтор покупал бы второго агента каждый раз.

_LOST_ANSWER = cursor_cloud.Refusal(status=0, detail="ReadTimeout: ")
_REAL_REFUSAL = cursor_cloud.Refusal(
    status=400, code="invalid_model", detail="Model is not available"
)


class _Listing:
    """Подставка списка агентов у провайдера.

    Форма ответа — ключ `items` и `nextCursor`, как у настоящего API
    (проверено вызовом 06.09). Выдуманная форма дала бы пустой список, а
    пустой список здесь означает «агента нет» и разрешает купить второго.
    """

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    async def __call__(self, limit=50, cursor=""):
        self.calls += 1
        if self.pages is None:
            return None
        index = int(cursor or 0)
        if index >= len(self.pages):
            return {"items": [], "nextCursor": ""}
        nxt = str(index + 1) if index + 1 < len(self.pages) else ""
        return {"items": self.pages[index], "nextCursor": nxt}


# ---------------------------------------------------------------------------
# #1238 — почему отчёт неполон: отказ среды против исчерпания профиля
# ---------------------------------------------------------------------------
#
# Повод — отчёт #334 (задача #1169, профиль deep, 12 агентов, счёт провайдера
# 1 238 071 токен). Он честно записал в lost_dimensions «в среде нет
# uv/pytest — сюит не исполнялся, только чтение» и «локальный ref develop не
# резолвится», то есть потерял два измерения из-за среды, а не из-за кода. В
# карточке это выглядело так же, как любая другая неполнота, и ноль
# подтверждённых находок читался как «чисто».


async def _seed_report(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    incomplete: bool,
    reason: str | None,
) -> int:
    """Отчёт прямо в таблицу. ``reason=None`` — отчёт СТАРОГО образца.

    None здесь не «пустая причина», а «поля не было вовсе»: аргумент не
    передаётся, и колонка берёт свой DEFAULT. Именно эти строки не должны
    задним числом становиться отказом среды.
    """
    task = dict(await repo.get_task(db, task_id))
    kwargs = {} if reason is None else {"incomplete_reason": reason}
    return await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=int(task["submission_generation"] or 0),
        harness_skill="lite-diff-review",
        model="grok-4.6",
        raw_count=1,
        findings_confirmed=json.dumps([]),
        incomplete=incomplete,
        submitted_by="cursor-cloud-reviewer",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# #1235 — круг ревью: заходы, где находки ЗАКРЫВАЛИСЬ
# ---------------------------------------------------------------------------


def _confirmed(title: str, category: str, line: int) -> dict:
    """Подтверждённая находка в форме, в которой она лежит в отчёте."""
    return {
        "title": title,
        "severity": "high",
        "category": category,
        "file": "hub/services/probe.py",
        "line": line,
        "start_line": line,
        "end_line": line,
        "locator": "lines",
        "detail": "",
    }


async def _generation_with_findings(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    *,
    confirmed: list[dict] | None = None,
    unresolved: list[dict] | None = None,
) -> int:
    """Поставить задачу на поколение N и положить на него отчёт."""
    await db.execute(
        "UPDATE tasks SET submission_generation=? WHERE id=?", (generation, task_id)
    )
    review_id = await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill="deep-review",
        model="grok-4.6",
        raw_count=len(confirmed or []) + len(unresolved or []),
        findings_confirmed=json.dumps(confirmed or [], ensure_ascii=False),
        unresolved=json.dumps(unresolved or [], ensure_ascii=False),
        submitted_by="cursor-cloud-reviewer",
    )
    await db.commit()
    return review_id


async def _author_closed_them(
    db: aiosqlite.Connection,
    task_id: int,
    review_id: int,
    generation: int,
    *,
    confirmed: list[dict],
    unresolved: list[dict],
    outcome_confirmed: str = "fixed",
    outcome_unresolved: str = "real_fixed",
) -> None:
    """Автор отчитался, что находки этого поколения закрыты правкой.

    Ровно тот путь, которым исходы попадают в базу на живой задаче: они
    пишутся на СДАЧЕ СЛЕДУЮЩЕГО поколения против отчёта, который вернул
    работу (lifecycle._step_finding_outcomes), и потому лежат с номером
    поколения ОТЧЁТА, а не новой сдачи.
    """
    from hub.services.finding_identity import finding_uids, unresolved_uids

    for index, (uid, finding) in enumerate(zip(finding_uids(confirmed), confirmed)):
        await repo.upsert_finding_outcome(
            db,
            review_id=review_id,
            task_id=task_id,
            submission_generation=generation,
            finding_uid=uid,
            finding_index=index,
            finding_title=finding["title"],
            outcome=outcome_confirmed,
            note="разобрано опытом",
            linked_task_id=None,
            reported_by="dev-agent",
            finding_kind="confirmed",
        )
    for index, (uid, finding) in enumerate(
        zip(unresolved_uids(unresolved), unresolved)
    ):
        await repo.upsert_finding_outcome(
            db,
            review_id=review_id,
            task_id=task_id,
            submission_generation=generation,
            finding_uid=uid,
            finding_index=index,
            finding_title=finding["title"],
            outcome=outcome_unresolved,
            note="воспроизведено зондом",
            linked_task_id=None,
            reported_by="dev-agent",
            finding_kind="unresolved",
        )
    await db.commit()


async def _walk_the_circle(
    db: aiosqlite.Connection,
    task_id: int,
    layers: list[tuple[list[dict], list[dict]]],
) -> None:
    """Пройти заданные слои находок: каждый закрыт, следующий принёс новые."""
    previous: tuple[int, int, list[dict], list[dict]] | None = None
    for generation, (confirmed, unresolved) in enumerate(layers, start=1):
        review_id = await _generation_with_findings(
            db, task_id, generation, confirmed=confirmed, unresolved=unresolved
        )
        if previous is not None:
            prev_review, prev_generation, prev_conf, prev_unres = previous
            await _author_closed_them(
                db,
                task_id,
                prev_review,
                prev_generation,
                confirmed=prev_conf,
                unresolved=prev_unres,
            )
        previous = (review_id, generation, confirmed, unresolved)


# --- #1252: вторая дверь ревью после НАБЛЮДЁННОГО отказа облака -------------
#
# Замерено 10.09.2026 на проде: HTTP 400, код usage_limit_exceeded, тело
# называет причину («Background Agent requires at least $2 remaining until
# your hard limit»). Наблюдалось на задачах #1250 в 09:46 UTC и #1172 в 10:03.
_LIMIT_REFUSAL = cursor_cloud.Refusal(
    status=400,
    code="usage_limit_exceeded",
    detail="Background Agent requires at least $2 remaining until your hard limit",
)


def _no_local_path(monkeypatch) -> None:
    """Локальный путь ВЫКЛЮЧЕН явно, а не по счастливому умолчанию."""
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", "")
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "")


async def _local_dispatches(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    return [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM review_dispatches WHERE task_id=? AND channel='local'",
            (task_id,),
        )
    ]


async def _a_report_from(db, task_id: int, principal_id: int, username: str) -> int:
    """Отчёт этого принципала по текущему поколению. Возвращает его id."""
    from hub.models import MachineReviewSubmit
    from hub.services.machine_review_intake import record_machine_review

    payload = {**_LOCAL_REPORT}
    payload.pop("orchestrator", None)
    await record_machine_review(
        db,
        task_id,
        MachineReviewSubmit(**payload),
        principal_id=principal_id,
        username=username,
    )
    await db.commit()
    rows = await repo.machine_reviews_of_generation(db, task_id, 1)
    return int(dict(rows[-1])["id"])


# --- #1208: документ выката и охват стражей песочницы -------------------------
#
# Найдено ВЫКАТОМ 08.09.2026, а не чтением: развёртывание локального ревьюера
# по собственному документу #1180 остановилось четырежды. Имена настроек в
# документе стояли без префикса HAIPLANE_, который подставляет config.env_get,
# — и это не ломало запуск, а МОЛЧА выключало локальный путь. Рекомендованная
# строка systemd-run --uid= от непривилегированного хаба не запускалась вовсе.
# А стражи знали ровно ту форму записи, которой на рабочей конфигурации нет.

_HUB_ROOT = Path(__file__).resolve().parents[1]
_DEPLOY_DOC = _HUB_ROOT / "deploy/LOCAL-REVIEW.md"
_ENV_EXAMPLE = _HUB_ROOT / "deploy/local-hub.env.example"


# --- #1242: несостоявшееся ревью переспрашивается во времени ----------------
#
# Наблюдено 22.09.2026 стюардом: сдачи #1162 (сдача 3) и #1281 (сдача 2)
# получили «провайдер отказал: HTTP 429» и остались без отчёта и без повтора.
# Вторая дверь (#1252) закрывает случай, где настроен локальный путь; здесь
# он НЕ настроен — и именно здесь переспрашивать было некому.
_RATE_LIMIT = cursor_cloud.Refusal(
    status=429, code="rate_limited", detail="Too Many Requests"
)


class _Sequence:
    """Подставка провайдера с заготовленной ОЧЕРЕДЬЮ исходов по вызовам."""

    def __init__(self, outcomes: list[tuple]):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.outcomes) > 1:
            return self.outcomes.pop(0)
        return self.outcomes[0]


async def _age_dispatches(db: aiosqlite.Connection) -> None:
    """Все заказы старше паузы переспроса и окна grace."""
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()


async def _alerts_of(db: aiosqlite.Connection, task_id: int) -> list[str]:
    return [
        dict(r)["content"]
        for r in await db.execute_fetchall(
            "SELECT content FROM task_updates WHERE task_id=? AND kind='alert' "
            "ORDER BY id",
            (task_id,),
        )
    ]


async def _rows_of(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    return [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM review_dispatches WHERE task_id=? ORDER BY id", (task_id,)
        )
    ]


async def _report_on(client, task_id: int, *, confirmed=(), rejected=()) -> None:
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "agent_count": 1,
            "tokens_spent": 1000,
            "model": "grok-4.6",
            "raw_count": len(confirmed) + len(rejected),
            "findings_confirmed": list(confirmed),
            "findings_rejected": list(rejected),
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "cursor-cloud-reviewer",
        },
    )
    assert resp.status_code == 200, resp.text


# --- #1414: суточный потолок deep-ревью на проект ---------------------------
#
# Прод, облачный канал: 22.09 — 21 deep, 23.09 — 40, 24.09 — 29. Правило
# профиля покупало deep, не зная, сколько deep проект уже купил сегодня.


async def _cap_project(db: aiosqlite.Connection, slug: str, policy: dict) -> int:
    pid = await repo.create_project(
        db,
        slug=slug,
        name=slug.title(),
        repo_name="mrPDA/spike-repo",
        workspace_path="/tmp/ws",
    )
    await repo.update_project(
        db, pid, gate_policy=json.dumps({"verdict": "auto", **policy})
    )
    return pid


async def _submitted_in(
    db: aiosqlite.Connection,
    pid: int,
    *,
    diff: str = _DIFF_506,
    risks: list[dict] | None = None,
    override: str = "",
) -> int:
    """Сдача в УЖЕ заведённый проект: потолок считается по проекту."""
    epic = await _node(db, title="epic", task_type="epic", parent_id=None)
    await repo.update_task(db, epic, project_id=pid)
    feature = await _node(db, title="feature", task_type="feature", parent_id=epic)
    task_id = await _node(db, title="probe", task_type="task", parent_id=feature)
    await repo.add_task_update(db, task_id, "dev", "status", "Plan: work")
    await repo.update_task_structured(
        db, task_id, TaskRefine(affected_areas=["docs/notes.md"], risks=risks)
    )
    if override:
        await db.execute(
            "UPDATE tasks SET machine_review_override = ? WHERE id = ?",
            (override, task_id),
        )
    await db.commit()
    plugins.git_ops = _PinnedGitOps(_TIP, ["docs/notes.md"], diff)
    started = await services.pair_start_task(db, task_id, caller="dev-agent")
    assert started.status.value == "running"
    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )
    assert view.status.value == "review"
    return task_id


async def _dispatch_notes(db: aiosqlite.Connection, task_id: int) -> list[str]:
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND content LIKE ?",
        (task_id, "Кросс-модельное ревью вызвано хабом%"),
    )
    return [r[0] for r in rows]


# --- Маленькая пересдача получает lite (#1416) --------------------------------
#
# 13-25.09: 73 из 125 deep — пересдачи. На пересдаче deep даёт подтверждённых
# почти столько же, сколько lite (0,48 против 0,43 на отчёт), а фиксированная
# цена харнесса (~4M токенов) при паре правленых строк не окупается. Правило
# меряет АВТОРСКУЮ часть дельты (#1249) и не трогает ни ручной запрос, ни
# security, ни пересдачу, где дельту не доказать.


def _author_patch(lines: int, *, generated: int = 0) -> str:
    """Авторская серия правок: ``lines`` изменённых строк, последняя —
    маркер процессной поверхности. ``generated`` строк сверху — в uv.lock,
    которые в счёт не идут."""
    body = [f"+    value_{i} = {i}" for i in range(lines - 1)]
    body.append("+    subprocess.run(['true'], check=False)")
    patch = (
        f"diff --git a/{_AUTHOR_FILE} b/{_AUTHOR_FILE}\n"
        f"--- a/{_AUTHOR_FILE}\n+++ b/{_AUTHOR_FILE}\n"
        f"@@ -0,0 +1,{lines} @@\n" + "\n".join(body) + "\n"
    )
    if generated:
        patch += (
            "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n"
            f"@@ -0,0 +1,{generated} @@\n"
            + "\n".join(f'+name = "pkg-{i}"' for i in range(generated))
            + "\n"
        )
    return patch


async def _gen2_reasons(db: aiosqlite.Connection, task_id: int) -> list[str]:
    rows = await db.execute_fetchall(
        "SELECT payload FROM events WHERE kind='review_dispatched' AND task_id=?",
        (task_id,),
    )
    payloads = [json.loads(r[0]) for r in rows]
    gen2 = [p for p in payloads if p.get("generation") == 2]
    assert gen2, payloads
    return list(gen2[-1]["profile_reasons"])


async def _small_delta_profile(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch,
    slug: str,
    lines: int,
    *,
    ancestor: bool = True,
    override: str = "",
    **submitted: object,
) -> tuple[str, list[str]]:
    recorder = _DispatchRecorder({"agent": {"id": f"bc-{slug}"}, "run": {"id": slug}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, slug, **submitted)
    if override:
        await db.execute(
            "UPDATE tasks SET machine_review_override = ? WHERE id = ?",
            (override, task_id),
        )
        await db.commit()
    patch = _author_patch(lines, generated=200)
    plugins.git_ops = _AncestryGitOps(
        _TIP, ["docs/notes.md"], patch, ancestor=ancestor, delta=patch, own=patch
    )

    assert await maybe_dispatch_review(db, task_id)

    return (await _any_dispatch_row(db, task_id))["profile"], await _gen2_reasons(
        db, task_id
    )


async def _card(db: aiosqlite.Connection, task_id: int) -> list[str]:
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? ORDER BY id", (task_id,)
    )
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# #1561: deep сначала в локальный путь — по ключу deep_reviewer проекта
# ---------------------------------------------------------------------------

_QWEN = "qwen3.8-max"
_LOCAL_FIRST = {"review": "dispatch", "deep_reviewer": "local"}


def _qwen_reviewer(monkeypatch, tmp_path, script: str | None = None) -> None:
    """Локальный CLI, настроенный на qwen3.8-max, как на проде (--model в CMD)."""
    import shlex

    _stub_reviewer(monkeypatch, tmp_path, script or _reporting_stub())
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_CMD",
        shlex.join(
            ["-c", script or _reporting_stub(), "--model", _QWEN],
        ),
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_MODEL", "")


async def _all_dispatches(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    return [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM review_dispatches WHERE task_id=? ORDER BY id", (task_id,)
        )
    ]


async def _card_text(db: aiosqlite.Connection, task_id: int) -> str:
    return "\n".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
