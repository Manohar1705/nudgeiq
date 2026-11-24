"""Offer agent: which offers and recovery levers fit this customer?"""

from app.agents.eligibility import is_eligible
from app.models import CustomerState, UseCaseConfig


def propose_offers(state: CustomerState, config: UseCaseConfig) -> list[str]:
    return [o for o in config.action_space.offers if is_eligible(o, state, config)]


def propose_levers(state: CustomerState, config: UseCaseConfig) -> list[str]:
    return [lv for lv in config.action_space.levers if is_eligible(lv, state, config)]
