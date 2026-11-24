"""The server must start and serve everything else when langgraph is not installed (8e-3)."""

from fastapi.testclient import TestClient

from app.api.main import create_app


def test_variant_runs_answer_501_when_langgraph_is_missing(monkeypatch, repo, config):
    def missing(*args, **kwargs):
        raise ImportError("No module named 'langgraph'")

    monkeypatch.setattr("app.api.variant_runs.VariantRunService", missing)
    api = TestClient(create_app(repo))

    data = config.model_dump(mode="json")
    data["use_case_id"] = "spring"
    assert api.post("/campaigns", json=data).status_code == 201  # the rest still works

    response = api.post(
        "/campaigns/spring/variant-runs", json={"template_id": "t_push_still_there"}
    )
    assert response.status_code == 501 and "langgraph" in response.json()["detail"]
