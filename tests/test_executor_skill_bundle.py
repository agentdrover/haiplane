"""The manifest hashes raw bodies and rejects unsafe or incomplete input."""

from hashlib import sha256

import pytest

from hub.services import executor_skill_bundle as skills
from tests.test_executor_skill_delivery import SKILLS, _publish


async def _seed(db, overrides=None):
    overrides = overrides or {}
    versions = {}
    bodies = {}
    for name in SKILLS:
        body = overrides.get(name, f"  Инструкция {name}  \r\n\n")
        versions[name] = await _publish(db, name, body)
        bodies[name] = body
    return versions, bodies


async def test_active_bundle_is_complete(db):
    versions, bodies = await _seed(db)
    await _publish(db, "hub-operator", "Неактивный черновик", status="draft")

    snapshot = await skills.read_active_bundle(db)
    assert snapshot.discipline_version == versions[SKILLS[0]]
    expected = ["<<<HAIPLANE_SKILL_BUNDLE_BEGIN>>>"]
    for name in SKILLS:
        raw = bodies[name]
        digest = sha256(raw.encode("utf-8")).hexdigest()
        expected += [
            f"<<<HAIPLANE_SKILL_BEGIN slug={name} version={versions[name]} sha256={digest}>>>",
            raw,
            "<<<HAIPLANE_SKILL_END>>>",
        ]
    expected.append("<<<HAIPLANE_SKILL_BUNDLE_END>>>")
    assert snapshot.text.encode("utf-8") == "\n".join(expected).encode("utf-8")


async def test_bundle_size_and_reserved_content(db):
    await _seed(db, {"hub-operator": "x"})
    baseline = await skills.read_active_bundle(db)
    remaining = 131072 - len(baseline.text.encode("utf-8"))
    padding = ("я" * (remaining // 2)) + ("x" * (remaining % 2))
    await _publish(db, "hub-operator", "x" + padding)
    inclusive = await skills.read_active_bundle(db)
    assert len(inclusive.text.encode("utf-8")) == 131072

    await _publish(db, "hub-operator", "xx" + padding)
    with pytest.raises(skills.SkillBundleError, match="executor_skill_bundle_oversize"):
        await skills.read_active_bundle(db)

    for forbidden in (
        " ",
        "встроенный\0нуль",
        "{{PAIR_CODE}}",
        "<<<HAIPLANE_SKILL_END>>>",
    ):
        await _publish(db, "hub-operator", forbidden)
        with pytest.raises(
            skills.SkillBundleError, match="executor_skill_bundle_invalid"
        ):
            await skills.read_active_bundle(db)


def test_task_implementer_wrapper_scopes_the_bundle():
    text = skills.TASK_IMPLEMENTER_RULES
    assert "task implementer" in text
    assert "hub_get_skill" in text and "уже вложены" in text
    assert "Не запрашивай /api/skills" in text
    assert "GET /api/tasks/{task_id}/review-brief" in text
    assert "POST /api/tasks/{task_id}/submit-review" in text
    assert "актуальные working_rules и политика проекта" in text
    assert "независимого ревьюера и решения человека" in text
    assert "После принятой сдачи заверши прогон" in text
