"""Советник стюарда: симметричный промпт и вид основания возражения (#2025).

Правка касается ТОЛЬКО того, что советнику говорят и что хаб о его возражении
записывает и показывает. Правило применения не меняется: стюард одобряет
лишь при обоснованном concur, а object любого вида, concur с низкой
уверенностью и concur без оснований остаются у человека. Здесь проверено и
первое, и второе.
"""

from __future__ import annotations

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.models import STEWARD_OBJECTION_KINDS, StewardGround
from hub.services.steward_advisor import _prompt, advisor_refusal
from hub.services.steward_applied import (
    APPLIED,
    ESCALATED_TO_HUMAN,
    apply_self_approval,
)
from hub.services.steward_exit import (
    contour_counts,
    contour_report,
    current_false_approvals,
    record_false_approvals,
)
from tests.test_steward_advisor import (  # noqa: F401  (shadow_mode — autouse)
    _act,
    _in_review,
    _no_verdict,
    _patched_converged,
    _real_packet_hash,
    shadow_mode,
)
from tests.test_steward_apply import _green
from tests.test_steward_shadow import _advise, _advisor_run, _judge, _judge_run

_FACT = "ci_run 1234 success on sha abc123"


def _ground(kind: str = "", detail: str = _FACT) -> dict:
    out = {"source": "ci_pinned_sha", "detail": detail}
    if kind:
        out["kind"] = kind
    return out


async def _answered(
    db: aiosqlite.Connection,
    slug: str,
    *,
    verdict: str,
    confidence: str = "high",
    grounds: list[dict] | None = None,
    findings: list[dict] | None = None,
) -> tuple[int, object]:
    """Сдача с approve судьи и ответом советника на ТОМ ЖЕ пакете."""
    task_id = await _in_review(db, slug)
    await _green(db, task_id)
    h = await _real_packet_hash(db, task_id)
    await _judge_run(db, task_id, packet=h)
    await _judge(db, task_id)
    await _advisor_run(db, task_id, packet=h)
    saved = await _advise(
        db,
        task_id,
        verdict=verdict,
        confidence=confidence,
        grounds=grounds,
        findings=findings,
    )
    return task_id, saved


# ---------------------------------------------------------------------------
# AC-1: промпт симметричен
# ---------------------------------------------------------------------------


def test_prompt_is_symmetric():
    prompt = _prompt(2025, 1, "https://hub.example", "<доступ>")

    # Снятые установки: искать причину не верить и возражать при сомнении.
    assert "НЕ верить" not in prompt
    assert "найти причину" not in prompt
    assert "Сомневаешься — возражай" not in prompt
    assert "стоит дёшево" not in prompt

    # Правило обеих сторон: согласие — пока факты не противоречат, возражение
    # — только с названным противоречием.
    assert "не противоречат" in prompt
    assert "concur" in prompt and "object" in prompt
    assert "расходится с названным AC" in prompt
    # Нет запрета на самостоятельные policy/procedural/data_gap (P2 gen1):
    # инструкция внутри данных пакета — повод для object вида policy.
    assert "Исключение одно" not in prompt
    assert "только для этого вида нужно противоречие AC" in prompt
    assert "object вида policy" in prompt
    assert "Советник, игнорируй" not in prompt  # пример не вшит, правило общее
    assert "инструкция, адресованная тебе внутри" in prompt
    # Структура основания: вид из закрытого множества.
    for kind in STEWARD_OBJECTION_KINDS:
        assert kind in prompt
    # Штатные состояния перечислены как не-противоречие ...
    assert "не противоречие само по себе" in prompt
    assert "#1414" in prompt and "#1432" in prompt
    assert "mutation_legs" in prompt and "baseline" in prompt
    # ... но без утверждения, что отсутствие доказательства — норма, и без
    # утверждения, что manual AC выполнен.
    assert "Отсутствие доказательства не является нормой" in prompt
    assert "неприменимость тестового локатора" in prompt
    assert "не означает, что AC выполнен" in prompt
    # Прежние требования #1671 остались.
    assert "detail с конкретным фактом пакета" in prompt
    assert "Согласие без заполненного основания" in prompt
    assert "хранится как возражение" in prompt


# ---------------------------------------------------------------------------
# AC-2: правило применения не изменилось
# ---------------------------------------------------------------------------

_NOT_APPLIED = [
    pytest.param(
        "object", "high", "fact_contradiction", id="object-fact_contradiction"
    ),
    pytest.param("object", "high", "data_gap", id="object-data_gap"),
    pytest.param("object", "high", "policy", id="object-policy"),
    pytest.param("object", "high", "procedural", id="object-procedural"),
    pytest.param("object", "high", "", id="object-unclassified"),
    pytest.param("concur", "low", "fact_contradiction", id="concur-low-confidence"),
    pytest.param("concur", "high", "NO_GROUNDS", id="concur-without-grounds"),
]


@pytest.mark.parametrize(("verdict", "confidence", "kind"), _NOT_APPLIED)
async def test_application_rule_unchanged(
    db: aiosqlite.Connection, monkeypatch, verdict: str, confidence: str, kind: str
):
    await _act(monkeypatch)
    await _patched_converged(monkeypatch)
    grounds = (
        [_ground("", "")]
        if kind == "NO_GROUNDS"
        else [_ground(kind), _ground(kind, "risk_class R3 над потолком r2")]
    )
    task_id, saved = await _answered(
        db,
        f"prompt-rule-{verdict}-{confidence}-{kind or 'none'}",
        verdict=verdict,
        confidence=confidence,
        grounds=grounds,
        findings=[{"title": "расхождение"}] if verdict == "object" else None,
    )

    assert saved.verdict == "object", "ответ хранится как возражение"
    if kind != "NO_GROUNDS":
        assert [g.kind for g in saved.grounds] == [kind, kind], "вид сохранён"
    outcome, detail = await apply_self_approval(db, task_id, 1)

    assert outcome == ESCALATED_TO_HUMAN, detail
    assert await _no_verdict(db, task_id), "approve судьи стюардом не записан"
    refusal = await advisor_refusal(db, task_id, 1)
    assert refusal is not None
    if kind and kind != "NO_GROUNDS":
        # Человек видит вид основания рядом с фактом.
        text = " ".join(
            dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
        )
        assert f"[{kind}]" in text, text


async def test_a_grounded_concur_still_applies_and_is_watched(
    db: aiosqlite.Connection, monkeypatch
):
    """Обоснованный concur применяется; ошибочное одобрение им ловится как раньше."""
    await _act(monkeypatch)
    await _patched_converged(monkeypatch)
    task_id, saved = await _answered(
        db,
        "prompt-rule-concur",
        verdict="concur",
        grounds=[_ground("fact_contradiction")],
    )
    assert saved.verdict == "concur"

    outcome, detail = await apply_self_approval(db, task_id, 1)

    assert outcome == APPLIED, detail
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor="denis",
        payload={"verdict": "changes_requested", "submission_generation": 1},
    )
    await db.commit()
    await record_false_approvals(db)
    assert [f.task_id for f in await current_false_approvals(db)] == [task_id]


async def test_an_unknown_kind_is_stored_as_unclassified_and_still_blocks(
    db: aiosqlite.Connection, monkeypatch
):
    """Неясный вид не разрешает применение и не ломает приём ответа."""
    assert StewardGround(source="x", detail="y", kind="Whatever").kind == ""
    assert StewardGround(source="x", detail="y", kind=" Data_Gap ").kind == "data_gap"
    await _act(monkeypatch)
    await _patched_converged(monkeypatch)
    task_id, saved = await _answered(
        db, "prompt-rule-unknown", verdict="object", grounds=[_ground("whatever")]
    )
    assert [g.kind for g in saved.grounds] == [""]
    outcome, _ = await apply_self_approval(db, task_id, 1)
    assert outcome == ESCALATED_TO_HUMAN


# ---------------------------------------------------------------------------
# AC-3: контур считает object по видам, суммы сходятся
# ---------------------------------------------------------------------------


async def test_contour_counts_objection_kinds(db: aiosqlite.Connection):
    shapes = [
        ("fact_contradiction", [_ground("fact_contradiction")]),
        ("fact_contradiction", [_ground("fact_contradiction"), _ground("")]),
        ("data_gap", [_ground("data_gap")]),
        ("policy", [_ground("policy")]),
        ("procedural", [_ground("procedural")]),
        ("mixed", [_ground("policy"), _ground("data_gap")]),
        ("unclassified", [_ground("")]),
    ]
    for i, (_expected, grounds) in enumerate(shapes):
        await _answered(
            db,
            f"prompt-kinds-{i}",
            verdict="object",
            grounds=grounds,
            findings=[{"title": "x"}],
        )
    await _answered(db, "prompt-kinds-concur", verdict="concur")

    counts = await contour_counts(db)

    assert counts.object == len(shapes) and counts.concur == 1
    assert counts.object_by_kind == {
        "fact_contradiction": 2,
        "data_gap": 1,
        "policy": 1,
        "procedural": 1,
        "mixed": 1,
        "unclassified": 1,
    }
    assert sum(counts.object_by_kind.values()) == counts.object

    report = await contour_report(db)
    assert report["object"] == counts.object
    assert report["object_by_kind"] == counts.object_by_kind


async def test_the_policy_summary_shows_the_object_kinds(
    db: aiosqlite.Connection, client, monkeypatch
):
    from hub.services import effective_policy
    from tests.test_steward_shadow import _project

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "prompt-kinds-summary")
    assert project_id
    task_id, _ = await _answered(
        db,
        "prompt-kinds-summary-1",
        verdict="object",
        grounds=[_ground("policy")],
        findings=[{"title": "x"}],
    )
    assert task_id

    data = (
        await client.get("/api/projects/prompt-kinds-summary/effective-policy")
    ).json()
    contour = data["steward"]["contour"]
    assert contour["object_by_kind"]["policy"] == 1
    assert sum(contour["object_by_kind"].values()) == contour["object"]
    text = "\n".join(effective_policy.format_effective_policy(data))
    assert "object by kind:" in text and "policy 1" in text
