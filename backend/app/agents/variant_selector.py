"""How the Message agent picks one variant among the APPROVED ones that fit (5B-9).

Two selectors, both only ever see approved variants (the Message agent filters first):

- UniformSelector      every option equally likely. The default.
- EpsilonGreedySelector uses each variant's past conversion. Three rules, in this order:
    1. Warm-up: if any option has fewer than `min_resolved` resolved decisions, pick uniformly
       among those, so a new variant gets a fair first look instead of being starved.
    2. Explore: with probability `epsilon`, pick uniformly among all options.
    3. Exploit: otherwise pick the option with the highest conversion (ties broken at random).

A "resolved" decision is one with at least one recorded outcome, the same rule analytics uses.
"""

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from app.models import MessageVariant


@dataclass(frozen=True)
class VariantRecord:
    resolved: int  # decisions with a recorded outcome
    orders: int  # of those, how many ended in an order

    @property
    def conversion(self) -> float:
        return self.orders / self.resolved if self.resolved else 0.0


StatsSource = Callable[[], Mapping[str, VariantRecord]]


@dataclass(frozen=True)
class Choice:
    variant: MessageVariant
    how: str  # short reason, written to the decision trace


class VariantSelector(Protocol):
    def choose(self, options: list[MessageVariant]) -> Choice: ...


class UniformSelector:
    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    def choose(self, options: list[MessageVariant]) -> Choice:
        if not options:
            raise ValueError("no options to choose from")
        return Choice(self._rng.choice(options), f"uniform among {len(options)} approved")


class EpsilonGreedySelector:
    def __init__(
        self,
        stats: StatsSource,
        *,
        epsilon: float = 0.1,
        min_resolved: int = 30,
        seed: int | None = None,
    ) -> None:
        if not 0 <= epsilon <= 1:
            raise ValueError("epsilon must be between 0 and 1")
        if min_resolved < 1:
            raise ValueError("min_resolved must be at least 1")
        self._stats = stats
        self.epsilon = epsilon
        self.min_resolved = min_resolved
        self._rng = random.Random(seed)

    def choose(self, options: list[MessageVariant]) -> Choice:
        if not options:
            raise ValueError("no options to choose from")
        if len(options) == 1:
            return Choice(options[0], "only one approved variant")
        stats = self._stats()
        record = {v.variant_id: stats.get(v.variant_id, VariantRecord(0, 0)) for v in options}

        young = [v for v in options if record[v.variant_id].resolved < self.min_resolved]
        if young:
            picked = self._rng.choice(young)
            return Choice(
                picked,
                f"warm-up: {len(young)} of {len(options)} variants have under "
                f"{self.min_resolved} resolved decisions",
            )
        if self._rng.random() < self.epsilon:
            return Choice(self._rng.choice(options), f"explore (epsilon {self.epsilon:g})")
        best = max(record[v.variant_id].conversion for v in options)
        top = [v for v in options if record[v.variant_id].conversion == best]
        picked = self._rng.choice(top)
        return Choice(picked, f"exploit: best conversion {best:.1%} among {len(options)} variants")
