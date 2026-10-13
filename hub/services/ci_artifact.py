"""Read a CI results artifact the hub downloaded, as data only (#1687).

The trusted reporter (scripts/ci_trusted_report.py, #1686) reads the artifact of
a finished CI run and sends its content to the hub. The hub no longer takes that
on trust: it downloads the same artifact itself and compares. This module is the
hub's side of the reading, with the SAME limits and the same strictness as the
reporter's — a body the reporter would have refused must not become evidence
here either.

The reporter is a stdlib script that runs from the default branch in GitHub, so
it cannot import the hub; the limits are repeated here and a test pins them to
the script's constants so the two cannot drift apart.

The archive is read in memory by member name. Nothing is extracted, and no name
in the archive ever becomes a path on this machine.
"""

from __future__ import annotations

import io
import json
import stat
import zipfile
from typing import Any

RESULTS_MEMBER = "results.json"
AUDIT_MEMBER = "audit.json"
ALLOWED_MEMBERS = frozenset({RESULTS_MEMBER, AUDIT_MEMBER})
MAX_ARCHIVE_BYTES = 5 * 1024 * 1024
MAX_MEMBERS = 10
MAX_UNPACKED_BYTES = 20 * 1024 * 1024
MAX_RESULTS_BYTES = 2 * 1024 * 1024
RESULTS_SCHEMA = 1

AC_STATUSES = frozenset({"pass", "fail", "not_found"})
CHECK_STATUSES = frozenset({"pass", "fail", "skipped"})
VALIDATION_STATUSES = frozenset({"pass", "fail", "unknown", "skipped", ""})
RESULT_KEYS = frozenset(
    {
        "schema",
        "task_id",
        "head_sha",
        "ac_results",
        "validation_status",
        "validation_log",
        "reason",
        "reported_by",
        "checks",
        "mutations",
        "baseline",
    }
)
LOG_MAX = 4000
REASON_MAX = 500
BLOCK_MAX_CHARS = 32_000


class ArtifactRefused(Exception):
    """The archive or its results are not something the hub reads. ``str`` is why."""


def _member_problem(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if info.flag_bits & 0x1:
        return f"{name!r} is encrypted"
    if name.startswith(("/", "\\")) or "\\" in name or ".." in name.split("/"):
        return f"{name!r} is not a plain relative name"
    if info.is_dir() or "/" in name:
        return f"{name!r} is not a top-level file"
    if stat.S_ISLNK(info.external_attr >> 16):
        return f"{name!r} is a symbolic link"
    if name not in ALLOWED_MEMBERS:
        return f"{name!r} is not an expected file"
    return ""


def read_archive(blob: bytes) -> dict[str, bytes]:
    """The expected members of the archive, in memory; ArtifactRefused otherwise."""
    if len(blob) > MAX_ARCHIVE_BYTES:
        raise ArtifactRefused("the archive is larger than 5 MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArtifactRefused(
            f"the artifact is not a readable archive ({exc})"
        ) from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ArtifactRefused(f"{len(infos)} files, the limit is {MAX_MEMBERS}")
        names = [i.filename for i in infos]
        if len(set(names)) != len(names):
            raise ArtifactRefused("the archive repeats a file name")
        members: dict[str, bytes] = {}
        budget = MAX_UNPACKED_BYTES
        for info in infos:
            problem = _member_problem(info)
            if problem:
                raise ArtifactRefused(problem)
            if info.file_size > budget:
                raise ArtifactRefused("the unpacked size exceeds 20 MB")
            with archive.open(info) as handle:
                data = handle.read(budget + 1)
            if len(data) > budget:
                raise ArtifactRefused("the unpacked size exceeds 20 MB")
            budget -= len(data)
            members[info.filename] = data
        return members


def _string_map(value: Any, label: str, allowed: frozenset[str]) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 500:
        raise ArtifactRefused(f"{label} is not a small object")
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 200:
            raise ArtifactRefused(f"{label} has a bad key")
        if item not in allowed:
            raise ArtifactRefused(f"{label}[{key!r}] = {item!r} is not allowed")
    return dict(value)


def _bounded_text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ArtifactRefused(f"{label} is not text")
    return value if len(value) <= limit else value[-limit:]


def strip_run_stamp(block: Any) -> Any:
    """An evidence block without the run stamp the reporter added to it.

    The reporter writes ``provenance`` (run id, URL, the moment of sending) into
    each mutations/baseline block; that is a fact about the sending, not
    something the artifact holds.
    """
    if isinstance(block, dict):
        return {k: v for k, v in block.items() if k != "provenance"}
    return block


def _evidence_block(value: Any, label: str, stamp: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ArtifactRefused(f"{label} is not an object")
    # The reporter measures the block together with its stamp; so does the hub,
    # with the stamp the submitted block carried.
    stamped = dict(value, provenance=stamp if isinstance(stamp, dict) else {})
    if len(json.dumps(stamped, ensure_ascii=False)) > BLOCK_MAX_CHARS:
        return {
            "state": "error",
            "reason": f"the {label} block exceeds {BLOCK_MAX_CHARS} characters",
        }
    # The reporter overwrites any ``provenance`` key of its own with the stamp.
    return strip_run_stamp(value)


def expected_body(
    archive: bytes, *, head_sha: str, task_id: int, submitted: dict[str, Any]
) -> dict[str, Any]:
    """What the artifact says the report body must be, or ArtifactRefused.

    ``submitted`` is only consulted for the run stamp of the evidence blocks
    (see ``_evidence_block``). Keys: ac_results, checks, validation_status,
    validation_log, mutations (absent when the artifact has none), baseline.
    """
    raw = read_archive(archive).get(RESULTS_MEMBER)
    if raw is None:
        raise ArtifactRefused("no results.json in the artifact")
    if len(raw) > MAX_RESULTS_BYTES:
        raise ArtifactRefused("results.json is larger than 2 MB")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArtifactRefused(f"results.json is not JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise ArtifactRefused("results.json is not an object")
    extra = sorted(set(doc) - RESULT_KEYS)
    if extra:
        raise ArtifactRefused(f"results.json has unexpected keys: {extra}")
    if doc.get("schema") != RESULTS_SCHEMA:
        raise ArtifactRefused("results.json schema is not 1")
    if doc.get("head_sha") != head_sha:
        raise ArtifactRefused("the artifact is about another commit than the report")
    if doc.get("task_id") != task_id:
        raise ArtifactRefused("the artifact is about another task than the report")
    status = doc.get("validation_status", "")
    if status not in VALIDATION_STATUSES:
        raise ArtifactRefused(f"validation_status {status!r} is not recognised")
    body: dict[str, Any] = {
        "ac_results": _string_map(doc.get("ac_results", {}), "ac_results", AC_STATUSES),
        "checks": _string_map(doc.get("checks", {}), "checks", CHECK_STATUSES),
        "validation_status": status,
        "validation_log": _bounded_text(
            doc.get("validation_log", ""), "validation_log", LOG_MAX
        ),
    }
    for key in ("mutations", "baseline"):
        block = _evidence_block(
            doc.get(key),
            key,
            (submitted.get(key) or {}).get("provenance")
            if isinstance(submitted.get(key), dict)
            else None,
        )
        if block is not None:
            body[key] = block
    return body


def submitted_comparable(submitted: dict[str, Any]) -> dict[str, Any]:
    """The reporter's body in the shape ``expected_body`` returns."""
    body: dict[str, Any] = {
        "ac_results": submitted.get("ac_results") or {},
        "checks": submitted.get("checks") or {},
        "validation_status": submitted.get("validation_status") or "",
        "validation_log": submitted.get("validation_log") or "",
    }
    for key in ("mutations", "baseline"):
        if key in submitted and submitted[key] is not None:
            body[key] = strip_run_stamp(submitted[key])
    return body
