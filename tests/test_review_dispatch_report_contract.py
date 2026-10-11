"""Hub-dispatched cross-model reviews (#757): report_contract (split of the former single file, #1670)."""

from __future__ import annotations

import json
import re

import aiosqlite
from httpx import AsyncClient

from hub import config, services
from hub import repository as repo
from hub.integrations import cursor_cloud, local_reviewer
from hub.integrations.registry import plugins
from hub.models import TaskSubmitReview
from hub.services.review_dispatch import (
    DEEP,
    sweep_review_dispatches,
)
from tests.review_dispatch_support import (
    _HARMLESS_DIFF,
    _TIP,
    _agent_key,
    _any_dispatch_row,
    _dispatch_row,
    _DispatchRecorder,
    _local_principal,
    _pinned_setup,
    _PinnedGitOps,
    _report_on_current,
    _reporting_stub,
    _stub_reviewer,
    _submitted,
    _wire,
)


# --- Отчёт по контракту без MCP (#1084) --------------------------------------
#
# У облачного агента Cursor нет инструментов MCP — заголовок с токеном до рана
# не доезжает, и отчёт приходится вытаскивать из текста прогона. Сеть при этом
# работает: ран получил от хаба 401, то есть дошёл и упёрся в отсутствие
# личности. Значит личность надо доставить иначе — одноразовым кодом в промпте,
# который ран меняет на короткую сессию с двумя маршрутами.


_CODE_IN_PROMPT = re.compile(r"AH-[0-9A-HJKMNP-TV-Z]{8}")


def _auth_on(monkeypatch) -> None:
    """Обмен кода — публичный маршрут, но в open mode он отвечает 503: без
    принципалов нет и личности, которую код мог бы нести. _pinned_setup
    закрывает open mode только для middleware, а сам маршрут смотрит на
    config.HUB_TOKENS."""
    from hub.config import TokenIdentity

    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(
        config, "HUB_TOKENS", {"probe-token": TokenIdentity("probe", "agent")}
    )


async def test_dispatch_mints_a_reviewer_code_and_never_the_token(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#1084): диспатч чеканит код, привязанный к ЭТОЙ задаче, и кладёт
    # его в промпт. Долгоживущего токена там нет никогда, и кода нет в ленте.
    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-code"}, "run": {"id": "run-code"}}
    )
    _wire(monkeypatch, recorder)
    _, reviewer_token, _ = await _pinned_setup(db, monkeypatch)
    _auth_on(monkeypatch)
    task_id = await _submitted(client, db, "spike-code", policy={"review": "dispatch"})

    prompt = recorder.calls[-1]["prompt_text"]
    assert reviewer_token not in prompt, "долгоживущий токен в промпт не попадает"

    found = _CODE_IN_PROMPT.search(prompt)
    assert found, "в промпте нет одноразового кода"
    code = found.group(0)

    # Код проверяется ОБМЕНОМ, а не совпадением строки: строка в промпте может
    # быть похожа на код и не быть им.
    redeemed = await client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": code},
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    assert redeemed.status_code == 200, redeemed.text
    body = redeemed.json()
    assert body["kind"] == "reviewer"
    assert body["bound_task_id"] == task_id

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert all(code not in (u["content"] or "") for u in updates), "код утёк в ленту"


async def test_contract_report_wins_and_text_recovery_remains(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#1084): отчёт, сданный сессией кода, записан как контрактный —
    # происхождение mcp, не run_text, — привязан к текущему поколению и
    # сходится с пином принципала своего диспатча. Восстановление из текста
    # при этом остаётся: оно проверяется соседними тестами файла и не должно
    # перезаписывать уже принятый контрактный отчёт.
    recorder = _DispatchRecorder({"agent": {"id": "bc-ctr"}, "run": {"id": "run-ctr"}})
    _wire(monkeypatch, recorder)
    expected_pid, _, _ = await _pinned_setup(db, monkeypatch)
    _auth_on(monkeypatch)
    task_id = await _submitted(
        client, db, "spike-contract", policy={"review": "dispatch"}
    )

    code = _CODE_IN_PROMPT.search(recorder.calls[-1]["prompt_text"]).group(0)
    session = (
        await client.post(
            "/api/auth/chat-pair/redeem",
            json={"code": code},
            headers={"x-forwarded-for": "203.0.113.10"},
        )
    ).json()
    headers = {"Authorization": f"Bearer {session['token']}"}

    brief = await client.get(f"/api/tasks/{task_id}/review-brief", headers=headers)
    assert brief.status_code == 200, brief.text

    filed = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "raw_count": 2,
            "incomplete": False,
            "harness_skill": "multi-agent-review",
            "findings_confirmed": [],
            "findings_rejected": [],
        },
        headers=headers,
    )
    assert filed.status_code == 200, filed.text

    row = dict(await repo.get_latest_machine_review(db, task_id))
    # Происхождение живёт в orchestrator: восстановленный из текста отчёт
    # несёт префикс "cursor-cloud-result:", сданный по контракту — нет.
    assert not str(row["orchestrator"]).startswith("cursor-cloud-result"), (
        "контрактный отчёт не должен помечаться как восстановленный из текста"
    )
    assert row["submission_generation"] == 1
    assert not row["self_reviewed"], "ревьюер — не исполнитель задачи"
    dispatch = await _any_dispatch_row(db, task_id)
    assert dispatch["reviewer_principal_id"] == expected_pid == row["principal_id"]


async def test_prompt_names_the_path_the_run_can_actually_take(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # #1084: промпт не должен звать в MCP, которого у рана нет. Пока код
    # выписан, основной путь — HTTP, и обзор ревью тоже читается по HTTP:
    # иначе за ним пойдут в несуществующий инструмент, а код живёт минуты.
    recorder = _DispatchRecorder({"agent": {"id": "bc-p1"}, "run": {"id": "run-p1"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    _auth_on(monkeypatch)
    await _submitted(client, db, "spike-prompt-http", policy={"review": "dispatch"})

    with_code = recorder.calls[-1]["prompt_text"]
    assert _CODE_IN_PROMPT.search(with_code), "код в промпте — предпосылка теста"
    assert "/review-brief" in with_code, "обзор ревью тоже по HTTP"
    assert "Основной путь — HTTP" in with_code
    assert "сдай hub_submit_machine_review, это основной путь" not in with_code
    assert "haiplane-review" in with_code, "запасной блок остаётся при любом пути"

    # Без кода (нет ревьюер-принципала) формулировка прежняя: правка условная,
    # а не вычёркивание MCP отовсюду — Cursor может починить доставку.
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", "no-such-token")
    await _submitted(client, db, "spike-prompt-mcp", policy={"review": "dispatch"})
    without_code = recorder.calls[-1]["prompt_text"]
    assert not _CODE_IN_PROMPT.search(without_code)
    assert "сдай hub_submit_machine_review, это основной путь" in without_code


async def test_dispatch_pins_the_generation_it_minted_for(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # #1084, находка ревью: пин проверялся ТОЛЬКО на кодах, вычеканенных
    # руками с bound_generation. Диспатч его не передавал, поэтому в проде
    # колонка была NULL, сравнение в обмене пропускалось, сессия несла None,
    # и проверка на приёме была ложной — вся защита оказывалась мёртвой при
    # зелёных тестах. Этот тест идёт ЧЕРЕЗ ДИСПАТЧ и потому её сторожит.
    recorder = _DispatchRecorder({"agent": {"id": "bc-gen"}, "run": {"id": "run-gen"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    _auth_on(monkeypatch)
    task_id = await _submitted(
        client, db, "spike-gen-pin", policy={"review": "dispatch"}
    )

    code = _CODE_IN_PROMPT.search(recorder.calls[-1]["prompt_text"]).group(0)
    rows = await db.execute_fetchall(
        "SELECT bound_generation FROM chat_pair_codes WHERE kind='reviewer'"
    )
    assert [dict(r)["bound_generation"] for r in rows] == [1], "диспатч обязан пинить"

    # Работа пересдана, пока прогон жив: статус остаётся review, меняется сдача.
    await db.execute("UPDATE tasks SET submission_generation=2 WHERE id=?", (task_id,))
    await db.commit()

    refused = await client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": code},
        headers={"x-forwarded-for": "203.0.113.11"},
    )
    assert refused.status_code == 401, refused.text


async def test_cloud_review_never_calls_cursor_for_a_gitverse_project(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1119): отказ проверяется ПОВЕДЕНИЕМ, а не формой константы.

    Первая редакция этого критерия сверяла ``CLOUD_REVIEW_FORGES == ("github",)``
    — то есть читала объявление вслух. Такой тест переживает удаление самой
    проверки из ``maybe_dispatch_review`` и остаётся зелёным, а платит за это
    оплаченный прогон облачного агента в пустоту (найдено ревью, отчёт #201).

    Здесь Cursor замокан и СЧИТАЕТ вызовы: единственное доказательство, что до
    него не дошли, — пустой список.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-gv"}, "run": {"id": "r-gv"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(
        client,
        db,
        "spike-gitverse",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )

    assert recorder.calls == [], (
        "облачный ревьюер не должен быть вызван для GitVerse-проекта: "
        "измерено 31.08.2026, что тот же запрос с адресом GitVerse даёт 400 и "
        "500, и ни один ответ не называет причину"
    )
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "review", "отказ ревьюера не должен ломать сдачу"

    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert any("gitverse" in a for a in alerts), (
        "карточка обязана назвать ФОРЖ как причину: молчаливый пропуск "
        "неотличим от «ревьюер не настроен» (#498)"
    )


async def test_the_same_submission_on_github_does_reach_cursor(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Контроль к предыдущему: тест обязан уметь падать.

    Без этой пары «вызовов ноль» доказывало бы что угодно — например, что
    диспатч не сработал по совсем другой причине и форж тут ни при чём.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-gh"}, "run": {"id": "r-gh"}})
    _wire(monkeypatch, recorder)

    await _submitted(
        client,
        db,
        "spike-github-control",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="github",
    )

    assert len(recorder.calls) == 1, (
        "при том же наборе условий и форже github вызов обязан состояться — "
        "иначе предыдущий тест зелен по посторонней причине"
    )


async def test_same_sha_buys_no_second_review(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1152): код уже прочитан — второй прогон не заказывается.

    Измерено на живой базе прода: 8 дублей из 87 прогонов, порядка 15M
    токенов провайдера. Пересдача на неизменившемся sha — ровно тот путь,
    которым они возникали: генерация новая, дифф прежний.

    Проверяется ОТСУТСТВИЕ строки в review_dispatches, а не отсутствие
    отчёта: заказ и есть оплата.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-same-sha")
    assert len(recorder.calls) == 1, "первая сдача ревью получает"
    review_id = await _report_on_current(db, task_id)
    await db.commit()
    # #1399: после #1265 пересдача из review поколение не поднимает, и отчёт
    # оказывался на ТОЙ ЖЕ генерации — граница «<=» в страже переставала
    # проверяться («=» оставался зелёным). Отчёт — на прошлом поколении.
    await _back_to_running(db, task_id)

    # Пересдача НА ТОМ ЖЕ коммите: поколение растёт, вершина та же.
    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )

    assert len(recorder.calls) == 1, (
        "второй прогон на том же sha не заказывается — он вернул бы то же "
        "чтение за те же деньги"
    )
    rows = await db.execute_fetchall(
        "SELECT id FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    assert len(rows) == 1, "заказа не появляется: заказ и есть оплата"
    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert any(f"отчёт #{review_id}" in c for c in updates), (
        "отказ обязан назвать отчёт, который уже покрывает этот код: "
        "молчаливый отказ неотличим от сломавшегося диспетчера"
    )


async def _back_to_running(db: aiosqlite.Connection, task_id: int) -> None:
    """Вернуть сдачу исполнителю, чтобы пересдача того же sha открыла НОВОЕ
    поколение (#1399).

    Эти тесты написаны до #1265, когда любая пересдача поднимала поколение:
    страж новизны ключуется по коду именно затем, чтобы видеть «поколения
    разные, sha тот же». Пересдача из review теперь поколение не поднимает,
    и второй заказ того же профиля на ТУ ЖЕ сдачу — дубль, который #1399
    закрывает. Вопрос тестов — про новое поколение, его и воспроизводим.
    """
    await db.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
    await db.commit()


async def test_a_real_resubmission_still_gets_reviewed(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1152): новый код ревью ПОЛУЧАЕТ, и тот же код без отчёта тоже.

    Ложный отказ здесь дороже лишнего прогона: сдача, оставшаяся без
    отчёта, уйдёт к человеку вслепую или встанет вовсе. Поэтому зеркало
    двустороннее — сдвинулась вершина, и отдельно случай «тот же sha, но
    отчёта по нему нет».
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)

    # Тот же sha, но отчёта нет вовсе — сравнивать не с чем, прогон нужен.
    fresh = await _submitted(client, db, "spike-no-report")
    assert len(recorder.calls) == 1
    await _back_to_running(db, fresh)
    await services.submit_for_review(
        db, fresh, TaskSubmitReview(model="claude-fable-5")
    )
    assert len(recorder.calls) == 2, (
        "без отчёта по этому sha отказывать не за что: незнание не есть совпадение"
    )

    # Отчёт есть, но вершина сдвинулась — это новая работа.
    moved = await _submitted(client, db, "spike-moved")
    assert len(recorder.calls) == 3
    await _report_on_current(db, moved)
    await db.commit()
    # #1361: новая вершина с НОВОЙ авторской строкой. Двойник, отдающий один
    # дифф на любую вершину, здесь больше не годится: такая пересдача теперь
    # честно читается как «слита только база».
    plugins.git_ops = _ShaDiffGitOps(
        "b" * 40,
        {_TIP: _HARMLESS_DIFF, "b" * 40: _HARMLESS_DIFF + "+новая строка\n"},
    )
    await services.submit_for_review(
        db, moved, TaskSubmitReview(model="claude-fable-5")
    )
    assert len(recorder.calls) == 4, "новый sha — новая работа, ревью заказывается"


# ---------------------------------------------------------------------------
# #1361 — пересдача «только слита база» не покупает второго ревью
# ---------------------------------------------------------------------------


class _ShaDiffGitOps(_PinnedGitOps):
    """Дифф ветки к базе — свой у каждой вершины; имя ветки — текущая вершина."""

    def __init__(self, tip: str, diffs: dict[str, str]) -> None:
        super().__init__(tip, ["docs/notes.md"], diffs[tip])
        self._diffs = diffs

    async def branch_diff(self, repo, base, branch, context=0):
        return self._diffs.get(branch, self._diffs[self._tip])


# Та же правка автора, что и в _MERGE_OLD, после слияния базы в тот же файл:
# другие строка index и смещение ханка, строки +/- те же и в том же порядке.
_MERGE_OLD = (
    "diff --git a/docs/notes.md b/docs/notes.md\n"
    "index 1111111..2222222 100644\n"
    "--- a/docs/notes.md\n"
    "+++ b/docs/notes.md\n"
    "@@ -3,0 +4,2 @@ intro\n"
    "+первая авторская строка\n"
    "+вторая авторская строка\n"
)
_MERGE_NEW = _MERGE_OLD.replace("1111111..2222222", "3333333..4444444").replace(
    "@@ -3,0 +4,2 @@", "@@ -5,0 +6,2 @@"
)


async def test_a_merge_only_resubmission_carries_the_report_over(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1361): слита только база — отчёт переносится, провайдер не зван.

    Измерено 23.09.2026: #1333 сдача 3 — только слияние develop, заказано
    deep-ревью, ~4,7 млн токенов, отчёт чистый. Здесь вершина новая, дифф к
    базе побайтно другой, а правка автора та же — и ревью не покупается: в
    новом поколении лежит перенесённый отчёт с пометкой, откуда он.

    Обратная сторона в том же тесте: новая авторская строка — ревью
    заказывается как раньше.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-merge-only", diff=_MERGE_OLD)
    assert len(recorder.calls) == 1, "первая сдача ревью получает"
    review_id = await _report_on_current(db, task_id)
    await db.commit()

    plugins.git_ops = _ShaDiffGitOps("b" * 40, {_TIP: _MERGE_OLD, "b" * 40: _MERGE_NEW})
    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )

    assert len(recorder.calls) == 1, "слияние базы не покупает второго ревью"
    rows = await db.execute_fetchall(
        "SELECT id FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    assert len(rows) == 1, "заказа не появляется: заказ и есть оплата"
    task = dict(await repo.get_task(db, task_id))
    carried = [
        dict(r)
        for r in await repo.machine_reviews_of_generation(
            db, task_id, int(task["submission_generation"])
        )
    ]
    assert len(carried) == 1, "в новом поколении лежит перенесённый отчёт"
    assert carried[0]["carried_from_review_id"] == review_id, (
        "перенесённый отчёт помечен как перенесённый и называет источник"
    )
    assert not carried[0]["provider_tokens"] and not carried[0]["tokens_spent"], (
        "перенос не выдаётся за новое чтение: денег он не стоил"
    )
    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert any("перенесён с поколения 1" in c for c in updates), updates

    # Новая авторская строка — это новая работа, ревью заказывается.
    plugins.git_ops = _ShaDiffGitOps(
        "d" * 40,
        {
            _TIP: _MERGE_OLD,
            "b" * 40: _MERGE_NEW,
            "d" * 40: _MERGE_NEW + "+строка, которой ревью не видело\n",
        },
    )
    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )
    assert len(recorder.calls) == 2, "новая авторская строка покупает ревью"


async def test_a_carried_report_is_not_a_new_read_in_the_metrics(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Перенос не считается новым чтением в метриках практики (#1361).

    Чек-лист постановки: перенесённый отчёт помечен и в числе прочитанных
    отчётов не появляется — иначе экономия выглядела бы как лишний прогон.
    """
    from hub.services.orchestration import practice_metrics

    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-merge-metrics", diff=_MERGE_OLD)
    await _report_on_current(db, task_id)
    await db.commit()
    before = (await practice_metrics(db))["machine_reviews"]

    plugins.git_ops = _ShaDiffGitOps("b" * 40, {_TIP: _MERGE_OLD, "b" * 40: _MERGE_NEW})
    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )
    after = (await practice_metrics(db))["machine_reviews"]

    assert after["reports_total"] == before["reports_total"]
    assert after["reviews"] == before["reviews"]
    assert after["carried_over"] == before["carried_over"] + 1, (
        "перенос виден отдельным счётом, а не прячется и не выдаётся за чтение"
    )


async def test_a_carried_report_stands_for_the_current_generation(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Перенесённый отчёт — отчёт ТЕКУЩЕГО поколения, с суждениями источника.

    Без этого перенос кончался бы тупиком: гейт сказал бы «machine-review
    устарел», а очередь находок снова показала бы человеку то, что он уже
    разметил на исходном отчёте.
    """
    from hub.services.orchestration import machine_review_gap

    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-merge-queue", diff=_MERGE_OLD)
    source = await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        harness_skill="lite-diff-review",
        model="grok-4.6",
        raw_count=1,
        findings_confirmed=json.dumps(
            [{"title": "race on retry", "severity": "high", "file": "hub/a.py"}]
        ),
        submitted_by="cursor-cloud-reviewer",
    )
    await repo.upsert_finding_disposition(
        db,
        review_id=source,
        task_id=task_id,
        submission_generation=1,
        finding_index=0,
        finding_title="race on retry",
        disposition="wont_fix",
        note="принято осознанно",
        decided_by="owner",
        finding_uid="",
    )
    await db.commit()
    assert (await repo.count_unjudged_findings(db))["findings"] == 0

    plugins.git_ops = _ShaDiffGitOps("b" * 40, {_TIP: _MERGE_OLD, "b" * 40: _MERGE_NEW})
    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )
    task = dict(await repo.get_task(db, task_id))
    current = await repo.machine_reviews_of_generation(
        db, task_id, int(task["submission_generation"])
    )
    assert [dict(r)["carried_from_review_id"] for r in current] == [source]
    assert "race on retry" in dict(current[0])["findings_confirmed"], (
        "находки переносятся вместе с отчётом, а не теряются"
    )
    assert await machine_review_gap(db, task) is None, (
        "перенесённый отчёт закрывает требование отчёта текущего поколения"
    )
    assert (await repo.count_unjudged_findings(db))["findings"] == 0, (
        "суждение по исходному отчёту отвечает и за перенесённый"
    )


async def test_a_submission_without_a_pinned_sha_is_not_a_match(
    db: aiosqlite.Connection,
):
    """Незакреплённый sha — «сравнивать нечего», а не «совпало».

    Ветка иначе не исполняется ни одним тестом: живая сдача всегда
    закрепляет вершину. Но незнание не есть совпадение (#762), и цена
    ошибки здесь несимметрична — ложный отказ отнимает у сдачи отчёт, а
    лишний прогон стоит только денег.
    """
    from hub.services.review_dispatch import _report_already_covers_this_sha

    assert (
        await _report_already_covers_this_sha(
            db, {"id": 1, "submission_generation": 2, "submission_sha": ""}
        )
        is None
    )
    assert (
        await _report_already_covers_this_sha(
            db, {"id": 1, "submission_generation": 2, "submission_sha": "   "}
        )
        is None
    )


async def test_an_incomplete_report_does_not_lock_the_sha(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Неполный отчёт не есть прочитанный код (#879).

    Найдено кросс-модельным ревью первой сдачи, и находка бьёт в саму
    задачу: правило, написанное ради экономии прогонов, запирало добор
    непрочитанного. Отчёт с incomplete=true САМ говорит, что дочитал не
    всё, — назвать его чтением значит поверить утверждению, которое он о
    себе опровергает.

    Направление ошибки то же, что и во всём правиле: лишний прогон стоит
    денег, пропущенный — сдачи без ревью.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-incomplete-sha")
    assert len(recorder.calls) == 1
    await _report_on_current(db, task_id, incomplete=True)
    await db.commit()
    await _back_to_running(db, task_id)

    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )

    assert len(recorder.calls) == 2, (
        "отчёт, объявивший себя неполным, покрытием не является — иначе "
        "правило запирает лестницу добора #879"
    )


async def test_a_self_report_does_not_cancel_the_independent_reviewer(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Отчёт исполнителя о себе не отменяет ревьюера со стороны.

    Найдено кросс-модельным ревью. Тот же автор уже однажды закрыл чужой
    диспетчер как выполненный своим параллельным отчётом (#1011, #1025) —
    здесь он закрывал бы его ещё до старта, и «код прочитан» означало бы
    «автор прочитал свой код». Независимость — весь смысл кросс-модельного
    контура, и правило экономии не должно её покупать.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-self-report")
    assert len(recorder.calls) == 1
    await _report_on_current(db, task_id, self_reviewed=True)
    await db.commit()
    await _back_to_running(db, task_id)

    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )

    assert len(recorder.calls) == 2, (
        "самоотчёт не есть независимое чтение — он не может отменить "
        "кросс-модельного ревьюера"
    )


async def test_the_refusal_is_as_loud_as_the_other_dispatcher_refusals(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Отказ пишется алертом, как остальные отказы диспетчера.

    Найдено кросс-модельным ревью: я написал его как status, то есть тише
    соседей. Отказ, который тише остальных, читается как «ничего не
    произошло» ровно там, где ревьюера не позвали, — а не позвать
    ревьюера это событие, а не отсутствие события.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-refusal-loud")
    review_id = await _report_on_current(db, task_id)
    await db.commit()

    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )

    kinds = {
        dict(u)["kind"]
        for u in await repo.get_task_updates(db, task_id)
        if f"отчёт #{review_id}" in dict(u)["content"]
    }
    assert kinds == {"alert"}, f"отказ обязан быть слышен как алерт: {kinds}"


async def test_the_top_up_is_not_stopped_by_the_same_sha_guard(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Добор лестницы #879 проверку новизны не проходит и не должен.

    Найдено кросс-модельным ревью, и это ВТОРОЙ раз, когда правило экономии
    мешало добору — первый был про неполный отчёт, этот про принудительный
    профиль. Один и тот же узел с двух сторон.

    Причина в том, что вопросы разные. Страж спрашивает «читали ли уже этот
    код», и на новую сдачу это верный вопрос. Добор спрашивает «дочитал ли
    НАШ прогон», и отчёт чужой генерации на том же sha про это не знает
    ничего.

    Проверяется РАЗНИЦА ИСХОДОВ ИЗ ОДНОГО СОСТОЯНИЯ: при совпавшем sha
    обычный заказ отказывает, а добор проходит. Тест зовёт точку входа
    напрямую, а не через естественную последовательность: чтобы страж
    встретил добор, полный отчёт чужой генерации должен появиться МЕЖДУ
    прогоном этой генерации и его добором, и такую гонку я собрать не смог.
    Правило от этого не зависит — оно про то, какой вопрос кому задан.
    """
    from hub.services.review_dispatch import DEEP, maybe_dispatch_review

    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-topup-guard")
    assert len(recorder.calls) == 1
    await _report_on_current(db, task_id)
    await db.commit()

    # Пересдача НА ТОМ ЖЕ коммите: страж срабатывает, заказа нет.
    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )
    assert len(recorder.calls) == 1, "обычный заказ на прочитанном коде отказывает"

    # То же состояние, тот же sha — но это добор.
    dispatched = await maybe_dispatch_review(db, task_id, force_profile=DEEP)

    assert dispatched, (
        "добор обязан пройти там, где обычный заказ отказал: у него другой "
        "вопрос и свой потолок (REVIEW_LADDER_MAX_STEPS)"
    )
    assert len(recorder.calls) == 2
    assert (await _dispatch_row(db, task_id))["profile"] == DEEP


async def test_the_top_up_still_asks_the_policy(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Выключенный контур выключен и для лестницы.

    Мутация, выжившая при первом заходе: я написал в докстринге, что добор
    спрашивает политику, и не проверил этого. Освобождение от проверки
    новизны легко расползается на соседний отказ, стоящий в той же
    функции, — и тогда лестница покупала бы прогоны на проекте, который
    ревью вообще не заказывает.

    Разница между двумя отказами именно в том, на какой вопрос они
    отвечают: «дочитал ли наш прогон» у добора свой, а «просят ли здесь
    ревью» — общий для всех, и лестница из него не выведена.
    """
    from hub.services.review_dispatch import DEEP, maybe_dispatch_review

    recorder = _DispatchRecorder({"agent": {"id": "bc-off"}, "run": {"id": "r-off"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-topup-off", policy={"review": "off"})
    assert recorder.calls == [], "контур выключен — обычного заказа нет"

    dispatched = await maybe_dispatch_review(db, task_id, force_profile=DEEP)

    assert not dispatched, (
        "добор не обходит выключённый контур: освобождение касается только "
        "проверки новизны"
    )
    assert recorder.calls == []


async def test_local_review_lands_an_independent_report(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-1 (#1180): на форже без облака отчёт всё равно появляется, и он чужой.

    Проверяется СТРОКОЙ В БАЗЕ, а не намерением запуска: отчёт текущего
    поколения, ``self_reviewed=0``, непустые agent_count и tokens_spent. Это и
    есть то, чего на GitVerse не бывало вовсе.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-nope"}, "run": {"id": "r-nope"}})
    _wire(monkeypatch, recorder)
    reviewer_pid = await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-local-report",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()
    await db.commit()

    assert recorder.calls == [], "облако на этом форже не зовут (#1119)"
    reports = [
        dict(r) for r in await repo.machine_reviews_of_generation(db, task_id, 1)
    ]
    assert len(reports) == 1, "прогон обязан оставить ровно один отчёт"
    report = reports[0]
    assert report["principal_id"] == reviewer_pid, (
        "отчёт принадлежит принципалу ревьюера — независимость держит токен, "
        "а не машина (#728)"
    )
    assert not report["self_reviewed"], (
        "ровно это и не получалось на #1128: отчёт автора гейт не засчитывает "
        "при REVIEW_SELF_APPROVE=forbid"
    )
    assert report["agent_count"] == 4 and report["tokens_spent"] == 1_500_000, (
        "пустые метрики — признак отчёта без исполнения (харнесс v8)"
    )
    dispatch = dict(
        (
            await db.execute_fetchall(
                "SELECT * FROM review_dispatches WHERE task_id=?", (task_id,)
            )
        )[-1]
    )
    assert dispatch["channel"] == "local" and dispatch["status"] == "done"


async def test_the_local_reviewer_gets_neither_hub_secrets_nor_the_clone(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-2, машинная часть: процесс не получает ни секретов, ни клона.

    Проверяется ПОПЫТКОЙ, а не чтением конфига: секрет кладётся в окружение
    хаба по-настоящему, заглушка честно печатает про себя всё, что видит, и
    утверждение делается по её собственному свидетельству. Настройка, о
    которой только заявлено, защитой не является.

    Ручная половина AC-2 — попытка прочитать secrets.env и записать в рабочий
    клон ПОД ПОЛЬЗОВАТЕЛЕМ ревьюера — этим тестом не закрывается и за
    закрытую не выдаётся: на машине разработчика второго пользователя нет.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    probe = tmp_path / "probe.json"
    recorder = _DispatchRecorder({"agent": {"id": "bc-x"}, "run": {"id": "r-x"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    monkeypatch.setenv("CURSOR_API_KEY", "secret-cursor-key")
    monkeypatch.setenv("HAIPLANE_HUB_TOKEN", "secret-hub-token")
    monkeypatch.setenv("HUB_ENV_CANARY", "canary-value")
    _stub_reviewer(
        monkeypatch,
        tmp_path,
        "import json, os, sys\n"
        "prompt = sys.stdin.read()\n"
        f"open({str(probe)!r}, 'w').write(json.dumps("
        "{'env': dict(os.environ), 'cwd': os.getcwd(), 'argv': sys.argv, "
        "'prompt': prompt}))\n",
    )

    await _submitted(
        client,
        db,
        "spike-local-env",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()

    seen = json.loads(probe.read_text())
    assert "CURSOR_API_KEY" not in seen["env"], (
        "ключ провайдера лежит в окружении хаба и не имеет права уехать в "
        "чужой процесс: окружение собирается белым списком, а не копией"
    )
    assert "HAIPLANE_HUB_TOKEN" not in seen["env"]
    assert "secret-cursor-key" not in json.dumps(seen["env"]), (
        "проверка по ЗНАЧЕНИЮ, а не только по имени: переименованный секрет "
        "утёк бы мимо проверки по ключу"
    )
    assert "canary-value" not in json.dumps(seen["env"]), (
        "канарейка положена в окружение хаба специально: если она доехала, "
        "значит окружение копируется, а не собирается"
    )
    # Утверждать «в ребёнке ровно наш набор» нельзя, и это не придирка:
    # интерпретатор ребёнка и macOS заводят себе переменные сами (LC_CTYPE,
    # __CF_USER_TEXT_ENCODING), причём с теми же значениями, что у родителя, —
    # обе стороны вычислили их одинаково, а не унаследовали. Поэтому что хаб
    # ПЕРЕДАЁТ, спрашивается у сборщика окружения, а что доехало — у ребёнка.
    passed = local_reviewer._clean_env("/scratch/run")
    assert set(passed) <= {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "HOME", "TMPDIR"}, (
        f"передаётся только названный список, а не {sorted(passed)}"
    )
    assert passed["HOME"] == passed["TMPDIR"] == "/scratch/run"
    assert seen["env"]["HOME"] == seen["cwd"], "дом ревьюера — каталог прогона"
    assert seen["cwd"].startswith(str(tmp_path / "scratch")), (
        "работа идёт в одноразовом каталоге, а не в клоне проекта"
    )
    assert not any("/tmp/ws" in str(a) for a in seen["argv"]), (
        "путь к рабочему клону проекта процессу не передаётся вовсе"
    )
    assert "не коммить" in seen["prompt"], "промт тот же, что уходит в облако"
    assert not any("haiplane-review" in str(a) for a in seen["argv"]), (
        "промт уходит в stdin: аргументы видны в ps, а промт несёт "
        "одноразовый код доступа к хабу"
    )
    assert not (tmp_path / "scratch").exists() or not list(
        (tmp_path / "scratch").iterdir()
    ), "каталог прогона одноразовый — после прогона от него ничего не остаётся"


async def test_a_dead_local_reviewer_names_its_cause(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-3: зависший прогон снят, хаб жив, причина названа.

    «Ревью не состоялось» и «ревью ничего не нашло» — разные исходы (#419,
    #725). Молчаливый пропуск здесь означал бы, что вердикт выносится по
    отсутствию отчёта, принятому за чистоту.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-h"}, "run": {"id": "r-h"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, "import time\ntime.sleep(120)\n")
    monkeypatch.setattr(config, "LOCAL_REVIEW_TIMEOUT_SEC", 1)

    task_id = await _submitted(
        client,
        db,
        "spike-local-hang",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()
    await db.commit()

    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert any("снято по таймауту" in a for a in alerts), (
        f"причина обязана быть названа, а не выведена читателем: {alerts}"
    )
    rows = await db.execute_fetchall(
        "SELECT status FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    assert dict(rows[-1])["status"] == "failed"
    _, probe_token = await _agent_key(db, "liveness-probe")
    alive = await client.get(
        f"/api/tasks/{task_id}", headers={"Authorization": f"Bearer {probe_token}"}
    )
    assert alive.status_code == 200, "снятый ревьюер не трогает доступность хаба"


async def test_local_review_obeys_policy_and_cost_ceiling(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-4: без просьбы политики и сверх потолка прогона нет, и это сказано.

    Потолок проверяется на ДОБОРЕ (#879) — единственном месте, где второй
    прогон по той же задаче вообще возможен: проверка новизны отказала бы
    раньше и по другой причине, и тест тогда был бы зелен не за то.
    """
    from hub.services.review_dispatch import maybe_dispatch_review, wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-c"}, "run": {"id": "r-c"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)

    runs: list[str] = []
    real_run = local_reviewer.run_review

    async def _counting(prompt, *, timeout=None, prompt_at_slot=None):
        runs.append(prompt)
        return await real_run(prompt, timeout=timeout, prompt_at_slot=prompt_at_slot)

    monkeypatch.setattr(local_reviewer, "run_review", _counting)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    off_id = await _submitted(
        client,
        db,
        "spike-local-off",
        policy={"review": "off"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()
    assert runs == [], "политика ревью не просила — прогона нет"
    assert not await db.execute_fetchall(
        "SELECT 1 FROM review_dispatches WHERE task_id=?", (off_id,)
    )

    task_id = await _submitted(
        client,
        db,
        "spike-local-ceiling",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()
    await db.commit()
    assert len(runs) == 1, "первый прогон состоялся и стоил 1.5M токенов"

    monkeypatch.setattr(config, "LOCAL_REVIEW_TOKEN_CEILING", 1_000_000)
    dispatched = await maybe_dispatch_review(db, task_id, force_profile=DEEP)

    assert not dispatched and len(runs) == 1, "сверх потолка прогон не покупается"
    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert any("потолок стоимости исчерпан" in a for a in alerts), alerts
    assert any("1500000" in a and "1000000" in a for a in alerts), (
        "названы обе величины: потраченное и потолок — иначе причину нельзя "
        "проверить, не залезая в конфиг"
    )


async def test_github_still_goes_to_the_cloud_reviewer(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-5: на GitHub облачный путь остаётся первым и единственным.

    Контрольный тест ко всем остальным: локальный путь настроен полностью, и
    именно поэтому его молчание здесь что-то значит.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-gh"}, "run": {"id": "r-gh"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    runs: list[str] = []

    async def _never(prompt, *, timeout=None, prompt_at_slot=None):
        runs.append(prompt)
        return None

    monkeypatch.setattr(local_reviewer, "run_review", _never)

    task_id = await _submitted(
        client, db, "spike-github-stays", policy={"review": "dispatch"}
    )

    assert len(recorder.calls) == 1, "GitHub уходит в облако, как и раньше"
    assert runs == [], "локальный прогон на GitHub не запускается"
    rows = await db.execute_fetchall(
        "SELECT channel FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    assert dict(rows[-1])["channel"] == "cloud"


async def test_a_local_run_lost_to_a_restart_fails_with_its_own_cause(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Прогон, начатый другим процессом хаба, не висит активным вечно.

    И, что важнее формы отказа, свип НЕ идёт за его судьбой в Cursor:
    облачный API про локальный прогон знает только то, что такого агента у
    него нет, — то есть вернул бы ложную причину. Отличать канал по строке
    базы, а не по виду идентификатора, только ради этого и стоило.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-r"}, "run": {"id": "r-r"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, "import sys\nsys.stdin.read()\n")

    task_id = await _submitted(
        client,
        db,
        "spike-local-restart",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()
    # Прогон закрылся своей корутиной; возвращаем строку в состояние
    # «активна и старше grace» — так она выглядит после перезапуска хаба.
    await db.execute(
        "UPDATE review_dispatches SET status='active', "
        "created_at = datetime('now', '-60 minutes') WHERE task_id=?",
        (task_id,),
    )
    await db.commit()

    asked: list[str] = []

    async def _get_run(agent_id, run_id):
        asked.append(agent_id)
        return {"id": run_id, "status": "FINISHED"}

    monkeypatch.setattr(cursor_cloud, "get_run", _get_run)

    await sweep_review_dispatches(db)

    assert asked == [], "про локальный прогон облачный API не спрашивают"
    rows = await db.execute_fetchall(
        "SELECT status FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    assert dict(rows[-1])["status"] == "failed"
    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert any("потеряно" in a and "перезапуска" in a for a in alerts), alerts
