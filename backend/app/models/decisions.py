"""The decision record: the 'why' behind every nudge (powers explainability)."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.actions import Action
from app.models.customer import CustomerState


def _now() -> datetime:
    return datetime.now(UTC)


class ScoredAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action
    score: float


class DecisionStep(BaseModel):
    """One agent's contribution to the trace."""

    model_config = ConfigDict(extra="forbid")
    agent: str
    note: str


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: str
    customer_id: str
    created_at: datetime = Field(default_factory=_now)
    state: CustomerState
    candidates: list[ScoredAction] = Field(default_factory=list)
    blocked: list[str] = Field(default_factory=list)  # action keys removed by guardrails
    chosen: Action = Field(default_factory=Action.wait)
    explored: bool = False
    is_holdout: bool = False
    bucket: str = ""  # segment the Master learned under (needed to apply rewards later)
    scheduled_for: datetime | None = None  # when the nudge goes out (None = wait)
    variant_id: str | None = None
    message: str | None = None
    # which campaign (and which version of it) made this decision, and which template was used
    use_case_id: str | None = None
    campaign_version: int | None = Field(default=None, ge=1)
    template_id: str | None = None
    steps: list[DecisionStep] = Field(default_factory=list)

    def log(self, agent: str, note: str) -> None:
        self.steps.append(DecisionStep(agent=agent, note=note))


class RewardRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: str
    customer_id: str
    points: int
    reason: str
