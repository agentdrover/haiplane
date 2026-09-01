"""GitHub как форж: всё, что раньше звало ``gh`` прямо из git_ops (#1113).

Тела перенесены без правок логики, кодов возврата и текстов — задача #1113
объявлена рефакторингом с нулевой сменой поведения, и любая правка ожидания
в существующем тесте означала бы, что поведение всё-таки поменялось.

Что здесь НЕ живёт: git. Файлы конфликта ищет ``git_ops`` своим клоном, а
этот адаптер отдаёт исход без имён файлов — форж их и не знает. Разделение
не косметическое: без него адаптер второго форжа обязан был бы притащить
свою копию работы с клоном.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from hub.config import GH_BIN, REPO_NAME
from hub.integrations import proc
from hub.integrations.protocols import (
    CIProbeOutcome,
    CIProbeResult,
    MergeabilityOutcome,
)

log = logging.getLogger(__name__)


async def _gh(*args: str, repo: str | None = None, **kw) -> tuple[int, str, str]:
    repo = repo or proc.repo_root()
    return await proc.run(GH_BIN, *args, cwd=repo, **kw)


def _parse_pr_number(gh_output: str) -> int | None:
    m = re.search(r"/pull/(\d+)", gh_output)
    return int(m.group(1)) if m else None


class GitHubForge:
    """Concrete forge plugin backed by the ``gh`` CLI."""

    name = "github"
    #: gh pr merge — один вызов, форж сливает сам (#1116).
    can_merge_via_api = True

    # -- pull requests ------------------------------------------------------

    async def create_pr(
        self,
        title: str,
        body: str,
        branch: str,
        base: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> int | None:
        rc, out, err = await _gh(
            "pr",
            "create",
            "--repo",
            gh_repo or REPO_NAME,
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
            repo=repo,
            check=False,
            timeout=30,
        )
        if rc != 0:
            if "already exists" in err:
                return await self.pr_for_branch(branch, repo=repo, gh_repo=gh_repo)
            log.error("Failed to create PR for %s: %s", branch, err)
            return None

        pr_number = _parse_pr_number(out)
        if pr_number:
            log.info("Created PR #%d for branch %s", pr_number, branch)
        return pr_number

    async def pr_for_branch(
        self, branch: str, *, repo: str | None = None, gh_repo: str | None = None
    ) -> int | None:
        rc, out, _ = await _gh(
            "pr",
            "list",
            "--repo",
            gh_repo or REPO_NAME,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number",
            repo=repo,
            check=False,
        )
        if rc == 0 and out:
            try:
                prs = json.loads(out)
                if prs:
                    return prs[0]["number"]
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
        return None

    async def open_or_update_pr(
        self,
        base: str,
        head: str,
        title: str,
        body: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> int | None:
        """The open PR for this range — found and updated, or created.

        Idempotent on purpose (#812 AC-5): two sessions can deliver within
        seconds of each other, and a second pull request over the same range
        would split one release into two stories about the same commits.
        """
        existing = await self.pr_for_branch(head, repo=repo, gh_repo=gh_repo)
        if existing:
            await _gh(
                "pr",
                "edit",
                str(existing),
                "--repo",
                gh_repo or REPO_NAME,
                "--title",
                title,
                "--body",
                body,
                repo=repo,
                check=False,
            )
            return existing
        rc, out, err = await _gh(
            "pr",
            "create",
            "--repo",
            gh_repo or REPO_NAME,
            "--base",
            base,
            "--head",
            head,
            "--title",
            title,
            "--body",
            body,
            repo=repo,
            check=False,
        )
        if rc != 0:
            log.warning("release PR not created: %s", (err or out or "").strip()[:200])
            return None
        return _parse_pr_number(out)

    async def pr_state(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> str:
        """Where this PR stands: "open", "merged", "closed", "absent", or "".

        Empty means the question could not be asked — no gh, no network, an
        unreadable answer. The caller treats that as a cause to report, never
        as an answer: "could not look" and "closed" lead to opposite decisions
        about delivery (#802, the rule #725 wrote down).

        "absent" is the fourth answer (#959), and it is an ANSWER: the number
        is not in the project's repository. Until it had a name it arrived as
        "" — the same value a blinking network produces — so the gate kept
        waiting for CI on a PR that cannot exist. That is not hypothetical: a
        project that moved between repositories carries numbers from the old
        one, and on #880 the gate offered to wait for a green CI forever.
        """
        # One field, and it is the whole answer: gh reports MERGED as a STATE,
        # and there is no `merged` flag to ask for. Asking for one made the
        # call fail outright ("Unknown JSON field"), so this method returned
        # "could not look" every single time and the gate it feeds quietly
        # fell back to the old behaviour (#803, found on the first live run).
        rc, out, _ = await _gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "state",
            repo=repo,
            check=False,
        )
        if rc != 0 or not out:
            return await self._pr_absent_or_unknown(pr_number, repo, gh_repo)
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return ""
        return str(data.get("state") or "").lower()

    async def _pr_absent_or_unknown(
        self, pr_number: int, repo: str | None, gh_repo: str | None
    ) -> str:
        """Tell "no such PR here" from "could not ask", or "" (#959).

        Asked ONLY after ``gh pr view`` has already failed, so the working path
        pays nothing for it. Deliberately a second question rather than a
        different first one: the comment above remembers #803, where changing
        what pr_state asks broke the reading of MERGED for every PR. The cheap
        answer keeps its transport; only the failure gets a follow-up.

        The signal is the REST status in the response body, not a substring of
        gh's prose — error text drifts between versions, a 404 does not. A
        missing repository answers 404 too, and that is correct: "not reachable
        as this project's PR" is the same decision either way.
        """
        rc, out, _ = await _gh(
            "api",
            f"repos/{gh_repo or REPO_NAME}/pulls/{pr_number}",
            repo=repo,
            check=False,
        )
        if rc == 0:
            # The REST call answered where the GraphQL one did not. Nothing is
            # claimed from that: the state is read by the caller's next pass.
            return ""
        try:
            body = json.loads(out or "{}")
        except json.JSONDecodeError:
            return ""
        return "absent" if str(body.get("status") or "") == "404" else ""

    async def pr_is_draft(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> bool:
        """Whether GitHub still treats this PR as a draft (#1053).

        A Cloud Agent opens PRs as drafts by default. Hub ``create_pr`` never
        does. ``gh pr merge`` refuses a draft with the same boolean
        ``merge_pr`` already returns for a conflict or a revoked token, and
        that boolean used to send the task to ``needs_decision``. False here
        means "not a draft or could not look" — same #498 rule as the other
        readers: silence is not an accusation, and the merge call still runs.
        """
        rc, out, err = await _gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "isDraft",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            log.info(
                "PR #%d draft probe unavailable: %s",
                pr_number,
                (err or "gh молчит").strip()[:200],
            )
            return False
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return False
        return bool(data.get("isDraft"))

    async def mark_pr_ready(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> bool:
        """Convert a draft PR to ready. Hub approval is the ready signal (#1053)."""
        rc, _, err = await _gh(
            "pr",
            "ready",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            repo=repo,
            check=False,
        )
        if rc == 0:
            log.info("Marked PR #%d ready", pr_number)
            return True
        log.warning(
            "Failed to mark PR #%d ready: %s",
            pr_number,
            (err or "").strip()[:200],
        )
        return False

    async def pr_head_sha(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> str:
        rc, out, _err = await _gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "headRefOid",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            return ""
        try:
            return str(json.loads(out).get("headRefOid") or "").strip()
        except (json.JSONDecodeError, AttributeError, TypeError):
            return ""

    async def pr_refs(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> tuple[str, str]:
        """``(base, head)`` ref names of this PR, or ``("", "")``."""
        rc, out, _ = await _gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "baseRefName,headRefName",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            return ("", "")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return ("", "")
        return (
            str(data.get("baseRefName") or "").strip(),
            str(data.get("headRefName") or "").strip(),
        )

    async def pr_mergeability(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> tuple[MergeabilityOutcome, str]:
        """Can this PR be merged, and if not, why (#970).

        The release asked GitHub one question before merging — is CI green —
        and learned the rest by being refused. On 26.08.2026 PR #83 answered
        both at once: «Ruff and pytest» pass 4m2s, and mergeable=CONFLICTING.
        The poller called ``merge_pr`` every cycle, was refused every cycle,
        and reported «GitHub отказал» — a sentence that names who said no.
        A conflict, a revoked token and a deleted base branch all produce it,
        and none of them is fixed the same way.

        UNKNOWN is passed through as itself rather than folded into a
        conflict. GitHub computes mergeability asynchronously and honestly
        says UNKNOWN for the first seconds of a pull request's life — the
        release PR the poller just opened is exactly that case, every time.
        The next cycle asks again, the same way it already does for CI.

        The conflict detail names no files: they come from the clone, which
        is git_ops' side of the fence (#1113). The caller adds them.
        """
        rc, out, err = await _gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "mergeable,mergeStateStatus",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            detail = (err or "").strip() or "gh молчит"
            return (MergeabilityOutcome.unavailable, detail[:200])
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return (MergeabilityOutcome.unavailable, "ответ gh не разобран")

        mergeable = str(data.get("mergeable") or "").upper()
        state = str(data.get("mergeStateStatus") or "").upper()
        if mergeable == "MERGEABLE":
            return (MergeabilityOutcome.mergeable, state.lower() or "mergeable")
        if mergeable == "CONFLICTING":
            return (MergeabilityOutcome.conflicting, "конфликт с базовой веткой")
        if mergeable == "UNKNOWN" or not mergeable:
            return (
                MergeabilityOutcome.unknown,
                f"GitHub ещё не посчитал слияние ({state.lower() or 'без статуса'})",
            )
        return (MergeabilityOutcome.unavailable, f"нераспознанный ответ {mergeable}")

    async def merge_commit_sha(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> str:
        """The SHA of the commit THIS pull request produced, or "" (#534).

        Not the tip of the base branch. The tip is whatever landed last, and
        between the merge and the read a direct push can land — which would
        write the intruder into the whitelist and mark the real merge as
        drift. The pull request knows its own merge commit, so ask it.
        """
        rc, out, err = await _gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "mergeCommit",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            log.warning(
                "could not read the merge commit of PR #%d: %s",
                pr_number,
                (err or "").strip(),
            )
            return ""
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            log.warning("PR #%d returned no readable merge commit", pr_number)
            return ""
        return str((data.get("mergeCommit") or {}).get("oid") or "").strip()

    async def merge_pr(
        self,
        pr_number: int,
        subject: str,
        *,
        delete_branch: bool = True,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool:
        """Merge one PR; ``delete_branch`` says what happens to its head (#949).

        Deleting the head is right for a task PR — short-lived branches, the
        repository's own rule. But this one call served the RELEASE PR too,
        whose head is the project's integration branch: every auto-release of
        24–25.08 deleted develop, three times in two days, and the repo's
        delete_branch_on_merge=false proves it was us, not GitHub. The default
        stays True so the task path is untouched; the release path passes
        False, because a release must not remove the branch work lands on.
        """
        args = [
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--squash",
            "--admin",
        ]
        if delete_branch:
            args.append("--delete-branch")
        args += ["--subject", subject]
        rc, _, err = await _gh(
            *args,
            repo=repo,
            check=False,
            timeout=30,
        )
        if rc == 0:
            log.info("Merged PR #%d (squash, admin)", pr_number)
            return True
        log.error("Failed to merge PR #%d: %s", pr_number, err)
        return False

    async def close_pr(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> bool:
        """Закрыть PR, ничего не вливая (#1116).

        На GitHub этот путь не нужен для доставки — мерж закрывает PR сам, —
        но контракт один на все форжи, и реализация обязана существовать.
        """
        rc, _, err = await _gh(
            "pr",
            "close",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            repo=repo,
            check=False,
        )
        if rc == 0:
            return True
        log.warning("Failed to close PR #%d: %s", pr_number, (err or "").strip()[:200])
        return False

    async def branch_contains(
        self,
        branch: str,
        sha: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool | None:
        """Достижим ли ``sha`` в ``branch`` на remote, или None (#1116).

        None — «спросить не удалось», и это не то же, что «не достижим»:
        первое означает повторить вопрос, второе — что работа не доставлена.
        """
        if not sha:
            return None
        rc, out, _ = await _gh(
            "api",
            f"repos/{gh_repo or REPO_NAME}/compare/{sha}...{branch}",
            "--jq",
            ".status",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            return None
        # ahead — база ушла вперёд от sha, identical — стоит ровно на нём:
        # в обоих случаях коммит УЖЕ в ветке. behind и diverged означают, что
        # его там нет.
        return (out or "").strip() in ("ahead", "identical")

    # -- CI -----------------------------------------------------------------

    async def has_workflows(
        self, *, repo: str | None = None, gh_repo: str | None = None
    ) -> bool | None:
        """Whether the repository defines any Actions workflow (#1041).

        Called only on the branch that used to return ``absent``: empty
        ``gh pr checks`` or empty ``actions/runs?head_sha=``. A working
        probe never pays this extra GitHub call.
        """
        rc, out, _err = await _gh(
            "api",
            f"repos/{gh_repo or REPO_NAME}/actions/workflows",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            return None
        try:
            payload = json.loads(out)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        workflows = payload.get("workflows")
        if isinstance(workflows, list) and workflows:
            return True
        try:
            return int(payload.get("total_count") or 0) > 0
        except (TypeError, ValueError):
            return None

    async def _absent_or_missing_run(
        self,
        pr_number: int,
        reason: str,
        *,
        repo: str | None,
        gh_repo: str | None,
        sha: str = "",
    ) -> CIProbeResult:
        """Split 'no CI in the repo' from 'this SHA has no run yet' (#1041)."""
        has = await self.has_workflows(repo=repo, gh_repo=gh_repo)
        if has is None:
            return CIProbeResult(CIProbeOutcome.unavailable, "workflows_unavailable")
        if not has:
            return CIProbeResult(CIProbeOutcome.absent, reason)
        if not sha:
            sha = await self.pr_head_sha(pr_number, repo=repo, gh_repo=gh_repo)
        return CIProbeResult(CIProbeOutcome.missing_run, reason, details=sha or None)

    async def _workflow_runs_probe(
        self, pr_number: int, repo: str | None, gh_repo: str | None
    ) -> CIProbeResult:
        """CI verdict from Actions workflow runs, by the PR's head SHA (#606).

        The primary probe (``gh pr checks``) needs ``checks:read`` — a
        permission GitHub removed from the fine-grained token picker while
        the API still demands it (verified 2026-08-03: the picker's search
        for "check" answers "No items available", the API answers 403). This
        path uses only permissions a token can actually be granted: Pull
        requests (headRefOid) and Actions (workflow runs).

        The mapping is conservative — unknown is never ok: an unrecognised
        conclusion reads as unavailable, not as passed. A workflow's
        conclusion is GitHub's own aggregate over its jobs, so skipped jobs
        (Deploy on a PR) are already accounted for.
        """
        rc, out, err = await _gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "headRefOid",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            return CIProbeResult(
                CIProbeOutcome.unavailable,
                "workflow_runs_unavailable",
                details=f"could not read headRefOid: {(err or '').strip()}",
            )
        try:
            head_sha = str(json.loads(out).get("headRefOid") or "").strip()
        except json.JSONDecodeError:
            head_sha = ""
        if not head_sha:
            return CIProbeResult(
                CIProbeOutcome.unavailable,
                "workflow_runs_unavailable",
                details="PR carries no readable headRefOid",
            )

        rc, out, err = await _gh(
            "api",
            f"repos/{gh_repo or REPO_NAME}/actions/runs?head_sha={head_sha}",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            return CIProbeResult(
                CIProbeOutcome.unavailable,
                "workflow_runs_unavailable",
                details=(err or "").strip(),
            )
        try:
            runs = json.loads(out).get("workflow_runs") or []
        except (json.JSONDecodeError, AttributeError):
            return CIProbeResult(
                CIProbeOutcome.unavailable, "workflow_runs_invalid_json"
            )

        # No runs for this SHA used to mean "the repository has no CI"
        # (#419). That collapsed two facts: no workflows at all, and
        # workflows that have not yet produced a run for this commit.
        if not runs:
            return await self._absent_or_missing_run(
                pr_number,
                "no_workflow_runs",
                repo=repo,
                gh_repo=gh_repo,
                sha=head_sha,
            )

        statuses = [str(r.get("status") or "").lower() for r in runs]
        conclusions = [str(r.get("conclusion") or "").lower() for r in runs]
        if any(
            s in ("queued", "in_progress", "waiting", "requested", "pending")
            for s in statuses
        ):
            return CIProbeResult(CIProbeOutcome.pending, "workflow_runs_running")
        if any(
            c
            in (
                "failure",
                "cancelled",
                "timed_out",
                "startup_failure",
                "action_required",
            )
            for c in conclusions
        ):
            return CIProbeResult(CIProbeOutcome.failed, "workflow_runs_failed")
        if all(s == "completed" for s in statuses) and all(
            c in ("success", "neutral", "skipped") for c in conclusions
        ):
            return CIProbeResult(CIProbeOutcome.passed, "workflow_runs_passed")
        return CIProbeResult(
            CIProbeOutcome.unavailable,
            "workflow_runs_unknown_state",
            details=",".join(sorted(set(conclusions) | set(statuses))),
        )

    async def check_pr_ci(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> CIProbeResult:
        rc, out, err = await _gh(
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "name,state",
            repo=repo,
            check=False,
        )
        # rc error / empty output — the probe itself could not run. Before
        # answering "unavailable", try the workflow-runs fallback (#606): the
        # primary path needs checks:read, which GitHub's token picker no
        # longer offers while the API still demands it. A WORKING primary
        # path never reaches this line, so its behaviour is untouched.
        if rc != 0 or not out or not out.strip():
            fallback = await self._workflow_runs_probe(pr_number, repo, gh_repo)
            if fallback.outcome != CIProbeOutcome.unavailable:
                return fallback
            # Both paths failed: name the primary error — it is the one an
            # operator can act on (#419: not pending, nothing known in flight).
            return CIProbeResult(
                CIProbeOutcome.unavailable,
                "gh_error",
                details=(err or "").strip() or fallback.details,
            )
        try:
            checks = json.loads(out)
        except json.JSONDecodeError:
            return CIProbeResult(CIProbeOutcome.unavailable, "invalid_json")

        # An empty check set used to skip the conveyor. It is still "no
        # checks", but if the repo has workflows this SHA simply has no run
        # yet — GitHub's registration lag, not "there is nothing to check".
        if not checks:
            return await self._absent_or_missing_run(
                pr_number, "no_checks", repo=repo, gh_repo=gh_repo
            )

        states = [c.get("state", "").upper() for c in checks]
        if any(
            s in ("PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "")
            for s in states
        ):
            return CIProbeResult(CIProbeOutcome.pending, "checks_running")
        if any(s in ("FAILURE", "ERROR", "ACTION_REQUIRED") for s in states):
            return CIProbeResult(CIProbeOutcome.failed, "checks_failed")
        if all(s in ("SUCCESS", "NEUTRAL", "SKIPPED") for s in states):
            return CIProbeResult(CIProbeOutcome.passed, "checks_passed")
        # Reached only for states gh reports that we do not recognise — treat as
        # unavailable (a stable reason) rather than silently waiting.
        return CIProbeResult(
            CIProbeOutcome.unavailable, "unknown_state", details=",".join(states)
        )

    async def branch_ci_runs(
        self,
        branch: str,
        limit: int = 20,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """Push runs on ``branch``, newest first — or None if unreadable (#929).

        The PR probe above asks about one commit; this asks about the branch's
        own history, which is what says whether the BASE is green right now
        and, if not, which commits arrived since it last was.

        None rather than an empty list when the question could not be asked:
        "no runs" and "could not look" lead to opposite conclusions, and the
        caller must not be able to confuse them by accident (#725).
        """
        rc, out, err = await _gh(
            "api",
            f"repos/{gh_repo or REPO_NAME}/actions/runs"
            f"?branch={branch}&event=push&per_page={max(1, min(limit, 100))}",
            repo=repo,
            check=False,
        )
        if rc != 0 or not (out or "").strip():
            log.warning(
                "branch CI history for %s unavailable: %s", branch, (err or "").strip()
            )
            return None
        try:
            runs = json.loads(out).get("workflow_runs")
        except (json.JSONDecodeError, AttributeError):
            log.warning("branch CI history for %s: invalid json", branch)
            return None
        if runs is None:
            return None
        return [
            {
                "sha": str(r.get("head_sha") or ""),
                "status": str(r.get("status") or "").lower(),
                "conclusion": str(r.get("conclusion") or "").lower(),
                "created_at": str(r.get("created_at") or ""),
                "name": str(r.get("name") or ""),
            }
            for r in runs
        ]

    async def ci_failure_logs(
        self,
        pr_number: int,
        branch: str,
        max_log_chars: int = 12000,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"failed_checks": [], "log_summary": "", "run_url": ""}

        rc, out, _ = await _gh(
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            gh_repo or REPO_NAME,
            "--json",
            "name,state",
            repo=repo,
            check=False,
        )
        if rc == 0 and out:
            try:
                checks = json.loads(out)
                result["failed_checks"] = [
                    c["name"]
                    for c in checks
                    if c.get("state", "").upper()
                    in ("FAILURE", "ERROR", "ACTION_REQUIRED")
                ]
            except (json.JSONDecodeError, KeyError):
                pass

        rc, out, _ = await _gh(
            "run",
            "list",
            "--repo",
            gh_repo or REPO_NAME,
            "--branch",
            branch,
            "--limit",
            "1",
            "--json",
            "databaseId,url,status",
            repo=repo,
            check=False,
        )
        run_id = None
        if rc == 0 and out:
            try:
                runs = json.loads(out)
                if runs:
                    run_id = runs[0].get("databaseId")
                    result["run_url"] = runs[0].get("url", "")
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

        if run_id:
            rc, out, _ = await _gh(
                "run",
                "view",
                str(run_id),
                "--repo",
                gh_repo or REPO_NAME,
                "--log-failed",
                repo=repo,
                check=False,
                timeout=90,
            )
            if rc == 0 and out:
                if len(out) > max_log_chars:
                    out = out[-max_log_chars:]
                    out = "... (truncated) ...\n" + out
                result["log_summary"] = out

        return result

    # -- release ------------------------------------------------------------

    async def compare_subjects(
        self,
        base: str,
        head: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[str]:
        """Commit subjects that ``head`` carries over ``base``, newest first.

        The subject is cut by JQ, not here (#963). Asking for the whole
        message and splitting the reply by lines counts LINES, not commits:
        jq prints a multi-line string as multiple output lines, so one commit
        with a body arrived as as many "commits" as it had lines. Release PR
        #40 listed 25 items — Co-authored-by among them — for a single commit,
        and #44 claimed four tasks for three: a squash message repeats the
        branch commit's subject, which also ends in «(#NNN)», so the number
        was counted twice.

        One output line = one commit is a contract, and it holds only while
        the cut happens in the query: commit boundaries cannot be recovered
        from concatenated text by anything but guessing.
        """
        rc, out, _ = await _gh(
            "api",
            f"repos/{gh_repo or REPO_NAME}/compare/{base}...{head}",
            "--jq",
            '.commits[].commit.message | split("\n")[0]',
            repo=repo,
            check=False,
        )
        if rc != 0 or not out:
            return []
        return [line.strip() for line in reversed(out.splitlines()) if line.strip()]

    async def merge_branches(
        self,
        into_branch: str,
        from_branch: str,
        message: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> tuple[str, str]:
        """Merge ``from_branch`` into ``into_branch`` server-side (#969).

        ``(returned <sha> | nothing | conflict | unavailable, detail)``. Four
        names rather than three, because a conflict and a git that could not
        be asked need different hands: one is a merge somebody has to resolve,
        the other is a question to ask again next cycle. Collapsing them is
        how #725 gets repeated with new words.

        Asks GitHub to do the merge rather than driving the workspace clone.
        The clone is shared, may sit on someone else's branch with a dirty
        tree, and carries an armed pre-push hook — three ways for a
        bookkeeping merge to damage work in progress (#949 was one of them).
        The merges endpoint has no such surface: it answers 201 with the new
        commit, 204 when there is nothing to merge, 409 on a conflict.

        The conflict detail names no files — those come from the clone, and
        the clone is git_ops' side of the fence (#1113).
        """
        if not gh_repo and not REPO_NAME:
            return ("unavailable", "не названо, в каком репозитории возвращать")
        rc, out, err = await _gh(
            "api",
            "--method",
            "POST",
            f"repos/{gh_repo or REPO_NAME}/merges",
            "-f",
            f"base={into_branch}",
            "-f",
            f"head={from_branch}",
            "-f",
            f"commit_message={message}",
            repo=repo,
            check=False,
        )
        if rc == 0:
            # 204 — «уже содержит», и gh печатает пустоту. Это ответ, а не
            # промах: возвращать нечего.
            body = (out or "").strip()
            if not body:
                return ("nothing", f"{into_branch} уже содержит {from_branch}")
            try:
                sha = str(json.loads(body).get("sha") or "").strip()
            except json.JSONDecodeError:
                return ("unavailable", f"ответ GitHub не разобран: {body[:150]}")
            if not sha:
                return ("unavailable", "GitHub не назвал коммит возврата")
            return ("returned", sha)

        detail = (err or "").strip() or "gh молчит"
        if "409" in detail or "conflict" in detail.lower():
            return ("conflict", "")
        return ("unavailable", detail[:200])
