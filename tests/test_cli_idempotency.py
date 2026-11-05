"""Operation headers for singular task/typed CLI commands, with unchanged JSON."""

import json

import pytest

from tests.cli_support import _run_main

OP = "cli-operation-2241"


@pytest.mark.parametrize(
    "command,args",
    [
        ("task", []),
        ("epic", []),
        ("feature", ["--parent", "1"]),
        ("subtask", ["--parent", "1"]),
    ],
)
def test_create_commands_send_operation_header_and_keep_json(command, args, capsys):
    result = {"id": 42, "title": "CLI"}
    rc, api = _run_main(
        [command, "--title", "CLI", "--operation-id", OP, *args], api_result=result
    )
    assert rc == 0
    assert api.call_args.args[:2] == ("POST", "/api/tasks")
    assert "operation_id" not in api.call_args.args[2]
    assert api.call_args.args[2]["client_request_id"] == OP
    assert api.call_args.kwargs["extra_headers"] == {
        "Idempotency-Key": OP,
        "X-Client-Request-Id": OP,
    }
    assert json.loads(capsys.readouterr().out) == result


def test_task_can_send_both_alias_and_operation(capsys):
    rc, api = _run_main(
        ["task", "--title", "CLI", "--request-id", "req-1", "--operation-id", OP]
    )
    assert rc == 0
    assert api.call_args.args[2]["client_request_id"] == "req-1"
    assert api.call_args.kwargs["extra_headers"] == {
        "Idempotency-Key": OP,
        "X-Client-Request-Id": "req-1",
    }
