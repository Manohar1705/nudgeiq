"""5B-9: choosing among APPROVED variants, uniform by default, optional epsilon-greedy."""

import random

import pytest

from app.agents.message import MessageAgent, NoApprovedVariantError
from app.agents.variant_selector import (
    EpsilonGreedySelector,
    UniformSelector,
    VariantRecord,
)
from app.models import Action, Channel, MessageVariant, VariantStatus
from app.orchestrator import Orchestrator
from app.orchestrator.variant_stats import CampaignVariantStats


def variant(vid: str) -> MessageVariant:
    return MessageVariant(
        variant_id=vid, template_id="t", text="hi", principle="p", tone="t",
        status=VariantStatus.APPROVED,
    )  # fmt: skip


A, B, C = variant("a"), variant("b"), variant("c")
PUSH = Action(channel=Channel.PUSH, timing="T+1h", offer="free_shipping")


def greedy(stats, **kw):
    return EpsilonGreedySelector(lambda: stats, seed=1, **kw)


# ------------------------------------------------------------------ uniform
def test_uniform_uses_every_option_and_explains_itself():
    sel = UniformSelector(random.Random(1))
    picks = [sel.choose([A, B, C]) for _ in range(200)]
    assert {p.variant.variant_id for p in picks} == {"a", "b", "c"}
    assert picks[0].how == "uniform among 3 approved"


def test_selectors_refuse_an_empty_list():
    with pytest.raises(ValueError):
        UniformSelector(random.Random(1)).choose([])
    with pytest.raises(ValueError):
        greedy({}).choose([])


def test_default_message_agent_is_unchanged_and_reproducible(library, make_state):
    one, two = MessageAgent(library, seed=5), MessageAgent(library, seed=5)
    seq1 = [one.render(PUSH, make_state()).variant_id for _ in range(30)]
    seq2 = [two.render(PUSH, make_state()).variant_id for _ in range(30)]
    assert seq1 == seq2


# ------------------------------------------------------------------ epsilon-greedy
def test_warm_up_favours_variants_without_enough_data():
    stats = {"a": VariantRecord(100, 90), "b": VariantRecord(2, 0)}  # b is new, a is great
    sel = greedy(stats, min_resolved=30, epsilon=0.0)
    picks = [sel.choose([A, B]) for _ in range(50)]
    assert {p.variant.variant_id for p in picks} == {"b"}
    assert picks[0].how.startswith("warm-up")


def test_exploits_the_best_converter_once_everyone_has_data():
    stats = {
        "a": VariantRecord(100, 10),
        "b": VariantRecord(100, 40),
        "c": VariantRecord(100, 20),
    }
    sel = greedy(stats, min_resolved=30, epsilon=0.0)
    picks = [sel.choose([A, B, C]) for _ in range(50)]
    assert {p.variant.variant_id for p in picks} == {"b"}
    assert picks[0].how.startswith("exploit")


def test_epsilon_one_always_explores():
    stats = {"a": VariantRecord(100, 90), "b": VariantRecord(100, 1)}
    sel = greedy(stats, min_resolved=30, epsilon=1.0)
    picks = [sel.choose([A, B]) for _ in range(100)]
    assert {p.variant.variant_id for p in picks} == {"a", "b"}
    assert all(p.how.startswith("explore") for p in picks)


def test_explore_rate_is_roughly_epsilon():
    stats = {"a": VariantRecord(100, 90), "b": VariantRecord(100, 1)}
    sel = greedy(stats, min_resolved=30, epsilon=0.2)
    n = 4000
    explored = sum(sel.choose([A, B]).how.startswith("explore") for _ in range(n))
    assert 0.15 < explored / n < 0.25


def test_ties_are_broken_at_random_not_by_list_order():
    stats = {"a": VariantRecord(50, 10), "b": VariantRecord(50, 10)}
    sel = greedy(stats, min_resolved=30, epsilon=0.0)
    assert {sel.choose([A, B]).variant.variant_id for _ in range(100)} == {"a", "b"}


def test_a_single_option_needs_no_data():
    assert greedy({}).choose([A]).variant is A


def test_bad_settings_are_rejected():
    with pytest.raises(ValueError):
        greedy({}, epsilon=1.5)
    with pytest.raises(ValueError):
        greedy({}, min_resolved=0)


# ------------------------------------------------------------------ stats from stored decisions
def test_stats_count_resolved_decisions_per_variant_for_one_version(repo, add):
    for _ in range(3):
        add(variant_id="v1", outcome="order")
    add(variant_id="v1", outcome="ignored")
    add(variant_id="v1", outcome=None)  # still waiting: not a failure, not counted
    add(variant_id="v2", outcome="ignored")
    add(variant_id="v1", outcome="order", version=2)  # other version: not counted
    add(variant_id=None, outcome="order")  # no variant (e.g. holdout/legacy): ignored
    stats = CampaignVariantStats(repo, "target_cart", 1)()
    assert stats == {"v1": VariantRecord(4, 3), "v2": VariantRecord(1, 0)}


def test_stats_are_cached_between_refreshes(repo, add):
    add(variant_id="v1", outcome="order")
    source = CampaignVariantStats(repo, "target_cart", 1, refresh_every=3)
    assert source()["v1"].resolved == 1
    add(variant_id="v1", outcome="order")
    assert source()["v1"].resolved == 1  # still the old read
    source()
    assert source()["v1"].resolved == 2  # fourth call refreshes


# ------------------------------------------------------------------ through the orchestrator
def make_orch(repo, config, library, **learning):
    cfg = config.model_copy(update={"learning": config.learning.model_copy(update=learning)})
    return Orchestrator(repo, cfg, library, seed=3, campaign_version=1)


def test_orchestrator_defaults_to_uniform(repo, config, library):
    orch = make_orch(repo, config, library)
    assert type(orch.message_agent.selector).__name__ == "UniformSelector"


def test_flag_turns_on_the_bandit(repo, config, library):
    orch = make_orch(
        repo, config, library,
        variant_selection="epsilon_greedy", variant_epsilon=0.25, variant_min_resolved=7,
    )  # fmt: skip
    sel = orch.message_agent.selector
    assert isinstance(sel, EpsilonGreedySelector)
    assert (sel.epsilon, sel.min_resolved) == (0.25, 7)


def test_bandit_only_ever_sends_approved_and_logs_why(repo, config, library, make_state):
    orch = make_orch(repo, config, library, variant_selection="epsilon_greedy")
    agent = orch.message_agent
    approved = {v.variant_id for v in agent.approved_variants()}
    for _ in range(40):
        assert agent.render(PUSH, make_state()).variant_id in approved
    assert agent.render(PUSH, make_state()).selection.startswith("warm-up")


def test_learning_config_rejects_bad_values(config):
    learning = type(config.learning)
    for bad in ({"variant_selection": "x"}, {"variant_min_resolved": 0}, {"variant_epsilon": 2}):
        with pytest.raises(ValueError):
            learning(**bad)


def test_no_approved_variant_still_waits_instead_of_sending_unreviewed_copy(library, make_state):
    library = library.model_copy(
        update={"variants": [v for v in library.variants if "push" not in v.variant_id]}
    )
    for selector in (None, greedy({})):
        agent = MessageAgent(library, selector=selector)
        assert not agent.can_send(Channel.PUSH, "free_shipping")
        with pytest.raises(NoApprovedVariantError):
            agent.render(PUSH, make_state())


def test_decide_records_variant_template_and_how_it_was_picked(
    repo, config, library, make_state, now
):
    orch = make_orch(repo, config, library, variant_selection="epsilon_greedy", holdout_fraction=0)
    sent = None
    for i in range(30):  # the Master may wait; find a real send
        state = make_state(customer_id=f"c{i}")
        repo.save_customer(state)
        d = orch.decide(state.customer_id, now)
        if d.variant_id:
            sent = d
            break
    assert sent is not None, "expected at least one send"
    assert sent.template_id is not None
    notes = [s.note for s in sent.steps if s.agent == "message"]
    assert any(n.startswith(f"used approved variant {sent.variant_id} (") for n in notes)
    stored = repo.get_decision(sent.decision_id)
    assert stored is not None and stored.variant_id == sent.variant_id
