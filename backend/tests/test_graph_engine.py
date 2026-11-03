"""The graph engine must give exactly the same decisions as the classic one (step 8e-2).

Needs the langgraph package; skipped when it is not installed.
"""

import itertools
from datetime import timedelta

import pytest

pytest.importorskip("langgraph")

from app.agents.state_agent import CustomerNotFoundError  # noqa: E402
from app.graph.builder import ROUTERS  # noqa: E402
from app.graph.engine import ENGINES, engine_from_env  # noqa: E402
from app.graph.state import GRAPH_EDGES, START  # noqa: E402
from app.models import Outcome, OutcomeType  # noqa: E402
from app.orchestrator import LogDispatcher, Orchestrator  # noqa: E402
from app.repository import InMemoryRepository  # noqa: E402
from app.simulator.customers import generate_customers  # noqa: E402

NOW_OFFSETS = [timedelta(0), timedelta(hours=1), timedelta(hours=26), timedelta(hours=50)]


def build(engine, config, library):
    repo = InMemoryRepository()
    counter = itertools.count()
    orch = Orchestrator(
        repo,
        config,
        library,
        seed=11,
        dispatcher=LogDispatcher(),
        id_factory=lambda: f"d{next(counter):05d}",
        campaign_version=1,
        engine=engine,
    )
    return repo, orch


def dump(model):
    return model.model_dump(mode="json")


def summary(m):
    return (m.decision_id, m.customer_id, m.send_at, m.message)


def run_both(config, library, customers, times):
    (repo_a, classic), (repo_b, graph) = build("classic", config, library), build(
        "graph", config, library
    )
    for c in customers:
        repo_a.save_customer(c)
        repo_b.save_customer(c)
    sent = 0
    for t in times:
        for i, c in enumerate(customers):
            a = classic.decide(c.customer_id, t)
            b = graph.decide(c.customer_id, t)
            assert dump(a) == dump(b), (c.customer_id, t.isoformat())
            stored_a = repo_a.get_decision(a.decision_id)
            stored_b = repo_b.get_decision(b.decision_id)
            assert dump(stored_a) == dump(stored_b)
            if not a.chosen.is_wait:
                sent += 1
                kind = OutcomeType.ORDER_PLACED if i % 7 == 0 else OutcomeType.IGNORED
                out = Outcome(
                    decision_id=a.decision_id,
                    customer_id=c.customer_id,
                    outcome_type=kind,
                    occurred_at=t,
                )
                assert dump(classic.record_outcome(out)) == dump(graph.record_outcome(out))
    assert classic.master.export_state() == graph.master.export_state()
    assert [summary(m) for m in classic.dispatcher.sent] == [
        summary(m) for m in graph.dispatcher.sent
    ]
    for c in customers:
        assert repo_a.get_contact_times(c.customer_id) == repo_b.get_contact_times(c.customer_id)
    return sent


def customers_for(config, n=120):
    return generate_customers(n, config, seed=3)


def test_normal_flow_matches_over_several_days(config, library, now):
    # 400 customers: with 120 only 6 get a nudge (cooldown, hours, the bandit's waits)
    sent = run_both(config, library, customers_for(config, 400), [now + d for d in NOW_OFFSETS])
    assert sent > 20, "the scenario must actually send nudges, or the comparison proves little"


def test_outside_allowed_hours_matches(config, library, now):
    run_both(config, library, customers_for(config, 40), [now.replace(hour=3)])


def test_no_approved_variants_matches(config, library, now):
    empty = library.model_copy(update={"variants": []})
    sent = run_both(config, empty, customers_for(config, 40), [now])
    assert sent == 0


def test_epsilon_greedy_variant_selection_matches(config, library, now):
    learning = config.learning.model_copy(
        update={"variant_selection": "epsilon_greedy", "variant_epsilon": 0.3,
                "variant_min_resolved": 3}
    )
    cfg = config.model_copy(update={"learning": learning})
    run_both(cfg, library, customers_for(cfg), [now + d for d in NOW_OFFSETS])


def test_special_customers_match(config, library, now, make_state):
    special = [
        make_state(customer_id="dnd", dnd=True),
        make_state(customer_id="optout", contactable=False),
        make_state(customer_id="tiny", cart_value=1.0),
        make_state(customer_id="fresh", abandoned_minutes=1),
        make_state(customer_id="plain"),
    ]
    run_both(config, library, special, [now, now + timedelta(hours=1)])


def test_unknown_customer_raises_the_same_error(config, library, now):
    for engine in ENGINES:
        _, orch = build(engine, config, library)
        with pytest.raises(CustomerNotFoundError):
            orch.decide("ghost", now)


def test_graph_engine_is_built_only_when_asked(config, library):
    assert build("classic", config, library)[1]._graph is None
    assert build("graph", config, library)[1]._graph is not None


def test_routers_only_return_nodes_the_spec_allows():
    branching = {src for src, _, _ in GRAPH_EDGES if src != START}
    branching = {n for n in branching if sum(1 for s, _, _ in GRAPH_EDGES if s == n) > 1}
    assert set(ROUTERS) == branching


def test_engine_setting():
    assert engine_from_env({}) == "classic"
    assert engine_from_env({"NUDGEIQ_ENGINE": " Graph "}) == "graph"
    with pytest.raises(ValueError, match="NUDGEIQ_ENGINE"):
        engine_from_env({"NUDGEIQ_ENGINE": "dag"})


def test_bad_engine_fails_early(config, library):
    with pytest.raises(ValueError, match="NUDGEIQ_ENGINE"):
        Orchestrator(InMemoryRepository(), config, library, engine="dag")


