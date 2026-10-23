"""``hp-hub worktree purge`` и ``restore`` - вторая половина карантина (#2038).

``prune --apply`` переносит копию в ``<корень-копий>/.trash/task-N-<UTC>`` (``git
worktree move``), пишет маркер в её gitdir и ставит защитные refs в клоне (см.
``cli_worktree_trash``). Удаление необратимо, поэтому его делает только ``purge``
и только по доказательству, а не по имени каталога. Копия удаляется, если:

* путь лежит прямо в ``.trash`` и не симлинк, имя ``task-N-<UTC yyyymmddThhmmss>``;
* маркер есть, разбирается, его id задачи и имя совпадают с именем каталога, а
  время переноса - с временем в имени (допуск 2 минуты);
* HEAD в маркере равен текущему HEAD копии;
* возраст по времени МАРКЕРА не меньше ``--older-than-days`` (>= 1, конечное);
* время маркера не из будущего (допуск 5 минут).

Всё иное - «оставить» с названной причиной; сбой маркера, HEAD или времени даёт
ненулевой код возврата. Без ``--apply`` печатается список с возрастом. Удаление -
``git worktree remove --force``, после удачи снимаются защитные refs; неудачное
удаление оставляет refs.

``restore <имя>``: (a) канонического пути не должно быть; (b) ``git worktree move``
копии на канонический путь; (c) ``git switch <ветка из маркера>`` в ней; (d) снятие
refs ``refs/haiplane-trash/<имя>/*`` и маркера. Любая проверка (a) или сбой шага
останавливают восстановление с причиной и ненулевым кодом.

Охват. Модель угроз - случайная потеря работы кооперативными исполнителями. Процесс
с тем же uid, который хочет уничтожить работу (подделать маркер), не покрыт: он и
так может удалить копию сам (решение по #2038, раунд 6).
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from calendar import timegm
from typing import Any, Callable

from hub import cli_worktree_trash as trash
from hub.cli_worktree_prune import (
    TRASH_DIR,
    Entry,
    _git,
    _parse_porcelain,
    _real,
    trash_root,
)

_TRASH_NAME = re.compile(r"^task-(\d+)-(\d{8}T\d{6})(?:-\d+)?$")
_DAY = 86400.0
NAME_SKEW_S = 120  # время в имени против времени маркера
FUTURE_SKEW_S = 300  # время из будущего дальше этого - подозрительно


def _name_epoch(stamp: str) -> int | None:
    try:
        return timegm(time.strptime(stamp, "%Y%m%dT%H%M%S"))
    except ValueError:
        return None


def _structure(entry: Entry, tdir: str) -> tuple[str, str, re.Match[str] | None]:
    """(действие, текст, совпадение имени): skip | keep | proof (нужна проверка маркера)."""
    path = os.path.abspath(entry.path)
    name = os.path.basename(path)
    match = _TRASH_NAME.match(name)
    inside = _real(os.path.dirname(path)) == tdir
    if not match and not inside:
        return "skip", "", None
    if not inside:
        return (
            "keep",
            f"имя как у карантина, но путь вне {tdir}: purge не принимает",
            None,
        )
    if not match:
        return "keep", f"имя не по шаблону task-N-<UTC>: {name}", None
    if os.path.islink(path):
        return "keep", "симлинк: purge не принимает", None
    return "proof", "", match


def _marker_problem(
    path: str, match: re.Match[str]
) -> tuple[dict[str, Any] | None, str]:
    """Маркер, подтверждающий имя и HEAD, или причина отказа."""
    name = os.path.basename(path)
    rc, gitdir, _ = _git(path, "rev-parse", "--absolute-git-dir")
    if rc != 0 or not gitdir.strip():
        return None, "gitdir копии не прочитан"
    marker, err = trash.read_marker(gitdir.strip())
    if marker is None:
        return None, err
    if marker["task_id"] != int(match.group(1)) or marker["name"] != name:
        return None, "маркер не про эту копию: id задачи или имя не совпадают"
    named = _name_epoch(match.group(2))
    if named is None or abs(named - marker["moved_epoch"]) > NAME_SKEW_S:
        return None, "время в имени не сходится со временем маркера"
    rc, head, _ = _git(path, "rev-parse", "HEAD")
    if rc != 0 or head.strip() != marker["head"]:
        return None, "HEAD копии не равен HEAD в маркере: после карантина её меняли"
    return marker, ""


def _classify(
    entry: Entry, tdir: str, older: float, clone: str, pin: bool
) -> tuple[str, str, bool]:
    """(действие, текст, аномалия): purge | keep | skip; аномалия даёт rc != 0."""
    action, text, match = _structure(entry, tdir)
    if action != "proof" or match is None:
        return action, text, False
    marker, problem = _marker_problem(os.path.abspath(entry.path), match)
    if marker is None:
        return "keep", problem, True
    age_s = time.time() - marker["moved_epoch"]
    if age_s < -FUTURE_SKEW_S:
        return "keep", "время маркера из будущего: возраст не определить", True
    if age_s / _DAY < older:
        return "keep", f"в карантине {max(age_s, 0) / _DAY:.1f} сут < {older:g}", False
    held = _held_work(os.path.abspath(entry.path), clone, pin=pin)
    if held:
        return "keep", held, True
    return "purge", f"в карантине {age_s / _DAY:.1f} сут >= {older:g}", False


def _failed_purge(path: str, gitdir: str, rc: int, err: str) -> str:
    here, meta = os.path.lexists(path), os.path.lexists(gitdir)
    tail = f"git rc {rc}: {err.strip()[:200]}"
    if here and meta:
        return f"ошибка удаления, путь и gitdir на месте (возможна частичная очистка): {tail}"
    if here or meta:
        return f"удалена частично ({'путь' if here else 'gitdir'} остался): {tail}"
    return f"результат неизвестен: путь и gitdir исчезли, но git вернул ошибку: {tail}"


def _list_entries(args: Any) -> tuple[str, list[Entry]] | None:
    from hub.integrations.git_ops import clone_root

    clone = asyncio.run(clone_root(args.repo or "."))
    if not clone:
        print(f"{args.repo or '.'} не git-клон: нечего убирать", file=sys.stderr)
        return None
    rc, text, err = _git(clone, "worktree", "list", "--porcelain")
    if rc != 0:
        print(
            f"git worktree list не прочитан (rc {rc}): {err.strip()}", file=sys.stderr
        )
        return None
    return clone, _parse_porcelain(text)


def _held_work(path: str, clone: str, *, pin: bool) -> str:
    """Работа, найденная в копии после карантина; "" - можно удалять.

    Те же проверки для dry-run и --apply, и на КАЖДОМ проходе: не закреплённая и не
    достижимая из refs/remotes sha (reflog с обеих сторон, ORIG/FETCH/AUTO_MERGE)
    оставляет копию; при ``pin`` она закрепляется как ``late-<sha>`` (при сбое
    refs этого прохода откатываются). Уже существующие ``late-*`` тоже держат
    копию, пока человек их не снял (restore или update-ref -d).
    """
    name = os.path.basename(path)
    _, gitdir, _ = _git(path, "rev-parse", "--absolute-git-dir")
    if not gitdir.strip():
        return "gitdir копии не прочитан"
    late, err = trash.late_refs(_git, clone, name)
    if err:
        return f"защитные refs не прочитаны: {err}"
    missing, err = trash.unpinned_shas(_git, clone, name, gitdir.strip())
    if err:
        return f"reflog не сверен, копию не удаляю: {err}"
    if not late and not missing:
        return ""
    parts = []
    if late:
        parts.append(
            f"после карантина в копии нашлась работа (уже закреплено {len(late)} шт., "
            f"{trash.REF_PREFIX}/{name}/{trash.LATE}*): проверьте её, restore или "
            "git update-ref -d снимают метку"
        )
    if missing:
        named = ", ".join(f"{sha[:12]} ({label})" for label, sha in missing[:3])
        note = "dry-run ничего не закрепляет"
        if pin:
            made, perr = trash.pin_late(_git, clone, name, [sha for _, sha in missing])
            note = (
                f"закреплено в этом проходе: {len(made)}"
                if not perr
                else f"закрепить не удалось, refs этого прохода откатаны: {perr}"
            )
        parts.append(f"коммиты без защитного ref и вне refs/remotes: {named}; {note}")
    return "; ".join(parts)


def _purge_one(entry: Entry, clone: str, out: Callable[[str], None]) -> bool:
    """Удалить копию и снять её refs; True - всё чисто."""
    _, gitdir, _ = _git(entry.path, "rev-parse", "--absolute-git-dir")
    why = _held_work(entry.path, clone, pin=True)
    if why:
        out(f"оставить  {entry.path}: {why}")
        return False
    rc, _, err = _git(clone, "worktree", "remove", "--force", entry.path)
    if rc != 0:
        out(
            f"ошибка    {entry.path}: {_failed_purge(entry.path, gitdir.strip(), rc, err)}"
        )
        return False
    bad = trash.unprotect(_git, clone, os.path.basename(entry.path))
    if bad:
        out(f"удалено   {entry.path}, но защитные refs не сняты: {bad}")
        return False
    out(f"удалено   {entry.path}")
    return True


def _drop_orphans(
    clone: str, orphans: dict[str, list[str]], out: Callable[[str], None]
) -> bool:
    """Удалить осиротевшие refs (только их); True - всё чисто."""
    clean = True
    for name, refs in orphans.items():
        bad = trash.unprotect_refs(_git, clone, refs)
        out(
            f"ошибка    осиротевшие refs {name} не сняты: {bad}"
            if bad
            else f"сняты     осиротевшие refs {name}"
        )
        clean = clean and not bad
    return clean


def run_purge(args: Any, out: Callable[[str], None] = print) -> int:
    listed = _list_entries(args)
    if listed is None:
        return 1
    clone, entries = listed
    tdir, older = trash_root(clone), float(args.older_than_days)
    plan = [(e, *_classify(e, tdir, older, clone, bool(args.apply))) for e in entries]
    failed = False
    for entry, action, why, anomaly in plan:
        if action != "skip":
            word = "удалить  " if action == "purge" else "оставить "
            out(f"{word} {entry.path}: {why}")
        failed = failed or anomaly
    live = {
        os.path.basename(e.path)
        for e in entries
        if _real(os.path.dirname(e.path)) == tdir
    }
    orphans, orphan_err = trash.orphan_refs(_git, clone, live)
    if orphan_err:
        out(f"ошибка    защитные refs не прочитаны: {orphan_err}")
        failed = True
    for name, refs in orphans.items():
        out(
            f"осиротевшие refs: {name} ({len(refs)} шт.): "
            f"копии нет среди зарегистрированных в {TRASH_DIR}"
        )
    if not args.apply:
        out(
            "dry-run: ничего не удалено; --apply убирает копии со строкой «удалить» "
            f"из {TRASH_DIR} и осиротевшие refs"
        )
        return 1 if failed else 0
    for entry, action, _why, _anomaly in plan:
        if action == "purge" and not _purge_one(entry, clone, out):
            failed = True
    return 1 if (failed or not _drop_orphans(clone, orphans, out)) else 0


def run_restore(args: Any, out: Callable[[str], None] = print) -> int:
    """Вернуть копию из карантина на канонический путь (шаги a-d из докстринга)."""
    from hub.integrations.git_ops import _worktree_path

    listed = _list_entries(args)
    if listed is None:
        return 1
    clone, entries = listed
    name, tdir = os.path.basename(args.name.rstrip("/")), trash_root(clone)
    found = [e for e in entries if name and os.path.basename(e.path) == name]
    match = _TRASH_NAME.match(name)
    if not found or not match or _real(os.path.dirname(found[0].path)) != tdir:
        print(
            f"restore: в {tdir} нет зарегистрированной копии {args.name!r}",
            file=sys.stderr,
        )
        return 1
    path = os.path.abspath(found[0].path)
    marker, problem = _marker_problem(path, match)
    if marker is None:
        print(f"restore: {problem}", file=sys.stderr)
        return 1
    canonical = _worktree_path(marker["task_id"], clone)
    if os.path.abspath(marker["old_path"]) != os.path.abspath(canonical):
        print(
            f"restore: старый путь {marker['old_path']} не канонический {canonical}",
            file=sys.stderr,
        )
        return 1
    if os.path.lexists(canonical):
        print(
            f"restore: канонический путь {canonical} уже занят (новая копия задачи?): "
            "уберите её сами или вернитесь к ней, карантин не тронут",
            file=sys.stderr,
        )
        return 1
    busy = f"refs/heads/{marker['branch']}"
    if any(e.branch == busy for e in entries):
        print(
            f"restore: ветка {marker['branch']} уже checked out в другой копии: "
            "карантин не тронут",
            file=sys.stderr,
        )
        return 1
    rc, _, err = _git(clone, "worktree", "move", path, canonical)
    if rc != 0:
        print(
            f"restore: git worktree move rc {rc}: {err.strip()[:200]}", file=sys.stderr
        )
        return 1
    rc, _, err = _git(canonical, "switch", "--no-overwrite-ignore", marker["branch"])
    if rc != 0:
        back, _, berr = _git(clone, "worktree", "move", canonical, path)
        state = (
            f"копия возвращена в карантин {path}"
            if back == 0
            else f"ОБРАТНЫЙ перенос не удался (rc {back}: {berr.strip()[:120]}), "
            f"копия на {canonical}"
        )
        print(
            f"restore: git switch {marker['branch']} rc {rc}: {err.strip()[:200]}; "
            f"{state}; маркер и refs оставлены",
            file=sys.stderr,
        )
        return 1
    _, gitdir, _ = _git(canonical, "rev-parse", "--absolute-git-dir")
    bad = trash.unprotect(_git, clone, name)
    try:
        os.remove(os.path.join(gitdir.strip(), trash.MARKER))
    except OSError as exc:
        bad = bad or f"маркер не снят ({type(exc).__name__})"
    out(f"восстановлена: {canonical} (ветка {marker['branch']})")
    if bad:
        print(
            f"restore: копия возвращена, но уборка следов неполна: {bad}",
            file=sys.stderr,
        )
        return 1
    return 0
