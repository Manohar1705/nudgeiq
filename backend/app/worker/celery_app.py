"""The Celery app: one place that says where Redis is and how tasks are run.

  NUDGEIQ_REDIS_URL   redis://host:port/db (default redis://localhost:6379/0).
                      rediss:// (TLS) also works. Anything else stops start-up with a clear error.

The same Redis is the queue and the place results are kept. Results expire after an hour, so
Redis does not fill up with old task records.

  NUDGEIQ_DISPATCH_INTERVAL_SECONDS   how often due sends are looked for (default 30, minimum 5).

Scheduled sends (8d-2) do NOT use Celery's delayed delivery (eta). The schedule lives in the
database and a periodic task, started by `celery beat`, delivers what is due. Delayed tasks
would hold a worker slot for days (a T+3d send would block everything behind it) and Redis would
re-deliver them after its visibility timeout (1 hour by default).

Choices that matter for outcome processing (8d-3b):
  * acks_late: a task is removed from the queue only after it finishes, so a worker that dies
    mid-task does not lose it. The price is that a task can run twice, so tasks must be safe to
    repeat (the send guard in the database does that for sends).
  * prefetch 1: a worker takes one task at a time, so a slow task does not hold others hostage.
  * JSON only: no pickle, so a poisoned message cannot run code.
"""

import os
from collections.abc import Mapping

from celery import Celery

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
QUEUE_NAME = "nudgeiq"
RESULT_TTL_SECONDS = 3600


def redis_url_from_env(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    raw = (source.get("NUDGEIQ_REDIS_URL") or "").strip()
    if not raw:
        return DEFAULT_REDIS_URL
    if not raw.startswith(("redis://", "rediss://")):
        raise ValueError(
            "NUDGEIQ_REDIS_URL must start with redis:// or rediss:// "
            "(for example redis://localhost:6379/0)"
        )
    return raw


DEFAULT_DISPATCH_INTERVAL = 30
MIN_DISPATCH_INTERVAL = 5
DISPATCH_TASK_NAME = "nudgeiq.dispatch_due"
OUTCOME_TASK_NAME = "nudgeiq.process_outcome"  # 8d-3b: reward and learn from one outcome
SWEEP_TASK_NAME = "nudgeiq.sweep_outcomes"  # 8d-3b step 3: queue stuck pending outcomes again
SWEEP_INTERVAL_SECONDS = 60.0


def dispatch_interval_from_env(env: Mapping[str, str] | None = None) -> int:
    source = os.environ if env is None else env
    raw = (source.get("NUDGEIQ_DISPATCH_INTERVAL_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_DISPATCH_INTERVAL
    try:
        seconds = int(raw)
    except ValueError:
        seconds = -1
    if seconds < MIN_DISPATCH_INTERVAL:
        raise ValueError(
            f"NUDGEIQ_DISPATCH_INTERVAL_SECONDS must be a whole number of seconds, "
            f"{MIN_DISPATCH_INTERVAL} or more"
        )
    return seconds


def create_celery_app(redis_url: str | None = None, dispatch_interval: int | None = None) -> Celery:
    url = redis_url or redis_url_from_env()
    interval = dispatch_interval or dispatch_interval_from_env()
    app = Celery("nudgeiq", broker=url, backend=url, include=["app.worker.tasks"])
    app.conf.update(
        task_default_queue=QUEUE_NAME,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        result_expires=RESULT_TTL_SECONDS,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True,
        timezone="UTC",
        enable_utc=True,
        worker_hijack_root_logger=False,  # keep the app's own logging set-up
        beat_schedule={
            "dispatch-due-sends": {
                "task": DISPATCH_TASK_NAME,
                "schedule": float(interval),
                # a pass that waited longer than two intervals is stale: the next one covers it
                "options": {"expires": float(interval * 2)},
            },
            "sweep-pending-outcomes": {
                "task": SWEEP_TASK_NAME,
                "schedule": SWEEP_INTERVAL_SECONDS,
                "options": {"expires": SWEEP_INTERVAL_SECONDS * 2},
            },
        },
    )
    return app


celery_app = create_celery_app()
