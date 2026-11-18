from hub import repository as repo
from hub.integrations.registry import plugins


async def test_passport_readable_safe_and_read_only(client, db, monkeypatch):
    from hub.services.agent_passport import DECLARATION_PATH

    await repo.create_project(
        db,
        slug="unsafe-passport",
        name="Unsafe passport",
        workspace_path="/srv/SERVER-CANARY",
    )
    await db.commit()
    reads = []

    async def read(workspace, branch, path, **kwargs):
        reads.append(path)
        return {
            "state": "unreadable",
            "reason": '<script id="PASSPORT-CANARY">alert(1)</script>/srv/SERVER-CANARY',
        }

    monkeypatch.setattr(plugins.git_ops, "read_file_at_ref", read)
    before = db.total_changes
    page = await client.get("/projects")
    assert page.status_code == 200 and "Паспорт агента" in page.text
    assert DECLARATION_PATH not in reads
    for headers in ({}, {"HX-Request": "true"}):
        rendered = await client.get(
            "/projects/unsafe-passport/agent-passport", headers=headers
        )
        assert rendered.status_code == 200
        assert "Источник декларации недоступен" in rendered.text
        assert "unknown" in rendered.text and DECLARATION_PATH in rendered.text
        assert "PASSPORT-CANARY" not in rendered.text
        assert "SERVER-CANARY" not in rendered.text
        assert "<summary>Данные и источники</summary>" in rendered.text
    assert reads.count(DECLARATION_PATH) == 2
    assert db.total_changes == before


async def test_passport_displays_stale_declaration(client, db, monkeypatch):
    await repo.create_project(
        db, slug="stale-passport", name="Stale", workspace_path="/git"
    )
    await db.commit()

    async def read(*args, **kwargs):
        return {
            "state": "present",
            "content": '{"schema_version":"1","review_by":"2000-01-01","roles":[{"role":"infrastructure"}]}',
        }

    monkeypatch.setattr(plugins.git_ops, "read_file_at_ref", read)
    response = await client.get("/projects/stale-passport/agent-passport")
    assert response.status_code == 200
    assert "планируется" in response.text
    assert "Срок проверки декларации истёк" in response.text
