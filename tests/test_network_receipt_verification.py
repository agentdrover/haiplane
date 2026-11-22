"""#2414: GitHub file identity, project CI projection, submission and verdict."""

import base64
import json
from unittest.mock import AsyncMock

import pytest

from hub.services import ci_contract
from hub.services import network_receipt_verification as verify

HEAD, COMMIT, OLD, NEW, EVIDENCE, BLOB = [c * 40 for c in "abcdef"]
NONCE = "0" * 32
CONTENTS = (
    f'{{"head_sha":"{HEAD}","order_id":17,"receipt_id":"{NONCE}",'
    '"schema":1,"task_id":2413}\n'
).encode()
BRANCH = "task-2413/k8-network-receipt"
WORKFLOW = ".github/workflows/tests.yml"
REPOSITORY = "agentdrover/home-infra"


def _entry(path, mode="100644", kind="blob", sha=BLOB):
    return {"path": path, "mode": mode, "type": kind, "sha": sha}


def _tree(sha, *entries):
    return {"sha": sha, "tree": list(entries), "truncated": False}


@pytest.fixture
def github(monkeypatch):
    github_data = {
        f"git/commits/{COMMIT}": {
            "sha": COMMIT,
            "parents": [{"sha": HEAD}],
            "tree": {"sha": NEW},
        },
        f"git/commits/{HEAD}": {"sha": HEAD, "tree": {"sha": OLD}},
        f"git/trees/{OLD}": _tree(OLD, _entry("README")),
        f"git/trees/{NEW}": _tree(
            NEW, _entry("README"), _entry("evidence", "040000", "tree", EVIDENCE)
        ),
        f"git/trees/{EVIDENCE}": _tree(
            EVIDENCE, _entry("k8-network-receipt-2413.json")
        ),
        f"git/blobs/{BLOB}": {
            "sha": BLOB,
            "size": len(CONTENTS),
            "encoding": "base64",
            "content": base64.encodebytes(CONTENTS).decode(),
        },
        f"git/ref/heads/{BRANCH}": {
            "ref": f"refs/heads/{BRANCH}",
            "object": {"sha": COMMIT, "type": "commit"},
        },
    }

    async def read(self, endpoint, *, repo):
        prefix = "repos/agentdrover/home-infra/"
        assert endpoint.startswith(prefix)
        data = github_data.get(endpoint.removeprefix(prefix))
        return data, data is None

    async def contains(self, branch, sha, *, gh_repo):
        assert (branch, sha, gh_repo) == ("main", HEAD, REPOSITORY)
        return True

    monkeypatch.setattr(verify.GitHubForge, "_api_json", read)
    monkeypatch.setattr(verify.GitHubForge, "branch_contains", contains)
    return github_data


@pytest.fixture
async def card(db):
    policy = {"ci_contract": {"workflow_path": WORKFLOW, "required_checks": ["tests"]}}
    cur = await db.execute(
        "INSERT INTO projects(slug,name,repo,forge,gate_policy) VALUES ('home-infra','Home',?,'github',?)",
        (REPOSITORY, json.dumps(policy)),
    )
    await db.execute(
        "INSERT INTO tasks (id,title,status,project_id,branch,submission_generation,submission_sha,"
        "review_verdict,review_verdict_generation,auto_review) VALUES "
        "(2413,'Network pilot','review',?,?,2,?,'approved',2,0)",
        (cur.lastrowid, BRANCH, COMMIT),
    )
    await db.execute(
        "INSERT INTO ci_run_reports(task_id,head_sha,checks,validation_status,trust,contract_fp,provenance) "
        "VALUES (2413,?,?,'pass','verified',?,?)",
        (
            COMMIT,
            '{"tests":"pass"}',
            ci_contract.fingerprint_of(WORKFLOW, ["tests"]),
            json.dumps(
                {
                    "run_conclusion": "success",
                    "head_repository": REPOSITORY,
                    "workflow_path": WORKFLOW,
                }
            ),
        ),
    )
    await db.commit()
    return db


async def _verify(db, **different):
    inputs = dict(order_id=17, receipt_id=NONCE, head_sha=HEAD, commit_sha=COMMIT)
    return await verify.verify_network_receipt(db, **(inputs | different))


async def test_exact_file_trusted_ci_and_current_verdict_are_accepted(card, github):
    assert (await _verify(card)).accepted
    # Existing evidence directory may already contain other, unchanged files.
    old_dir = "1" * 40
    github[f"git/trees/{OLD}"]["tree"].append(
        _entry("evidence", "040000", "tree", old_dir)
    )
    github[f"git/trees/{old_dir}"] = _tree(old_dir, _entry("prior"))
    github[f"git/trees/{EVIDENCE}"]["tree"].append(_entry("prior"))
    assert (await _verify(card)).accepted


@pytest.mark.parametrize(
    ("path", "mutate"),
    [
        (f"git/commits/{COMMIT}", lambda obj: obj["parents"].append({"sha": NEW})),
        (f"git/commits/{COMMIT}", lambda obj: obj["parents"][0].update(sha=NEW)),
        (f"git/commits/{COMMIT}", lambda obj: obj.update(sha=NEW)),
        (f"git/trees/{OLD}", lambda obj: obj.update(truncated=True)),
        (f"git/trees/{NEW}", lambda obj: obj["tree"].append(_entry("something-else"))),
        (f"git/trees/{NEW}", lambda obj: obj["tree"].pop(0)),
        (f"git/trees/{EVIDENCE}", lambda obj: obj["tree"].append(_entry("second"))),
        (f"git/trees/{EVIDENCE}", lambda obj: obj["tree"][0].update(mode="120000")),
        (f"git/trees/{EVIDENCE}", lambda obj: obj["tree"][0].update(mode="100755")),
        (
            f"git/trees/{EVIDENCE}",
            lambda obj: obj["tree"][0].update(path="different.json"),
        ),
        (
            f"git/blobs/{BLOB}",
            lambda obj: obj.update(
                content=base64.b64encode(
                    CONTENTS.replace(b'"schema":1', b'"schema":2')
                ).decode()
            ),
        ),
        (f"git/blobs/{BLOB}", lambda obj: obj.update(size=0)),
        (f"git/ref/heads/{BRANCH}", lambda obj: obj["object"].update(sha=NEW)),
    ],
)
async def test_unproven_commit_stops_before_ci(github, path, mutate):
    mutate(github[path])
    result = await _verify(None)
    assert not result.commit_verified and not result.accepted


async def test_parent_already_contains_receipt(github):
    prior = "1" * 40
    github[f"git/trees/{OLD}"]["tree"].append(
        _entry("evidence", "040000", "tree", prior)
    )
    github[f"git/trees/{prior}"] = _tree(prior, _entry("k8-network-receipt-2413.json"))
    assert not (await _verify(None)).commit_verified


@pytest.mark.parametrize(
    "different", [{"order_id": 18}, {"receipt_id": "1" * 32}, {"head_sha": NEW}]
)
async def test_other_orders_cannot_reuse_the_same_file(github, different):
    assert not (await _verify(None, **different)).commit_verified


async def test_main_ancestry_must_be_proven(github, monkeypatch):
    monkeypatch.setattr(
        verify.GitHubForge, "branch_contains", AsyncMock(return_value=None)
    )
    result = await _verify(None)
    assert result.reason == "parent_not_confirmed_on_main"


@pytest.mark.parametrize(
    ("change", "flag"),
    [
        ("UPDATE ci_run_reports SET head_sha='other'", "ci_verified"),
        ("UPDATE ci_run_reports SET trust='unverified'", "ci_verified"),
        ("UPDATE ci_run_reports SET contract_fp='obsolete'", "ci_verified"),
        ("UPDATE ci_run_reports SET checks='{}'", "ci_verified"),
        ("UPDATE ci_run_reports SET validation_status='fail'", "ci_verified"),
        ("UPDATE ci_run_reports SET provenance='{}'", "ci_verified"),
        (
            "UPDATE projects SET repo='someone/else' WHERE slug='home-infra'",
            "commit_verified",
        ),
        (
            "UPDATE tasks SET submission_sha='other' WHERE id=2413",
            "submission_verified",
        ),
        ("UPDATE tasks SET branch='other' WHERE id=2413", "submission_verified"),
        ("UPDATE tasks SET review_verdict=NULL WHERE id=2413", "verdict_verified"),
        ("UPDATE tasks SET status='rejected' WHERE id=2413", "verdict_verified"),
        ("UPDATE tasks SET archived=1 WHERE id=2413", "verdict_verified"),
        (
            "UPDATE tasks SET review_verdict_generation=1 WHERE id=2413",
            "verdict_verified",
        ),
        (
            "UPDATE tasks SET review_verdict_closed_generation=2 WHERE id=2413",
            "verdict_verified",
        ),
    ],
)
async def test_recheck_closes_gate_when_evidence_changes(card, github, change, flag):
    first = await _verify(card)
    assert first.accepted
    await card.execute(change)
    second = await verify.recheck_network_receipt(
        card, commit_sha=COMMIT, commit_verified=first.commit_verified
    )
    assert not getattr(second, flag)
    assert not second.accepted


async def test_green_workflow_and_manual_validation_are_distinct(card, github):
    await card.execute("UPDATE ci_run_reports SET validation_status='unknown'")
    await card.execute("UPDATE tasks SET review_verdict=NULL WHERE id=2413")
    result = await _verify(card)
    assert result.ci_verified and not result.verdict_verified and not result.accepted


@pytest.mark.parametrize(
    "change",
    [
        {"head_repository": "other/home-infra"},
        {"workflow_path": ".github/workflows/other.yml"},
        {"run_conclusion": "failure"},
    ],
)
async def test_ci_requires_right_repository_workflow_and_conclusion(
    card, github, change
):
    provenance = {
        "run_conclusion": "success",
        "head_repository": REPOSITORY,
        "workflow_path": WORKFLOW,
    }
    await card.execute(
        "UPDATE ci_run_reports SET provenance=?", (json.dumps(provenance | change),)
    )
    assert not (await _verify(card)).ci_verified
