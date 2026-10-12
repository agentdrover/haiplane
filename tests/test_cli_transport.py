"""CLI: HTTP-транспорт _api и печать HTTP-ошибок."""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from hub import cli
from tests.cli_support import _assert_no_traceback, _FakeResponse


def test_api_rejects_non_http_scheme(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "HUB_URL", "file:///etc/passwd")
    monkeypatch.setattr(cli, "HUB_TOKEN", "")
    with pytest.raises(SystemExit) as exc:
        cli._api("GET", "/api/tasks")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "HAIPLANE_HUB_URL" in err
    assert ("OPEN" + "CLAW") not in err, "Wave 5: no legacy name in errors"
    assert "must use http/https" in err


def test_api_uses_validated_base_url_for_requests(monkeypatch) -> None:
    monkeypatch.setattr(cli, "HUB_URL", "https://hub.example.test")
    monkeypatch.setattr(cli, "HUB_TOKEN", "")
    mock_urlopen = MagicMock(return_value=_FakeResponse(b'{"ok": true}'))
    with patch("urllib.request.urlopen", mock_urlopen):
        result = cli._api("GET", "/api/tasks")
    assert result == {"ok": True}
    request = mock_urlopen.call_args.args[0]
    assert request.full_url == "https://hub.example.test/api/tasks"


def test_print_http_error_pretty_prints_dor_failed_detail(capsys) -> None:
    body = json.dumps(
        {
            "detail": {
                "error": "dor_failed",
                "task_id": 12,
                "score": 40,
                "missing_required": ["has_problem_statement"],
                "recommendations": [
                    {
                        "field": "problem_statement",
                        "severity": "blocking",
                        "message": "Describe the problem",
                    }
                ],
                "hint": "pass force=true to override the DoR gate",
            }
        }
    )
    cli._print_http_error(422, body)
    err = capsys.readouterr().err
    assert "DoR failed" in err
    assert "score=40" in err
    assert "has_problem_statement" in err
    assert "Describe the problem" in err
    assert "force=true" in err


def test_api_sends_bearer_token(monkeypatch) -> None:
    monkeypatch.setattr(cli, "HUB_URL", "https://hub.example.test")
    monkeypatch.setattr(cli, "HUB_TOKEN", "tok-1")
    mock_urlopen = MagicMock(return_value=_FakeResponse(b'{"ok": true}'))
    with patch("urllib.request.urlopen", mock_urlopen):
        cli._api(
            "POST",
            "/api/tasks",
            {"title": "x"},
            extra_headers={"X-Client-Request-Id": "req-1"},
        )
    request = mock_urlopen.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer tok-1"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("X-client-request-id") == "req-1"


def test_api_http_error_exits_1_without_traceback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "HUB_URL", "https://hub.example.test")
    monkeypatch.setattr(cli, "HUB_TOKEN", "")

    def _boom(req, timeout=30):
        raise urllib.error.HTTPError(
            req.full_url,
            409,
            "Conflict",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(b'{"reason":"already_claimed"}'),
        )

    with patch("urllib.request.urlopen", _boom), pytest.raises(SystemExit) as exc:
        cli._api("POST", "/api/tasks/1/claim", {"agent": "x"})
    assert exc.value.code == 1
    _, err = _assert_no_traceback(capsys)
    assert "HTTP 409" in err
    assert "already_claimed" in err


def test_api_connection_error_exits_1_without_traceback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "HUB_URL", "https://hub.example.test")
    monkeypatch.setattr(cli, "HUB_TOKEN", "")

    def _boom(req, timeout=30):
        raise urllib.error.URLError("refused")

    with patch("urllib.request.urlopen", _boom), pytest.raises(SystemExit) as exc:
        cli._api("GET", "/api/tasks")
    assert exc.value.code == 1
    _, err = _assert_no_traceback(capsys)
    assert "Connection error" in err


def test_print_http_error_plain_and_generic_json(capsys) -> None:
    cli._print_http_error(500, "plain boom")
    assert "HTTP 500: plain boom" in capsys.readouterr().err
    cli._print_http_error(409, '{"reason":"busy"}')
    assert "HTTP 409" in capsys.readouterr().err
