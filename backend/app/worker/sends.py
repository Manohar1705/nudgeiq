"""Deliver the sends that are due (8d-2). Plain Python: no Celery, so tests need no Redis.

The schedule lives in the `scheduled_sends` table. Each run:
  1. marks sends stuck in 'sending' (a worker died mid-send) as 'failed',
  2. claims the sends whose time has come (one atomic step, so two workers never take the same
     one), and
  3. for each: skips it if the customer is no longer eligible or it is far too late, otherwise
     delivers it and marks it 'sent'.

Delivery is AT MOST ONCE. If a worker dies after claiming a send, that send is marked 'failed'
and is not retried, because the message may already have gone out. A missed nudge is cheaper
than a customer getting the same text twice.
"""

import logging
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from app.models import Channel, JourneyStatus, ScheduledSend
from app.repository.base import Repository

log = logging.getLogger("nudgeiq.worker")

DEFAULT_MAX_LATE_MINUTES = 120  # a 09:00 send is not delivered at 23:00 when a worker was down
DEFAULT_BATCH = 100
STUCK_AFTER = timedelta(minutes=10)

Deliver = Callable[[ScheduledSend], None]


def max_late_from_env(env: Mapping[str, str] | None = None) -> timedelta:
    source = os.environ if env is None else env
    raw = (source.get("NUDGEIQ_SEND_MAX_LATE_MINUTES") or "").strip()
    if not raw:
        return timedelta(minutes=DEFAULT_MAX_LATE_MINUTES)
    try:
        minutes = int(raw)
    except ValueError:
        minutes = -1
    if minutes < 1:
        raise ValueError(
            "NUDGEIQ_SEND_MAX_LATE_MINUTES must be a whole number of minutes, 1 or more"
        )
    return timedelta(minutes=minutes)


def log_deliver(send: ScheduledSend) -> None:
    """The only sender there is today: write the message to the log. A real SMS/push/email
    provider replaces this one function."""
    log.info(
        "SEND decision=%s customer=%s channel=%s variant=%s text=%r",
        send.decision_id,
        send.customer_id,
        send.channel.value,
        send.variant_id,
        send.text,
    )


def skip_reason(repo: Repository, send: ScheduledSend) -> str | None:
    """Why this send should not go out now, or None if it is still fine. Looks at the customer's
    current state, which may have changed since the decision was made."""
    customer = repo.get_customer(send.customer_id)
    if customer is None:
        return "customer not found"
    if not customer.contactable:
        return "customer is no longer contactable"
    if send.channel == Channel.SMS and not customer.sms_opt_in:
        return "customer is not opted in to SMS"
    if send.channel == Channel.EMAIL and not customer.email_opt_in:
        return "customer is not opted in to email"
    if send.channel == Channel.PUSH and not customer.has_app:
        return "customer has no app for push"
    journey = repo.get_journey(send.customer_id)
    if journey is not None and journey.status != JourneyStatus.ACTIVE:
        return f"journey is {journey.status.value}"
    return None


def run_due_sends(
    repo: Repository,
    now: datetime,
    *,
    deliver: Deliver = log_deliver,
    max_late: timedelta | None = None,
    batch: int = DEFAULT_BATCH,
) -> dict[str, int]:
    """One pass. Returns counts: sent, skipped, failed, recovered (stuck ones marked failed)."""
    late_limit = max_late if max_late is not None else max_late_from_env()
    counts = {"sent": 0, "skipped": 0, "failed": 0, "recovered": 0}
    counts["recovered"] = repo.fail_stuck_sends(now - STUCK_AFTER, now)
    if counts["recovered"]:
        log.warning(
            "%d send(s) were stuck in 'sending' and were marked failed", counts["recovered"]
        )

    for send in repo.claim_due_sends(now, batch):
        if now - send.send_at > late_limit:
            repo.finish_scheduled_send(
                send.decision_id, "skipped", f"too late (due {send.send_at.isoformat()})", now
            )
            counts["skipped"] += 1
            continue
        try:
            reason = skip_reason(repo, send)
            if reason is not None:
                repo.finish_scheduled_send(send.decision_id, "skipped", reason, now)
                counts["skipped"] += 1
                continue
            deliver(send)
        except Exception as exc:  # one bad send must not stop the rest of the batch
            log.exception("send for decision %s failed", send.decision_id)
            repo.finish_scheduled_send(
                send.decision_id, "failed", f"{type(exc).__name__}: {exc}", now
            )
            counts["failed"] += 1
            continue
        repo.finish_scheduled_send(send.decision_id, "sent", "", now)
        counts["sent"] += 1
    return counts
