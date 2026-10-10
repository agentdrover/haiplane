"""Definition of Ready (DoR) evaluator.

Pure deterministic logic — no LLM involvement. The required check set is
table-driven per ``WorkType`` so that the gate can be relaxed for chores
or tightened for incidents without touching call sites.

Severity (blocking/high/medium/low) intentionally lives in the
Recommendations engine (#38), not here. DoR only answers "is this check
satisfied?". It is the readiness/recommendations layer that decides how
much a missing piece costs.
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hub import config, repository as repo
from hub.db import deserialize_str_list
from hub.models import DoRCheckItem, WorkType
from hub.services.orchestrator_queue import overlap

if TYPE_CHECKING:
    from hub.services.dor_snapshot import DorSnapshot

log = logging.getLogger("hub.services.dor")

# All known DoR check keys. Anything outside this tuple is a typo somewhere.
DOR_CHECK_KEYS: tuple[str, ...] = (
    "has_user_story",
    "has_problem_statement",
    "has_business_value",
    "has_scope_in",
    "has_acceptance_criteria",
    "has_validation_commands",
    "has_size",
    "has_wip_tag",
    # #842: which code the work touches. Required only for the work types
    # that mean a code change — see DOR_REQUIRED_BY_WORK_TYPE.
    "has_affected_areas",
    # Discovery checks (#331). Advisory — see DOR_ADVISORY_KEYS below.
    "has_outcome_hypothesis",
    "has_redesign_decision",
    "has_agent_fit",
    # #1456: paths in validation_commands / test_ref exist on the base branch
    # or are created by the task. Advisory for scoring, see below.
    "statement_paths_resolve",
)

#: #1604: проект задачи без workspace_path. Не входит в DOR_CHECK_KEYS: пункт
#: появляется в отчёте только когда клона нет, у проекта с клоном его нет совсем.
WORKSPACE_MISSING_CHECK = "workspace_missing"

# Checks that are visible but free. They appear in the DoR table and earn a
# recommendation, but cost nothing in the readiness score.
#
# Why a separate set rather than "just leave them out of every required
# profile": a non-required failing check still costs penalty_optional
# (readiness.py). Since no task in an existing installation can have these
# fields — they did not exist until this change — every task in the backlog
# would silently lose points on the day this ships, and would look like it
# had got worse without anyone touching it. Advisory keys are scored at
# zero, following the precedent of the AC-quality warnings (#331).
DOR_ADVISORY_KEYS: frozenset[str] = frozenset(
    {
        "has_outcome_hypothesis",
        "has_redesign_decision",
        "has_agent_fit",
        # #1456: free in the score like the Discovery checks, but asked of
        # EVERY work type — see DoREvaluation.advisory users in evaluate.
        "statement_paths_resolve",
        WORKSPACE_MISSING_CHECK,
    }
)

#: #1647: пункты профиля задачи-состояния. Не входят в DOR_CHECK_KEYS: у
#: commit-задачи их в таблице нет вовсе (как workspace_missing), у state
#: добавляются в конец.
STATE_ROLLBACK_CHECK = "has_rollback"
STATE_AC_CHECK = "has_state_ac"

#: Проверки по коду, которые к задаче-состоянию неприменимы: у неё нет ни
#: областей кода, ни команд проверки. Они отдаются как пройденные с пометкой —
#: так они не штрафуют оценку и не рождают кодовых рекомендаций.
STATE_INAPPLICABLE: frozenset[str] = frozenset(
    {"has_affected_areas", "has_validation_commands"}
)
STATE_NOT_APPLICABLE_DETAIL = "not applicable to a state task (no code, no commands)"

STATEMENT_PATHS_CHECK = "statement_paths_resolve"
#: Начало detail при нарушении — по нему approve находит, что записать в карточку.
STATEMENT_PATHS_MARK = "Путь постановки не найден на базе и не покрыт affected_areas"

# Which work types are actually ASKED for Discovery. The spec scopes the
# Discovery block to the feature profile, and that scoping is what keeps the
# nudges meaningful: a bugfix or a chore has no outcome hypothesis to state,
# and three permanent suggestions on every task in the backlog would become
# wallpaper within a week. Checks outside this set are still evaluated and
# still rendered — they just do not generate a suggestion (#331).
DOR_ADVISORY_WORK_TYPES: frozenset[str] = frozenset({WorkType.feature.value})

# Required check sets per work type.
#
# Rationale per profile:
# - feature: full DoR — most expensive to do wrong, must be ready.
# - bug: skip user_story (problem_statement is the "what broke"), but keep
#   business_value so we can distinguish a $1M-customer P1 from a cosmetic
#   glitch. AC + validation are mandatory so the fix is verifiable.
# - refactor: no user_story / business_value (internal change). wip_tag
#   required for capacity tracking — refactors usually load tech_debt.
# - chore: minimal — scope, validation, size. Lots of chores would never
#   pass full DoR and would just clutter the inbox.
# - docs: scope + size only. Adding ACs to a doc change is overkill.
# - spike: time-boxed exploration. AC required as a proxy for the
#   completion criterion (e.g. "we have a documented answer to <Q>"); a
#   first-class ``timebox_hours`` field is on the post-MVP backlog.
# - incident: must explain what broke (problem_statement) and how we'll
#   verify the fix (validation_commands + AC). Even under fire, the team
#   needs an explicit "fixed when" criterion so the postmortem is honest.
DOR_REQUIRED_BY_WORK_TYPE: dict[str, frozenset[str]] = {
    WorkType.feature.value: frozenset(
        {
            "has_affected_areas",
            "has_user_story",
            "has_problem_statement",
            "has_business_value",
            "has_scope_in",
            "has_acceptance_criteria",
            "has_validation_commands",
            "has_size",
            "has_wip_tag",
        }
    ),
    WorkType.bug.value: frozenset(
        {
            "has_affected_areas",
            "has_problem_statement",
            "has_business_value",
            "has_scope_in",
            "has_acceptance_criteria",
            "has_validation_commands",
            "has_size",
            "has_wip_tag",
        }
    ),
    WorkType.refactor.value: frozenset(
        {
            "has_affected_areas",
            "has_problem_statement",
            "has_scope_in",
            "has_acceptance_criteria",
            "has_validation_commands",
            "has_size",
            "has_wip_tag",
        }
    ),
    WorkType.chore.value: frozenset(
        {"has_scope_in", "has_validation_commands", "has_size"}
    ),
    WorkType.docs.value: frozenset({"has_scope_in", "has_size"}),
    WorkType.spike.value: frozenset(
        {"has_problem_statement", "has_acceptance_criteria", "has_size"}
    ),
    WorkType.incident.value: frozenset(
        {
            "has_affected_areas",
            "has_problem_statement",
            "has_acceptance_criteria",
            "has_validation_commands",
        }
    ),
}


@dataclass(frozen=True)
class DoREvaluation:
    """Result of a DoR evaluation for one task.

    - ``checks`` always contains the full ``DOR_CHECK_KEYS`` set so
      consumers can render a complete table.
    - ``required`` is the subset that must pass for ``passed=True``.
    - ``missing_required`` is the easy lookup for what to fix first.
    """

    checks: list[DoRCheckItem]
    required: frozenset[str]
    missing_required: frozenset[str]
    # Advisory keys that this work type is asked about (#331). Never part of
    # ``required``, so never blocking and never scored — they only decide
    # whether a suggestion is offered.
    advisory: frozenset[str] = frozenset()

    @property
    def passed(self) -> bool:
        return not self.missing_required


def _required_for(work_type: str | None) -> frozenset[str]:
    """Resolve required checks for a work type, defaulting to feature.

    Unknown / missing work_type is treated as 'feature' on purpose:
    strict-by-default avoids accidentally letting under-specified tasks
    sneak past the gate. Unknown values are logged so a missing profile
    after a WorkType extension does not stay silent in production.
    """
    if not work_type:
        return DOR_REQUIRED_BY_WORK_TYPE[WorkType.feature.value]
    profile = DOR_REQUIRED_BY_WORK_TYPE.get(work_type)
    if profile is None:
        log.warning(
            "unknown work_type %r — falling back to 'feature' DoR profile", work_type
        )
        return DOR_REQUIRED_BY_WORK_TYPE[WorkType.feature.value]
    return profile


# --- Пути постановки (#1456) ---------------------------------------------
#
# Статическая проверка: хаб команды не запускает, он читает строки и сверяет
# пути с деревом базовой ветки. Путь репо — аргумент с '/' или известным
# расширением файла; флаги, URL, абсолютные пути, пути с подстановкой и цели
# перенаправления путями репо не считаются. Точность намеренно ниже полноты:
# ложное срабатывание в warn стоит строки в карточке, пропуск — хака в продукте.

_FILE_EXT = re.compile(
    r"\.(py|toml|json|ya?ml|md|txt|sh|cfg|ini|sql|html|css|js|ts|lock|csv)$"
)
_SHELL_MAGIC = frozenset("*?[]{}$`")
_PUNCT = frozenset("();<>|&")


@dataclass(frozen=True)
class StatementPathViolation:
    path: str
    source: str


@dataclass(frozen=True)
class StatementPathsResult:
    """Итог сверки путей: нарушения, либо «не проверено» с причиной."""

    mode: str
    checked: bool = True
    violations: tuple[StatementPathViolation, ...] = ()
    reason: str = ""
    paths_total: int = 0


_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _path_candidate(token: str) -> str | None:
    if token.startswith("-"):
        # Only ``--opt=value`` carries a value; a bare flag is never a path.
        if "=" not in token:
            return None
        token = token.split("=", 1)[1]
    elif _ENV_ASSIGN.match(token):
        token = token.split("=", 1)[1]
    # The path is what stands before ``::`` — a parametrized locator such as
    # ``a.py::t[mode=require]`` keeps its ``=`` and ``[`` out of the path.
    token = token.split("::", 1)[0].strip().strip("'\"")
    if not token or token.startswith(("-", "/", "~")) or "://" in token:
        return None
    if _SHELL_MAGIC & set(token) or ".." in token.split("/"):
        # Glob and $VAR: not checkable statically — unknown, not a violation.
        return None
    while token.startswith("./"):
        token = token[2:]
    token = token.rstrip("/")
    if token and ("/" in token or _FILE_EXT.search(token)):
        return token
    return None


def _command_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return command.split()


def statement_paths_in(command: str) -> list[str]:
    """Пути репо из строки команды или test_ref, без повторов, по порядку."""
    found: list[str] = []
    skip_next = False
    for token in _command_tokens(command):
        if set(token) <= _PUNCT:
            skip_next = "<" in token or ">" in token
            continue
        if skip_next:
            skip_next = False
            continue
        path = _path_candidate(token)
        if path and path not in found:
            found.append(path)
    return found


def _on_base(path: str, tree: set[str]) -> bool:
    return path in tree or any(name.startswith(path + "/") for name in tree)


def check_statement_paths(
    *,
    validation_commands: list[str],
    test_refs: list[tuple[str, str]],
    affected_areas: list[str],
    tree: set[str] | None,
    mode: str,
    unread_reason: str = "",
) -> StatementPathsResult:
    """Каждый путь постановки — на базе или покрыт affected_areas (#1456).

    Покрытие — то же правило, что у очереди (``orchestrator_queue.overlap``):
    второй копии нет. ``tree=None`` — база не прочитана: «не проверено» с
    причиной, а не «чисто» (#725).
    """
    if mode == "off":
        return StatementPathsResult(mode=mode)
    sources: list[tuple[str, str]] = [
        (path, f"команда «{cmd}»")
        for cmd in validation_commands
        for path in statement_paths_in(cmd)
    ]
    for ac_id, ref in test_refs:
        sources.extend((p, f"test_ref {ac_id}") for p in statement_paths_in(ref))
    if not sources:
        return StatementPathsResult(mode=mode)
    total = len({path for path, _ in sources})
    if tree is None:
        return StatementPathsResult(
            mode=mode,
            checked=False,
            reason=unread_reason or "базовая ветка не прочитана",
            paths_total=total,
        )
    seen: set[str] = set()
    violations: list[StatementPathViolation] = []
    for path, source in sources:
        if path in seen:
            continue
        if any(overlap(area, path) for area in affected_areas) or _on_base(path, tree):
            continue
        seen.add(path)
        violations.append(StatementPathViolation(path=path, source=source))
    return StatementPathsResult(
        mode=mode, violations=tuple(violations), paths_total=total
    )


def _statement_paths_item(result: StatementPathsResult | None) -> DoRCheckItem:
    key = STATEMENT_PATHS_CHECK
    if result is None or result.mode == "off":
        return DoRCheckItem(key=key, passed=True, detail="statement path check is off")
    if result.paths_total == 0:
        return DoRCheckItem(
            key=key,
            passed=True,
            detail="no repo paths in validation commands or test_ref",
        )
    if not result.checked:
        return DoRCheckItem(
            key=key, passed=False, detail=f"не проверено: {result.reason}"
        )
    if result.violations:
        listed = "; ".join(f"{v.path} ({v.source})" for v in result.violations)
        return DoRCheckItem(
            key=key, passed=False, detail=f"{STATEMENT_PATHS_MARK}: {listed}"
        )
    return DoRCheckItem(
        key=key,
        passed=True,
        detail=f"{result.paths_total} path(s) exist on base or are created by the task",
    )


@dataclass(frozen=True)
class WorkspaceGap:
    """Проект задачи без workspace_path: кто он и откуда взялся (#1604)."""

    slug: str
    source: str


def _workspace_missing_item(gap: WorkspaceGap) -> DoRCheckItem:
    return DoRCheckItem(
        key=WORKSPACE_MISSING_CHECK,
        passed=False,
        detail=(
            f"{WORKSPACE_MISSING_CHECK}: у проекта {gap.slug} ({gap.source}) не "
            "задан workspace_path — sha сдачи не закрепляется; автозаказ "
            "машинного ревью не идёт; проверки предмета и путей постановки "
            "не применяются"
        ),
    )


def _state_items(rollback: str | None, state_ac_count: int) -> list[DoRCheckItem]:
    """Пункты профиля state: способ отката и AC, проверяемый человеком."""
    from hub.services.result_kind import STATE_AC_KINDS

    kinds = ", ".join(STATE_AC_KINDS)
    return [
        DoRCheckItem(
            key=STATE_ROLLBACK_CHECK,
            passed=bool(rollback and rollback.strip()),
            detail=(
                "rollback is filled"
                if (rollback or "").strip()
                else "rollback is empty"
            ),
        ),
        DoRCheckItem(
            key=STATE_AC_CHECK,
            passed=state_ac_count > 0,
            detail=(
                f"{state_ac_count} AC verifiable by {kinds}"
                if state_ac_count
                else f"no AC with verifiable_by in ({kinds}): a test AC alone "
                "does not prove the state of the world"
            ),
        ),
    ]


def evaluate_from_data(
    *,
    work_type: str | None,
    user_story: str | None,
    problem_statement: str | None,
    business_value: str | None,
    scope_in_count: int,
    validation_count: int,
    size: str | None,
    wip_tag: str | None,
    ac_count: int,
    affected_areas_count: int = 0,
    outcome_metric: str | None = None,
    redesign_decision: str | None = None,
    agent_fit: str | None = None,
    statement_paths: StatementPathsResult | None = None,
    workspace_gap: WorkspaceGap | None = None,
    result_kind: str | None = None,
    rollback: str | None = None,
    state_ac_count: int = 0,
) -> DoREvaluation:
    """Pure, side-effect-free DoR evaluation from explicit data.

    Always returns checks for every key in ``DOR_CHECK_KEYS``, regardless
    of whether the work type cares about each one. ``passed`` is computed
    only against the required subset for the given ``work_type``.
    """
    checks_by_key: dict[str, DoRCheckItem] = {
        "has_user_story": DoRCheckItem(
            key="has_user_story",
            passed=bool(user_story and user_story.strip()),
            detail="user_story is filled" if user_story else "user_story is empty",
        ),
        "has_problem_statement": DoRCheckItem(
            key="has_problem_statement",
            passed=bool(problem_statement and problem_statement.strip()),
            detail=(
                "problem_statement is filled"
                if problem_statement
                else "problem_statement is empty"
            ),
        ),
        "has_business_value": DoRCheckItem(
            key="has_business_value",
            passed=bool(business_value and business_value.strip()),
            detail=(
                "business_value is filled"
                if business_value
                else "business_value is empty"
            ),
        ),
        "has_scope_in": DoRCheckItem(
            key="has_scope_in",
            passed=scope_in_count > 0,
            detail=f"scope_in has {scope_in_count} item(s)",
        ),
        "has_affected_areas": DoRCheckItem(
            key="has_affected_areas",
            passed=affected_areas_count > 0,
            detail=f"affected_areas has {affected_areas_count} item(s)",
        ),
        "has_acceptance_criteria": DoRCheckItem(
            key="has_acceptance_criteria",
            passed=ac_count > 0,
            detail=f"{ac_count} acceptance criteria defined",
        ),
        "has_validation_commands": DoRCheckItem(
            key="has_validation_commands",
            passed=validation_count > 0,
            detail=f"{validation_count} validation command(s) defined",
        ),
        "has_size": DoRCheckItem(
            key="has_size",
            passed=bool(size),
            detail=f"size = {size}" if size else "size is not set",
        ),
        "has_wip_tag": DoRCheckItem(
            key="has_wip_tag",
            passed=bool(wip_tag),
            detail=f"wip_tag = {wip_tag}" if wip_tag else "wip_tag is not set",
        ),
        "has_outcome_hypothesis": DoRCheckItem(
            key="has_outcome_hypothesis",
            passed=bool(outcome_metric and outcome_metric.strip()),
            detail=(
                "outcome_metric is filled"
                if outcome_metric
                else "outcome_metric is empty"
            ),
        ),
        "has_redesign_decision": DoRCheckItem(
            key="has_redesign_decision",
            passed=bool(redesign_decision),
            detail=(
                f"redesign_decision = {redesign_decision}"
                if redesign_decision
                else "redesign_decision is not set"
            ),
        ),
        "has_agent_fit": DoRCheckItem(
            key="has_agent_fit",
            passed=bool(agent_fit),
            detail=f"agent_fit = {agent_fit}" if agent_fit else "agent_fit is not set",
        ),
        STATEMENT_PATHS_CHECK: _statement_paths_item(statement_paths),
    }

    state = (result_kind or "") == "state"
    if state:
        # #1647: профиль work_type минус две кодовые проверки, плюс rollback и
        # AC, которые человек может проверить. Пути постановки и workspace
        # проекта — тоже про код, и для state не спрашиваются.
        for key in STATE_INAPPLICABLE:
            checks_by_key[key] = DoRCheckItem(
                key=key, passed=True, detail=STATE_NOT_APPLICABLE_DETAIL
            )
        checks_by_key[STATEMENT_PATHS_CHECK] = _statement_paths_item(None)
        statement_paths = None
        workspace_gap = None

    # Stable order — always DOR_CHECK_KEYS — for deterministic UI rendering.
    checks = [checks_by_key[k] for k in DOR_CHECK_KEYS]
    if workspace_gap is not None:
        checks.append(_workspace_missing_item(workspace_gap))
    required = _required_for(work_type)
    if state:
        checks += _state_items(rollback, state_ac_count)
        required = (required - STATE_INAPPLICABLE) | {
            STATE_ROLLBACK_CHECK,
            STATE_AC_CHECK,
        }
    if (
        statement_paths
        and statement_paths.mode == "require"
        and (statement_paths.violations)
    ):
        # Отказывает только найденное нарушение: «не проверено» не блокирует.
        required = required | {STATEMENT_PATHS_CHECK}
    missing = frozenset(c.key for c in checks if c.key in required and not c.passed)
    advisory = frozenset({STATEMENT_PATHS_CHECK, WORKSPACE_MISSING_CHECK}) | (
        DOR_ADVISORY_KEYS - {STATEMENT_PATHS_CHECK, WORKSPACE_MISSING_CHECK}
        if (work_type or WorkType.feature.value) in DOR_ADVISORY_WORK_TYPES
        else frozenset()
    )
    return DoREvaluation(
        checks=checks,
        required=required,
        missing_required=missing,
        advisory=advisory,
    )


async def record_statement_paths(db, task_id: int, dor_checks) -> None:
    """Нарушение путей постановки при одобрении — записью в карточку (#1456).

    Зовётся из approve_task и из автоодобрения: в ``warn`` одобрение проходит, и
    эта запись — единственный след. «Не проверено» не пишется: это не
    нарушение, оно видно в таблице DoR.
    """
    for check in dor_checks:
        if check.key == STATEMENT_PATHS_CHECK and check.detail.startswith(
            STATEMENT_PATHS_MARK
        ):
            await repo.add_task_update(
                db, task_id, "", "alert", f"Approve: {check.detail}"
            )


def workspace_gap_from_project(
    project: Mapping[str, Any], task_id: int
) -> WorkspaceGap | None:
    """Проект без workspace_path по уже снятым данным проекта (#1604, #1610).

    ``project`` - поля снимка (``PROJECT_FIELDS``): ``source_id`` - узел, давший
    проект, ``None`` - запасной default. Нет проекта вовсе - не утверждаем ничего.
    """
    if project["id"] is None or (project["workspace_path"] or "").strip():
        return None
    source_id = project["source_id"]
    if source_id is None:
        source = "резервный проект default"
    elif source_id == task_id:
        source = "свой"
    else:
        source = f"унаследован от эпика #{source_id}"
    return WorkspaceGap(slug=str(project["slug"]), source=source)


async def workspace_gap_for(db, task_id: int) -> WorkspaceGap | None:
    """Проект без workspace_path для задачи или ``None`` (#1604).

    Проект определяет тот же разбор, что у project_git_context
    (``resolve_project_with_source``), и смотрится значение ПРОЕКТА, не env:
    пустой путь у default остаётся «без workspace», даже если git_ops упадёт
    на запасной путь из окружения.
    """
    project, source_id = await repo.resolve_project_with_source(db, task_id)
    if project is None:
        return None
    return workspace_gap_from_project(
        {
            "id": project["id"],
            "slug": project["slug"],
            "workspace_path": project["workspace_path"],
            "source_id": source_id,
        },
        task_id,
    )


async def record_workspace_missing(db, task_id: int) -> None:
    """Проект без workspace при одобрении - одной строкой в ленту (#1604).

    Рядом с record_statement_paths, из тех же двух мест (approve_task и
    автоодобрение). Читает проект сама, а не из отчёта DoR: автоодобрение из
    стюарда не получает ``dor_checks``, и строка не должна зависеть от того,
    кто вызвал. Не блокирует и не заменяет другие alert одобрения.
    """
    gap = await workspace_gap_for(db, task_id)
    if gap is not None:
        await repo.add_task_update(
            db,
            task_id,
            "",
            "alert",
            f"Approve: {_workspace_missing_item(gap).detail}",
        )


async def _base_tree(
    workspace_path: str, default_branch: str
) -> tuple[set[str] | None, str]:
    """Дерево базовой ветки проекта из локального клона и причина, если не прочитано.

    Без сети: ``origin/<base>``, затем локальное имя — как у проверки предмета
    (#1232). Любой сбой читается как «не прочитано», не как пустое дерево.
    Вход - поля снимка проекта, не БД: это внешнее наблюдение (git), и оно
    вне отпечатка (#1610).
    """
    from hub.services.readiness import _tree_at

    repo_path = workspace_path.strip()
    base = default_branch.strip() or config.PAIR_BASE_BRANCH
    if not repo_path:
        return None, "у проекта не задан workspace_path — клона нет"
    try:
        found = await _tree_at(repo_path, (f"origin/{base}", base))
    except Exception:  # noqa: BLE001 - DoR must assemble regardless
        log.warning("statement path base read failed in %s", repo_path)
        return None, "базовую ветку прочитать не удалось"
    if found is None:
        return None, f"базовую ветку {base} в клоне {repo_path} прочитать не удалось"
    return found[1], ""


def _statement_paths_for(snapshot: DorSnapshot) -> StatementPathsResult:
    return _statement_paths_with(snapshot, set(), "")


def _statement_paths_with(
    snapshot: DorSnapshot, tree: set[str] | None, reason: str
) -> StatementPathsResult:
    acs = snapshot.acs
    return check_statement_paths(
        validation_commands=deserialize_str_list(snapshot.task["validation_commands"]),
        test_refs=[(str(a["ac_id"]), str(a["test_ref"])) for a in acs if a["test_ref"]],
        affected_areas=deserialize_str_list(snapshot.task["affected_areas"]),
        tree=tree,
        mode=snapshot.project["statement_paths"] or "warn",
        unread_reason=reason,
    )


async def _statement_paths_checked(snapshot: DorSnapshot) -> StatementPathsResult:
    # Без путей в постановке (или при off) git не нужен вовсе.
    first = _statement_paths_for(snapshot)
    if first.mode == "off" or first.paths_total == 0:
        return first
    tree, reason = await _base_tree(
        snapshot.workspace_path, str(snapshot.project["default_branch"] or "")
    )
    return _statement_paths_with(snapshot, tree, reason)


async def evaluate_dor(
    db, task_id: int, snapshot: DorSnapshot | None = None
) -> DoREvaluation:
    """Evaluate DoR from one DB snapshot of the task (#1610).

    Без ``snapshot`` снимок берётся здесь; одобрение передаёт свой, чтобы DoR и
    отпечаток считались из одних и тех же данных.
    """
    from hub.services.dor_snapshot import load_dor_snapshot

    if snapshot is None:
        snapshot = await load_dor_snapshot(db, task_id)
    row = snapshot.task
    from hub.services.result_kind import is_state, qualifying_ac_count

    state = is_state(row)

    # Снимок содержит только отпечатываемые поля: чтение чужого - KeyError, а не
    # тихо устаревший DoR (guard #1610). With strict migrations (review I2) it
    # is safer to fail loudly than to hide a missing column.
    return evaluate_from_data(
        work_type=row["work_type"],
        user_story=row["user_story"],
        problem_statement=row["problem_statement"],
        business_value=row["business_value"],
        scope_in_count=len(deserialize_str_list(row["scope_in"])),
        validation_count=len(deserialize_str_list(row["validation_commands"])),
        size=row["size"],
        wip_tag=row["wip_tag"],
        ac_count=len(snapshot.acs),
        affected_areas_count=len(deserialize_str_list(row["affected_areas"])),
        outcome_metric=row["outcome_metric"],
        redesign_decision=row["redesign_decision"],
        agent_fit=row["agent_fit"],
        # #1647: у задачи-состояния путей постановки нет, и git не читается.
        statement_paths=(None if state else await _statement_paths_checked(snapshot)),
        workspace_gap=workspace_gap_from_project(snapshot.project, task_id),
        result_kind=row["result_kind"],
        rollback=row["rollback"],
        state_ac_count=qualifying_ac_count(snapshot.acs),
    )


__all__ = [
    "DOR_ADVISORY_KEYS",
    "DOR_ADVISORY_WORK_TYPES",
    "DOR_CHECK_KEYS",
    "DOR_REQUIRED_BY_WORK_TYPE",
    "DoREvaluation",
    "STATEMENT_PATHS_CHECK",
    "STATEMENT_PATHS_MARK",
    "WORKSPACE_MISSING_CHECK",
    "WorkspaceGap",
    "record_workspace_missing",
    "workspace_gap_for",
    "workspace_gap_from_project",
    "StatementPathsResult",
    "check_statement_paths",
    "record_statement_paths",
    "evaluate_dor",
    "evaluate_from_data",
    "statement_paths_in",
]
