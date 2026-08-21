"""Which changes belong to which criterion — and which belong to nobody (#825).

The gate now shows what was checked (#823) and what changed (#824), but as two
separate lists. Joining them is the reader's job, it is the expensive part of a
review, and it is the part that gets skipped. Two things stay invisible while
the lists sit apart:

- a criterion whose test is green and whose files nothing touched. It reads as
  done because the tick is there;
- a change no criterion asked for. Nothing marks it, and that is where the risk
  that survives review usually lives.

Three buckets, never two, and the middle one exists because collapsing it would
be a lie in one direction or the other:

``exact``    the file carries the criterion's own test — the locator says so.
``assumed``  the file falls inside the task's declared ``affected_areas``: the
             work was asked for, but nothing ties it to a particular criterion.
``nobody``   neither, and no confirmed review finding mentions it.

An ambiguous change goes to ``nobody``, never to a criterion. A wrong
attribution is worse than an absent one: it puts a tick where a reader would
otherwise look, which is the exact failure this module exists to prevent.
"""

from __future__ import annotations

from typing import Any

from hub.services.test_locator import parse_test_locator

EXACT = "exact"
ASSUMED = "assumed"
NOBODY = "nobody"


def _ac_test_file(ac: Any) -> str:
    """The test file bound to this criterion, or "" when it has no locator."""
    parsed = parse_test_locator(getattr(ac, "test_ref", "") or "")
    return parsed[0] if parsed else ""


def _in_declared_areas(path: str, areas: list[str]) -> bool:
    """Does this path fall inside a declared area?

    Areas are written by hand and mix files with directories, so both are
    honoured — ``hub/web.py`` matches itself, ``hub/services`` matches what is
    under it. Nothing is inferred beyond what was literally declared.
    """
    for area in areas:
        area = (area or "").strip().strip("/")
        if not area:
            continue
        if path == area or path.startswith(area + "/"):
            return True
    return False


def _finding_paths(findings: list[Any]) -> set[str]:
    """Files a confirmed review finding points at (#808 carries file:line)."""
    paths: set[str] = set()
    for finding in findings or []:
        raw = ""
        if isinstance(finding, dict):
            raw = str(finding.get("file") or finding.get("path") or "")
        else:
            raw = str(getattr(finding, "file", "") or "")
        raw = raw.split(":", 1)[0].strip()
        if raw:
            paths.add(raw)
    return paths


def build(
    files: list[dict[str, Any]],
    acceptance_criteria: list[Any],
    ac_test_results: list[Any],
    affected_areas: list[str],
    findings: list[Any] | None = None,
) -> dict[str, Any]:
    """Lay the submission's changed files against the criteria that ordered them.

    ``files`` are ``{"path", "added", "removed"}`` entries — the numstat of the
    pinned submission, not the full diff: this map needs paths, and the hunks
    are loaded on demand elsewhere (#824).
    """
    results = {
        getattr(r, "ac_id", None)
        or (r.get("ac_id") if isinstance(r, dict) else None): r
        for r in ac_test_results or []
    }
    by_path = {f["path"]: f for f in files}
    claimed: set[str] = set()

    criteria: list[dict[str, Any]] = []
    for ac in acceptance_criteria or []:
        ac_id = getattr(ac, "id", "") or ""
        test_file = _ac_test_file(ac)
        own = [by_path[test_file]] if test_file and test_file in by_path else []
        claimed.update(f["path"] for f in own)
        result = results.get(ac_id)
        status = ""
        is_current = False
        if result is not None:
            status = getattr(result, "status", "") or (
                result.get("status", "") if isinstance(result, dict) else ""
            )
            is_current = bool(
                getattr(result, "is_current", False)
                if not isinstance(result, dict)
                else result.get("is_current", False)
            )
        verifiable_by = getattr(ac, "verifiable_by", None)
        verifiable_by = getattr(verifiable_by, "value", verifiable_by) or ""
        criteria.append(
            {
                "ac_id": ac_id,
                "then": getattr(ac, "then", "") or "",
                "verifiable_by": verifiable_by,
                "test_file": test_file,
                "files": own,
                "level": EXACT if own else NOBODY,
                "status": status,
                "is_current": is_current,
                # The case this whole module was written to surface: a tick
                # with nothing behind it in this submission.
                "green_without_changes": status == "pass" and not own,
            }
        )

    finding_paths = _finding_paths(findings or [])
    assumed: list[dict[str, Any]] = []
    nobody: list[dict[str, Any]] = []
    for entry in files:
        path = entry["path"]
        if path in claimed:
            continue
        if _in_declared_areas(path, affected_areas or []):
            assumed.append(entry)
        elif path in finding_paths:
            # A reviewer already pointed at it, so it is not unnoticed — but it
            # is still not something a criterion asked for.
            assumed.append({**entry, "seen_by_review": True})
        else:
            nobody.append(entry)

    return {
        "criteria": criteria,
        # Keyed for the template: a per-criterion lookup in Jinja would be a
        # filter chain, and this map is built once here anyway.
        "by_ac": {c["ac_id"]: c for c in criteria},
        "assumed": assumed,
        "nobody": nobody,
        "total_files": len(files),
    }
