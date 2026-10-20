#!/usr/bin/env python3
"""CI: merge the reports of the mutation legs into one ``mutation-report.json`` (#1921).

The mutations of a branch run as N legs in parallel (``mutation_changed.py
--shard i/N``); the hub reads ONE report with the same shape as before. This
script is that step, in job ``evidence``:

    python3 -I scripts/merge_mutation_legs.py --legs-dir legs --out mutation-report.json

Each leg report sits in ``<legs-dir>/<any name>/mutation-report.json``. The merge
is a statement about evidence, so every doubt is an ``error`` with its reason,
never a quiet "ran":

* a missing leg (some ``i`` of ``1..N`` has no file), a duplicate leg, an
  unreadable file or legs that name different ``N`` -> ``error``; no files at all
  -> ``error``;
* legs measured against different code (``head``, ``merge_base``, ``base_tip``,
  ``base``) -> ``error``;
* all legs ``ran``: counts add up, ``survivors`` are concatenated and ordered,
  ``functions``/``untested`` are the union (every leg selects the same ones);
* any leg ``baseline_red`` -> ``baseline_red`` (a series that did not run on one leg
  is not "ran"); any leg ``error`` -> ``error``;
* all legs ``no_changed_functions`` or all ``no_tests`` -> that state;
* any other mix -> ``error`` ("legs disagree").

Standard library only: the job installs nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPORT_NAME = "mutation-report.json"
_SHARD = re.compile(r"(\d+)/(\d+)")
_SAME_KEYS = ("base", "head", "merge_base", "base_tip")
_SUMS = ("mutants", "killed", "survived", "not_run")
_MAX_FILES = 64
_MAX_BYTES = 2 * 1024 * 1024


def _error(reason: str, **extra: object) -> dict:
    return {"state": "error", "reason": reason, "shard": "merged", **extra}


def _read(path: Path) -> dict:
    if path.stat().st_size > _MAX_BYTES:
        raise ValueError("report too large")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("not an object")
    return document


def _union(legs: list[dict], key: str) -> list:
    seen: list = []
    for leg in legs:
        for item in leg.get(key) or []:
            if item not in seen:
                seen.append(item)
    return seen


def merge(reports: list[tuple[str, dict]]) -> dict:
    """One report from ``(file label, leg report)`` pairs. Never raises."""
    if not reports:
        return _error("no leg reports: every leg failed before it wrote one")
    parsed: dict[int, dict] = {}
    totals: set[int] = set()
    for label, leg in reports:
        match = _SHARD.fullmatch(str(leg.get("shard", "")))
        if not match:
            return _error(f"{label}: no shard mark, so the leg cannot be placed")
        index, total = int(match.group(1)), int(match.group(2))
        totals.add(total)
        if index in parsed:
            return _error(f"leg {index}/{total} is reported twice")
        parsed[index] = leg
    if len(totals) != 1:
        return _error(f"legs name different totals: {sorted(totals)}")
    total = totals.pop()
    missing = [i for i in range(1, total + 1) if i not in parsed]
    if missing or set(parsed) - set(range(1, total + 1)):
        return _error(
            f"leg(s) {missing} of {total} missing: the series is incomplete",
            legs=total,
        )
    legs = [parsed[i] for i in range(1, total + 1)]
    for key in _SAME_KEYS:
        if len({str(leg.get(key, "")) for leg in legs}) != 1:
            return _error(f"legs disagree on {key}: they measured different code")
    states = [str(leg.get("state", "")) for leg in legs]
    reasons = []
    for i, leg in enumerate(legs, 1):
        if leg.get("reason"):
            reasons.append(f"[leg {i}/{total}] {str(leg['reason']).strip()}")
    base = {
        **{key: legs[0].get(key, "") for key in _SAME_KEYS},
        "shard": "merged",
        "legs": total,
        "reason": "\n".join(reasons),
        "functions": _union(legs, "functions"),
        "untested": _union(legs, "untested"),
        # the same keys as a whole series, in every state
        **{key: 0 for key in _SUMS},
        "survivors": None,
        "seconds": 0.0,
    }
    if "error" in states:
        return {**base, "state": "error"}
    if "baseline_red" in states:
        return {**base, "state": "baseline_red"}
    if len(set(states)) != 1:
        return _error(f"legs disagree on the state: {states}", legs=total)
    state = states[0]
    if state != "ran":
        return {**base, "state": state}
    survivors = sorted(
        (s for leg in legs for s in (leg.get("survivors") or [])),
        key=lambda s: (
            str(s.get("file")),
            int(s.get("line") or 0),
            str(s.get("operator")),
        ),
    )
    merged = {key: sum(int(leg.get(key) or 0) for leg in legs) for key in _SUMS}
    return {
        **base,
        "state": "ran",
        **merged,
        "survivors": survivors,
        "seconds": round(max(float(leg.get("seconds") or 0) for leg in legs), 1),
    }


def collect(legs_dir: Path) -> list[tuple[str, dict]] | dict:
    """The leg reports found under ``legs_dir``, or an error report."""
    found = sorted(legs_dir.glob(f"**/{REPORT_NAME}"))[:_MAX_FILES]
    reports: list[tuple[str, dict]] = []
    for path in found:
        try:
            reports.append((str(path.relative_to(legs_dir)), _read(path)))
        except (OSError, ValueError) as exc:
            return _error(f"{path.relative_to(legs_dir)}: unreadable ({exc})")
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--legs-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    collected = collect(Path(args.legs_dir))
    report = collected if isinstance(collected, dict) else merge(collected)
    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"merged mutation legs: state={report['state']} "
        f"legs={report.get('legs', '?')} {str(report.get('reason', ''))[:200]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
