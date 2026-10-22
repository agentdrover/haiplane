"""Провижининг нового GitHub-проекта кладёт пару, по которой отчёт подтверждается (#2027).

Прежний посев клал один ``haiplane-ci.yml`` с прямой отправкой ключом записи. Такой
отчёт по #1687/#1848 не подтверждается никогда (no_provenance), а контракт CI по
умолчанию ждёт ``ci.yml``: проект навсегда ``unverified``, стюард ждёт потолок
окна на каждой сдаче. Подтверждаемый путь (docs/satellite-ci-report.md) состоит
из двух файлов, и у них разные ветки:

* PRODUCER ``haiplane-ci.yml`` (name «Haiplane CI») - прогон тестов, лежит в
  интеграционной ветке вместе с ``haiplane-stale.yml``;
* SENDER ``haiplane-ci-report.yml`` - ``workflow_run`` читается GitHub из ЛЮБОЙ
  ветки только по default-ветке репозитория, поэтому он кладётся туда, даже если
  интеграционная ветка другая (develop против main).

Посеянная пара НЕ считается готовым CI: environment ``hub-report``, ключи и
``ci_contract`` делает владелец, хаб этого не создаёт. Поэтому деталь провижининга
называет оба файла и статус «ожидает настройки владельцем» с чек-листом.

Только новые GitHub-проекты без своих workflow (проверяется и интеграционная, и
default-ветка). GitVerse и существующие проекты не меняются. Модуль не бросает
исключений наружу: каждый отказ - состояние и фраза (контракт workflow_seed).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile

from hub import brand
from hub.integrations.proc import HUB_GIT_EMAIL, HUB_GIT_NAME
from hub.services import satellite_ci, workflow_seed
from hub.services.workflow_seed import (
    FAILED,
    PRESENT,
    SEEDED,
    UNAVAILABLE,
    WORKFLOWS_DIR,
    SeedResult,
)

#: Имя producer-а: оно же ``workflows:`` у sender-а и ``name:`` в файле.
PRODUCER_NAME = "Haiplane CI"
PRODUCER_PATH = f"{WORKFLOWS_DIR.replace(os.sep, '/')}/{brand.SEEDED_CI}"

STATUS = "ожидает настройки владельцем"
CHECKLIST = (
    "чек-лист: (1) ключ ci_reader со scope project; (2) ключ ci_runner только в "
    "environment hub-report, деплой только из default-ветки; (3) старый секрет "
    "записи удалить из репозитория; (4) ci_contract задаёт человек через PATCH, "
    f"например {{{brand.SEEDED_CI}, [tests]}}, и добавить шаг id: tests; "
    "инструкция docs/satellite-ci-report.md"
)


def default_branch(workspace: str, fallback: str) -> tuple[str, bool]:
    """``(default-ветка origin, прочитана ли)``; не прочиталась - ``fallback``, False."""
    rc, out = workflow_seed._git(workspace, "ls-remote", "--symref", "origin", "HEAD")
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("ref: refs/heads/"):
                name = line.split("\t", 1)[0].removeprefix("ref: refs/heads/")
                if name:
                    return name, True
    return fallback, False


def _rendered(base: str, release: str, ac_runner: str) -> dict[str, str]:
    """``{producer, stale}`` для интеграционной ветки."""
    producer = satellite_ci.render_producer(
        PRODUCER_NAME,
        PRODUCER_PATH,
        push_branches=json.dumps(workflow_seed.push_branches(base, release)),
        ac_runner=(ac_runner or "").strip() or workflow_seed.DEFAULT_AC_RUNNER,
    )
    stale = workflow_seed.render(
        "stale.yml", base_branch=base, release_branch=release, ac_runner=ac_runner
    )
    return {brand.SEEDED_CI: producer, brand.SEEDED_STALE: stale}


def _sender() -> str:
    return satellite_ci.render_sender(PRODUCER_NAME, PRODUCER_PATH)


def _push_to_default(workspace: str, default: str, sender: str) -> SeedResult:
    """Положить sender в default-ветку origin, не трогая ветку рабочей копии.

    Через временное дерево на ``origin/<default>``: рабочая копия остаётся на
    интеграционной ветке (её могут использовать), а чужой CI на default-ветке
    виден до записи.
    """
    rc, out = workflow_seed._git(workspace, "fetch", "origin", default)
    if rc != 0:
        return SeedResult(UNAVAILABLE, f"could not fetch origin/{default}: {out}")
    tmp = tempfile.mkdtemp(prefix="haiplane-seed-")
    try:
        rc, out = workflow_seed._git(
            workspace, "worktree", "add", "--detach", tmp, f"origin/{default}"
        )
        if rc != 0:
            return SeedResult(UNAVAILABLE, f"could not open origin/{default}: {out}")
        try:
            return _commit_sender(tmp, default, sender)
        finally:
            workflow_seed._git(workspace, "worktree", "remove", "--force", tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _commit_sender(tree: str, default: str, sender: str) -> SeedResult:
    present = workflow_seed.existing_workflows(tree)
    if present:
        return SeedResult(
            PRESENT,
            f"default branch {default} already carries workflows, none added: "
            + ", ".join(present),
        )
    directory = os.path.join(tree, WORKFLOWS_DIR)
    os.makedirs(directory, exist_ok=True)
    with open(
        os.path.join(directory, brand.SEEDED_CI_REPORT), "w", encoding="utf-8"
    ) as f:
        f.write(sender)
    path = os.path.join(WORKFLOWS_DIR, brand.SEEDED_CI_REPORT)
    rc, out = workflow_seed._git(tree, "add", "--", path)
    if rc == 0:
        rc, out = workflow_seed._git(
            tree,
            "-c",
            f"user.name={HUB_GIT_NAME}",
            "-c",
            f"user.email={HUB_GIT_EMAIL}",
            "commit",
            "-m",
            "ci: add the Haiplane CI report sender (hub provisioning, #2027)",
        )
    if rc != 0:
        return SeedResult(FAILED, f"could not commit the sender: {out}")
    rc, out = workflow_seed._git(tree, "push", "origin", f"HEAD:refs/heads/{default}")
    if rc != 0:
        return SeedResult(
            FAILED,
            f"could not push the sender to {default} (needs write access and the "
            f"workflow scope): {out}",
        )
    return SeedResult(SEEDED, "", (brand.SEEDED_CI_REPORT,))


def _detail(base: str, default: str, read: bool) -> str:
    where = (
        f"{default}/{WORKFLOWS_DIR}: {brand.SEEDED_CI_REPORT} (sender)"
        if default != base
        else f"{base}: {brand.SEEDED_CI_REPORT} (sender)"
    )
    guess = "" if read else f" (default-ветка не прочитана, взята {default})"
    return (
        f"{STATUS}: CI не считается готовым. Положено в {base}/{WORKFLOWS_DIR}: "
        f"{brand.SEEDED_CI} (producer, «{PRODUCER_NAME}»), {brand.SEEDED_STALE}; в "
        f"{where}{guess}. {CHECKLIST}"
    )


def seed_pair(
    workspace: str,
    *,
    base_branch: str,
    release_branch: str,
    ac_runner: str,
    push: bool,
) -> SeedResult:
    """Положить producer+stale в ``base_branch`` и sender в default-ветку."""
    refusal = workflow_seed.preflight(workspace, base_branch)
    if refusal is not None:
        return refusal
    files = _rendered(base_branch, release_branch, ac_runner)
    default, read = default_branch(workspace, base_branch)
    if not push:
        files[brand.SEEDED_CI_REPORT] = _sender()
        return workflow_seed.publish_files(
            workspace, base_branch, WORKFLOWS_DIR, files, push=False
        )
    if default == base_branch:
        files[brand.SEEDED_CI_REPORT] = _sender()
    else:
        sent = _push_to_default(workspace, default, _sender())
        if sent.state != SEEDED:
            return sent
    result = workflow_seed.publish_files(
        workspace, base_branch, WORKFLOWS_DIR, files, push=True
    )
    if result.state != SEEDED:
        return result
    names = (
        *result.written,
        *(() if default == base_branch else (brand.SEEDED_CI_REPORT,)),
    )
    return SeedResult(SEEDED, _detail(base_branch, default, read), tuple(sorted(names)))
