import pytest
from pydantic import ValidationError

from app.agents.channel import propose_channels
from app.agents.eligibility import is_eligible
from app.agents.offer import propose_levers, propose_offers
from app.agents.timing import propose_timings
from app.config.loader import USE_CASE_DIR
from app.models import Channel, UseCaseConfig, parse_timing


def test_all_options_for_fully_eligible_customer(config, make_state):
    s = make_state()
    assert propose_channels(s, config) == [Channel.PUSH, Channel.EMAIL, Channel.SMS]
    assert propose_offers(s, config) == ["free_shipping", "bonus_points", "percent_off"]
    assert len(propose_levers(s, config)) == 4


def test_channel_eligibility(config, make_state):
    assert Channel.PUSH not in propose_channels(make_state(has_app=False), config)
    assert Channel.SMS not in propose_channels(make_state(sms_opt_in=False), config)
    assert Channel.EMAIL not in propose_channels(make_state(email_opt_in=False), config)


def test_deliverable_filter(config, make_state):
    got = propose_channels(make_state(), config, deliverable={Channel.EMAIL})
    assert got == [Channel.EMAIL]


def test_offer_and_lever_eligibility(config, make_state):
    guest = make_state(
        signals={"loyalty_tier": "none", "card_holder": False, "fulfillment": "ship"}
    )
    assert "bonus_points" not in propose_offers(guest, config)
    levers = propose_levers(guest, config)
    assert levers == ["free_shipping"]


def test_hidden_signal_fails_closed(config, make_state):
    # card_holder missing (payment signal switched off) -> card_pitch not offered
    s = make_state(signals={"loyalty_tier": "circle", "fulfillment": "pickup"})
    assert "card_pitch" not in propose_levers(s, config)


def test_timings_drop_passed_moments(config, make_state):
    assert propose_timings(make_state(abandoned_minutes=35), config) == ["T+1h", "T+24h"]
    assert propose_timings(make_state(abandoned_minutes=90), config) == ["T+24h"]
    assert propose_timings(make_state(abandoned_minutes=2000), config) == []


def test_unknown_rule_raises(config, make_state):
    space = config.action_space.model_copy(update={"eligibility": {"sms": "no_such_rule"}})
    bad = config.model_copy(update={"action_space": space})
    with pytest.raises(ValueError, match="no_such_rule"):
        is_eligible("sms", make_state(), bad)


def test_parse_timing():
    assert parse_timing("T+45m").total_seconds() == 45 * 60
    assert parse_timing("T+1h").total_seconds() == 3600
    assert parse_timing("T+3d").total_seconds() == 3 * 86400
    with pytest.raises(ValueError):
        parse_timing("tomorrow")


def test_config_rejects_bad_timing():
    import json

    raw = json.loads((USE_CASE_DIR / "target_cart.json").read_text())
    raw["action_space"]["timings"] = ["T+1h", "soon"]
    with pytest.raises(ValidationError):
        UseCaseConfig.model_validate(raw)
