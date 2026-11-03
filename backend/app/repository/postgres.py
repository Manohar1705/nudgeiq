"""PostgreSQL repository: the same interface as the in-memory one, backed by real tables.

    repo = PostgresRepository("postgresql://user:password@localhost:5432/nudgeiq")

Every call commits immediately (autocommit), so flush() has nothing to do.
"""

import threading
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

POLICY_LOCK_TIMEOUT = "10s"  # how long a credit waits for another writer before giving up

_CAMPAIGN_COLUMNS = (
    "campaign_id, status, version, config, created_at, updated_at, activated_at, archived_at"
)
_TEMPLATE_COLUMNS = "template_id, name, channel, body, offers"
_VARIANT_COLUMNS = (
    "variant_id, template_id, text, principle, tone, status, source, generated_by, "
    "created_at, reviewed_at, review_note"
)

_SEND_COLUMNS = (
    "decision_id, customer_id, send_at, channel, text, variant_id, template_id, status, reason, "
    "created_at, claimed_at, finished_at"
)

_OUTCOME_COLUMNS = (
    "id, decision_id, customer_id, outcome_type, occurred_at, payload, status, "
    "idempotency_key, received_at, processed_at, attempts, last_error"
)


def _outcome_record(row: dict[str, Any]) -> OutcomeRecord:
    return OutcomeRecord(
        outcome_id=row["id"],
        outcome=Outcome(
            decision_id=row["decision_id"],
            customer_id=row["customer_id"],
            outcome_type=row["outcome_type"],
            occurred_at=row["occurred_at"],
            payload=row["payload"],
        ),
        status=row["status"],
        idempotency_key=row["idempotency_key"],
        received_at=row["received_at"],
        processed_at=row["processed_at"],
        attempts=row["attempts"],
        last_error=row["last_error"],
    )


_CUSTOMER_COLUMNS = (
    "customer_id, cohort_id, cart_value, abandoned_minutes, timing_anchor_minutes, "
    "contactable, dnd, has_app, sms_opt_in, email_opt_in, preferred_channel, "
    "contacts_in_window, last_contact_at, is_holdout, signals"
)


class PostgresRepository:
    def __init__(self, dsn: str, *, create_schema: bool = True) -> None:
        self._conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        self._lock = threading.Lock()  # one connection shared by API worker threads
        if create_schema:
            self.create_schema()

    # ------------------------------------------------------------------ admin
    def create_schema(self) -> None:
        with self._lock:
            self._conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    def reset(self) -> None:
        """Delete all data (tests and demos only)."""
        with self._lock:
            self._conn.execute(
                "TRUNCATE rewards, outcomes, decisions, journeys, contacts, customers, policies,"
                " policy_credits, variants, templates, campaigns, scheduled_sends"
            )

    def close(self) -> None:
        self._conn.close()

    def flush(self) -> None:
        return None

    def _all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _run(self, sql: str, params: Any = None) -> None:
        with self._lock:
            self._conn.execute(sql, params)

    # -------------------------------------------------------------- customers
    def save_customer(self, state: CustomerState) -> None:
        row = state.model_dump(mode="python")
        row["signals"] = Jsonb(state.signals)
        row["preferred_channel"] = (
            state.preferred_channel.value if state.preferred_channel else None
        )
        self._run(
            f"""INSERT INTO customers ({_CUSTOMER_COLUMNS})
                VALUES (%(customer_id)s, %(cohort_id)s, %(cart_value)s, %(abandoned_minutes)s,
                        %(timing_anchor_minutes)s, %(contactable)s, %(dnd)s, %(has_app)s,
                        %(sms_opt_in)s, %(email_opt_in)s, %(preferred_channel)s,
                        %(contacts_in_window)s, %(last_contact_at)s, %(is_holdout)s, %(signals)s)
                ON CONFLICT (customer_id) DO UPDATE SET
                    cohort_id = EXCLUDED.cohort_id, cart_value = EXCLUDED.cart_value,
                    abandoned_minutes = EXCLUDED.abandoned_minutes,
                    timing_anchor_minutes = EXCLUDED.timing_anchor_minutes,
                    contactable = EXCLUDED.contactable, dnd = EXCLUDED.dnd,
                    has_app = EXCLUDED.has_app, sms_opt_in = EXCLUDED.sms_opt_in,
                    email_opt_in = EXCLUDED.email_opt_in,
                    preferred_channel = EXCLUDED.preferred_channel,
                    contacts_in_window = EXCLUDED.contacts_in_window,
                    last_contact_at = EXCLUDED.last_contact_at,
                    is_holdout = EXCLUDED.is_holdout, signals = EXCLUDED.signals,
                    updated_at = now()""",
            row,
        )

    add_customer = save_customer  # same alias the in-memory repository has

    def get_customer(self, customer_id: str) -> CustomerState | None:
        row = self._one(
            f"SELECT {_CUSTOMER_COLUMNS} FROM customers WHERE customer_id = %s", (customer_id,)
        )
        return CustomerState.model_validate(row) if row else None

    def list_customers(self, limit: int = 100) -> list[CustomerState]:
        rows = self._all(
            f"SELECT {_CUSTOMER_COLUMNS} FROM customers ORDER BY customer_id LIMIT %s", (limit,)
        )
        return [CustomerState.model_validate(r) for r in rows]

    def record_contact(self, customer_id: str, when: datetime) -> None:
        self._run(
            "INSERT INTO contacts (customer_id, contact_at) VALUES (%s, %s)", (customer_id, when)
        )

    def get_contact_times(self, customer_id: str) -> list[datetime]:
        rows = self._all(
            "SELECT contact_at FROM contacts WHERE customer_id = %s ORDER BY contact_at",
            (customer_id,),
        )
        return [r["contact_at"] for r in rows]

    # --------------------------------------------------------------- journeys
    def get_journey(self, customer_id: str) -> Journey | None:
        row = self._one(
            """SELECT customer_id, status, started_at, base_abandoned_minutes, nudges_sent,
                      closed_reason, closed_at FROM journeys WHERE customer_id = %s""",
            (customer_id,),
        )
        return Journey.model_validate(row) if row else None

    def save_journey(self, journey: Journey) -> None:
        row = journey.model_dump(mode="python")
        row["status"] = journey.status.value
        self._run(
            """INSERT INTO journeys (customer_id, status, started_at, base_abandoned_minutes,
                                     nudges_sent, closed_reason, closed_at)
               VALUES (%(customer_id)s, %(status)s, %(started_at)s, %(base_abandoned_minutes)s,
                       %(nudges_sent)s, %(closed_reason)s, %(closed_at)s)
               ON CONFLICT (customer_id) DO UPDATE SET
                   status = EXCLUDED.status, started_at = EXCLUDED.started_at,
                   base_abandoned_minutes = EXCLUDED.base_abandoned_minutes,
                   nudges_sent = EXCLUDED.nudges_sent, closed_reason = EXCLUDED.closed_reason,
                   closed_at = EXCLUDED.closed_at""",
            row,
        )

    # -------------------------------------------------------------- decisions
    def save_decision(self, decision: Decision) -> None:
        chosen = decision.chosen
        self._run(
            """INSERT INTO decisions (decision_id, customer_id, created_at, is_holdout, explored,
                   bucket, chosen_channel, chosen_timing, chosen_offer, chosen_lever,
                   scheduled_for, variant_id, message, state, candidates, blocked, steps,
                   use_case_id, campaign_version, template_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s)
               ON CONFLICT (decision_id) DO UPDATE SET
                   is_holdout = EXCLUDED.is_holdout, explored = EXCLUDED.explored,
                   bucket = EXCLUDED.bucket, chosen_channel = EXCLUDED.chosen_channel,
                   chosen_timing = EXCLUDED.chosen_timing, chosen_offer = EXCLUDED.chosen_offer,
                   chosen_lever = EXCLUDED.chosen_lever, scheduled_for = EXCLUDED.scheduled_for,
                   variant_id = EXCLUDED.variant_id, message = EXCLUDED.message,
                   state = EXCLUDED.state, candidates = EXCLUDED.candidates,
                   blocked = EXCLUDED.blocked, steps = EXCLUDED.steps,
                   use_case_id = EXCLUDED.use_case_id,
                   campaign_version = EXCLUDED.campaign_version,
                   template_id = EXCLUDED.template_id""",
            (
                decision.decision_id,
                decision.customer_id,
                decision.created_at,
                decision.is_holdout,
                decision.explored,
                decision.bucket,
                chosen.channel.value if chosen.channel else None,
                chosen.timing,
                chosen.offer,
                chosen.lever,
                decision.scheduled_for,
                decision.variant_id,
                decision.message,
                Jsonb(decision.state.model_dump(mode="json")),
                Jsonb([c.model_dump(mode="json") for c in decision.candidates]),
                Jsonb(decision.blocked),
                Jsonb([s.model_dump(mode="json") for s in decision.steps]),
                decision.use_case_id,
                decision.campaign_version,
                decision.template_id,
            ),
        )

    @staticmethod
    def _decision_from_row(row: dict[str, Any]) -> Decision:
        return Decision.model_validate(
            {
                "decision_id": row["decision_id"],
                "customer_id": row["customer_id"],
                "created_at": row["created_at"],
                "state": row["state"],
                "candidates": row["candidates"],
                "blocked": row["blocked"],
                "chosen": {
                    "channel": row["chosen_channel"],
                    "timing": row["chosen_timing"],
                    "offer": row["chosen_offer"],
                    "lever": row["chosen_lever"],
                },
                "explored": row["explored"],
                "is_holdout": row["is_holdout"],
                "bucket": row["bucket"],
                "scheduled_for": row["scheduled_for"],
                "variant_id": row["variant_id"],
                "message": row["message"],
                "steps": row["steps"],
                "use_case_id": row["use_case_id"],
                "campaign_version": row["campaign_version"],
                "template_id": row["template_id"],
            }
        )

    def get_decision(self, decision_id: str) -> Decision | None:
        row = self._one("SELECT * FROM decisions WHERE decision_id = %s", (decision_id,))
        return self._decision_from_row(row) if row else None

    def list_decisions(
        self, customer_id: str | None = None, limit: int | None = 100
    ) -> list[Decision]:
        rows = self._all(
            """SELECT * FROM decisions WHERE (%(cid)s::text IS NULL OR customer_id = %(cid)s)
               ORDER BY created_at DESC, decision_id LIMIT %(limit)s""",
            {"cid": customer_id, "limit": limit},
        )
        return [self._decision_from_row(r) for r in rows]

    # ------------------------------------------------------ outcomes, rewards
    def save_outcome(
        self,
        outcome: Outcome,
        *,
        status: OutcomeStatus = "processed",
        idempotency_key: str | None = None,
        received_at: datetime | None = None,
    ) -> SavedOutcome:
        check_outcome_status(status)
        received = received_at or datetime.now(UTC)
        with self._lock, self._conn.transaction():
            # One statement: a second delivery of the same key does nothing (ON CONFLICT waits
            # for a concurrent first insert to finish), then the stored row is read back.
            row = self._conn.execute(
                f"""INSERT INTO outcomes (decision_id, customer_id, outcome_type, occurred_at,
                                          payload, status, idempotency_key, received_at,
                                          processed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
                    RETURNING {_OUTCOME_COLUMNS}""",
                (
                    outcome.decision_id,
                    outcome.customer_id,
                    outcome.outcome_type.value,
                    outcome.occurred_at,
                    Jsonb(outcome.payload),
                    status,
                    idempotency_key,
                    received,
                    received if status == "processed" else None,
                ),
            ).fetchone()
            if row is not None:
                return SavedOutcome(_outcome_record(row), True)
            existing = self._conn.execute(
                f"SELECT {_OUTCOME_COLUMNS} FROM outcomes WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
        if existing is None:  # the row vanished between the two statements (deleted by hand)
            raise RuntimeError(f"outcome with idempotency key {idempotency_key!r} disappeared")
        record = _outcome_record(existing)
        same = record.outcome
        if (same.decision_id, same.customer_id, same.outcome_type) != (
            outcome.decision_id,
            outcome.customer_id,
            outcome.outcome_type,
        ):
            raise OutcomeKeyConflict(
                f"idempotency key {idempotency_key!r} was already used for a different outcome"
            )
        return SavedOutcome(record, False)

    def get_outcome(self, outcome_id: int) -> OutcomeRecord | None:
        row = self._one(f"SELECT {_OUTCOME_COLUMNS} FROM outcomes WHERE id = %s", (outcome_id,))
        return _outcome_record(row) if row else None

    def list_outcomes(
        self,
        decision_id: str | None = None,
        customer_id: str | None = None,
        status: OutcomeStatus | None = None,
    ) -> list[Outcome]:
        rows = self._all(
            """SELECT decision_id, customer_id, outcome_type, occurred_at, payload FROM outcomes
               WHERE (%(did)s::text IS NULL OR decision_id = %(did)s)
                 AND (%(cid)s::text IS NULL OR customer_id = %(cid)s)
                 AND (%(st)s::text IS NULL OR status = %(st)s)
               ORDER BY occurred_at, id""",
            {"did": decision_id, "cid": customer_id, "st": status},
        )
        return [Outcome.model_validate(r) for r in rows]

    def list_outcome_records(
        self,
        decision_id: str | None = None,
        customer_id: str | None = None,
        statuses: Collection[OutcomeStatus] | None = None,
    ) -> list[OutcomeRecord]:
        for status in statuses or ():
            check_outcome_status(status)
        rows = self._all(
            f"""SELECT {_OUTCOME_COLUMNS} FROM outcomes
                WHERE (%(did)s::text IS NULL OR decision_id = %(did)s)
                  AND (%(cid)s::text IS NULL OR customer_id = %(cid)s)
                  AND (%(st)s::text[] IS NULL OR status = ANY(%(st)s::text[]))
                ORDER BY occurred_at, id""",
            {
                "did": decision_id,
                "cid": customer_id,
                "st": None if statuses is None else list(statuses),
            },
        )
        return [_outcome_record(r) for r in rows]

    def mark_outcome(
        self, outcome_id: int, status: OutcomeStatus, at: datetime, error: str = ""
    ) -> None:
        check_outcome_status(status)
        row = self._one(
            """UPDATE outcomes
               SET status = %(st)s::text, last_error = %(err)s,
                   processed_at = CASE WHEN %(st)s::text = 'processed' THEN %(at)s END
               WHERE id = %(id)s RETURNING id""",
            {"st": status, "err": error, "at": at, "id": outcome_id},
        )
        if row is None:
            raise KeyError(f"outcome {outcome_id} not found")

    def count_outcome_attempt(self, outcome_id: int, error: str = "") -> int:
        row = self._one(
            """UPDATE outcomes SET attempts = attempts + 1, last_error = %s
               WHERE id = %s RETURNING attempts""",
            (error, outcome_id),
        )
        if row is None:
            raise KeyError(f"outcome {outcome_id} not found")
        return int(row["attempts"])

    def list_pending_outcomes(self, received_by: datetime, limit: int = 100) -> list[OutcomeRecord]:
        rows = self._all(
            f"""SELECT {_OUTCOME_COLUMNS} FROM outcomes
                WHERE status = 'pending' AND received_at <= %s
                ORDER BY received_at, id LIMIT %s""",
            (received_by, limit),
        )
        return [_outcome_record(r) for r in rows]

    def save_reward(self, reward: RewardRecord) -> None:
        self._run(
            """INSERT INTO rewards (decision_id, customer_id, points, reason)
               VALUES (%(decision_id)s, %(customer_id)s, %(points)s, %(reason)s)
               ON CONFLICT (decision_id) DO UPDATE SET
                   points = EXCLUDED.points, reason = EXCLUDED.reason""",
            reward.model_dump(),
        )

    def get_reward(self, decision_id: str) -> RewardRecord | None:
        row = self._one(
            "SELECT decision_id, customer_id, points, reason FROM rewards WHERE decision_id = %s",
            (decision_id,),
        )
        return RewardRecord.model_validate(row) if row else None

    # ------------------------------------------------------- scheduled sends (8d-2)
    def save_scheduled_send(self, send: ScheduledSend) -> bool:
        with self._lock:
            cur = self._conn.execute(
                f"""INSERT INTO scheduled_sends ({_SEND_COLUMNS})
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (decision_id) DO NOTHING""",
                (
                    send.decision_id,
                    send.customer_id,
                    send.send_at,
                    send.channel.value,
                    send.text,
                    send.variant_id,
                    send.template_id,
                    send.status,
                    send.reason,
                    send.created_at,
                    send.claimed_at,
                    send.finished_at,
                ),
            )
            return cur.rowcount == 1

    def get_scheduled_send(self, decision_id: str) -> ScheduledSend | None:
        row = self._one(
            f"SELECT {_SEND_COLUMNS} FROM scheduled_sends WHERE decision_id = %s", (decision_id,)
        )
        return ScheduledSend.model_validate(row) if row else None

    def list_scheduled_sends(
        self, status: SendStatus | None = None, limit: int = 100
    ) -> list[ScheduledSend]:
        where, params = ("WHERE status = %s", [status]) if status else ("", [])
        rows = self._all(
            f"SELECT {_SEND_COLUMNS} FROM scheduled_sends {where}"
            " ORDER BY send_at DESC, decision_id DESC LIMIT %s",
            (*params, limit),
        )
        return [ScheduledSend.model_validate(r) for r in rows]

    def claim_due_sends(self, now: datetime, limit: int = 100) -> list[ScheduledSend]:
        # One statement: the inner SELECT locks the rows (SKIP LOCKED, so a second worker moves
        # on to other rows instead of waiting) and the UPDATE flips them. No row is returned twice.
        rows = self._all(
            f"""UPDATE scheduled_sends SET status = 'sending', claimed_at = %s
                WHERE decision_id IN (
                    SELECT decision_id FROM scheduled_sends
                    WHERE status = 'scheduled' AND send_at <= %s
                    ORDER BY send_at, decision_id LIMIT %s FOR UPDATE SKIP LOCKED)
                RETURNING {_SEND_COLUMNS}""",
            (now, now, limit),
        )
        sends = [ScheduledSend.model_validate(r) for r in rows]
        sends.sort(key=lambda s: (s.send_at, s.decision_id))
        return sends

    def finish_scheduled_send(
        self, decision_id: str, status: SendStatus, reason: str, at: datetime
    ) -> None:
        self._run(
            "UPDATE scheduled_sends SET status = %s, reason = %s, finished_at = %s"
            " WHERE decision_id = %s",
            (status, reason, at, decision_id),
        )

    def fail_stuck_sends(self, claimed_before: datetime, at: datetime) -> int:
        reason = "worker stopped while sending; not retried to avoid a double send"
        with self._lock:
            cur = self._conn.execute(
                "UPDATE scheduled_sends SET status = 'failed', reason = %s, finished_at = %s"
                " WHERE status = 'sending' AND claimed_at < %s",
                (reason, at, claimed_before),
            )
            return cur.rowcount

    # ----------------------------------------------------------------- policy
    def save_policy(self, use_case_id: str, policy: dict[str, Any]) -> None:
        self._run(
            """INSERT INTO policies (use_case_id, policy, version) VALUES (%s, %s, 1)
               ON CONFLICT (use_case_id) DO UPDATE
               SET policy = EXCLUDED.policy, version = policies.version + 1, updated_at = now()""",
            (use_case_id, Jsonb(policy)),
        )

    def load_policy(self, use_case_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT policy FROM policies WHERE use_case_id = %s", (use_case_id,))
        return row["policy"] if row else None

    def read_policy(self, use_case_id: str) -> StoredPolicy | None:
        row = self._one(
            "SELECT policy, version FROM policies WHERE use_case_id = %s", (use_case_id,)
        )
        return StoredPolicy(row["policy"], int(row["version"])) if row else None

    def policy_version(self, use_case_id: str) -> int:
        row = self._one("SELECT version FROM policies WHERE use_case_id = %s", (use_case_id,))
        return int(row["version"]) if row else 0

    def credit_policy(
        self,
        use_case_id: str,
        decision_id: str,
        bucket: str,
        action_key: str,
        points: float,
        legacy_points: float | None = None,
    ) -> PolicyCredit:
        """One transaction: lock the policy row, read what this decision already added, apply
        the difference, write both. A second writer waits on the row lock (at most
        POLICY_LOCK_TIMEOUT, then it fails instead of hanging) and then sees the first one's
        result, so nothing is lost and a repeat adds nothing. Any error rolls everything back."""
        with self._lock, self._conn.transaction():
            self._conn.execute(f"SET LOCAL lock_timeout = '{POLICY_LOCK_TIMEOUT}'")
            # Make sure the row exists so there is something to lock (rolled back on error).
            self._conn.execute(
                "INSERT INTO policies (use_case_id, policy, version) VALUES (%s, %s, 0)"
                " ON CONFLICT (use_case_id) DO NOTHING",
                (use_case_id, Jsonb({"cells": []})),
            )
            row = self._conn.execute(
                "SELECT policy, version FROM policies WHERE use_case_id = %s FOR UPDATE",
                (use_case_id,),
            ).fetchone()
            assert row is not None
            cells = parse_cells(row["policy"])
            credit = self._conn.execute(
                "SELECT points FROM policy_credits WHERE decision_id = %s", (decision_id,)
            ).fetchone()
            already = float(credit["points"]) if credit else legacy_points
            changed = apply_credit(cells, bucket, action_key, points, already)
            policy = cells_to_policy(cells)
            version = int(row["version"])
            if changed:
                version += 1
                self._conn.execute(
                    "UPDATE policies SET policy = %s, version = %s, updated_at = now()"
                    " WHERE use_case_id = %s",
                    (Jsonb(policy), version, use_case_id),
                )
            if changed or credit is None:
                self._conn.execute(
                    """INSERT INTO policy_credits (decision_id, use_case_id, points)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (decision_id) DO UPDATE
                       SET points = EXCLUDED.points, updated_at = now()""",
                    (decision_id, use_case_id, float(points)),
                )
            return PolicyCredit(policy, version, changed)

    # -------------------------------------------------------------- campaigns
    def save_campaign(self, campaign: Campaign) -> None:
        # name and trigger_event are copies of config fields, kept as columns for easy querying
        self._run(
            """INSERT INTO campaigns (campaign_id, name, trigger_event, status, version, config,
                                      created_at, updated_at, activated_at, archived_at)
               VALUES (%(campaign_id)s, %(name)s, %(trigger_event)s, %(status)s, %(version)s,
                       %(config)s, %(created_at)s, %(updated_at)s, %(activated_at)s,
                       %(archived_at)s)
               ON CONFLICT (campaign_id) DO UPDATE SET
                   name = EXCLUDED.name, trigger_event = EXCLUDED.trigger_event,
                   status = EXCLUDED.status, version = EXCLUDED.version,
                   config = EXCLUDED.config, created_at = EXCLUDED.created_at,
                   updated_at = EXCLUDED.updated_at, activated_at = EXCLUDED.activated_at,
                   archived_at = EXCLUDED.archived_at""",
            {
                "campaign_id": campaign.campaign_id,
                "name": campaign.config.name,
                "trigger_event": campaign.config.trigger_event,
                "status": campaign.status.value,
                "version": campaign.version,
                "config": Jsonb(campaign.config.model_dump(mode="json")),
                "created_at": campaign.created_at,
                "updated_at": campaign.updated_at,
                "activated_at": campaign.activated_at,
                "archived_at": campaign.archived_at,
            },
        )

    def get_campaign(self, campaign_id: str) -> Campaign | None:
        row = self._one(
            f"SELECT {_CAMPAIGN_COLUMNS} FROM campaigns WHERE campaign_id = %s", (campaign_id,)
        )
        return Campaign.model_validate(row) if row else None

    def list_campaigns(self, status: CampaignStatus | None = None) -> list[Campaign]:
        rows = self._all(
            f"""SELECT {_CAMPAIGN_COLUMNS} FROM campaigns
                WHERE (%(status)s::text IS NULL OR status = %(status)s)
                ORDER BY created_at, campaign_id""",
            {"status": status.value if status else None},
        )
        return [Campaign.model_validate(r) for r in rows]

    # -------------------------------------------------------------- templates
    def save_template(self, campaign_id: str, template: MessageTemplate) -> None:
        try:
            self._run(
                """INSERT INTO templates (campaign_id, template_id, name, channel, body, offers)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (campaign_id, template_id) DO UPDATE SET
                       name = EXCLUDED.name, channel = EXCLUDED.channel,
                       body = EXCLUDED.body, offers = EXCLUDED.offers""",
                (
                    campaign_id,
                    template.template_id,
                    template.name,
                    template.channel.value,
                    template.body,
                    Jsonb(template.offers),
                ),
            )
        except psycopg.errors.ForeignKeyViolation:
            raise KeyError(f"unknown campaign: {campaign_id}") from None

    def get_template(self, campaign_id: str, template_id: str) -> MessageTemplate | None:
        row = self._one(
            f"""SELECT {_TEMPLATE_COLUMNS} FROM templates
                WHERE campaign_id = %s AND template_id = %s""",
            (campaign_id, template_id),
        )
        return MessageTemplate.model_validate(row) if row else None

    def list_templates(self, campaign_id: str) -> list[MessageTemplate]:
        rows = self._all(
            f"SELECT {_TEMPLATE_COLUMNS} FROM templates WHERE campaign_id = %s ORDER BY seq",
            (campaign_id,),
        )
        return [MessageTemplate.model_validate(r) for r in rows]

    # --------------------------------------------------------------- variants
    def save_variant(self, campaign_id: str, variant: MessageVariant) -> None:
        # The foreign key on (campaign_id, template_id) rejects an unknown campaign or template.
        try:
            self._run(
                """INSERT INTO variants (campaign_id, variant_id, template_id, text, principle,
                       tone, status, source, generated_by, created_at, reviewed_at, review_note)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (campaign_id, variant_id) DO UPDATE SET
                       template_id = EXCLUDED.template_id, text = EXCLUDED.text,
                       principle = EXCLUDED.principle, tone = EXCLUDED.tone,
                       status = EXCLUDED.status, source = EXCLUDED.source,
                       generated_by = EXCLUDED.generated_by, created_at = EXCLUDED.created_at,
                       reviewed_at = EXCLUDED.reviewed_at, review_note = EXCLUDED.review_note""",
                (
                    campaign_id,
                    variant.variant_id,
                    variant.template_id,
                    variant.text,
                    variant.principle,
                    variant.tone,
                    variant.status.value,
                    variant.source.value,
                    variant.generated_by,
                    variant.created_at,
                    variant.reviewed_at,
                    variant.review_note,
                ),
            )
        except psycopg.errors.ForeignKeyViolation:
            raise KeyError(
                f"unknown campaign or template: {campaign_id} / {variant.template_id}"
            ) from None

    def get_variant(self, campaign_id: str, variant_id: str) -> MessageVariant | None:
        row = self._one(
            f"""SELECT {_VARIANT_COLUMNS} FROM variants
                WHERE campaign_id = %s AND variant_id = %s""",
            (campaign_id, variant_id),
        )
        return MessageVariant.model_validate(row) if row else None

    def list_variants(
        self,
        campaign_id: str,
        template_id: str | None = None,
        status: VariantStatus | None = None,
    ) -> list[MessageVariant]:
        rows = self._all(
            f"""SELECT {_VARIANT_COLUMNS} FROM variants
                WHERE campaign_id = %(cid)s
                  AND (%(tid)s::text IS NULL OR template_id = %(tid)s)
                  AND (%(status)s::text IS NULL OR status = %(status)s)
                ORDER BY seq""",
            {
                "cid": campaign_id,
                "tid": template_id,
                "status": status.value if status else None,
            },
        )
        return [MessageVariant.model_validate(r) for r in rows]
