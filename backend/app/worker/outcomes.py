"""Process one stored outcome in the background (8d-3b, step 2). Plain Python: no Celery, so the
tests need no Redis. `app/worker/tasks.py` wraps this in a task.

An outcome is stored first, as 'pending' (step 1), and this turns it into 'processed': reward,
learning, unsubscribe and journey, through the same `OutcomeProcessor` the API uses.

What can happen, and what it does to the stored outcome:

  already processed      nothing. A task delivered twice (acks_late allows it) does no harm.
  already failed         nothing. It stays failed until a person puts it back to 'pending'.
  no such outcome        nothing to mark. Logged, not retried: the id will never appear.
  PERMANENT error        marked 'failed' with the reason, not retried. Trying again could not
                         change the answer: the decision or its campaign does not exist, the
                         outcome belongs to another customer, a stored policy is malformed.
  TRANSIENT error        not marked; retried after a growing delay. A lock timeout, a deadlock,
                         a restarting database, a dropped connection.
  any other error        retried too (it may be a bug that the next deploy fixes), but the tries
                         are counted in the database and after MAX_ATTEMPTS it is marked 'failed'
                         ("gave up"), so a bad outcome cannot be retried forever.

The count lives in the `attempts` column, not in Celery, so it survives a worker restart and a
re-delivery. When the database itself is down the count cannot be written either; those retries
are bounded by Celery's own retry limit (tasks.py). The outcome stays 'pending' and the sweep
(step 3) picks it up later, so nothing is lost either way.

Applying an outcome is safe to repeat (see app/orchestrator/outcomes.py), so a try that died
half way is simply run again from the start.
"""

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from app.models import CampaignStatus, Decision, PolicyError, UseCaseConfig
from app.orchestrator.outcomes import OutcomeProcessor
from app.repository.base import Repository

log = logging.getLogger("nudgeiq.worker")

RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 300
MAX_ATTEMPTS = 8  # counted tries (in the database) before an outcome is marked failed

try:  # the worker image has psycopg; a test run without it still imports this module
    import psycopg

    _DB_ERRORS: tuple[type[BaseException], ...] = (psycopg.OperationalError, psycopg.InterfaceError)
except ImportError:  # pragma: no cover
    _DB_ERRORS = ()

# PostgreSQL error codes that mean "the server is fine, this try collided with another":
# lock_not_available, deadlock_detected, serialization_failure, query_canceled (statement timeout).
# Anything else from the driver (no code, or a connection or shutdown class) is treated as a
# dead connection, and the worker opens a new one.
_COLLISION_STATES = {"55P03", "40P01", "40001", "57014"}


class PermanentOutcomeError(Exception):
    """Trying again cannot help. The outcome is marked 'failed' with this message."""


def retry_delay(attempts: int) -> int:
    """Seconds to wait after the `attempts`-th failed try: 5, 10, 20, 40, ... up to 300."""
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS << max(0, attempts - 1))


@dataclass(frozen=True)
class OutcomeRun:
    """What one try did. `status` is one of: processed, already_processed, already_failed,
    missing, failed (marked failed now), retry (try again in `retry_in` seconds)."""

    status: str
    detail: str = ""
    retry_in: int = 0
    reconnect: bool = False  # the connection is dead: the caller should open a new one

    def as_dict(self, outcome_id: int) -> dict[str, Any]:
        data: dict[str, Any] = {"outcome_id": outcome_id, "status": self.status}
        if self.detail:
            data["detail"] = self.detail
        return data


# ------------------------------------------------------------------------ classification
def is_collision(exc: BaseException) -> bool:
    return isinstance(exc, _DB_ERRORS) and getattr(exc, "sqlstate", None) in _COLLISION_STATES


def is_dead_connection(exc: BaseException) -> bool:
    """The error says the connection itself is gone, not that two tries collided."""
    if isinstance(exc, _DB_ERRORS):
        return not is_collision(exc)
    return isinstance(exc, OSError)  # includes ConnectionError and TimeoutError


def is_permanent(exc: BaseException) -> bool:
    return isinstance(exc, PermanentOutcomeError | PolicyError | ValidationError)


def _describe(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {text}"[:500]


# ------------------------------------------------------------------------ the work
def campaign_config_for(repo: Repository, decision: Decision) -> UseCaseConfig:
    """The config of the campaign that made the decision (the API does the same, so an outcome
    is rewarded by the rules of the campaign that sent the message, even after a swap)."""
    if decision.use_case_id is not None:
        campaign = repo.get_campaign(decision.use_case_id)
        if campaign is None:
            raise PermanentOutcomeError(f"campaign {decision.use_case_id} not found")
        return campaign.config
    # Made before decisions carried a campaign id: the live campaign, as the API does.
    live = repo.list_campaigns(CampaignStatus.ACTIVE)
    if len(live) != 1:
        # Not permanent: activating a campaign fixes it. The counted retries bound the wait.
        raise RuntimeError(
            f"decision {decision.decision_id} has no campaign and {len(live)} are active"
        )
    return live[0].config


def _process(repo: Repository, outcome_id: int, now: datetime) -> OutcomeRun:
    record = repo.get_outcome(outcome_id)
    if record is None:
        log.warning("outcome %s does not exist; nothing to do", outcome_id)
        return OutcomeRun("missing", f"outcome {outcome_id} not found")
    if record.status == "processed":
        return OutcomeRun("already_processed")
    if record.status == "failed":
        return OutcomeRun("already_failed", record.last_error)

    outcome = record.outcome
    decision = repo.get_decision(outcome.decision_id)
    if decision is None:
        raise PermanentOutcomeError(f"decision {outcome.decision_id} not found")
    if decision.customer_id != outcome.customer_id:
        raise PermanentOutcomeError(
            f"decision {decision.decision_id} belongs to {decision.customer_id}, "
            f"not {outcome.customer_id}"
        )
    config = campaign_config_for(repo, decision)
    # now=outcome.occurred_at, as the synchronous path does: the journey rules measure time up
    # to when the customer acted, not up to when a worker got round to it.
    OutcomeProcessor(repo, config).apply(decision, outcome, outcome.occurred_at)
    repo.mark_outcome(outcome_id, "processed", now)
    return OutcomeRun("processed")


def process_outcome_record(
    repo: Repository,
    outcome_id: int,
    *,
    now: datetime | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> OutcomeRun:
    """One try at one outcome. Never raises for a problem with the outcome or the database: it
    returns what to do next (see the module docstring)."""
    when = now or datetime.now(UTC)
    try:
        return _process(repo, outcome_id, when)
    except Exception as exc:
        return _after_error(repo, outcome_id, exc, when, max_attempts)


def _after_error(
    repo: Repository, outcome_id: int, exc: Exception, now: datetime, max_attempts: int
) -> OutcomeRun:
    reason = _describe(exc)
    reconnect = is_dead_connection(exc)

    if is_permanent(exc):
        log.warning("outcome %s failed for good: %s", outcome_id, reason)
        return _mark_failed(repo, outcome_id, reason, now, reconnect)

    log.warning("outcome %s: try failed (will retry): %s", outcome_id, reason)
    try:
        attempts = repo.count_outcome_attempt(outcome_id, reason)
    except KeyError:
        return OutcomeRun("missing", f"outcome {outcome_id} not found")
    except Exception as count_exc:  # the database cannot even count: it is down
        log.warning("outcome %s: could not count the try: %s", outcome_id, _describe(count_exc))
        return OutcomeRun(
            "retry",
            reason,
            retry_in=retry_delay(1),
            reconnect=reconnect or is_dead_connection(count_exc),
        )
    if attempts >= max_attempts:
        log.error("outcome %s: giving up after %d tries: %s", outcome_id, attempts, reason)
        return _mark_failed(
            repo, outcome_id, f"gave up after {attempts} tries: {reason}", now, reconnect
        )
    return OutcomeRun("retry", reason, retry_in=retry_delay(attempts), reconnect=reconnect)


def _mark_failed(
    repo: Repository, outcome_id: int, reason: str, now: datetime, reconnect: bool
) -> OutcomeRun:
    try:
        repo.mark_outcome(outcome_id, "failed", now, reason)
    except KeyError:
        return OutcomeRun("missing", f"outcome {outcome_id} not found")
    except Exception as mark_exc:
        # Could not record the failure (database trouble). Stay pending and try the whole thing
        # again: it will fail the same way and be marked then.
        log.warning("outcome %s: could not mark it failed: %s", outcome_id, _describe(mark_exc))
        return OutcomeRun(
            "retry",
            reason,
            retry_in=retry_delay(1),
            reconnect=reconnect or is_dead_connection(mark_exc),
        )
    return OutcomeRun("failed", reason, reconnect=reconnect)


# ------------------------------------------------------------------------ the sweep (step 3)
# An outcome is stored as 'pending' before anything is queued, so it is never lost. But the task
# can still go missing: Redis was down when the API tried to queue it, or a worker died and the
# queue lost the message. The sweep finds outcomes that stayed 'pending' for too long and queues
# them again. Safe to repeat: a second delivery does nothing (see _process).
#
# The grace period is longer than the longest retry delay (RETRY_MAX_SECONDS), so an outcome that
# is only waiting for its next retry is not queued a second time.
DEFAULT_SWEEP_GRACE_SECONDS = 600
MIN_SWEEP_GRACE_SECONDS = 60
SWEEP_BATCH = 100


def sweep_grace_from_env(env: Mapping[str, str] | None = None) -> timedelta:
    source = os.environ if env is None else env
    raw = (source.get("NUDGEIQ_OUTCOME_SWEEP_GRACE_SECONDS") or "").strip()
    if not raw:
        return timedelta(seconds=DEFAULT_SWEEP_GRACE_SECONDS)
    try:
        seconds = int(raw)
    except ValueError:
        seconds = -1
    if seconds < MIN_SWEEP_GRACE_SECONDS:
        raise ValueError(
            "NUDGEIQ_OUTCOME_SWEEP_GRACE_SECONDS must be a whole number of seconds, "
            f"{MIN_SWEEP_GRACE_SECONDS} or more"
        )
    return timedelta(seconds=seconds)


def sweep_pending_outcomes(
    repo: Repository,
    now: datetime,
    *,
    enqueue: Callable[[int], bool],
    grace: timedelta | None = None,
    limit: int = SWEEP_BATCH,
) -> dict[str, int]:
    """One pass: queue again every outcome that has been 'pending' for longer than the grace
    period (oldest first, at most `limit`). `enqueue` returns False when the queue cannot be
    reached; the pass then stops, since the rest would fail the same way, and the next pass
    tries again. Returns counts: found, queued, not_queued."""
    wait = grace if grace is not None else sweep_grace_from_env()
    stale = repo.list_pending_outcomes(now - wait, limit)
    counts = {"found": len(stale), "queued": 0, "not_queued": 0}
    for record in stale:
        if not enqueue(record.outcome_id):
            counts["not_queued"] = len(stale) - counts["queued"]
            break
        counts["queued"] += 1
    if stale:
        log.warning(
            "sweep: %d outcome(s) pending for over %ds; %d queued again, %d not queued",
            len(stale),
            int(wait.total_seconds()),
            counts["queued"],
            counts["not_queued"],
        )
    return counts
