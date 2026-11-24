"""Channel agent: which channels may we use for this customer?"""

from app.agents.eligibility import is_eligible
from app.models import Channel, CustomerState, UseCaseConfig


def propose_channels(
    state: CustomerState,
    config: UseCaseConfig,
    deliverable: set[Channel] | None = None,
) -> list[Channel]:
    """Configured channels the customer can receive and we can actually deliver.

    `deliverable` is the set of channels that have at least one approved message
    variant; a channel with nothing approved to send is not a real option.
    """
    return [
        ch
        for ch in config.action_space.channels
        if is_eligible(ch.value, state, config) and (deliverable is None or ch in deliverable)
    ]
