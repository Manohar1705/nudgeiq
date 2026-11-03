from app.agents.reward import compute_reward
from app.config.loader import load_use_case
from app.models import OutcomeType

REWARDS = load_use_case("target_cart").rewards


def test_order_gives_forty():
    r = compute_reward("d1", "c1", [OutcomeType.ORDER_PLACED], REWARDS)
    assert r.points == 40
    assert "order_placed" in r.reason


def test_positive_outcomes_stack():
    r = compute_reward(
        "d1",
        "c1",
        [OutcomeType.MESSAGE_OPENED, OutcomeType.CTA_CLICKED, OutcomeType.CHECKOUT_RESTARTED],
        REWARDS,
    )
    assert r.points == 2 + 5 + 18


def test_duplicates_counted_once():
    r = compute_reward("d1", "c1", [OutcomeType.CTA_CLICKED, OutcomeType.CTA_CLICKED], REWARDS)
    assert r.points == 5


def test_penalties_subtract():
    r = compute_reward("d1", "c1", [OutcomeType.MESSAGE_OPENED, OutcomeType.UNSUBSCRIBED], REWARDS)
    assert r.points == 2 - 20


def test_no_response_is_zero():
    r = compute_reward("d1", "c1", [], REWARDS)
    assert r.points == 0
    assert r.reason == "no response"
