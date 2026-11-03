"""/insights/messages, /insights/guardrails and /demo/respond."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.models import Channel, Outcome, OutcomeType
from app.orchestrator import guardrail_summary, message_funnel

OUTCOME_AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(repo):
    return TestClient(create_app(repo))


def _outcome(repo, d, kind):
    repo.save_outcome(
        Outcome(
            decision_id=d.decision_id,
            customer_id=d.customer_id,
            outcome_type=kind,
            occurred_at=OUTCOME_AT,
        )
    )


# ---------------------------------------------------------------- message funnel
def test_funnel_counts_each_decision_once_per_outcome(repo, add):
    a = add("treated", variant_id="v1", outcome="order")
    _outcome(repo, a, OutcomeType.MESSAGE_OPENED)
    _outcome(repo, a, OutcomeType.MESSAGE_OPENED)  # duplicates must not double count
    _outcome(repo, a, OutcomeType.CTA_CLICKED)
    b = add("treated", variant_id="v1", outcome="ignored")
    _outcome(repo, b, OutcomeType.MESSAGE_OPENED)
    add("treated", variant_id="v2", channel=Channel.EMAIL, outcome=None)

    rows = {r.variant_id: r for r in message_funnel(repo)}
    v1 = rows["v1"]
    assert (v1.sent, v1.opened, v1.clicked, v1.orders) == (2, 2, 1, 1)
    assert v1.avg_reward == pytest.approx(20.0)  # (40 + 0) / 2
    assert v1.channel == "push"
    assert (rows["v2"].sent, rows["v2"].opened, rows["v2"].channel) == (1, 0, "email")
    assert rows["v2"].avg_reward == 0.0


def test_funnel_skips_holdout_wait_and_decisions_without_a_variant(repo, add):
    add("holdout", variant_id="v1")
    add("wait", variant_id="v1")
    add("treated", variant_id=None)
    assert message_funnel(repo) == []


def test_funnel_filters_by_campaign(repo, add):
    add("treated", variant_id="v1", campaign="target_cart")
    add("treated", variant_id="v9", campaign="other")
    assert [r.variant_id for r in message_funnel(repo, "other")] == ["v9"]


# ---------------------------------------------------------------- guardrails
def test_guardrail_counts(repo, add):
    t1 = add("treated")
    _outcome(repo, t1, OutcomeType.UNSUBSCRIBED)
    _outcome(repo, t1, OutcomeType.UNSUBSCRIBED)
    t2 = add("treated")
    _outcome(repo, t2, OutcomeType.REFUNDED)
    _outcome(repo, t2, OutcomeType.APP_OPENED)
    h = add("holdout")
    _outcome(repo, h, OutcomeType.APP_OPENED)
    add("wait")  # not part of either group

    g = guardrail_summary(repo)
    assert g.treated.model_dump() == {"sent": 2, "unsubscribed": 1, "refunded": 1, "app_opened": 1}
    assert g.holdout.model_dump() == {"sent": 1, "unsubscribed": 0, "refunded": 0, "app_opened": 1}


def test_new_insight_endpoints_on_an_empty_store(client):
    assert client.get("/insights/messages").json() == []
    body = client.get("/insights/guardrails").json()
    assert body["treated"]["sent"] == 0 and body["holdout"]["sent"] == 0
    assert client.get("/insights/messages?campaign_id=nope").status_code == 404
    assert client.get("/insights/guardrails?campaign_id=nope").status_code == 404


# ---------------------------------------------------------------- /demo/respond
def _run(client, n=60):
    client.post("/demo/seed", json={"count": n, "seed": 3})
    customers = client.get("/customers?limit=500").json()
    decisions = [
        client.post(
            "/decide", json={"customer_id": c["customer_id"], "as_of": "2026-11-03T20:00:00Z"}
        ).json()
        for c in customers
    ]
    return decisions


def test_respond_records_outcomes_and_a_reward(client):
    decisions = _run(client)
    nudged = next(
        d for d in decisions if d["chosen"]["channel"] is not None and not d["is_holdout"]
    )
    r = client.post("/demo/respond", json={"decision_id": nudged["decision_id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["nudged"] is True and body["outcomes"] and body["reward"] is not None
    assert body["policy_updated"] is True
    stored = client.get(f"/decisions/{nudged['decision_id']}").json()
    assert {o["outcome_type"] for o in stored["outcomes"]} == set(body["outcomes"])


def test_respond_twice_is_409_and_unknown_is_404(client):
    decisions = _run(client, 20)
    d = next(d for d in decisions if d["chosen"]["channel"] is not None)
    assert client.post("/demo/respond", json={"decision_id": d["decision_id"]}).status_code == 200
    assert client.post("/demo/respond", json={"decision_id": d["decision_id"]}).status_code == 409
    assert client.post("/demo/respond", json={"decision_id": "nope"}).status_code == 404


def test_respond_to_wait_records_nothing(client):
    decisions = _run(client)
    waits = [d for d in decisions if d["chosen"]["channel"] is None and not d["is_holdout"]]
    if not waits:
        pytest.skip("no wait decision in this sample")
    body = client.post("/demo/respond", json={"decision_id": waits[0]["decision_id"]}).json()
    assert body["nudged"] is False and body["outcomes"] == [] and body["reward"] is None


def test_holdout_is_never_messaged_but_is_resolved(client):
    decisions = _run(client, 200)
    held = [d for d in decisions if d["is_holdout"]]
    assert held, "expected some holdout decisions in 200 customers"
    for d in held:
        body = client.post("/demo/respond", json={"decision_id": d["decision_id"]}).json()
        assert body["nudged"] is False
        assert OutcomeType.MESSAGE_OPENED not in body["outcomes"]
        assert body["policy_updated"] is False


def test_full_loop_feeds_every_insight_endpoint(client):
    decisions = _run(client, 200)
    for d in decisions:
        client.post("/demo/respond", json={"decision_id": d["decision_id"]})
    funnel = client.get("/insights/messages").json()
    guard = client.get("/insights/guardrails").json()
    uplift = client.get("/insights/uplift").json()
    sent = sum(r["sent"] for r in funnel)
    assert sent > 0 and sent <= guard["treated"]["sent"]
    assert all(r["opened"] <= r["sent"] and r["orders"] <= r["sent"] for r in funnel)
    assert guard["treated"]["sent"] == uplift["treated"]["decisions"]
    assert guard["holdout"]["sent"] == uplift["holdout"]["decisions"]
