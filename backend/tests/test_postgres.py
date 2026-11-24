"""PostgreSQL-only behaviour: constraints, restarts, schema. Skipped without a database."""

import os
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("psycopg")
DSN = os.environ.get("NUDGEIQ_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set NUDGEIQ_TEST_DSN to run PostgreSQL tests")

import psycopg  # noqa: E402

from app.models import Decision, Outcome, OutcomeType  # noqa: E402
from app.orchestrator import Orchestrator  # noqa: E402
from app.repository import PostgresRepository  # noqa: E402

T = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


@pytest.fixture
def pg():
    repo = PostgresRepository(DSN)
    repo.reset()
    yield repo
    repo.close()


def test_schema_can_be_created_twice(pg):
    pg.create_schema()
    pg.create_schema()


def test_decision_for_unknown_customer_is_rejected(pg, make_state):
    d = Decision(
        decision_id="d1", customer_id="ghost", state=make_state(customer_id="ghost"), created_at=T
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg.save_decision(d)


def test_outcome_for_unknown_decision_is_rejected(pg, make_state):
    pg.save_customer(make_state())
    bad = Outcome(
        decision_id="nope", customer_id="c1", outcome_type=OutcomeType.ORDER_PLACED, occurred_at=T
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg.save_outcome(bad)


def test_bad_values_are_rejected_by_the_database(pg, make_state):
    pg.save_customer(make_state())
    with pytest.raises(psycopg.errors.CheckViolation):
        pg._run("UPDATE customers SET cart_value = -1 WHERE customer_id = 'c1'")  # noqa: SLF001


def test_data_and_learning_survive_a_restart(pg, config, library, make_state):
    orch = Orchestrator(pg, config, library, seed=7)
    pg.save_customer(make_state())
    d = orch.decide("c1", T)
    assert not d.chosen.is_wait
    orch.record_outcome(
        Outcome(
            decision_id=d.decision_id,
            customer_id="c1",
            outcome_type=OutcomeType.ORDER_PLACED,
            occurred_at=T + timedelta(hours=30),
        )
    )

    second = PostgresRepository(DSN, create_schema=False)  # a new connection = a restart
    try:
        restarted = Orchestrator(second, config, library, seed=7)
        assert restarted.master.times_tried(d.bucket, d.chosen) == 1
        got = second.get_decision(d.decision_id)
        assert got is not None and got.message == d.message
        assert second.get_contact_times("c1") == [d.scheduled_for]
        j = second.get_journey("c1")
        assert j is not None and j.status.value == "converted"
    finally:
        second.close()


def test_sql_can_answer_business_questions(pg, config, library, make_state):
    """The point of real columns: plain SQL works for analytics."""
    orch = Orchestrator(pg, config, library, seed=1)
    for i in range(5):
        pg.save_customer(make_state(customer_id=f"s{i}"))
        orch.decide(f"s{i}", T)
    rows = pg._all(  # noqa: SLF001
        "SELECT chosen_channel, count(*) AS n FROM decisions GROUP BY chosen_channel"
    )
    assert sum(r["n"] for r in rows) == 5


# ---- 5B: campaigns, templates, variants ---------------------------------------------------
def _campaign_with_variant(pg, config, library):
    from app.models import Campaign

    campaign = Campaign(campaign_id=config.use_case_id, config=config, created_at=T, updated_at=T)
    pg.save_campaign(campaign)
    tpl = library.templates[0]
    pg.save_template(campaign.campaign_id, tpl)
    var = next(v for v in library.variants if v.template_id == tpl.template_id)
    pg.save_variant(campaign.campaign_id, var)
    return campaign, tpl, var


def test_campaigns_templates_and_variants_survive_a_restart(pg, config, library):
    campaign, tpl, var = _campaign_with_variant(pg, config, library)
    again = PostgresRepository(DSN)
    try:
        assert again.get_campaign(campaign.campaign_id) == campaign
        assert again.get_template(campaign.campaign_id, tpl.template_id) == tpl
        assert again.get_variant(campaign.campaign_id, var.variant_id) == var
    finally:
        again.close()


def test_deleting_a_campaign_removes_its_templates_and_variants(pg, config, library):
    campaign, tpl, var = _campaign_with_variant(pg, config, library)
    pg._run("DELETE FROM campaigns WHERE campaign_id = %s", (campaign.campaign_id,))  # noqa: SLF001
    assert pg.list_templates(campaign.campaign_id) == []
    assert pg.list_variants(campaign.campaign_id) == []


def test_database_rejects_an_active_campaign_without_activated_at(pg, config):
    from app.models import Campaign

    pg.save_campaign(Campaign(campaign_id=config.use_case_id, config=config))
    with pytest.raises(psycopg.errors.CheckViolation):
        pg._run(  # noqa: SLF001
            "UPDATE campaigns SET status = 'active' WHERE campaign_id = %s", (config.use_case_id,)
        )
