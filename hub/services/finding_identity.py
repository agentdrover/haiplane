"""Who a machine-review finding IS, and where it sits (#1007).

Until now a confirmed finding had no identity of its own. Dispositions
addressed it by ``finding_index`` — its position in ``findings_confirmed`` —
and a position is a property of the list, not of the finding. Three things
followed, all of them live:

* a resubmitted report reorders the list, so a judgement filed against slot 2
  starts describing whatever landed in slot 2 this time;
* the same defect found twice cannot be recognised as the same defect, so
  nothing can say whether a finding is recurring or new;
* a finding cannot be matched against the diff at all — ``file`` was optional
  and ``line`` was a single optional number, so "no location" and "location
  not filled in" were the same empty value.

Two rules fix that, and they are deliberately different in kind.

**Identity is DERIVED, never carried.** A harness has no memory of the previous
report, so any id it invents is fresh randomness — it would satisfy a schema
and answer none of the questions above. What repeats across runs is the
finding's own content, so the id is a hash of (category, file, normalised
title). The guarantee is exactly that and no more: identical content yields an
identical id. A reworded title is a different id, because the hub cannot know
that two sentences describe one defect, and a confident id over that guess
would be worse than an honest miss.

**Location is DECLARED, and refusing to place a finding is a valid answer.**
``locator='none'`` says the reviewer could not point at a place. That is
information. An empty ``file`` is not: it cannot be told apart from a harness
that forgot the field, which is the substitution #549 removed from the
neighbouring keys.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping, Sequence

from fastapi import HTTPException

#: Length of the hex digest kept as the id. 16 hex chars is 64 bits: at the
#: scale of one task's reports a collision is not a practical concern, and a
#: short id stays readable in a URL, a log line and a button's data attribute.
_UID_CHARS = 16

_WHITESPACE = re.compile(r"\s+")


def _normalised(value: Any) -> str:
    """Lowercase, whitespace-collapsed text — the form two runs can agree on."""
    return _WHITESPACE.sub(" ", str(value or "").strip().lower())


def finding_uid(entry: Mapping[str, Any] | Any, ordinal: int = 0) -> str:
    """Content-derived id of one confirmed finding.

    ``ordinal`` disambiguates duplicates INSIDE one report: two findings with
    the same category, file and title are two rows a person has to judge
    separately, so they must not collapse into one id. It is deliberately not
    part of the id for the first occurrence — otherwise every finding's id
    would depend on how many twins happened to precede it, and the id would
    stop surviving a reordering, which is the whole point.
    """
    if not isinstance(entry, Mapping):
        entry = {
            "category": getattr(entry, "category", ""),
            "file": getattr(entry, "file", ""),
            "title": getattr(entry, "title", ""),
        }
    material = "\x1f".join(
        (
            _normalised(entry.get("category")),
            _normalised(entry.get("file")),
            _normalised(entry.get("title")),
        )
    )
    if ordinal:
        material = f"{material}\x1f{ordinal}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:_UID_CHARS]


def finding_uids(entries: Sequence[Any]) -> list[str]:
    """Ids for a whole ``findings_confirmed`` list, twins disambiguated.

    Returned positionally so a caller can still map an id back to the index the
    storage layer keeps — the two addressing schemes have to coexist while rows
    filed by index are still readable.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for entry in entries:
        base = finding_uid(entry)
        ordinal = seen.get(base, 0)
        out.append(base if ordinal == 0 else finding_uid(entry, ordinal))
        seen[base] = ordinal + 1
    return out


def require_locator_decision(findings: Sequence[Any]) -> None:
    """Refuse confirmed findings that never said where they sit (#1007).

    A WRITE-path check, like :func:`hub.services.test_locator.validate_test_locators`:
    stored reports predate the field and must keep loading, so the read model
    leaves ``locator`` optional and this guard is what makes new reports state
    it. There is no warn mode — a report is submitted by a harness we can fix
    in the same breath, unlike the 425 AC locators #596 found in the ground.
    """
    missing = [
        idx
        for idx, finding in enumerate(findings)
        if getattr(finding, "locator", None) is None
    ]
    if not missing:
        return
    listed = ", ".join(f"#{i}" for i in missing)
    raise HTTPException(
        422,
        f"confirmed findings {listed} do not say where they sit: set "
        "'locator' to 'lines' (with file and start_line), 'file' (module "
        "known, line not) or 'none' (no place could be identified). "
        "'none' is an answer and is accepted; an empty file is not, because "
        "it cannot be told apart from a field nobody filled. The whole report "
        "was rejected — nothing was stored.",
    )


__all__ = ["finding_uid", "finding_uids", "require_locator_decision"]
