"""The structured boundary an enforcing pilot runtime must implement.

The current k8 worker and both review clients have no such boundary. Keep
ordinary transports unavailable for paid pilot effects, even with a grant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import aiosqlite

from hub.epic_plan_view import SubscriptionEvidence
from hub.services.epic_plan_authority import SpendingGrant

if TYPE_CHECKING:
    from hub.integrations.cursor_cloud import Reconciliation
    from hub.services.executor_orders import _Booking
    from hub.services.review_dispatch import _Started


class SubscriptionTransport(Protocol):
    def subscription(self) -> SubscriptionEvidence | None:
        """Return current provider-confirmed account terms, or None without proof."""
        ...


class ExecutorTransport(SubscriptionTransport, Protocol):
    async def issue(
        self,
        db: aiosqlite.Connection,
        project: Any,
        booking: _Booking,
        grant: SpendingGrant,
    ) -> tuple[int, int]:
        """Publish only to an enforcing worker using the exact grant SHA/model."""
        ...


@dataclass(frozen=True)
class ReviewRequest:
    marker: str
    repo_url: str
    branch: str
    head_sha: str
    model: str
    prompt: str
    hub_url: str


class ReviewTransport(SubscriptionTransport, Protocol):
    async def lookup(
        self, request: ReviewRequest, grant: SpendingGrant
    ) -> Reconciliation:
        """Reconcile the exact funded operation. Uncertainty means no retry."""
        ...

    async def launch(self, request: ReviewRequest, grant: SpendingGrant) -> _Started:
        """Enforce the structured grant and exact SHA before provider creation."""
        ...


def executor_transport(host: str) -> ExecutorTransport | None:
    """The installed k8 worker does not consume provider-enforced grants."""
    return None


def review_transport() -> ReviewTransport | None:
    """The installed Cursor and local review clients have no metered contract."""
    return None
