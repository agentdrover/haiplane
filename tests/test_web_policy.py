from __future__ import annotations

import re
from httpx import AsyncClient
from hub import repository as repo
from hub.services import project_policy

from tests.web_support import (
    _form_carries_policy_version,  # noqa: F401
    _card,
)


async def test_project_form_sets_gate_policy(client: AsyncClient):
    # AC-1 (#753): the select writes the policy through the same PATCH path,
    # the badge appears, and the form comes back prefilled.
    resp = await client.post(
        "/api/projects", json={"slug": "spike-form", "name": "Spike Form"}
    )
    pid = resp.json()["id"]

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_dor": "auto", "gate_policy_verdict": "human"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" not in resp.headers.get("location", "")

    listed = (await client.get("/api/projects")).json()
    row = next(p for p in listed if p["id"] == pid)
    assert row["gate_policy"] == {"dor": "auto", "verdict": "human"}

    page = (await client.get("/projects")).text
    assert "dor: auto" in page, "the autopilot badge must appear on the card"
    assert 'name="gate_policy_dor"' in page


async def test_project_form_default_locked(client: AsyncClient):
    # AC-2 (#753): the default card shows the lock instead of selects, and a
    # forged submit is refused by the SHARED server-side check, landing in
    # the form as an error — never as a silent success.
    resp = await client.post(
        "/api/projects", json={"slug": "default", "name": "Default"}
    )
    pid = resp.json()["id"]

    page = (await client.get("/projects")).text
    assert "заперт: всегда human" in page

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_dor": "auto", "gate_policy_verdict": "human"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" in resp.headers.get("location", "")

    listed = (await client.get("/api/projects")).json()
    row = next(p for p in listed if p["id"] == pid)
    assert row["gate_policy"] == {}, "the forged submit must change nothing"


async def test_project_form_on_default_keeps_the_allowed_steward_verdict(
    client: AsyncClient,
):
    # #1602: у default форма не показывает селекты dor/verdict, и сохранение
    # формы не имеет права сбросить разрешённое verdict=steward в human.
    resp = await client.post(
        "/api/projects", json={"slug": "default", "name": "Default"}
    )
    pid = resp.json()["id"]
    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"verdict": "steward"}}
    )
    assert resp.status_code == 200, resp.text
    version = (await client.get("/api/projects/default/effective-policy")).json()[
        "policy_version"
    ]

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_review": "off", "policy_version": version},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" not in resp.headers.get("location", "")
    listed = (await client.get("/api/projects")).json()
    row = next(p for p in listed if p["id"] == pid)
    assert row["gate_policy"].get("verdict") == "steward", row["gate_policy"]
    assert row["gate_policy"].get("dor") == "human"
    page = (await client.get("/projects")).text
    assert "кроме verdict=steward" in page


async def test_project_form_exception_is_confined_to_default(client: AsyncClient):
    # #1602 P3: у другого проекта отсутствующее поле формы по-прежнему human.
    resp = await client.post(
        "/api/projects", json={"slug": "spike-other", "name": "Other"}
    )
    pid = resp.json()["id"]
    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"verdict": "steward"}}
    )
    assert resp.status_code == 200, resp.text
    version = (await client.get("/api/projects/spike-other/effective-policy")).json()[
        "policy_version"
    ]
    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_review": "off", "policy_version": version},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    listed = (await client.get("/api/projects")).json()
    row = next(p for p in listed if p["id"] == pid)
    assert row["gate_policy"].get("verdict") == "human", row["gate_policy"]


async def test_project_form_reverts_policy_to_human(client: AsyncClient):
    # AC-3 (#753): rolling the autopilot back is the same single click.
    resp = await client.post(
        "/api/projects", json={"slug": "spike-revert", "name": "Spike Revert"}
    )
    pid = resp.json()["id"]
    await client.patch(f"/api/projects/{pid}", json={"gate_policy": {"dor": "auto"}})

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_dor": "human", "gate_policy_verdict": "human"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    listed = (await client.get("/api/projects")).json()
    row = next(p for p in listed if p["id"] == pid)
    assert row["gate_policy"] == {"dor": "human", "verdict": "human"}


# --- Поверхность выставления гейтов (#1163) ---------------------------------
#
# #1151 расширил множество допустимых значений вердикта, но поверхность, через
# которую их выставляют, осталась прежней: селектор предлагал два значения из
# трёх, а вариант human был помечен выбранным по условию «не auto» — то есть
# при verdict=steward форма ЯВНО показывала то, чего в базе нет, и обработчик
# записывал показанное. Гейт снимался первым же сохранением формы, без следа.
#
# Поэтому проверяется не разметка, а пара «показанное ↔ сохранённое», и
# отдельно — что перечень формы выведён из перечня реализованных значений, а
# не набран руками ещё раз.


def _select_block(page: str, field: str) -> str:
    found = re.search(rf'<select name="{field}".*?</select>', page, re.S)
    assert found, f"на странице нет селектора {field}"
    return found.group(0)


def _offered_values(page: str, field: str) -> list[str]:
    """Значения, которые форма ПРЕДЛАГАЕТ в этом селекторе, по порядку."""
    return re.findall(r'<option value="([^"]*)"', _select_block(page, field))


def _shown_value(page: str, field: str) -> str:
    """Значение, которое форма ПОКАЗЫВАЕТ выбранным — ровно одно."""
    block = _select_block(page, field)
    chosen = re.findall(r'<option value="([^"]*)"[^>]*\bselected\b', block)
    assert len(chosen) == 1, f"{field}: выбранным помечено {len(chosen)} вариантов"
    return chosen[0]


def _what_the_form_would_submit(page: str) -> dict[str, str]:
    """То, что отправит браузер, если человек ничего не трогал.

    Именно этот случай и терял гейт: сохранение ради имени проекта уносило с
    собой все селекторы политики такими, какими форма их показала.
    """
    return {
        f"gate_policy_{key}": _shown_value(page, f"gate_policy_{key}")
        for key in ("dor", "verdict", "review", "release")
    }


async def test_the_form_can_set_verdict_steward(client: AsyncClient):
    # AC-1: значение выставляется через форму, и проверяется это чтением
    # проекта после сохранения, а не наличием option в разметке. Наличие
    # варианта всё же проверяется — но как ПРЕДПОСЫЛКА выбора, а не как
    # доказательство результата: выбрать можно только предложенное.
    pid = await _project_with_policy(client, "spike-steward", {})

    page = (await client.get("/projects")).text
    assert "steward" in _offered_values(page, "gate_policy_verdict")

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_dor": "human", "gate_policy_verdict": "steward"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" not in resp.headers.get("location", "")

    assert await _policy_of(client, pid) == {"dor": "human", "verdict": "steward"}


async def test_saving_the_form_does_not_roll_the_gate_back(client: AsyncClient):
    # AC-2: форма показывает steward выбранным — потеря гейта начиналась
    # именно с этой лжи, — и сохранение показанного его не снимает.
    pid = await _project_with_policy(client, "spike-hold", {"verdict": "steward"})

    page = (await client.get("/projects")).text
    assert _shown_value(page, "gate_policy_verdict") == "steward"

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"name": "Переименовали, гейт не трогали"}
        | _what_the_form_would_submit(page),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" not in resp.headers.get("location", "")

    assert (await _policy_of(client, pid))["verdict"] == "steward"


async def test_the_form_offers_every_implemented_gate_value(client: AsyncClient):
    # AC-3: множества совпадают поимённо для обоих ключей. Значение,
    # добавленное в перечень и забытое в форме (или наоборот), роняет тест.
    await _project_with_policy(client, "spike-offer", {})
    page = (await client.get("/projects")).text

    for key, choices in project_policy.IMPLEMENTED_GATE_VALUES.items():
        assert _offered_values(page, f"gate_policy_{key}") == [
            c.value for c in choices
        ], f"перечень формы для {key} разошёлся с перечнем реализованных значений"


async def test_an_unimplemented_value_is_not_offered(client: AsyncClient):
    # AC-4: dor=steward API примет — поведения за ним нет до #1157, и форма
    # его не предлагает. Выбор, за которым ничего не происходит, — такой же
    # обман, как и спрятанное реализованное значение.
    pid = await _project_with_policy(client, "spike-unimplemented", {})
    accepted = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"dor": "steward"}}
    )
    assert accepted.status_code == 200, "граница приёма не сужается — это не её работа"

    page = (await client.get("/projects")).text
    assert "steward" not in _offered_values(page, "gate_policy_dor")
    assert "steward" in _offered_values(page, "gate_policy_verdict")


async def test_the_card_shows_the_stored_delegate(client: AsyncClient):
    # AC-5: витрина называет делегата, а не молчит о нём. Блок «автопилот»
    # сравнивал значение со строкой auto и при steward не рисовался вовсе.
    await _project_with_policy(client, "spike-card", {"verdict": "steward"})
    page = (await client.get("/projects")).text
    assert "verdict: steward" in page
    assert project_policy.IMPLEMENTED_GATE_VALUES["verdict"][-1].hint in page


def test_every_implemented_value_is_accepted_and_labelled():
    # Реализованное обязано быть принимаемым: перечень «что уже работает»
    # живёт рядом с GATE_VALUES и сверяется с ним на включение. Значение без
    # подписи роняет этот тест, а не исчезает из формы молча.
    for key, choices in project_policy.IMPLEMENTED_GATE_VALUES.items():
        assert choices, f"{key}: гейт без реализованных значений"
        values = [c.value for c in choices]
        assert len(values) == len(set(values)), f"{key}: значение перечислено дважды"
        assert set(values) <= project_policy.GATE_VALUES, (
            f"{key}: перечень предлагает значение, которого не примет API"
        )
        assert project_policy.GATE_HUMAN in values, f"{key}: human обязан остаться"
        for choice in choices:
            assert choice.label.strip(), f"{key}/{choice.value}: нет подписи в форме"
            assert choice.hint.strip(), f"{key}/{choice.value}: нет подписи на карточке"

    # Читатель политики спрашивает ровно эти гейты и спрашивает их литералами,
    # чтобы сверка разрешённых ключей их видела. Гейт, добавленный в перечень
    # и забытый в читателе, отсюда и виден.
    assert set(project_policy._stored_gate_values({})) == set(
        project_policy.IMPLEMENTED_GATE_VALUES
    ), "перечень гейтов разошёлся с тем, что читатель спрашивает у политики"


# --- Релизная политика в форме проекта (#926) -------------------------------
#
# #812 дал проекту ручку release, но переключать её можно было только PATCH-ом,
# то есть curl вместо клика. Стоп-кран, записанный первым шагом условия
# пересмотра в #927, обязан жить там же, где остальные ручки политики.


async def _project_with_policy(client: AsyncClient, slug: str, policy: dict) -> int:
    resp = await client.post("/api/projects", json={"slug": slug, "name": slug})
    pid = resp.json()["id"]
    if policy:
        resp = await client.patch(f"/api/projects/{pid}", json={"gate_policy": policy})
        assert resp.status_code == 200, resp.text
    return pid


async def _policy_of(client: AsyncClient, pid: int) -> dict:
    listed = (await client.get("/api/projects")).json()
    return next(p for p in listed if p["id"] == pid)["gate_policy"]


async def test_project_form_saves_release_auto(client: AsyncClient):
    # AC-1: the knob is offered to the default project too — the #743 lock is
    # about taking a human OUT of a gate, and a release merge decides nothing
    # that was not already approved task by task (#812).
    from hub.services.project_policy import release_auto_enabled

    pid = await _project_with_policy(client, "default", {})

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_release": "auto"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" not in resp.headers.get("location", "")

    policy = await _policy_of(client, pid)
    assert release_auto_enabled(policy), (
        "the reader is what decides whether the autopilot releases; asserting "
        f"on the raw string would pass a key it never reads. Stored: {policy}"
    )


async def test_project_form_switches_release_back_to_manual(client: AsyncClient):
    # AC-2: the half that carry-through (#886) can silently undo. release is
    # in _FORM_GATE_POLICY_KEYS precisely so an un-chosen knob the form DID
    # show means "remove it" — otherwise the stored 'auto' comes straight back
    # and the stop-lever only works in one direction.
    from hub.services.project_policy import release_auto_enabled

    pid = await _project_with_policy(client, "spike-release", {"release": "auto"})

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_release": "manual"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    policy = await _policy_of(client, pid)
    assert "release" not in policy, f"manual is the absence of the key: {policy}"
    assert not release_auto_enabled(policy)


async def test_unknown_release_value_is_dropped(client: AsyncClient):
    # AC-3: same shape as review (#805) — only the recognised value is stored,
    # never a knob nothing reads.
    pid = await _project_with_policy(client, "spike-garbage", {})

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_release": "yes-please"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    policy = await _policy_of(client, pid)
    assert "release" not in policy, f"garbage must not become a knob: {policy}"


async def test_release_auto_allowed_on_default_but_verdict_lock_holds(
    client: AsyncClient,
):
    # AC-4: the two must not be confused. Release is delegable on default;
    # the verdict gate is not, and adding this knob may not open that door.
    pid = await _project_with_policy(client, "default", {})

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_release": "auto"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" not in resp.headers.get("location", "")
    assert (await _policy_of(client, pid)).get("release") == "auto"

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_release": "auto", "gate_policy_verdict": "auto"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" in resp.headers.get("location", ""), (
        "the #743 lock refuses 'auto' on the verdict gate for the default "
        "project, and a release select on the same form cannot soften it"
    )
    policy = await _policy_of(client, pid)
    assert policy.get("verdict") != "auto"
    assert policy.get("release") == "auto", "the refused submit changed nothing"


async def test_projects_page_shows_current_release_policy(client: AsyncClient):
    # AC-5: a switch that always renders its default is a switch that lies
    # about the state it is in.
    await _project_with_policy(client, "spike-auto", {"release": "auto"})
    await _project_with_policy(client, "spike-manual", {})

    page = (await client.get("/projects")).text
    assert 'name="gate_policy_release"' in page
    assert '<option value="auto" selected>' in page
    assert '<option value="manual" selected>' in page


async def test_release_select_does_not_break_carry_through(client: AsyncClient):
    # AC-6: ci_runner is still a knob the form never shows, and #886 says a
    # submit cannot mean "remove it". Adding release to _FORM_GATE_POLICY_KEYS
    # must move release out of that set and nothing else.
    pid = await _project_with_policy(
        client, "spike-carry", {"ci_runner": "make test", "release": "auto"}
    )

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_release": "auto"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    policy = await _policy_of(client, pid)
    assert policy.get("ci_runner") == "make test", (
        "the form has no ci_runner field, so submitting it cannot mean "
        f"'remove ci_runner' (#886). Stored: {policy}"
    )
    assert policy.get("release") == "auto"


async def _local_reviewer_ready(db, monkeypatch, tmp_path) -> None:
    """Настроить локальный путь так, как его требует выкат (#1180).

    Конфигурация — настоящая, включая права каталога прогонов: именно её
    читает страж, и подменять её значило бы проверять не то. А вот разрешение
    токена в принципала подменяется: настоящее требует закрытого режима
    аутентификации, в котором сама страница /projects отвечает 401, и тест
    проверял бы уже не форму. Идентичность ревьюера закреплена там, где она
    решает, — tests/test_forge_links.py на пути записи.
    """
    import os
    import pwd

    from hub import config
    from hub.services import review_dispatch

    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    os.chmod(scratch, 0o2770)
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "/bin/true")
    # Песочница обязана быть формой из ЗАКРЫТОГО НАБОРА (#1208): «/usr/bin/env»
    # хаб больше не принимает, и готовность на такой строке была бы «не
    # настроено» — то есть тест судил бы не то, что называет. Пользователь —
    # сам вызывающий: он владеет каталогом прогонов и проходит по группе.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        f"/usr/bin/sudo -n -u {pwd.getpwuid(os.getuid()).pw_name} /usr/local/bin/wrap",
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(scratch))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "reviewer-key")

    async def _principal(_db):
        return 7

    monkeypatch.setattr(review_dispatch, "local_reviewer_principal_id", _principal)


async def test_review_dispatch_is_not_offered_when_nothing_can_run_it(
    client: AsyncClient, db
):
    """AC-1: нечем исполнить — не предлагаем, и говорим почему.

    Проверяется рендером страницы и ответом API, а не чтением кода: селект,
    который предлагает невыполнимое, выглядит в коде так же, как рабочий.
    """
    created = await client.post(
        "/api/projects", json={"slug": "reach-none", "name": "Reach None"}
    )
    pid = created.json()["id"]
    assert (
        await client.patch(f"/api/projects/{pid}", json={"forge": "gitverse"})
    ).status_code == 200

    card = _card((await client.get("/projects")).text, "reach-none")

    assert 'name="gate_policy_review"' in card, "селект на месте"
    assert 'value="dispatch"' not in card, (
        "выбор, который этот проект не исполнит, предлагать нельзя — человек "
        "выберет существующий пункт меню и получит отказ"
    )
    assert "dispatch недоступен" in card and "gitverse" in card, (
        "молча убранный пункт — такой же обман, как пункт без исполнения: "
        "причина обязана стоять рядом"
    )
    refused = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"review": "dispatch"}}
    )
    assert refused.status_code == 422, "и запись отказана той же причиной"
    assert refused.json()["detail"]["error"] == "review_unrunnable_here"


async def test_review_dispatch_is_offered_when_the_local_reviewer_is_ready(
    client: AsyncClient, db, monkeypatch, tmp_path
):
    """AC-2 со стороны формы: настроенный локальный путь возвращает выбор."""
    created = await client.post(
        "/api/projects", json={"slug": "reach-local", "name": "Reach Local"}
    )
    pid = created.json()["id"]
    assert (
        await client.patch(f"/api/projects/{pid}", json={"forge": "gitverse"})
    ).status_code == 200
    await _local_reviewer_ready(db, monkeypatch, tmp_path)

    card = _card((await client.get("/projects")).text, "reach-local")

    assert 'value="dispatch"' in card, (
        "локальный ревьюер настроен — значит ревью здесь добывается, и "
        "прятать выбор больше не за что (#1180)"
    )
    assert "dispatch недоступен" not in card


async def test_review_dispatch_is_still_offered_on_github(client: AsyncClient):
    """AC-3, контроль: без него AC-1 был бы зелён и при удалённом селекте."""
    created = await client.post(
        "/api/projects", json={"slug": "reach-github", "name": "Reach GitHub"}
    )
    pid = created.json()["id"]

    card = _card((await client.get("/projects")).text, "reach-github")
    assert 'value="dispatch"' in card
    assert "dispatch недоступен" not in card

    saved = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"review": "dispatch"}}
    )
    assert saved.status_code == 200 and saved.json()["gate_policy"]["review"] == (
        "dispatch"
    )


async def test_the_form_shows_the_stored_review_value_even_when_unoffered(
    client: AsyncClient, db
):
    """AC-4: сужение перечня не имеет права лгать о сохранённом (#1163)."""
    created = await client.post(
        "/api/projects", json={"slug": "reach-stored", "name": "Reach Stored"}
    )
    pid = created.json()["id"]
    assert (
        await client.patch(
            f"/api/projects/{pid}", json={"gate_policy": {"review": "dispatch"}}
        )
    ).status_code == 200
    # Форж меняем в обход инварианта — именно так это состояние и достижимо
    # в жизни: политику поставили, когда способ был, а потом он пропал.
    await repo.update_project(db, pid, forge="gitverse")
    await db.commit()

    card = _card((await client.get("/projects")).text, "reach-stored")

    assert 'value="dispatch"' in card and "selected" in card, (
        "хранимое значение показывается выбранным, даже когда его больше не "
        "предлагают: иначе форма утверждает то, чего в базе нет"
    )


async def test_a_refused_project_edit_is_shown_at_that_project(client: AsyncClient):
    """AC-5: отказ показан у карточки, а не под всеми проектами.

    Мера — ПОЛОЖЕНИЕ: до правки нота стояла последним блоком страницы (символ
    15925 из 18038 при двух проектах), и «есть на странице» выполнялось,
    когда дефект был в полном разгаре.
    """
    first = await client.post(
        "/api/projects", json={"slug": "refused-here", "name": "Refused Here"}
    )
    pid = first.json()["id"]
    assert (
        await client.patch(f"/api/projects/{pid}", json={"forge": "gitverse"})
    ).status_code == 200
    await client.post(
        "/api/projects", json={"slug": "zz-other", "name": "Other Project"}
    )

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"name": "Refused Here", "gate_policy_review": "dispatch"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = (await client.get(resp.headers["location"])).text

    card = _card(page, "refused-here")
    assert "project-error-note" in card, (
        "нота обязана стоять внутри карточки своего проекта, а не под всеми: "
        "до правки она была последним блоком страницы — символ 15925 из 18038 "
        "при двух проектах"
    )
    assert "project-error-note" not in _card(page, "zz-other"), (
        "чужая карточка про этот отказ ничего не знает"
    )
    assert page.count("project-error-note") == 1, (
        "второго экземпляра того же текста внизу страницы быть не должно"
    )
    assert '<details class="project-admin-edit" open' in card, (
        "форма открыта: схлопнувшаяся после редиректа, она показывала прежние "
        "значения и читалась как «ничего не произошло»"
    )


async def test_the_refusal_names_the_project_and_the_field(client: AsyncClient):
    """AC-6: из текста видно проект, поле и цену отказа."""
    created = await client.post(
        "/api/projects", json={"slug": "refusal-text", "name": "Refusal Text"}
    )
    pid = created.json()["id"]
    assert (
        await client.patch(f"/api/projects/{pid}", json={"forge": "gitverse"})
    ).status_code == 200

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"name": "Refusal Text", "gate_policy_review": "dispatch"},
        follow_redirects=False,
    )
    page = (await client.get(resp.headers["location"])).text
    note = page[page.index("project-error-note") :][:900]

    assert "refusal-text" in note, "какой из девяти проектов — должно быть сказано"
    assert "Агентское ревью на сдаче" in note, (
        "поле названо тем же именем, каким подписано в форме: иначе человек "
        "перебирает всё подряд"
    )
    assert "НИЧЕГО из этой отправки не сохранено" in note, (
        "цена отказа — вся отправка, и молчать об этом значит оставить человека "
        "с мыслью, что сохранилось хоть что-то"
    )


# --- Теневое участие стюарда включается формой, а не PATCH руками (#1280) ----
#
# #1268 завела ключ steward_shadow: проект участвует в теневой фазе, вердикт
# остаётся человеку, применение суждения запрещено. Ключ принимал только API,
# и теневая фаза не начиналась ни на одном проекте, пока её включение стоило
# человеческого токена и ручного запроса.
#
# Отдельного внимания стоит ВЫКЛЮЧЕНИЕ. Снятый чекбокс браузер не присылает
# вовсе, поэтому рычаг, собранный «наивно», работает в одну сторону: включить
# можно, выключить нельзя. Здесь это закрыто скрытым полем с тем же именем —
# намерение «выключить» приезжает в запросе, а не выводится из молчания.


async def test_the_project_form_turns_shadow_participation_on(client: AsyncClient):
    """AC-1: галочка на default включает теневое участие, гейты не трогая."""
    import json

    from hub.services.steward_dispatch import STEWARD_SHADOW_KEY, _policy_wants_steward

    pid = await _project_with_policy(client, "default", {})

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_steward_shadow": ["off", "on"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" not in resp.headers.get("location", ""), (
        "теневое участие ничего не делегирует, поэтому замок #743 его "
        f"пропускает: {resp.headers.get('location', '')}"
    )

    policy = await _policy_of(client, pid)
    assert policy.get(STEWARD_SHADOW_KEY) is True, (
        "читатель ключа сравнивает именно с True (#1268), так что строка "
        f"'on' в политике была бы ключом, которого никто не читает: {policy}"
    )
    assert _policy_wants_steward({"gate_policy": json.dumps(policy)}), (
        f"включённым участие считает диспетчер прогонов, а не форма: {policy}"
    )
    assert policy.get("dor") == "human" and policy.get("verdict") == "human", (
        f"вердикт и DoR остаются у человека: {policy}"
    )


async def test_the_project_form_turns_shadow_participation_off(client: AsyncClient):
    """AC-2: снятая галочка выключает участие, а не сохраняет прежнее."""
    import json

    from hub.services.steward_dispatch import STEWARD_SHADOW_KEY, _policy_wants_steward

    pid = await _project_with_policy(client, "default", {STEWARD_SHADOW_KEY: True})

    # Ровно то, что шлёт браузер со снятым чекбоксом: скрытое поле и ничего
    # больше. Без него запрос был бы неотличим от «этот POST про переключатель
    # не знает», и выключить участие через форму стало бы нельзя.
    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_steward_shadow": "off"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" not in resp.headers.get("location", "")

    policy = await _policy_of(client, pid)
    assert policy.get(STEWARD_SHADOW_KEY) is False, (
        f"участие выключено, а не оставлено прежним: {policy}"
    )
    assert not _policy_wants_steward({"gate_policy": json.dumps(policy)}), (
        "и диспетчер больше не заказывает теневой прогон на этом проекте"
    )


async def test_saving_the_shadow_toggle_keeps_unknown_policy_keys(
    client: AsyncClient,
):
    """AC-3: ключи, которых форма не показывает, переживают сохранение (#1163)."""
    from hub.services.steward_dispatch import STEWARD_SHADOW_KEY

    pid = await _project_with_policy(
        client, "spike-shadow-carry", {"ci_runner": "make test", "release": "auto"}
    )

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={
            "gate_policy_steward_shadow": ["off", "on"],
            "gate_policy_release": "auto",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    policy = await _policy_of(client, pid)
    assert policy.get("ci_runner") == "make test", (
        "формы для ci_runner нет, поэтому её отправка не может значить "
        f"«убрать ci_runner» (#886): {policy}"
    )
    assert policy.get(STEWARD_SHADOW_KEY) is True and policy.get("release") == "auto"


async def test_the_projects_page_shows_shadow_participation_state(
    client: AsyncClient,
):
    """AC-4: переключатель показывает сохранённое состояние и честную подпись."""
    from hub.services.steward_dispatch import STEWARD_SHADOW_KEY

    await _project_with_policy(client, "spike-shadow-on", {STEWARD_SHADOW_KEY: True})
    await _project_with_policy(client, "spike-shadow-off", {})

    page = (await client.get("/projects")).text
    on_card = _card(page, "spike-shadow-on")
    off_card = _card(page, "spike-shadow-off")

    assert 'name="gate_policy_steward_shadow"' in on_card, "переключатель на месте"
    assert "checked" in on_card.split('name="gate_policy_steward_shadow"')[-1][:120], (
        "включённое участие показано включённым: переключатель, всегда "
        "рисующий своё значение по умолчанию, врёт о состоянии проекта"
    )
    assert (
        "checked" not in off_card.split('name="gate_policy_steward_shadow"')[-1][:120]
    ), f"выключенное участие не показывается включённым: {off_card[:400]}"
    assert "вердикт остаётся у человека" in on_card, (
        "подпись рядом обязана сказать, что суждение записывается, а решение "
        "остаётся человеку — иначе переключатель обещает больше, чем делает"
    )


# --- Сохранение формы проекта не теряет ключей политики (#1335) --------------
#
# #1264 завёл лимит очереди review — ключи review_limit и review_limit_mode,
# которые выставлялись только через API. Форма проекта собирает gate_policy
# сама, а PATCH заменяет политику целиком, поэтому любой ключ, который форма
# не знает, держится только на том, что сборщик начинает с сохранённой
# политики. Здесь это проверено и на именах лимита, и на классе, и на том,
# что форма теперь показывает и правит сам лимит.

#: Политика default на проде в день постановки #1335: сохранение формы обязано
#: оставить её ровно такой, какой она была, кроме правленого поля.
_PROD_DEFAULT_POLICY = {
    "dor": "human",
    "verdict": "human",
    "review": "dispatch",
    "release": "auto",
    "steward_shadow": True,
    "review_limit": 8,
    "review_limit_mode": "warn",
}


async def test_saving_the_project_form_keeps_the_review_limit(client: AsyncClient):
    """AC-1: правка тени стюарда не стирает review_limit и review_limit_mode."""
    pid = await _project_with_policy(client, "default", dict(_PROD_DEFAULT_POLICY))

    # Запрос, который знает только о переключателе: полей лимита в нём нет.
    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_steward_shadow": "off"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" not in resp.headers.get("location", ""), resp.headers
    policy = await _policy_of(client, pid)
    assert policy.get("review_limit") == 8, f"лимит стёрт сохранением: {policy}"
    assert policy.get("review_limit_mode") == "warn", f"режим стёрт: {policy}"

    # И запрос, который шлёт браузер с настоящей страницы: форма показывает
    # сохранённый лимит, человек меняет только тень — политика та же, кроме
    # тени, и замок #743 на default этот запрос пропускает.
    card = _card((await client.get("/projects")).text, "default")
    assert re.search(r'name="gate_policy_review_limit"[^>]*value="8"', card), (
        f"форма показывает сохранённый лимит: {card[:600]}"
    )
    assert re.search(r'<option value="warn"\s+selected', card), (
        "форма показывает сохранённый режим лимита"
    )
    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={
            "gate_policy_review": "dispatch",
            "gate_policy_release": "auto",
            "gate_policy_steward_shadow": ["off", "on"],
            "gate_policy_review_limit": "8",
            "gate_policy_review_limit_mode": "warn",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" not in resp.headers.get("location", ""), resp.headers
    assert await _policy_of(client, pid) == _PROD_DEFAULT_POLICY


async def test_saving_the_project_form_keeps_unknown_policy_keys(
    client: AsyncClient, db
):
    """AC-2: ключ, которого форма не знает, переживает сохранение — любой.

    Класс выводится, а не перечисляется: это все ключи, которые принимает
    запись (GATE_POLICY_KEYS), за вычетом ручек, которые форма показывает
    всегда (_FORM_GATE_POLICY_KEYS). Новый ключ попадает в проверку сам — от
    автора нужен только образец значения, и без него тест падает, называя
    ключ. Ключ, которого запись не знает вовсе, форма потерять тоже не может:
    сохранение отказывает вслух и не пишет ничего.
    """
    import json

    from hub.models import GATE_POLICY_KEYS
    from hub.web import _FORM_GATE_POLICY_KEYS, _REVIEW_COST_KEYS

    samples = {
        "ci_runner": "make test",
        "base_fetch_wait_hours": 36,
        "review_limit": 3,
        "review_limit_mode": "warn",
        "orchestrator_queue": "shadow",
        "wip_limit": 2,
        "executor_launch": "manual",
        "executor_push_rights_task": 1409,
        "executor_task_cents_ceiling": 10500,
        "executor_task_token_ceiling": 24000000,
        "submission_contract": "warn",
        "claim_area_check": "warn",
        "claim_project_check": "warn",
        "bug_red_test": "warn",
        "ci_before_submit": "warn",
        "statement_paths": "require",
        "delivery_fresh_base_rerun": "on",
        "slot_dead_minutes": 90,
        "merge_is_delivery": True,
        "path_notices": [{"pattern": "deploy/**", "text": "ручной шаг"}],
        "freeze": {
            "until": None,
            "allow_work_types": ["bug", "chore"],
            "note": "до MS-A2",
        },
        "critic_host": "k8",
        "steward_watch": ["needs_decision", "release"],
        "steward_approve_max_class": "r1",
        "auto_approve_max_class": "r1",
        "steward_approve_max_class_double_clean": "r3",  # #1986
        "double_clean_reviewers": [21, 22],
        "ci_contract": {
            "workflow_path": ".github/workflows/tests.yml",
            "required_checks": ["tests"],
        },
        "release_artifacts": [
            {
                "repo_path": "deploy/x.sh",
                "server_path": "/usr/local/sbin/x.sh",
                "update_hint": "обновить копию",
            }
        ],
    }
    # #1638: четыре настройки стоимости ревью форма теперь показывает; их
    # сохранность проверяют явные test_project_form_*review_cost* ниже.
    not_shown = set(GATE_POLICY_KEYS) - _FORM_GATE_POLICY_KEYS - set(_REVIEW_COST_KEYS)
    assert not_shown, "класс пуст — проверять нечего, тест бы лгал"
    missing = not_shown - set(samples)
    assert not missing, f"дайте образец значения для {sorted(missing)}"
    stored = {key: samples[key] for key in not_shown}
    pid = await _project_with_policy(client, "spike-unknown-keys", stored)

    for data in (
        {"gate_policy_steward_shadow": "off"},
        {"gate_policy_dor": "human", "gate_policy_verdict": "human"},
        {"gate_policy_release": "manual", "gate_policy_review": "off"},
        {"gate_policy_dor_max_class": "", "gate_policy_risk_map": ""},
    ):
        resp = await client.post(
            f"/projects/{pid}/web-edit", data=data, follow_redirects=False
        )
        assert resp.status_code == 303
        assert "project_error" not in resp.headers.get("location", ""), (data, resp)
        policy = await _policy_of(client, pid)
        lost = {k: v for k, v in stored.items() if policy.get(k) != v}
        assert not lost, f"сохранение {data} потеряло {lost}: {policy}"

    # Ключ, положенный мимо API и неизвестный записи: сохранение не роняет его
    # молча, а отказывает и оставляет политику как была.
    legacy = {**stored, "legacy_knob": {"nested": [1, 2]}}
    await db.execute(
        "UPDATE projects SET gate_policy = ? WHERE id = ?", (json.dumps(legacy), pid)
    )
    await db.commit()
    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_steward_shadow": "off"},
        follow_redirects=False,
    )
    assert "legacy_knob" in resp.headers.get("location", ""), resp.headers
    assert await _policy_of(client, pid) == legacy


async def test_the_project_form_edits_the_review_limit(client: AsyncClient):
    """AC-3: лимит 8/warn записывается, пустое поле его снимает, неверное — отказ."""
    from urllib.parse import unquote_plus

    import pytest

    from hub.models import _validate_review_limit

    pid = await _project_with_policy(client, "spike-limit-form", {})

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_review_limit": "8", "gate_policy_review_limit_mode": "warn"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" not in resp.headers.get("location", ""), resp.headers
    policy = await _policy_of(client, pid)
    assert policy.get("review_limit") == 8, f"число, а не строка: {policy}"
    assert policy.get("review_limit_mode") == "warn", policy

    # Неверные значения отказывает ТА ЖЕ проверка, что у API: текст отказа —
    # её текст, и сохранённое не меняется.
    for raw, mode in (("0", "warn"), ("abc", "warn"), ("2.5", "warn"), ("3", "loud")):
        resp = await client.post(
            f"/projects/{pid}/web-edit",
            data={
                "gate_policy_review_limit": raw,
                "gate_policy_review_limit_mode": mode,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = unquote_plus(resp.headers.get("location", ""))
        assert "project_error" in location, (raw, mode, location)
        with pytest.raises(ValueError) as refused:
            _validate_review_limit(
                {
                    "review_limit": int(raw) if raw.isdigit() else raw,
                    "review_limit_mode": mode,
                }
            )
        assert str(refused.value) in location, (raw, mode, location)
        assert await _policy_of(client, pid) == policy, "отказ ничего не записал"

    # Пустое поле — явное «снять лимит»: оба ключа уходят.
    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_review_limit": "", "gate_policy_review_limit_mode": "warn"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" not in resp.headers.get("location", ""), resp.headers
    policy = await _policy_of(client, pid)
    assert "review_limit" not in policy and "review_limit_mode" not in policy, policy


_COST_KEYS_STORED = {
    "deep_reviewer": "local",
    "deep_daily_cap": 0,
    "small_delta_lines": 120,
    "circle_deep_stop": 3,
}


def _cost_form_block(page: str, pid: int) -> str:
    """Кусок страницы от формы правки проекта pid до следующей формы правки."""
    marker = f"/projects/{pid}/web-edit"
    start = page.index(marker)
    nxt = page.find("/web-edit", start + len(marker))
    return page[start : nxt if nxt != -1 else len(page)]


def _now_text(block: str, key: str) -> str:
    found = re.search(rf'data-key="{key}">([^<]*)<', block)
    assert found, f"нет «действует» для {key}"
    return found.group(1)


async def test_project_form_shows_review_cost_settings_with_effective_values(
    client: AsyncClient, monkeypatch
):
    """AC-1: четыре контрола, действующее значение и источник, подписи нуля."""
    from hub import config

    monkeypatch.setattr(config, "REVIEW_SMALL_DELTA_LINES", "80")
    monkeypatch.setattr(config, "REVIEW_DEEP_DAILY_CAP", "")
    monkeypatch.setattr(config, "REVIEW_CIRCLE_DEEP_STOP", "")
    empty = await _project_with_policy(client, "cost-empty", {})
    full = await _project_with_policy(client, "cost-full", dict(_COST_KEYS_STORED))
    page = (await client.get("/projects")).text

    for pid in (empty, full):
        block = _cost_form_block(page, pid)
        for name in (
            "gate_policy_deep_reviewer",
            "gate_policy_deep_daily_cap",
            "gate_policy_small_delta_lines",
            "gate_policy_circle_deep_stop",
        ):
            assert f'name="{name}"' in block, (pid, name)
        assert "0 — обычный deep уходит в lite" in block
        assert block.count("0 — правило удешевления выключено") == 2

    block = _cost_form_block(page, empty)
    assert "действует: 80 (server)" in _now_text(block, "small_delta_lines")
    assert "cloud (default)" in _now_text(block, "deep_reviewer")
    assert "без потолка (default)" in _now_text(block, "deep_daily_cap")
    assert "правило выключено (default)" in _now_text(block, "circle_deep_stop")
    assert "наследовать: cloud" in block

    block = _cost_form_block(page, full)
    assert "local (project)" in _now_text(block, "deep_reviewer")
    assert "действует: 0 (project)" in _now_text(block, "deep_daily_cap")
    assert "120 (project)" in _now_text(block, "small_delta_lines")
    assert "3 (project)" in _now_text(block, "circle_deep_stop")
    assert (
        'name="gate_policy_deep_daily_cap" class="task-action-control" value="0"'
        in block
    )
    assert re.search(r'<option value="local"\s+selected', block)


async def test_projects_page_isolates_review_cost_failure_and_skips_full_summary(
    client: AsyncClient, monkeypatch
):
    """P2 Codex: сбой одного проекта не роняет /projects; сводка целиком не считается."""
    from hub.services import effective_policy

    await _project_with_policy(client, "cost-ok-a", dict(_COST_KEYS_STORED))
    boom = await _project_with_policy(client, "cost-boom", {})
    ok_b = await _project_with_policy(client, "cost-ok-b", {})

    calls = {"full": 0}
    real = effective_policy.key_rows

    def flaky(project):
        if project["slug"] == "cost-boom":
            raise RuntimeError("boom")
        return real(project)

    async def no_full(*args, **kwargs):
        calls["full"] += 1
        raise AssertionError("форма не должна считать сводку целиком")

    monkeypatch.setattr(effective_policy, "key_rows", flaky)
    monkeypatch.setattr(effective_policy, "effective_policy", no_full)
    resp = await client.get("/projects")
    assert resp.status_code == 200
    assert calls["full"] == 0
    page = resp.text
    assert "действующее значение не рассчитано" in _cost_form_block(page, boom)
    assert "(default)" in _now_text(_cost_form_block(page, ok_b), "deep_reviewer")


async def test_project_form_saves_review_cost_settings(client: AsyncClient):
    """AC-2: четыре значения с правильным типом, 0 не удалён, чужие ключи целы."""
    other = {"ci_runner": "make test", "wip_limit": 2, "executor_launch": "manual"}
    pid = await _project_with_policy(client, "cost-save", dict(other))
    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={
            "gate_policy_deep_reviewer": "local",
            "gate_policy_deep_daily_cap": "0",
            "gate_policy_small_delta_lines": "120",
            "gate_policy_circle_deep_stop": "3",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" not in resp.headers.get("location", ""), resp.headers
    policy = await _policy_of(client, pid)
    for key, value in _COST_KEYS_STORED.items():
        assert policy[key] == value, (key, policy)
        assert type(policy[key]) is type(value), (key, policy)
    assert {k: policy[k] for k in other} == other


async def test_project_form_review_cost_fields_absent_keeps_and_empty_clears(
    client: AsyncClient,
):
    """AC-3: пустые поля снимают ключи; запрос без полей их не трогает."""
    pid = await _project_with_policy(client, "cost-keep", dict(_COST_KEYS_STORED))

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_review": "off", "gate_policy_steward_shadow": "off"},
        follow_redirects=False,
    )
    assert "project_error" not in resp.headers.get("location", ""), resp.headers
    policy = await _policy_of(client, pid)
    assert {k: policy.get(k) for k in _COST_KEYS_STORED} == _COST_KEYS_STORED

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={
            "gate_policy_deep_reviewer": "",
            "gate_policy_deep_daily_cap": "",
            "gate_policy_small_delta_lines": "",
            "gate_policy_circle_deep_stop": "",
        },
        follow_redirects=False,
    )
    assert "project_error" not in resp.headers.get("location", ""), resp.headers
    policy = await _policy_of(client, pid)
    assert not set(_COST_KEYS_STORED) & set(policy), policy
    rows = {
        row["key"]: row
        for row in (
            await client.get("/api/projects/cost-keep/effective-policy")
        ).json()["keys"]
    }
    for key in _COST_KEYS_STORED:
        assert rows[key]["source"] in ("server", "default"), rows[key]


async def test_project_form_rejects_bad_review_cost_values(client: AsyncClient):
    """AC-4: -1, 1.5, abc и deep_reviewer=mars — отказ текстом общего валидатора."""
    from urllib.parse import unquote_plus

    import pytest

    from hub.models import _validate_count, _validate_deep_reviewer

    pid = await _project_with_policy(client, "cost-bad", dict(_COST_KEYS_STORED))
    before = await _policy_of(client, pid)

    for key in ("deep_daily_cap", "small_delta_lines", "circle_deep_stop"):
        for raw in ("-1", "1.5", "abc"):
            resp = await client.post(
                f"/projects/{pid}/web-edit",
                data={f"gate_policy_{key}": raw},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            location = unquote_plus(resp.headers.get("location", ""))
            assert "project_error" in location, (key, raw, location)
            with pytest.raises(ValueError) as refused:
                _validate_count({key: int(raw) if raw == "-1" else raw}, key)
            assert str(refused.value) in location, (key, raw, location)
            assert await _policy_of(client, pid) == before

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_deep_reviewer": "mars", "gate_policy_deep_daily_cap": "5"},
        follow_redirects=False,
    )
    location = unquote_plus(resp.headers.get("location", ""))
    assert "project_error" in location, location
    with pytest.raises(ValueError) as refused:
        _validate_deep_reviewer({"deep_reviewer": "mars"})
    assert str(refused.value) in location, location
    assert await _policy_of(client, pid) == before, "отказ атомарный"


async def test_project_form_review_cost_fields_keep_default_lock_and_version(
    client: AsyncClient,
):
    """AC-5: на default новые ключи сохранены, verdict=steward цел; старая форма отклонена."""
    from urllib.parse import unquote

    pid = await _project_with_policy(client, "default", {"verdict": "steward"})
    old = (await client.get("/api/projects/default/effective-policy")).json()[
        "policy_version"
    ]
    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"gate_policy_deep_daily_cap": "7", "gate_policy_deep_reviewer": "local"},
        follow_redirects=False,
    )
    assert "project_error" not in resp.headers.get("location", ""), resp.headers
    policy = await _policy_of(client, pid)
    assert policy.get("deep_daily_cap") == 7 and policy.get("deep_reviewer") == "local"
    assert policy.get("verdict") == "steward", policy

    # Форма, открытая до сохранения выше, несёт прежнюю версию политики.
    stale = await client.post(
        f"/projects/{pid}/web-edit",
        data={"policy_version": old, "gate_policy_small_delta_lines": "55"},
        follow_redirects=False,
    )
    assert "политика проекта изменилась" in unquote(stale.headers["location"])
    assert await _policy_of(client, pid) == policy, "ничего не сохранено"


async def test_emptied_project_form_fields_still_clear_their_keys(
    client: AsyncClient,
):
    """Риск #1335: каждое поле, которое форма сбрасывала пустым, сбрасывает и теперь.

    Сборщик начинает с сохранённой политики, поэтому снятие ручки держится на
    том, что её ключ назван ручкой формы. Проверяется поимённо.
    """
    pid = await _project_with_policy(
        client,
        "spike-clear-all",
        {
            "dor": "auto",
            "verdict": "auto",
            "review": "dispatch",
            "release": "auto",
            "steward_shadow": True,
            "dor_max_class": "r1",
            "risk_map": {"docs/**": "docs"},
            "review_limit": 5,
            "review_limit_mode": "warn",
            "ci_runner": "make test",
        },
    )
    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={
            "gate_policy_dor": "",
            "gate_policy_verdict": "",
            "gate_policy_review": "off",
            "gate_policy_release": "manual",
            "gate_policy_steward_shadow": "off",
            "gate_policy_dor_max_class": "",
            "gate_policy_risk_map": "",
            "gate_policy_review_limit": "",
            "gate_policy_review_limit_mode": "enforce",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error" not in resp.headers.get("location", ""), resp.headers
    assert await _policy_of(client, pid) == {
        "dor": "human",
        "verdict": "human",
        "steward_shadow": False,
        "ci_runner": "make test",
    }


async def test_project_form_saves_local_review_fallback(client: AsyncClient):
    """AC-8 (#1653): on/off сохранены, нет поля — ключ цел, пусто — снят, мусор — отказ."""
    from urllib.parse import unquote_plus

    other = {"ci_runner": "make test", "wip_limit": 2, "deep_reviewer": "local"}
    pid = await _project_with_policy(client, "lrf-form", dict(other))
    page = (await client.get("/projects")).text
    block = _cost_form_block(page, pid)
    assert 'name="gate_policy_local_review_fallback"' in block
    assert "off" in _now_text(block, "local_review_fallback")

    async def _post(data):
        return await client.post(
            f"/projects/{pid}/web-edit", data=data, follow_redirects=False
        )

    resp = await _post(
        {"gate_policy_local_review_fallback": "on", "gate_policy_deep_daily_cap": "3"}
    )
    assert "project_error" not in resp.headers.get("location", ""), resp.headers
    policy = await _policy_of(client, pid)
    assert policy["local_review_fallback"] == "on" and policy["deep_daily_cap"] == 3
    assert {k: policy[k] for k in other} == other, "соседние настройки не потеряны"
    page = (await client.get("/projects")).text
    assert "on (project)" in _now_text(
        _cost_form_block(page, pid), "local_review_fallback"
    )
    assert re.search(r'<option value="on"\s+selected', _cost_form_block(page, pid)), (
        "выбор отражает сохранённое"
    )

    resp = await _post({"gate_policy_local_review_fallback": "off"})
    assert (await _policy_of(client, pid))["local_review_fallback"] == "off"

    await _post({"gate_policy_local_review_fallback": "on"})
    resp = await _post({"gate_policy_review": "off"})  # форма без поля
    assert "project_error" not in resp.headers.get("location", ""), resp.headers
    assert (await _policy_of(client, pid))["local_review_fallback"] == "on"

    before = await _policy_of(client, pid)
    resp = await _post({"gate_policy_local_review_fallback": "maybe"})
    assert "project_error" in unquote_plus(resp.headers.get("location", ""))
    assert await _policy_of(client, pid) == before, "отказ не меняет политику"

    resp = await _post({"gate_policy_local_review_fallback": ""})  # пустой выбор
    assert "project_error" not in resp.headers.get("location", ""), resp.headers
    policy = await _policy_of(client, pid)
    assert "local_review_fallback" not in policy
    assert {k: policy[k] for k in other} == other


async def test_project_form_local_review_fallback_keeps_the_full_form_neighbours(
    client: AsyncClient,
):
    """#1653 P3: полная форма — review, verdict, release и соседние ключи не теряются."""
    other = {"ci_runner": "make test", "deep_reviewer": "local", "wip_limit": 2}
    pid = await _project_with_policy(client, "lrf-full", dict(other))
    full = {
        "gate_policy_dor": "human",
        "gate_policy_verdict": "human",
        "gate_policy_review": "dispatch",
        "gate_policy_release": "auto",
        "gate_policy_deep_reviewer": "local",
    }
    for value in ("on", "off", ""):
        resp = await client.post(
            f"/projects/{pid}/web-edit",
            data={**full, "gate_policy_local_review_fallback": value},
            follow_redirects=False,
        )
        assert "project_error" not in resp.headers.get("location", ""), resp.headers
        policy = await _policy_of(client, pid)
        assert policy.get("local_review_fallback") == (value or None), policy
        assert policy["review"] == "dispatch" and policy["release"] == "auto"
        assert policy["verdict"] == "human" and policy["dor"] == "human"
        assert policy["deep_reviewer"] == "local"
        assert {k: policy[k] for k in ("ci_runner", "wip_limit")} == {
            "ci_runner": "make test",
            "wip_limit": 2,
        }
