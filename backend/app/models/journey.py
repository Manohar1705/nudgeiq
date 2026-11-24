"""Journey = one customer's run through the flow after a trigger (e.g. a cart abandonment)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.decisions import RewardRecord
from app.models.enums import JourneyStatus, OutcomeType


class Journey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str
    status: JourneyStatus = JourneyStatus.ACTIVE
    started_at: datetime
    base_abandoned_minutes: int = Field(ge=0)  # minutes since abandonment at started_at
    nudges_sent: int = Field(default=0, ge=0)  # > 0 means the next decision is a retry round
    closed_reason: str | None = None
    closed_at: datetime | None = None


class OutcomeResult(BaseModel):
    """What record_outcome() returns: the reward, whether the policy learned, the journey."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    outcome_type: OutcomeType
    reward: RewardRecord
    policy_updated: bool
    journey: Journey
    # closed = journey over | retry = call decide again | wait_for_outcome = nothing to do yet
    next_step: Literal["closed", "retry", "wait_for_outcome"]
