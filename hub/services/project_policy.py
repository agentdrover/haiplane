"""One reader for a project's gate policy, resolved from a task (#760).

The policy lives on the project and every consumer reaches it the same way —
by walking the hierarchy with ``resolve_project_for_task`` (#747). Before this
module each consumer inlined its own ``json.loads`` with its own failure
handling; three copies of a rule is how the copies start to disagree.

Every failure mode here — no project, unreadable column, a policy that is not
an object — returns the empty policy, which means "nothing delegated". A
policy that cannot be read must never read as permission.
"""

from __future__ import annotations

import json
import logging
from typing import NamedTuple

import aiosqlite

from hub import config, git_policy
from hub import repository as repo
from hub.models import DEFAULT_FORGE, FORGES

log = logging.getLogger(__name__)


def base_branch_of(project) -> str:
    """The integration branch of an already-loaded project row (#475).

    One reader for the question every gate asks — "which branch does work
    land on here?" — because the answer differs per project: the hub itself
    lives on ``develop``, calc-kids on ``master``, spike-bo on ``main``. Each
    gate that answered it with a literal answered it for one project only.

    The fallback is ``config.PAIR_BASE_BRANCH`` and it applies in exactly one
    case: the project declares no branch at all (missing column, NULL, empty
    or whitespace). A project that DOES declare one is never overridden — a
    fallback that can win over a declared value is not a fallback, it is a
    second source of truth, which is the defect this function removes.

    Takes a row or a mapping, and tolerates both being absent: gates run on
    projects that may not be resolvable, and a lookup failure must degrade to
    the configured default rather than raise inside a gate.
    """
    declared = ""
    if project is not None:
        try:
            declared = str(project["default_branch"] or "").strip()
        except (KeyError, IndexError, TypeError):
            declared = ""
    return declared or config.PAIR_BASE_BRANCH


def forge_of(project) -> str:
    """На каком форже живёт репозиторий уже загруженного проекта (#1114).

    Один читатель рядом с ``base_branch_of`` и по тем же правилам: терпит
    отсутствующий проект, отсутствующую колонку и мусор в ней, и во всех этих
    случаях отвечает ``github``.

    Почему падение читается как github, а не как «неизвестно». Неизвестного
    форжа не бывает: у репозитория всегда есть хостинг. Вопрос лишь в том,
    объявлен ли он, и до #1114 не был объявлен ни у кого — все проекты хаба
    на GitHub. То есть github здесь описывает то, что есть, а не догадку.
    Отвечать «неизвестно» было бы хуже вдвойне: вызывающему пришлось бы
    выбирать адаптер самому, и он выбрал бы тот же github, только молча и в
    каждом месте по-своему.

    Незнакомое значение тоже сводится к github, и это НЕ дублирование
    валидации на записи, а её дополнение. Запись отказывает и тем чинит
    причину; чтение случается в гейте, где отказать некому — и подставить
    туда несуществующий адаптер значит уронить доставку из-за строки в базе.
    """
    declared = ""
    if project is not None:
        try:
            declared = str(project["forge"] or "").strip().lower()
        except (KeyError, IndexError, TypeError):
            declared = ""
    return declared if declared in FORGES else DEFAULT_FORGE


async def forge_for_task(db: aiosqlite.Connection, task_id: int) -> str:
    """Форж проекта, которому принадлежит задача (#1114)."""
    try:
        project = await repo.resolve_project_for_task(db, task_id)
    except Exception:  # noqa: BLE001 - degradation is the contract
        log.warning("could not resolve project for task #%s", task_id)
        return DEFAULT_FORGE
    return forge_of(project)


async def base_branch_for_task(db: aiosqlite.Connection, task_id: int) -> str:
    """The integration branch of the project this task belongs to (#475)."""
    try:
        project = await repo.resolve_project_for_task(db, task_id)
    except Exception:  # noqa: BLE001 - degradation is the contract
        log.warning("could not resolve project for task #%s", task_id)
        return config.PAIR_BASE_BRANCH
    return base_branch_of(project)


# Recognised key of the release base in ``default_branch_policy`` (#812/#475).
# The UI has advertised ``{"release_base": "main"}`` since the policy column
# existed; until #475 nothing read it, so a project whose default_branch is
# already ``main`` (spike-bo) would have had a release PR opened from main
# into main. Declared per project, falling back to the configured branch.
RELEASE_BASE_KEY = "release_base"
# The closed set of keys ``default_branch_policy`` may carry (#886). It lives
# next to the reader on purpose: a set kept in the write layer drifts from the
# reader that gives keys their meaning, and the drift is invisible — an
# unrecognised key reads exactly like a key nobody wrote. ``release_base``
# missing is a legitimate state ("this project declared no release branch");
# ``releaseBase`` present is a typo that produces the same fallback while the
# owner looks at their JSON and believes the policy is set. Refusing on write
# is what tells those two apart, and it is the only moment where the person
# who made the typo is still there to fix it.
DEFAULT_BRANCH_POLICY_KEYS: tuple[str, ...] = (RELEASE_BASE_KEY,)


def validate_default_branch_policy(policy: object) -> dict:
    """Return the policy, or raise ``ValueError`` naming the unknown keys.

    The message names both halves — what was written and what exists —
    because it is shown to a human in the project card, not only logged:
    "unknown key" without the allowed list sends the reader to the source.
    """
    if policy is None:
        return {}
    if not isinstance(policy, dict):
        raise ValueError("default_branch_policy must be an object")
    unknown = set(policy) - set(DEFAULT_BRANCH_POLICY_KEYS)
    if unknown:
        raise ValueError(
            f"unknown default_branch_policy keys: {sorted(unknown)}; "
            f"allowed: {', '.join(DEFAULT_BRANCH_POLICY_KEYS)}"
        )
    return policy


def release_base_of(project) -> str:
    """Where this project's releases land; ``config.RELEASE_BRANCH`` by default."""
    try:
        policy = json.loads(project["default_branch_policy"] or "{}")
    except (ValueError, KeyError, IndexError, TypeError):
        policy = {}
    declared = ""
    if isinstance(policy, dict):
        declared = str(policy.get(RELEASE_BASE_KEY) or "").strip()
    return declared or config.RELEASE_BRANCH


def _workspace_of(project) -> str:
    """The clone path a project declares; ``""`` when it declares none."""
    if project is None:
        return ""
    try:
        return str(project["workspace_path"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""


def clone_branch_state(project) -> git_policy.BranchSyncState:
    """Does this project's clone protect the branch the project declares (#887).

    The project card and the API read the SAME function, because a divergence
    visible in one place and not the other is how two answers to one question
    start to disagree — the defect ``base_branch_of`` removed for the branch
    itself.

    Three states, and the third is load-bearing: a project with no workspace,
    or a workspace the hub cannot read, is ``unknown`` with a cause, never
    ``match``. Reading "could not look" as "agrees" is the exact shape of the
    bug this task fixes, and the rule is already settled twice in this code
    base — CIRunReportState (#546) and sha_check (#572).
    """
    return git_policy.branch_sync(_workspace_of(project), base_branch_of(project))


def rearm_clone(project) -> git_policy.HookStatus | None:
    """Rewrite the branch keys in this project's clone; None when there is none.

    The single point where a project's branches reach its clone outside the two
    moments #475 covered (cloning, and hub startup). Between those two, an owner
    changing ``default_branch`` in the UI changed nothing the hook could see
    until the next restart: the card showed the new branch while the clone kept
    refusing pushes from it.

    Not a second way to write the keys — it calls the same
    ``git_policy.activate_quietly`` those two moments call, with the same two
    readers for the branches. Idempotent (git config sets a key to a value it
    may already hold) and it touches no key but these; never raises, because a
    clone the hub cannot reach must not fail the edit that reached the database.
    """
    workspace = _workspace_of(project)
    if not workspace:
        return None
    return git_policy.activate_quietly(
        workspace,
        base_branch=base_branch_of(project),
        release_branch=release_base_of(project),
    )


async def gate_policy_for_task(db: aiosqlite.Connection, task_id: int) -> dict:
    """The project's gate policy for this task; ``{}`` when unknown."""
    try:
        project = await repo.resolve_project_for_task(db, task_id)
    except Exception:  # noqa: BLE001 - degradation is the contract
        log.warning("could not resolve project for task #%s", task_id)
        return {}
    if project is None:
        return {}
    return gate_policy_of(project)


def gate_policy_of(project) -> dict:
    """The gate policy of an already-loaded project row; ``{}`` when unknown."""
    try:
        policy = json.loads(project["gate_policy"] or "{}")
    except (ValueError, KeyError, TypeError):
        return {}
    return policy if isinstance(policy, dict) else {}


# Recognised values of the `review` key (#805). Anything else — a typo, a
# value from a future version, an empty string — reads as OFF: an unreadable
# policy must never spend tokens, exactly as it must never grant approval.
REVIEW_OFF = "off"
REVIEW_DISPATCH = "dispatch"

# Значения гейта, означающие «решает не человек» (#1151). Их два, и они
# ОДИН перечень на весь хаб: его читают и потребители политики, и замок
# #743, который такую политику не даёт сохранить на репозитории самого
# хаба. Два списка рядом разъезжаются, и разъезжается тот, который мягче —
# а мягче здесь означает «автоматика включилась там, где её запретили».
#
# steward стоит рядом с auto, а не вместо него: автопилот выносит вердикт
# на ЧИСТОЙ сдаче, стюард судит грязный путь. Проект, отдавший вердикт
# стюарду, не забирал его у автопилота — он добавил второго судью на те
# случаи, где первый молчит.
DELEGATED_VERDICTS: frozenset[str] = frozenset({"auto", "steward"})

# Все значения, которые вообще принимаются на гейтах dor и verdict.
# «human» плюс делегирующие: список один, чтобы новое делегирующее слово
# нельзя было научиться понимать, не научив API его принимать — и наоборот,
# нельзя было начать принимать значение, которого не понимает ни один
# потребитель.
GATE_HUMAN = "human"
GATE_VALUES: frozenset[str] = frozenset({GATE_HUMAN}) | DELEGATED_VERDICTS


class GateChoice(NamedTuple):
    """Значение гейта, за которым УЖЕ ЕСТЬ поведение, с подписью для человека.

    ``label`` читает тот, кто выбирает в форме; ``hint`` — тот, кто смотрит на
    карточку. Обе подписи обязательны: значение без подписи не «покажется как
    есть», оно уронит сверку в тестах — потому что молча показанное слово,
    которого никто не объяснил, и есть та самая ложь управления, ради которой
    заведён этот перечень.
    """

    value: str
    label: str
    hint: str


# ЧТО УЖЕ РАБОТАЕТ на каждом гейте — поключевой перечень (#1163), и это
# ДРУГОЙ вопрос, чем тот, на который отвечает GATE_VALUES.
#
# GATE_VALUES отвечает «что вообще можно записать»: он общий для dor и
# verdict, потому что граница приёма у них одна. Форма, собранная прямо из
# него, немедленно предложила бы dor=steward — значение, которое API примет,
# а поведения за ним нет до #1157: auto_approve сверяет dor ровно с "auto",
# то есть выбравший steward на dor не включил бы ничего. Управление, которое
# предлагает несуществующее, лжёт ровно так же, как то, которое прячет
# существующее — просто в другую сторону.
#
# Поэтому перечней два, и они не два описания одной границы: этот отвечает
# «что уже работает», и он ПОКЛЮЧЕВОЙ. Он же — единственный источник и для
# селекторов формы, и для бейджей карточки: одно место, куда #1157 допишет
# свою строку, чтобы поверхность узнала о доставленном поведении правкой, а
# не чьей-то памятью. Тест сверяет его с GATE_VALUES на включение:
# реализованное обязано быть принимаемым.
IMPLEMENTED_GATE_VALUES: dict[str, tuple[GateChoice, ...]] = {
    "dor": (
        GateChoice(
            GATE_HUMAN,
            "human — одобряет владелец",
            "DoR-апрув драфтов остаётся за владельцем проекта",
        ),
        GateChoice(
            "auto",
            "auto — одобряет политика",
            "DoR-апрув драфтов выполняет политика (#744)",
        ),
    ),
    "verdict": (
        GateChoice(
            GATE_HUMAN,
            "human — вердикт выносит владелец",
            "Вердикт на ревью остаётся за владельцем проекта",
        ),
        GateChoice(
            "auto",
            "auto — вердикт выносит политика",
            "Вердикт по чистым сдачам выносит политика (#745)",
        ),
        GateChoice(
            "steward",
            "steward — вердикт выносит агент-стюард",
            "Грязный путь судит агент-стюард, чистый по-прежнему автопилот (#1151)",
        ),
    ),
}


def gate_choices(key: str) -> tuple[GateChoice, ...]:
    """Реализованные значения гейта — то, и только то, что вправе предложить форма."""
    return IMPLEMENTED_GATE_VALUES.get(key, ())


def _stored_gate_values(policy: dict) -> dict[str, object]:
    """Сырые значения обоих гейтов, прочитанные ЛИТЕРАЛАМИ по одному на ключ.

    Перебирать ``IMPLEMENTED_GATE_VALUES`` здесь было бы короче и хуже: сверка
    ключей политики (tests/test_branch_policy_validation.py) читает ЭТОТ файл
    и обязана увидеть каждый ключ, который читатель спрашивает у политики —
    иначе ключ, забытый в разрешённом наборе, перестанет ловиться. Расхождение
    этих двух перечислений ловит тест рядом с перечнем.
    """
    return {"dor": policy.get("dor"), "verdict": policy.get("verdict")}


def gate_form_value(policy: dict, key: str) -> str:
    """Значение, которое форма обязана показать ВЫБРАННЫМ.

    Ровно сохранённое, если оно есть в перечне реализованных. Всё остальное —
    отсутствие ключа, мусор, значение, которое API принял, но поведения за ним
    ещё нет, — читается как ``human``, потому что именно так его читают
    потребители политики (#835): слово, которого никто не узнал, значит
    «человек», а не «кто-нибудь».

    Чего эта функция НЕ делает: она не подставляет human вместо распознанного
    значения. Прежний шаблон помечал human по условию «не auto» и тем показывал
    human при verdict=steward — утверждая то, чего в базе нет. Разница между
    «не смогли выбрать» и «показали неправду» здесь и живёт.
    """
    stored = _stored_gate_values(policy).get(key) if isinstance(policy, dict) else None
    if isinstance(stored, str) and any(c.value == stored for c in gate_choices(key)):
        return stored
    return GATE_HUMAN


def gate_delegate_badge(policy: dict, key: str) -> GateChoice | None:
    """Делегирующее значение гейта для витрины — или None, если решает человек.

    Карточка сравнивала значение со строкой ``auto`` и при steward молчала:
    тот же обман по умолчанию, что и в форме, только на витрине. Теперь
    ответ приходит из того же перечня и того же множества делегатов, что
    читают потребители политики и замок #743.
    """
    value = gate_form_value(policy, key)
    if value not in DELEGATED_VERDICTS:
        return None
    return next((c for c in gate_choices(key) if c.value == value), None)


def verdict_is_delegated(policy: dict) -> bool:
    """Отдан ли гейт вердикта машине — любой из них.

    Нераспознанное значение сюда не попадает и попадать не должно: слово,
    которого никто не узнал, значит «человек», а не «кто-нибудь» (#835).
    """
    if not isinstance(policy, dict):
        return False
    value = policy.get("verdict")
    return isinstance(value, str) and value in DELEGATED_VERDICTS


def review_dispatch_enabled(policy: dict) -> bool:
    """Whether the hub calls a reviewer for this project's submissions.

    Two ways to say yes, and they mean different things:

    * ``review='dispatch'`` — call the reviewer, leave the verdict to the
      human. This is the mode the hub's own project needs: the gate keeps
      its owner, but the owner finally has something to read (#804).
    * a DELEGATED verdict (``auto`` or ``steward``) — the machine decides,
      and it decides BY READING THE REPORT (#745, #1151). A project asking
      for a delegated verdict is asking for the review that feeds it, so
      this keeps dispatching for calc-kids and spike-bo without touching
      their stored policy. The steward case is not a nicety: its whole input
      is the evidence packet, and the packet's central fact is that report
      (#1074). A steward project without a dispatched review would judge
      blind and escalate everything — an expensive way to do nothing.

    Note what this function is NOT: it does not weaken the default-project
    lock (#743). That lock refuses every delegating value on the dor and
    verdict gates, and dispatching a reviewer removes no human from anywhere
    — it hands the human evidence. The two are opposite in direction.
    """
    if not isinstance(policy, dict):
        return False
    if verdict_is_delegated(policy):
        return True
    return policy.get("review") == REVIEW_DISPATCH


# Recognised values of the `release` key (#812). Default is manual, and it is
# the default on purpose: releasing takes what is in develop as a whole,
# including other sessions' work, so turning it on is a decision about the
# project rather than a convenience for whoever delivered last.
RELEASE_MANUAL = "manual"
RELEASE_AUTO = "auto"


def release_auto_enabled(policy: dict) -> bool:
    """Whether the hub carries develop into main for this project by itself.

    Anything that is not exactly 'auto' — a typo, a missing key, a value from
    a future version — reads as manual. An unreadable policy must never ship
    code, the same way it never grants approval (#743).
    """
    if not isinstance(policy, dict):
        return False
    return policy.get("release") == RELEASE_AUTO


async def release_policy_for_task(db: aiosqlite.Connection, task_id: int) -> str:
    """``auto`` or ``manual`` for the project this task belongs to."""
    policy = await gate_policy_for_task(db, task_id)
    return RELEASE_AUTO if release_auto_enabled(policy) else RELEASE_MANUAL


# Recognised key of the CI test command in ``gate_policy`` (#476). The hub
# lays a CI workflow into a provisioned repository, and that workflow reports
# acceptance-test results back — but HOW this repository runs its tests is a
# fact only the project knows. Undeclared means "use the documented default of
# the shared reporting action", never "guess a build command": a wrong guess
# turns a missing CI run into a failing one, and the delivery gate treats red
# as a blocker while it routes silence to a human.
CI_RUNNER_KEY = "ci_runner"


def ci_runner_of(project) -> str:
    """How this project's acceptance tests are run in CI; ``""`` when unsaid."""
    policy = gate_policy_of(project)
    return str(policy.get(CI_RUNNER_KEY) or "").strip()


async def risk_map_for_task(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, str] | None:
    """The project's path map for risk derivation, or None when it has none.

    None and ``{}`` mean the same thing to the derivation, but None is the
    honest word for "this project never described its paths".
    """
    policy = await gate_policy_for_task(db, task_id)
    raw = policy.get("risk_map")
    if not isinstance(raw, dict) or not raw:
        return None
    return {str(k): str(v) for k, v in raw.items()}
