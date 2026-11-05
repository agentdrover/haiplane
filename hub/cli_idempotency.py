"""Create command transport options, shared by task and typed creates."""

from typing import Any


def add_operation_id_option(parser: Any) -> None:
    parser.add_argument("--operation-id", default="", help="Stable operation key")


def create_headers(body: dict[str, Any], args: Any) -> dict[str, str] | None:
    headers = {}
    operation_id = (getattr(args, "operation_id", "") or "").strip()
    # T3 §4.12: without a caller alias the operation id is sent as both, so
    # the hub writes the legacy key too and an old code rollback can replay.
    alias = (getattr(args, "request_id", "") or "").strip() or operation_id
    if alias:
        body["client_request_id"] = alias
        headers["X-Client-Request-Id"] = alias
    if operation_id:
        headers["Idempotency-Key"] = operation_id
    return headers or None
