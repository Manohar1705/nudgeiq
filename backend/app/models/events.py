"""Events in (triggers) and events back (outcomes)."""

from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import OutcomeType


def _now() -> datetime:
    return datetime.now(UTC)


class TriggerEvent(BaseModel):
    """Incoming JSON, e.g. 'cart_abandoned'. Starts the decision flow."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    customer_id: str
    event_type: str
    occurred_at: datetime = Field(default_factory=_now)
    payload: dict[str, Any] = Field(default_factory=dict)


class Outcome(BaseModel):
    """What the customer did after a nudge. Feeds the reward agent."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    customer_id: str
    outcome_type: OutcomeType
    occurred_at: datetime = Field(default_factory=_now)
    payload: dict[str, Any] = Field(default_factory=dict)


# ---- stored outcomes (8d-3b step 1) ----------------------------------------------------------
# pending   -> processed   an outcome is saved the moment it arrives, then rewarded and learned
#                          from (by the worker, from step 2 on)
# pending   -> failed      it can never be processed (bad data); kept for a person to look at
# Outcomes that existed before this step were processed on arrival, so they are 'processed'.
OutcomeStatus = Literal["pending", "processed", "failed"]
OUTCOME_STATUSES: tuple[str, ...] = ("pending", "processed", "failed")


def check_outcome_status(status: str) -> str:
    if status not in OUTCOME_STATUSES:
        raise ValueError(f"outcome status must be one of {OUTCOME_STATUSES}, got {status!r}")
    return status


class OutcomeKeyConflict(ValueError):
    """An idempotency key was reused for a different outcome (another decision, customer or type).
    Quietly returning the first outcome would lose this one, so it is refused."""


class OutcomeRecord(BaseModel):
    """An outcome as stored: the outcome itself plus where it is in being processed."""

    model_config = ConfigDict(extra="forbid")

    outcome_id: int  # the handle a background task is given; never reused
    outcome: Outcome
    status: OutcomeStatus = "processed"
    idempotency_key: str | None = None  # a repeat delivery with the same key is stored once
    received_at: datetime  # when WE got it. `outcome.occurred_at` is when the customer acted
    processed_at: datetime | None = None
    attempts: int = 0  # tries that ended in a retryable error
    last_error: str = ""


class SavedOutcome(NamedTuple):
    """What `save_outcome` did. created=False means this idempotency key was already stored, and
    `record` is the one stored earlier (unchanged)."""

    record: OutcomeRecord
    created: bool
