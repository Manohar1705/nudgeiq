from app.agents.action_space import build_action_list
from app.agents.master import MasterAgent, bucket_for
from app.config.loader import load_use_case
from app.simulator.customers import generate_customers
from app.simulator.run import oracle_expected, simulate

CFG = load_use_case("target_cart")
ACTIONS = build_action_list(CFG)


def _customer(holdout: bool = False):
    c = generate_customers(1, CFG, seed=4)[0]
    return c.model_copy(update={"is_holdout": holdout})


def test_holdout_is_never_nudged_or_learned():
    agent = MasterAgent(seed=1)
    sel = agent.select(_customer(holdout=True), ACTIONS)
    assert sel.action.is_wait
    agent.update(sel.bucket, sel.action, 40)
    assert agent.cells_learned == 0


def test_update_changes_score():
    agent = MasterAgent(epsilon=0.0, seed=1)
    state = _customer()
    bucket = bucket_for(state)
    action = ACTIONS[0]
    before = agent.score(bucket, action)
    agent.update(bucket, action, 40)
    assert agent.score(bucket, action) > before
    assert agent.times_tried(bucket, action) == 1


def test_exploit_picks_best_when_epsilon_zero():
    agent = MasterAgent(epsilon=0.0, seed=1)
    state = _customer()
    bucket = bucket_for(state)
    for a in ACTIONS:
        for _ in range(5):
            agent.update(bucket, a, 0)
    for _ in range(5):
        agent.update(bucket, ACTIONS[7], 40)
    sel = agent.select(state, ACTIONS)
    assert sel.action == ACTIONS[7] and not sel.explored


def test_decide_returns_explainable_record():
    agent = MasterAgent(seed=2)
    d = agent.decide("d1", _customer(), ACTIONS)
    assert len(d.candidates) == len(ACTIONS)
    assert d.candidates[0].score >= d.candidates[-1].score
    assert d.steps and d.steps[0].agent == "master"


def test_learning_beats_random_milestone():
    customers = generate_customers(20000, CFG, seed=11)
    random_run = simulate("random", customers, CFG, learn=False, seed=11)
    learned = simulate("learned", customers, CFG, learn=True, seed=11)
    r_end = random_run.mean(random_run.expected, 0.8, 1.0)
    l_end = learned.mean(learned.expected, 0.8, 1.0)
    assert l_end > r_end * 1.2
    assert l_end <= oracle_expected(customers, CFG) + 1e-9
    assert learned.uplift > 0


def test_adjust_and_state_round_trip():
    agent = MasterAgent(epsilon=0.0, seed=1)
    state = _customer()
    bucket = bucket_for(state)
    action = ACTIONS[0]
    agent.update(bucket, action, 2)
    agent.adjust(bucket, action, 5)
    assert agent.times_tried(bucket, action) == 1  # adjusting never adds a trial
    clone = MasterAgent(epsilon=0.0, seed=1)
    clone.load_state(agent.export_state())
    assert clone.score(bucket, action) == agent.score(bucket, action)
