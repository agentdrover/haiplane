"""One reading of a unified diff for everything that needs its file headers (#1856).

A line is a header or a hunk body by the hunk COUNTERS, never by how it looks:
an added ``++ x`` is ``+++ x`` in the diff and a removed ``-- x`` is ``--- x``.
Every consumer that used to ask ``line.startswith("+++ ")`` reads this instead.
When the counters do not add up, nothing from there to the end of the diff is
trusted: consumers must treat that text as code and count it, never skip it.
"""

from __future__ import annotations

import dataclasses
import re
from typing import NamedTuple

_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13}


def _unquote_path(raw: str) -> str | None:
    """THE key of a path named in a diff header, or None when it cannot be read.

    One rule for every reader (``changed_paths``, section keys, ``rename to``,
    ``split_generated``, ``_header_paths``). A quoted name is decoded and
    returned as it is, a trailing tab included. An unquoted one loses only the
    line ending and git's service TAB after ``---``/``+++`` names; spaces are
    part of the name (an unquoted name never ends in a tab: git quotes it).

    With ``core.quotepath`` (the default) a name with non-ASCII bytes, quotes
    or control characters comes C-quoted: ``"a/docs/\\320\\267.md"``.
    Undecodable means unknown, never documentation.
    """
    # git's service TAB follows a name with a space, quoted or not. A TAB INSIDE
    # the quotes is the two characters backslash-t, so cutting trailing TABs is safe.
    raw = raw.rstrip("\r\n").rstrip("\t")
    if not raw.startswith('"'):
        return raw
    if len(raw) < 2 or not raw.endswith('"'):
        return None
    body, out, i = raw[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out += ch.encode("utf-8")
            i += 1
        elif body[i + 1 : i + 4].isdigit() and len(body[i + 1 : i + 4]) == 3:
            out.append(int(body[i + 1 : i + 4], 8) & 0xFF)
            i += 4
        elif i + 1 < len(body):
            nxt = body[i + 1]
            out.append(_C_ESCAPES.get(nxt, ord(nxt) if nxt.isascii() else 0))
            i += 2
        else:
            return None
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


_HUNK_RE = re.compile(r"^(@{2,}) ((?:[-+]\d+(?:,\d+)?(?: |$))+)\1")
FILE_STARTS = ("diff --git ", "diff --cc ", "diff --combined ")


class _Hunk:
    """Lines still owed to the current hunk, from its ``@@ -a,b +c,d @@`` header.

    The boundary of a hunk is its counters, never the look of a line: inside
    the body an added ``++ x`` reads exactly like a ``+++ x`` header (#1856).
    A combined diff (``@@@ -a,b -c,d +e,f @@@``) carries one old counter per
    parent. A missing counter is 1. ``consume`` is False when the body does
    not add up; the caller then trusts nothing about where files begin.
    """

    def __init__(self) -> None:
        self.parents = 1
        self.old: list[int] = []
        self.new = 0

    @property
    def open(self) -> bool:
        return self.new > 0 or any(n > 0 for n in self.old)

    def start(self, header: str) -> bool:
        match = _HUNK_RE.match(header)
        if match is None:
            return False
        counts = [
            int(tok.split(",")[1]) if "," in tok else 1
            for tok in match.group(2).split()
        ]
        self.parents = len(match.group(1)) - 1
        if len(counts) != self.parents + 1:
            return False
        self.old, self.new = counts[:-1], counts[-1]
        return True

    def consume(self, line: str) -> bool:
        if line.startswith("\\"):
            return True  # "\ No newline at end of file"
        prefix = line[: self.parents].ljust(self.parents)
        if any(c not in " +-" for c in prefix):
            return False
        # git's columns: on a line the result lost, '-' marks the parents that
        # had it; on any other line, ' ' marks the parents that had it, '+'
        # the ones that did not. The result side owns every line not lost.
        lost = "-" in prefix
        for i, c in enumerate(prefix):
            if c == ("-" if lost else " "):
                self.old[i] -= 1
        if not lost:
            self.new -= 1
        return self.new >= 0 and all(n >= 0 for n in self.old)

    def reset(self) -> None:
        self.old, self.new = [], 0


@dataclasses.dataclass
class _FileSpan:
    path: str | None = None  # THE key of the file; every reader takes it from here
    old: str | None = None  # the name it had before a rename or a copy
    plus_at: int | None = None  # index of the "+++ " line: the cut starts there
    hunks: int = 0
    deleted: bool = False
    trusted: bool = True


def _name(line: str) -> str:
    """The name on a ``--- `` / ``+++ `` line: one key rule, no a/ b/ prefix."""
    raw = _unquote_path(line[4:]) or line[4:].strip()
    return raw[2:] if raw[:2] in ("a/", "b/") else raw


def _git_line_paths(rest: str) -> list[str | None]:
    """The two sides of ``diff --git <a> <b>``, quoted or not."""
    if rest.startswith('"'):
        end = rest.find('" ', 1)
        if end < 0:
            return [None]
        return [_unquote_path(rest[: end + 1]), _unquote_path(rest[end + 2 :])]
    # Same name on both sides (everything but a rename or copy): "a/P b/P" is
    # split by length, so a " b/" inside P cannot move the cut.
    size = (len(rest) - 5) // 2
    if (
        size > 0
        and len(rest) == 2 * size + 5
        and rest.startswith("a/")
        and rest[2 + size : 5 + size] == " b/"
        and rest[2 : 2 + size] == rest[5 + size :]
    ):
        return [rest[: 2 + size], "b/" + rest[5 + size :]]
    left, sep, right = rest.partition(" b/")
    if not sep or not left.startswith("a/"):
        return [None]
    return [left, "b/" + right]


def git_header_path(line: str) -> str | None:
    """Key of the file a ``diff --git`` / ``diff --cc`` line opens, or None.

    A plain ``diff --git`` line names the file only when both sides spell the
    same path; sides that differ are a rename or a copy, and the key waits for
    ``rename to`` / ``+++``. ``diff --cc P`` names it outright.
    """
    line = line.rstrip("\r\n")
    if line.startswith(("diff --cc ", "diff --combined ")):
        return _unquote_path(line.split(" ", 2)[2]) or None
    sides = _git_line_paths(line[len("diff --git ") :])
    if len(sides) == 2 and sides[0] and sides[1]:
        old, new = sides[0].removeprefix("a/"), sides[1].removeprefix("b/")
        if old == new:
            return new
    return None


def _old_name(line: str) -> str | None:
    named = _name(line)
    return None if named == "/dev/null" else named


def diff_lines(diff: str) -> list[str]:
    """Lines as git ends them: by LF only, never by VT, FF, U+2028 and the like.

    ``str.splitlines`` would cut a hunk body at a ``\\v`` inside a literal and
    invent a line the counters never owed (#1856).
    """
    return re.findall(r"[^\n]*\n|[^\n]+", diff)


def diff_texts(diff: str) -> list[str]:
    """The lines of ``diff`` as git ends them, without LF and CR (#1856)."""
    return [ln.rstrip("\n").removesuffix("\r") for ln in diff_lines(diff)]


class Scanned(NamedTuple):
    """One diff line with the file it belongs to and how it was read."""

    line: str  # as it came, with its line ending
    text: str  # without LF and CR
    span: _FileSpan
    body: int  # hunk body: width of the +/- columns (1; parents of a merge); else 0
    idx: int  # line number in the diff


_HEADER_NOTES = ("--- ", "rename ", "copy ", "deleted file mode")


def _note_header(cur: _FileSpan, text: str) -> None:
    """What a header line before the first hunk says about the file."""
    if text.startswith("--- "):
        cur.old = cur.old or _old_name(text)
    elif text.startswith(("rename to ", "copy to ")):
        cur.path = _unquote_path(text.split(" ", 2)[2]) or cur.path
    elif text.startswith(("rename from ", "copy from ")):
        cur.old = _unquote_path(text.split(" ", 2)[2]) or cur.old
    elif text.startswith("deleted file mode"):
        cur.deleted = True


def scan_diff(diff: str) -> list[Scanned]:
    """Every line of ``diff`` with its file and its role (header or hunk body).

    After the FIRST place where the counters do not add up nothing is trusted
    any more, to the end of the diff: once we cannot tell where a hunk ends we
    cannot tell where the next file starts, and a guess is a hidden signal.
    """
    spans = [_FileSpan()]
    out: list[Scanned] = []
    hunk = _Hunk()
    broke = False
    prev = ""

    def fresh() -> _FileSpan:
        span = _FileSpan(trusted=not broke)
        spans.append(span)
        return span

    for n, line in enumerate(diff_lines(diff)):
        text = line.rstrip("\n").removesuffix("\r")
        cur = spans[-1]
        if hunk.open:
            if hunk.consume(text):
                out.append(Scanned(line, text, cur, hunk.parents, n))
                prev = ""  # a body line is never half of a header
                continue
            broke, cur.trusted = True, False
            hunk.reset()
        if text.startswith(FILE_STARTS):
            cur = fresh()
            cur.path = git_header_path(text)
        elif text.startswith("--- ") and cur.plus_at is not None:
            cur = fresh()
            cur.old = _old_name(text)
        elif not cur.hunks and text.startswith(_HEADER_NOTES):
            _note_header(cur, text)
        elif text.startswith("+++ ") and cur.hunks and not prev.startswith("--- "):
            # A file header is the pair "--- " + "+++ ". A lone "+++ " after a
            # hunk is body text the counters undercounted, not a new file.
            broke, cur.trusted = True, False
        elif text.startswith("+++ "):
            cur = fresh() if cur.plus_at is not None else cur
            named = _name(text)
            cur.plus_at = n
            if named == "/dev/null":
                cur.deleted, cur.path = True, cur.path or cur.old
            else:
                cur.path = named
        elif text.startswith("@@"):
            cur.hunks += 1
            if not hunk.start(text):
                broke, cur.trusted = True, False
        elif cur.hunks and text[:1] in ("+", "-", " "):
            broke, cur.trusted = True, False  # counters undercounted the body
        out.append(Scanned(line, text, cur, 0, n))
        prev = text
    if hunk.open and out:
        out[-1].span.trusted = False
    return out


def is_plus_header(entry: Scanned) -> bool:
    """Is this the ``+++`` line that names the file (not text that looks like it)?"""
    return entry.idx == entry.span.plus_at


def changed_line(entry: Scanned) -> bool:
    """Does this line add or remove text? Headers and context do not.

    A hunk body line counts by its +/- columns. Outside a hunk (a diff with no
    ``@@``, a truncated one, text the counters could not place) a line that
    starts with ``+`` or ``-`` counts unless it is a ``+++ `` or ``--- ``
    header: undercounting is the failure to avoid.
    """
    if entry.body:
        return any(c in "+-" for c in entry.text[: entry.body])
    text = entry.text
    return text[:1] in ("+", "-") and not text.startswith(("+++ ", "--- "))
