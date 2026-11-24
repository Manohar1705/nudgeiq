"""Tasks the worker runs: a test task (8d-1), the scheduled-send pass (8d-2) and outcome
processing (8d-3b)."""

import logging
import os
import threading
from datetime import UTC, datetime
from typing import Any

from app.repository import PostgresRepository
from app.worker.celery_app import (
    DISPATCH_TASK_NAME,
    OUTCOME_TASK_NAME,
    SWEEP_TASK_NAME,
    celery_app,
)
from app.worker.outcomes import process_outcome_record, retry_delay, sweep_pending_outcomes
from app.worker.sends import run_due_sends

log = logging.getLogger("nudgeiq.worker")

# A safety net on top of the try count kept in the database (see app/worker/outcomes.py). It only
# matters when the database cannot be asked to count: after this many retries the task stops, the
# outcome is still 'pending', and the sweep (step 3) picks it up later.
OUTCOME_MAX_RETRIES = 12

_repo: PostgresRepository | None = None
_repo_lock = threading.Lock()


def worker_repo() -> PostgresRepository:
    """This worker process's own database connection, opened on first use."""
    global _repo
    with _repo_lock:
        if _repo is None:
            url = os.environ.get("NUDGEIQ_DATABASE_URL")
            if not url:
                raise RuntimeError(
                    "NUDGEIQ_DATABASE_URL is not set: the worker needs the database to send"
                )
            # The API creates the tables; the worker only uses them.
            _repo = PostgresRepository(url, create_schema=False)
        return _repo


def reset_worker_repo() -> None:
    """Close this process's database connection and forget it, so the next use opens a new one.
    Used when the connection has died; otherwise every later task would fail the same way."""
    global _repo
    with _repo_lock:
        old, _repo = _repo, None
    if old is not None:
        try:
            old.close()
        except Exception:  # already dead: that is why we are here
            log.debug("closing the old database connection failed", exc_info=True)


@celery_app.task(bind=True, name="nudgeiq.ping")
def ping(self: Any, message: str = "ping") -> dict[str, Any]:
    """Proves the whole path works: API side -> Redis -> worker -> Redis -> back."""
    return {
        "echo": message,
        "worker": self.request.hostname,
        "at": datetime.now(UTC).isoformat(),
    }


@celery_app.task(name=DISPATCH_TASK_NAME)
def dispatch_due() -> dict[str, int]:
    """Deliver every scheduled send that is due. Started every few seconds by `celery beat`.
    Safe to run twice at once or to be re-delivered: the database hands each send out once."""
    return run_due_sends(worker_repo(), datetime.now(UTC))


class OutcomeRetry(Exception):
    """Why a process_outcome try is being retried (shown in the worker log)."""


@celery_app.task(
    bind=True,
    name=OUTCOME_TASK_NAME,
    max_retries=OUTCOME_MAX_RETRIES,
    ignore_result=True,  # the result is the outcome's status in the database, not a Redis record
)
def process_outcome(self: Any, outcome_id: int) -> dict[str, Any]:
    """Reward and learn from one stored outcome (saved earlier as 'pending').

    Safe to run twice or to be re-delivered: app/worker/outcomes.py says what repeats and what
    fails for good. A transient problem (lock timeout, database restart) is retried later with a
    growing delay; the number of counted tries is kept in the database."""
    try:
        repo = worker_repo()
    except Exception as exc:  # the database is down, or its URL is not set
        raise self.retry(exc=exc, countdown=retry_delay(self.request.retries + 1)) from exc
    run = process_outcome_record(repo, outcome_id)
    if run.reconnect:
        reset_worker_repo()
    if run.status == "retry":
        raise self.retry(exc=OutcomeRetry(run.detail), countdown=run.retry_in)
    return run.as_dict(outcome_id)


def enqueue_outcome(outcome_id: int) -> bool:
    """Ask a worker to process this outcome. Returns False, and does not raise, when the queue
    cannot be reached: the outcome is already stored as 'pending', so it is not lost, and the
    sweep (step 3) will find it. The caller (the API) must not fail a request over that."""
    try:
        process_outcome.apply_async(args=[outcome_id], retry=False)
    except Exception as exc:
        log.warning("could not queue outcome %s (it stays pending): %s", outcome_id, exc)
        return False
    return True


@celery_app.task(name=SWEEP_TASK_NAME)
def sweep_outcomes() -> dict[str, int]:
    """Queue again every outcome stuck as 'pending'. Started every minute by `celery beat`.
    Safe to run twice at once: processing an outcome twice does nothing the second time."""
    return sweep_pending_outcomes(worker_repo(), datetime.now(UTC), enqueue=enqueue_outcome)
