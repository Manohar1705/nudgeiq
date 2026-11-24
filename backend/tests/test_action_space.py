from app.agents.action_space import build_action_list
from app.config.loader import load_use_case


def test_size_matches_config():
    cfg = load_use_case("target_cart")
    actions = build_action_list(cfg)
    assert len(actions) == cfg.action_space.size == 72


def test_actions_unique_and_no_wait():
    actions = build_action_list(load_use_case("target_cart"))
    assert len({a.key for a in actions}) == len(actions)
    assert not any(a.is_wait for a in actions)
