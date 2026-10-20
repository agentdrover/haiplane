#!/usr/bin/env python3
"""CI: reuse the evidence of the dispatch run on the PR run of the same head (#1919).

The executor dispatches CI before submitting; the hub opens the PR at submission.
Both runs sit on the same head sha, and both used to count the mutations and the
red-test baseline from scratch (~10 min). This script decides, in job ``treedup``,
whether the PR run may take the evidence artifact of the dispatch run instead, and
writes the adopted files, stamped with the source run, into ``--out-dir``; the job
then uploads them as the artifact of THIS run (#1921: the decision and the files
come from one step, so a doubt after the decision cannot leave a run without them).

    decide  --head-sha SHA --base-ref develop --github-output FILE --out-dir DIR

Rules of ``decide`` (anything else is a recount, never a red job):

* the PR run checks out ``refs/pull/N/merge``: its real base is ``HEAD^1``, not the
  event's ``base.sha`` (which may lag), and its tree must equal the tree of the PR
  head, so the data of the dispatch run describe the same code;
* the source is the newest successful ``workflow_dispatch`` run of ``ci.yml`` on the
  same head sha whose job ``evidence`` succeeded and whose artifact is still there;
* both files are in a state that carries information, name the same head, and their
  ``merge_base`` and ``base_tip`` both equal ``HEAD^1`` (a file without these fields
  comes from a run before #1919 and is recounted).

Standard library only: job ``treedup`` installs nothing.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

WORKFLOW = "ci.yml"
EVIDENCE_JOB = "Mutations and red-test baseline"
ARTIFACT = "hub-ci-evidence"
MUTATIONS_FILE = "mutation-report.json"
BASELINE_FILE = "red-test-baseline.json"
# The names the writer uses (scripts/mutation_changed.py STATE_*): a name the
# writer never emits would silently turn every such run into a recount.
MUTATIONS_STATES = frozenset({"ran", "no_changed_functions", "no_tests"})
BASELINE_STATES = frozenset({"ran", "no_tests"})
ADOPTED_KEY = "adopted_from_run"
_ZIP_LIMIT = 5 * 1024 * 1024
_MEMBER_LIMIT = 2 * 1024 * 1024
_CANDIDATES = 20

Api = Callable[[str], Any]
FetchZip = Callable[[int], bytes]


def _document(zip_bytes: bytes, name: str) -> dict:
    """One JSON object from the archive; a missing or oversized member raises."""
    if len(zip_bytes) > _ZIP_LIMIT:
        raise ValueError("archive too large")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        info = archive.getinfo(name)
        if info.file_size > _MEMBER_LIMIT:
            raise ValueError(f"{name} too large")
        value = json.loads(archive.read(info))
    if not isinstance(value, dict):
        raise ValueError(f"{name} is not an object")
    return value


def refusal(
    mutations: dict, baseline: dict, *, head_sha: str, parent1: str, base_ref: str
) -> str:
    """Why these two documents cannot be adopted; '' when they can."""
    if mutations.get("state") not in MUTATIONS_STATES:
        return f"mutations state {mutations.get('state')!r}"
    if baseline.get("state") not in BASELINE_STATES:
        return f"baseline state {baseline.get('state')!r}"
    if mutations.get("head") != head_sha or baseline.get("head") != head_sha:
        return "evidence names another head"
    for label, document in (("mutations", mutations), ("baseline", baseline)):
        given = document.get("base") or document.get("base_ref")
        if given != f"origin/{base_ref}":
            return f"{label} counted against {given!r}, not origin/{base_ref}"
        for key in ("merge_base", "base_tip"):
            if document.get(key) != parent1:
                return f"{label} {key} differs from the base of this run"
    return ""


def find_source(
    api: Api,
    fetch_zip: FetchZip,
    *,
    repo: str,
    head_sha: str,
    parent1: str,
    base_ref: str,
    keep: Callable[[int, dict, dict], None] | None = None,
) -> tuple[int, str]:
    """(run id, '') of the newest adoptable dispatch run, or (0, reason).

    ``keep(run_id, mutations, baseline)`` is called once, with the documents of
    the chosen run, before the answer is returned.
    """
    listing = api(
        f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch"
        f"&head_sha={head_sha}&status=success&per_page={_CANDIDATES}"
    )
    runs = sorted(
        listing.get("workflow_runs", []),
        key=lambda run: run.get("created_at", ""),
        reverse=True,
    )
    reason = "no successful dispatch run on this head"
    for run in runs:
        run_id = int(run["id"])
        jobs = api(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
        if not any(
            job.get("name") == EVIDENCE_JOB and job.get("conclusion") == "success"
            for job in jobs.get("jobs", [])
        ):
            reason = f"run {run_id}: job evidence did not succeed"
            continue
        found = api(f"repos/{repo}/actions/runs/{run_id}/artifacts?name={ARTIFACT}")
        live = [a for a in found.get("artifacts", []) if not a.get("expired")]
        if not live:
            reason = f"run {run_id}: no live artifact {ARTIFACT}"
            continue
        try:
            archive = fetch_zip(int(live[0]["id"]))
            mutations = _document(archive, MUTATIONS_FILE)
            baseline = _document(archive, BASELINE_FILE)
        except (KeyError, ValueError, OSError, zipfile.BadZipFile) as exc:
            reason = f"run {run_id}: unreadable artifact ({exc.__class__.__name__})"
            continue
        why = refusal(
            mutations,
            baseline,
            head_sha=head_sha,
            parent1=parent1,
            base_ref=base_ref,
        )
        if why:
            reason = f"run {run_id}: {why}"
            continue
        if keep is not None:
            keep(run_id, mutations, baseline)
        return run_id, ""
    return 0, reason


def merge_facts(git: Callable[..., str], head_sha: str) -> tuple[str, str]:
    """(HEAD^1, '') when HEAD is the merge of ``head_sha`` with identical trees."""
    if git("rev-parse", "HEAD^2") != head_sha:
        return "", "HEAD is not the merge commit of the PR head"
    if git("rev-parse", "HEAD^{tree}") != git("rev-parse", f"{head_sha}^{{tree}}"):
        return "", "the merge tree differs from the head tree"
    return git("rev-parse", "HEAD^1"), ""


def decide(
    api: Api,
    fetch_zip: FetchZip,
    git: Callable[..., str],
    *,
    repo: str,
    head_sha: str,
    base_ref: str,
    out_dir: Path | None = None,
) -> tuple[int, str]:
    """(run id to adopt, '') or (0, reason). Never raises.

    With ``out_dir`` the adopted documents are written there, stamped; a failure
    to write them is a recount, so ``adopt`` never promises files that are not there.
    """
    try:
        parent1, why = merge_facts(git, head_sha)
        if why:
            return 0, why
        return find_source(
            api,
            fetch_zip,
            repo=repo,
            head_sha=head_sha,
            parent1=parent1,
            base_ref=base_ref,
            keep=None
            if out_dir is None
            else lambda *args: write_adopted(out_dir, *args),
        )
    except Exception as exc:  # noqa: BLE001 - any failure is a recount
        return 0, f"{exc.__class__.__name__}: {str(exc)[:200]}"


def write_adopted(
    directory: Path, run_id: int, mutations: dict, baseline: dict
) -> None:
    """Write both documents, with the source run inside the block (not provenance)."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, document in ((MUTATIONS_FILE, mutations), (BASELINE_FILE, baseline)):
        stamped = {**document, ADOPTED_KEY: run_id}
        (directory / name).write_text(
            json.dumps(stamped, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _gh(*args: str) -> bytes:
    done = subprocess.run(  # nosec B603 B607 - gh with fixed argv
        ["gh", *args], capture_output=True, check=True, timeout=120
    )
    return done.stdout


def _real_api(path: str) -> Any:
    return json.loads(_gh("api", path))


def _real_zip(artifact_id: int) -> bytes:
    repo = os.environ["GITHUB_REPOSITORY"]
    return _gh("api", f"repos/{repo}/actions/artifacts/{artifact_id}/zip")


def _real_git(*args: str) -> str:
    done = subprocess.run(  # nosec B603 B607 - git with fixed argv
        ["git", *args], capture_output=True, text=True, check=True, timeout=60
    )
    return done.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    decide_cmd = sub.add_parser("decide")
    decide_cmd.add_argument("--head-sha", required=True)
    decide_cmd.add_argument("--base-ref", required=True)
    decide_cmd.add_argument("--github-output", required=True)
    decide_cmd.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)

    run_id, reason = decide(
        _real_api,
        _real_zip,
        _real_git,
        repo=os.environ.get("GITHUB_REPOSITORY", ""),
        head_sha=args.head_sha,
        base_ref=args.base_ref,
        out_dir=Path(args.out_dir),
    )
    with open(args.github_output, "a", encoding="utf-8") as out:
        out.write(f"adopt={'true' if run_id else 'false'}\n")
        out.write(f"adopted_run_id={run_id or ''}\n")
    print(
        f"adopt evidence: run {run_id}"
        if run_id
        else f"adopt evidence: recount ({reason})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
