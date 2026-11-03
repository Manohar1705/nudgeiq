"""5B-7 over HTTP."""

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app


@pytest.fixture
def client(repo):
    return TestClient(create_app(repo))


def test_breakdown_endpoint_returns_slices(client, many):
    many(6, 4)
    many(2, 8, kind="holdout")
    r = client.get("/insights/uplift/by/channel?min_resolved=5")
    assert r.status_code == 200
    body = r.json()
    assert body["dimension"] == "channel" and body["confidence"] == 0.95
    assert body["slices"][0]["key"] == "push"
    assert body["slices"][0]["uplift_abs"] == pytest.approx(0.4)
    assert body["slices"][0]["enough_data"] is True
    assert body["overall"]["treated"]["resolved"] == 10


def test_every_dimension_is_served(client):
    for dim in ("channel", "timing", "offer", "lever", "cohort", "variant"):
        assert client.get(f"/insights/uplift/by/{dim}").status_code == 200


def test_unknown_dimension_is_422(client):
    assert client.get("/insights/uplift/by/colour").status_code == 422


def test_bad_parameters_are_422(client):
    assert client.get("/insights/uplift/by/channel?min_resolved=0").status_code == 422
    assert client.get("/insights/uplift/by/channel?campaign_version=0").status_code == 422
    r = client.get("/insights/uplift/by/channel?campaign_version=1")
    assert r.status_code == 422 and "campaign_id" in r.text


def test_unknown_campaign_is_404(client):
    assert client.get("/insights/uplift/by/channel?campaign_id=nope").status_code == 404
    assert client.get("/insights/uplift?campaign_id=nope").status_code == 404


def test_campaign_filter_over_http(client, many):
    many(4, 0, campaign="target_cart", version=1)
    many(0, 4, campaign="other", version=1)
    pooled = client.get("/insights/uplift").json()
    mine = client.get("/insights/uplift?campaign_id=target_cart").json()
    assert pooled["treated"]["decisions"] == 8 and mine["treated"]["decisions"] == 4
    r = client.get("/insights/uplift/by/channel?campaign_id=target_cart&campaign_version=1")
    assert r.json()["overall"]["treated"]["orders"] == 4
    v2 = client.get("/insights/uplift/by/channel?campaign_id=target_cart&campaign_version=2")
    assert v2.json()["overall"]["treated"]["decisions"] == 0


def test_breakdown_after_a_real_decision_loop(client):
    customer = {
        "customer_id": "api_9",
        "cohort_id": "cart_high_aov",
        "cart_value": 120.0,
        "abandoned_minutes": 35,
        "preferred_channel": "push",
        "signals": {"loyalty_tier": "circle", "card_holder": True, "fulfillment": "pickup"},
    }
    client.post("/customers", json=customer)
    decision = client.post(
        "/decide", json={"customer_id": "api_9", "as_of": "2026-11-02T10:00:00Z"}
    ).json()
    client.post(
        "/outcomes",
        json={
            "decision_id": decision["decision_id"],
            "customer_id": "api_9",
            "outcome_type": "order_placed",
            "occurred_at": "2026-11-03T12:00:00Z",
        },
    )
    body = client.get("/insights/uplift/by/cohort?campaign_id=target_cart").json()
    assert body["slices"][0]["key"] == "cart_high_aov"
    assert body["slices"][0]["treated"]["orders"] == 1
    assert body["slices"][0]["enough_data"] is False  # one decision is not enough
