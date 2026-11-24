from datetime import timedelta

import pytest

from app.agents.state_agent import CustomerNotFoundError, StateAgent, stable_holdout
from app.repository import InMemoryRepository


def setup(config, make_state, **kw):
    repo = InMemoryRepository()
    repo.add_customer(
        make_state(
            signals={
                "loyalty_tier": "circle",
                "card_holder": True,
                "fulfillment": "pickup",
                "device": "ios",
                "first_name": "Asha",
                "top_item": "lamp",
            },
            **kw,
        )
    )
    return repo, StateAgent(repo, config)


def test_only_enabled_signals_are_visible(config, make_state, now):
    _, agent = setup(config, make_state)
    state = agent.build_state("c1", now)
    assert "loyalty_tier" in state.signals
    assert "device" not in state.signals  # 'device' signal is switched off
    assert "first_name" not in state.signals  # personalisation is separate


def test_contact_history_feeds_cap_and_cooldown(config, make_state, now):
    repo, agent = setup(config, make_state)
    repo.record_contact("c1", now - timedelta(days=2))
    repo.record_contact("c1", now - timedelta(days=40))  # outside the 30-day window
    repo.record_contact("c1", now - timedelta(hours=5))
    state = agent.build_state("c1", now)
    assert state.contacts_in_window == 2
    assert state.last_contact_at == now - timedelta(hours=5)


def test_no_history(config, make_state, now):
    _, agent = setup(config, make_state)
    state = agent.build_state("c1", now)
    assert state.contacts_in_window == 0 and state.last_contact_at is None


def test_personalization_keys(config, make_state):
    _, agent = setup(config, make_state)
    assert agent.personalization("c1") == {"first_name": "Asha", "top_item": "lamp"}


def test_unknown_customer(config, make_state, now):
    _, agent = setup(config, make_state)
    with pytest.raises(CustomerNotFoundError):
        agent.build_state("nobody", now)


def test_holdout_is_stable_and_roughly_ten_percent():
    assert stable_holdout("abc", 0.1) == stable_holdout("abc", 0.1)
    share = sum(stable_holdout(f"cust_{i}", 0.1) for i in range(20000)) / 20000
    assert abs(share - 0.1) < 0.01
    assert not any(stable_holdout(f"x{i}", 0.0) for i in range(500))
