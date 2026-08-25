"""Did this task's work reach production, or only the default branch (#497).

The hub records two facts now: the merge it performed (``pipeline_merges``,
#534) and the deploy CI reported (``releases``, #839 + #496). Until they were
compared, "merged" was read as "delivered" — and on 21.08.2026 that reading was
wrong in the way that costs most: task #823 sat ``completed`` with its PR
merged into develop while the deploy job was skipped, because deployment runs
from main. The only way to see it was to open GitHub's logs.

Three states, never two:

``in_prod``      the merge commit is in the history of the deployed commit.
``not_in_prod``  it is not — the work is merged and waiting for a release.
``unknown``      the question could not be answered, and the reason says why.

The third exists because the alternative is worse than useless. An
installation with no delivery facts, a project without a workspace, or a git
that would not answer are all "we do not know" — printing them as
``not_in_prod`` would turn silence into a denial, which is the same defect the
evidence blocks were cleaned of (#725) and the empty release table refuses to
commit (#839).

Computed, never stored. The answer changes with every deploy — a task that is
``not_in_prod`` at noon is ``in_prod`` after the next release without anything
about the task changing. A cached flag would be one more thing that goes stale,
which is precisely the class of defect this epic exists to remove.

One later question in this file is stored, and the difference is worth naming
(#897, bottom of the file): "is this completed task's PR still open" is asked
of GitHub, not of git, and it is asked on behalf of a list that renders on
every dashboard load. Recomputing it per render would put the whole board at
the mercy of a provider that has no opinion about how often people refresh a
page. So that one is swept on a timer and read from a table — the same trade
#883 made for deploy commits — while everything above stays computed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from hub import config
from hub import repository as repo
from hub.integrations.registry import plugins

log = logging.getLogger("hub")

IN_PROD = "in_prod"
NOT_IN_PROD = "not_in_prod"
UNKNOWN = "unknown"

# #937: the ANSWER is still computed, never stored — what is cached is the
# FACT of a failed network fetch for (workspace, sha), so a dashboard that
# renders fifty cards does not repeat the same doomed round-trip on every
# render. A deploy that later makes the sha fetchable is picked up after the
# TTL, or immediately once commit_exists starts answering True.
FETCH_MISS_TTL_SECONDS = 600.0
_FETCH_MISS_CAP = 512
_fetch_misses: dict[tuple[str, str], float] = {}


def _fetch_miss_fresh(workspace: str, sha: str) -> float | None:
    """Seconds since the cached miss, or None when there is no fresh miss."""
    at = _fetch_misses.get((workspace, sha))
    if at is None:
        return None
    age = time.monotonic() - at
    if age >= FETCH_MISS_TTL_SECONDS:
        _fetch_misses.pop((workspace, sha), None)
        return None
    return age


def _record_fetch_miss(workspace: str, sha: str) -> None:
    if len(_fetch_misses) >= _FETCH_MISS_CAP:
        now = time.monotonic()
        for key, at in list(_fetch_misses.items()):
            if now - at >= FETCH_MISS_TTL_SECONDS:
                _fetch_misses.pop(key, None)
        if len(_fetch_misses) >= _FETCH_MISS_CAP:
            _fetch_misses.clear()
    _fetch_misses[(workspace, sha)] = time.monotonic()


# #946: a squash release keeps the content and drops the ancestry, so the
# ancestry question alone answers "not deployed" about running code. The twin
# — the base-branch commit holding exactly what is deployed — is asked of git,
# not stored, for the same reason the state itself is computed: a release
# changes it, and a stored copy would go stale. Cached per deployed commit
# because a dashboard renders many cards against ONE release, and the answer
# cannot differ between them.
_TWIN_CAP = 64
_release_twins: dict[tuple[str, str, str], str] = {}


async def _release_twin(workspace: str, deployed_sha: str, base: str) -> str | None:
    """The base-branch commit whose content is what production runs (#946)."""
    key = (workspace, deployed_sha, base)
    cached = _release_twins.get(key)
    if cached is not None:
        return cached
    twin = await plugins.git_ops.commit_with_same_tree(workspace, deployed_sha, base)
    # Only an ANSWER is cached. A failure to look may be transient (a fetch
    # away, a branch not yet in this clone), and caching it would freeze "we
    # could not tell" until the process restarts.
    if twin is None:
        return None
    if len(_release_twins) >= _TWIN_CAP:
        _release_twins.clear()
    _release_twins[key] = twin
    return twin


def _answer(state: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "state": state,
        "reason": reason,
        "merge_sha": "",
        "deployed_sha": "",
        "deployed_at": "",
        **extra,
    }


async def ensure_commit_available(
    db: Any, sha: str, ref: str = "", *, project_id: int | None = None
) -> bool:
    """Make sure the workspace carries ``sha``, fetching once if it does not.

    Called where a deploy is RECORDED (#883): deploys happen a few times a day,
    cards are read constantly, and paying for the network on every render to
    learn something that changes once per release is the wrong trade. Returns
    whether the commit is available afterwards; the caller decides what an
    unavailable one means — here it never means "not deployed".
    """
    sha = (sha or "").strip()
    if not sha:
        return False
    # The workspace comes from the PROJECT, not from a task: a deploy callback
    # names no task. project_git_context resolves through a task id and would
    # answer an empty context here, which would have made this whole path a
    # no-op — caught before the first test, and worth stating so the next
    # reader does not reintroduce it.
    try:
        row = (
            await repo.get_project(db, project_id)
            if project_id
            else await repo.get_project_by_slug(db, "default")
        )
    except Exception as exc:  # noqa: BLE001 - best effort by contract
        log.warning("pre-fetch of %s: no project row: %s", sha[:12], exc)
        return False
    workspace = (dict(row).get("workspace_path") or "").strip() if row else ""
    if not workspace:
        return False
    if await plugins.git_ops.commit_exists(workspace, sha):
        return True
    fetched, error = await plugins.git_ops.fetch_commit(workspace, sha, ref)
    if not fetched:
        log.warning("pre-fetch of %s failed: %s", sha[:12], error)
    return fetched


async def delivery_state(db: Any, task_id: int) -> dict[str, Any]:
    """Whether this task's merge is part of what production is running."""
    from hub import services

    merge_sha = await repo.merge_sha_for_task(db, task_id)
    if not merge_sha:
        return _answer(
            UNKNOWN,
            "хаб не мержил эту задачу — сверять с выкатом нечего. "
            "Это не значит, что работа не доехала: значит, что факта мержа у хаба нет",
        )

    release = await repo.latest_successful_release(db)
    if release is None:
        return _answer(
            UNKNOWN,
            "хаб не знает, что сейчас раскатано: успешных выкатов не записано. "
            "Пустая история выкатов — незнание, а не отрицание",
            merge_sha=merge_sha,
        )

    deployed_sha = str(release.get("deployed_sha") or "")
    deployed_at = str(release.get("deployed_at") or "")
    known = {
        "merge_sha": merge_sha,
        "deployed_sha": deployed_sha,
        "deployed_at": deployed_at,
    }

    try:
        ctx = await services.project_git_context(db, task_id)
        workspace = ctx.get("repo")
    except Exception as exc:  # noqa: BLE001 - a card must render regardless
        log.warning("delivery state for #%s: no project context: %s", task_id, exc)
        return _answer(UNKNOWN, f"рабочую копию определить не удалось: {exc}", **known)
    if not workspace:
        return _answer(
            UNKNOWN,
            "у проекта нет рабочей копии на этом хосте — достижимость коммита "
            "проверить негде",
            **known,
        )

    # #937: the release the hub records is a deploy of the HUB's repository.
    # For a task whose project lives in a different repo, that sha cannot
    # exist in the project's workspace — asking git (and then the network)
    # was a guaranteed miss paid on every dashboard render.
    project_repo = (ctx.get("gh_repo") or "").strip().lower()
    hub_repo = (config.REPO_NAME or "").strip().lower()
    if project_repo and hub_repo and project_repo != hub_repo:
        return _answer(
            UNKNOWN,
            f"релиз хаба ({hub_repo}) не применим к проекту в {project_repo} — "
            "у этого проекта свой репозиторий, факт его выката хаб не записывает",
            **known,
        )

    # #883: the objects have to be here before git can be asked about them.
    # The workspace tracks the base branch, so a commit deployed from another
    # ref is simply absent — and that absence was answering "could not check"
    # for every task on this installation. Checked first, and only fetched
    # when missing: a present commit must cost no network at all.
    if await plugins.git_ops.commit_exists(workspace, deployed_sha) is False:
        miss_age = _fetch_miss_fresh(workspace, deployed_sha)
        if miss_age is not None:
            return _answer(
                UNKNOWN,
                f"коммита {deployed_sha[:12]} нет в рабочей копии; недавний "
                f"промах fetch ({int(miss_age)}с назад) — повторная попытка "
                "отложена. Это не «не раскатано»",
                **known,
            )
        fetched, fetch_error = await plugins.git_ops.fetch_commit(
            workspace, deployed_sha, str(release.get("ref") or "")
        )
        if not fetched:
            _record_fetch_miss(workspace, deployed_sha)
            return _answer(
                UNKNOWN,
                f"коммита {deployed_sha[:12]} нет в рабочей копии, и подтянуть "
                f"его не удалось: {fetch_error or 'причина не названа'}. "
                "Это не «не раскатано»",
                **known,
            )

    reachable = await plugins.git_ops.is_ancestor(workspace, merge_sha, deployed_sha)
    if reachable is None:
        return _answer(
            UNKNOWN,
            f"git не смог ответить, входит ли {merge_sha[:12]} в историю "
            f"{deployed_sha[:12]} — возможно, коммитов нет в этой копии. "
            "Это не «не раскатано»",
            **known,
        )
    if reachable:
        return _answer(
            IN_PROD,
            f"мерж {merge_sha[:12]} входит в раскатанный {deployed_sha[:12]}"
            + (f" от {deployed_at}" if deployed_at else ""),
            **known,
        )

    # Not an ancestor is not yet an answer (#946). A release merged by squash
    # writes a NEW commit on the release branch, so nothing merged into the
    # base branch is ever an ancestor of it — including work that is provably
    # running. Ask git the question that survives a squash: which state of the
    # base branch holds exactly what is deployed, and does the merge belong to
    # it. Observed on prod 24.08.2026 on the first policy-made release (#927).
    base = (ctx.get("base_branch") or "").strip() or config.PAIR_BASE_BRANCH
    twin = await _release_twin(workspace, deployed_sha, base)
    if twin is None:
        return _answer(
            UNKNOWN,
            f"git не смог сказать, какое состояние {base} раскатано в "
            f"{deployed_sha[:12]} — сверить squash-релиз не с чем. "
            "Это не «не раскатано»",
            **known,
        )
    if twin:
        carried = await plugins.git_ops.is_ancestor(workspace, merge_sha, twin)
        if carried is None:
            return _answer(
                UNKNOWN,
                f"git не смог ответить, входит ли {merge_sha[:12]} в "
                f"раскатанное состояние {twin[:12]}. Это не «не раскатано»",
                **known,
            )
        if carried:
            return _answer(
                IN_PROD,
                f"релиз собран squash-ом, поэтому {merge_sha[:12]} не предок "
                f"{deployed_sha[:12]}; раскатано содержимое {base} на "
                f"{twin[:12]}, и мерж в него входит"
                + (f" (выкат {deployed_at})" if deployed_at else ""),
                **known,
            )
    # #950: ancestry is cut twice in this flow — the release squashes, and the
    # base branch can be recreated from the release branch afterwards. The
    # twin above survives the first cut but not the second: a merge left on
    # the abandoned line belongs to no reachable history at all, and the
    # answer below would say "waiting for a release" about running code (the
    # exact refusal #949's live check hit, update #3496). The release itself
    # recorded which merges it carried; that stamp lives on the RELEASE
    # branch's own line, which nothing rewrites.
    fact = await repo.release_fact_for_task(db, task_id)
    if fact and str(fact.get("released_sha") or ""):
        release_sha = str(fact["released_sha"])
        release_pr = fact.get("released_pr")
        carried_out = await plugins.git_ops.is_ancestor(
            workspace, release_sha, deployed_sha
        )
        if carried_out is None:
            return _answer(
                UNKNOWN,
                f"мерж увезён релизом PR #{release_pr} ({release_sha[:12]}), но "
                f"git не смог ответить, входит ли этот релиз в раскатанный "
                f"{deployed_sha[:12]}. Это не «не раскатано»",
                **known,
            )
        if carried_out or release_sha == deployed_sha:
            return _answer(
                IN_PROD,
                f"родословная мержа {merge_sha[:12]} оборвана, но релиз "
                f"PR #{release_pr} записал, что увёз его: {release_sha[:12]} "
                f"входит в раскатанный {deployed_sha[:12]}"
                + (f" (выкат {deployed_at})" if deployed_at else ""),
                **known,
            )
        return _answer(
            NOT_IN_PROD,
            f"мерж увезён релизом PR #{release_pr} ({release_sha[:12]}), но "
            f"этот релиз ещё не раскатан: в проде {deployed_sha[:12]}",
            **known,
        )
    return _answer(
        NOT_IN_PROD,
        f"мерж {merge_sha[:12]} не входит в раскатанный {deployed_sha[:12]}: "
        "работа смёржена, но ждёт релиза",
        **known,
    )


async def merged_into_base(db: Any, task_row: dict[str, Any]) -> bool | None:
    """Is this task's submitted commit already in its project's base branch (#885)?

    True — yes, wherever the merge came from. False — looked and it is not.
    None — could not look, which is NOT the same as "not delivered" (#725).

    Exists because delivery was read from ``pipeline_merges`` alone — merges
    the hub performed itself (#534). A merge made outside the gate leaves no
    row there, so the blocker read as undelivered while its code sat in the
    base branch. On 21.08.2026 the edge #830 → #818 said exactly that, an hour
    after #818 was merged. A warning that is wrong in the obvious case teaches
    the reader to skip the line, and then it is silent in the case that
    mattered.

    Cheap by construction: callers ask ONLY for blockers with no pipeline
    merge, so in the normal path this runs zero git commands.
    """
    sha = (task_row.get("submission_sha") or "").strip()
    if not sha:
        return None
    try:
        from hub import services

        ctx = await services.project_git_context(db, task_row["id"])
        workspace = (ctx.get("repo") or "").strip()
        base = (ctx.get("base_branch") or "").strip() or config.PAIR_BASE_BRANCH
    except Exception as exc:  # noqa: BLE001 - advisory path, never fatal
        log.warning("delivery check for #%s: no git context: %s", task_row["id"], exc)
        return None
    if not workspace:
        return None
    # origin/<base> rather than <base>: the shared clone sits on the base
    # branch but may be behind, and the question is about what has landed
    # upstream, not about this checkout.
    return await plugins.git_ops.is_ancestor(workspace, sha, f"origin/{base}")


async def blocker_delivery(db: Any, blocker: dict[str, Any]) -> dict[str, Any]:
    """Fill in ``delivered``/``reason`` for one blocker row (#885).

    The gate's own merges answer first and cost nothing. Only when there is
    none does the base branch get asked — and its answer is kept distinct:
    delivered outside the gate clears the block, and says so, because manual
    merges into the base branch are against the rules here and the drift guard
    (#534) has its own opinion about them. Hiding that would trade one silent
    wrong answer for another.
    """
    if blocker.get("delivered"):
        return {**blocker, "delivery_path": "gate"}
    row = await repo.get_task(db, blocker["task_id"])
    task = dict(row) if row is not None else {}
    reached = await merged_into_base(db, task) if task else None
    # A blocker that never pinned a commit has nothing to look for, so the
    # second source staying silent is not news — saying "could not check"
    # there would add noise to a reason that is already complete.
    had_something_to_check = bool((task.get("submission_sha") or "").strip())
    if reached is True:
        return {
            **blocker,
            "delivered": True,
            "delivery_path": "outside_gate",
            "reason": "код в базовой ветке, но мерж прошёл мимо гейта",
        }
    if reached is None and had_something_to_check and blocker.get("reason"):
        # Keep the original reason and say the second source stayed silent —
        # "could not look" must not read as "looked and it is not there".
        return {
            **blocker,
            "delivery_path": "unknown",
            "reason": f"{blocker['reason']}; проверить базовую ветку не удалось",
        }
    return {**blocker, "delivery_path": "none"}


async def with_delivery(
    db: Any, blockers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The same enrichment for every reader (#885) — the gate (#484), the task
    context (#485), REST (#486) and MCP (#487) must not answer differently."""
    return [await blocker_delivery(db, b) for b in blockers]


# --- Completed, but where is the work? (#897) -------------------------------
#
# 21.08.2026: #878 and #885 were ``completed`` for two hours with their code
# outside develop. Nothing lied. The gate refused to merge because CI was still
# running and said so; the human took the exit the refusal offered and accepted
# the task; CI went green minutes later and by then the PR had no owner —
# the gate delivers on a done report, and a completed task never files one
# again. Two right behaviours, and the work fell through the seam between them.
#
# What is added here is the missing fact, not a new rule: after this, a
# completed task whose PR is still open is a row somebody can read, and manual
# acceptance says out loud what it is leaving behind. Accepting by hand stays
# allowed in every form — it is the owner's way out, sometimes because the work
# is deliberately cancelled.

DELIVERED = "delivered"
PR_OPEN = "pr_open"
PR_CLOSED = "pr_closed"
# UNKNOWN, defined above for #497, is reused verbatim: one vocabulary for
# "could not look", so a reader does not have to learn a second one.

#: What the owner said should happen to the PR when they accepted by hand.
#: Empty is a real value — "not stated" — and never reads as either choice.
DISPOSITION_DELIVER = "deliver"
DISPOSITION_ABANDON = "abandon"
DISPOSITIONS = (DISPOSITION_DELIVER, DISPOSITION_ABANDON)

_DISPOSITION_TEXT = {
    DISPOSITION_DELIVER: "Владелец сказал: работу довезти, PR остаётся к мержу",
    DISPOSITION_ABANDON: "Владелец сказал: работа отменена, PR закрыть",
    "": "Судьба PR не выбрана",
}


def _task_answer(
    state: str, reason: str, *, pr_number: int | None, delivery_path: str
) -> dict[str, Any]:
    return {
        "state": state,
        "reason": reason,
        "pr_number": pr_number,
        "delivery_path": delivery_path,
    }


async def task_delivery(db: Any, task: dict[str, Any]) -> dict[str, Any]:
    """Did this task's work reach the base branch — and if not, why not?

    Four named answers, three of which are the #546/#572 triad applied to one
    task: ``delivered`` (yes), ``pr_open`` (no, and the PR is still standing),
    ``unknown`` (could not look, with the cause). ``pr_closed`` is the fourth
    because it is neither: a PR closed without a merge is work dropped on
    purpose, and calling that ``delivered`` would be a lie while calling it
    ``pr_open`` would raise an alarm about a decision already taken. What is
    NOT done here is collapsing ignorance into either definite answer — that is
    the line this codebase has now drawn three times.

    The delivery fact is computed exactly the way #885 computes it for a
    blocker, through the same functions: the gate's own merge first (free),
    then the base branch, and only then the provider. There is no second source
    of truth about delivery in this file or anywhere else.

    ``delivery_path`` keeps #885's vocabulary — gate | outside_gate | unknown |
    none — so the two readers describe the same world with the same words.

    Not a rival to ``prod_state`` (#499), which asks a later question: whether
    a MERGED task is in the deployed commit. A task whose PR never merged has
    no merge sha, so it lands in that snapshot's ``unknown`` bucket alongside
    everything else the hub did not merge — correct there, and useless for
    finding the case here. Both read the same facts through this module; they
    differ in what they ask of them, which is why neither is a second source.
    """
    task_id = int(task["id"])
    pr_raw = task.get("pr_number")
    pr_number = int(pr_raw) if pr_raw is not None else None

    if await repo.merge_sha_for_task(db, task_id):
        return _task_answer(
            DELIVERED,
            "хаб смержил эту задачу сам — работа в базовой ветке",
            pr_number=pr_number,
            delivery_path="gate",
        )

    reached = await merged_into_base(db, task)
    if reached is True:
        return _task_answer(
            DELIVERED,
            "код в базовой ветке, но мерж прошёл мимо гейта",
            pr_number=pr_number,
            delivery_path="outside_gate",
        )

    if pr_number is None:
        # Assumption stated on the task: a completed task with no pinned PR is
        # indistinguishable here from work that never needed one. Saying so is
        # the honest answer; guessing would put research and spikes on a list
        # about undelivered pull requests.
        return _task_answer(
            UNKNOWN,
            "у задачи не закреплён PR — по нему сверять нечего. "
            "Это не «не доставлено»: работы без ветки здесь не отличить",
            pr_number=None,
            delivery_path="none",
        )

    base_note = "" if reached is False else "; базовую ветку проверить не удалось"
    workspace, gh_repo = "", ""
    try:
        from hub import services

        ctx = await services.project_git_context(db, task_id)
        workspace = (ctx.get("repo") or "").strip()
        gh_repo = (ctx.get("gh_repo") or "").strip()
    except Exception as exc:  # noqa: BLE001 - a refusal to answer, never a 500
        log.warning("delivery check for #%s: no git context: %s", task_id, exc)
        return _task_answer(
            UNKNOWN,
            f"рабочую копию проекта определить не удалось: {exc}. "
            f"Состояние PR #{pr_number} не спрашивали{base_note}",
            pr_number=pr_number,
            delivery_path="unknown",
        )

    try:
        state = (
            (
                await plugins.git_ops.pr_state(
                    pr_number, repo=workspace or None, gh_repo=gh_repo or None
                )
                or ""
            )
            .strip()
            .lower()
        )
    except Exception as exc:  # noqa: BLE001 - the provider blinking is not a verdict
        log.warning("pr_state for #%s (PR #%s): %s", task_id, pr_number, exc)
        state = ""

    if state == "merged":
        return _task_answer(
            DELIVERED,
            f"PR #{pr_number} смержен, но записи о мерже у хаба нет — "
            "доставка прошла мимо гейта",
            pr_number=pr_number,
            delivery_path="outside_gate",
        )
    if state == "open":
        return _task_answer(
            PR_OPEN,
            f"PR #{pr_number} открыт и не смержен — работа не в базовой ветке",
            pr_number=pr_number,
            delivery_path="none",
        )
    if state == "closed":
        return _task_answer(
            PR_CLOSED,
            f"PR #{pr_number} закрыт без мержа — работу свернули намеренно",
            pr_number=pr_number,
            delivery_path="none",
        )
    return _task_answer(
        UNKNOWN,
        f"состояние PR #{pr_number} узнать не удалось: провайдер не ответил. "
        f"Это не «доставлено» и не «не доставлено»{base_note}",
        pr_number=pr_number,
        delivery_path="unknown",
    )


def _acceptance_note(answer: dict[str, Any], disposition: str) -> str:
    """The sentence a manual acceptance leaves behind instead of silence."""
    intent = _DISPOSITION_TEXT.get(disposition, _DISPOSITION_TEXT[""])
    if answer["state"] == PR_OPEN:
        head = f"Задача принята вручную, но работа НЕ доставлена: {answer['reason']}."
    elif answer["state"] == UNKNOWN:
        head = (
            f"Задача принята вручную, доставку подтвердить не удалось: "
            f"{answer['reason']}."
        )
    else:
        head = f"Задача принята вручную: {answer['reason']}."
    # Not ``.capitalize()``: it lowercases the rest of the string, and "PR"
    # became "pr" in the one sentence whose job is to name a pull request.
    return (
        f"{head} {intent}. "
        "Принятие руками — законный выход, и оно ничего не отменяет: "
        "запись оставлена, чтобы работа не потерялась молча (#897)."
    )


async def note_completion_without_delivery(
    db: Any,
    task_id: int,
    *,
    via: str,
    actor: str = "human",
    disposition: str = "",
) -> dict[str, Any] | None:
    """Say out loud what a completion outside the delivery gate left behind.

    Called from every path that turns a task ``completed`` without the gate
    having merged anything — human acceptance after arbitration, the human
    force-complete override, and the unreviewed ``pending_report`` done report.
    The gate's own path needs nothing: it completes only after merging.

    Never raises and never blocks: a completion that already happened must not
    be undone because GitHub was slow, and manual acceptance stays available in
    every form. Returns the answer it recorded, or ``None`` if it could not
    look at the task at all.
    """
    try:
        row = await repo.get_task(db, task_id)
        if row is None:
            return None
        task = dict(row)
        if not task.get("pr_number"):
            # No pinned PR: #498's warning already covers "work that never
            # started delivering", and repeating it here would put two
            # different findings under one name.
            return None
        answer = await task_delivery(db, task)
        disposition = disposition if disposition in DISPOSITIONS else ""
        await repo.record_delivery_discrepancy(
            db,
            task_id=task_id,
            state=answer["state"],
            reason=answer["reason"],
            pr_number=answer["pr_number"],
            delivery_path=answer["delivery_path"],
            disposition=disposition,
            accepted_via=via,
            # The alert below IS this state's alert, so the sweep must not
            # repeat it. Delivered rows carry no alert to suppress.
            alerted_state=(answer["state"] if answer["state"] != DELIVERED else ""),
        )
        if answer["state"] != DELIVERED:
            await repo.add_task_update(
                db, task_id, "hub", "alert", _acceptance_note(answer, disposition)
            )
            await repo.insert_event(
                db,
                kind="completed_without_delivery",
                task_id=task_id,
                actor=actor,
                payload={
                    "via": via,
                    "state": answer["state"],
                    "pr": answer["pr_number"],
                    "disposition": disposition,
                },
            )
            await db.commit()
        return answer
    except Exception:  # noqa: BLE001 - a note about a completion, not a gate
        log.exception("could not record delivery state for completed #%s", task_id)
        return None


async def scan_completed_deliveries(
    db: Any, *, lookback_days: int = 30, limit: int = 100
) -> list[dict[str, Any]]:
    """Periodic reconciliation of "completed" against "the PR is still open".

    Modelled on the stale-alert loop in ``hub/poller.py``: run on a timer, look
    only at rows that can still be news, and alert at most once per state so
    the owner is told something new rather than reminded every half minute.
    Repeats are damped by ``alerted_state`` on the stored row rather than by
    matching alert text — durable, and it survives an unrelated update landing
    on the task, which the text heuristic does not.

    This is the only place that pays a provider call for this question. Every
    reader — the inbox, REST, MCP — reads the rows it writes, which is what
    keeps "показать расхождение" from costing a network round trip per card.
    """
    candidates = await repo.completed_tasks_awaiting_delivery(
        db, lookback_days=lookback_days, limit=limit
    )
    found: list[dict[str, Any]] = []
    for row in candidates:
        task = dict(row)
        task_id = int(task["id"])
        try:
            answer = await task_delivery(db, task)
            prior = await repo.get_delivery_discrepancy(db, task_id)
            already = (prior or {}).get("alerted_state") or ""
            should_alert = answer["state"] in (PR_OPEN, UNKNOWN) and (
                already != answer["state"]
            )
            await repo.record_delivery_discrepancy(
                db,
                task_id=task_id,
                state=answer["state"],
                reason=answer["reason"],
                pr_number=answer["pr_number"],
                delivery_path=answer["delivery_path"],
                alerted_state=(answer["state"] if should_alert else None),
            )
            if should_alert:
                await repo.add_task_update(
                    db,
                    task_id,
                    "hub",
                    "alert",
                    f"Задача числится completed, но работа не доставлена: "
                    f"{answer['reason']}. Расхождение видно в списке "
                    "недоставленных завершённых задач (#897).",
                )
                await db.commit()
            if answer["state"] == PR_OPEN:
                found.append({"task_id": task_id, **answer})
        except Exception:  # noqa: BLE001 - one bad row must not stop the sweep
            log.exception("delivery sweep failed for #%s", task_id)
    return found


async def undelivered_completed_tasks(
    db: Any, *, project_id: int | None = None, limit: int = 50
) -> dict[str, Any]:
    """The discrepancy list as the owner reads it — stored answers only.

    ``unknown`` rows travel beside the list, never inside it: a question the
    hub could not ask is not a task somebody forgot to deliver, and mixing the
    two is how a list stops being believed.
    """
    open_rows = await repo.list_delivery_discrepancies(
        db, states=(PR_OPEN,), project_id=project_id, limit=limit
    )
    unknown_rows = await repo.list_delivery_discrepancies(
        db, states=(UNKNOWN,), project_id=project_id, limit=limit
    )
    return {"undelivered": open_rows, "unknown": unknown_rows}
