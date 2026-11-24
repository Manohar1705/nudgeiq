"""Basic uplift: do nudged customers convert more than the holdout control group?

Rates use decisions that have at least one outcome recorded ('resolved'), so decisions
still waiting for a customer response do not count as failures. Breakdowns by channel,
timing, offer, lever, cohort and variant (with confidence intervals) live in analytics.py.
"""

from pydantic import BaseModel

from app.models import OutcomeType
from app.repository.base import Repository


class GroupStats(BaseModel):
    decisions: int = 0
    resolved: int = 0
    orders: int = 0
    conversion: float = 0.0
    avg_reward: float = 0.0


class UpliftSummary(BaseModel):
    treated: GroupStats  # a nudge was chosen
    holdout: GroupStats  # control: never nudged
    waited: int  # eligible but no legal or useful action (not part of the comparison)
    uplift: float | None  # (treated conversion - holdout conversion) / holdout conversion


def _finish(g: GroupStats, reward_total: float) -> GroupStats:
    g.conversion = g.orders / g.resolved if g.resolved else 0.0
    g.avg_reward = reward_total / g.resolved if g.resolved else 0.0
    return g


def uplift_summary(repo: Repository, campaign_id: str | None = None) -> UpliftSummary:
    """Pooled uplift. With campaign_id, only decisions made by that campaign count."""
    outcomes_by_decision: dict[str, set[OutcomeType]] = {}
    # only processed outcomes: a pending one has no reward yet and a failed one never will
    for o in repo.list_outcomes(status="processed"):
        outcomes_by_decision.setdefault(o.decision_id, set()).add(o.outcome_type)

    treated, holdout = GroupStats(), GroupStats()
    totals = {"treated": 0.0, "holdout": 0.0}
    waited = 0
    for d in repo.list_decisions(limit=None):
        if campaign_id is not None and d.use_case_id != campaign_id:
            continue
        if d.is_holdout:
            group, key = holdout, "holdout"
        elif not d.chosen.is_wait:
            group, key = treated, "treated"
        else:
            waited += 1
            continue
        group.decisions += 1
        seen = outcomes_by_decision.get(d.decision_id)
        if not seen:
            continue
        group.resolved += 1
        group.orders += OutcomeType.ORDER_PLACED in seen
        reward = repo.get_reward(d.decision_id)
        totals[key] += reward.points if reward else 0

    _finish(treated, totals["treated"])
    _finish(holdout, totals["holdout"])
    uplift = (
        (treated.conversion - holdout.conversion) / holdout.conversion
        if holdout.conversion > 0
        else None
    )
    return UpliftSummary(treated=treated, holdout=holdout, waited=waited, uplift=uplift)
