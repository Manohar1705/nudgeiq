"""Monitor/Reward agent: turns customer outcomes into reward points."""

from collections.abc import Iterable

from app.models import OutcomeType, RewardConfig, RewardRecord


def compute_reward(
    decision_id: str,
    customer_id: str,
    outcomes: Iterable[OutcomeType],
    config: RewardConfig,
) -> RewardRecord:
    """Sum the configured points for every distinct outcome that happened.

    Positive outcomes stack (open + click + restart + order) because the demo
    uses interim 'shaping' rewards that guide the policy toward the final order.
    """
    seen: list[OutcomeType] = []
    for outcome in outcomes:
        if outcome not in seen:
            seen.append(outcome)
    total = sum(config.reward_for(o) for o in seen)
    reason = ", ".join(f"{o.value} ({config.reward_for(o):+d})" for o in seen) or "no response"
    return RewardRecord(
        decision_id=decision_id, customer_id=customer_id, points=total, reason=reason
    )
