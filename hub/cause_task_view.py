"""The brief block that names the task whose delivery broke this one (#1858).

Kept out of ``models.py`` (file-size budget) the way ``plain_summary`` is: a
view model with no imports from the hub, so ``models`` can take it without a
cycle.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CauseTaskView(BaseModel):
    """Task whose delivery broke this one; ``state`` is ``known`` or ``no_data``.

    ``affected_areas`` are what that task DECLARED, not what its diff touched;
    ``areas_note`` says so, so a reviewer does not read them as a measurement.
    """

    state: str = "no_data"
    task_id: int | None = None
    title: str = ""
    link: str = ""
    affected_areas: list[str] = Field(default_factory=list)
    areas_note: str = ""
    cause_kind: str = "regression"
    note: str = ""
