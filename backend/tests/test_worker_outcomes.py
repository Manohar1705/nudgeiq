"""8d-3b step 2: the Celery task around the outcome work. No Redis needed.

The work itself is tested in test_outcome_processing.py. Here only the thin task is checked: it is
registered, it passes the right things on, and it turns "retry" into a Celery retry. These need the
`celery` package (the whole file is skipped without it). A real queue is NOT used.
"""

import pytest

pytest.importorskip("celery")

from app.worker import tasks  # noqa: E402
from app.worker.celery_app import OUTCOME_TASK_NAME, SWEEP_TASK_NAME, celery_app  # noqa: E402
from app.worker.outcomes import OutcomeRun  # noqa: E402


def test_the_task_is_registered_under_its_name_on_the_shared_app():
    assert OUTCOME_TASK_NAME == "nudgeiq.process_outcome"
    assert OUTCOME_TASK_NAME in celery_app.tasks
    assert tasks.process_outcome.name == OUTCOME_TASK_NAME


def test_the_task_keeps_its_result_in_the_database_not_in_redis():
    assert tasks.process_outcome.ignore_result is True
    assert tasks.process_outcome.max_retries == tasks.OUTCOME_MAX_RETRIES


def test_a_finished_outcome_returns_its_status(monkeypatch):
    monkeypatch.setattr(tasks, "worker_repo", lambda: object())
    monkeypatch.setattr(tasks, "process_outcome_record", lambda repo, oid: OutcomeRun("processed"))
    assert tasks.process_outcome.run(5) == {"outcome_id": 5, "status": "processed"}


def test_a_retry_status_becomes_a_celery_retry(monkeypatch):
    monkeypatch.setattr(tasks, "worker_repo", lambda: object())
    monkeypatch.setattr(
        tasks,
        "process_outcome_record",
        lambda repo, oid: OutcomeRun("retry", "lock timeout", retry_in=7),
    )
    # Called directly (not by a worker), Celery's retry raises the exception it was given.
    with pytest.raises(tasks.OutcomeRetry, match="lock timeout"):
        tasks.process_outcome.run(5)


def test_a_dead_connection_is_dropped_so_the_next_task_opens_a_new_one(monkeypatch):
    dropped = []
    monkeypatch.setattr(tasks, "worker_repo", lambda: object())
    monkeypatch.setattr(tasks, "reset_worker_repo", lambda: dropped.append(1))
    monkeypatch.setattr(
        tasks,
        "process_outcome_record",
        lambda repo, oid: OutcomeRun("retry", "gone", retry_in=5, reconnect=True),
    )
    with pytest.raises(tasks.OutcomeRetry):
        tasks.process_outcome.run(5)
    assert dropped == [1]


def test_no_database_means_a_retry_not_a_crash(monkeypatch):
    def down():
        raise RuntimeError("NUDGEIQ_DATABASE_URL is not set")

    monkeypatch.setattr(tasks, "worker_repo", down)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        tasks.process_outcome.run(5)  # called directly, retry re-raises the original error


def test_reset_worker_repo_closes_and_forgets_the_connection(monkeypatch):
    closed = []

    class Fake:
        def close(self):
            closed.append(1)

    monkeypatch.setattr(tasks, "_repo", Fake())
    tasks.reset_worker_repo()
    assert closed == [1] and tasks._repo is None
    tasks.reset_worker_repo()  # nothing left: harmless
    assert closed == [1]


def test_reset_worker_repo_survives_a_close_that_fails(monkeypatch):
    class Broken:
        def close(self):
            raise OSError("already dead")

    monkeypatch.setattr(tasks, "_repo", Broken())
    tasks.reset_worker_repo()
    assert tasks._repo is None


def test_enqueue_hands_the_outcome_id_to_the_queue(monkeypatch):
    sent = []
    monkeypatch.setattr(
        tasks.process_outcome, "apply_async", lambda **kw: sent.append(kw) or object()
    )
    assert tasks.enqueue_outcome(9) is True
    assert sent == [{"args": [9], "retry": False}]


def test_enqueue_does_not_raise_when_the_queue_is_down(monkeypatch):
    def down(**kwargs):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(tasks.process_outcome, "apply_async", down)
    assert tasks.enqueue_outcome(9) is False  # the outcome is stored; the sweep will find it


def test_the_sweep_task_is_registered_and_scheduled_every_minute():
    assert SWEEP_TASK_NAME in celery_app.tasks
    entry = celery_app.conf.beat_schedule["sweep-pending-outcomes"]
    assert entry["task"] == SWEEP_TASK_NAME and entry["schedule"] == 60.0


def test_the_sweep_task_queues_through_enqueue_outcome(monkeypatch):
    seen = {}

    def fake_sweep(repo, now, *, enqueue):
        seen["enqueue"] = enqueue
        return {"found": 0, "queued": 0, "not_queued": 0}

    monkeypatch.setattr(tasks, "worker_repo", lambda: object())
    monkeypatch.setattr(tasks, "sweep_pending_outcomes", fake_sweep)
    assert tasks.sweep_outcomes.run() == {"found": 0, "queued": 0, "not_queued": 0}
    assert seen["enqueue"] is tasks.enqueue_outcome
