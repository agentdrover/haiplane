"""One assembly of a task's review evidence, for both of its readers (#823).

The evidence blocks — AC test results (#507), locator resolution (#506), the
CI run against the pinned sha (#546), statement freshness (#615), call sites
(#601), the live check (#814) and the ``evidence_coverage`` verdict over all
of them (#725) — were computed inside the ``/api/tasks/{id}/review-brief``
route body, so only the reviewing AGENT could read them. The human at the
verdict gate saw the reviewer's report (#808) and the submission prose, and
nothing about whether the ordered criteria were actually checked.

Moving the assembly here makes the card and the brief share one builder: the
two readers agree by construction rather than by keeping two call sites in
step. That is the same move #808 made for ``review_report``, and #814 did not
get to make — it reached the card through its own separate query.

Everything here is best-effort by contract, inherited unchanged from the route:
a block that could not be computed answers with its CAUSE, never with silence
or a zero. A silent block reads as "checked, nothing found", which is the
half-truth #549 and #725 were written to remove.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from hub import commit_scope, config
from hub import repository as repo
from hub.services.outcomes import outcome_status_for_task
from hub.integrations.registry import plugins
from hub.models import (
    ACLocatorResolution,
    ACTestResultView,
    CallSiteEntry,
    CallSiteSection,
    CIRunReportState,
    DiffBaseState,
    EvidenceCoverage,
    LiveCheckState,
    MachineReviewView,
    ReviewBrief,
    SelfReviewWarning,
    TaskProjectRef,
)
from hub.services import call_sites, review_evidence
from hub.services.ac_tests import current_ac_test_results
from hub.services.ci_report import ci_report_state
from hub.services.statement_freshness import statement_freshness
from hub.services.test_existence import collect_test_nodeids, resolve_ac_locators

log = logging.getLogger("hub")

# The statuses whose card pays for the evidence assembly. A verdict is only
# ever cast in these two, and the assembly costs git work (one branch-tip
# resolution, one diff read) that a draft's card has no reason to spend.
GATE_STATUSES = ("review", "fix_requested")


async def _locator_sources(
    db, task_id: int, task_view, ctx: dict, ac_models: list
) -> tuple[dict[str, str | None], set[str]]:
    """Locator files as of the submitted commit: ``(texts, absent)`` (#764).

    ``texts`` maps path to file content, with ``None`` where it could not be
    read; ``absent`` names the files the commit demonstrably does not contain.
    The two are kept apart because they mean opposite things to a reviewer —
    "the submission never added this file" is a finding, "I could not look" is
    not — and #506's rule is that the second must never be dressed as the
    first. The ref is the pinned submission sha when there is one, so the
    answer is about the code being judged rather than wherever the branch has
    since moved.
    """
    from hub.services.test_existence import locator_files

    files = locator_files(ac_models)
    if not files:
        return {}, set()
    repo = ctx.get("repo")
    ref = (task_view.submission_sha or "").strip() or (task_view.branch or "").strip()
    if not repo or not ref:
        return dict.fromkeys(files), set()
    try:
        in_tree = await plugins.git_ops.files_at_ref(repo, ref)
    except Exception:  # noqa: BLE001 - the brief must assemble regardless
        log.warning("locator tree listing failed for task #%s", task_id)
        in_tree = None
    absent = {p for p in files if in_tree is not None and p not in in_tree}
    wanted = [p for p in files if p not in absent]
    reads = await asyncio.gather(
        *(plugins.git_ops.file_at_ref(repo, ref, path) for path in wanted),
        return_exceptions=True,
    )
    sources: dict[str, str | None] = dict.fromkeys(files)
    for path, text in zip(wanted, reads, strict=True):
        if isinstance(text, BaseException):
            log.warning("locator source read failed for task #%s: %s", task_id, path)
            sources[path] = None
        else:
            sources[path] = text
    return sources, absent


async def build_call_sites_section(
    db, task_id: int, task_view, diff_base: dict | None = None
) -> CallSiteSection:
    """The call-site enumeration for a task's branch (#601).

    Best effort by contract: every failure answers ``unknown`` with a reason
    rather than an empty section. An empty section would say "no other call
    sites exist", which is exactly the false reassurance this was written to
    remove.

    #725: when the diff base itself did not resolve, the reason says so in
    those words. This section used to report "the diff named no changed lines"
    over a branch with 67 changed files, because the base it was diffed against
    did not exist — an unknown that reads as an independent finding when it is
    a consequence of one failure stated elsewhere.
    """
    from hub import services

    branch = (task_view.branch or "").strip()
    if not branch:
        return CallSiteSection(status=call_sites.UNKNOWN, reason="task has no branch")

    diff_base = diff_base or {}
    if review_evidence.base_blocks_diff(str(diff_base.get("state") or "")):
        return CallSiteSection(
            status=call_sites.UNKNOWN,
            reason=review_evidence.DISABLED_BY_BASE + str(diff_base.get("reason", "")),
        )

    try:
        ctx = await services.project_git_context(db, task_id)
        workspace = ctx.get("repo")
        base = (
            diff_base.get("base") or ctx.get("base_branch") or config.PAIR_BASE_BRANCH
        )
        if not workspace:
            return CallSiteSection(
                status=call_sites.UNKNOWN, reason="project has no workspace"
            )
        diff = await plugins.git_ops.branch_diff(workspace, base, branch)
        if diff is None:
            return CallSiteSection(
                status=call_sites.UNKNOWN,
                reason=f"could not read the diff of {branch} against {base}",
            )
        report = await asyncio.to_thread(call_sites.analyse, workspace, diff)
    except Exception as exc:  # noqa: BLE001 - advisory section, never fatal
        log.warning("call-site section failed for task #%s: %s", task_id, exc)
        return CallSiteSection(status=call_sites.UNKNOWN, reason=f"failed: {exc}")

    return CallSiteSection(
        status=report.status,
        reason=report.reason,
        summary=report.summary(),
        note=report.note,
        unparsed=report.unparsed,
        entries=[
            CallSiteEntry(
                symbol=s.symbol,
                defined_in=s.defined_in,
                state=s.state,
                statement=s.statement(),
                total_sites=len(s.sites),
                untouched=[
                    f"{site.file}:{site.line} ({site.caller})"
                    for site in s.sites
                    if not site.touched
                ],
            )
            for s in report.symbols
        ],
    )


async def build_review_brief(
    db, task_id: int, *, self_review_warning: SelfReviewWarning | None = None
) -> ReviewBrief | None:
    """Assemble the whole review brief for ``task_id`` (#308, #823).

    Returns ``None`` when the task does not exist, so the caller decides
    whether that is a 404 or an empty panel.

    ``self_review_warning`` (#433) is passed in rather than computed here: it
    is a statement about the CALLER, and this builder is deliberately blind to
    identity so that both readers get the same evidence.
    """
    from hub import services

    row = await repo.get_task(db, task_id)
    if not row:
        return None
    task_row = dict(row)
    task_view = services.row_to_task(row)
    project_row = await repo.resolve_project_for_task(db, task_id)
    if project_row is not None:
        task_view.project = TaskProjectRef(
            id=project_row["id"], slug=project_row["slug"]
        )
    ac_rows = await repo.list_acceptance_criteria(db, task_id)

    # Latest submission context: the most recent done report, falling back
    # to the most recent status update when the task has not reported yet.
    latest_submission_summary = ""
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    for kind in ("done", "status"):
        for u in reversed(updates):
            if u.get("kind") == kind:
                latest_submission_summary = u.get("content", "")
                break
        if latest_submission_summary:
            break

    # #725: the base comes from the project (or the PR), and is resolved in the
    # project workspace before the command is printed. A hardcoded "develop"
    # produced an uncomputable diff on every project whose base is named
    # differently, and the blocks that read the diff then reported bare
    # unknowns as if each had looked and found nothing.
    diff_base = await review_evidence.resolve_diff_base(
        db, task_id, task_view.branch or ""
    )
    diff_command = review_evidence.diff_command_for(diff_base, task_view.branch or "")

    machine_review = None
    mr_row = await repo.get_latest_machine_review(db, task_id)
    if mr_row is not None:
        machine_review = MachineReviewView(**dict(mr_row))
        machine_review.is_current = machine_review.submission_generation == (
            task_view.submission_generation or 0
        )
        # #876: what the gate said these findings turned out to be. The same
        # filler the card uses — the brief shows a later reviewer what was
        # already judged, and the two readers cannot disagree.
        await review_evidence.attach_dispositions(db, machine_review)

    # Advisory branch-stacking check (#438): the reviewer should know when
    # the diff includes another task's unmerged work. Best-effort — no repo
    # access means no warning, never an error.
    stacking_warning = ""
    if task_view.branch:
        stacking = await services.detect_branch_stacking(db, task_id, task_view.branch)
        if stacking:
            stacking_warning = stacking["message"]

    # #506: resolve each verifiable_by=test AC's locator to a real test via
    # pytest collect-only (best-effort). Only pays the collection cost when the
    # brief actually has test-AC to check.
    ac_models = [services.row_to_ac(r) for r in ac_rows]
    locator_resolution: list[ACLocatorResolution] = []
    if any(a.verifiable_by.value == "test" for a in ac_models):
        ctx = await services.project_git_context(db, task_id)
        # #764: look where the task's code actually is. Before worktree-per-task
        # (#459) the shared clone was the only candidate; with it on, the clone
        # sits on the base branch for every task, so the HEAD guard below never
        # matched and collection was never attempted — which is why every brief
        # said "test collection unavailable" while pytest ran fine in the
        # worktree three metres away, and why the log held not one line about a
        # collection that failed. It never got that far.
        from hub.services.orchestration import live_pair_worktree_info

        _mode, worktree = await live_pair_worktree_info(db, task_id)
        # "" when the tree is gone: submit asks for its removal and the
        # retirement sweep (#1033) clears the rest, so a path that no longer
        # exists is never read as the task's code. Whether the tree survives
        # to review time is not something this code should assume either way —
        # today most do (205 stand on production, because the removal at
        # submit raises and the exception is swallowed), and the fallback
        # below is what makes that difference not matter.
        workspace = worktree or ctx.get("repo")
        # #506: a working tree can hold another task's branch — the shared
        # clone is shared, and the pair flow switches it. Collecting there
        # would report THIS task's tests as missing. Only trust collection
        # when HEAD matches the task's branch.
        collected = None
        if task_view.branch:
            head = await plugins.git_ops.current_branch(repo=workspace)
            if head == task_view.branch:
                collected = await collect_test_nodeids(workspace)
        # #764: the fallback, for every case where collection could not answer
        # — the project's dependencies are not installed, the tree was retired,
        # or no tree ever held this branch. The files are read out of the
        # submitted commit itself: git hands over the text without a checkout,
        # ast answers "is this test written here" without imports, and neither
        # needs the project's dependencies. Weaker evidence than collection,
        # which is why the reason says which one answered.
        sources: dict[str, str | None] | None = None
        absent: set[str] = set()
        if collected is None:
            sources, absent = await _locator_sources(
                db, task_id, task_view, ctx, ac_models
            )
        locator_resolution = [
            ACLocatorResolution(**r)
            for r in resolve_ac_locators(ac_models, collected, sources, absent)
        ]

    # #572: does the branch still stand where the submission pinned it? Three
    # states, never collapsed — the reviewer must see "could not look" as
    # itself, not as "nothing moved". Costs one fetch, and only when there is
    # a pinned submission to compare against.
    submission_sha = (task_view.submission_sha or "").strip()
    current_tip = ""
    sha_check = "unknown"
    sha_check_reason = "branch tip was not pinned at submission"
    if submission_sha and task_view.branch:
        current_tip, tip_reason = await services.resolve_branch_tip(
            db, task_id, task_view.branch
        )
        if not current_tip:
            sha_check_reason = tip_reason
        elif current_tip == submission_sha:
            sha_check = "match"
            # #725: never a bare green word. Beside blocks that produced no
            # signal, "match" with an empty reason was read as verification,
            # while this check only knows where a branch pointer stands.
            sha_check_reason = review_evidence.sha_check_statement(
                sha_check, submission_sha, current_tip, task_view.branch or ""
            )
        else:
            sha_check = "diverged"
            sha_check_reason = (
                f"submitted at {submission_sha[:12]}, branch now at "
                f"{current_tip[:12]} — the diff under review is not the code "
                "in the branch"
            )

    # #601: where else is each changed symbol called, and does this diff touch
    # those places. Same shape as #506 above and for the same reason: the
    # analysis needs the checkout, so it runs against the project workspace and
    # answers `unknown` with a reason when that is not available. Silence here
    # would read as "no other call sites", which is the very mistake the
    # section exists to catch.
    call_sites_section = await build_call_sites_section(
        db, task_id, task_view, diff_base
    )

    # #507: recorded pass/fail of each test-AC for the current generation.
    ac_result_rows = await repo.list_ac_test_results(db, task_id)
    ac_test_results = [
        ACTestResultView(**r)
        for r in current_ac_test_results(
            ac_result_rows, task_view.submission_generation or 0
        )
    ]

    # #546: is there run evidence for the COMMIT under review? Two states only,
    # and the unknown one always carries its cause — a reviewer must be able to
    # tell "nobody ran it" from "it ran and failed".
    ci_state, ci_reason = await ci_report_state(
        db,
        {
            "id": task_id,
            "submission_sha": task_view.submission_sha,
        },
    )
    ci_run_report = CIRunReportState(
        state=ci_state,
        reason=ci_reason,
        head_sha=task_view.submission_sha or "",
    )

    # #875: WHICH checks ran, not just whether a run was reported. "A run
    # exists" and "ruff found nothing" are different facts, and only the second
    # can buy the reviewer's silence on a class.
    prepass = await review_evidence.prepass_state(
        db, {"id": task_id, "submission_sha": task_view.submission_sha}
    )

    # #615: the statement the reviewer is judging may predate the work that
    # invalidated it. Same computation as pair-start, one source.
    freshness = await statement_freshness(db, task_row)

    # #814: the newest live-check evidence, against the commit that shipped.
    # The delivered sha comes from the merge the gate recorded — the same
    # answer the evidence itself defaults to, so brief and record agree.
    delivered_sha = await repo.merge_sha_for_task(db, task_id)
    live_check = await review_evidence.live_check_state(
        db, task_id, delivered_sha=delivered_sha
    )

    # #725: one verdict over every evidence block, in the same place the green
    # words are. Four blocks silent, one reassuring wrongly and one narrow-but-
    # green read as six independent findings across a day of briefs; they were
    # one absence, and only a combined statement says so.
    coverage = review_evidence.evidence_coverage(
        diff_base=diff_base,
        branch=task_view.branch or "",
        call_sites_status=call_sites_section.status,
        has_test_acs=any(a.verifiable_by.value == "test" for a in ac_models),
        locator_resolution=locator_resolution,
        ac_test_results=ac_test_results,
        ci_state=ci_state,
        freshness=freshness,
        sha_check=sha_check,
        live_check=live_check,
    )

    # #808: the block the human reads at the gate, built by the same function
    # that feeds the task card. Two readers, one report.
    brief_review_report = await review_evidence.review_report(db, task_row, mr_row)

    # #890: scope accepted at submission, newest first. Read from the feed
    # rather than a column: the growth IS an event, and an event that only
    # existed as a field would lose when it happened.
    growth_updates = await repo.get_task_updates(db, task_row["id"])
    scope_growth = [
        str(u["content"])
        for u in reversed(list(growth_updates))
        if str(u["content"]).startswith(commit_scope.SCOPE_GROWTH_MARKER)
    ]

    return ReviewBrief(
        review_report=brief_review_report,
        task_id=task_view.id,
        title=task_view.title,
        status=task_view.status,
        description=task_view.description,
        project=task_view.project,
        acceptance_criteria=ac_models,
        locator_resolution=locator_resolution,
        ac_test_results=ac_test_results,
        ci_run_report=ci_run_report,
        prepass=prepass,
        live_check=LiveCheckState(**live_check),
        statement_freshness=freshness,
        scope_in=task_view.scope_in,
        scope_out=task_view.scope_out,
        scope_growth=scope_growth,
        out_of_scope_for_review=task_view.out_of_scope_for_review,
        review_checklist=task_view.review_checklist,
        validation_commands=task_view.validation_commands,
        constraints=task_view.constraints,
        technical_hints=task_view.technical_hints,
        outcome_metric=task_view.outcome_metric,
        outcome_indicator=task_view.outcome_indicator,
        outcome_deadline=task_view.outcome_deadline,
        outcome_revisit_condition=task_view.outcome_revisit_condition,
        outcome_status=await outcome_status_for_task(db, task_row),
        redesign_decision=task_view.redesign_decision,
        redesign_rationale=task_view.redesign_rationale,
        agent_fit=task_view.agent_fit,
        branch=task_view.branch,
        pr_number=task_view.pr_number,
        diff_command=diff_command,
        diff_base=DiffBaseState(**diff_base),
        evidence_coverage=EvidenceCoverage(**coverage),
        submission_sha=submission_sha,
        current_branch_tip=current_tip,
        sha_check=sha_check,
        sha_check_reason=sha_check_reason,
        call_sites=call_sites_section,
        review_cycle=task_view.review_cycle,
        submission_generation=task_view.submission_generation,
        latest_submission_summary=latest_submission_summary,
        latest_review=task_view.latest_review,
        machine_review=machine_review,
        self_review_warning=self_review_warning,
        stacking_warning=stacking_warning,
        review_in_flight=await review_evidence.inflight_view(db, task_row),
    )


async def gate_evidence(db, task_row: dict[str, Any]) -> ReviewBrief | None:
    """The evidence panel for a task card, or ``None`` when there is no gate.

    The card pays for the assembly only where a verdict can be cast. Outside
    those statuses there is nothing to decide, and the panel would cost git
    work to tell a reader who is not deciding anything.
    """
    status = task_row.get("status")
    status = getattr(status, "value", status)
    if status not in GATE_STATUSES:
        return None
    try:
        return await build_review_brief(db, int(task_row["id"]))
    except Exception as exc:  # noqa: BLE001 - the card must render regardless
        log.warning("evidence panel failed for task #%s: %s", task_row.get("id"), exc)
        return None
