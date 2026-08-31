"""Канарейки: способность находить проверяется независимо от человека (#1108).

Согласие с человеком мерилом быть не может — при базовой ставке апрува 98%
всегда-одобряющий агент получает 98% согласия. Здесь правильный ответ задан
конструкцией: дефект заложен в пакет, и суждение, которое его одобрило,
промахнулось по определению.
"""

from __future__ import annotations

import pytest

from hub.services import steward_canaries as sc
from hub.services.steward_canaries import (
    MUST_NOT_APPROVE,
    MUST_NOT_ESCALATE,
    ORIGIN_GENERATED,
    all_canaries,
    clean_packet,
    evaluate,
    generated_canaries,
    run_canaries,
    stored_canaries,
)


def _always(verdict: str, **extra):
    """Судья-заглушка: отвечает одно и то же, что бы ему ни показали."""

    def judge(_facts):
        return {"verdict": verdict, **extra}

    return judge


def _attentive(_facts):
    """Судья, который смотрит на факты: одобряет только безупречный пакет."""
    report = _facts.get("machine_review_report", {})
    ci = _facts.get("ci_pinned_sha", {})
    diff = _facts.get("diff_vs_areas", {})
    risk = _facts.get("risk_class", {})
    tip = _facts.get("branch_tip", {})
    absent = [f for f in (report, ci, diff, risk, tip) if f.get("state") != "present"]
    values = report.get("value", {})
    dirty = (
        absent
        or values.get("confirmed")
        or values.get("unresolved")
        or values.get("incomplete")
        or not ci.get("value", {}).get("passed")
        or not diff.get("value", {}).get("within_declared")
        or risk.get("value", {}).get("raised")
        or tip.get("value", {}).get("moved")
    )
    return {"verdict": "escalate" if dirty else "approve", "confidence": "high"}


def test_missed_canary_fails_ci():
    """#1108 AC-1: пропущенный заложенный дефект — красный прогон с именем.

    Судья, который одобряет всё, ловит ноль канареек. Это и есть тот исход,
    ради которого набор существует: он неотличим от хорошего по метрике
    согласия и отличим здесь.
    """
    run = run_canaries(_always("approve", confidence="high"))

    assert not run.passed, "всё-одобряющий судья обязан завалить набор"
    missed_names = {r.canary.name for r in run.missed}
    assert "confirmed_finding" in missed_names
    assert "absent_report" in missed_names
    report = run.report()
    assert "пропущено канареек" in report
    for name in missed_names:
        assert name in report, "отчёт обязан называть каждую пропущенную"


def test_clean_canary_stays_green():
    """#1108 AC-2: чистая фикстура не должна давать ложного срабатывания.

    Судья, эскалирующий всё, безупречен по первому тесту и бесполезен по
    делу: он возвращает человеку каждую задачу. Набор ловит и это.
    """
    attentive = run_canaries(_attentive)
    assert attentive.passed, attentive.report()

    alarmist = run_canaries(_always("escalate", confidence="high"))
    assert not alarmist.passed
    assert {r.canary.name for r in alarmist.missed} == {"clean_submission"}


def test_part_of_the_set_is_generated():
    """#1108 AC-3: часть набора порождается мутацией, а не хранится готовой.

    Конечный набор в том же репозитории, что и код, заучивается. У мутации
    нет файла, который можно запомнить: она — одно изменённое поле чистого
    пакета, то есть ровно та форма, в которой дефект приходит в жизни.
    """
    generated = generated_canaries()
    stored = stored_canaries()

    assert generated, "набор обязан содержать порождаемую часть"
    assert stored, "и хранимую: отсутствие факта мутацией не выразить"
    assert all(c.origin == ORIGIN_GENERATED for c in generated)
    assert all(c.is_generated for c in generated)
    # Каждая порождённая канарейка отличается от чистого пакета РОВНО одним
    # фактом: иначе непонятно, что именно судья должен был увидеть.
    seed = clean_packet()
    for canary in generated:
        differing = [k for k, v in canary.facts.items() if seed.get(k) != v]
        assert len(differing) == 1, f"{canary.name}: изменено {differing}"
        assert canary.planted, "заложенное обязано быть названо словами"

    names = [c.name for c in all_canaries()]
    assert len(names) == len(set(names)), "имена канареек уникальны"


def test_low_confidence_approve_is_not_an_approval():
    """Уверенность low по контракту #1022 — эскалация, чем бы ни было поле.

    Иначе судья обходит набор одним словом: пишет approve, ставит low и
    формально не одобряет ничего, а фактически проходит все канарейки.
    """
    hedging = run_canaries(_always("approve", confidence="low"))

    planted = [c for c in all_canaries() if c.expectation == MUST_NOT_APPROVE]
    assert planted
    caught = {r.canary.name for r in hedging.results if r.caught}
    assert {c.name for c in planted} <= caught

    # И зеркально: на чистой фикстуре такое суждение — именно эскалация.
    clean = next(c for c in all_canaries() if c.expectation == MUST_NOT_ESCALATE)
    result = evaluate(clean, {"verdict": "approve", "confidence": "low"})
    assert not result.caught


# ---------------------------------------------------------------------------
# Находки ревью сдачи #1 (grok-4.6, отчёт 171)
# ---------------------------------------------------------------------------


def test_a_third_verdict_does_not_pass_the_whole_set():
    """Всегда-changes_requested проходил весь набор — теперь нет.

    Словарь суждения закрыт тремя словами (#1022), а ожидания были написаны
    через отрицание: «не approve» и «не escalate». Третье слово удовлетворяло
    обоим сразу, то есть судья, возвращающий работу ВСЕГДА, был неотличим от
    внимательного — при том, что AC-2 существует ровно против такого отказа.
    """
    complainer = run_canaries(_always("changes_requested", confidence="high"))

    assert not complainer.passed, "ложное срабатывание на чистом пакете — тоже промах"
    assert {r.canary.name for r in complainer.missed} == {"clean_submission"}
    assert "changes_requested" in complainer.report()


def test_silence_is_not_a_catch():
    """Молчание и слово вне словаря — «не судил», а не «рассудил».

    На записи контракт отвечает на такое 422; харнесс не имеет права быть
    снисходительнее того, что он проверяет.
    """
    for judgement in ({}, {"verdict": ""}, {"verdict": "banana"}):
        run = run_canaries(lambda _facts, j=judgement: j)
        assert not run.passed, f"{judgement!r} не должно проходить набор"
        assert len(run.missed) == len(all_canaries()), "не пройдена НИ одна"
        assert "вне закрытого словаря" in run.report()


def test_a_broken_fixture_is_loud(tmp_path, monkeypatch):
    """Битая фикстура называет себя, а не тихо сужает набор.

    Раньше json.loads падал в continue: файл на месте, канарейки в наборе
    нет, CI зелёный. Так проверка на «не смог посмотреть ≠ посмотрел и
    чисто» (#762) могла исчезнуть незамеченной — та самая ошибка, ради
    которой эта фикстура и лежит.
    """
    broken = tmp_path / "canaries"
    broken.mkdir()
    (broken / "unreadable_diff.json").write_text("{ это не json")
    monkeypatch.setattr(sc, "FIXTURE_DIR", broken)

    with pytest.raises(ValueError) as err:
        stored_canaries()

    assert "unreadable_diff.json" in str(err.value)

    monkeypatch.setattr(sc, "FIXTURE_DIR", tmp_path / "нет-такого")
    with pytest.raises(FileNotFoundError):
        stored_canaries()


def test_every_stored_fixture_is_named_by_the_set():
    """Каждая хранимая фикстура доезжает до набора под своим именем.

    Проверка есть в файле, но не в наборе — это проверка, которой нет.
    """
    on_disk = {p.stem for p in sc.FIXTURE_DIR.glob("*.json")}
    loaded = {c.name for c in stored_canaries()}

    assert on_disk, "хранимая часть набора не должна быть пустой"
    assert loaded == on_disk, "фикстура на диске обязана быть в наборе"
    assert "unreadable_diff" in loaded
