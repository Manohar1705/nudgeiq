"""Builds the candidate action list from the use-case config."""

from itertools import product

from app.models import Action, UseCaseConfig


def build_action_list(config: UseCaseConfig) -> list[Action]:
    """Every lever x channel x offer x timing combination (the 'menu').

    'wait' is not included here: guardrails (Zip 3) decide when waiting is forced.
    """
    space = config.action_space
    return [
        Action(channel=channel, timing=timing, offer=offer, lever=lever)
        for lever, channel, offer, timing in product(
            space.levers, space.channels, space.offers, space.timings
        )
    ]
