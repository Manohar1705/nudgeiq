"""Contract tests: every repository (memory, PostgreSQL) must behave the same way."""

from datetime import UTC, datetime

import pytest

from app.models import (
    Action,
    Channel,
    Decision,
    Journey,
    JourneyStatus,
    Outcome,
    OutcomeType,
    RewardRecord,
)
from app.repository import JsonFileRepository

T = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


def _decision(make_state, did="d1", cid="c1", minute=0):
    return Decision(
        decision_id=did,
        customer_id=cid,
        state=make_state(customer_id=cid),
        created_at=T.replace(minute=minute),
    )


def _with_customers(repo, make_state, *ids):
    for cid in ids:
        repo.save_customer(make_state(customer_id=cid))


def test_customer_round_trip_and_update(repo, make_state):
    repo.save_customer(make_state(signals={"loyalty_tier": "circle", "card_holder": True}))
    got = repo.get_customer("c1")
    assert got is not None and got.signals == {"loyalty_tier": "circle", "card_holder": True}
    assert got.preferred_channel == Channel.PUSH
    repo.save_customer(make_state(cart_value=55.5, dnd=True))
    again = repo.get_customer("c1")
    assert again is not None and again.cart_value == 55.5 and again.dnd is True
    assert repo.get_customer("ghost") is None
    assert len(repo.list_customers()) == 1


def test_contacts_come_back_in_time_order(repo, make_state):
    _with_customers(repo, make_state, "c1")
    repo.record_contact("c1", T.replace(hour=15))
    repo.record_contact("c1", T)
    assert repo.get_contact_times("c1") == [T, T.replace(hour=15)]
    assert repo.get_contact_times("nobody") == []


def test_journey_round_trip(repo, make_state):
    _with_customers(repo, make_state, "c1")
    assert repo.get_journey("c1") is None
    repo.save_journey(Journey(customer_id="c1", started_at=T, base_abandoned_minutes=35))
    repo.save_journey(
        Journey(
            customer_id="c1",
            started_at=T,
            base_abandoned_minutes=35,
            nudges_sent=2,
            status=JourneyStatus.CONVERTED,
            closed_reason="order placed",
            closed_at=T,
        )
    )
    j = repo.get_journey("c1")
    assert j is not None and j.status == JourneyStatus.CONVERTED and j.nudges_sent == 2


def test_decision_round_trip_keeps_every_field(repo, make_state):
    _with_customers(repo, make_state, "c1")
    d = _decision(make_state)
    d.chosen = Action(channel=Channel.SMS, timing="T+24h", offer="percent_off", lever="x")
    d.explored, d.bucket, d.scheduled_for = True, "push|member|high", T.replace(hour=12)
    d.variant_id, d.message, d.blocked = "v1", "Hello", ["a|b", "c|d"]
    d.use_case_id, d.campaign_version, d.template_id = "target_cart", 3, "t_push_still_there"
    d.log("guardrail", "ok")
    repo.save_decision(d)
    got = repo.get_decision("d1")
    assert got is not None
    assert got.chosen == d.chosen and got.scheduled_for == d.scheduled_for
    assert (got.explored, got.bucket, got.message) == (True, "push|member|high", "Hello")
    assert got.blocked == ["a|b", "c|d"] and got.steps == d.steps
    assert got.state == d.state
    assert (got.use_case_id, got.campaign_version) == ("target_cart", 3)
    assert got.template_id == "t_push_still_there"
    assert repo.get_decision("nope") is None


def test_wait_decision_round_trip(repo, make_state):
    _with_customers(repo, make_state, "c1")
    repo.save_decision(_decision(make_state))
    got = repo.get_decision("d1")
    assert got is not None and got.chosen.is_wait


def test_decisions_are_listed_newest_first_and_filtered(repo, make_state):
    _with_customers(repo, make_state, "c1", "c2")
    repo.save_decision(_decision(make_state, "d1", "c1", 1))
    repo.save_decision(_decision(make_state, "d2", "c2", 2))
    repo.save_decision(_decision(make_state, "d3", "c1", 3))
    assert [d.decision_id for d in repo.list_decisions()] == ["d3", "d2", "d1"]
    assert [d.decision_id for d in repo.list_decisions("c1")] == ["d3", "d1"]
    assert len(repo.list_decisions(limit=1)) == 1
    assert len(repo.list_decisions(limit=None)) == 3


def test_outcomes_filter_by_decision_and_customer(repo, make_state):
    _with_customers(repo, make_state, "c1", "c2")
    repo.save_decision(_decision(make_state, "d1", "c1"))
    repo.save_decision(_decision(make_state, "d2", "c2"))
    kinds = (OutcomeType.MESSAGE_OPENED, OutcomeType.ORDER_PLACED)
    repo.save_outcome(
        Outcome(decision_id="d1", customer_id="c1", outcome_type=kinds[0], occurred_at=T)
    )
    repo.save_outcome(
        Outcome(
            decision_id="d2",
            customer_id="c2",
            outcome_type=kinds[1],
            occurred_at=T,
            payload={"order_value": 99.5},
        )
    )
    assert len(repo.list_outcomes()) == 2
    assert [o.outcome_type for o in repo.list_outcomes(decision_id="d1")] == [kinds[0]]
    only = repo.list_outcomes(customer_id="c2")
    assert len(only) == 1 and only[0].payload == {"order_value": 99.5}


def test_outcomes_are_returned_oldest_first(repo, make_state):
    _with_customers(repo, make_state, "c1")
    repo.save_decision(_decision(make_state))
    for kind, hour in ((OutcomeType.ORDER_PLACED, 14), (OutcomeType.MESSAGE_OPENED, 11)):
        repo.save_outcome(
            Outcome(
                decision_id="d1",
                customer_id="c1",
                outcome_type=kind,
                occurred_at=T.replace(hour=hour),
            )
        )
    assert [o.outcome_type for o in repo.list_outcomes(decision_id="d1")] == [
        OutcomeType.MESSAGE_OPENED,
        OutcomeType.ORDER_PLACED,
    ]


def test_reward_is_upserted(repo, make_state):
    _with_customers(repo, make_state, "c1")
    repo.save_decision(_decision(make_state))
    repo.save_reward(RewardRecord(decision_id="d1", customer_id="c1", points=2, reason="a"))
    repo.save_reward(RewardRecord(decision_id="d1", customer_id="c1", points=47, reason="b"))
    reward = repo.get_reward("d1")
    assert reward is not None and (reward.points, reward.reason) == (47, "b")
    assert repo.get_reward("nope") is None


def test_policy_round_trip_and_overwrite(repo):
    assert repo.load_policy("uc") is None
    repo.save_policy("uc", {"cells": [{"bucket": "b", "action": "a", "n": 1, "total": 2.5}]})
    repo.save_policy("uc", {"cells": [{"bucket": "b", "action": "a", "n": 2, "total": 9.0}]})
    assert repo.load_policy("uc") == {
        "cells": [{"bucket": "b", "action": "a", "n": 2, "total": 9.0}]
    }


def test_json_file_round_trip(tmp_path, make_state, config):
    path = tmp_path / "data.json"
    repo = JsonFileRepository(path)
    repo.save_customer(make_state())
    repo.record_contact("c1", T)
    repo.save_journey(Journey(customer_id="c1", started_at=T, base_abandoned_minutes=35))
    repo.save_decision(_decision(make_state))
    repo.save_outcome(
        Outcome(
            decision_id="d1", customer_id="c1", outcome_type=OutcomeType.ORDER_PLACED, occurred_at=T
        )
    )
    repo.save_reward(RewardRecord(decision_id="d1", customer_id="c1", points=40, reason="r"))
    repo.save_policy(config.use_case_id, {"cells": []})
    repo.flush()

    again = JsonFileRepository(path)
    assert again.get_customer("c1") is not None
    assert again.get_contact_times("c1") == [T]
    assert again.get_journey("c1") is not None
    assert again.get_decision("d1") is not None
    assert len(again.list_outcomes(decision_id="d1")) == 1
    assert again.load_policy(config.use_case_id) == {"cells": []}


@pytest.mark.parametrize("count", [3])
def test_many_customers_listing_respects_limit(repo, make_state, count):
    _with_customers(repo, make_state, *[f"c{i}" for i in range(count)])
    assert len(repo.list_customers(limit=2)) == 2
