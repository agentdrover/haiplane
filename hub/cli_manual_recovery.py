"""Human attestation and snapshot for manual-claim recovery."""

import argparse
from typing import Any


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--manual-recover",
        action="store_true",
        help="Recover a manual claim after owner verification",
    )
    parser.add_argument("--expected-holder", default="")
    parser.add_argument("--expected-session", default="")
    parser.add_argument("--expected-claimed-at", default="")
    parser.add_argument("--expected-last-update-id", type=int, required=False)
    parser.add_argument("--confirmed-process-stopped", action="store_true")
    parser.add_argument(
        "--verification", default="", help="How the owner checked the process stopped"
    )


def recovery_body(args: argparse.Namespace) -> dict[str, Any]:
    if not getattr(args, "manual_recover", False):
        return {}
    return {
        "manual_claim_recovery": {
            "expected_status": "running",
            "expected_holder": getattr(args, "expected_holder", ""),
            "expected_session": getattr(args, "expected_session", ""),
            "expected_claimed_at": getattr(args, "expected_claimed_at", ""),
            "expected_last_update_id": getattr(args, "expected_last_update_id", None),
            "confirmed_process_stopped": getattr(
                args, "confirmed_process_stopped", False
            ),
            "verification": getattr(args, "verification", ""),
        }
    }
