import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.smoke import main, run_smoke


def _caller(client):
    def call(method, path, body):
        response = client.request(method, path, json=body)
        return response.status_code, response.json()

    return call


@pytest.fixture
def client(repo):
    return TestClient(create_app(repo))


def test_read_only_smoke_passes(client):
    results = run_smoke(_caller(client))
    assert results and all(ok for _, ok, _ in results), results


def test_write_smoke_seeds_and_decides(client):
    results = run_smoke(_caller(client), write=True)
    names = [name for name, _, _ in results]
    assert "seed 5 demo customers" in names and "decide for one customer" in names
    assert all(ok for _, ok, _ in results), results


def test_smoke_reports_an_unreachable_server():
    # Port 9 (discard) is closed on a normal machine; the checks must fail, not crash.
    assert main(["http://127.0.0.1:9"]) == 1
