"""In-memory repository for development and tests.

It returns the stored objects themselves (no copies), so treat results as read-only.
"""

import copy
import threading
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from app.models import (
    Campaign,
    CampaignStatus,
    CustomerState,
    Decision,
    Journey,
    MessageTemplate,
    MessageVariant,
    Outcome,
    OutcomeKeyConflict,
    OutcomeRecord,
    OutcomeStatus,
    PolicyCredit,
    RewardRecord,
    SavedOutcome,
    ScheduledSend,
    SendStatus,
    StoredPolicy,
    VariantStatus,
    apply_credit,
    cells_to_policy,
    check_outcome_status,
    parse_cells,
)


class InMemoryRepository:
    def __init__(self) -> None:
        self._customers: dict[str, CustomerState] = {}
        self._sends: dict[str, ScheduledSend] = {}
        self._sends_lock = threading.Lock()
        self._contacts: dict[str, list[datetime]] = {}
        self._journeys: dict[str, Journey] = {}
        self._decisions: dict[str, Decision] = {}
        # 8d-3b: outcomes are stored as records (outcome + status). The records are mutable and
        # shared by the three indexes below, so a status change shows up in all of them.
        self._outcomes: list[OutcomeRecord] = []
        self._outcomes_by_decision: dict[str, list[OutcomeRecord]] = {}
        self._outcomes_by_customer: dict[str, list[OutcomeRecord]] = {}
        self._outcomes_by_id: dict[int, OutcomeRecord] = {}
        self._outcomes_by_key: dict[str, OutcomeRecord] = {}
        self._outcome_seq = 0
        self._outcomes_lock = threading.Lock()
        self._decision_ids_by_customer: dict[str, list[str]] = {}
        self._rewards: dict[str, RewardRecord] = {}
        self._policies: dict[str, dict[str, Any]] = {}
        self._policy_versions: dict[str, int] = {}  # 8d-3a: 0 / missing = nothing stored
        self._policy_credits: dict[str, float] = {}  # decision_id -> points it already added
        self._policy_lock = threading.Lock()
        # 5B. Dicts keep insertion order, and re-assigning a key keeps its position,
        # which gives "creation order, upserts do not move it" for free.
        self._campaigns: dict[str, Campaign] = {}
        self._templates: dict[str, dict[str, MessageTemplate]] = {}
        self._variants: dict[str, dict[str, MessageVariant]] = {}

    # customers
    def save_customer(self, state: CustomerState) -> None:
        self._customers[state.customer_id] = state

    add_customer = save_customer  # Zip 3 name, kept so older code and tests still work

    def get_customer(self, customer_id: str) -> CustomerState | None:
        return self._customers.get(customer_id)

    def list_customers(self, limit: int = 100) -> list[CustomerState]:
        return list(self._customers.values())[:limit]

    def record_contact(self, customer_id: str, when: datetime) -> None:
        self._contacts.setdefault(customer_id, []).append(when)

    def get_contact_times(self, customer_id: str) -> list[datetime]:
        return sorted(self._contacts.get(customer_id, []))

    def all_contacts(self) -> dict[str, list[datetime]]:
        """Every contact by customer (used by the guardrail audit in the full simulation)."""
        return {k: list(v) for k, v in self._contacts.items()}

    # journeys
    def get_journey(self, customer_id: str) -> Journey | None:
        return self._journeys.get(customer_id)

    def save_journey(self, journey: Journey) -> None:
        self._journeys[journey.customer_id] = journey

    # decisions
    def save_decision(self, decision: Decision) -> None:
        if decision.decision_id not in self._decisions:
            self._decision_ids_by_customer.setdefault(decision.customer_id, []).append(
                decision.decision_id
            )
        self._decisions[decision.decision_id] = decision

    def get_decision(self, decision_id: str) -> Decision | None:
        return self._decisions.get(decision_id)

    def list_decisions(
        self, customer_id: str | None = None, limit: int | None = 100
    ) -> list[Decision]:
        if customer_id is None:
            found = list(self._decisions.values())
        else:
            ids = self._decision_ids_by_customer.get(customer_id, [])
            found = [self._decisions[i] for i in ids]
        found.sort(key=lambda d: d.created_at, reverse=True)
        return found if limit is None else found[:limit]

    # outcomes and rewards
    def _insert_outcome(self, record: OutcomeRecord) -> None:
        """Index a record. The caller holds the lock (or is single-threaded, as on load)."""
        self._outcomes.append(record)
        self._outcomes_by_decision.setdefault(record.outcome.decision_id, []).append(record)
        self._outcomes_by_customer.setdefault(record.outcome.customer_id, []).append(record)
        self._outcomes_by_id[record.outcome_id] = record
        if record.idempotency_key is not None:
            self._outcomes_by_key[record.idempotency_key] = record
        self._outcome_seq = max(self._outcome_seq, record.outcome_id)

    def save_outcome(
        self,
        outcome: Outcome,
        *,
        status: OutcomeStatus = "processed",
        idempotency_key: str | None = None,
        received_at: datetime | None = None,
    ) -> SavedOutcome:
        check_outcome_status(status)
        with self._outcomes_lock:
            if idempotency_key is not None:
                existing = self._outcomes_by_key.get(idempotency_key)
                if existing is not None:
                    same = existing.outcome
                    if (same.decision_id, same.customer_id, same.outcome_type) != (
                        outcome.decision_id,
                        outcome.customer_id,
                        outcome.outcome_type,
                    ):
                        raise OutcomeKeyConflict(
                            f"idempotency key {idempotency_key!r} was already used for a "
                            f"different outcome"
                        )
                    return SavedOutcome(existing.model_copy(), False)
            received = received_at or datetime.now(UTC)
            record = OutcomeRecord(
                outcome_id=self._outcome_seq + 1,
                outcome=outcome,
                status=status,
                idempotency_key=idempotency_key,
                received_at=received,
                processed_at=received if status == "processed" else None,
            )
            self._insert_outcome(record)
            return SavedOutcome(record.model_copy(), True)

    def get_outcome(self, outcome_id: int) -> OutcomeRecord | None:
        with self._outcomes_lock:
            found = self._outcomes_by_id.get(outcome_id)
            return found.model_copy() if found else None

    def list_outcomes(
        self,
        decision_id: str | None = None,
        customer_id: str | None = None,
        status: OutcomeStatus | None = None,
    ) -> list[Outcome]:
        if decision_id is not None:
            found = self._outcomes_by_decision.get(decision_id, [])
            if customer_id is not None:
                found = [r for r in found if r.outcome.customer_id == customer_id]
        elif customer_id is not None:
            found = self._outcomes_by_customer.get(customer_id, [])
        else:
            found = self._outcomes
        if status is not None:
            found = [r for r in found if r.status == status]
        return sorted((r.outcome for r in found), key=lambda o: o.occurred_at)

    def list_outcome_records(
        self,
        decision_id: str | None = None,
        customer_id: str | None = None,
        statuses: Collection[OutcomeStatus] | None = None,
    ) -> list[OutcomeRecord]:
        for status in statuses or ():
            check_outcome_status(status)
        with self._outcomes_lock:
            if decision_id is not None:
                found = self._outcomes_by_decision.get(decision_id, [])
                if customer_id is not None:
                    found = [r for r in found if r.outcome.customer_id == customer_id]
            elif customer_id is not None:
                found = self._outcomes_by_customer.get(customer_id, [])
            else:
                found = self._outcomes
            if statuses is not None:
                found = [r for r in found if r.status in statuses]
            ordered = sorted(found, key=lambda r: (r.outcome.occurred_at, r.outcome_id))
            return [r.model_copy() for r in ordered]

    def _outcome(self, outcome_id: int) -> OutcomeRecord:
        try:
            return self._outcomes_by_id[outcome_id]
        except KeyError:
            raise KeyError(f"outcome {outcome_id} not found") from None

    def mark_outcome(
        self, outcome_id: int, status: OutcomeStatus, at: datetime, error: str = ""
    ) -> None:
        check_outcome_status(status)
        with self._outcomes_lock:
            record = self._outcome(outcome_id)
            record.status = status
            record.last_error = error
            record.processed_at = at if status == "processed" else None

    def count_outcome_attempt(self, outcome_id: int, error: str = "") -> int:
        with self._outcomes_lock:
            record = self._outcome(outcome_id)
            record.attempts += 1
            record.last_error = error
            return record.attempts

    def list_pending_outcomes(self, received_by: datetime, limit: int = 100) -> list[OutcomeRecord]:
        with self._outcomes_lock:
            due = [
                r for r in self._outcomes if r.status == "pending" and r.received_at <= received_by
            ]
            due.sort(key=lambda r: (r.received_at, r.outcome_id))
            return [r.model_copy() for r in due[:limit]]

    def save_reward(self, reward: RewardRecord) -> None:
        self._rewards[reward.decision_id] = reward

    def get_reward(self, decision_id: str) -> RewardRecord | None:
        return self._rewards.get(decision_id)

    # learned policy
    def save_policy(self, use_case_id: str, policy: dict[str, Any]) -> None:
        with self._policy_lock:
            self._policies[use_case_id] = copy.deepcopy(policy)
            self._policy_versions[use_case_id] = self._policy_versions.get(use_case_id, 0) + 1

    def load_policy(self, use_case_id: str) -> dict[str, Any] | None:
        with self._policy_lock:
            stored = self._policies.get(use_case_id)
            return copy.deepcopy(stored) if stored is not None else None

    def read_policy(self, use_case_id: str) -> StoredPolicy | None:
        with self._policy_lock:
            stored = self._policies.get(use_case_id)
            if stored is None:
                return None
            return StoredPolicy(copy.deepcopy(stored), self._policy_versions.get(use_case_id, 0))

    def policy_version(self, use_case_id: str) -> int:
        with self._policy_lock:
            return self._policy_versions.get(use_case_id, 0)

    def credit_policy(
        self,
        use_case_id: str,
        decision_id: str,
        bucket: str,
        action_key: str,
        points: float,
        legacy_points: float | None = None,
    ) -> PolicyCredit:
        with self._policy_lock:
            cells = parse_cells(self._policies.get(use_case_id) or {"cells": []})
            already = self._policy_credits.get(decision_id, legacy_points)
            changed = apply_credit(cells, bucket, action_key, points, already)
            if changed:
                self._policies[use_case_id] = cells_to_policy(cells)
                self._policy_versions[use_case_id] = self._policy_versions.get(use_case_id, 0) + 1
                self._policy_credits[decision_id] = float(points)
            elif decision_id not in self._policy_credits:
                self._policy_credits[decision_id] = float(points)  # legacy decision, now recorded
            return PolicyCredit(
                copy.deepcopy(self._policies.get(use_case_id) or cells_to_policy(cells)),
                self._policy_versions.get(use_case_id, 0),
                changed,
            )

    # campaigns
    def save_campaign(self, campaign: Campaign) -> None:
        self._campaigns[campaign.campaign_id] = campaign

    def get_campaign(self, campaign_id: str) -> Campaign | None:
        return self._campaigns.get(campaign_id)

    def list_campaigns(self, status: CampaignStatus | None = None) -> list[Campaign]:
        found = [c for c in self._campaigns.values() if status is None or c.status == status]
        return sorted(found, key=lambda c: (c.created_at, c.campaign_id))

    # templates
    def save_template(self, campaign_id: str, template: MessageTemplate) -> None:
        if campaign_id not in self._campaigns:
            raise KeyError(f"unknown campaign: {campaign_id}")
        self._templates.setdefault(campaign_id, {})[template.template_id] = template

    def get_template(self, campaign_id: str, template_id: str) -> MessageTemplate | None:
        return self._templates.get(campaign_id, {}).get(template_id)

    def list_templates(self, campaign_id: str) -> list[MessageTemplate]:
        return list(self._templates.get(campaign_id, {}).values())

    # variants
    def save_variant(self, campaign_id: str, variant: MessageVariant) -> None:
        if campaign_id not in self._campaigns:
            raise KeyError(f"unknown campaign: {campaign_id}")
        if variant.template_id not in self._templates.get(campaign_id, {}):
            raise KeyError(f"unknown template: {variant.template_id}")
        self._variants.setdefault(campaign_id, {})[variant.variant_id] = variant

    def get_variant(self, campaign_id: str, variant_id: str) -> MessageVariant | None:
        return self._variants.get(campaign_id, {}).get(variant_id)

    def list_variants(
        self,
        campaign_id: str,
        template_id: str | None = None,
        status: VariantStatus | None = None,
    ) -> list[MessageVariant]:
        return [
            v
            for v in self._variants.get(campaign_id, {}).values()
            if (template_id is None or v.template_id == template_id)
            and (status is None or v.status == status)
        ]

    def flush(self) -> None:
        return None

    # ------------------------------------------------------------ scheduled sends (8d-2)
    # Kept in memory only (not written by JsonFileRepository.flush): they need a worker, and a
    # worker needs PostgreSQL. These exist so tests and the interface work on every repository.
    def save_scheduled_send(self, send: ScheduledSend) -> bool:
        with self._sends_lock:
            if send.decision_id in self._sends:
                return False
            self._sends[send.decision_id] = send
            return True

    def get_scheduled_send(self, decision_id: str) -> ScheduledSend | None:
        return self._sends.get(decision_id)

    def list_scheduled_sends(
        self, status: SendStatus | None = None, limit: int = 100
    ) -> list[ScheduledSend]:
        rows = [s for s in self._sends.values() if status is None or s.status == status]
        rows.sort(key=lambda s: (s.send_at, s.decision_id), reverse=True)
        return rows[:limit]

    def claim_due_sends(self, now: datetime, limit: int = 100) -> list[ScheduledSend]:
        with self._sends_lock:
            due = sorted(
                (s for s in self._sends.values() if s.status == "scheduled" and s.send_at <= now),
                key=lambda s: (s.send_at, s.decision_id),
            )[:limit]
            claimed = [s.model_copy(update={"status": "sending", "claimed_at": now}) for s in due]
            for c in claimed:
                self._sends[c.decision_id] = c
            return claimed

    def finish_scheduled_send(
        self, decision_id: str, status: SendStatus, reason: str, at: datetime
    ) -> None:
        with self._sends_lock:
            current = self._sends.get(decision_id)
            if current is not None:
                self._sends[decision_id] = current.model_copy(
                    update={"status": status, "reason": reason, "finished_at": at}
                )

    def fail_stuck_sends(self, claimed_before: datetime, at: datetime) -> int:
        reason = "worker stopped while sending; not retried to avoid a double send"
        with self._sends_lock:
            stuck = [
                s
                for s in self._sends.values()
                if s.status == "sending"
                and s.claimed_at is not None
                and s.claimed_at < claimed_before
            ]
            for s in stuck:
                self._sends[s.decision_id] = s.model_copy(
                    update={"status": "failed", "reason": reason, "finished_at": at}
                )
            return len(stuck)
