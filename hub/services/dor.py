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
from dataclasses import dataclass

from hub import config, repository as repo
from hub.db import deserialize_str_list
from hub.models import DoRCheckItem, WorkType
from hub.services.orchestrator_queue import overlap

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
    }
)

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


def _path_candidate(token: str) -> str | None:
    if "=" in token:
        token = token.split("=", 1)[1]
    token = token.split("::", 1)[0].strip()
    if not token or token.startswith(("-", "/", "~")) or "://" in token:
        return None
    if _SHELL_MAGIC & set(token) or ".." in token.split("/"):
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

    # Stable order — always DOR_CHECK_KEYS — for deterministic UI rendering.
    checks = [checks_by_key[k] for k in DOR_CHECK_KEYS]
    required = _required_for(work_type)
    if (
        statement_paths
        and statement_paths.mode == "require"
        and (statement_paths.violations)
    ):
        # Отказывает только найденное нарушение: «не проверено» не блокирует.
        required = required | {STATEMENT_PATHS_CHECK}
    missing = frozenset(c.key for c in checks if c.key in required and not c.passed)
    advisory = frozenset({STATEMENT_PATHS_CHECK}) | (
        DOR_ADVISORY_KEYS - {STATEMENT_PATHS_CHECK}
        if (work_type or WorkType.feature.value) in DOR_ADVISORY_WORK_TYPES
        else frozenset()
    )
    return DoREvaluation(
        checks=checks,
        required=required,
        missing_required=missing,
        advisory=advisory,
    )


async def _base_tree(db, task_id: int) -> tuple[set[str] | None, str]:
    """Дерево базовой ветки проекта из локального клона и причина, если не прочитано.

    Без сети: ``origin/<base>``, затем локальное имя — как у проверки предмета
    (#1232). Любой сбой читается как «не прочитано», не как пустое дерево.
    """
    from hub.services.orchestration import project_git_context
    from hub.services.readiness import _tree_at

    try:
        ctx = await project_git_context(db, task_id)
        repo_path = str(ctx.get("repo") or "").strip()
        base = str(ctx.get("base_branch") or "").strip() or config.PAIR_BASE_BRANCH
        if not repo_path:
            return None, "у проекта не задан workspace_path — клона нет"
        found = await _tree_at(repo_path, (f"origin/{base}", base))
    except Exception:  # noqa: BLE001 - DoR must assemble regardless
        log.warning("statement path base read failed for task #%s", task_id)
        return None, "базовую ветку прочитать не удалось"
    if found is None:
        return None, f"базовую ветку {base} в клоне {repo_path} прочитать не удалось"
    return found[1], ""


async def _statement_paths_for(db, task_id: int, row, acs) -> StatementPathsResult:
    from hub.services.project_policy import gate_policy_for_task, statement_paths_of

    mode = statement_paths_of(await gate_policy_for_task(db, task_id))
    commands = deserialize_str_list(row["validation_commands"])
    areas = deserialize_str_list(row["affected_areas"])
    test_refs = [(str(a["ac_id"]), str(a["test_ref"])) for a in acs if a["test_ref"]]

    def run(tree: set[str] | None, reason: str = "") -> StatementPathsResult:
        return check_statement_paths(
            validation_commands=commands,
            test_refs=test_refs,
            affected_areas=areas,
            tree=tree,
            mode=mode,
            unread_reason=reason,
        )

    # Без путей в постановке (или при off) git не нужен вовсе.
    first = run(set())
    if first.mode == "off" or first.paths_total == 0:
        return first
    tree, reason = await _base_tree(db, task_id)
    return run(tree, reason)


async def evaluate_dor(db, task_id: int) -> DoREvaluation:
    """Load task data + ACs from the repository and evaluate DoR."""
    row = await repo.get_task(db, task_id)
    if row is None:
        raise ValueError(f"task {task_id} not found")
    acs = await repo.list_acceptance_criteria(db, task_id)

    # The structured columns are guaranteed to exist after migration #46
    # for tasks-table. With strict migrations (review I2) it's safer to
    # let a KeyError propagate than to silently return None and hide a
    # missing-column bug behind a "task is empty" diagnostic.
    return evaluate_from_data(
        work_type=row["work_type"],
        user_story=row["user_story"],
        problem_statement=row["problem_statement"],
        business_value=row["business_value"],
        scope_in_count=len(deserialize_str_list(row["scope_in"])),
        validation_count=len(deserialize_str_list(row["validation_commands"])),
        size=row["size"],
        wip_tag=row["wip_tag"],
        ac_count=len(acs),
        affected_areas_count=len(deserialize_str_list(row["affected_areas"])),
        outcome_metric=row["outcome_metric"],
        redesign_decision=row["redesign_decision"],
        agent_fit=row["agent_fit"],
        statement_paths=await _statement_paths_for(db, task_id, row, acs),
    )


__all__ = [
    "DOR_ADVISORY_KEYS",
    "DOR_ADVISORY_WORK_TYPES",
    "DOR_CHECK_KEYS",
    "DOR_REQUIRED_BY_WORK_TYPE",
    "DoREvaluation",
    "STATEMENT_PATHS_CHECK",
    "STATEMENT_PATHS_MARK",
    "StatementPathsResult",
    "check_statement_paths",
    "evaluate_dor",
    "evaluate_from_data",
    "statement_paths_in",
]
