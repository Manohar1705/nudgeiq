from datetime import UTC, datetime, timedelta

from app.agents.guardrail import GuardrailAgent
from app.models import Action, Channel, Guardrails, OutcomeType


def act(timing="T+1h", channel=Channel.PUSH):
    return Action(channel=channel, timing=timing, offer="free_shipping", lever="free_shipping")


def agent(config, **over):
    return GuardrailAgent(config.guardrails.model_copy(update=over))


def test_entry_passes_for_normal_customer(config, make_state):
    assert agent(config).check_entry(make_state()).allowed


def test_entry_blocks_each_rule(config, make_state):
    g = agent(config)
    assert not g.check_entry(make_state(dnd=True)).allowed
    assert not g.check_entry(make_state(contactable=False)).allowed
    assert not g.check_entry(make_state(cart_value=10.0)).allowed  # must be > 10
    assert g.check_entry(make_state(cart_value=10.01)).allowed
    assert not g.check_entry(make_state(abandoned_minutes=29)).allowed
    assert g.check_entry(make_state(abandoned_minutes=30)).allowed


def test_entry_lists_all_reasons(config, make_state):
    result = agent(config).check_entry(make_state(dnd=True, cart_value=5.0))
    assert len(result.reasons) == 2


def test_contact_cap_blocks_everything(config, make_state, now):
    g = agent(config)
    result = g.filter_actions(make_state(contacts_in_window=4), [act()], now)
    assert result.allowed == [] and "contact cap" in (result.global_block or "")
    assert g.filter_actions(make_state(contacts_in_window=3), [act()], now).allowed


def test_cooldown_is_checked_at_send_time(config, make_state, now):
    g = agent(config)
    state = make_state(last_contact_at=now - timedelta(hours=10))
    short, long_ = act("T+1h"), act("T+3d")
    result = g.filter_actions(state, [short, long_], now)
    assert short.key in result.blocked and "cool-down" in result.blocked[short.key]
    assert result.allowed == [long_]


def test_allowed_hours(config, make_state):
    g = agent(config)
    late = datetime(2026, 11, 2, 22, 0, tzinfo=UTC)
    result = g.filter_actions(make_state(), [act("T+1h")], late)
    assert result.allowed == [] and "allowed hours" in next(iter(result.blocked.values()))
    morning = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert g.filter_actions(make_state(), [act("T+1h")], morning).allowed


def test_blocked_weekday(config, make_state):
    g = agent(config, allowed_weekdays=[0, 1, 2, 3, 4])  # no weekends
    saturday = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
    result = g.filter_actions(make_state(), [act("T+1h")], saturday)
    assert result.allowed == [] and "not allowed" in next(iter(result.blocked.values()))


def test_journey_window(config, make_state, now):
    g = agent(config)
    result = g.filter_actions(make_state(), [act("T+30d")], now)
    assert result.allowed == [] and "journey window" in next(iter(result.blocked.values()))


def test_send_time_never_in_past(config, make_state, now):
    g = agent(config)
    state = make_state(abandoned_minutes=300)
    assert g.send_time(state, act("T+1h"), now) == now


def test_exit_rules(config):
    g = agent(config)
    assert g.check_exit([OutcomeType.ORDER_PLACED], 1).reason == "order placed"
    assert g.check_exit([OutcomeType.UNSUBSCRIBED], 1).exit
    assert g.check_exit([], 14).exit
    assert not g.check_exit([OutcomeType.MESSAGE_OPENED], 3).exit


def test_guardrails_model_rejects_bad_hours():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Guardrails(
            max_contacts_per_window=4,
            window_days=30,
            cooldown_hours=48,
            allowed_start_hour=21,
            allowed_end_hour=9,
        )
