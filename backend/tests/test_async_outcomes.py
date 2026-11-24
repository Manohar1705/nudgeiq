"""8d-3b step 3c: POST /outcomes in background mode (NUDGEIQ_ASYNC_OUTCOMES).

No Redis or worker is used: the queue is replaced by a stand-in, and the worker's work is run
by calling `process_outcome_record` directly. Tests using `repo` also run on PostgreSQL when
NUDGEIQ_TEST_DSN is set. The whole file is skipped without the `celery` package.
"""

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("celery")

from app.api.main import async_outcomes_enabled, create_app  # noqa: E402
from app.repository import InMemoryRepository  # noqa: E402
from app.worker import tasks  # noqa: E402
from app.worker.outcomes import process_outcome_record  # noqa: E402

AS_OF = "2026-11-02T10:00:00Z"
CUSTOMER = {
    "customer_id": "api_3",
    "cohort_id": "cart_high_aov",
    "cart_value": 120.0,
    "abandoned_minutes": 35,
    "preferred_channel": "push",
    "signals": {"loyalty_tier": "circle", "card_holder": True, "fulfillment": "pickup"},
}


class Queue:
    """Stand-in for enqueue_outcome: remembers what was queued; can be switched off."""

    def __init__(self, up: bool = True) -> None:
        self.up = up
        self.queued: list[int] = []

    def __call__(self, outcome_id: int) -> bool:
        if self.up:
            self.queued.append(outcome_id)
        return self.up


@pytest.fixture
def queue(monkeypatch):
    stand_in = Queue()
    monkeypatch.setattr(tasks, "enqueue_outcome", stand_in)
    return stand_in


@pytest.fixture
def client(repo, queue):
    return TestClient(create_app(repo, async_outcomes=True))


@pytest.fixture
def decision_id(client):
    client.post("/customers", json=CUSTOMER)
    decision = client.post("/decide", json={"customer_id": "api_3", "as_of": AS_OF}).json()
    return decision["decision_id"]


def body(decision_id, kind="order_placed", customer="api_3"):
    return {
        "decision_id": decision_id,
        "customer_id": customer,
        "outcome_type": kind,
        "occurred_at": "2026-11-03T12:00:00Z",
    }


def test_an_outcome_is_stored_pending_queued_and_answered_with_202(client, queue, decision_id):
    response = client.post("/outcomes", json=body(decision_id))
    assert response.status_code == 202
    data = response.json()
    assert (data["status"], data["queued"], data["duplicate"]) == ("pending", True, False)
    assert queue.queued == [data["outcome_id"]]
    # nothing was rewarded or learned yet: that is the worker's job
    assert client.get(f"/decisions/{decision_id}").json()["reward"] is None
    assert client.get("/health").json()["cells_learned"] == 0


def test_the_worker_then_finishes_the_job(client, repo, decision_id):
    outcome_id = client.post("/outcomes", json=body(decision_id)).json()["outcome_id"]
    assert process_outcome_record(repo, outcome_id).status == "processed"
    assert client.get(f"/decisions/{decision_id}").json()["reward"]["points"] == 40
    # the API picks up what the worker learned at its next decision, not before
    assert client.get("/health").json()["cells_learned"] == 0
    client.post("/decide", json={"customer_id": "api_3", "as_of": AS_OF})
    assert client.get("/health").json()["cells_learned"] == 1


def test_when_the_queue_is_down_the_outcome_is_still_accepted_and_stays_pending(
    client, queue, repo, decision_id
):
    queue.up = False
    response = client.post("/outcomes", json=body(decision_id))
    assert response.status_code == 202 and response.json()["queued"] is False
    outcome_id = response.json()["outcome_id"]
    record = repo.get_outcome(outcome_id)
    assert record is not None and record.status == "pending"  # the sweep will queue it


def test_a_repeated_key_is_stored_once_and_queued_again_only_while_pending(
    client, queue, repo, decision_id
):
    headers = {"Idempotency-Key": "order-1"}
    first = client.post("/outcomes", json=body(decision_id), headers=headers).json()
    second = client.post("/outcomes", json=body(decision_id), headers=headers).json()
    assert second["outcome_id"] == first["outcome_id"] and second["duplicate"] is True
    assert len(repo.list_outcomes(decision_id=decision_id)) == 1
    assert queue.queued == [first["outcome_id"]] * 2  # still pending: a lost message is replaced
    process_outcome_record(repo, first["outcome_id"])
    third = client.post("/outcomes", json=body(decision_id), headers=headers).json()
    assert (third["status"], third["queued"]) == ("processed", False)
    assert len(queue.queued) == 2


def test_a_key_reused_for_a_different_outcome_is_a_conflict(client, decision_id):
    headers = {"Idempotency-Key": "k"}
    assert client.post("/outcomes", json=body(decision_id), headers=headers).status_code == 202
    other = body(decision_id, kind="refunded")
    assert client.post("/outcomes", json=other, headers=headers).status_code == 409


def test_bad_outcomes_are_refused_before_anything_is_stored(client, queue, repo, decision_id):
    assert client.post("/outcomes", json=body("ghost")).status_code == 404
    assert (
        client.post("/outcomes", json=body(decision_id, customer="someone_else")).status_code == 422
    )
    assert queue.queued == [] and repo.list_outcomes(decision_id=decision_id) == []


def test_without_the_switch_the_answer_is_still_immediate(repo, queue):
    client = TestClient(create_app(repo, async_outcomes=False))
    client.post("/customers", json=CUSTOMER)
    did = client.post("/decide", json={"customer_id": "api_3", "as_of": AS_OF}).json()[
        "decision_id"
    ]
    response = client.post("/outcomes", json=body(did))
    assert response.status_code == 200 and response.json()["reward"]["points"] == 40
    assert queue.queued == []


def test_the_switch_reads_the_environment(monkeypatch):
    assert async_outcomes_enabled({}) is False
    assert async_outcomes_enabled({"NUDGEIQ_ASYNC_OUTCOMES": " On "}) is True
    assert async_outcomes_enabled({"NUDGEIQ_ASYNC_OUTCOMES": "0"}) is False


def test_the_switch_on_without_postgresql_stops_start_up(monkeypatch):
    monkeypatch.setenv("NUDGEIQ_ASYNC_OUTCOMES", "1")
    with pytest.raises(ValueError, match="NUDGEIQ_ASYNC_OUTCOMES"):
        create_app(InMemoryRepository())
