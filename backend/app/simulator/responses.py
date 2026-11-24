"""Fake customer behaviour: the hidden 'truth' the agent has to discover.

Each customer reacts to a nudge through a funnel:
    open -> click -> checkout restart -> order        (plus a small unsubscribe risk)
The probabilities depend on the customer and the action (channel match, timing,
offer, lever). The agent never sees these rules; it only sees outcomes.
"""

import random
from dataclasses import dataclass

from app.models import Action, CustomerState, OutcomeType, RewardConfig

BASE_OPEN = {"push": 0.45, "email": 0.30, "sms": 0.55}
UNSUB = {"push": 0.005, "email": 0.01, "sms": 0.03}
NATURAL_ORDER = 0.04  # customers who come back with no nudge at all


def _clip(p: float) -> float:
    return max(0.0, min(0.95, p))


@dataclass(frozen=True)
class FunnelProbs:
    open: float
    click: float
    restart: float
    order: float
    unsub: float
    natural: float

    def expected_reward(self, rewards: RewardConfig) -> float:
        o = self.open
        oc = o * self.click
        ocr = oc * self.restart
        funnel_order = ocr * self.order
        any_order = 1 - (1 - funnel_order) * (1 - self.natural)
        return (
            o * rewards.reward_for(OutcomeType.MESSAGE_OPENED)
            + oc * rewards.reward_for(OutcomeType.CTA_CLICKED)
            + ocr * rewards.reward_for(OutcomeType.CHECKOUT_RESTARTED)
            + any_order * rewards.reward_for(OutcomeType.ORDER_PLACED)
            + self.unsub * rewards.reward_for(OutcomeType.UNSUBSCRIBED)
        )


class ResponseModel:
    def natural_order_prob(self, state: CustomerState) -> float:
        member = state.signals.get("loyalty_tier", "none") != "none"
        return NATURAL_ORDER + (0.02 if member else 0.0)

    def probabilities(self, state: CustomerState, action: Action) -> FunnelProbs:
        natural = self.natural_order_prob(state)
        if action.is_wait or action.channel is None:
            return FunnelProbs(0, 0, 0, 0, 0, natural)

        channel = action.channel.value
        member = state.signals.get("loyalty_tier", "none") != "none"
        high_cart = state.cart_value >= 100

        p_open = BASE_OPEN.get(channel, 0.3)
        p_open *= 1.6 if state.preferred_channel == action.channel else 0.8
        if action.timing == "T+1h":
            p_open *= 0.8 if high_cart else 1.3
        elif action.timing == "T+24h":
            p_open *= 1.3 if high_cart else 0.8

        p_click = 0.35
        if member:
            p_click *= {"bonus_points": 1.6, "free_shipping": 1.0, "percent_off": 0.9}.get(
                action.offer or "", 1.0
            )
        else:
            p_click *= {"bonus_points": 0.6, "free_shipping": 1.4, "percent_off": 1.2}.get(
                action.offer or "", 1.0
            )
        if high_cart and action.offer == "percent_off":
            p_click *= 1.3
        if action.lever == "card_pitch":
            p_click *= 1.5 if state.signals.get("card_holder") else 0.6
        elif action.lever == "switch_to_pickup":
            p_click *= 1.5 if state.signals.get("fulfillment") == "pickup" else 0.8
        elif action.lever == "loyalty_bonus":
            p_click *= 1.3 if member else 0.7

        return FunnelProbs(
            open=_clip(p_open),
            click=_clip(p_click),
            restart=0.5,
            order=0.6,
            unsub=UNSUB.get(channel, 0.01),
            natural=natural,
        )

    def expected_reward(self, state: CustomerState, action: Action, rewards: RewardConfig) -> float:
        return self.probabilities(state, action).expected_reward(rewards)

    def sample_outcomes(
        self, state: CustomerState, action: Action, rng: random.Random
    ) -> list[OutcomeType]:
        p = self.probabilities(state, action)
        outcomes: list[OutcomeType] = []
        funnel_order = False
        if rng.random() < p.open:
            outcomes.append(OutcomeType.MESSAGE_OPENED)
            if rng.random() < p.click:
                outcomes.append(OutcomeType.CTA_CLICKED)
                if rng.random() < p.restart:
                    outcomes.append(OutcomeType.CHECKOUT_RESTARTED)
                    funnel_order = rng.random() < p.order
        if rng.random() < p.natural or funnel_order:
            outcomes.append(OutcomeType.ORDER_PLACED)
        if rng.random() < p.unsub:
            outcomes.append(OutcomeType.UNSUBSCRIBED)
        return outcomes
