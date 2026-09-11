"""Применение суждения: стюард впервые двигает чужую задачу (#1149).

До этого он советует, и ошибка стоит строки в карточке. Здесь он меняет
исход, и проверяется не «умеет ли применить», а три правила, каждое из
которых существует против своей ошибки: клиентский путь без облачного
исполнителя, общий бюджет циклов и старшинство человека.
"""

from __future__ import annotations

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import config
from hub import repository as repo
from hub.models import ReviewVerdict, TaskReviewVerdict
from hub.models import (
    ACTestResultView,
    AcceptanceCriterion,
    ACVerifiableBy,
    CIRunReportState,
    EvidenceCoverage,
    LiveCheckState,
    MachineReviewView,
    PrepassState,
    TaskStatus,
)
from hub.services.steward_applied import (
    APPLIED,
    ESCALATED_TO_HUMAN,
    RETURNED,
    SELF_APPROVAL_FORBIDS,
    SELF_APPROVAL_SIGNALS,
    SelfApproval,
    apply_judgement,
    approve_without_a_human,
    self_approval,
    self_approval_for,
)
from hub.services.steward_evidence import ReviewBrief
from tests.test_steward_shadow import _project, _task


async def _judge(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    verdict: str,
    generation: int = 1,
) -> None:
    """Суждение приходит контрактом #1022 — тем же путём, что у живого прогона."""
    from hub.config import TokenIdentity
    from hub.models import StewardJudgementSubmit
    from hub.services.steward_judgement import record_steward_judgement

    await record_steward_judgement(
        db,
        task_id,
        StewardJudgementSubmit(
            generation=generation,
            kind="verdict",
            verdict=verdict,
            confidence="high",
            escalate_reason="precondition_failed" if verdict == "escalate" else None,
            model="gpt-5.3-codex",
        ),
        TokenIdentity("steward-bot", "steward", principal_id=42),
    )


async def _client_task(db: aiosqlite.Connection, project_id: int, **fields) -> int:
    """Задача клиентского пути: в review и БЕЗ review_job_id."""
    task_id = await _task(db, project_id)
    await repo.update_task(db, task_id, status="review", review_job_id="", **fields)
    await db.commit()
    return task_id


async def test_client_path_changes_requested(db: aiosqlite.Connection, monkeypatch):
    """AC-1: возврат в ту же ветку, цикл +1, и НИЧЕГО больше.

    У клиентской задачи нет облачного исполнителя, которому можно поручить
    правку. Серверный маршрут породил бы либо висящий job, либо вторую
    задачу на ту же работу — поэтому проверяется не только куда задача
    ушла, но и что рядом ничего не появилось.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    project_id = await _project(db, "applied-client")
    task_id = await _client_task(db, project_id)
    before = dict(await repo.get_task(db, task_id))
    await _judge(db, task_id, verdict="changes_requested")

    outcome, detail = await apply_judgement(db, task_id, 1)

    assert outcome == RETURNED, detail
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", "работа возвращается автору, а не висит"
    assert task["branch"] == before["branch"], "та же ветка — правка идёт туда же"
    assert task["review_cycle"] == (before["review_cycle"] or 0) + 1
    assert not (task.get("review_job_id") or ""), (
        "review_job_id на клиентском пути не появляется: поручать правку некому"
    )
    from hub.db import fetchall

    children = await fetchall(db, "SELECT id FROM tasks WHERE parent_id=?", (task_id,))
    assert list(children) == [], "параллельных fix-задач не создаётся"


async def test_budget_exhausted_client_path_needs_decision(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-2: исчерпанный бюджет ведёт к человеку существующим переходом.

    Арбитра на клиентском пути нет, и отдельной квоты для стюарда тоже:
    счётчик, который никто не сверяет с общим, разъезжается. Проверяется
    по КРАЮ — на единицу меньше потолка работа ещё возвращается автору, на
    потолке уходит к человеку.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    project_id = await _project(db, "applied-budget")

    almost = await _client_task(db, project_id, review_cycle=2)
    await _judge(db, almost, verdict="changes_requested")
    outcome, _ = await apply_judgement(db, almost, 1)
    assert outcome == RETURNED, "на единицу меньше потолка бюджет ещё есть"

    spent = await _client_task(db, project_id, review_cycle=3)
    await _judge(db, spent, verdict="changes_requested")

    outcome, detail = await apply_judgement(db, spent, 1)

    assert outcome == ESCALATED_TO_HUMAN, detail
    task = dict(await repo.get_task(db, spent))
    assert task["status"] == "needs_decision"
    assert task["review_cycle"] == 3, "исчерпанный бюджет не тратится дальше"
    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, spent)]
    assert any("Бюджет циклов ревью исчерпан" in c for c in updates), (
        "молчаливая эскалация неотличима от зависшей задачи"
    )


async def test_the_budget_question_has_one_owner(db: aiosqlite.Connection, monkeypatch):
    """Потолок читается из общей функции, а не сравнивается на месте.

    #423 прямо запрещает любому потоку сравнивать review_cycle с
    MAX_REVIEW_CYCLES самостоятельно. Проверяется сдвигом потолка: при
    MAX=1 та же задача с одним циклом уже уходит к человеку, при MAX=9 —
    ещё возвращается автору. Своё сравнение на это не отреагировало бы.
    """
    project_id = await _project(db, "applied-owner")

    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 9)
    generous = await _client_task(db, project_id, review_cycle=1)
    await _judge(db, generous, verdict="changes_requested")
    assert (await apply_judgement(db, generous, 1))[0] == RETURNED

    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 1)
    strict = await _client_task(db, project_id, review_cycle=1)
    await _judge(db, strict, verdict="changes_requested")
    assert (await apply_judgement(db, strict, 1))[0] == ESCALATED_TO_HUMAN


async def test_human_verdict_wins_race(db: aiosqlite.Connection, monkeypatch):
    """AC-3: вердикт на эту генерацию уже стоит — суждение опоздало.

    Проверяется ОБОИМИ порядками прихода, потому что «человек старше» —
    правило о старшинстве, а не о скорости: и когда человек успел раньше,
    и когда стюард уже применил, второй записи не будет.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    project_id = await _project(db, "applied-race")

    # Человек успел раньше.
    from hub.services.lifecycle import record_review_verdict

    first = await _client_task(db, project_id)
    await _judge(db, first, verdict="approve")
    await record_review_verdict(
        db,
        first,
        TaskReviewVerdict(agent="Denis", verdict=ReviewVerdict.approved),
    )
    with pytest.raises(HTTPException) as refused:
        await apply_judgement(db, first, 1)
    assert refused.value.status_code == 409
    assert "человек старше" in str(refused.value.detail)

    # Стюард уже применил: второе применение — тот же отказ, то же поле.
    second = await _client_task(db, project_id)
    await _judge(db, second, verdict="approve")
    assert (await apply_judgement(db, second, 1))[0] == APPLIED
    with pytest.raises(HTTPException) as twice:
        await apply_judgement(db, second, 1)
    assert twice.value.status_code == 409


async def test_an_approve_is_recorded_as_a_verdict_by_the_steward(
    db: aiosqlite.Connection, monkeypatch
):
    """Применение видно там же, где человеческое решение, и с актором steward.

    Событие steward_applied из #1023 пишется в момент ЗАПИСИ суждения и
    означает «вердикт не эскалация», а не «применено» — следом применения
    служит обычная запись вердикта. Она же кладёт суждение в те же
    метрики, где считаются человеческие решения.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    project_id = await _project(db, "applied-trail")
    task_id = await _client_task(db, project_id)
    await _judge(db, task_id, verdict="approve")

    assert (await apply_judgement(db, task_id, 1))[0] == APPLIED

    task = dict(await repo.get_task(db, task_id))
    assert task["review_verdict"] == "approved"
    assert task["review_verdict_generation"] == 1
    events = await repo.list_events(
        db, since=0, kinds=["review_verdict_recorded"], limit=20
    )
    mine = [dict(e) for e in events if dict(e)["task_id"] == task_id]
    assert mine and mine[-1]["actor"] == "steward", (
        "актор обязан называть, кто решил: иначе суждение стюарда неотличимо "
        "от человеческого в тех же метриках"
    )


async def test_an_escalation_is_not_applied(db: aiosqlite.Connection, monkeypatch):
    """Эскалация — отказ судить, применять в ней нечего.

    Отдельный тест, потому что молчаливый пропуск незнакомого вердикта
    выглядел бы как применение: задача осталась бы в review без записи, и
    отличить это от «применили и ничего не изменилось» было бы нечем.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    project_id = await _project(db, "applied-escalate")
    task_id = await _client_task(db, project_id)
    await _judge(db, task_id, verdict="escalate")

    with pytest.raises(HTTPException) as refused:
        await apply_judgement(db, task_id, 1)

    assert refused.value.status_code == 409
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "review", "задача не двинулась"
    assert not (task.get("review_verdict") or ""), "и вердикта не появилось"


async def test_a_judgement_about_an_older_submission_is_refused(
    db: aiosqlite.Connection, monkeypatch
):
    """Суждение о прошлой сдаче не становится вердиктом нынешней.

    Найдено кросс-модельным ревью на первой сдаче #1149 и воспроизведено
    здесь тем же сценарием: человек одобрил живую сдачу, а суждение о
    ПРЕДЫДУЩЕЙ приходит следом. Без пина оно записывалось вердиктом на
    текущую генерацию — потому что запись вердикта привязывает его к
    текущей сдаче, а не к той, о которой судили.

    Проверка «вердикт на эту генерацию уже стоит» этот случай пропускает
    по устройству: она сравнивает поле с ЗАПРОШЕННОЙ генерацией, и чужая
    проходит мимо неё именно потому, что чужая. Поэтому тест смотрит на
    вердикт задачи ПОСЛЕ отказа — что он остался человеческим.
    """
    project_id = await _project(db, "applied-stale-gen")
    task_id = await _client_task(db, project_id, submission_generation=2)
    await _judge(db, task_id, verdict="changes_requested", generation=1)
    await repo.update_task(
        db,
        task_id,
        review_verdict="approved",
        review_verdict_generation=2,
    )
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await apply_judgement(db, task_id, 1)
    assert exc.value.status_code == 409
    assert "живая сдача" in str(exc.value.detail)

    row = dict(await repo.get_task(db, task_id))
    assert row["review_verdict"] == "approved"
    assert row["review_verdict_generation"] == 2


async def test_the_exhausted_budget_escalates_once(
    db: aiosqlite.Connection, monkeypatch
):
    """Второе применение на исчерпанном бюджете отказывает, а не алертит снова.

    Найдено ревью как unresolved и подтверждено: бюджетный путь вердикта
    не пишет, поэтому замок «вердикт уже стоит» его не держит. Раньше
    повтор молча проваливал переход и всё равно клал в карточку второй
    алерт — два одинаковых события там, где произошло одно.

    Заодно проверяется само событие: канонический путь эскалации пишет
    needs_decision с причиной review_cycle_limit, и стюард обязан писать
    ТО ЖЕ, иначе счётчик исчерпанных бюджетов разойдётся по тому, кто
    вернул работу.
    """
    project_id = await _project(db, "applied-budget-once")
    task_id = await _client_task(
        db,
        project_id,
        submission_generation=1,
        review_cycle=config.MAX_REVIEW_CYCLES,
    )
    await _judge(db, task_id, verdict="changes_requested", generation=1)

    outcome, _ = await apply_judgement(db, task_id, 1)
    assert outcome == ESCALATED_TO_HUMAN

    events = await repo.fetchall(
        db,
        "SELECT kind, payload FROM events WHERE task_id = ? AND kind = ?",
        (task_id, ESCALATED_TO_HUMAN),
    )
    assert len(events) == 1
    assert "review_cycle_limit" in str(dict(events[0])["payload"])

    alerts_before = await _budget_alerts(db, task_id)
    with pytest.raises(HTTPException) as exc:
        await apply_judgement(db, task_id, 1)
    assert exc.value.status_code == 409
    assert await _budget_alerts(db, task_id) == alerts_before


async def _budget_alerts(db: aiosqlite.Connection, task_id: int) -> int:
    rows = await repo.fetchall(
        db,
        "SELECT content FROM task_updates WHERE task_id = ? AND kind = 'alert'",
        (task_id,),
    )
    return sum(1 for r in rows if "Бюджет циклов ревью исчерпан" in dict(r)["content"])


# ---------------------------------------------------------------------------
# #1231 — самостоятельное одобрение по сошедшемуся набору свидетельств
# ---------------------------------------------------------------------------

_SHA_1231 = "b" * 40


def _brief(**over) -> ReviewBrief:
    """Сдача #1164/#1216 от 09.09.2026: набор, который сошёлся ЦЕЛИКОМ.

    Заводится целиком и портится ровно в одном месте каждым тестом ниже —
    тот же приём, которым проверяется привратник (#1147): тест, где чисто
    всё, кроме проверяемого, отличает сработавшее правило от несобранного
    брифа. Тест, начинающийся с пустого брифа, «поймал» бы любой признак
    любой проверкой.
    """
    mr_over = over.pop("machine_review_fields", {})
    fields: dict = dict(
        task_id=1231,
        title="сдача, у которой всё сошлось",
        status=TaskStatus.review,
        submission_generation=1,
        submission_sha=_SHA_1231,
        sha_check="match",
        sha_check_reason="вершина ветки и закреплённый коммит совпадают",
        machine_review=MachineReviewView(
            **{
                "id": 322,
                "task_id": 1231,
                "submission_generation": 1,
                "is_current": True,
                "incomplete": False,
                "lost_dimensions": [],
                "self_reviewed": False,
                "submitted_by": "cursor-cloud-reviewer",
                **mr_over,
            }
        ),
        prepass=PrepassState(state="covered", head_sha=_SHA_1231, passed=["lint"]),
        ci_run_report=CIRunReportState(state="current", head_sha=_SHA_1231),
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1",
                given="сошедшийся набор",
                when="стюард выносит суждение",
                then="APPROVED без человека",
                verifiable_by=ACVerifiableBy.test,
                test_ref="tests/test_steward_applied.py::x",
            )
        ],
        ac_test_results=[
            ACTestResultView(ac_id="AC-1", status="pass", is_current=True)
        ],
        evidence_coverage=EvidenceCoverage(
            state="complete",
            headline="every applicable evidence block produced a signal",
        ),
        live_check=LiveCheckState(state="done", observation="прод отвечает"),
    )
    fields.update(over)
    return ReviewBrief(**fields)


def _decide(**over) -> SelfApproval:
    return self_approval(
        _brief(**over), diff_paths=["hub/services/x.py"], reviewer_reachable=True
    )


async def test_a_fully_evidenced_submission_is_approved_without_a_human():
    """AC-1: сошёлся весь набор — одобрение самостоятельное, и оно названо.

    Проверяется не только «разрешено», но и ПЕРЕЧЕНЬ: одобрение, не
    назвавшее оснований, человек на спот-чеке проверить не может, а
    правило без перечня нельзя ни расширить, ни отозвать по причине.
    """
    decision = _decide()

    assert decision.allowed, decision.reason
    assert decision.missing == ()
    assert decision.forbidden == ()
    assert decision.converged == SELF_APPROVAL_SIGNALS, (
        "решение обязано стоять на ВСЕХ восьми признаках поимённо: "
        "одобрение без перечня оснований нечем проверить"
    )
    assert len(SELF_APPROVAL_SIGNALS) == 8


async def test_each_missing_signal_names_itself_and_calls_a_human():
    """AC-2: ровно один признак не сошёлся — к человеку, и назван ИМЕННО он.

    Восемь прогонов, а не один общий: каждая порча трогает свой признак и
    обязана уронить именно его. Тест, проверяющий «не одобрено», пережил бы
    снятие любой из восьми проверок — правило требует «названо это», а не
    «не зелёное».
    """
    spoilers: dict[str, dict] = {
        "report_is_current": {"machine_review_fields": {"is_current": False}},
        "no_confirmed_findings": {
            "machine_review_fields": {
                "findings_confirmed": [{"title": "гонка", "severity": "high"}]
            }
        },
        "no_unresolved_findings": {
            "machine_review_fields": {
                "unresolved": [
                    {"title": "не рассудили", "why": "адъюдикаторы разошлись"}
                ]
            }
        },
        "report_is_whole": {"machine_review_fields": {"incomplete": True}},
        "reviewed_by_someone_else": {"machine_review_fields": {"self_reviewed": True}},
        "checks_ran_on_the_submitted_commit": {"sha_check": "diverged"},
        "acceptance_criteria_are_green": {
            "ac_test_results": [
                ACTestResultView(ac_id="AC-1", status="fail", is_current=True)
            ]
        },
        "evidence_coverage_is_complete": {
            "evidence_coverage": EvidenceCoverage(state="partial", headline="1 of 7")
        },
    }
    assert set(spoilers) == set(SELF_APPROVAL_SIGNALS), (
        "каждый признак обязан иметь СВОЮ порчу: признак, добавленный в "
        "правило и забытый здесь, остался бы непроверенным"
    )

    for signal, spoiler in spoilers.items():
        decision = _decide(**spoiler)
        assert not decision.allowed, f"{signal}: набор не сошёлся, а одобрение выдано"
        assert [code for code, _ in decision.missing] == [signal], (
            f"{signal}: причина обязана назвать ИМЕННО этот признак, а названо "
            f"{[code for code, _ in decision.missing]} — иначе человек ищет "
            "причину заново"
        )
        assert decision.converged == tuple(
            s for s in SELF_APPROVAL_SIGNALS if s != signal
        ), f"{signal}: остальные семь обязаны остаться сошедшимися"
        assert decision.missing[0][1], f"{signal}: имя без детали нечем чинить"


async def test_absence_of_a_report_is_not_a_clean_report():
    """Ноль находок без отчёта — это «смотреть было некому», а не чистота.

    Самая дорогая подмена всего правила и причина, по которой каждый
    читаемый из отчёта признак роняется ОТДЕЛЬНОЙ строкой: 09.09 два отчёта
    из одиннадцати показывали ноль подтверждённых ровно потому, что один
    был оборван, а второй сдан автором кода.
    """
    decision = self_approval(
        _brief(machine_review=None), diff_paths=[], reviewer_reachable=True
    )

    assert not decision.allowed
    named = [code for code, _ in decision.missing]
    for signal in (
        "report_is_current",
        "no_confirmed_findings",
        "no_unresolved_findings",
        "report_is_whole",
        "reviewed_by_someone_else",
    ):
        assert signal in named, (
            f"{signal} читается из отчёта: без отчёта он обязан не сойтись, "
            "а не промолчать"
        )
    assert all("отчёт" in detail for code, detail in decision.missing if code in named)


async def test_a_report_silent_about_its_own_completeness_is_not_whole():
    """incomplete=None — «никогда не заявляли», и это не «полон» (#549).

    Отдельным тестом, потому что False и None различает одна ветка, и
    проверка на истинность съела бы её молча: отчёт, написанный до
    появления поля, объявил бы себя целым, ничего про это не сказав.
    """
    decision = _decide(machine_review_fields={"incomplete": None})

    assert not decision.allowed
    assert [code for code, _ in decision.missing] == ["report_is_whole"]
    assert "не заявлен" in decision.missing[0][1]


async def test_a_lost_dimension_is_not_a_whole_report():
    """Потерянное измерение — тот же неполный отчёт другими словами (#1198)."""
    decision = _decide(machine_review_fields={"lost_dimensions": ["security"]})

    assert not decision.allowed
    assert [code for code, _ in decision.missing] == ["report_is_whole"]
    assert "security" in decision.missing[0][1]


async def test_an_unrun_criterion_is_not_a_green_one():
    """Критерий без записанного прогона — непроверенный, а не пройденный.

    И результат прошлой сдачи — тоже не зелёный: он описывает код, которого
    на ветке уже нет. Три случая одного признака перечислены, а не показаны
    одним, потому что чинятся они по-разному.
    """
    no_result = _decide(ac_test_results=[])
    assert [code for code, _ in no_result.missing] == ["acceptance_criteria_are_green"]
    assert "не проверен" in no_result.missing[0][1]

    stale = _decide(
        ac_test_results=[
            ACTestResultView(ac_id="AC-1", status="pass", is_current=False)
        ]
    )
    assert [code for code, _ in stale.missing] == ["acceptance_criteria_are_green"]
    assert "прошлой сдачи" in stale.missing[0][1]

    untestable = _decide(acceptance_criteria=[], ac_test_results=[])
    assert [code for code, _ in untestable.missing] == ["acceptance_criteria_are_green"]


async def test_checks_must_have_run_on_the_commit_that_was_submitted():
    """Признак 6 перечисляет, ЧТО разошлось: предпас, CI и вершина ветки.

    Один признак и четыре разные причины: «зелёное было, но на другом
    коммите» и «зелёного не было вовсе» чинятся по-разному, и общий текст
    отказа заставил бы искать вслепую.
    """
    cases = {
        "предпас 'unknown'": _decide(
            prepass=PrepassState(state="unknown", reason="отчёта нет")
        ),
        "предпас снят на": _decide(
            prepass=PrepassState(state="covered", head_sha="c" * 40, passed=["lint"])
        ),
        "отчёт CI": _decide(
            ci_run_report=CIRunReportState(state="unknown", reason="прогона не было")
        ),
        "CI отчитался о": _decide(
            ci_run_report=CIRunReportState(state="current", head_sha="d" * 40)
        ),
        "sha_check": _decide(sha_check="unknown"),
        "не закрепила коммит": _decide(submission_sha=""),
    }
    for expected, decision in cases.items():
        assert not decision.allowed
        assert [code for code, _ in decision.missing] == [
            "checks_ran_on_the_submitted_commit"
        ], expected
        assert expected in decision.missing[0][1], (
            f"отказ обязан сказать, что именно разошлось: ждали {expected!r}, "
            f"получили {decision.missing[0][1]!r}"
        )


async def test_the_gate_never_signs_a_change_to_its_own_rules():
    """AC-3: запреты не снимаются никаким набором и названы ОТДЕЛЬНО.

    Набор во всех трёх случаях сошёлся полностью — именно это и проверяется:
    запрет обязан пережить полный комплект свидетельств. Настоящая такая
    задача берётся не выдуманным путём, а перечнем ladder-поверхностей,
    который читает и автовердикт (#1147): свой список рядом с общим означал
    бы, что новая поверхность появится в одном месте и не появится в другом.
    """
    # Задача, меняющая сам путь решения гейта, — по ФАКТИЧЕСКОМУ диффу.
    # Первой стоит НАСТОЯЩАЯ такая задача: файл, в котором живёт само это
    # правило. Его собственная сдача обязана уехать к человеку — иначе
    # правило умеет разрешать себе всё, что про себя перепишет.
    for surface in (
        "hub/services/steward_applied.py",
        "hub/services/auto_verdict.py",
        "hub/auth.py",
        "hub/services/lifecycle.py",
    ):
        gate = self_approval(_brief(), diff_paths=[surface], reviewer_reachable=True)
        assert not gate.allowed, surface
        assert [code for code, _ in gate.forbidden] == ["gate_decision_path"]
        assert gate.missing == (), (
            "признаки сошлись все до одного: запрет обязан стоять сам по себе, "
            "а не выглядеть как несобранное свидетельство"
        )
        assert gate.converged == SELF_APPROVAL_SIGNALS
        assert surface in gate.forbidden[0][1]

    # Нечитаемый дифф — не безопасный дифф.
    blind = self_approval(_brief(), diff_paths=None, reviewer_reachable=True)
    assert [code for code, _ in blind.forbidden] == ["gate_decision_path"]
    assert "прочитать не удалось" in blind.forbidden[0][1]

    # Постановка требует живой проверки, а её состояние unknown.
    manual_ac = AcceptanceCriterion(
        id="AC-9",
        given="раскатано",
        when="смотрят прод",
        then="ручка отвечает",
        verifiable_by=ACVerifiableBy.manual,
    )
    for live in (
        LiveCheckState(state="unknown", reason="никто не наблюдал"),
        # Коммит назван, и он ЧУЖОЙ. Флаг ``sha_mismatch`` здесь ни при чём:
        # бриф считает его против доставленного merge-коммита, которого на
        # ревью ещё нет, и до доставки он ложен всегда.
        LiveCheckState(state="done", sha="9" * 40, sha_mismatch=False),
    ):
        watched = self_approval(
            _brief(
                acceptance_criteria=[
                    _brief().acceptance_criteria[0],
                    manual_ac,
                ],
                live_check=live,
            ),
            diff_paths=["hub/services/x.py"],
            reviewer_reachable=True,
        )
        assert not watched.allowed
        assert [code for code, _ in watched.forbidden] == ["live_check_unknown"]
        assert watched.missing == ()

    # Проект, где хаб не умеет позвать ревьюера.
    unreachable = self_approval(
        _brief(), diff_paths=["hub/services/x.py"], reviewer_reachable=False
    )
    assert not unreachable.allowed
    assert [code for code, _ in unreachable.forbidden] == ["reviewer_unreachable"]
    assert unreachable.missing == ()

    assert {c for c, _ in blind.forbidden} | {
        "live_check_unknown",
        "reviewer_unreachable",
    } == set(SELF_APPROVAL_FORBIDS)


async def test_a_self_issued_approval_is_visible_and_sampled(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-4: одобрение без человека видно в карточке, в дайджесте и в выборке.

    Решение сюда приходит НЕ выдуманным: оно посчитано тем же правилом на
    сошедшемся наборе. Проверяются три читателя сразу, потому что тихое
    самостоятельное одобрение неотличимо от подлога.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    project_id = await _project(db, "self-approval-visible")
    task_id = await _client_task(db, project_id)
    await _judge(db, task_id, verdict="approve")
    decision = _decide()
    assert decision.allowed

    outcome, _ = await approve_without_a_human(db, task_id, 1, decision)

    assert outcome == APPLIED
    # 1. Карточка: строка называет и то, что человека не было, и все восемь
    #    признаков, на которых решение стоит.
    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    line = [c for c in updates if "Одобрено стюардом без человека" in c]
    assert line, "самостоятельное одобрение обязано назвать себя в карточке"
    for signal in SELF_APPROVAL_SIGNALS:
        assert signal in line[0], f"{signal} не назван в карточке"
    # 2. Тот же вердикт в поле, где лежит и человеческий, — но с актором,
    #    который называет, кто решил.
    events = await repo.list_events(
        db, since=0, kinds=["review_verdict_recorded"], limit=20
    )
    mine = [dict(e) for e in events if dict(e)["task_id"] == task_id]
    assert mine and mine[-1]["actor"] == "steward"
    # 3. Выборка на спот-чек: переиспользуется механика #1144, а не заводится
    #    вторая. Самостоятельное одобрение обязано попасть в oversample.
    from hub.services.digest import _audit_pool_and_oversample

    pool, oversample = _audit_pool_and_oversample(
        [], [], [], [{"task_id": task_id, "verdict": "approve"}]
    )
    assert task_id in pool and task_id in oversample, (
        "решение, которого человек не видел, обязано проверяться чаще среднего"
    )


async def test_a_decision_that_did_not_converge_never_applies(
    db: aiosqlite.Connection, monkeypatch
):
    """Несошедшийся набор к вердикту не приводит, и причина названа в карточке.

    Проверяется по ИСХОДУ задачи, а не только по возвращённому слову:
    применение, записавшее вердикт вопреки отказу, вернуло бы то же слово.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    project_id = await _project(db, "self-approval-refused")
    task_id = await _client_task(db, project_id)
    await _judge(db, task_id, verdict="approve")
    decision = _decide(machine_review_fields={"self_reviewed": True})
    assert not decision.allowed

    outcome, detail = await approve_without_a_human(db, task_id, 1, decision)

    assert outcome == ESCALATED_TO_HUMAN, detail
    task = dict(await repo.get_task(db, task_id))
    assert not (task.get("review_verdict") or ""), (
        "вердикта не появилось: несошедшийся набор решает человек"
    )
    assert task["status"] == "review", "задача ждёт человека там же, где ждала"
    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert any("reviewed_by_someone_else" in c for c in updates), (
        "причина обязана назвать признак: возврат без имени заставляет искать её заново"
    )


async def test_the_composer_reads_the_hub_own_facts(
    db: aiosqlite.Connection, monkeypatch
):
    """Сборщик входов берёт факты у хаба и на голой задаче не одобряет.

    Прогон настоящий, а не подставной: бриф и пакет собираются в окружении
    без клона репозитория, поэтому дифф честно не читается, а свидетельств
    нет вовсе — и правило обязано это увидеть само, а не получить готовым.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    project_id = await _project(db, "self-approval-composed")
    task_id = await _client_task(db, project_id)

    decision = await self_approval_for(db, task_id, 1)

    assert not decision.allowed
    named = [code for code, _ in decision.missing]
    assert "report_is_current" in named and "evidence_coverage_is_complete" in named
    assert "gate_decision_path" in [code for code, _ in decision.forbidden], (
        "дифф прочитать не удалось — это запрет, а не разрешение"
    )


async def test_a_signal_the_rule_forgot_to_count_is_not_a_converged_one():
    """Признак, который правило не посчитало вовсе, не попадает ни в один список.

    Мутация «читать только пустоту missing» пережила восемь порч подряд:
    каждая из них СЧИТАЛА свой признак и клала его в missing, поэтому
    пустота missing и полнота converged были неотличимы. Разойтись они
    могут ровно в одном случае — правило перестало считать признак совсем,
    — и это самая тихая из возможных поломок: одобрение выдаётся по
    неполному набору, не сказав об этом ни слова (#762).
    """
    forgotten = SelfApproval(converged=SELF_APPROVAL_SIGNALS[:-1])

    assert forgotten.missing == () and forgotten.forbidden == ()
    assert not forgotten.allowed, (
        "непосчитанный признак — не сошедшийся: пустота missing не есть "
        "полнота оснований"
    )
    assert SelfApproval(converged=SELF_APPROVAL_SIGNALS).allowed


# ---------------------------------------------------------------------------
# #1231, вторая сдача — находки внешнего ревью
# ---------------------------------------------------------------------------


async def test_a_live_check_on_another_commit_is_not_evidence_about_this_one(
    db: aiosqlite.Connection,
):
    """Живая проверка принимается ТОЛЬКО против сдаваемого коммита.

    Состояние живой проверки собирается настоящим ``live_check_state``, а не
    выдумывается: именно его на ревью вызывает ``build_review_brief``, и
    именно его ``sha_mismatch`` до доставки всегда ложен — сравнивать не с
    чем, merge-коммита ещё нет. Поэтому запрет, полагающийся на чужой флаг,
    пропускал наблюдение неопознанного кода.
    """
    from hub.services.review_evidence import live_check_state

    project_id = await _project(db, "live-check-foreign")
    task_id = await _client_task(db, project_id)
    manual_ac = AcceptanceCriterion(
        id="AC-9",
        given="раскатано",
        when="смотрят прод",
        then="ручка отвечает",
        verifiable_by=ACVerifiableBy.manual,
    )

    # Два случая, и ОТКАЗ У НИХ РАЗНЫЙ. Проверять только «не одобрено» здесь
    # мало: ветка про безымянный коммит и ветка про чужой перекрывают друг
    # друга по исходу — пустая строка не равна закреплённому sha, и вторая
    # поймала бы первую. Мутация «снять проверку на безымянный коммит»
    # пережила серию ровно поэтому. Чинится тем, чем и должно: отказ обязан
    # сказать, что именно не так, — «коммита не назвали» и «назвали чужой»
    # автор чинит по-разному.
    for recorded_sha, why, expected in (
        ("", "живая проверка не назвала коммит вовсе", "не назвала коммита"),
        ("f" * 40, "живая проверка снята на другом коммите", "снята на ffffffffffff"),
    ):
        await repo.insert_live_check(
            db,
            task_id=task_id,
            sha=recorded_sha,
            outcome="done",
            observation="ручка ответила",
        )
        raw = await live_check_state(
            db, task_id, delivered_sha=await repo.merge_sha_for_task(db, task_id)
        )
        assert raw["state"] == "done"
        assert not raw.get("sha_mismatch"), (
            "до доставки хабу не с чем сверять — на этот флаг опираться нельзя"
        )

        decision = self_approval(
            _brief(
                acceptance_criteria=[_brief().acceptance_criteria[0], manual_ac],
                live_check=LiveCheckState(**raw),
            ),
            diff_paths=["hub/services/x.py"],
            reviewer_reachable=True,
        )

        assert not decision.allowed, why
        assert [code for code, _ in decision.forbidden] == ["live_check_unknown"], why
        detail = decision.forbidden[0][1]
        assert _SHA_1231[:12] in detail, (
            "отказ обязан назвать коммит, о котором свидетельство обязано было говорить"
        )
        assert expected in detail, (
            f"{why}: отказ обязан назвать ИМЕННО этот случай, ждали {expected!r}, "
            f"получили {detail!r}"
        )


async def test_the_card_never_records_an_approval_the_judgement_did_not_give(
    db: aiosqlite.Connection, monkeypatch
):
    """Запись «одобрено без человека» появляется только при настоящем approve.

    Решение по свидетельствам и суждение стюарда — разные вопросы, и они
    расходятся: набор может сойтись, а стюард просить правок. Тогда
    ``apply_judgement`` возвращает работу автору, и строка про одобрение в
    карточке описывала бы исход, которого не было, — ложная запись в
    аудите, то есть ровно то, ради чего вся видимость и заводилась.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    project_id = await _project(db, "self-approval-vs-judgement")
    task_id = await _client_task(db, project_id)
    await _judge(db, task_id, verdict="changes_requested")
    decision = _decide()
    assert decision.allowed, "свидетельства сошлись — расходится именно суждение"

    outcome, detail = await approve_without_a_human(db, task_id, 1, decision)

    assert outcome == RETURNED, detail
    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert not [c for c in updates if "Одобрено стюардом без человека" in c], (
        "суждение просило правок — записи об одобрении в карточке быть не может"
    )
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", "работа вернулась автору, как и решил стюард"


# ---------------------------------------------------------------------------
# Боевой вход: правило вызывается не только из тестов (#1231, вторая сдача)
# ---------------------------------------------------------------------------


def _grant_act(monkeypatch) -> None:
    """Выдать режим act ТЕМ ЖЕ читателем, который его выдаёт в бою.

    ``effective_mode`` — единственное место, где слово ``act`` вообще может
    быть возвращено (#1107). Подменяется именно оно, потому что тест про
    вызов правила, а не про условия выдачи автономии: их проверяет #1107.
    """
    from hub.services import steward_shadow

    async def _act(_db):
        return "act"

    monkeypatch.setattr(steward_shadow, "effective_mode", _act)


async def _steward_lines(db: aiosqlite.Connection, task_id: int) -> list[str]:
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if "самостоятельн" in dict(u)["content"].lower()
        or "Одобрено стюардом без человека" in dict(u)["content"]
    ]


async def test_with_the_contour_off_a_recorded_judgement_changes_nothing(
    db: aiosqlite.Connection, monkeypatch
):
    """Режим не act — хаб ведёт себя ровно как до этой задачи.

    Замок читается настоящим ``effective_mode``, без подмен: ``STEWARD_MODE``
    по умолчанию ``off``, и это то состояние, в котором прод живёт сегодня.
    Проверяется не «не одобрено», а ОТСУТСТВИЕ следа вообще — ни вердикта, ни
    смены статуса, ни строки в карточке: выключенный контур обязан быть
    неотличим от несуществующего.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    monkeypatch.setattr(config, "STEWARD_MODE", "off")
    project_id = await _project(db, "live-path-off")
    task_id = await _client_task(db, project_id)
    before = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]

    await _judge(db, task_id, verdict="approve")

    task = dict(await repo.get_task(db, task_id))
    assert not (task.get("review_verdict") or ""), "вердикта при off не появляется"
    assert task["status"] == "review", "задача ждёт человека там же, где ждала"
    assert await _steward_lines(db, task_id) == [], (
        "выключенный контур не пишет в карточку ничего — ни одобрения, ни отказа"
    )
    after = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert after == before + ["Steward judgement recorded: verdict approve."], (
        "единственная новая строка — сама запись суждения, как и до #1231"
    )


async def test_a_project_that_did_not_delegate_the_verdict_gets_no_autonomy(
    db: aiosqlite.Connection, monkeypatch
):
    """Режим act есть, делегирования нет — правило не применяется.

    Автономия включается глобально, а делегируется ПОПРОЕКТНО (#743, #1151).
    Проект, не отдавший гейт стюарду, не получает самостоятельных одобрений
    оттого, что их получил соседний.
    """
    from hub.services.steward_applied import apply_self_approval

    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    _grant_act(monkeypatch)
    project_id = await _project(db, "live-path-not-delegated")
    await db.execute(
        "UPDATE projects SET gate_policy=? WHERE id=?",
        ('{"verdict": "human"}', project_id),
    )
    await db.commit()
    task_id = await _client_task(db, project_id)
    await _judge(db, task_id, verdict="approve")

    assert await apply_self_approval(db, task_id, 1) is None, (
        "гейт не делегирован — правило здесь не решает ничего"
    )
    assert await _steward_lines(db, task_id) == []


async def test_the_gatekeeper_is_asked_before_the_eight_signals(
    db: aiosqlite.Connection, monkeypatch
):
    """Право применять спрашивается раньше свидетельств и своим отказом.

    Восемь признаков не заменяют привратника (#1147, #1148): они ничего не
    говорят ни про класс риска, ни про громкие основания автовердикта. Отказ
    привратника обязан назвать СВОЙ код, а не выглядеть как несобранное
    свидетельство.
    """
    from hub.services.steward_applied import apply_self_approval

    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    _grant_act(monkeypatch)
    project_id = await _project(db, "live-path-gatekeeper")
    task_id = await _client_task(db, project_id)
    await _judge(db, task_id, verdict="approve")

    outcome, detail = await apply_self_approval(db, task_id, 1)

    assert outcome == ESCALATED_TO_HUMAN, detail
    assert "precondition_failed" in detail
    lines = await _steward_lines(db, task_id)
    assert lines and "Привратник применения возражает" in lines[0], (
        "отказ права применять читается иначе, чем несошедшееся свидетельство"
    )
    task = dict(await repo.get_task(db, task_id))
    assert not (task.get("review_verdict") or "")


async def test_a_recorded_approve_reaches_the_live_rule(
    db: aiosqlite.Connection, monkeypatch
):
    """Запись approve-суждения ДОХОДИТ до правила — не только из теста.

    Первая сдача #1231 оставила правило без вызывающих в ``hub/``, и
    собственный анализатор хаба (#601) сказал про оба входа ``only_tests``.
    Этот тест и есть тот вызывающий: проверяется не возвращённое значение
    правила, а СЛЕД его работы в карточке после настоящей записи суждения
    контрактом #1022.
    """
    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    _grant_act(monkeypatch)
    project_id = await _project(db, "live-path-reached")
    task_id = await _client_task(db, project_id)

    await _judge(db, task_id, verdict="approve")

    assert await _steward_lines(db, task_id), (
        "правило обязано быть вызвано записью суждения: механизм без "
        "вызывающего не меняет ни одного исхода"
    )


async def test_only_a_verdict_approve_reaches_the_rule(
    db: aiosqlite.Connection, monkeypatch
):
    """Правило спрашивают только про approve на вердикте, и ни про что ещё.

    ``changes_requested`` и ``escalate`` самостоятельного одобрения не
    порождают по определению, а драфт решает свой привратник (#1159, scope_out
    #1231). Спрашивать «сошлись ли свидетельства» там, где судья уже сказал
    «нет», значило бы завести второй ответ на решённый вопрос.
    """
    from hub.services import steward_applied

    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    _grant_act(monkeypatch)
    asked: list[tuple[int, int]] = []

    async def _spy(_db, task_id: int, generation: int):
        asked.append((task_id, generation))
        return None

    monkeypatch.setattr(steward_applied, "apply_self_approval", _spy)
    project_id = await _project(db, "live-path-only-approve")

    refused = await _client_task(db, project_id)
    await _judge(db, refused, verdict="changes_requested")
    assert asked == [], "правку судья уже запросил — правило здесь не спрашивают"

    escalated = await _client_task(db, project_id)
    await _judge(db, escalated, verdict="escalate")
    assert asked == [], "эскалация и есть отказ судить — применять нечего"

    approved = await _client_task(db, project_id)
    await _judge(db, approved, verdict="approve")
    assert asked == [(approved, 1)], "approve обязан дойти до правила"


async def test_a_converged_submission_applies_without_a_human_on_the_live_path(
    db: aiosqlite.Connection, monkeypatch
):
    """Все три замка открыты и свидетельства сошлись — вердикт уезжает сам.

    Свидетельства подставляются готовым решением, а НЕ выдуманным вердиктом:
    что именно считается сошедшимся набором, проверяют тесты AC-1..AC-3 выше,
    а здесь проверяется, что открытый контур доводит их ответ до настоящей
    записи вердикта — того же поля, в которое пишет человек.
    """
    from hub.services import steward_apply, steward_applied

    monkeypatch.setattr(config, "MAX_REVIEW_CYCLES", 3)
    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    _grant_act(monkeypatch)

    async def _no_refusals(_db, _task_id, _generation=None):
        return []

    async def _converged(_db, _task_id, _generation):
        return _decide()

    monkeypatch.setattr(steward_apply, "apply_refusals", _no_refusals)
    monkeypatch.setattr(steward_applied, "self_approval_for", _converged)
    project_id = await _project(db, "live-path-applies")
    task_id = await _client_task(db, project_id)

    await _judge(db, task_id, verdict="approve")

    task = dict(await repo.get_task(db, task_id))
    assert (task.get("review_verdict") or "") == ReviewVerdict.approved.value, (
        "сошедшаяся сдача уезжает без человеческого вердикта — в этом вся задача"
    )
    lines = await _steward_lines(db, task_id)
    assert lines and "Одобрено стюардом без человека" in lines[0]


async def test_an_unverified_deployment_is_not_a_live_check(db: aiosqlite.Connection):
    """Совпавший sha не отвечает на вопрос «а доехал ли этот коммит до прода».

    ``record_live_check`` СОЗНАТЕЛЬНО принимает наблюдение с
    ``deploy_state='unknown'`` (#837): установка без фактов о доставке ничего
    не знает про прод, и отказ там превратил бы незнание в гейт. Но принятая
    запись — не подтверждённая: хаб прямо сказал, что не смог убедиться, что
    наблюдали именно выкаченный код.

    Sha здесь не спасает, и это главное в находке: его присылает ТОТ ЖЕ
    вызывающий, что и наблюдение, а вопрос не «тот ли коммит назвали», а
    «доехал ли он». Проверяются оба ответа сразу — неподтверждённая доставка
    запрещает, подтверждённая пропускает, — иначе сторож, отказывающий
    всегда, выглядел бы работающим.
    """
    from hub.services.review_evidence import live_check_state

    project_id = await _project(db, "live-check-deploy")
    manual_ac = AcceptanceCriterion(
        id="AC-9",
        given="раскатано",
        when="смотрят прод",
        then="ручка отвечает",
        verifiable_by=ACVerifiableBy.manual,
    )

    async def _decide_with(deploy_state: str) -> SelfApproval:
        task_id = await _client_task(db, project_id)
        await repo.insert_live_check(
            db,
            task_id=task_id,
            sha=_SHA_1231,
            outcome="done",
            probe="curl /healthz",
            observation="200 OK",
            deploy_state=deploy_state,
        )
        raw = await live_check_state(
            db, task_id, delivered_sha=await repo.merge_sha_for_task(db, task_id)
        )
        assert raw["state"] == "done" and raw["sha"] == _SHA_1231, (
            "запись про ТОТ САМЫЙ коммит: расхождение sha здесь ни при чём"
        )
        return self_approval(
            _brief(
                acceptance_criteria=[_brief().acceptance_criteria[0], manual_ac],
                live_check=LiveCheckState(**raw),
            ),
            diff_paths=["hub/services/x.py"],
            reviewer_reachable=True,
        )

    unverified = await _decide_with("unknown")
    assert not unverified.allowed, (
        "хаб не подтвердил выкат — наблюдение не говорит о раскатанном коде"
    )
    assert [code for code, _ in unverified.forbidden] == ["live_check_unknown"]
    assert "выкат" in unverified.forbidden[0][1], (
        "отказ обязан назвать ИМЕННО доставку, а не коммит: ветки про sha и "
        "ветка про выкат чинятся по-разному"
    )
    assert unverified.missing == (), "признаки сошлись все — это запрет, а не пробел"

    # Запись, которую хаб сверил с выкатом, проходит. Без этой половины
    # сторож, отказывающий всегда, был бы неотличим от работающего.
    verified = await _decide_with("in_prod")
    assert verified.allowed, verified.reason
