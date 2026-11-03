from app.config.loader import load_use_case
from app.repository import InMemoryRepository
from app.simulator.full_run import audit, realistic_customers, simulate_full

CFG = load_use_case("target_cart")


def test_full_pipeline_respects_every_guardrail():
    customers = realistic_customers(150, CFG, seed=3)
    for learn in (False, True):
        stats = simulate_full("t", customers, CFG, learn, seed=3, touches=3)
        assert stats.violations == []
        assert stats.nudges > 0 and stats.waits > 0
        assert stats.treated + stats.control == 150


def test_audit_catches_a_broken_rule():
    from datetime import UTC, datetime

    repo = InMemoryRepository()
    t = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    repo.record_contact("x", t)
    repo.record_contact("x", t.replace(hour=11))  # one hour later: breaks the 48h cool-down
    repo.record_contact("y", t.replace(hour=3))  # 03:00 UTC: outside allowed hours
    problems = audit(repo, CFG)
    assert any("cool-down" in p for p in problems)
    assert any("outside allowed hours" in p for p in problems)


def test_learning_run_is_reproducible():
    customers = realistic_customers(100, CFG, seed=5)
    a = simulate_full("a", customers, CFG, True, seed=5)
    b = simulate_full("b", customers, CFG, True, seed=5)
    assert a.per_customer == b.per_customer
