"""Per-node trace for the decision graph (step 8e-4, second half).

The recorder tests need no langgraph. The rest run the graph engine and are skipped without it.
The rule under test: the trace goes to the recorder and the log, never into the Decision, so
`Decision.steps` stays identical to the classic engine.
"""

import itertools
import logging

import pytest
from fastapi.testclient import TestClient

from app.graph.trace import DecisionTrace, TraceRecorder, traced
from app.models import DecisionStep

# ---- the recorder itself (no langgraph) -----------------------------------------------------


def add(rec, run_id, node="n"):
    return rec.add(run_id, node=node, attempt=1, status="ok", started_at="t", duration_ms=1.0)


def test_the_recorder_keeps_only_the_newest_runs_when_given_a_limit():
    rec = TraceRecorder(max_runs=2)
    for run in ("a", "b", "c"):
        add(rec, run)
    assert rec.get("a") == []  # the oldest was dropped
    assert len(rec.get("b")) == 1 and len(rec.get("c")) == 1


def test_a_run_that_is_still_adding_entries_is_never_the_one_dropped():
    rec = TraceRecorder(max_runs=1)
    add(rec, "a")
    add(rec, "b")
    add(rec, "b")
    assert rec.get("a") == [] and [e.seq for e in rec.get("b")] == [1, 2]


def test_without_a_limit_nothing_is_dropped():
    rec = TraceRecorder()
    for i in range(50):
        add(rec, f"r{i}")
    assert all(len(rec.get(f"r{i}")) == 1 for i in range(50))


def test_a_run_limit_below_one_is_refused():
    with pytest.raises(ValueError):
        TraceRecorder(max_runs=0)


def test_rename_moves_the_entries_and_keeps_their_order():
    rec = TraceRecorder()
    add(rec, "run", "first")
    add(rec, "run", "second")
    rec.rename("run", "d_1")
    assert rec.get("run") == []
    assert [(e.seq, e.node) for e in rec.get("d_1")] == [(1, "first"), (2, "second")]


def test_renaming_a_run_that_does_not_exist_changes_nothing():
    rec = TraceRecorder()
    add(rec, "keep")
    rec.rename("ghost", "keep")
    assert len(rec.get("keep")) == 1


def test_a_renamed_run_counts_as_new_for_the_limit():
    rec = TraceRecorder(max_runs=2)
    add(rec, "old")
    add(rec, "run")
    rec.rename("run", "d_1")
    add(rec, "next")
    assert rec.get("old") == [] and rec.get("d_1") != [] and rec.get("next") != []


def test_notes_are_read_from_decision_step_models_as_well_as_dicts():
    rec = TraceRecorder()
    node = traced(
        "n",
        lambda s: {"steps": [DecisionStep(agent="guardrail", note="entry rule passed")]},
        rec,
    )
    node({"run_id": "r1"})
    assert rec.get("r1")[0].notes == ["entry rule passed"]


# ---- the decision graph ---------------------------------------------------------------------
pytest.importorskip("langgraph")

from app.agents.state_agent import CustomerNotFoundError  # noqa: E402
from app.api.main import create_app  # noqa: E402
from app.graph.state import NODES  # noqa: E402
from app.models import Journey, JourneyStatus  # noqa: E402
from app.orchestrator import LogDispatcher, Orchestrator  # noqa: E402
from app.repository import InMemoryRepository  # noqa: E402

ALL_SEND_NODES = [
    "load_state",
    "entry_guardrail",
    "propose_actions",
    "guardrail_filter",
    "master_select",
    "compose_message",
    "dispatch",
    "finalize",
]


def build(config, library, engine="graph", trace=None, seed=11, prefix="d"):
    repo = InMemoryRepository()
    counter = itertools.count()
    orch = Orchestrator(
        repo,
        config,
        library,
        seed=seed,
        dispatcher=LogDispatcher(),
        id_factory=lambda: f"{prefix}{next(counter):05d}",
        campaign_version=1,
        engine=engine,
        trace=trace,
    )
    return repo, orch


def nodes_of(orch, decision):
    return [e.node for e in orch.trace.get(decision.decision_id)]


def a_sending_seed(config, library, now, make_state):
    """A bandit seed whose first decision for the standard customer is a send.

    Not every seed sends (the bandit explores and sometimes waits), so look for one. The same
    seed and customer give the same choice every time, which the failure test relies on.
    """
    for seed in range(60):
        repo, orch = build(config, library, seed=seed)
        repo.save_customer(make_state())
        if not orch.decide("c1", now).chosen.is_wait:
            return seed
    raise AssertionError("no seed produced a send; the scenario proves nothing")


def a_sending_customer(config, library, now, make_state):
    seed = a_sending_seed(config, library, now, make_state)
    repo, orch = build(config, library, seed=seed)
    repo.save_customer(make_state())
    return repo, orch, orch.decide("c1", now)


def test_a_send_leaves_one_ok_entry_per_node_in_graph_order(config, library, now, make_state):
    _, orch, decision = a_sending_customer(config, library, now, make_state)
    entries = orch.trace.get(decision.decision_id)
    assert [e.node for e in entries] == ALL_SEND_NODES
    assert {e.status for e in entries} == {"ok"} and {e.attempt for e in entries} == {1}
    assert [e.seq for e in entries] == list(range(1, 9))
    assert set(ALL_SEND_NODES) <= set(NODES)


def test_the_trace_notes_are_exactly_the_decisions_steps_in_order(config, library, now, make_state):
    _, orch, decision = a_sending_customer(config, library, now, make_state)
    noted = [n for e in orch.trace.get(decision.decision_id) for n in e.notes]
    assert noted == [s.note for s in decision.steps]


def test_each_entry_says_which_state_keys_the_node_wrote(config, library, now, make_state):
    _, orch, decision = a_sending_customer(config, library, now, make_state)
    by_node = {e.node: e for e in orch.trace.get(decision.decision_id)}
    assert by_node["dispatch"].wrote == ["nudge_sent", "steps"]
    assert by_node["finalize"].wrote == ["decision"]
    assert "decision_id" in by_node["load_state"].wrote


def test_the_trace_is_not_part_of_the_decision(config, library, now, make_state):
    seed = a_sending_seed(config, library, now, make_state)
    dumps = {}
    for engine in ("classic", "graph"):
        repo, orch = build(config, library, engine=engine, seed=seed)
        repo.save_customer(make_state())
        dumps[engine] = orch.decide("c1", now).model_dump(mode="json")
    assert dumps["graph"] == dumps["classic"]  # same fields, same steps, nothing extra


def test_a_closed_journey_goes_load_state_then_finalize(config, library, now, make_state):
    repo, orch = build(config, library)
    repo.save_customer(make_state(customer_id="gone"))
    repo.save_journey(
        Journey(
            customer_id="gone",
            started_at=now,
            base_abandoned_minutes=35,
            status=JourneyStatus.EXITED,
            closed_reason="test",
            closed_at=now,
        )
    )
    decision = orch.decide("gone", now)
    assert nodes_of(orch, decision) == ["load_state", "finalize"]
    assert orch.trace.get(decision.decision_id)[0].notes == ["journey is exited: no nudge"]


def test_a_customer_suppressed_at_entry_goes_through_no_candidates(
    config, library, now, make_state
):
    repo, orch = build(config, library)
    repo.save_customer(make_state(customer_id="optout", contactable=False))
    decision = orch.decide("optout", now)
    assert nodes_of(orch, decision) == [
        "load_state",
        "entry_guardrail",
        "no_candidates",
        "finalize",
    ]


def test_every_decision_gets_its_own_trace(config, library, now, make_state):
    repo, orch = build(config, library)
    for cid in ("a", "b"):
        repo.save_customer(make_state(customer_id=cid))
    first, second = orch.decide("a", now), orch.decide("b", now)
    assert first.decision_id != second.decision_id
    assert nodes_of(orch, first)[0] == "load_state" and nodes_of(orch, second)[0] == "load_state"
    assert [e.seq for e in orch.trace.get(first.decision_id)][0] == 1
    assert [e.seq for e in orch.trace.get(second.decision_id)][0] == 1


def test_no_trace_is_left_under_the_temporary_run_id(config, library, now, make_state):
    repo, orch = build(config, library)
    repo.save_customer(make_state(customer_id="a"))
    orch.decide("a", now)
    assert len(orch.trace._runs) == 1
    assert next(iter(orch.trace._runs)).startswith("d")  # the decision id, not "r_..."


def test_the_classic_engine_records_nothing(config, library, now, make_state):
    repo, orch = build(config, library, engine="classic")
    repo.save_customer(make_state(customer_id="a"))
    decision = orch.decide("a", now)
    assert orch.trace.get(decision.decision_id) == [] and orch.trace._runs == {}


def test_the_log_ties_the_run_to_the_decision(config, library, now, make_state, caplog):
    repo, orch = build(config, library)
    repo.save_customer(make_state(customer_id="a"))
    with caplog.at_level(logging.INFO, logger="nudgeiq.graph"):
        decision = orch.decide("a", now)
    assert "node=load_state run=r_" in caplog.text
    assert f"is decision {decision.decision_id}" in caplog.text


# ---- failures -------------------------------------------------------------------------------
def test_an_unknown_customer_still_raises_the_same_error_and_leaves_no_trace(
    config, library, now, caplog
):
    _, orch = build(config, library)
    with caplog.at_level(logging.WARNING, logger="nudgeiq.graph"):
        with pytest.raises(CustomerNotFoundError):
            orch.decide("ghost", now)
    assert orch.trace._runs == {}
    assert "node=load_state" in caplog.text and "status=error" in caplog.text
    assert "CustomerNotFoundError" in caplog.text


def test_a_failing_send_is_raised_unchanged_and_not_retried(
    config, library, now, make_state, caplog
):
    seed = a_sending_seed(config, library, now, make_state)  # same seed: this one does send
    repo, orch = build(config, library, seed=seed)
    repo.save_customer(make_state())
    calls = []

    def broken(*_a, **_k):
        calls.append(1)
        raise RuntimeError("smtp is down")

    orch.dispatcher.send = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="nudgeiq.graph"):
        with pytest.raises(RuntimeError, match="smtp is down"):
            orch.decide("c1", now)
    assert calls == [1]  # one attempt: a send is never replayed
    assert orch.trace._runs == {}  # a failed run leaves nothing behind; the log has it
    assert "node=dispatch" in caplog.text and "status=error" in caplog.text
    assert "RuntimeError: smtp is down" in caplog.text
    assert repo.list_decisions("c1", limit=None) == []  # finalize never ran: nothing was saved


def test_a_failed_decision_does_not_disturb_the_next_one(config, library, now, make_state):
    repo, orch = build(config, library)
    repo.save_customer(make_state(customer_id="a"))
    with pytest.raises(CustomerNotFoundError):
        orch.decide("ghost", now)
    decision = orch.decide("a", now)
    assert nodes_of(orch, decision)[0] == "load_state"


# ---- memory is bounded, and a recorder can be shared ----------------------------------------
def test_only_the_newest_decisions_keep_their_trace(config, library, now, make_state):
    repo, orch = build(config, library, trace=TraceRecorder(max_runs=2))
    made = []
    for cid in ("a", "b", "c"):
        repo.save_customer(make_state(customer_id=cid))
        made.append(orch.decide(cid, now))
    assert orch.trace.get(made[0].decision_id) == []
    assert orch.trace.get(made[1].decision_id) != [] and orch.trace.get(made[2].decision_id) != []


def test_two_orchestrators_can_share_one_recorder(config, library, now, make_state):
    shared = TraceRecorder()
    repo_a, a = build(config, library, trace=shared, prefix="a")
    repo_b, b = build(config, library, trace=shared, prefix="b")
    repo_a.save_customer(make_state(customer_id="x"))
    repo_b.save_customer(make_state(customer_id="y"))
    da, db = a.decide("x", now), b.decide("y", now)
    assert da.decision_id.startswith("a") and db.decision_id.startswith("b")
    # either orchestrator (or the API, through the runtime) can read both traces
    assert shared.get(da.decision_id) != [] and shared.get(db.decision_id) != []
    assert a.trace is b.trace is shared


# ---- over HTTP ------------------------------------------------------------------------------
CUSTOMER = {
    "customer_id": "api_trace",
    "cohort_id": "cart_high_aov",
    "cart_value": 120.0,
    "abandoned_minutes": 35,
    "preferred_channel": "push",
    "signals": {"loyalty_tier": "circle", "card_holder": True, "fulfillment": "pickup"},
}
AS_OF = "2026-11-02T10:00:00Z"


def decide_over_http(repo, engine, monkeypatch):
    monkeypatch.setenv("NUDGEIQ_ENGINE", engine)
    api = TestClient(create_app(repo))
    assert api.post("/customers", json=CUSTOMER).status_code == 200
    decision = api.post("/decide", json={"customer_id": "api_trace", "as_of": AS_OF}).json()
    return api, decision


def test_http_the_trace_of_a_graph_decision_is_readable(repo, monkeypatch):
    api, decision = decide_over_http(repo, "graph", monkeypatch)
    body = api.get(f"/decisions/{decision['decision_id']}/trace").json()
    DecisionTrace.model_validate(body)
    assert body["engine"] == "graph" and body["decision_id"] == decision["decision_id"]
    assert [e["node"] for e in body["entries"]][0] == "load_state"
    assert body["entries"][-1]["node"] == "finalize"
    assert all(e["status"] == "ok" for e in body["entries"])
    # the notes are the ones the Live page shows
    noted = [n for e in body["entries"] for n in e["notes"]]
    assert noted == [s["note"] for s in decision["steps"]]
    assert "trace" not in decision


def test_http_the_classic_engine_has_an_empty_trace(repo, monkeypatch):
    api, decision = decide_over_http(repo, "classic", monkeypatch)
    body = api.get(f"/decisions/{decision['decision_id']}/trace").json()
    assert body == {"decision_id": decision["decision_id"], "engine": "classic", "entries": []}


def test_http_a_trace_for_an_unknown_decision_is_a_404(repo, monkeypatch):
    api, _ = decide_over_http(repo, "graph", monkeypatch)
    assert api.get("/decisions/nope/trace").status_code == 404


def test_http_the_decision_detail_is_unchanged(repo, monkeypatch):
    api, decision = decide_over_http(repo, "graph", monkeypatch)
    detail = api.get(f"/decisions/{decision['decision_id']}").json()
    assert set(detail) == {"decision", "outcomes", "reward"}
