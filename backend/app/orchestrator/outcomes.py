"""Apply one outcome: reward, learning, unsubscribe and journey (8d-3b, step 2).

This is the part of `Orchestrator.record_outcome` that does the work, moved out so that the API
(through the orchestrator) and a background worker run exactly the same code. It needs the
repository and the campaign's config, nothing else: no message library, no decision engine.

Rules this code keeps:

  * Only outcomes that COUNT are read: 'pending' and 'processed', in one read. A 'failed'
    outcome is junk (it could never be processed) and must not change a reward or close a
    journey. 'pending' counts because the outcome being applied is itself still pending, and so
    are its neighbours that arrived together with it.
  * Applying is safe to repeat. The reward is recomputed from the stored outcomes, the policy
    is credited through `repo.credit_policy` (which remembers what each decision already added
    and only adds the difference), and the unsubscribe and the journey close are overwrites.
  * The reward is settled. Another outcome for the same decision can be stored, and processed
    by another worker, between our read and our write. After saving we read again; if the reward
    has moved we save the newer one, until it stops moving. Without this a slower worker could
    leave the older, lower reward stored.

It does NOT change the outcome's status. The caller does that after this returns: the status
changes to 'processed' only when all of the work is done.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.agents.guardrail import GuardrailAgent
from app.agents.master import bucket_for
from app.agents.reward import compute_reward
from app.models import (
    CustomerState,
    Decision,
    Journey,
    JourneyStatus,
    Outcome,
    OutcomeResult,
    OutcomeStatus,
    OutcomeType,
    RewardRecord,
    UseCaseConfig,
)
from app.repository.base import Repository

log = logging.getLogger("nudgeiq")

# The statuses that count for rewards, the journey and (see analytics) the numbers shown.
COUNTED_STATUSES: tuple[OutcomeStatus, ...] = ("pending", "processed")

# How often the reward is re-read after saving. Each extra round needs yet another outcome for the
# same decision to arrive in the few milliseconds in between, so more than a couple is unheard of.
MAX_SETTLE_ROUNDS = 5

PolicyListener = Callable[[dict[str, Any], int], None]


def journey_for(repo: Repository, state: CustomerState, now: datetime) -> Journey:
    """The customer's journey, started now if there is none."""
    journey = repo.get_journey(state.customer_id)
    if journey is None:
        journey = Journey(
            customer_id=state.customer_id,
            started_at=now,
            base_abandoned_minutes=state.abandoned_minutes,
        )
        repo.save_journey(journey)
    return journey


def minutes_since_abandonment(journey: Journey, now: datetime) -> int:
    elapsed = max(0, int((now - journey.started_at).total_seconds() // 60))
    return journey.base_abandoned_minutes + elapsed


class OutcomeProcessor:
    def __init__(
        self,
        repo: Repository,
        config: UseCaseConfig,
        on_policy: PolicyListener | None = None,
    ) -> None:
        self.repo = repo
        self.config = config
        self.guardrail = GuardrailAgent(config.guardrails)
        # Called with (policy, version) after each credit, so a live Orchestrator can adopt the
        # policy it just changed. A worker has no master to update and passes nothing.
        self._on_policy = on_policy

    # ----------------------------------------------------------------- the work
    def apply(self, decision: Decision, outcome: Outcome, now: datetime) -> OutcomeResult:
        """Reward, learn, unsubscribe and update the journey for `outcome`, which is already
        stored. `now` is the time the journey rules see (the sync path and the worker both
        pass the time the customer acted, so a late worker does not end a journey early)."""
        learnable = not decision.chosen.is_wait and not decision.is_holdout
        reward = self._settle_reward(decision, learnable)

        if outcome.outcome_type == OutcomeType.UNSUBSCRIBED:
            customer = self.repo.get_customer(decision.customer_id)
            if customer is not None:
                self.repo.save_customer(customer.model_copy(update={"contactable": False}))

        journey = self._update_journey(decision, now)
        if journey.status != JourneyStatus.ACTIVE:
            next_step = "closed"
        elif outcome.outcome_type == OutcomeType.IGNORED:
            next_step = "retry"
        else:
            next_step = "wait_for_outcome"
        return OutcomeResult(
            decision_id=decision.decision_id,
            outcome_type=outcome.outcome_type,
            reward=reward,
            policy_updated=learnable,
            journey=journey,
            next_step=next_step,
        )

    # ------------------------------------------------------------------ reward
    def _reward(self, decision: Decision) -> RewardRecord:
        """From all outcomes that count for this decision. A timeout ('ignored') only counts
        when nothing else happened, so a late timeout never cancels real engagement."""
        records = self.repo.list_outcome_records(
            decision_id=decision.decision_id, statuses=COUNTED_STATUSES
        )
        seen = [r.outcome.outcome_type for r in records]
        counted = [t for t in seen if t != OutcomeType.IGNORED] or seen
        return compute_reward(
            decision.decision_id, decision.customer_id, counted, self.config.rewards
        )

    def _settle_reward(self, decision: Decision, learnable: bool) -> RewardRecord:
        reward = self._reward(decision)
        for _ in range(MAX_SETTLE_ROUNDS):
            self._store_reward(decision, reward, learnable)
            latest = self._reward(decision)
            if latest == reward:
                return reward
            reward = latest  # another outcome arrived while we worked: store the newer reward
        self._store_reward(decision, reward, learnable)
        log.warning(
            "reward for decision %s was still changing after %d rounds; the next outcome for it "
            "settles it",
            decision.decision_id,
            MAX_SETTLE_ROUNDS,
        )
        return reward

    def _store_reward(self, decision: Decision, reward: RewardRecord, learnable: bool) -> None:
        # Learn once per decision, then correct by the difference as more outcomes arrive. The
        # repository does this under a lock and remembers what this decision already added, so a
        # repeated outcome, or another process learning at the same moment, cannot double-count
        # or overwrite. The policy it hands back includes what everyone has learned so far.
        if learnable:
            previous = self.repo.get_reward(decision.decision_id)
            credit = self.repo.credit_policy(
                self.config.use_case_id,
                decision.decision_id,
                decision.bucket or bucket_for(decision.state),
                decision.chosen.key,
                reward.points,
                legacy_points=previous.points if previous else None,
            )
            if self._on_policy is not None:
                self._on_policy(credit.policy, credit.version)
        self.repo.save_reward(reward)

    # ----------------------------------------------------------------- journey
    def _update_journey(self, decision: Decision, now: datetime) -> Journey:
        journey = self.repo.get_journey(decision.customer_id) or journey_for(
            self.repo, decision.state, now
        )
        if journey.status != JourneyStatus.ACTIVE:
            return journey
        in_journey = {
            d.decision_id
            for d in self.repo.list_decisions(decision.customer_id, limit=None)
            if d.created_at >= journey.started_at
        }
        types = [
            r.outcome.outcome_type
            for r in self.repo.list_outcome_records(
                customer_id=decision.customer_id, statuses=COUNTED_STATUSES
            )
            if r.outcome.decision_id in in_journey
        ]
        days = minutes_since_abandonment(journey, now) / 1440
        verdict = self.guardrail.check_exit(types, days)
        if not verdict.exit:
            return journey
        status = (
            JourneyStatus.CONVERTED if OutcomeType.ORDER_PLACED in types else JourneyStatus.EXITED
        )
        closed = journey.model_copy(
            update={"status": status, "closed_reason": verdict.reason, "closed_at": now}
        )
        self.repo.save_journey(closed)
        return closed
