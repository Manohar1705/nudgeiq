import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app

AS_OF = "2026-11-02T10:00:00Z"
CUSTOMER = {
    "customer_id": "api_2",
    "cohort_id": "cart_high_aov",
    "cart_value": 120.0,
    "abandoned_minutes": 35,
    "preferred_channel": "push",
    "signals": {"loyalty_tier": "circle", "card_holder": True, "fulfillment": "pickup"},
}


@pytest.fixture
def client(repo):
    return TestClient(create_app(repo))


def test_health_and_campaigns(client):
    assert client.get("/health").json()["status"] == "ok"
    campaigns = client.get("/campaigns").json()
    assert campaigns[0]["use_case_id"] == "target_cart" and campaigns[0]["active"]
    assert client.get("/campaigns/target_cart").json()["guardrails"]["cooldown_hours"] == 48
    assert client.get("/campaigns/nope").status_code == 404


def test_full_loop_over_http(client):
    assert client.post("/customers", json=CUSTOMER).status_code == 200
    decision = client.post("/decide", json={"customer_id": "api_2", "as_of": AS_OF}).json()
    assert decision["chosen"]["channel"] and decision["message"]
    did = decision["decision_id"]

    result = client.post(
        "/outcomes",
        json={
            "decision_id": did,
            "customer_id": "api_2",
            "outcome_type": "order_placed",
            "occurred_at": "2026-11-03T12:00:00Z",
        },
    ).json()
    assert result["reward"]["points"] == 40 and result["next_step"] == "closed"

    detail = client.get(f"/decisions/{did}").json()
    assert detail["reward"]["points"] == 40 and len(detail["outcomes"]) == 1
    assert [d["decision_id"] for d in client.get("/decisions?customer_id=api_2").json()] == [did]
    assert client.get("/insights/uplift").json()["treated"]["orders"] == 1
    assert client.get("/health").json()["cells_learned"] == 1


def test_event_endpoint(client):
    client.post("/customers", json=CUSTOMER)
    body = {
        "event_id": "e1",
        "customer_id": "api_2",
        "event_type": "cart_abandoned",
        "occurred_at": AS_OF,
        "payload": {"cart_value": 90, "abandoned_minutes": 40},
    }
    assert client.post("/events", json=body).json()["state"]["cart_value"] == 90
    body["event_type"] = "nonsense"
    assert client.post("/events", json=body).status_code == 422


def test_errors_are_clean(client):
    assert client.post("/decide", json={"customer_id": "ghost"}).status_code == 404
    assert client.get("/customers/ghost").status_code == 404
    assert client.get("/decisions/ghost").status_code == 404
    missing = {"decision_id": "ghost", "customer_id": "x", "outcome_type": "order_placed"}
    assert client.post("/outcomes", json=missing).status_code == 404
    assert client.post("/decide", json={}).status_code == 422
    assert client.post("/customers", json={**CUSTOMER, "cart_value": -1}).status_code == 422


def test_outcome_for_wrong_customer_is_rejected(client):
    client.post("/customers", json=CUSTOMER)
    did = client.post("/decide", json={"customer_id": "api_2", "as_of": AS_OF}).json()[
        "decision_id"
    ]
    bad = {"decision_id": did, "customer_id": "other", "outcome_type": "order_placed"}
    assert client.post("/outcomes", json=bad).status_code == 422


def test_seed_and_list(client):
    assert client.post("/demo/seed", json={"count": 20, "seed": 1}).json()["created"] == 20
    assert len(client.get("/customers?limit=5").json()) == 5
    assert client.post("/demo/seed", json={"count": 0}).status_code == 422


def test_data_file_survives_restart(tmp_path):
    path = tmp_path / "d.json"
    from app.repository import JsonFileRepository

    first = TestClient(create_app(JsonFileRepository(path)))
    first.post("/customers", json=CUSTOMER)
    did = first.post("/decide", json={"customer_id": "api_2", "as_of": AS_OF}).json()["decision_id"]
    first.post(
        "/outcomes",
        json={
            "decision_id": did,
            "customer_id": "api_2",
            "outcome_type": "cta_clicked",
            "occurred_at": "2026-11-03T12:00:00Z",
        },
    )
    second = TestClient(create_app(JsonFileRepository(path)))
    assert second.get(f"/decisions/{did}").status_code == 200
    assert second.get("/health").json()["cells_learned"] == 1
