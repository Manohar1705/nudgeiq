import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.models import VariantStatus

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
    """A server with the seeded target_cart campaign live."""
    return TestClient(create_app(repo))


def cfg_json(config, campaign_id, **changes):
    data = config.model_dump(mode="json")
    data["use_case_id"] = campaign_id
    data.update(changes)
    return data


def build_ready(client, config, library, campaign_id):
    """Create a draft over HTTP and fill it with the sample templates and variants."""
    assert client.post("/campaigns", json=cfg_json(config, campaign_id)).status_code == 201
    for t in library.templates:
        url = f"/campaigns/{campaign_id}/templates/{t.template_id}"
        assert client.put(url, json=t.model_dump(mode="json")).status_code == 200
    for v in library.variants:
        url = f"/campaigns/{campaign_id}/variants/{v.variant_id}"
        assert client.put(url, json=v.model_dump(mode="json")).status_code == 200


def statuses(client):
    return {r["use_case_id"]: r["status"] for r in client.get("/campaigns").json()}


def codes(validation):
    return {i["code"] for i in validation["issues"]}


# ---- create -----------------------------------------------------------------------------


def test_create_a_draft(client, config):
    response = client.post("/campaigns", json=cfg_json(config, "spring"))
    assert response.status_code == 201
    body = response.json()
    assert body["campaign"]["status"] == "draft"
    assert body["campaign"]["version"] == 1
    assert body["validation"]["can_activate"] is False  # nothing approved to send yet
    assert "no_approved_variants" in codes(body["validation"])
    assert statuses(client)["spring"] == "draft"


def test_a_draft_with_validation_errors_is_still_saved(client, config):
    response = client.post("/campaigns", json=cfg_json(config, "spring", name="  "))
    assert response.status_code == 201
    assert "empty_name" in codes(response.json()["validation"])


def test_create_over_an_existing_id_is_409(client, config):
    assert client.post("/campaigns", json=cfg_json(config, "target_cart")).status_code == 409


def test_create_with_a_schema_error_is_422_and_saves_nothing(client, config):
    data = cfg_json(config, "spring")
    data["cohorts"][0]["size"] = -1
    assert client.post("/campaigns", json=data).status_code == 422
    assert "spring" not in statuses(client)


# ---- update -----------------------------------------------------------------------------


def test_update_a_draft_bumps_the_version(client, config):
    client.post("/campaigns", json=cfg_json(config, "spring"))
    response = client.put("/campaigns/spring", json=cfg_json(config, "spring", name="Spring v2"))
    assert response.status_code == 200
    campaign = response.json()["campaign"]
    assert campaign["version"] == 2 and campaign["config"]["name"] == "Spring v2"


def test_update_unknown_campaign_is_404(client, config):
    assert client.put("/campaigns/nope", json=cfg_json(config, "nope")).status_code == 404


def test_update_the_live_campaign_is_409(client, config):
    response = client.put("/campaigns/target_cart", json=cfg_json(config, "target_cart"))
    assert response.status_code == 409
    assert "draft" in response.json()["detail"]


def test_update_cannot_change_the_id(client, config):
    client.post("/campaigns", json=cfg_json(config, "spring"))
    assert client.put("/campaigns/spring", json=cfg_json(config, "other")).status_code == 422


# ---- clone ------------------------------------------------------------------------------


def test_clone_the_live_campaign(client):
    response = client.post(
        "/campaigns/target_cart/clone", json={"new_id": "spring", "name": "Spring"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["campaign"]["status"] == "draft"
    assert body["campaign"]["config"]["name"] == "Spring"
    assert body["validation"]["can_activate"] is True  # copied approved variants
    assert (
        client.get("/campaigns/spring/templates").json()
        == client.get("/campaigns/target_cart/templates").json()
    )
    assert statuses(client)["target_cart"] == "active"  # the source is untouched


def test_clone_over_an_existing_id_is_409(client):
    response = client.post("/campaigns/target_cart/clone", json={"new_id": "target_cart"})
    assert response.status_code == 409


def test_clone_unknown_source_is_404(client):
    assert client.post("/campaigns/nope/clone", json={"new_id": "x"}).status_code == 404


def test_clone_needs_a_new_id(client):
    assert client.post("/campaigns/target_cart/clone", json={"new_id": ""}).status_code == 422
    assert client.post("/campaigns/target_cart/clone", json={}).status_code == 422


# ---- activate ---------------------------------------------------------------------------


def test_activating_with_replace_swaps_what_the_server_serves(client, config, library):
    build_ready(client, config, library, "spring")
    response = client.post("/campaigns/spring/activate", json={"replace_active": True})
    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert statuses(client) == {"target_cart": "archived", "spring": "active"}

    client.post("/customers", json=CUSTOMER)
    decision = client.post("/decide", json={"customer_id": "api_2", "as_of": AS_OF}).json()
    assert decision["use_case_id"] == "spring"  # served immediately, no restart


def test_activating_a_second_campaign_without_replace_is_409(client, config, library):
    build_ready(client, config, library, "spring")
    response = client.post("/campaigns/spring/activate")
    assert response.status_code == 409
    assert "already active" in response.json()["detail"]
    assert statuses(client) == {"target_cart": "active", "spring": "draft"}


def test_activating_with_nothing_live_needs_no_body(client, config, library):
    build_ready(client, config, library, "spring")
    assert client.post("/campaigns/target_cart/archive").status_code == 200
    assert client.post("/campaigns/spring/activate").status_code == 200
    assert statuses(client)["spring"] == "active"


def test_activation_blocked_by_validation_is_422_with_the_report(client, config):
    client.post("/campaigns", json=cfg_json(config, "spring"))  # no variants
    response = client.post("/campaigns/spring/activate", json={"replace_active": True})
    assert response.status_code == 422
    body = response.json()
    assert body["validation"]["can_activate"] is False
    assert "no_approved_variants" in codes(body["validation"])
    assert statuses(client) == {"target_cart": "active", "spring": "draft"}


def test_activate_unknown_is_404(client):
    assert client.post("/campaigns/nope/activate").status_code == 404


def test_an_archived_campaign_cannot_be_activated(client, config, library):
    build_ready(client, config, library, "spring")
    client.post("/campaigns/spring/archive")
    response = client.post("/campaigns/spring/activate", json={"replace_active": True})
    assert response.status_code == 409


# ---- archive ----------------------------------------------------------------------------


def test_archiving_the_live_campaign_stops_serving(client):
    response = client.post("/campaigns/target_cart/archive")
    assert response.status_code == 200 and response.json()["status"] == "archived"
    assert client.post("/decide", json={"customer_id": "api_2"}).status_code == 503
    assert client.get("/health").json() == {"status": "no_active_campaign"}


def test_archive_a_draft_then_again_is_409(client, config):
    client.post("/campaigns", json=cfg_json(config, "spring"))
    assert client.post("/campaigns/spring/archive").status_code == 200
    assert client.post("/campaigns/spring/archive").status_code == 409


def test_archive_unknown_is_404(client):
    assert client.post("/campaigns/nope/archive").status_code == 404


# ---- validation -------------------------------------------------------------------------


def test_stored_campaign_validation_report(client):
    body = client.get("/campaigns/target_cart/validation").json()
    assert body["can_activate"] is True
    assert body["error_count"] == 0 and body["warning_count"] == 0 and body["issues"] == []


def test_validation_of_unknown_campaign_is_404(client):
    assert client.get("/campaigns/nope/validation").status_code == 404


def test_dry_run_of_valid_json_is_clean(client, config, library):
    body = client.post(
        "/campaigns/validate",
        json={"config": config.model_dump(mode="json"), "library": library.model_dump(mode="json")},
    ).json()
    assert body["can_activate"] is True and body["issues"] == []


def test_dry_run_reports_schema_errors_with_field_paths(client, config):
    data = config.model_dump(mode="json")
    del data["name"]
    body = client.post("/campaigns/validate", json={"config": data}).json()
    assert body["can_activate"] is False
    assert any(i["code"] == "schema" and i["path"] == "config.name" for i in body["issues"])


def test_dry_run_saves_nothing(client, config):
    client.post("/campaigns/validate", json={"config": cfg_json(config, "ghost")})
    assert "ghost" not in statuses(client)


# ---- templates --------------------------------------------------------------------------


def test_list_templates(client, library):
    ids = [t["template_id"] for t in client.get("/campaigns/target_cart/templates").json()]
    assert ids == [t.template_id for t in library.templates]


def test_save_a_template_on_a_draft(client, config, library):
    client.post("/campaigns", json=cfg_json(config, "spring"))
    template = library.templates[0].model_dump(mode="json")
    response = client.put(f"/campaigns/spring/templates/{template['template_id']}", json=template)
    assert response.status_code == 200
    listed = client.get("/campaigns/spring/templates").json()
    assert [t["template_id"] for t in listed] == [template["template_id"]]


def test_save_a_template_on_the_live_campaign_is_409(client, library):
    template = library.templates[0].model_dump(mode="json")
    url = f"/campaigns/target_cart/templates/{template['template_id']}"
    assert client.put(url, json=template).status_code == 409


def test_template_id_must_match_the_url(client, config, library):
    client.post("/campaigns", json=cfg_json(config, "spring"))
    template = library.templates[0].model_dump(mode="json")
    assert client.put("/campaigns/spring/templates/other", json=template).status_code == 422


def test_save_template_on_unknown_campaign_is_404(client, library):
    template = library.templates[0].model_dump(mode="json")
    url = f"/campaigns/nope/templates/{template['template_id']}"
    assert client.put(url, json=template).status_code == 404


# ---- variants ---------------------------------------------------------------------------


def test_list_variants_and_filter(client, library):
    everything = client.get("/campaigns/target_cart/variants").json()
    assert len(everything) == len(library.variants)

    approved = client.get("/campaigns/target_cart/variants?status=approved").json()
    expected = [v for v in library.variants if v.status == VariantStatus.APPROVED]
    assert [v["variant_id"] for v in approved] == [v.variant_id for v in expected]

    tid = library.variants[0].template_id
    scoped = client.get(f"/campaigns/target_cart/variants?template_id={tid}").json()
    assert {v["template_id"] for v in scoped} == {tid}


def test_approve_a_pending_variant_on_a_draft(client, config, library):
    build_ready(client, config, library, "spring")
    pending = next(v for v in library.variants if v.status == VariantStatus.PENDING)
    data = pending.model_dump(mode="json")
    data["status"] = "approved"
    url = f"/campaigns/spring/variants/{pending.variant_id}"
    assert client.put(url, json=data).status_code == 200
    approved = client.get("/campaigns/spring/variants?status=approved").json()
    assert pending.variant_id in {v["variant_id"] for v in approved}


def test_variant_for_a_missing_template_is_422(client, config, library):
    client.post("/campaigns", json=cfg_json(config, "spring"))
    data = library.variants[0].model_dump(mode="json")
    data["template_id"] = "missing"
    response = client.put(f"/campaigns/spring/variants/{data['variant_id']}", json=data)
    assert response.status_code == 422
    assert "missing" in response.json()["detail"]


def test_save_a_variant_on_the_live_campaign_is_409(client, library):
    data = library.variants[0].model_dump(mode="json")
    url = f"/campaigns/target_cart/variants/{data['variant_id']}"
    assert client.put(url, json=data).status_code == 409


def test_variant_id_must_match_the_url(client, config, library):
    build_ready(client, config, library, "spring")
    data = library.variants[0].model_dump(mode="json")
    assert client.put("/campaigns/spring/variants/other", json=data).status_code == 422


def test_variants_of_unknown_campaign_is_404(client):
    assert client.get("/campaigns/nope/variants").status_code == 404
    assert client.get("/campaigns/nope/templates").status_code == 404


# ---- the whole workflow over HTTP -------------------------------------------------------


def test_clone_edit_activate_over_http(client, config):
    """The intended way to change a live campaign."""
    assert client.post("/campaigns/target_cart/clone", json={"new_id": "v2"}).status_code == 201
    edited = cfg_json(config, "v2", name="Improved")
    assert client.put("/campaigns/v2", json=edited).json()["campaign"]["version"] == 2
    assert client.get("/campaigns/v2/validation").json()["can_activate"] is True

    assert client.post("/campaigns/v2/activate", json={"replace_active": True}).status_code == 200
    assert statuses(client) == {"target_cart": "archived", "v2": "active"}
    assert client.get("/campaigns/v2").json()["name"] == "Improved"
    assert client.get("/health").json()["campaign_version"] == 2
