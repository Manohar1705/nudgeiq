"""Customer state: what the agent 'sees' when it decides."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Channel


class CustomerState(BaseModel):
    """Snapshot of one customer at decision time.

    Fixed fields are the ones guardrails always need. Everything else the
    business selects on the 'Context' screen lives in `signals`, so a new
    use case needs no code change.
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: str
    cohort_id: str
    cart_value: float = Field(ge=0)
    abandoned_minutes: int = Field(ge=0)
    # Minutes the T+ timings are measured from. None = since abandonment (first round).
    # The orchestrator sets 0 on a retry, so "T+1h" means "1 hour from now".
    timing_anchor_minutes: int | None = Field(default=None, ge=0)
    contactable: bool = True
    dnd: bool = False
    has_app: bool = True
    sms_opt_in: bool = True
    email_opt_in: bool = True
    preferred_channel: Channel | None = None
    contacts_in_window: int = Field(default=0, ge=0)
    last_contact_at: datetime | None = None
    is_holdout: bool = False
    signals: dict[str, Any] = Field(default_factory=dict)
