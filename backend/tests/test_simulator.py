import random

from app.agents.action_space import build_action_list
from app.config.loader import load_use_case
from app.models import Action, Channel
from app.simulator.customers import generate_customers
from app.simulator.responses import ResponseModel

CFG = load_use_case("target_cart")


def test_generation_is_deterministic():
    a = generate_customers(50, CFG, seed=7)
    b = generate_customers(50, CFG, seed=7)
    assert [c.model_dump() for c in a] == [c.model_dump() for c in b]


def test_holdout_fraction_roughly_matches_config():
    customers = generate_customers(5000, CFG, seed=3)
    share = sum(c.is_holdout for c in customers) / len(customers)
    assert abs(share - CFG.learning.holdout_fraction) < 0.02


def test_all_probabilities_valid():
    model = ResponseModel()
    customers = generate_customers(100, CFG, seed=2)
    for c in customers:
        for a in build_action_list(CFG):
            p = model.probabilities(c, a)
            for v in (p.open, p.click, p.restart, p.order, p.unsub, p.natural):
                assert 0.0 <= v <= 1.0


def test_preferred_channel_opens_more():
    model = ResponseModel()
    state = generate_customers(1, CFG, seed=1)[0].model_copy(
        update={"preferred_channel": Channel.EMAIL}
    )
    match = Action(channel=Channel.EMAIL, timing="T+24h", offer="free_shipping")
    other = Action(channel=Channel.PUSH, timing="T+24h", offer="free_shipping")
    # email has the lower base open rate, yet the preference should still win per-channel
    p_match = model.probabilities(state, match).open / 0.30
    p_other = model.probabilities(state, other).open / 0.45
    assert p_match > p_other


def test_wait_only_has_natural_conversion():
    model = ResponseModel()
    state = generate_customers(1, CFG, seed=1)[0]
    p = model.probabilities(state, Action.wait())
    assert p.open == 0 and p.unsub == 0 and p.natural > 0


def test_expected_reward_matches_sampling():
    model = ResponseModel()
    state = generate_customers(1, CFG, seed=5)[0]
    action = build_action_list(CFG)[10]
    rng = random.Random(0)
    n = 40000
    total = 0
    for _ in range(n):
        outcomes = model.sample_outcomes(state, action, rng)
        total += sum(CFG.rewards.reward_for(o) for o in set(outcomes))
    assert abs(total / n - model.expected_reward(state, action, CFG.rewards)) < 0.4
