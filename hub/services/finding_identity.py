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
title, canonical place). "Canonical" is load-bearing: the place is derived from
what is known — a line, a file, or nothing — never copied from the ``locator``
field, because the same location described in two vocabularies must not produce
two ids (#1028). The guarantee is exactly that and no more:
identical content yields an identical id. A reworded title — or a defect that
moved to another line — is a different id, because the hub cannot know that two
sentences describe one defect, and a confident id over that guess would be
worse than an honest miss. The place is in the material on purpose: without it
two defects sharing a file and a title collapse into one id, and a human's
judgement then lands on whichever of them survived the next report.

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
    raw = getattr(value, "value", value)  # accepts the FindingLocator enum
    return _WHITESPACE.sub(" ", str(raw or "").strip().lower())


def _as_mapping(entry: Mapping[str, Any] | Any) -> Mapping[str, Any]:
    """A finding as a key-value view, whether it arrived as a dict or a model.

    Both shapes reach this module: the API layer holds parsed models, storage
    holds JSON. A helper that silently understood only one of them is exactly
    how ``getattr`` on a dict returned None for every field it was asked about.
    """
    if isinstance(entry, Mapping):
        return entry
    return {
        key: getattr(entry, key, None)
        for key in (
            "category",
            "file",
            "title",
            "locator",
            "start_line",
            "end_line",
            "line",
            "why",
            "finding_uid",
        )
    }


def _place(entry: Mapping[str, Any]) -> tuple[str, str]:
    """The finding's location, canonical rather than as declared (#1028).

    The PLACE belongs in the identity: two defects in one file can share a
    category and a title and still be two defects, and without a line they
    collapsed into one id (#1007). But the first attempt hashed the ``locator``
    field VERBATIM, and that broke the thing the id exists for.

    A declaration is a description of the location, not the location. The same
    defect is described differently by different reporters and by the same
    reporter over time: 116 reports predate the field and carry no locator at
    all; a harness that first said ``file`` and later pinned the line says
    ``lines``. Each of those rewordings produced a different id for one defect,
    so a disposition made on the previous report stopped matching — exactly at
    the format boundary this change was supposed to cross.

    So the kind is DERIVED from what is actually known, and the raw field never
    enters the hash: a line makes it "lines", a file alone makes it "file",
    neither makes it "none". Two reports agreeing about where a defect sits now
    agree about its id, whichever vocabulary they used to say so.

    THE EXTENT OF A FINDING IS NOT PART OF ITS PLACE. A finding that says
    lines 40-52 and one that says line 40 are one defect described with
    different precision, exactly like ``file`` and ``lines`` are — so
    ``end_line`` is deliberately absent from the material. Hashing it would
    reintroduce the instability this function removes: the same reviewer
    widening a range on a second run would produce a second id, and the
    disposition filed against the first would stop matching. Two defects that
    truly share a file, a category, a title AND a start line are twins, and
    twins are what ``ordinal`` is for.

    The remaining cost stays and is meant to: code that moves shifts the line
    and the finding gets a new id. Of the two possible errors — "the same
    defect looks new" and "two defects look like one" — only the second
    silently misfiles a human's judgement onto the wrong defect.
    """
    start = entry.get("start_line")
    if start is None:
        start = entry.get("line")
    if start is None:
        # A range whose start went missing still names a line, and a placed
        # finding must not degrade to file level over a field that was never
        # filled in. Reachable only through legacy rows and hand-built
        # payloads: the write model refuses ``end_line`` without a start.
        start = entry.get("end_line")
    if start is not None:
        return ("lines", str(start))
    if _normalised(entry.get("file")):
        return ("file", "")
    return ("none", "")


def finding_uid(entry: Mapping[str, Any] | Any, ordinal: int = 0) -> str:
    """Content-derived id of one confirmed finding.

    ``ordinal`` disambiguates duplicates INSIDE one report: two findings with
    the same category, file and title are two rows a person has to judge
    separately, so they must not collapse into one id. It is deliberately not
    part of the id for the first occurrence — otherwise every finding's id
    would depend on how many twins happened to precede it, and the id would
    stop surviving a reordering, which is the whole point.
    """
    entry = _as_mapping(entry)
    material = "\x1f".join(
        (
            _normalised(entry.get("category")),
            _normalised(entry.get("file")),
            _normalised(entry.get("title")),
            *_place(entry),
        )
    )
    if ordinal:
        material = f"{material}\x1f{ordinal}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:_UID_CHARS]


#: What the material of an unresolved finding's id starts with (#1085).
#:
#: Honest about how much work this does. TODAY the two sections cannot collide
#: even without it: a confirmed finding's material is five components and an
#: unresolved one's is a title, so the shapes differ whatever the words are.
#: That separation is an UNSTATED invariant of the confirmed material's shape,
#: and it holds only until someone adds or drops a component there. The label
#: says out loud what the shape says by accident, and it is the half that
#: survives such a change. It is pinned by a golden id taken from a live
#: report — ``LIVE_UNRESOLVED_UID`` in tests/test_review_disposition.py — so
#: changing the material is a red test rather than a silent renaming of every
#: unresolved finding. (An earlier revision said a mutation removing the label
#: broke no test. That was true of the tests that existed then, and the honest
#: fix was the pin, not the note.)
#:
#: Prepended ONLY here, so every confirmed uid stays the value it already was.
_UNRESOLVED_NAMESPACE = "unresolved"


def unresolved_uid(entry: Mapping[str, Any] | Any, ordinal: int = 0) -> str:
    """Content-derived id of one finding nobody managed to judge (#1085).

    Measured over five deep runs (#163-#167): 13 raw candidates, zero
    confirmed, six unresolved — and all six were real defects. Every one of
    them was invisible to the outcome ledger, because ids were handed out over
    ``findings_confirmed`` alone.

    The material is the title and nothing else, because the record itself is
    the title and nothing else: :class:`hub.models.MachineUnresolvedFinding`
    forbids extra keys and declares exactly ``title`` and ``why``. ``why`` is
    deliberately left out — it holds what the adjudicators said as they failed
    to agree, and two runs describing one standoff in different words are
    still one standoff. The cost is the same one confirmed ids pay: a reworded
    title reads as a different finding, which is an honest miss rather than a
    confident mismatch.
    """
    entry = _as_mapping(entry)
    material = "\x1f".join((_UNRESOLVED_NAMESPACE, _normalised(entry.get("title"))))
    if ordinal:
        material = f"{material}\x1f{ordinal}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:_UID_CHARS]


def unresolved_uids(entries: Sequence[Any]) -> list[str]:
    """Ids for a whole ``unresolved`` list, twins disambiguated by position.

    Same rule as :func:`finding_uids`, for the same reason: two records that
    say the same thing are two rows an author has to answer separately.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for entry in entries:
        base = unresolved_uid(entry)
        ordinal = seen.get(base, 0)
        out.append(base if ordinal == 0 else unresolved_uid(entry, ordinal))
        seen[base] = ordinal + 1
    return out


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
        if _as_mapping(finding).get("locator") is None
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


def refuse_supplied_uid(findings: Sequence[Any], section: str = "confirmed") -> None:
    """Refuse a report that brings its own finding ids (#1007).

    The id is derived from content precisely so that two runs of a harness with
    no memory of each other land on the same one. A submitted id would be
    random by construction and would quietly win over the derived one, which is
    the failure this whole change exists to prevent — so it is refused loudly
    instead of ignored silently (#553).

    ``section`` names the list being checked, because since #1085 unresolved
    records carry the field too: a refusal that says "confirmed findings" about
    an unresolved one sends the caller looking in the wrong list.
    """
    supplied = [
        idx
        for idx, finding in enumerate(findings)
        if str(_as_mapping(finding).get("finding_uid") or "").strip()
    ]
    if not supplied:
        return
    listed = ", ".join(f"#{i}" for i in supplied)
    raise HTTPException(
        422,
        f"{section} findings {listed} carry a finding_uid: the hub derives "
        "that id from the finding's own content, so a submitted one is "
        "random by construction. Drop the field — the id comes back on every "
        "read of the report.",
    )


__all__ = [
    "finding_uid",
    "finding_uids",
    "refuse_supplied_uid",
    "require_locator_decision",
    "unresolved_uid",
    "unresolved_uids",
]
