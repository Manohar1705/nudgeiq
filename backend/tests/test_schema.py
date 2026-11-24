"""Checks on schema.sql itself. These need PostgreSQL (set NUDGEIQ_TEST_DSN)."""

import os

import psycopg
import pytest
from psycopg.rows import dict_row

from app.repository import PostgresRepository

DSN = os.environ.get("NUDGEIQ_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set NUDGEIQ_TEST_DSN to run the schema tests")

CONFIG = "'{}'::jsonb"


@pytest.fixture
def pg():
    repo = PostgresRepository(DSN)
    repo.reset()
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    yield conn
    conn.close()
    repo.close()


def _campaign(pg, cid="c1", status="draft"):
    pg.execute(
        f"INSERT INTO campaigns (campaign_id, name, trigger_event, status, config) "
        f"VALUES (%s, 'n', 'cart_abandoned', %s, {CONFIG})",
        (cid, status),
    )


def _template(pg, cid="c1", tid="t1", channel="push"):
    pg.execute(
        "INSERT INTO templates (campaign_id, template_id, name, channel, body) "
        "VALUES (%s, %s, 'n', %s, 'Hi {{name}}')",
        (cid, tid, channel),
    )


def _variant(pg, cid="c1", vid="v1", tid="t1", **kw):
    status = kw.get("status", "pending")
    source = kw.get("source", "human")
    pg.execute(
        "INSERT INTO variants (campaign_id, variant_id, template_id, text, principle, tone,"
        " status, source) VALUES (%s, %s, %s, 'Hi', 'p', 'warm', %s, %s)",
        (cid, vid, tid, status, source),
    )


def test_new_tables_and_columns_exist(pg):
    tables = {
        r["table_name"]
        for r in pg.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    }
    assert {"campaigns", "templates", "variants"} <= tables
    cols = {
        r["column_name"]
        for r in pg.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'decisions'"
        )
    }
    assert {"use_case_id", "campaign_version", "template_id"} <= cols


def test_schema_can_run_twice(pg):
    PostgresRepository(DSN).close()  # a second run must not fail


def test_5a_database_upgrades_in_place(pg):
    for col in ("use_case_id", "campaign_version", "template_id"):
        pg.execute(f"ALTER TABLE decisions DROP COLUMN {col}")
    pg.execute("DROP TABLE variants, templates, campaigns")
    PostgresRepository(DSN).close()  # re-running the schema restores everything
    _campaign(pg)
    _template(pg)
    _variant(pg)


def test_campaign_status_is_constrained(pg):
    with pytest.raises(psycopg.errors.CheckViolation):
        _campaign(pg, status="paused")


def test_campaign_status_needs_matching_timestamps(pg):
    with pytest.raises(psycopg.errors.CheckViolation):
        _campaign(pg, "a", status="active")
    with pytest.raises(psycopg.errors.CheckViolation):
        _campaign(pg, "b", status="archived")
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            f"INSERT INTO campaigns (campaign_id, name, trigger_event, status, config,"
            f" archived_at) VALUES ('d', 'n', 't', 'draft', {CONFIG}, now())"
        )
    pg.execute(
        f"INSERT INTO campaigns (campaign_id, name, trigger_event, status, config,"
        f" activated_at) VALUES ('ok', 'n', 't', 'active', {CONFIG}, now())"
    )


def test_version_must_be_at_least_one(pg):
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            f"INSERT INTO campaigns (campaign_id, name, trigger_event, version, config)"
            f" VALUES ('x', 'n', 't', 0, {CONFIG})"
        )


def test_template_needs_a_campaign_and_a_known_channel(pg):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _template(pg, cid="ghost")
    _campaign(pg)
    with pytest.raises(psycopg.errors.CheckViolation):
        _template(pg, channel="pigeon")
    _template(pg)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _template(pg)


def test_same_template_id_is_allowed_in_different_campaigns(pg):
    _campaign(pg, "c1")
    _campaign(pg, "c2")
    _template(pg, "c1", "t1")
    _template(pg, "c2", "t1")


def test_variant_must_point_at_a_template_in_the_same_campaign(pg):
    _campaign(pg, "c1")
    _campaign(pg, "c2")
    _template(pg, "c1", "t1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _variant(pg, "c2", "v1", "t1")  # t1 exists, but only in c1


def test_variant_status_and_source_are_constrained(pg):
    _campaign(pg)
    _template(pg)
    with pytest.raises(psycopg.errors.CheckViolation):
        _variant(pg, status="maybe")
    with pytest.raises(psycopg.errors.CheckViolation):
        _variant(pg, source="intern")
    _variant(pg, vid="v_llm", source="llm")
    row = pg.execute("SELECT status, source FROM variants WHERE variant_id = 'v_llm'").fetchone()
    assert row == {"status": "pending", "source": "llm"}


def test_deleting_a_campaign_removes_its_templates_and_variants(pg):
    _campaign(pg)
    _template(pg)
    _variant(pg)
    pg.execute("DELETE FROM campaigns WHERE campaign_id = 'c1'")
    assert pg.execute("SELECT count(*) AS n FROM templates").fetchone()["n"] == 0
    assert pg.execute("SELECT count(*) AS n FROM variants").fetchone()["n"] == 0


def test_deleting_a_template_removes_its_variants(pg):
    _campaign(pg)
    _template(pg)
    _variant(pg)
    pg.execute("DELETE FROM templates WHERE template_id = 't1'")
    assert pg.execute("SELECT count(*) AS n FROM variants").fetchone()["n"] == 0


def test_reset_clears_the_new_tables(pg):
    _campaign(pg)
    _template(pg)
    _variant(pg)
    repo = PostgresRepository(DSN)
    repo.reset()
    repo.close()
    assert pg.execute("SELECT count(*) AS n FROM campaigns").fetchone()["n"] == 0
