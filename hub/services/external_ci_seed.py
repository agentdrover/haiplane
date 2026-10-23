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

import hashlib
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


def _known(base_branch: str, release_branch: str, ac_runner: str) -> dict[str, str]:
    """Every file of the pair as the hub renders it now, whichever branch it is for."""
    return {
        **_rendered(base_branch, release_branch, ac_runner),
        brand.SEEDED_CI_REPORT: _sender(),
    }


def foreign_only(
    workspace: str,
    paths: list[str],
    base_branch: str,
    release_branch: str,
    ac_runner: str,
) -> list[str]:
    """``paths`` without the hub's own files (owned path and the current bytes)."""
    known = _known(base_branch, release_branch, ac_runner)
    return [path for path in paths if not _is_ours(workspace, path, known)]


def _blob_sha(text: str) -> str:
    data = text.encode("utf-8")
    return hashlib.sha1(  # nosec B324 - git's object id, not a security hash
        b"blob %d\0" % len(data) + data, usedforsecurity=False
    ).hexdigest()


def _foreign_on(
    workspace: str, branch: str, files: dict[str, str], known: dict[str, str]
) -> tuple[SeedResult | None, bool]:
    """Чужой CI на ``origin/<branch>`` (не в рабочей копии) и полна ли наша часть.

    Решение берётся из удалённой ветки после fetch: общая копия может отставать,
    и чужой workflow, уже лежащий в origin, она не покажет. Свои файлы (blob
    совпал побайтно с любым рендером пары ``known``, а не только с файлами этой
    ветки: sender, оставшийся на интеграционной ветке, тоже свой) чужими не
    считаются. ``(None, True)`` - всё из ``files`` уже лежит.
    """
    rc, out = workflow_seed._git(workspace, "fetch", "origin", branch)
    if rc != 0:
        return (
            SeedResult(
                UNAVAILABLE,
                f"could not push the workflows to {branch}: could not fetch "
                f"origin/{branch}: {out}",
            ),
            False,
        )
    rc, out = workflow_seed._git(
        workspace,
        "ls-tree",
        "-r",
        "--full-name",
        f"origin/{branch}",
        "--",
        *workflow_seed.SCANNED_WORKFLOW_DIRS,
    )
    if rc != 0:
        return (
            SeedResult(UNAVAILABLE, f"could not read origin/{branch}: {out}"),
            False,
        )
    foreign: list[str] = []
    ours: set[str] = set()
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        if not path.endswith(workflow_seed.WORKFLOW_SUFFIXES):
            continue
        name = os.path.basename(path)
        expected = known.get(name)
        blob = meta.split()[2] if len(meta.split()) > 2 else ""
        if (
            expected is not None
            and path == os.path.join(WORKFLOWS_DIR, name)
            and blob == _blob_sha(expected)
        ):
            ours.add(name)
        else:
            foreign.append(path)
    if foreign:
        return (
            SeedResult(
                PRESENT,
                f"branch {branch} already carries workflows, none added: "
                + ", ".join(sorted(foreign)),
            ),
            False,
        )
    return None, set(files) <= ours


def _publish_in_worktree(
    workspace: str,
    branch: str,
    files: dict[str, str],
    message: str,
    known: dict[str, str],
) -> SeedResult:
    """Положить ``files`` в ``origin/<branch>``, не трогая общую рабочую копию.

    Коммит делается во временном дереве на ``origin/<branch>`` и на временной
    локальной ветке; общая копия (её ветку может сменить поллер или другой
    провижининг) не читается и не пишется. Пушится ровно свой коммит полным
    refspec на ``refs/heads/<branch>`` и проверяется, что удалённая ветка стала
    именно им. Чужой CI на ветке виден до записи; свои же файлы (содержимое
    совпало побайтно) повтор не трогает и не считает чужими.
    """
    rc, out = workflow_seed._git(workspace, "fetch", "origin", branch)
    if rc != 0:
        return SeedResult(
            UNAVAILABLE,
            f"could not push the workflows to {branch}: could not fetch "
            f"origin/{branch}: {out}",
        )
    tmp = tempfile.mkdtemp(prefix="haiplane-seed-")
    local = f"hub-seed-{os.path.basename(tmp)}"
    try:
        rc, out = workflow_seed._git(
            workspace, "worktree", "add", "-b", local, tmp, f"origin/{branch}"
        )
        if rc != 0:
            return SeedResult(UNAVAILABLE, f"could not open origin/{branch}: {out}")
        try:
            return _commit_files(tmp, branch, local, files, message, known)
        finally:
            workflow_seed._git(workspace, "worktree", "remove", "--force", tmp)
            workflow_seed._git(workspace, "branch", "-D", local)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _is_ours(tree: str, path: str, known: dict[str, str]) -> bool:
    """A file the hub wrote: an owned name AND byte-for-byte a current render.

    ``known`` is the whole pair, not this branch's part: the sender left on the
    integration branch by an earlier seed is the hub's file too.

    Anything else under such a name (another ``workflows:``, a copied header) is
    somebody's file and is left alone.
    """
    name = os.path.basename(path)
    expected = known.get(name)
    if expected is None or path != os.path.join(WORKFLOWS_DIR, name):
        return False
    try:
        with open(os.path.join(tree, path), encoding="utf-8", newline="") as handle:
            return handle.read() == expected
    except OSError:
        return False


def _commit_files(
    tree: str,
    branch: str,
    local: str,
    files: dict[str, str],
    message: str,
    known: dict[str, str],
) -> SeedResult:
    present = [
        path
        for path in workflow_seed.existing_workflows(tree)
        if not _is_ours(tree, path, known)
    ]
    if present:
        return SeedResult(
            PRESENT,
            f"branch {branch} already carries workflows, none added: "
            + ", ".join(present),
        )
    directory = os.path.join(tree, WORKFLOWS_DIR)
    os.makedirs(directory, exist_ok=True)
    for name, content in files.items():
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            handle.write(content)
    paths = [os.path.join(WORKFLOWS_DIR, name) for name in sorted(files)]
    rc, out = workflow_seed._git(tree, "add", "--", *paths)
    if rc != 0:
        return SeedResult(FAILED, f"could not stage the workflows: {out}")
    rc, dirty = workflow_seed._git(tree, "status", "--porcelain")
    if rc == 0 and not dirty:  # a retry: every file is already there
        return SeedResult(SEEDED, "", tuple(sorted(files)))
    rc, out = workflow_seed._git(
        tree,
        "-c",
        f"user.name={HUB_GIT_NAME}",
        "-c",
        f"user.email={HUB_GIT_EMAIL}",
        "commit",
        "-m",
        message,
    )
    if rc != 0:
        return SeedResult(FAILED, f"could not commit the workflows: {out}")
    rc, sha = workflow_seed._git(tree, "rev-parse", "HEAD")
    rc2, out = workflow_seed._git(
        tree, "push", "origin", f"refs/heads/{local}:refs/heads/{branch}"
    )
    if rc != 0 or rc2 != 0:
        return SeedResult(
            FAILED,
            f"could not push the workflows to {branch} (needs write access and "
            f"the workflow scope): {out}",
        )
    rc, remote = workflow_seed._git(tree, "ls-remote", "origin", f"refs/heads/{branch}")
    if rc != 0 or not remote.startswith(sha):
        return SeedResult(
            FAILED,
            f"could not push the workflows to {branch}: the remote branch is not "
            f"at {sha[:12]} after the push",
        )
    return SeedResult(SEEDED, "", tuple(sorted(files)))


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
        return SeedResult(
            UNAVAILABLE,
            "the verifiable pair is written to the remote only; a local-only "
            "seed would write into the shared clone",
        )
    known = _known(base_branch, release_branch, ac_runner)
    if default == base_branch:
        files[brand.SEEDED_CI_REPORT] = _sender()
    targets = [(base_branch, files)]
    if default != base_branch:
        targets.append((default, {brand.SEEDED_CI_REPORT: _sender()}))
    complete = True
    for branch, wanted in targets:
        refusal, whole = _foreign_on(workspace, branch, wanted, known)
        if refusal is not None:
            return refusal
        complete = complete and whole
    if complete:
        return SeedResult(
            PRESENT,
            "repository already carries workflows, none added (the hub's own "
            f"pair): {STATUS}. {CHECKLIST}",
        )
    if default != base_branch:
        sent = _publish_in_worktree(
            workspace,
            default,
            {brand.SEEDED_CI_REPORT: _sender()},
            "ci: add the Haiplane CI report sender (hub provisioning, #2027)",
            known,
        )
        if sent.state != SEEDED:
            return sent
    result = _publish_in_worktree(
        workspace,
        base_branch,
        files,
        "ci: add Haiplane workflows (hub provisioning, #476)",
        known,
    )
    if result.state != SEEDED:
        return result
    names = (
        *result.written,
        *(() if default == base_branch else (brand.SEEDED_CI_REPORT,)),
    )
    return SeedResult(SEEDED, _detail(base_branch, default, read), tuple(sorted(names)))
