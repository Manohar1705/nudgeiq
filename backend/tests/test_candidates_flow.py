from datetime import timedelta

from app.agents.candidates import generate_candidates
from app.agents.guardrail import GuardrailAgent
from app.agents.master import MasterAgent
from app.agents.message import MessageAgent
from app.agents.state_agent import StateAgent
from app.repository import InMemoryRepository


def build(config, library, make_state, **kw):
    repo = InMemoryRepository()
    repo.add_customer(make_state(**kw))
    guardrail = GuardrailAgent(config.guardrails)
    return repo, StateAgent(repo, config), guardrail, MessageAgent(library, 1)


def test_normal_customer_gets_full_menu(config, library, make_state, now):
    repo, sa, g, m = build(config, library, make_state)
    r = generate_candidates(sa.build_state("c1", now), config, g, now, m.can_send)
    assert not r.suppressed and not r.forced_wait
    assert len(r.candidates) == 72


def test_entry_suppression(config, library, make_state, now):
    repo, sa, g, m = build(config, library, make_state, dnd=True)
    r = generate_candidates(sa.build_state("c1", now), config, g, now, m.can_send)
    assert r.suppressed and r.candidates == []
    assert "do-not-disturb" in r.steps[0].note


def test_contact_cap_forces_wait(config, library, make_state, now):
    repo, sa, g, m = build(config, library, make_state)
    for d in (1, 3, 7, 12):
        repo.record_contact("c1", now - timedelta(days=d))
    r = generate_candidates(sa.build_state("c1", now), config, g, now, m.can_send)
    assert r.forced_wait and r.candidates == []
    assert any("contact cap" in s.note for s in r.steps)


def test_candidates_respect_eligibility_and_sendability(config, library, make_state, now):
    guest = {"loyalty_tier": "none", "card_holder": False, "fulfillment": "ship"}
    repo, sa, g, m = build(config, library, make_state, has_app=False, signals=guest)
    r = generate_candidates(sa.build_state("c1", now), config, g, now, m.can_send)
    assert r.candidates
    for a in r.candidates:
        assert a.channel.value in {"email", "sms"}
        assert a.offer in {"free_shipping", "percent_off"}
        assert a.lever == "free_shipping"
        assert m.can_send(a.channel, a.offer)


def test_every_candidate_can_be_rendered(config, library, make_state, now):
    repo, sa, g, m = build(config, library, make_state)
    r = generate_candidates(sa.build_state("c1", now), config, g, now, m.can_send)
    for a in r.candidates[::7]:
        assert m.render(a, make_state()).text


def test_unsendable_actions_are_dropped_and_logged(config, library, make_state, now):
    only_push = library.model_copy(
        update={"variants": [v for v in library.variants if v.variant_id.startswith("v_push")]}
    )
    repo, sa, g, _ = build(config, only_push, make_state)
    m = MessageAgent(only_push, 1)
    r = generate_candidates(sa.build_state("c1", now), config, g, now, m.can_send)
    assert {a.channel.value for a in r.candidates} == {"push"}
    assert any(s.agent == "message" for s in r.steps)


def test_master_picks_only_from_legal_candidates(config, library, make_state, now):
    repo, sa, g, m = build(config, library, make_state, has_app=False)
    state = sa.build_state("c1", now)
    r = generate_candidates(state, config, g, now, m.can_send)
    master = MasterAgent(epsilon=0.3, seed=5)
    for _ in range(100):
        pick = master.select(state, r.candidates)
        assert pick.action in r.candidates and pick.action.channel.value != "push"


def test_decision_trace_has_every_agent(config, library, make_state, now):
    repo, sa, g, m = build(config, library, make_state)
    state = sa.build_state("c1", now)
    r = generate_candidates(state, config, g, now, m.can_send)
    decision = MasterAgent(seed=1).decide("d1", state, r.candidates)
    decision.steps = r.steps + decision.steps
    agents = {s.agent for s in decision.steps}
    assert agents >= {"guardrail", "channel", "timing", "offer", "master"}
