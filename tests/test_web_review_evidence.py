from __future__ import annotations

from httpx import AsyncClient

from tests.web_support import (
    _form_carries_policy_version,  # noqa: F401
    _machine_report,
    _web_project_tokens,
    _web_task_in_review,
    _web_task_in_review_with_test_ac,
)


async def test_absent_review_is_stated_at_the_verdict_gate(client: AsyncClient):
    # AC-1 (#808): no report for this submission is a fact to state, not an
    # empty panel to interpret. Silence here used to read as "no objections".
    task_id = await _web_task_in_review(client)

    resp = await client.get(f"/tasks/{task_id}")

    assert resp.status_code == 200
    assert "Ревью за эту сдачу не проводилось" in resp.text
    assert "без независимой проверки" in resp.text


async def test_report_header_names_what_was_reviewed(client: AsyncClient):
    # AC-2 (#808): the header answers "what exactly was looked at" — branch,
    # commit, volume, model — so the reader knows the scope of the claim.
    task_id = await _web_task_in_review(client)
    await _machine_report(client, task_id)

    resp = await client.get(f"/tasks/{task_id}")

    assert "Проверялось:" in resp.text
    assert f"task-{task_id}/" in resp.text
    assert "grok-4.6" in resp.text
    assert "Ревью за эту сдачу не проводилось" not in resp.text


async def test_rejected_findings_are_readable_with_reasons(client: AsyncClient):
    # AC-3 (#808): half the reviewer's work is what it decided NOT to raise.
    # A counter alone cannot show whether it engaged or waved things away.
    task_id = await _web_task_in_review(client)
    await _machine_report(client, task_id)

    resp = await client.get(f"/tasks/{task_id}")

    assert "rejected findings" in resp.text
    assert "style nit" in resp.text
    assert "не дефект" in resp.text


async def test_evidence_panel_renders_at_the_verdict_gate(client: AsyncClient, db):
    # AC-1 (#823): every block below was already computed for the review brief
    # and reached only the agent calling it. The human deciding the verdict saw
    # the reviewer's report and the implementer's prose — nothing about whether
    # the ordered criteria had been checked.
    task_id = await _web_task_in_review_with_test_ac(client, db)

    resp = await client.get(f"/tasks/{task_id}")

    assert resp.status_code == 200
    assert "Проверено по этой сдаче:" in resp.text
    assert "AC-1" in resp.text, "per-AC result must be named at the gate"
    assert "CI по закреплённому коммиту:" in resp.text
    assert "Свежесть постановки:" in resp.text


async def test_missing_evidence_names_its_cause(client: AsyncClient):
    # AC-2 (#823): a check that could not run says so with its cause. Silence
    # in this panel would read as "checked, nothing found" — the half-truth
    # #549 closed for "0 confirmed" and #725 for the brief's blocks.
    task_id = await _web_task_in_review(client)

    resp = await client.get(f"/tasks/{task_id}")
    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    assert brief["evidence_coverage"]["state"] in ("partial", "none")
    assert brief["evidence_coverage"]["checks_missing"], "the fixture must have gaps"
    assert "не дали сигнала" in resp.text
    for check in brief["evidence_coverage"]["checks_missing"]:
        assert check["reason"], f"{check['check']} went silent without a cause"


async def test_implementer_summary_is_secondary_to_evidence(client: AsyncClient, db):
    # AC-4 (#823): the submission text is a statement by the party being
    # judged. It stays available, but below the evidence — while it was read
    # first, writing convincingly paid better than checking carefully.
    task_id = await _web_task_in_review_with_test_ac(client, db)

    resp = await client.get(f"/tasks/{task_id}")

    assert "что говорит исполнитель" in resp.text
    evidence_at = resp.text.index("Проверено по этой сдаче:")
    summary_at = resp.text.index("что говорит исполнитель")
    verdict_at = resp.text.index("web-review-verdict")
    assert evidence_at < summary_at < verdict_at, (
        "evidence must come before the implementer's own account"
    )


# ---- #824: the diff of the pinned submission, read inside the hub ----


def _diff_repo(tmp_path) -> tuple[str, str, str]:
    """A repo whose branch moved AFTER the submission was pinned.

    Returns ``(path, submitted_sha, later_sha)``. The two commits touch
    different files so the diff of one is recognisably not the diff of both.
    """
    import subprocess

    root = tmp_path / "diffrepo"
    root.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(root),
    }

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, env=env
        )

    git("init", "-b", "main")
    (root / "base.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-m", "base")
    git("checkout", "-b", "task-x/work")
    (root / "submitted.py").write_text(
        "def submitted():\n    return 'in the verdict'\n"
    )
    git("add", ".")
    git("commit", "-m", "submitted")
    submitted = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    (root / "afterwards.py").write_text(
        "def afterwards():\n    return 'not approved'\n"
    )
    git("add", ".")
    git("commit", "-m", "after the submission")
    later = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    return str(root), submitted, later


async def _task_pinned_at(client: AsyncClient, db, sha: str, branch: str) -> int:
    from hub import repository as repo_module

    task_id = await _web_task_in_review(client)
    await repo_module.update_task(db, task_id, branch=branch, submission_sha=sha)
    await db.commit()
    return task_id


def _use_real_git(monkeypatch, workspace: str) -> None:
    from unittest.mock import AsyncMock

    from hub import app as hub_app
    from hub.integrations.git_ops import GitOpsIntegration
    from hub.integrations.registry import plugins

    real = GitOpsIntegration()
    context = AsyncMock(return_value={"repo": workspace, "base_branch": "main"})
    # Two patch points on purpose: callers reach this helper both through the
    # services package and by importing it from orchestration directly, and the
    # diff base is resolved through the second one.
    monkeypatch.setattr(hub_app.services, "project_git_context", context)
    monkeypatch.setattr(
        "hub.services.orchestration.project_git_context", context, raising=False
    )
    for name in ("commit_exists", "commit_diff", "resolve_ref"):
        monkeypatch.setattr(plugins.git_ops, name, getattr(real, name), raising=False)


async def test_diff_is_served_for_the_pinned_submission(
    client: AsyncClient, db, tmp_path, monkeypatch
):
    # AC-1 (#824): the verdict is cast on one submission. A branch that moved
    # after it must not be what the reader sees — `sha_check` warns about the
    # move (#572), and showing the moved branch's diff would contradict it.
    workspace, submitted, _later = _diff_repo(tmp_path)
    _use_real_git(monkeypatch, workspace)
    task_id = await _task_pinned_at(client, db, submitted, "task-x/work")

    resp = await client.get(f"/tasks/{task_id}/diff")

    assert resp.status_code == 200
    assert "submitted.py" in resp.text
    assert "afterwards.py" not in resp.text, (
        "work committed after the submission is not what the verdict covers"
    )


async def test_truncated_diff_says_it_was_truncated(
    client: AsyncClient, db, tmp_path, monkeypatch
):
    # AC-2 (#824): a silently cut diff reads as the whole change while being a
    # fragment — the most expensive kind of half-truth at a gate.
    from hub import config as config_module

    workspace, submitted, _later = _diff_repo(tmp_path)
    _use_real_git(monkeypatch, workspace)
    monkeypatch.setattr(config_module, "DIFF_MAX_LINES", 1)
    task_id = await _task_pinned_at(client, db, submitted, "task-x/work")

    resp = await client.get(f"/tasks/{task_id}/diff")

    assert "показано не всё" in resp.text
    assert "потолок показа" in resp.text
    assert "git diff" in resp.text, "a truncated diff must hand over the command"


async def test_unreachable_sha_is_named_not_silent(
    client: AsyncClient, db, tmp_path, monkeypatch
):
    # AC-3 (#824): "that commit is not here" and "nothing changed" are
    # different answers. Collapsing them is how a gate reads as clean.
    workspace, _submitted, _later = _diff_repo(tmp_path)
    _use_real_git(monkeypatch, workspace)
    # A well-formed object name that this repository does not carry. Built from
    # a non-hex marker so the CI secret scanner has nothing to match (#813).
    absent = "".join("abcdef0123456789"[(i * 7) % 16] for i in range(40))
    task_id = await _task_pinned_at(client, db, absent, "task-x/work")

    resp = await client.get(f"/tasks/{task_id}/diff")

    assert "Дифф не показан" in resp.text
    assert "нет в рабочей копии" in resp.text
    assert "не «изменений нет»" in resp.text


async def test_diff_requires_the_same_auth_as_the_card(
    client: AsyncClient, db, tmp_path, monkeypatch
):
    # AC-4 (#824): this route serves source code. It is covered by the same
    # middleware as every other page — the test holds that fact rather than
    # trusting it.
    from hub import config as config_module

    workspace, submitted, _later = _diff_repo(tmp_path)
    _use_real_git(monkeypatch, workspace)
    task_id = await _task_pinned_at(client, db, submitted, "task-x/work")

    monkeypatch.setattr(config_module, "HUB_TOKENS", {"tok": "someone"})
    monkeypatch.setattr(config_module, "HUB_AUTH_DISABLED", False)
    resp = await client.get(
        f"/tasks/{task_id}/diff",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.status_code
    assert "submitted.py" not in resp.text


# ---- #825: criteria laid against the changes, and the changes nobody ordered ----


def _mapped_repo(tmp_path) -> tuple[str, str]:
    """A submission touching one file of each kind the map must separate."""
    import subprocess

    root = tmp_path / "maprepo"
    root.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(root),
    }

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, env=env
        )

    git("init", "-b", "main")
    (root / "seed.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-m", "base")
    git("checkout", "-b", "task-map/work")
    (root / "tests").mkdir()
    (root / "tests" / "test_bound.py").write_text(
        "def test_bound():\n    assert True\n"
    )
    (root / "declared.py").write_text("def declared():\n    return 'asked for'\n")
    (root / "stray.py").write_text("def stray():\n    return 'nobody asked'\n")
    git("add", ".")
    git("commit", "-m", "submission")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    return str(root), sha


async def _mapped_task(
    client: AsyncClient, db, monkeypatch, tmp_path, *, test_ref: str
) -> int:
    from hub import repository as repo_module

    workspace, sha = _mapped_repo(tmp_path)
    _use_real_git(monkeypatch, workspace)
    from hub.integrations.git_ops import GitOpsIntegration
    from hub.integrations.registry import plugins

    monkeypatch.setattr(
        plugins.git_ops,
        "commit_diff_stat",
        GitOpsIntegration().commit_diff_stat,
        raising=False,
    )

    task_id = await _web_task_in_review(client)
    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "affected_areas": ["declared.py"],
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "given": "сдача с изменениями",
                    "when": "читается панель",
                    "then": "критерий стоит рядом со своими изменениями",
                    "verifiable_by": "test",
                    "test_ref": test_ref,
                }
            ],
        },
    )
    row = await repo_module.get_task(db, task_id)
    await repo_module.upsert_ac_test_result(
        db,
        task_id=task_id,
        ac_id="AC-1",
        generation=dict(row)["submission_generation"],
        status="pass",
    )
    await repo_module.update_task(
        db, task_id, branch="task-map/work", submission_sha=sha
    )
    await db.commit()
    return task_id


async def test_criteria_are_bound_to_their_changes(
    client: AsyncClient, db, tmp_path, monkeypatch
):
    # AC-1 (#825): the criterion and the change it caused, in one place. While
    # they sat in two lists, joining them was the reader's job — the expensive
    # half of a review, and the half that gets skipped.
    task_id = await _mapped_task(
        client, db, monkeypatch, tmp_path, test_ref="tests/test_bound.py::test_bound"
    )

    resp = await client.get(f"/tasks/{task_id}")

    assert "изменения критерия:" in resp.text
    assert "tests/test_bound.py" in resp.text
    assert "точная привязка" in resp.text


async def test_green_criterion_without_changes_is_called_out(
    client: AsyncClient, db, tmp_path, monkeypatch
):
    # AC-2 (#825): a tick with nothing behind it in this submission. Today it
    # reads as done and passes the gate unnoticed.
    task_id = await _mapped_task(
        client,
        db,
        monkeypatch,
        tmp_path,
        test_ref="tests/test_absent.py::test_absent",
    )

    resp = await client.get(f"/tasks/{task_id}")

    assert "тест зелёный, изменений критерия нет" in resp.text
    assert "не менялся" in resp.text


async def test_unattributed_changes_are_listed(
    client: AsyncClient, db, tmp_path, monkeypatch
):
    # AC-3 (#825): the reverse question. Nothing marks these today, and this is
    # where the risk that survives review lives.
    task_id = await _mapped_task(
        client, db, monkeypatch, tmp_path, test_ref="tests/test_bound.py::test_bound"
    )

    resp = await client.get(f"/tasks/{task_id}")

    assert "Изменения, которых никто не просил:" in resp.text
    assert "stray.py" in resp.text
    assert "вне объявленных областей" in resp.text


async def test_weak_attribution_is_marked_as_such(
    client: AsyncClient, db, tmp_path, monkeypatch
):
    # AC-4 (#825): "declared in the task's areas" is not "verified by this
    # criterion". A confident wrong attribution is worse than an absent one —
    # it puts a tick where the reader would otherwise look.
    task_id = await _mapped_task(
        client, db, monkeypatch, tmp_path, test_ref="tests/test_bound.py::test_bound"
    )

    resp = await client.get(f"/tasks/{task_id}")

    assert "предположительно по объявленным областям" in resp.text
    assert "declared.py" in resp.text
    assert "к конкретному критерию не привязана" in resp.text
    # The weak bucket must not be dressed up as the exact one.
    weak_at = resp.text.index("предположительно по объявленным областям")
    exact_at = resp.text.index("точная привязка")
    assert exact_at < weak_at


# ---- #826: a finding that knows where it lives ----


async def test_verdict_finding_carries_file_and_line(client: AsyncClient):
    # AC-1 (#826): the address the reviewer clicked survives into the record.
    # Without it the implementer searches for the place by description.
    task_id = await _web_task_in_review(client)

    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={
            "verdict": "changes_requested",
            "comments": "смотри строку",
            "findings_text": "high: гонка в bump @ hub/services/lifecycle.py:412",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    finding = body["latest_review"]["findings"][0]
    assert finding["file"] == "hub/services/lifecycle.py"
    assert finding["line"] == 412
    assert finding["message"] == "гонка в bump", "the address must leave the text"


async def test_unanchored_finding_is_still_accepted(client: AsyncClient):
    # AC-2 (#826): anchoring stays optional. Made mandatory it would be
    # answered with a click on whatever line was under the cursor, and a
    # made-up address is worse than an honest none.
    task_id = await _web_task_in_review(client)

    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={
            "verdict": "changes_requested",
            "comments": "",
            "findings_text": "low: опечатка в докстринге\nмысль без severity и без адреса",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    findings = (await client.get(f"/api/tasks/{task_id}")).json()["latest_review"][
        "findings"
    ]
    assert [f["message"] for f in findings] == [
        "опечатка в докстринге",
        "мысль без severity и без адреса",
    ]
    assert all(f["file"] == "" and f["line"] is None for f in findings)


async def test_anchored_findings_reach_the_implementer(client: AsyncClient):
    # AC-3 (#826): the address is only worth capturing if it travels. The card
    # is where the implementer reads the verdict back.
    task_id = await _web_task_in_review(client)
    await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={
            "verdict": "changes_requested",
            "comments": "",
            "findings_text": "high: тут падает @ hub/web.py:77",
        },
        follow_redirects=False,
    )

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    finding = body["latest_review"]["findings"][0]
    page = (await client.get(f"/tasks/{task_id}")).text

    # Structured, not merely present in the prose: before #826 the address
    # travelled as part of the message text, which reads the same on the card
    # and is useless to anything that wants to open the file.
    assert (finding["file"], finding["line"]) == ("hub/web.py", 77)
    assert "@" not in finding["message"]
    assert '<code class="code-sm">hub/web.py:77</code>' in page


async def test_diff_lines_carry_their_new_side_address(
    client: AsyncClient, db, tmp_path, monkeypatch
):
    # #826: the click has to have something to read. A removed line keeps no
    # number — pointing at a neighbour's address would be a wrong address.
    workspace, submitted, _later = _diff_repo(tmp_path)
    _use_real_git(monkeypatch, workspace)
    task_id = await _task_pinned_at(client, db, submitted, "task-x/work")

    resp = await client.get(f"/tasks/{task_id}/diff")

    assert 'data-file="submitted.py"' in resp.text
    assert 'data-line="1"' in resp.text
    assert "task-diff-line--anchorable" in resp.text


# --- What the finding turned out to be (#876, feature #871) ------------------
#
# Review quality used to be measured by the review itself: findings_confirmed
# against findings_rejected is one run's own adjudication. Nobody recorded what
# happened to a finding after the gate, so precision by profile and by model
# could not be computed at all.

_TWO_FINDINGS = [
    {
        "title": "boundary lost",
        "severity": "medium",
        "file": "hub/a.py",
        "locator": "file",
    },
    {
        "title": "race on retry",
        "severity": "high",
        "file": "hub/b.py",
        "locator": "file",
    },
]


async def test_finding_disposition_saved_at_gate(client: AsyncClient, db):
    # AC-1 (#876): the gate marks each confirmed finding, and the judgement is
    # stored against the report and its generation, then read back on the card.
    from hub import repository as repo_module

    task_id = await _web_task_in_review(client)
    await _machine_report(
        client, task_id, findings_confirmed=_TWO_FINDINGS, raw_count=3
    )

    page = await client.get(f"/tasks/{task_id}")
    assert "судьба не названа" in page.text, "an unjudged finding must say so"
    assert "размечено 0 из 2" in page.text

    resp = await client.post(
        f"/tasks/{task_id}/web-finding-dispositions",
        data={
            "disposition-0": "fixed",
            "disposition-1": "false_positive",
            "note-1": "такого пути в коде нет",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    review = await repo_module.get_latest_machine_review(db, task_id)
    rows = [
        dict(r) for r in await repo_module.list_finding_dispositions(db, review["id"])
    ]
    assert [r["disposition"] for r in rows] == ["fixed", "false_positive"]
    assert rows[0]["finding_title"] == "boundary lost", "the title travels with it"
    assert rows[1]["note"] == "такого пути в коде нет"
    assert all(
        r["submission_generation"] == review["submission_generation"] for r in rows
    )

    page = await client.get(f"/tasks/{task_id}")
    assert "размечено 2 из 2" in page.text
    assert "судьба не названа" not in page.text


async def test_partial_judgement_stays_partial(client: AsyncClient, db):
    # A finding left alone is NOT judged. A default here would make every
    # report look fully reviewed the moment anyone touched the form (#549).
    from hub import repository as repo_module

    task_id = await _web_task_in_review(client)
    await _machine_report(
        client, task_id, findings_confirmed=_TWO_FINDINGS, raw_count=3
    )

    await client.post(
        f"/tasks/{task_id}/web-finding-dispositions",
        data={"disposition-0": "wont_fix"},
        follow_redirects=False,
    )

    review = await repo_module.get_latest_machine_review(db, task_id)
    rows = await repo_module.list_finding_dispositions(db, review["id"])
    assert len(rows) == 1 and dict(rows[0])["finding_index"] == 0
    page = await client.get(f"/tasks/{task_id}")
    assert "размечено 1 из 2" in page.text
    assert "судьба не названа" in page.text


async def test_card_agrees_on_a_finding_an_observation_closed(client: AsyncClient, db):
    # #1244 (review finding): the #1012 badge already discounts a finding a
    # foreign live check closed by name; the summary and the finding's own row
    # must say the same, or one card gives two answers to one question.
    # Counted SEPARATELY from dispositions: a closure by observation is not a
    # judgement (#876 stays human), so folding it into "размечено" would claim
    # somebody judged a finding nobody did.
    from hub import repository as repo_module
    from hub.services.finding_identity import finding_uids

    task_id = await _web_task_in_review(client)
    await _machine_report(
        client, task_id, findings_confirmed=_TWO_FINDINGS, raw_count=3
    )
    review = await repo_module.get_latest_machine_review(db, task_id)
    await db.execute(
        "UPDATE tasks SET implementer_principal_id = 7 WHERE id = ?", (task_id,)
    )
    await db.execute(
        "UPDATE machine_reviews SET created_at = '2026-09-09 12:00:00' WHERE id = ?",
        (review["id"],),
    )
    uid = finding_uids(_TWO_FINDINGS)[1]
    check_id = await repo_module.insert_live_check(
        db,
        task_id=task_id,
        sha="",
        outcome="done",
        probe="GET /v1/agents",
        observation=f"находка {uid}: не воспроизводится",
        recorded_by=8,
        recorded_agent="someone",
    )
    await db.execute(
        "UPDATE live_checks SET created_at = '2026-09-09 13:00:00' WHERE id = ?",
        (check_id,),
    )
    await db.commit()

    page = (await client.get(f"/tasks/{task_id}")).text

    assert "1 из 2 находок без диспозиции" in page
    assert "размечено 0, закрыто наблюдением 1 из 2" in page
    assert f"закрыта наблюдением: живая проверка #{check_id}" in page
    # Only the other finding is still without a fate.
    assert page.count("судьба не названа") == 1


async def test_card_names_a_repeat_of_a_deferred_finding(client: AsyncClient, db):
    # #1448: «не блокирует» не значит «спрятано». Повтор находки, отложенной
    # до недоставленной задачи, назван на строке самой находки; соседняя
    # находка метки не получает.
    from hub import repository as repo_module
    from hub.services.finding_identity import finding_uids

    task_id = await _web_task_in_review(client)
    follow_up = await repo_module.create_task(
        db,
        title="сюда отложено",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="open",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    prior = await repo_module.insert_machine_review(
        db, task_id=task_id, submission_generation=0, incomplete=False
    )
    await repo_module.upsert_finding_outcome(
        db,
        review_id=prior,
        task_id=task_id,
        submission_generation=0,
        finding_uid=finding_uids(_TWO_FINDINGS)[0],
        finding_index=0,
        finding_title=_TWO_FINDINGS[0]["title"],
        outcome="deferred",
        note="",
        linked_task_id=follow_up,
        reported_by="pda_claude",
    )
    await db.commit()
    await _machine_report(
        client, task_id, findings_confirmed=_TWO_FINDINGS, raw_count=3
    )

    page = (await client.get(f"/tasks/{task_id}")).text

    assert "отложена до" in page and f"#{follow_up}</a>, повтор не блокирует" in page
    assert page.count("повтор не блокирует") == 1


async def test_disposition_is_corrected_not_duplicated(client: AsyncClient, db):
    # A gate that changes its mind corrects the row instead of leaving two
    # contradictory ones for the metrics to average.
    from hub import repository as repo_module

    task_id = await _web_task_in_review(client)
    await _machine_report(
        client, task_id, findings_confirmed=_TWO_FINDINGS, raw_count=3
    )

    for value in ("fixed", "wont_fix"):
        await client.post(
            f"/tasks/{task_id}/web-finding-dispositions",
            data={"disposition-0": value},
            follow_redirects=False,
        )

    review = await repo_module.get_latest_machine_review(db, task_id)
    rows = [
        dict(r) for r in await repo_module.list_finding_dispositions(db, review["id"])
    ]
    assert len(rows) == 1 and rows[0]["disposition"] == "wont_fix"


async def test_web_disposition_route_rejects_agent_token(
    client: AsyncClient, monkeypatch, db
):
    # The card path is guarded by require_human_or_admin like every other
    # human gate: the reviewed party must not judge its reviewer.
    from hub import config
    from hub import repository as repo_module

    task_id = await _web_task_in_review(client)
    await _machine_report(
        client, task_id, findings_confirmed=_TWO_FINDINGS, raw_count=3
    )
    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        f"/tasks/{task_id}/web-finding-dispositions",
        data={"disposition-0": "false_positive"},
        headers={"Authorization": "Bearer agent-token"},
        follow_redirects=False,
    )

    assert resp.status_code == 403
    review = await repo_module.get_latest_machine_review(db, task_id)
    assert not await repo_module.list_finding_dispositions(db, review["id"])


async def test_task_card_shows_review_runs_and_their_provider_cost(
    client: AsyncClient, db
):
    # AC-3 (#893): the card counts RUNS and prices each one by the provider's
    # bill. The run is the unit that tracks spend — none billed under 777k,
    # and the size of the diff explained none of the spread — so a task
    # reviewed twice cost two entry prices, which a single total would hide.
    from hub import repository as repo_module

    task_id = await _web_task_in_review(client)
    await _machine_report(client, task_id, tokens_spent=25_000)
    await repo_module.set_machine_review_provider_tokens(db, task_id, 1, 777_389)

    page = await client.get(f"/tasks/{task_id}")

    assert "Прогонов ревью: 1" in page.text
    assert "777 389" in page.text, "the bill is shown, not the self-report alone"
    assert "25 000" in page.text, "and the self-report stays visible beside it"


async def test_a_carried_report_is_not_a_review_run_on_the_card(
    client: AsyncClient, db
):
    # Находка deep-ревью #1361: после пересдачи «только слита база» в новом
    # поколении лежит перенесённый отчёт, и карточка считала его прогоном с
    # неизвестным счётом — «Прогонов ревью: 2», «счёт есть у 1 из 2». Прогона
    # не было, и денег он не стоил: перенос показывается своей строкой.
    from hub import repository as repo_module

    task_id = await _web_task_in_review(client)
    await _machine_report(client, task_id, tokens_spent=25_000)
    await repo_module.set_machine_review_provider_tokens(db, task_id, 1, 777_389)
    source = dict(await repo_module.get_latest_machine_review(db, task_id))
    await repo_module.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=2,
        harness_skill=source["harness_skill"],
        model=source["model"],
        raw_count=int(source["raw_count"] or 0),
        carried_from_review_id=int(source["id"]),
    )
    await db.commit()

    page = await client.get(f"/tasks/{task_id}")

    assert "Прогонов ревью: 1" in page.text, "перенос — не прогон"
    assert "счёт есть у 1 из 2" not in page.text
    assert "перенесён с поколения 1" in page.text, (
        "перенос виден отдельной строкой и называет источник"
    )


async def test_unbilled_review_run_reads_as_unknown_not_free(client: AsyncClient, db):
    # A run whose bill never arrived is an unknown cost, not a free one.
    # Printing nothing (or a zero) would make the runs we failed to measure
    # look like the cheap ones — the substitution #725 exists to stop.
    task_id = await _web_task_in_review(client)
    await _machine_report(client, task_id, tokens_spent=31_000)

    page = await client.get(f"/tasks/{task_id}")

    assert "Прогонов ревью: 1" in page.text
    assert "счёт провайдера неизвестен" in page.text
