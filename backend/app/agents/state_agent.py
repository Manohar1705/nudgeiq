"""Data/State agent: builds the customer state the decision agents see."""

import hashlib
from datetime import datetime, timedelta

from app.models import CustomerState, UseCaseConfig
from app.repository.base import CustomerRepository

# Merge fields for messages. They are personalisation, not decision context.
PERSONALIZATION_KEYS = ("first_name", "top_item", "home_store", "units_left")


class CustomerNotFoundError(LookupError):
    pass


def stable_holdout(customer_id: str, fraction: float) -> bool:
    """Same customer always lands in the same group (no flipping between runs)."""
    digest = hashlib.sha256(customer_id.encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF < fraction


class StateAgent:
    def __init__(self, repo: CustomerRepository, config: UseCaseConfig) -> None:
        self.repo = repo
        self.config = config

    def _visible_fields(self) -> set[str]:
        return {f for s in self.config.context_signals if s.enabled for f in s.fields}

    def build_state(self, customer_id: str, now: datetime) -> CustomerState:
        raw = self.repo.get_customer(customer_id)
        if raw is None:
            raise CustomerNotFoundError(customer_id)

        # Only signals the business switched on are visible to the agent.
        visible = self._visible_fields()
        signals = {k: v for k, v in raw.signals.items() if k in visible}

        # Contact history -> cap and cool-down inputs.
        window = timedelta(days=self.config.guardrails.window_days)
        times = self.repo.get_contact_times(customer_id)
        recent = [t for t in times if now - t <= window]

        return raw.model_copy(
            update={
                "signals": signals,
                "contacts_in_window": len(recent),
                "last_contact_at": max(times) if times else None,
                "is_holdout": stable_holdout(customer_id, self.config.learning.holdout_fraction),
            }
        )

    def personalization(self, customer_id: str) -> dict[str, str]:
        raw = self.repo.get_customer(customer_id)
        if raw is None:
            raise CustomerNotFoundError(customer_id)
        return {k: str(raw.signals[k]) for k in PERSONALIZATION_KEYS if k in raw.signals}
