"""Server and K8 adapter contract for role deliveries."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

Identifier = Annotated[
    str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
]
MAX_SQL_INT = 2**63 - 1
DeliveryState = Literal["pending", "sent", "unknown", "failed"]


class OutboxScope(BaseModel):
    project: str
    role: str
    task_id: int = Field(gt=0, le=MAX_SQL_INT)
    generation: int = Field(ge=0, le=MAX_SQL_INT)
    run: int = Field(gt=0, le=MAX_SQL_INT)


class OutboxPublish(OutboxScope):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=512)
    role: str = Field(min_length=1, max_length=32)
    lease_id: SecretStr
    delivery_key: Identifier
    continuation: Identifier | None = None
    kind: Literal["result", "question", "silent"]
    body: str = Field(default="", max_length=4000)


class OutboxAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=4000)


class OutboxAck(BaseModel):
    id: int
    accepted_state: Literal["pending", "sent"]


class OutboxStatus(BaseModel):
    id: int
    state: DeliveryState
    reason: str
    message_id: int | None
    answer_message_id: int | None
    answer_event_id: int | None
    continuation: str | None
    answer: str | None
    continuation_state: Literal[
        "none", "queued", "waiting", "answered", "stale", "undeliverable"
    ]
    continuation_reason: str = ""
