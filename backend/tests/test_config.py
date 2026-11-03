import json

import pytest
from pydantic import ValidationError

from app.config.loader import USE_CASE_DIR, list_use_cases, load_use_case
from app.models import OutcomeType, UseCaseConfig


def test_sample_config_loads():
    cfg = load_use_case("target_cart")
    assert cfg.use_case_id == "target_cart"
    assert "target_cart" in list_use_cases()


def test_active_signals_only_enabled():
    cfg = load_use_case("target_cart")
    assert len(cfg.active_signals) == 6
    assert "inventory" not in cfg.active_signals


def test_action_space_size():
    cfg = load_use_case("target_cart")
    assert cfg.action_space.size == 4 * 3 * 3 * 2


def test_rewards_and_guardrails():
    cfg = load_use_case("target_cart")
    assert cfg.rewards.reward_for(OutcomeType.ORDER_PLACED) == 40
    assert cfg.rewards.reward_for(OutcomeType.UNSUBSCRIBED) == -20
    assert cfg.guardrails.max_contacts_per_window == 4
    assert cfg.guardrails.cooldown_hours == 48


def test_missing_config_raises():
    with pytest.raises(FileNotFoundError):
        load_use_case("does_not_exist")


def test_bad_hours_rejected():
    raw = json.loads((USE_CASE_DIR / "target_cart.json").read_text())
    raw["guardrails"]["allowed_start_hour"] = 22
    with pytest.raises(ValidationError):
        UseCaseConfig.model_validate(raw)


def test_change_use_case_without_code():
    raw = json.loads((USE_CASE_DIR / "target_cart.json").read_text())
    raw["use_case_id"] = "insurance_xsell"
    raw["action_space"]["channels"] = ["email", "sms"]
    cfg = UseCaseConfig.model_validate(raw)
    assert cfg.action_space.size == 4 * 2 * 3 * 2
