"""The image a reader builds is this repo, pinned, not a floating tag (#1336).

Distribution is `docker compose up -d --build` from a clone. A `:latest`
base would make that command rebuild somebody else's image, which is how
the quickstart kept serving the #944 snapshot after the hub had moved on.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_distribution_bases_are_digest_pinned():
    dockerfile = _text("Dockerfile")
    assert ":latest" not in dockerfile
    assert (
        "python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9"
        in dockerfile
    )
    assert (
        "ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc"
        in dockerfile
    )
    assert "UV_LINK_MODE=copy" in dockerfile
    assert "mkdir -p /data" in dockerfile


def test_quickstart_command_matches_the_compose_contract():
    compose = _text("docker-compose.yml")
    assert '"8080:8080"' in compose
    assert 'HAIPLANE_DEMO_SEED: "1"' in compose
    entry = _text("deploy/docker/entrypoint.sh")
    assert "${HAIPLANE_DEMO_SEED:-0}" in entry
    assert "python /app/scripts/demo_seed.py" in entry
    for name in ("README.md", "README.en.md", "hub/templates/landing.html"):
        text = _text(name)
        assert "docker compose up -d --build" in text, name
        assert "localhost:8080" in text, name
