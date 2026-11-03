from datetime import timedelta

import pytest

from app.agents.state_agent import CustomerNotFoundError, stable_holdout
from app.models import JourneyStatus, Outcome, OutcomeType, TriggerEvent
from app.orchestrator import (
    DecisionNotFoundError,
    InvalidEventError,
    LogDispatcher,
    Orchestrator,
    OutcomeMismatchError,
    uplift_summary,
)


@pytest.fixture
def orch(repo, config, library):
    return Orchestrator(repo, config, library, seed=7, dispatcher=LogDispatcher())


def outcome(decision, kind, at):
    return Outcome(
        decision_id=decision.decision_id,
        customer_id=decision.customer_id,
        outcome_type=kind,
        occurred_at=at,
    )


def nudged(orch, repo, make_state, now, cid="c1", **kw):
    repo.save_customer(make_state(customer_id=cid, **kw))
    d = orch.decide(cid, now)
    assert not d.chosen.is_wait, [s.note for s in d.steps]
    return d


# ------------------------------------------------------------------ decide
def test_decide_builds_a_message_and_records_everything(orch, repo, make_state, now):
    d = nudged(orch, repo, make_state, now)
    assert d.message and d.variant_id and d.scheduled_for and d.bucket
    assert [s.agent for s in d.steps][-2:] == ["message", "dispatch"]
    assert len(d.candidates) <= 10
    stored = repo.get_decision(d.decision_id)
    assert stored is not None and stored.message == d.message and stored.steps == d.steps
    assert repo.get_contact_times("c1") == [d.scheduled_for]
    assert len(orch.dispatcher.sent) == 1  # type: ignore[attr-defined]


def test_unknown_customer_raises(orch, now):
    with pytest.raises(CustomerNotFoundError):
        orch.decide("ghost", now)


def test_second_decision_inside_cooldown_waits(orch, repo, make_state, now):
    nudged(orch, repo, make_state, now)
    again = orch.decide("c1", now + timedelta(hours=1))
    assert again.chosen.is_wait
    assert any("cool-down" in s.note for s in again.steps)


def test_do_not_disturb_is_suppressed(orch, repo, make_state, now):
    repo.save_customer(make_state(dnd=True))
    d = orch.decide("c1", now)
    assert d.chosen.is_wait and any("suppressed" in s.note for s in d.steps)
    assert not orch.dispatcher.sent  # type: ignore[attr-defined]


def test_holdout_is_never_nudged(orch, repo, make_state, now):
    cid = next(f"h{i}" for i in range(500) if stable_holdout(f"h{i}", 0.1))
    repo.save_customer(make_state(customer_id=cid))
    d = orch.decide(cid, now)
    assert d.is_holdout and d.chosen.is_wait and d.message is None
    assert repo.get_contact_times(cid) == []


# ---------------------------------------------------------------- outcomes
def test_rewards_accumulate_but_policy_counts_one_trial(orch, repo, make_state, now):
    d = nudged(orch, repo, make_state, now)
    at = now + timedelta(hours=30)
    r1 = orch.record_outcome(outcome(d, OutcomeType.MESSAGE_OPENED, at))
    r2 = orch.record_outcome(outcome(d, OutcomeType.CTA_CLICKED, at))
    assert (r1.reward.points, r2.reward.points) == (2, 7)
    assert r1.policy_updated and r2.next_step == "wait_for_outcome"
    assert orch.master.times_tried(d.bucket, d.chosen) == 1
    assert orch.master.cells_learned == 1
    # score reflects the full 7 points, not 2 + 7
    prior, w = orch.master.optimistic_init, orch.master.prior_weight
    assert orch.master.score(d.bucket, d.chosen) == pytest.approx((w * prior + 7) / (w + 1))


def test_order_closes_the_journey_and_stops_nudges(orch, repo, make_state, now):
    d = nudged(orch, repo, make_state, now)
    res = orch.record_outcome(outcome(d, OutcomeType.ORDER_PLACED, now + timedelta(hours=30)))
    assert res.journey.status == JourneyStatus.CONVERTED and res.next_step == "closed"
    assert res.reward.points == 40
    later = orch.decide("c1", now + timedelta(days=5))
    assert later.chosen.is_wait and any("converted" in s.note for s in later.steps)


def test_unsubscribe_exits_and_blocks_contact(orch, repo, make_state, now):
    d = nudged(orch, repo, make_state, now)
    res = orch.record_outcome(outcome(d, OutcomeType.UNSUBSCRIBED, now + timedelta(hours=30)))
    assert res.journey.status == JourneyStatus.EXITED and res.reward.points == -20
    customer = repo.get_customer("c1")
    assert customer is not None and customer.contactable is False


def test_timeout_triggers_retry_and_retry_counts_timings_from_now(orch, repo, make_state, now):
    d = nudged(orch, repo, make_state, now)
    res = orch.record_outcome(outcome(d, OutcomeType.IGNORED, now + timedelta(hours=48)))
    assert res.reward.points == -10 and res.next_step == "retry"
    retry = orch.decide("c1", now + timedelta(hours=49))
    assert not retry.chosen.is_wait
    assert retry.scheduled_for is not None and retry.scheduled_for > now + timedelta(hours=49)
    assert retry.state.timing_anchor_minutes == 0


def test_late_timeout_does_not_cancel_engagement(orch, repo, make_state, now):
    d = nudged(orch, repo, make_state, now)
    orch.record_outcome(outcome(d, OutcomeType.MESSAGE_OPENED, now + timedelta(hours=30)))
    res = orch.record_outcome(outcome(d, OutcomeType.IGNORED, now + timedelta(hours=60)))
    assert res.reward.points == 2


def test_journey_expires_after_exit_window(orch, repo, make_state, now):
    d = nudged(orch, repo, make_state, now)
    res = orch.record_outcome(outcome(d, OutcomeType.IGNORED, now + timedelta(days=15)))
    assert res.journey.status == JourneyStatus.EXITED and res.next_step == "closed"


def test_holdout_outcomes_are_stored_but_never_learned(orch, repo, make_state, now):
    cid = next(f"h{i}" for i in range(500) if stable_holdout(f"h{i}", 0.1))
    repo.save_customer(make_state(customer_id=cid))
    d = orch.decide(cid, now)
    res = orch.record_outcome(outcome(d, OutcomeType.ORDER_PLACED, now + timedelta(days=1)))
    assert not res.policy_updated and orch.master.cells_learned == 0
    assert res.journey.status == JourneyStatus.CONVERTED


def test_outcome_errors(orch, repo, make_state, now):
    d = nudged(orch, repo, make_state, now)
    with pytest.raises(DecisionNotFoundError):
        orch.record_outcome(
            Outcome(decision_id="nope", customer_id="c1", outcome_type=OutcomeType.ORDER_PLACED)
        )
    with pytest.raises(OutcomeMismatchError):
        orch.record_outcome(
            Outcome(
                decision_id=d.decision_id,
                customer_id="someone_else",
                outcome_type=OutcomeType.ORDER_PLACED,
            )
        )


def test_policy_survives_a_restart(orch, repo, config, library, make_state, now):
    d = nudged(orch, repo, make_state, now)
    orch.record_outcome(outcome(d, OutcomeType.ORDER_PLACED, now + timedelta(hours=30)))
    restarted = Orchestrator(repo, config, library, seed=7)
    assert restarted.master.times_tried(d.bucket, d.chosen) == 1


# ------------------------------------------------------------------ events
def test_event_starts_a_fresh_journey_with_new_cart(orch, repo, make_state, now):
    d = nudged(orch, repo, make_state, now)
    orch.record_outcome(outcome(d, OutcomeType.ORDER_PLACED, now + timedelta(hours=30)))
    event = TriggerEvent(
        event_id="e1",
        customer_id="c1",
        event_type="cart_abandoned",
        occurred_at=now + timedelta(days=20),
        payload={"cart_value": 80.0, "abandoned_minutes": 45},
    )
    new = orch.handle_event(event)
    journey = repo.get_journey("c1")
    assert journey is not None and journey.status == JourneyStatus.ACTIVE
    assert new.state.cart_value == 80.0 and not new.chosen.is_wait


def test_event_validation(orch, repo, make_state, now):
    repo.save_customer(make_state())
    bad_type = TriggerEvent(event_id="e", customer_id="c1", event_type="page_view")
    with pytest.raises(InvalidEventError):
        orch.handle_event(bad_type)
    bad_payload = TriggerEvent(
        event_id="e", customer_id="c1", event_type="cart_abandoned", payload={"cart_value": -5}
    )
    with pytest.raises(InvalidEventError):
        orch.handle_event(bad_payload)
    with pytest.raises(CustomerNotFoundError):
        orch.handle_event(
            TriggerEvent(event_id="e", customer_id="ghost", event_type="cart_abandoned")
        )


# ---------------------------------------------------------------- insights
def test_uplift_compares_treated_with_holdout(orch, repo, make_state, now):
    d = nudged(orch, repo, make_state, now)
    orch.record_outcome(outcome(d, OutcomeType.ORDER_PLACED, now + timedelta(hours=30)))
    cid = next(f"h{i}" for i in range(500) if stable_holdout(f"h{i}", 0.1))
    repo.save_customer(make_state(customer_id=cid))
    h = orch.decide(cid, now)
    orch.record_outcome(outcome(h, OutcomeType.IGNORED, now + timedelta(days=2)))
    s = uplift_summary(repo)
    assert s.treated.orders == 1 and s.treated.conversion == 1.0
    assert s.holdout.resolved == 1 and s.holdout.conversion == 0.0
    assert s.uplift is None  # holdout conversion is zero, so uplift is undefined
