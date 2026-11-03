"""Use-case configuration: everything a business user sets in the wizard."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import Channel, OutcomeType
from app.models.timing import parse_timing

_NEGATIVE = {
    OutcomeType.REFUNDED,
    OutcomeType.UNSUBSCRIBED,
    OutcomeType.BOUGHT_ELSEWHERE,
    OutcomeType.IGNORED,
}


class Cohort(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cohort_id: str
    label: str
    size: int = Field(ge=0)


class ContextSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal_id: str
    label: str
    enabled: bool = True
    # keys in CustomerState.signals this context signal provides to the agent
    fields: list[str] = Field(default_factory=list)


class ActionSpace(BaseModel):
    """The menu the Master agent chooses from."""

    model_config = ConfigDict(extra="forbid")
    levers: list[str]
    channels: list[Channel]
    offers: list[str]
    timings: list[str]
    # option name (channel, offer or lever) -> eligibility rule name, e.g. {"sms": "sms_opt_in"}
    eligibility: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _timings(self) -> "ActionSpace":
        for t in self.timings:
            parse_timing(t)  # raises ValueError on a bad value
        return self

    @property
    def size(self) -> int:
        return len(self.levers) * len(self.channels) * len(self.offers) * len(self.timings)


class RewardConfig(BaseModel):
    """Points per outcome. Positive = reward, negative = penalty."""

    model_config = ConfigDict(extra="forbid")
    points: dict[OutcomeType, int]

    @model_validator(mode="after")
    def _signs(self) -> "RewardConfig":
        for outcome, pts in self.points.items():
            if outcome in _NEGATIVE and pts > 0:
                raise ValueError(f"{outcome} must be zero or negative")
            if outcome not in _NEGATIVE and pts < 0:
                raise ValueError(f"{outcome} must be zero or positive")
        return self

    def reward_for(self, outcome: OutcomeType) -> int:
        return self.points.get(outcome, 0)


class Guardrails(BaseModel):
    """Hard rules. The learned policy can never override these."""

    model_config = ConfigDict(extra="forbid")
    max_contacts_per_window: int = Field(ge=0)
    window_days: int = Field(gt=0)
    cooldown_hours: int = Field(ge=0)
    allowed_start_hour: int = Field(ge=0, le=23)
    allowed_end_hour: int = Field(ge=1, le=24)
    allowed_weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    # entry rule
    min_cart_value: float = 0
    min_abandoned_minutes: int = 0
    # exit rule
    exit_after_days: int = Field(default=14, gt=0)

    @model_validator(mode="after")
    def _hours(self) -> "Guardrails":
        if self.allowed_start_hour >= self.allowed_end_hour:
            raise ValueError("allowed_start_hour must be before allowed_end_hour")
        return self


class LearningConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    epsilon: float = Field(default=0.1, ge=0, le=1)  # exploration rate
    holdout_fraction: float = Field(default=0.1, ge=0, lt=1)  # control group
    # How the message variant is picked among the approved ones (5B-9). "uniform" is the
    # default; "epsilon_greedy" favours variants that convert better once they have enough data.
    variant_selection: Literal["uniform", "epsilon_greedy"] = "uniform"
    variant_epsilon: float = Field(default=0.1, ge=0, le=1)
    variant_min_resolved: int = Field(default=30, ge=1)  # data a variant needs before it is judged


class UseCaseConfig(BaseModel):
    """One use case = one config. Change this, not the code."""

    model_config = ConfigDict(extra="forbid")
    use_case_id: str
    trigger_event: str = "cart_abandoned"  # event type that starts a journey
    name: str
    cohorts: list[Cohort]
    context_signals: list[ContextSignal]
    action_space: ActionSpace
    rewards: RewardConfig
    guardrails: Guardrails
    learning: LearningConfig = Field(default_factory=LearningConfig)

    @property
    def active_signals(self) -> list[str]:
        return [s.signal_id for s in self.context_signals if s.enabled]
