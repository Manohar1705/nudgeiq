import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.campaigns import CampaignService
from app.models import CampaignStatus
from app.repository import seed_campaign

AS_OF = "2026-11-02T10:00:00Z"


def customer(customer_id):
    return {
        "customer_id": customer_id,
        "cohort_id": "cart_high_aov",
        "cart_value": 120.0,
        "abandoned_minutes": 35,
        "preferred_channel": "push",
        "signals": {"loyalty_tier": "circle", "card_holder": True, "fulfillment": "pickup"},
    }


def order(decision_id, customer_id):
    return {
        "decision_id": decision_id,
        "customer_id": customer_id,
        "outcome_type": "order_placed",
        "occurred_at": "2026-11-03T12:00:00Z",
    }


def add_ready_campaign(service, config, library, campaign_id):
    """A second valid campaign, as a draft."""
    service.create_draft(config.model_copy(update={"use_case_id": campaign_id}))
    for t in library.templates:
        service.save_template(campaign_id, t)
    for v in library.variants:
        service.save_variant(campaign_id, v)


@pytest.fixture
def client(repo):
    return TestClient(create_app(repo))


@pytest.fixture
def idle_client(repo):
    """A server whose repository has campaigns but none active."""
    seed_campaign(repo, "target_cart")
    CampaignService(repo).archive("target_cart")
    return TestClient(create_app(repo))


# ---- first start and seeding ------------------------------------------------------------


def test_first_start_seeds_the_json_campaign_as_active(client):
    rows = client.get("/campaigns").json()
    assert [r["use_case_id"] for r in rows] == ["target_cart"]
    assert rows[0]["active"] is True
    assert rows[0]["status"] == "active"
    assert rows[0]["version"] == 1
    assert client.get("/health").json()["campaign_version"] == 1


def test_starting_twice_does_not_duplicate_or_reset(repo):
    first = TestClient(create_app(repo))
    service = first.app.state.service
    service.archive("target_cart")
    TestClient(create_app(repo))  # a second start on the same repository
    rows = TestClient(create_app(repo)).get("/campaigns").json()
    assert len(rows) == 1
    assert rows[0]["status"] == "archived"  # the edit was not undone by a re-seed


def test_a_repository_with_campaigns_is_never_seeded(repo, config):
    CampaignService(repo).create_draft(config.model_copy(update={"use_case_id": "mine"}))
    client = TestClient(create_app(repo))
    assert [r["use_case_id"] for r in client.get("/campaigns").json()] == ["mine"]
    assert client.get("/health").json()["status"] == "no_active_campaign"


def test_campaign_detail_comes_from_the_repository(client):
    assert client.get("/campaigns/target_cart").json()["guardrails"]["cooldown_hours"] == 48
    assert client.get("/campaigns/nope").status_code == 404


def test_edited_stored_campaign_is_what_the_api_returns(repo):
    seed_campaign(repo, "target_cart")
    service = CampaignService(repo)
    service.clone("target_cart", "spring", name="Spring recovery")
    client = TestClient(create_app(repo))
    assert client.get("/campaigns/spring").json()["name"] == "Spring recovery"
    by_id = {r["use_case_id"]: r for r in client.get("/campaigns").json()}
    assert by_id["spring"]["status"] == "draft" and by_id["spring"]["active"] is False
    assert by_id["target_cart"]["active"] is True


# ---- nothing live -----------------------------------------------------------------------


def test_health_reports_when_nothing_is_live(idle_client):
    response = idle_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "no_active_campaign"}


def test_decision_endpoints_answer_503_when_nothing_is_live(idle_client):
    idle_client.post("/customers", json=customer("api_1"))
    assert idle_client.post("/decide", json={"customer_id": "api_1"}).status_code == 503
    body = {
        "event_id": "e1",
        "customer_id": "api_1",
        "event_type": "cart_abandoned",
        "occurred_at": AS_OF,
        "payload": {"cart_value": 90, "abandoned_minutes": 40},
    }
    assert idle_client.post("/events", json=body).status_code == 503
    assert idle_client.post("/demo/seed", json={"count": 5}).status_code == 503
    assert "no campaign is active" in idle_client.post("/decide", json={"customer_id": "x"}).text


def test_reading_still_works_when_nothing_is_live(idle_client):
    assert idle_client.get("/campaigns").status_code == 200
    assert idle_client.get("/decisions").json() == []
    assert idle_client.get("/insights/uplift").status_code == 200
    assert idle_client.post("/customers", json=customer("api_1")).status_code == 200


def test_unknown_decision_is_404_not_503_when_nothing_is_live(idle_client):
    bad = {"decision_id": "ghost", "customer_id": "x", "outcome_type": "order_placed"}
    assert idle_client.post("/outcomes", json=bad).status_code == 404


# ---- swapping the live campaign while serving -------------------------------------------


def test_decisions_name_the_campaign_that_made_them(client):
    client.post("/customers", json=customer("api_2"))
    decision = client.post("/decide", json={"customer_id": "api_2", "as_of": AS_OF}).json()
    assert decision["use_case_id"] == "target_cart"
    assert decision["campaign_version"] == 1


def test_swap_the_live_campaign_without_restarting(repo, config, library):
    client = TestClient(create_app(repo))
    runtime = client.app.state.runtime
    add_ready_campaign(client.app.state.service, config, library, "spring")

    client.post("/customers", json=customer("api_2"))
    client.post("/customers", json=customer("api_3"))
    before = client.post("/decide", json={"customer_id": "api_2", "as_of": AS_OF}).json()
    assert before["use_case_id"] == "target_cart"

    runtime.activate("spring", replace_active=True)

    after = client.post("/decide", json={"customer_id": "api_3", "as_of": AS_OF}).json()
    assert after["use_case_id"] == "spring"
    assert client.get("/health").json()["status"] == "ok"
    by_id = {r["use_case_id"]: r["status"] for r in client.get("/campaigns").json()}
    assert by_id == {"target_cart": "archived", "spring": "active"}


def test_late_outcome_is_applied_to_the_campaign_that_made_the_decision(repo, config, library):
    client = TestClient(create_app(repo))
    add_ready_campaign(client.app.state.service, config, library, "spring")
    client.post("/customers", json=customer("api_2"))
    did = client.post("/decide", json={"customer_id": "api_2", "as_of": AS_OF}).json()[
        "decision_id"
    ]

    client.app.state.runtime.activate("spring", replace_active=True)
    response = client.post("/outcomes", json=order(did, "api_2"))

    assert response.status_code == 200
    assert response.json()["reward"]["points"] == 40
    # the OLD campaign learned from it; the new one has learned nothing
    old_policy = repo.load_policy("target_cart")
    assert old_policy is not None and len(old_policy["cells"]) == 1
    assert repo.load_policy("spring") is None
    assert client.get("/health").json()["cells_learned"] == 0


def test_outcome_after_the_live_campaign_is_archived(repo):
    client = TestClient(create_app(repo))
    client.post("/customers", json=customer("api_2"))
    did = client.post("/decide", json={"customer_id": "api_2", "as_of": AS_OF}).json()[
        "decision_id"
    ]
    client.app.state.runtime.archive("target_cart")
    assert client.post("/decide", json={"customer_id": "api_2"}).status_code == 503
    assert client.post("/outcomes", json=order(did, "api_2")).status_code == 200


def test_restart_after_a_swap_serves_the_new_campaign(repo, config, library):
    first = TestClient(create_app(repo))
    add_ready_campaign(first.app.state.service, config, library, "spring")
    first.app.state.runtime.activate("spring", replace_active=True)

    second = TestClient(create_app(repo))  # a fresh process on the same repository
    second.post("/customers", json=customer("api_3"))
    decision = second.post("/decide", json={"customer_id": "api_3", "as_of": AS_OF}).json()
    assert decision["use_case_id"] == "spring"
    assert [
        r["use_case_id"] for r in second.get("/campaigns").json() if r["status"] == "active"
    ] == ["spring"]


def test_service_and_runtime_are_exposed_for_the_next_step(client):
    assert client.app.state.service.get("target_cart").status == CampaignStatus.ACTIVE
    assert client.app.state.runtime.active_campaign_id == "target_cart"
