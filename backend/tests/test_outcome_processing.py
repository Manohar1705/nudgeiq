"""8d-3b step 2: a stored outcome is turned into a reward, learning and a journey update.

Nothing here needs Redis or Celery: the work is plain Python (app/worker/outcomes.py). Tests that
use `repo` run on memory and on PostgreSQL (skipped without NUDGEIQ_TEST_DSN). The three that
raise real psycopg errors skip themselves when psycopg is not installed.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.agents.reward import compute_reward
from app.models import (
    Campaign,
    CampaignStatus,
    Decision,
    JourneyStatus,
    Outcome,
    OutcomeType,
)
from app.orchestrator import LogDispatcher, Orchestrator, message_funnel, uplift_summary
from app.worker.outcomes import (
    MAX_ATTEMPTS,
    OutcomeRun,
    campaign_config_for,
    is_collision,
    is_dead_connection,
    process_outcome_record,
    retry_delay,
    sweep_grace_from_env,
    sweep_pending_outcomes,
)

T = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
LATER = T + timedelta(hours=1)
FAR_FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture
def orch(repo, config, library):
    return Orchestrator(repo, config, library, seed=7, dispatcher=LogDispatcher())


@pytest.fixture
def decided(orch, repo, make_state, now):
    repo.save_customer(make_state(customer_id="c1"))
    d = orch.decide("c1", now)
    assert not d.chosen.is_wait
    return d


@pytest.fixture
def campaign(repo, config):
    """The campaign the decision belongs to, stored, as it is when the API runs."""
    repo.save_campaign(
        Campaign(
            campaign_id=config.use_case_id,
            status=CampaignStatus.ACTIVE,
            config=config,
            activated_at=T,
        )
    )


def of(decision: Decision, kind=OutcomeType.ORDER_PLACED, at=T) -> Outcome:
    return Outcome(
        decision_id=decision.decision_id,
        customer_id=decision.customer_id,
        outcome_type=kind,
        occurred_at=at,
    )


def reward_for(orch, decision, kinds):
    return compute_reward(decision.decision_id, decision.customer_id, kinds, orch.config.rewards)


def accept(orch, decision, kind=OutcomeType.ORDER_PLACED, at=T) -> int:
    return orch.accept_outcome(of(decision, kind, at)).record.outcome_id


def status_of(repo, outcome_id):
    record = repo.get_outcome(outcome_id)
    assert record is not None
    return record


# ------------------------------------------------------------------ list_outcome_records
def test_records_come_with_status_and_id_and_several_statuses_in_one_read(repo, make_state):
    repo.save_customer(make_state(customer_id="c1"))
    repo.save_decision(
        Decision(decision_id="d1", customer_id="c1", state=make_state(), created_at=T)
    )
    a = repo.save_outcome(of_raw(OutcomeType.CTA_CLICKED), status="pending").record
    b = repo.save_outcome(of_raw(OutcomeType.ORDER_PLACED, LATER), status="processed").record
    c = repo.save_outcome(of_raw(OutcomeType.UNSUBSCRIBED, LATER), status="failed").record

    everything = repo.list_outcome_records()
    assert [r.outcome_id for r in everything] == [a.outcome_id, b.outcome_id, c.outcome_id]
    assert [r.status for r in everything] == ["pending", "processed", "failed"]

    counted = repo.list_outcome_records(statuses=("pending", "processed"))
    assert [r.outcome_id for r in counted] == [a.outcome_id, b.outcome_id]  # failed is left out
    assert repo.list_outcome_records(statuses=("failed",))[0].outcome_id == c.outcome_id
    assert repo.list_outcome_records(statuses=()) == []

    assert len(repo.list_outcome_records(decision_id="d1")) == 3
    assert repo.list_outcome_records(decision_id="nope") == []
    assert len(repo.list_outcome_records(customer_id="c1", statuses=("pending",))) == 1
    with pytest.raises(ValueError, match="status"):
        repo.list_outcome_records(statuses=("done",))  # type: ignore[arg-type]


def of_raw(kind, at=T) -> Outcome:
    return Outcome(decision_id="d1", customer_id="c1", outcome_type=kind, occurred_at=at)


# ------------------------------------------------------------------ the synchronous path
def test_record_outcome_stores_pending_first_and_marks_processed_at_the_end(
    orch, repo, decided, monkeypatch
):
    seen = []
    real = orch.outcomes.apply

    def spy(decision, outcome, now):
        seen.append([r.status for r in repo.list_outcome_records()])
        return real(decision, outcome, now)

    monkeypatch.setattr(orch.outcomes, "apply", spy)
    result = orch.record_outcome(of(decided))
    assert seen == [["pending"]]  # while the work ran, the outcome was not yet 'processed'
    records = repo.list_outcome_records()
    assert [r.status for r in records] == ["processed"] and records[0].processed_at is not None
    assert result.reward == reward_for(orch, decided, [OutcomeType.ORDER_PLACED])


def test_a_crash_while_applying_leaves_the_outcome_pending_and_the_worker_finishes_it(
    orch, repo, decided, campaign, monkeypatch
):
    def boom(*args):
        raise RuntimeError("process died here")

    monkeypatch.setattr(orch.outcomes, "apply", boom)
    with pytest.raises(RuntimeError):
        orch.record_outcome(of(decided))
    left = repo.list_pending_outcomes(FAR_FUTURE)
    assert len(left) == 1  # not claimed as processed

    run = process_outcome_record(repo, left[0].outcome_id)
    assert run.status == "processed"
    assert repo.get_reward(decided.decision_id) == reward_for(
        orch, decided, [OutcomeType.ORDER_PLACED]
    )
    assert repo.list_pending_outcomes(FAR_FUTURE) == []


# ------------------------------------------------------------------ the worker, normal cases
def test_the_worker_turns_a_pending_outcome_into_a_processed_one(orch, repo, decided, campaign):
    version = repo.policy_version(orch.config.use_case_id)
    outcome_id = accept(orch, decided)
    assert repo.get_reward(decided.decision_id) is None  # accepting does no work

    run = process_outcome_record(repo, outcome_id)
    assert run.status == "processed"
    record = status_of(repo, outcome_id)
    assert record.status == "processed" and record.processed_at is not None
    assert repo.get_reward(decided.decision_id) == reward_for(
        orch, decided, [OutcomeType.ORDER_PLACED]
    )
    assert repo.policy_version(orch.config.use_case_id) > version  # it learned
    journey = repo.get_journey("c1")
    assert journey is not None and journey.status == JourneyStatus.CONVERTED


def test_a_second_delivery_does_nothing(orch, repo, decided, campaign):
    outcome_id = accept(orch, decided)
    assert process_outcome_record(repo, outcome_id).status == "processed"
    version = repo.policy_version(orch.config.use_case_id)
    reward = repo.get_reward(decided.decision_id)

    again = process_outcome_record(repo, outcome_id)
    assert again.status == "already_processed"
    assert repo.policy_version(orch.config.use_case_id) == version
    assert repo.get_reward(decided.decision_id) == reward


def test_running_the_work_again_after_a_crash_before_the_mark_learns_nothing_twice(
    orch, repo, decided, campaign
):
    """The worker died after rewarding and learning but before it wrote 'processed'."""
    outcome_id = accept(orch, decided)
    assert process_outcome_record(repo, outcome_id).status == "processed"
    version = repo.policy_version(orch.config.use_case_id)
    reward = repo.get_reward(decided.decision_id)
    repo.mark_outcome(outcome_id, "pending", T)  # as if the last step never happened

    assert process_outcome_record(repo, outcome_id).status == "processed"
    assert repo.policy_version(orch.config.use_case_id) == version
    assert repo.get_reward(decided.decision_id) == reward


def test_two_outcomes_for_one_decision_end_with_the_combined_reward(orch, repo, decided, campaign):
    click = accept(orch, decided, OutcomeType.CTA_CLICKED)
    assert process_outcome_record(repo, click).status == "processed"
    assert repo.get_reward(decided.decision_id) == reward_for(
        orch, decided, [OutcomeType.CTA_CLICKED]
    )

    order = accept(orch, decided, OutcomeType.ORDER_PLACED, LATER)
    assert process_outcome_record(repo, order).status == "processed"
    assert repo.get_reward(decided.decision_id) == reward_for(
        orch, decided, [OutcomeType.CTA_CLICKED, OutcomeType.ORDER_PLACED]
    )


def test_either_order_of_processing_gives_the_same_reward(orch, repo, decided, campaign):
    click = accept(orch, decided, OutcomeType.CTA_CLICKED)
    order = accept(orch, decided, OutcomeType.ORDER_PLACED, LATER)
    combined = reward_for(orch, decided, [OutcomeType.CTA_CLICKED, OutcomeType.ORDER_PLACED])

    assert process_outcome_record(repo, order).status == "processed"
    assert repo.get_reward(decided.decision_id) == combined  # the pending click already counts
    version = repo.policy_version(orch.config.use_case_id)
    assert process_outcome_record(repo, click).status == "processed"
    assert repo.get_reward(decided.decision_id) == combined
    assert repo.policy_version(orch.config.use_case_id) == version


def test_an_outcome_that_arrives_while_another_is_processed_is_not_lost_from_the_reward(
    orch, repo, decided, campaign, monkeypatch
):
    """Another worker stores (and finishes) a second outcome between our read and our write.
    The first worker must end with the combined reward, not leave its older, lower one."""
    click = accept(orch, decided, OutcomeType.CTA_CLICKED)
    real = repo.save_reward
    fired: list[int] = []

    def save_reward(reward):
        real(reward)
        if not fired:
            fired.append(1)
            repo.save_outcome(of(decided, OutcomeType.ORDER_PLACED, LATER), status="pending")

    monkeypatch.setattr(repo, "save_reward", save_reward)
    assert process_outcome_record(repo, click).status == "processed"
    assert repo.get_reward(decided.decision_id) == reward_for(
        orch, decided, [OutcomeType.CTA_CLICKED, OutcomeType.ORDER_PLACED]
    )


def test_a_failed_outcome_changes_neither_reward_nor_journey(orch, repo, decided, campaign):
    repo.save_outcome(of(decided, OutcomeType.ORDER_PLACED), status="failed")
    click = accept(orch, decided, OutcomeType.CTA_CLICKED)
    assert process_outcome_record(repo, click).status == "processed"
    assert repo.get_reward(decided.decision_id) == reward_for(
        orch, decided, [OutcomeType.CTA_CLICKED]
    )
    journey = repo.get_journey("c1")
    assert journey is not None and journey.status == JourneyStatus.ACTIVE  # no order counted


def test_an_unsubscribe_stops_contact_and_closes_the_journey(orch, repo, decided, campaign):
    outcome_id = accept(orch, decided, OutcomeType.UNSUBSCRIBED)
    assert process_outcome_record(repo, outcome_id).status == "processed"
    customer = repo.get_customer("c1")
    assert customer is not None and not customer.contactable
    journey = repo.get_journey("c1")
    assert journey is not None and journey.status != JourneyStatus.ACTIVE


# ------------------------------------------------------------------ the worker, failures
def test_an_unknown_outcome_id_is_reported_not_retried(repo):
    run = process_outcome_record(repo, 987654)
    assert run.status == "missing" and run.retry_in == 0


def test_a_permanent_problem_marks_the_outcome_failed_and_is_not_retried(
    orch,
    repo,
    decided,  # no `campaign`: the decision's campaign is not stored
):
    outcome_id = accept(orch, decided)
    run = process_outcome_record(repo, outcome_id)
    assert run.status == "failed" and run.retry_in == 0
    record = status_of(repo, outcome_id)
    assert record.status == "failed" and "campaign" in record.last_error
    assert record.attempts == 0  # not a retried error, so no try was counted
    assert repo.get_reward(decided.decision_id) is None  # and nothing was half-applied

    assert process_outcome_record(repo, outcome_id).status == "already_failed"


def test_a_failed_outcome_can_be_put_back_and_then_succeeds(orch, repo, decided):
    outcome_id = accept(orch, decided)
    assert process_outcome_record(repo, outcome_id).status == "failed"
    repo.save_campaign(
        Campaign(
            campaign_id=orch.config.use_case_id,
            status=CampaignStatus.ACTIVE,
            config=orch.config,
            activated_at=T,
        )
    )
    repo.mark_outcome(outcome_id, "pending", T)
    assert process_outcome_record(repo, outcome_id).status == "processed"


def test_an_unexpected_error_is_retried_with_a_growing_delay_then_given_up(
    orch, repo, decided, campaign, monkeypatch
):
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(repo, "credit_policy", boom)
    outcome_id = accept(orch, decided)

    first = process_outcome_record(repo, outcome_id, max_attempts=3)
    assert (first.status, first.retry_in) == ("retry", 5)
    record = status_of(repo, outcome_id)
    assert record.status == "pending" and record.attempts == 1
    assert "disk on fire" in record.last_error

    second = process_outcome_record(repo, outcome_id, max_attempts=3)
    assert (second.status, second.retry_in) == ("retry", 10)
    assert status_of(repo, outcome_id).attempts == 2

    third = process_outcome_record(repo, outcome_id, max_attempts=3)
    assert third.status == "failed" and "gave up after 3 tries" in third.detail
    record = status_of(repo, outcome_id)
    assert record.status == "failed" and "disk on fire" in record.last_error
    assert repo.get_reward(decided.decision_id) is None


def test_a_try_that_fails_once_and_then_works_ends_processed(
    orch, repo, decided, campaign, monkeypatch
):
    real = repo.credit_policy
    calls: list[int] = []

    def flaky(*args, **kwargs):
        if not calls:
            calls.append(1)
            raise RuntimeError("blip")
        return real(*args, **kwargs)

    monkeypatch.setattr(repo, "credit_policy", flaky)
    outcome_id = accept(orch, decided)
    assert process_outcome_record(repo, outcome_id).status == "retry"
    assert process_outcome_record(repo, outcome_id).status == "processed"
    record = status_of(repo, outcome_id)
    assert record.status == "processed" and record.attempts == 1  # the one failed try stays counted


def test_when_the_database_cannot_even_count_the_try_the_outcome_just_stays_pending(
    orch, repo, decided, campaign, monkeypatch
):
    def boom(*args, **kwargs):
        raise RuntimeError("first failure")

    def down(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(repo, "credit_policy", boom)
    monkeypatch.setattr(repo, "count_outcome_attempt", down)
    outcome_id = accept(orch, decided)
    run = process_outcome_record(repo, outcome_id)  # does not raise
    assert run.status == "retry" and run.retry_in == 5 and run.reconnect
    assert status_of(repo, outcome_id).status == "pending"


def test_a_decision_without_a_campaign_id_uses_the_live_campaign(orch, repo, decided, campaign):
    repo.save_decision(decided.model_copy(update={"use_case_id": None}))
    outcome_id = accept(orch, decided)
    assert process_outcome_record(repo, outcome_id).status == "processed"


def test_a_decision_without_a_campaign_id_and_no_live_campaign_is_retried(orch, repo, decided):
    repo.save_decision(decided.model_copy(update={"use_case_id": None}))
    outcome_id = accept(orch, decided)
    run = process_outcome_record(repo, outcome_id)
    assert run.status == "retry"  # activating a campaign would fix it, so it is not failed
    assert status_of(repo, outcome_id).attempts == 1


def test_campaign_config_uses_the_campaign_that_made_the_decision(orch, repo, decided, campaign):
    other = orch.config.model_copy(update={"use_case_id": "other"})
    repo.save_campaign(
        Campaign(campaign_id="other", status=CampaignStatus.ARCHIVED, config=other, archived_at=T)
    )
    assert campaign_config_for(repo, decided).use_case_id == orch.config.use_case_id
    moved = decided.model_copy(update={"use_case_id": "other"})
    assert campaign_config_for(repo, moved).use_case_id == "other"  # a swap does not matter


# ------------------------------------------------------------------ real database errors
def test_a_lock_timeout_is_retried_without_a_new_connection(
    orch, repo, decided, campaign, monkeypatch
):
    psycopg = pytest.importorskip("psycopg")

    def lock_timeout(*args, **kwargs):
        raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")

    monkeypatch.setattr(repo, "credit_policy", lock_timeout)
    outcome_id = accept(orch, decided)
    run = process_outcome_record(repo, outcome_id)
    assert run.status == "retry" and not run.reconnect
    assert status_of(repo, outcome_id).attempts == 1
    assert is_collision(psycopg.errors.DeadlockDetected("deadlock detected"))


def test_a_lost_connection_is_retried_and_asks_for_a_new_connection(
    orch, repo, decided, campaign, monkeypatch
):
    psycopg = pytest.importorskip("psycopg")

    def gone(*args, **kwargs):
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(repo, "credit_policy", gone)
    outcome_id = accept(orch, decided)
    run = process_outcome_record(repo, outcome_id)
    assert run.status == "retry" and run.reconnect
    assert is_dead_connection(psycopg.InterfaceError("the connection is closed"))


def test_a_malformed_stored_policy_fails_the_outcome_for_good(orch, repo, decided, campaign):
    repo.save_policy(orch.config.use_case_id, {"cells": "not a list"})
    outcome_id = accept(orch, decided)
    run = process_outcome_record(repo, outcome_id)
    assert run.status == "failed" and "PolicyError" in run.detail
    assert status_of(repo, outcome_id).status == "failed"


# ------------------------------------------------------------------ small pieces
def test_retry_delay_doubles_up_to_a_cap():
    assert [retry_delay(i) for i in range(1, 9)] == [5, 10, 20, 40, 80, 160, 300, 300]
    assert retry_delay(0) == 5 and retry_delay(50) == 300  # no overflow for a huge count


def test_the_attempt_limit_is_finite():
    assert 1 < MAX_ATTEMPTS <= 20


def test_a_run_describes_itself_for_the_task_result():
    assert OutcomeRun("processed").as_dict(7) == {"outcome_id": 7, "status": "processed"}
    assert OutcomeRun("failed", "why").as_dict(7)["detail"] == "why"


# ------------------------------------------------------------------ the numbers shown
def test_pending_and_failed_outcomes_do_not_count_in_the_numbers(orch, repo, decided, campaign):
    assert decided.variant_id is not None
    outcome_id = accept(orch, decided)
    repo.save_outcome(of(decided, OutcomeType.ORDER_PLACED), status="failed")

    summary = uplift_summary(repo)
    assert summary.treated.decisions == 1 and summary.treated.resolved == 0
    assert sum(row.orders for row in message_funnel(repo)) == 0

    assert process_outcome_record(repo, outcome_id).status == "processed"
    summary = uplift_summary(repo)
    assert summary.treated.resolved == 1 and summary.treated.orders == 1
    assert sum(row.orders for row in message_funnel(repo)) == 1


# ------------------------------------------------------------------ the sweep (step 3)
class Queue:
    """A stand-in for the real queue: remembers what was queued, can be switched off."""

    def __init__(self, up: bool = True) -> None:
        self.up = up
        self.queued: list[int] = []

    def __call__(self, outcome_id: int) -> bool:
        if not self.up:
            return False
        self.queued.append(outcome_id)
        return True


def test_the_sweep_queues_an_outcome_that_stayed_pending(orch, repo, decided):
    outcome_id = accept(orch, decided)
    queue = Queue()
    counts = sweep_pending_outcomes(repo, FAR_FUTURE, enqueue=queue)
    assert queue.queued == [outcome_id]
    assert counts == {"found": 1, "queued": 1, "not_queued": 0}


def test_the_sweep_leaves_a_recent_outcome_alone(orch, repo, decided):
    accept(orch, decided)
    queue = Queue()
    now = datetime.now(UTC)  # received a moment ago: still inside the grace period
    counts = sweep_pending_outcomes(repo, now, enqueue=queue, grace=timedelta(minutes=10))
    assert queue.queued == [] and counts["found"] == 0


def test_the_sweep_ignores_processed_and_failed_outcomes(orch, repo, decided):
    done = accept(orch, decided)
    broken = accept(orch, decided, OutcomeType.MESSAGE_OPENED)
    repo.mark_outcome(done, "processed", T)
    repo.mark_outcome(broken, "failed", T, "bad data")
    queue = Queue()
    assert sweep_pending_outcomes(repo, FAR_FUTURE, enqueue=queue)["found"] == 0
    assert queue.queued == []


def test_when_the_queue_is_down_the_sweep_stops_and_the_outcomes_stay_pending(orch, repo, decided):
    kinds = (OutcomeType.MESSAGE_OPENED, OutcomeType.CTA_CLICKED)
    ids = [accept(orch, decided, kind) for kind in kinds]
    counts = sweep_pending_outcomes(repo, FAR_FUTURE, enqueue=Queue(up=False))
    assert counts == {"found": 2, "queued": 0, "not_queued": 2}
    assert all(status_of(repo, i).status == "pending" for i in ids)
    # the queue comes back: the next pass picks both up
    queue = Queue()
    assert sweep_pending_outcomes(repo, FAR_FUTURE, enqueue=queue)["queued"] == 2
    assert queue.queued == ids


def test_the_sweep_takes_the_oldest_first_and_at_most_the_limit(orch, repo, decided):
    kinds = (OutcomeType.MESSAGE_OPENED, OutcomeType.CTA_CLICKED)
    ids = [accept(orch, decided, kind) for kind in kinds]
    queue = Queue()
    counts = sweep_pending_outcomes(repo, FAR_FUTURE, enqueue=queue, limit=1)
    assert queue.queued == ids[:1] and counts["found"] == 1


def test_a_swept_outcome_ends_processed_and_a_second_sweep_finds_nothing(
    orch, repo, decided, campaign
):
    outcome_id = accept(orch, decided)
    # the "queue" runs the worker's work straight away
    sweep_pending_outcomes(
        repo,
        FAR_FUTURE,
        enqueue=lambda oid: process_outcome_record(repo, oid).status == "processed",
    )
    assert status_of(repo, outcome_id).status == "processed"
    queue = Queue()
    assert sweep_pending_outcomes(repo, FAR_FUTURE, enqueue=queue)["found"] == 0


def test_the_sweep_grace_comes_from_the_environment_and_is_checked():
    assert sweep_grace_from_env({}) == timedelta(minutes=10)
    assert sweep_grace_from_env({"NUDGEIQ_OUTCOME_SWEEP_GRACE_SECONDS": "120"}) == timedelta(
        seconds=120
    )
    for bad in ("soon", "30", "-5"):
        with pytest.raises(ValueError, match="NUDGEIQ_OUTCOME_SWEEP_GRACE_SECONDS"):
            sweep_grace_from_env({"NUDGEIQ_OUTCOME_SWEEP_GRACE_SECONDS": bad})
