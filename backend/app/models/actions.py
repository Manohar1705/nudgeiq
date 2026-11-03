"""The action space: what the Master agent can choose between."""

from pydantic import BaseModel, ConfigDict

from app.models.enums import Channel


class Action(BaseModel):
    """One candidate action = channel x timing x offer (x lever).

    A 'wait' action has channel=None.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: Channel | None = None
    timing: str | None = None  # e.g. "T+1h", "T+24h"
    offer: str | None = None  # e.g. "free_shipping"
    lever: str | None = None  # e.g. "loyalty_bonus"

    @property
    def is_wait(self) -> bool:
        return self.channel is None

    @property
    def key(self) -> str:
        """Stable id used by the policy and the logs."""
        if self.is_wait:
            return "wait"
        return "|".join(str(p) for p in (self.channel, self.timing, self.offer, self.lever))

    @classmethod
    def wait(cls) -> "Action":
        return cls()
