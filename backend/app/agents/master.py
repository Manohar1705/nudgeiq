"""Master agent: picks the action and learns from rewards.

Zip 2 version = a contextual epsilon-greedy bandit. The state is bucketed, and
each (bucket, action) pair keeps a running reward estimate. Unseen pairs start
optimistic, so every option gets tried a few times before the agent settles.
Later this class is replaced by a DDQN with the same interface.
"""

import random
from dataclasses import dataclass
from typing import Any

from app.models import (
    Action,
    CustomerState,
    Decision,
    ScoredAction,
    cells_to_policy,
    check_reward,
    parse_cells,
)


@dataclass(frozen=True)
class Selection:
    action: Action
    explored: bool
    bucket: str


def bucket_for(state: CustomerState) -> str:
    """Coarse customer segment the bandit learns separately for."""
    channel = state.preferred_channel.value if state.preferred_channel else "none"
    member = "member" if state.signals.get("loyalty_tier", "none") != "none" else "guest"
    cart = "high" if state.cart_value >= 100 else "low"
    return f"{channel}|{member}|{cart}"


class MasterAgent:
    def __init__(
        self,
        epsilon: float = 0.1,
        optimistic_init: float = 12.0,
        prior_weight: float = 2.0,
        seed: int | None = None,
    ) -> None:
        self.epsilon = epsilon
        self.optimistic_init = optimistic_init
        self.prior_weight = prior_weight
        self._rng = random.Random(seed)
        # (bucket, action key) -> (times tried, total reward)
        self._stats: dict[tuple[str, str], tuple[int, float]] = {}

    def score(self, bucket: str, action: Action) -> float:
        n, total = self._stats.get((bucket, action.key), (0, 0.0))
        prior = self.prior_weight
        return (prior * self.optimistic_init + total) / (prior + n)

    def times_tried(self, bucket: str, action: Action) -> int:
        return self._stats.get((bucket, action.key), (0, 0.0))[0]

    def select(self, state: CustomerState, candidates: list[Action]) -> Selection:
        """Fast path used by the simulator and by decide()."""
        bucket = bucket_for(state)
        # Holdout customers are the control group: never nudged, never learned from.
        if state.is_holdout or not candidates:
            return Selection(Action.wait(), False, bucket)
        if self._rng.random() < self.epsilon:
            return Selection(self._rng.choice(candidates), True, bucket)
        scores = [self.score(bucket, a) for a in candidates]
        best = max(scores)
        top = [a for a, s in zip(candidates, scores, strict=True) if s == best]
        return Selection(self._rng.choice(top), False, bucket)

    def decide(self, decision_id: str, state: CustomerState, candidates: list[Action]) -> Decision:
        """Full decision record with every candidate scored (for explainability)."""
        pick = self.select(state, candidates)
        bucket = pick.bucket
        scored = sorted(
            (ScoredAction(action=a, score=self.score(bucket, a)) for a in candidates),
            key=lambda s: s.score,
            reverse=True,
        )
        decision = Decision(
            decision_id=decision_id,
            customer_id=state.customer_id,
            state=state,
            candidates=scored,
            chosen=pick.action,
            explored=pick.explored,
            is_holdout=state.is_holdout,
        )
        if state.is_holdout:
            decision.log("master", "holdout (control) customer: no nudge sent")
        elif not candidates:
            decision.log("master", "no legal candidates: waiting")
        else:
            kind = "explored at random" if pick.explored else "exploited best estimate"
            decision.log("master", f"bucket={bucket}; {kind}; chose {pick.action.key}")
        return decision

    def update(self, bucket: str, action: Action, reward: float) -> None:
        if action.is_wait:
            return
        reward = check_reward(reward)  # a NaN would poison this cell for good
        n, total = self._stats.get((bucket, action.key), (0, 0.0))
        self._stats[(bucket, action.key)] = (n + 1, total + reward)

    @property
    def cells_learned(self) -> int:
        return len(self._stats)

    def adjust(self, bucket: str, action: Action, delta: float) -> None:
        """Correct an earlier reward (e.g. an order arrives after an open). Count unchanged."""
        if action.is_wait:
            return
        delta = check_reward(delta)
        n, total = self._stats.get((bucket, action.key), (1, 0.0))
        self._stats[(bucket, action.key)] = (max(n, 1), total + delta)

    def export_state(self) -> dict[str, Any]:
        """JSON-friendly snapshot of everything learned (saved through the repository)."""
        return cells_to_policy(self._stats)

    def load_state(self, data: dict[str, Any]) -> None:
        """Replace what is known with a stored policy. Raises PolicyError, and changes nothing,
        if the policy is malformed (negative count, NaN total, missing field...)."""
        self._stats = parse_cells(data)
