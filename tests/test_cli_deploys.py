"""CLI reads the REST contract and reuses the release view formatter."""

from hub import cli
from hub import repository as repo
from hub.services.deploys_view import DeploysView, format_deploys_view


async def test_cli_prints_shared_formatter_of_rest_json(
    client, db, monkeypatch, capsys
):
    await repo.create_project(db, slug="default", name="Default")
    await db.commit()
    response = await client.get(
        "/api/deploys", params={"project": "default", "limit": 3}
    )
    assert response.status_code == 200
    payload = response.json()
    calls = []

    def api(method, path):
        calls.append((method, path))
        return payload

    monkeypatch.setattr(cli, "_api", api)
    parser = cli.build_parser()
    args = parser.parse_args(["deploys", "--project", "default", "--limit", "3"])
    assert args.func(args) == 0
    assert calls == [("GET", "/api/deploys?project=default&limit=3")]
    assert (
        capsys.readouterr().out
        == format_deploys_view(DeploysView.model_validate(payload)) + "\n"
    )
