"""Check history and constraints when permitting a zero-cost reservation."""

import sqlite3
from contextlib import closing

import pytest

from hub.migrations.steps.m202611182245_2383_epic_plan_dispatch import (
    MIGRATIONS as original_steps,
)
from hub.migrations.steps.m202611201530_2391_zero_cost_reservations import (
    MIGRATIONS as zero_cost_steps,
)


def test_zero_cost_migration_keeps_history_and_unique_order():
    with closing(sqlite3.connect(":memory:")) as db:
        for _, sql in original_steps:
            db.execute(sql)
        add = """INSERT INTO epic_plan_work
            (id,task_id,plan_version,phase,generation,sha,attempt,host,model,
             principal_id,actor_principal_id,authority_source,authority_ref,
             grant_expires_at,ceiling_cents,charged_cents,executor_order_id)
             VALUES (?,2377,'version','executor',1,'sha',?,'k8','codex',
                     7,7,'source',?,'2026-11-24T00:00:00Z',?,?,?)"""
        db.execute(add, (3, 1, "old", 100, 37, 41))
        original = db.execute("SELECT * FROM epic_plan_work").fetchone()
        for _, sql in zero_cost_steps:
            db.execute(sql)
        assert db.execute("SELECT * FROM epic_plan_work").fetchone() == original
        db.execute(add, (4, 2, "included", 0, None, 42))
        for duplicate in [(6, 3, "old", 0, None, 44), (7, 3, "new", 0, None, 41)]:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(add, duplicate)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(add, (5, 3, "bad", -1, None, 43))
