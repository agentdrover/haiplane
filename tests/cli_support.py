"""Общие помощники тестов oc-hub (hp-hub) CLI: tests/test_cli*.py (#1676)."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock, patch

from hub import cli


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


# --- main() dispatch (#851) ---------------------------------------------------
#
# Coverage of build_parser only matters as a side effect of invoking a real
# command. These tests go through main() and assert return codes, operator
# messages, or the API call that would change Hub state.
def _run_main(
    argv: list[str],
    *,
    api_result: Any = None,
    api_side_effect: Any = None,
) -> tuple[int, MagicMock]:
    mock_api = MagicMock()
    if api_side_effect is not None:
        mock_api.side_effect = api_side_effect
    else:
        mock_api.return_value = {} if api_result is None else api_result
    with (
        patch.object(sys, "argv", ["oc-hub", *argv]),
        patch.object(cli, "_api", mock_api),
    ):
        try:
            rc = cli.main()
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    return int(rc), mock_api


def _assert_no_traceback(capsys) -> tuple[str, str]:
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Traceback" not in combined
    return captured.out, captured.err
