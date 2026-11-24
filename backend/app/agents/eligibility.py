"""Eligibility rules: which options a given customer may be offered.

The config maps an option name to a rule name (e.g. {"sms": "sms_opt_in"}).
The rules themselves live here, so config stays data and code stays small.
"""

from collections.abc import Callable

from app.models import CustomerState, UseCaseConfig

Predicate = Callable[[CustomerState], bool]

PREDICATES: dict[str, Predicate] = {
    "has_app": lambda s: s.has_app,
    "sms_opt_in": lambda s: s.sms_opt_in,
    "email_opt_in": lambda s: s.email_opt_in,
    "loyalty_member": lambda s: s.signals.get("loyalty_tier", "none") != "none",
    "card_holder": lambda s: bool(s.signals.get("card_holder", False)),
    "pickup_available": lambda s: s.signals.get("fulfillment") in {"pickup", "drive_up"},
}


def is_eligible(option: str, state: CustomerState, config: UseCaseConfig) -> bool:
    """True if the option has no rule, or the customer passes its rule.

    Rules that read a context signal fail closed: if the business turned that
    signal off, the agent cannot see it, so the option is treated as ineligible.
    """
    rule = config.action_space.eligibility.get(option)
    if rule is None:
        return True
    predicate = PREDICATES.get(rule)
    if predicate is None:
        raise ValueError(f"unknown eligibility rule '{rule}' for option '{option}'")
    return predicate(state)
