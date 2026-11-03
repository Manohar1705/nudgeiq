"""Per-message funnel and guardrail counts (the data behind /insights/messages and /guardrails).

Both read the same decisions the uplift numbers use:
  * treated = a nudge was chosen and the decision is not holdout
  * holdout = deliberately not messaged
Every decision counts once per outcome type, so no rate can exceed 100%.
"""

from dataclasses import dataclass

from pydantic import BaseModel

from app.models import Channel, OutcomeType
from app.repository.base import Repository


class MessageFunnelRow(BaseModel):
    variant_id: str
    text: str
    channel: str | None
    sent: int
    opened: int
    clicked: int
    orders: int
    avg_reward: float


class GuardrailGroup(BaseModel):
    sent: int = 0
    unsubscribed: int = 0
    refunded: int = 0
    app_opened: int = 0


class GuardrailSummary(BaseModel):
    treated: GuardrailGroup
    holdout: GuardrailGroup


def _outcomes_by_decision(repo: Repository) -> dict[str, set[OutcomeType]]:
    seen: dict[str, set[OutcomeType]] = {}
    # only processed outcomes: a pending one has no reward yet and a failed one never will
    for o in repo.list_outcomes(status="processed"):
        seen.setdefault(o.decision_id, set()).add(o.outcome_type)
    return seen


@dataclass
class _Tally:
    text: str | None
    channel: Channel | None
    sent: int = 0
    opened: int = 0
    clicked: int = 0
    orders: int = 0
    reward: int = 0


def message_funnel(repo: Repository, campaign_id: str | None = None) -> list[MessageFunnelRow]:
    """One row per message variant sent at least once, most sent first."""
    seen = _outcomes_by_decision(repo)
    tallies: dict[tuple[str | None, str], _Tally] = {}
    for d in repo.list_decisions(limit=None):
        if campaign_id is not None and d.use_case_id != campaign_id:
            continue
        if d.is_holdout or d.chosen.is_wait or d.variant_id is None:
            continue
        t = tallies.setdefault(
            (d.use_case_id, d.variant_id), _Tally(text=d.message, channel=d.chosen.channel)
        )
        got = seen.get(d.decision_id, set())
        t.sent += 1
        t.opened += OutcomeType.MESSAGE_OPENED in got
        t.clicked += OutcomeType.CTA_CLICKED in got
        t.orders += OutcomeType.ORDER_PLACED in got
        reward = repo.get_reward(d.decision_id)
        t.reward += reward.points if reward else 0

    out: list[MessageFunnelRow] = []
    for (use_case_id, variant_id), t in tallies.items():
        variant = repo.get_variant(use_case_id, variant_id) if use_case_id else None
        out.append(
            MessageFunnelRow(
                variant_id=variant_id,
                text=variant.text if variant else (t.text or variant_id),
                channel=t.channel.value if t.channel else None,
                sent=t.sent,
                opened=t.opened,
                clicked=t.clicked,
                orders=t.orders,
                avg_reward=t.reward / t.sent,
            )
        )
    out.sort(key=lambda m: (-m.sent, m.variant_id))
    return out


def guardrail_summary(repo: Repository, campaign_id: str | None = None) -> GuardrailSummary:
    seen = _outcomes_by_decision(repo)
    treated, holdout = GuardrailGroup(), GuardrailGroup()
    for d in repo.list_decisions(limit=None):
        if campaign_id is not None and d.use_case_id != campaign_id:
            continue
        if d.is_holdout:
            g = holdout
        elif not d.chosen.is_wait:
            g = treated
        else:
            continue
        got = seen.get(d.decision_id, set())
        g.sent += 1
        g.unsubscribed += OutcomeType.UNSUBSCRIBED in got
        g.refunded += OutcomeType.REFUNDED in got
        g.app_opened += OutcomeType.APP_OPENED in got
    return GuardrailSummary(treated=treated, holdout=holdout)
