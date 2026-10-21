"""Карточка C2 без справки (#1812, возврат gen1): состояние названо явно.

needs_info, open, ci_check, pending_report, failed, rejected справки не имеют
(``decision_brief`` отвечает «действий нет»). Карточка не должна молчать: тон,
слово, «От вас» и «К действию» берутся из общих читателей (статус,
``compute_awaiting``/``compute_next_action``, вопрос из ленты), а не из CSS.
"""

from __future__ import annotations

import re

import pytest

from hub import repository as repo
from hub.services import card_c2
from tests.test_decision_brief import _task_in

CASES = [
    ("needs_info", "amber", "Нужен ваш ответ"),
    ("open", "gray", "Не начато"),
    ("ci_check", "blue", "Идёт проверка CI"),
    ("pending_report", "amber", "Ждёт отчёта и решения"),
    ("failed", "red", "Задача не выполнена"),
    ("rejected", "red", "Отклонено"),
]


@pytest.mark.parametrize("status, tone, label", CASES)
def test_no_brief_notice_names_the_state(status, tone, label):
    got = card_c2.card_view({"id": 5, "status": status, "title": "t"}, None)
    assert got["tone"] == tone
    assert got["tone_label"] == label
    assert got["from_you"], "нет строки «От вас»"
    # Кнопка только к форме, которая есть: у остальных состояний её нет.
    assert got["action_target"] == ("web-answer-form" if status == "needs_info" else "")
    assert got["shape"]


def test_needs_info_carries_the_question():
    got = card_c2.card_view(
        {"id": 5, "status": "needs_info", "title": "t"},
        None,
        question="Какой формат выбрать?",
    )
    assert "Какой формат выбрать?" in got["from_you"]
    assert got["action_target"] == "web-answer-form"


def test_a_failed_assembly_is_not_a_known_state():
    # Статус с решением, но справки нет (сбой сборки): серый и «не собрана».
    got = card_c2.card_view({"id": 5, "status": "review", "title": "t"}, None)
    assert got["tone"] == "gray"
    assert "не собран" in got["tone_label"]


@pytest.mark.parametrize("status, tone, label", CASES)
async def test_page_states_without_a_brief(client, db, status, tone, label):
    task_id = await _task_in(db, status)
    if status == "needs_info":
        await repo.add_task_update(db, task_id, "dev", "question", "Что выбрать?")
        await db.commit()
    page = (await client.get(f"/tasks/{task_id}")).text
    root = page[page.index('id="decision-brief"') :]
    assert f'data-tone="{tone}"' in root[:300] or f'data-tone="{tone}"' in root
    happened = root[root.index('data-c2-section="happened"') :]
    happened = happened[: happened.index("</section>")]
    assert label in happened
    assert "От вас:" in happened
    assert "Справка не собрана" not in root[: root.index('id="c2-checks"')]
    # «К действию» ведёт на форму, которая есть на странице.
    found = re.search(r'class="c2-btn" href="#([\w-]+)"', root)
    if status != "needs_info":
        assert found is None, "кнопка ведёт к несуществующему действию"
    else:
        assert f'id="{found.group(1)}"' in page, found.group(1)
    target = found.group(1) if found else ""
    # Статус не потерян: слово статуса стоит в шапке C2, не только в бейдже героя.
    header = root[: root.index("</section>")]
    assert f">{status}<" in header
    if status == "needs_info":
        assert target == "web-answer-form"
        assert "Что выбрать?" in happened
