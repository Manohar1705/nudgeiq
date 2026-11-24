from datetime import UTC, datetime

import pytest

from app.config.loader import load_template_library, load_use_case
from app.models import (
    Action,
    Channel,
    CustomerState,
    Decision,
    Outcome,
    OutcomeType,
    RewardRecord,
)

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)  # Monday 10:00 UTC


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch):
    """Tests never reach a real model, whatever the developer's shell has set."""
    for name in (
        "NUDGEIQ_LLM",
        "GROQ_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "NUDGEIQ_GROQ_MODEL",
        "NUDGEIQ_GROQ_MODEL_2",
        "NUDGEIQ_GEMINI_MODEL",
        "NUDGEIQ_OPENROUTER_MODEL",
        "NUDGEIQ_LLM_MODEL",
        "NUDGEIQ_LLM_TIMEOUT",
        "NUDGEIQ_ENGINE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config():
    return load_use_case("target_cart")


@pytest.fixture
def library():
    return load_template_library("target_cart")


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def make_state():
    def _make(**kw):
        base = dict(
            customer_id="c1",
            cohort_id="cart_high_aov",
            cart_value=120.0,
            abandoned_minutes=35,
            preferred_channel=Channel.PUSH,
            signals={"loyalty_tier": "circle", "card_holder": True, "fulfillment": "pickup"},
        )
        base.update(kw)
        return CustomerState(**base)

    return _make


# ---- repositories: every test that uses `repo` runs against memory AND PostgreSQL --------
# PostgreSQL tests need a database. Set NUDGEIQ_TEST_DSN, for example
#   postgresql://nudgeiq:nudgeiq@localhost:5432/nudgeiq_test
# The test database is emptied before each test, so never point this at real data.
@pytest.fixture(params=["memory", "postgres"])
def repo(request):
    if request.param == "memory":
        from app.repository import InMemoryRepository

        yield InMemoryRepository()
        return
    import os

    dsn = os.environ.get("NUDGEIQ_TEST_DSN")
    if not dsn:
        pytest.skip("set NUDGEIQ_TEST_DSN to run the PostgreSQL tests")
    from app.repository import PostgresRepository

    pg = PostgresRepository(dsn)
    pg.reset()
    yield pg
    pg.close()


# ---- analytics helpers (5B-7): store decisions with known outcomes ------------------------
OUTCOME_AT = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


@pytest.fixture
def add(repo, make_state):
    """add(kind, ...) stores one decision. outcome: 'order', 'ignored' or None (unresolved)."""
    counter = iter(range(10_000))

    def _add(
        kind: str = "treated",  # treated | holdout | wait
        *,
        outcome: str | None = "ignored",
        channel: Channel = Channel.PUSH,
        timing: str | None = "T+1h",
        offer: str | None = "free_shipping",
        lever: str | None = "loyalty_bonus",
        cohort: str = "cart_high_aov",
        variant_id: str | None = None,
        campaign: str | None = "target_cart",
        version: int | None = 1,
    ) -> Decision:
        i = next(counter)
        state = make_state(customer_id=f"c{i}", cohort_id=cohort)
        repo.save_customer(state)
        chosen = (
            Action.wait()
            if kind == "wait"
            else Action(channel=channel, timing=timing, offer=offer, lever=lever)
        )
        d = Decision(
            decision_id=f"d{i}",
            customer_id=state.customer_id,
            state=state,
            chosen=chosen,
            is_holdout=kind == "holdout",
            variant_id=variant_id,
            use_case_id=campaign,
            campaign_version=version,
        )
        repo.save_decision(d)
        if outcome:
            kind_ = OutcomeType.ORDER_PLACED if outcome == "order" else OutcomeType.IGNORED
            repo.save_outcome(
                Outcome(
                    decision_id=d.decision_id,
                    customer_id=d.customer_id,
                    outcome_type=kind_,
                    occurred_at=OUTCOME_AT,
                )
            )
            repo.save_reward(
                RewardRecord(
                    decision_id=d.decision_id,
                    customer_id=d.customer_id,
                    points=40 if outcome == "order" else 0,
                    reason="test",
                )
            )
        return d

    return _add


@pytest.fixture
def many(add):
    """many(n_order, n_ignored, kind=..., **add_kwargs): a batch of resolved decisions."""

    def _many(n_order: int, n_ignored: int, kind: str = "treated", **kw):
        for _ in range(n_order):
            add(kind, outcome="order", **kw)
        for _ in range(n_ignored):
            add(kind, outcome="ignored", **kw)

    return _many
