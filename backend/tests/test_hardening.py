import logging

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.hardening import (
    LOGGER_NAME,
    cors_origins_from_env,
    log_level_from_env,
)
from app.api.main import create_app


@pytest.fixture
def client(repo):
    app = create_app(repo)

    @app.get("/_boom")
    def boom():
        raise RuntimeError("secret internal detail")

    @app.get("/_dbdown")
    def dbdown():
        raise psycopg.OperationalError("connection refused to db:5432")

    return TestClient(app)


def test_every_response_has_a_request_id(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert len(response.headers["x-request-id"]) >= 8


def test_a_safe_incoming_request_id_is_reused(client):
    response = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"


def test_an_unsafe_request_id_is_replaced(client):
    response = client.get("/health", headers={"X-Request-ID": "bad id\twith spaces"})
    rid = response.headers["x-request-id"]
    assert rid != "bad id\twith spaces" and " " not in rid


def test_unexpected_error_is_json_500_without_details(client, caplog):
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        response = client.get("/_boom")
    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "internal server error"
    assert body["request_id"] == response.headers["x-request-id"]
    assert "secret internal detail" not in response.text  # not shown to the client...
    assert "secret internal detail" in caplog.text  # ...but logged for the developer


def test_database_down_is_json_503(client):
    response = client.get("/_dbdown")
    assert response.status_code == 503
    assert response.json()["detail"] == "database unavailable"
    assert "x-request-id" in response.headers


def test_requests_are_logged(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        client.get("/campaigns")
    assert any("/campaigns" in r.getMessage() and "200" in r.getMessage() for r in caplog.records)


def test_health_checks_are_not_logged_at_info(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        client.get("/health")
    assert not any("/health" in r.getMessage() for r in caplog.records)


def test_existing_errors_keep_their_shape(client):
    assert client.get("/campaigns/does-not-exist").status_code == 404


def test_no_cors_headers_by_default(client):
    response = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert "access-control-allow-origin" not in response.headers


def test_cors_when_origins_are_set(repo, monkeypatch):
    monkeypatch.setenv("NUDGEIQ_CORS_ORIGINS", "http://localhost:5173, http://example.test")
    client = TestClient(create_app(repo))
    ok = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    other = client.get("/health", headers={"Origin": "http://evil.test"})
    assert "access-control-allow-origin" not in other.headers


def test_cors_origin_parsing():
    assert cors_origins_from_env({}) == []
    assert cors_origins_from_env({"NUDGEIQ_CORS_ORIGINS": " a , ,b "}) == ["a", "b"]


def test_log_level_parsing():
    assert log_level_from_env({}) == logging.INFO
    assert log_level_from_env({"NUDGEIQ_LOG_LEVEL": "debug"}) == logging.DEBUG
    assert log_level_from_env({"NUDGEIQ_LOG_LEVEL": " Warning "}) == logging.WARNING


def test_bad_log_level_fails_early(repo, monkeypatch):
    with pytest.raises(ValueError, match="NUDGEIQ_LOG_LEVEL"):
        log_level_from_env({"NUDGEIQ_LOG_LEVEL": "loud"})
    monkeypatch.setenv("NUDGEIQ_LOG_LEVEL", "loud")
    with pytest.raises(ValueError, match="NUDGEIQ_LOG_LEVEL"):
        create_app(repo)
