"""Repository interface. Agents and the orchestrator only talk to this, never to a database.

The PostgreSQL teammate implements this same interface. Method names are the contract.
"""

from collections.abc import Collection
from datetime import datetime
from typing import Any, Protocol

from app.models import (
    Campaign,
    CampaignStatus,
    CustomerState,
    Decision,
    Journey,
    MessageTemplate,
    MessageVariant,
    Outcome,
    OutcomeRecord,
    OutcomeStatus,
    PolicyCredit,
    RewardRecord,
    SavedOutcome,
    ScheduledSend,
    SendStatus,
    StoredPolicy,
    VariantStatus,
)


class CustomerRepository(Protocol):
    """The customer-side subset the Data/State agent needs."""

    def get_customer(self, customer_id: str) -> CustomerState | None: ...

    def get_contact_times(self, customer_id: str) -> list[datetime]:
        """Timestamps of every nudge already sent (or scheduled) for this customer."""
        ...


class Repository(CustomerRepository, Protocol):
    # customers
    def save_customer(self, state: CustomerState) -> None: ...
    def list_customers(self, limit: int = 100) -> list[CustomerState]: ...
    def record_contact(self, customer_id: str, when: datetime) -> None: ...

    # journeys (one current journey per customer)
    def get_journey(self, customer_id: str) -> Journey | None: ...
    def save_journey(self, journey: Journey) -> None: ...

    # decisions
    def save_decision(self, decision: Decision) -> None: ...
    def get_decision(self, decision_id: str) -> Decision | None: ...
    def list_decisions(
        self, customer_id: str | None = None, limit: int | None = 100
    ) -> list[Decision]:
        """Newest first. limit=None returns everything."""
        ...

    # outcomes and rewards
    def save_outcome(
        self,
        outcome: Outcome,
        *,
        status: OutcomeStatus = "processed",
        idempotency_key: str | None = None,
        received_at: datetime | None = None,
    ) -> SavedOutcome:
        """Store an outcome the moment it arrives (8d-3b step 1).

        `status` defaults to 'processed', so the synchronous path behaves as before; the
        asynchronous path stores 'pending' and a background task finishes the job.
        `idempotency_key`: when a record with this key exists, nothing is stored and that record
        comes back with created=False, so a retried request or a repeated delivery is stored
        once. A key reused for a different decision, customer or outcome type raises
        OutcomeKeyConflict. `received_at` (default: now, UTC) is when WE got it, and is what the
        stuck-outcome sweep looks at; it is not `outcome.occurred_at`."""
        ...

    def get_outcome(self, outcome_id: int) -> OutcomeRecord | None: ...

    def list_outcomes(
        self,
        decision_id: str | None = None,
        customer_id: str | None = None,
        status: OutcomeStatus | None = None,
    ) -> list[Outcome]:
        """Oldest first. status=None returns every status, as before 8d-3b, so a pending outcome
        is included. Callers that must only see finished work pass status='processed'."""
        ...

    def list_outcome_records(
        self,
        decision_id: str | None = None,
        customer_id: str | None = None,
        statuses: Collection[OutcomeStatus] | None = None,
    ) -> list[OutcomeRecord]:
        """Like list_outcomes, but each outcome comes with its id and status, and `statuses`
        picks several statuses in ONE read (None = all). Oldest first. Processing an outcome
        reads ('pending', 'processed') together: two separate reads could miss an outcome that
        moves from pending to processed in between."""
        ...

    def mark_outcome(
        self, outcome_id: int, status: OutcomeStatus, at: datetime, error: str = ""
    ) -> None:
        """Set the status and the last error. `processed_at` is set to `at` for 'processed' and
        cleared otherwise. Any change of status is allowed (a failed outcome can be put back to
        pending by hand). Raises KeyError if there is no such outcome."""
        ...

    def count_outcome_attempt(self, outcome_id: int, error: str = "") -> int:
        """One try ended in a retryable error: attempts goes up by one and last_error is
        replaced. Returns the new count. Raises KeyError if there is no such outcome."""
        ...

    def list_pending_outcomes(self, received_by: datetime, limit: int = 100) -> list[OutcomeRecord]:
        """Outcomes still 'pending' that were received at or before `received_by`, oldest first.
        For the sweep that picks up what a dead worker or a down queue left behind."""
        ...

    def save_reward(self, reward: RewardRecord) -> None:
        """Upsert: one reward row per decision, replaced as more outcomes arrive."""
        ...

    def get_reward(self, decision_id: str) -> RewardRecord | None: ...

    # learned policy (the Master agent's memory, so a restart does not wipe learning)
    def save_policy(self, use_case_id: str, policy: dict[str, Any]) -> None:
        """Replace the whole policy (admin or reset). Bumps the version. Not validated, so a
        bad policy can be stored; readers refuse it with PolicyError. Learning goes through
        credit_policy, never through this."""
        ...

    def load_policy(self, use_case_id: str) -> dict[str, Any] | None: ...

    # policy safety (8d-3a): the API and background workers learn into the same policy
    def read_policy(self, use_case_id: str) -> StoredPolicy | None:
        """The policy and its version together (one read), or None if nothing is stored."""
        ...

    def policy_version(self, use_case_id: str) -> int:
        """Goes up on every change; 0 = nothing stored. Cheap, so a process can ask before every
        decision whether another process has learned something."""
        ...

    def credit_policy(
        self,
        use_case_id: str,
        decision_id: str,
        bucket: str,
        action_key: str,
        points: float,
        legacy_points: float | None = None,
    ) -> PolicyCredit:
        """Add one decision's reward to the stored policy, atomically and at most once.

        Safe to repeat and safe to run in several processes at once: the policy is locked while
        it is changed, and the policy remembers what each decision already added. A first call
        adds a try and `points`; a later call with different points moves the total by the
        difference; a later call with the same points changes nothing (changed=False).
        `legacy_points` is for a decision learned before this record existed (its reward row is
        the only trace): treated as what it already added. Raises PolicyError, and changes
        nothing, if the stored policy is malformed or `points` is not finite."""
        ...

    # campaigns (5B): a campaign is a stored use case, keyed by campaign_id (= use_case_id)
    def save_campaign(self, campaign: Campaign) -> None:
        """Upsert by campaign_id. Replaces the whole campaign; the service owns versioning."""
        ...

    def get_campaign(self, campaign_id: str) -> Campaign | None: ...

    def list_campaigns(self, status: CampaignStatus | None = None) -> list[Campaign]:
        """Oldest created first (ties by campaign_id). status=None returns every status."""
        ...

    # message templates, owned by one campaign
    def save_template(self, campaign_id: str, template: MessageTemplate) -> None:
        """Upsert by (campaign_id, template_id); keeps its original list position.

        Raises KeyError if the campaign does not exist.
        """
        ...

    def get_template(self, campaign_id: str, template_id: str) -> MessageTemplate | None: ...

    def list_templates(self, campaign_id: str) -> list[MessageTemplate]:
        """In the order they were first saved. Unknown campaign gives an empty list."""
        ...

    # message variants, owned by one campaign and pointing at one of its templates
    def save_variant(self, campaign_id: str, variant: MessageVariant) -> None:
        """Upsert by (campaign_id, variant_id); keeps its original list position.

        Raises KeyError if the campaign or the variant's template does not exist.
        """
        ...

    def get_variant(self, campaign_id: str, variant_id: str) -> MessageVariant | None: ...

    def list_variants(
        self,
        campaign_id: str,
        template_id: str | None = None,
        status: VariantStatus | None = None,
    ) -> list[MessageVariant]:
        """In the order they were first saved, optionally narrowed by template and/or status."""
        ...

    # scheduled sends (8d-2): the schedule is kept in the database, so losing Redis loses nothing
    def save_scheduled_send(self, send: ScheduledSend) -> bool:
        """Insert unless this decision already has one. True = stored, False = already there."""
        ...

    def get_scheduled_send(self, decision_id: str) -> ScheduledSend | None: ...

    def list_scheduled_sends(
        self, status: SendStatus | None = None, limit: int = 100
    ) -> list[ScheduledSend]:
        """Newest send_at first."""
        ...

    def claim_due_sends(self, now: datetime, limit: int = 100) -> list[ScheduledSend]:
        """Atomically move up to `limit` sends with send_at <= now from 'scheduled' to 'sending'
        and return them (oldest send_at first). Two callers never get the same send."""
        ...

    def finish_scheduled_send(
        self, decision_id: str, status: SendStatus, reason: str, at: datetime
    ) -> None: ...

    def fail_stuck_sends(self, claimed_before: datetime, at: datetime) -> int:
        """'sending' rows claimed before the cutoff (a worker died mid-send) become 'failed'.
        They are NOT retried, because the message may already have gone out. Returns the count."""
        ...

    def flush(self) -> None:
        """Write pending changes to durable storage (no-op for pure in-memory)."""
        ...
