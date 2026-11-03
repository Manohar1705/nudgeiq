"""A nudge that has been scheduled for later delivery (step 8d-2)."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Channel

# scheduled -> sending -> sent | failed       (normal path)
# scheduled -> skipped                         (customer no longer eligible at send time)
SendStatus = Literal["scheduled", "sending", "sent", "skipped", "failed"]
SEND_STATUSES: tuple[str, ...] = ("scheduled", "sending", "sent", "skipped", "failed")


def _now() -> datetime:
    return datetime.now(UTC)


class ScheduledSend(BaseModel):
    """One row per decision that sends a nudge. The row is the double-send guard: a send is only
    delivered by whoever moves it from 'scheduled' to 'sending', and that can happen once."""

    model_config = ConfigDict(extra="forbid")
    decision_id: str
    customer_id: str
    send_at: datetime
    channel: Channel
    text: str
    variant_id: str
    template_id: str
    status: SendStatus = "scheduled"
    reason: str = ""  # why it was skipped or failed
    created_at: datetime = Field(default_factory=_now)
    claimed_at: datetime | None = None
    finished_at: datetime | None = None
