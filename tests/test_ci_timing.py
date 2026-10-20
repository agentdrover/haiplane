"""#1921: the minutes of the mutation legs are one number in the hub, by step name.

The trusted report names a step's minutes by the step NAME and the hub adds them
up over every job (ci_timing.summarize). So the legs of the matrix and the
baseline job cost exactly what one sequential job cost, as long as the step names
stay the ones in TIMING_STEPS: a renamed step shows up as a missing key, never as
a wrong one.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from hub.services.ci_timing import CIRunTimingSubmit, summarize

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "ci_trusted_report", ROOT / "scripts" / "ci_trusted_report.py"
)
assert _spec is not None and _spec.loader is not None
trusted = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trusted)

MUTATIONS = "Mutations of changed functions (warning only)"
BASELINE = "Red-test baseline on merge-base (data only)"
FACTS = {
    "run_id": "9",
    "run_attempt": "1",
    "head_sha": "a" * 40,
    "event": "workflow_dispatch",
    "run_conclusion": "success",
    "updated_at": "2026-10-20T12:00:00Z",
}


def _job(name: str, step: str, seconds: int) -> dict:
    return {
        "name": name,
        "conclusion": "success",
        "started_at": "2026-10-20T10:00:00Z",
        "completed_at": "2026-10-20T10:20:00Z",
        "steps": [
            {
                "name": step,
                "conclusion": "success",
                "started_at": "2026-10-20T10:01:00Z",
                "completed_at": f"2026-10-20T10:{1 + seconds // 60:02d}:{seconds % 60:02d}Z",
            }
        ],
    }


def _summary(jobs: list[dict]) -> dict[str, float]:
    body = CIRunTimingSubmit(**trusted.timing_body(FACTS, jobs))
    _runner, steps, _complete = summarize(body)
    return steps


def _workflow_step_names() -> tuple[str, str]:
    """The names of the mutation step and the baseline step AS ci.yml carries them."""
    import yaml

    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    mutations = next(
        s["name"]
        for s in doc["jobs"]["mutation_legs"]["steps"]
        if s.get("id") == "mutations"
    )
    baseline = next(
        s["name"] for s in doc["jobs"]["baseline"]["steps"] if s.get("id") == "baseline"
    )
    return mutations, baseline


def test_mutation_legs_are_summed_by_step_name() -> None:
    mutations, baseline = _workflow_step_names()
    legs = [
        _job(f"Mutations leg {i}/3", mutations, seconds)
        for i, seconds in ((1, 100), (2, 120), (3, 90))
    ]
    steps = _summary(
        [
            *legs,
            _job("Red-test baseline", baseline, 30),
            _job("Mutations and red-test baseline", "Put the legs together", 5),
            _job("Tests part 1/4", "Test", 200),
            _job("Tests part 2/4", "Test", 210),
        ]
    )
    assert steps["mutations"] == 310.0, "the three legs add up"
    assert steps["baseline"] == 30.0
    assert steps["test"] == 410.0, "the parts of the suite add up the same way"
    assert set(steps) == {"mutations", "baseline", "test"}, (
        "the merge step is not a timed step: no key grows here"
    )
    # the names are the ones the reporter and this file know
    assert (mutations, baseline) == (MUTATIONS, BASELINE)
    assert mutations in trusted.TIMING_STEPS and baseline in trusted.TIMING_STEPS


def test_a_renamed_leg_step_loses_its_minutes_visibly() -> None:
    """The failure mode the invariant guards: a missing key, never a wrong one."""
    steps = _summary(
        [
            _job("Mutations leg 1/3", MUTATIONS, 100),
            _job("Mutations leg 2/3", "Mutations (renamed)", 120),
        ]
    )
    assert steps == {"mutations": 100.0}


def test_the_number_of_jobs_stays_far_below_what_the_reporter_reads() -> None:
    import yaml

    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    legs = len(doc["jobs"]["mutation_legs"]["strategy"]["matrix"]["leg"])
    parts = len(doc["jobs"]["pytest"]["strategy"]["matrix"]["shard"])
    jobs = len(doc["jobs"]) - 2 + legs + parts  # two jobs are matrices
    assert jobs < trusted.MAX_TIMED_JOBS // 2, jobs
