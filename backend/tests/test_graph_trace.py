"""Per-node trace, retries and clean failures for the graphs (step 8e-4).

The first half tests `traced()` on its own (no langgraph). The second half runs the variant graph
and needs langgraph; those tests are skipped when it is not installed.
"""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.graph.trace import MAX_ERROR_CHARS, TraceRecorder, traced
from app.graph.variant_state import VARIANT_NODES
from app.llm import LLMError, LLMReplyError


# ---- traced(): no langgraph needed ----------------------------------------------------------
class Boom(Exception):
    pass


class Pause(Exception):
    """Stands in for LangGraph's pause signal."""


def wrap(fn, rec, **kw):
    return traced("node_a", fn, rec, sleep=lambda _s: None, **kw)


def test_a_successful_node_leaves_one_ok_entry():
    rec = TraceRecorder()
    node = wrap(lambda s: {"x": 1, "steps": [{"agent": "a", "note": "did it"}]}, rec)
    assert node({"run_id": "r1"}) == {"x": 1, "steps": [{"agent": "a", "note": "did it"}]}
    [entry] = rec.get("r1")
    assert (entry.seq, entry.node, entry.attempt, entry.status) == (1, "node_a", 1, "ok")
    assert entry.wrote == ["steps", "x"] and entry.notes == ["did it"]
    assert entry.error is None and entry.duration_ms >= 0
    assert entry.started_at.endswith("+00:00")
    assert rec.last_error("r1") is None


def test_entries_from_different_runs_do_not_mix_and_seq_counts_up():
    rec = TraceRecorder()
    node = wrap(lambda s: {}, rec)
    node({"run_id": "r1"})
    node({"run_id": "r1"})
    node({"run_id": "r2"})
    assert [e.seq for e in rec.get("r1")] == [1, 2]
    assert [e.seq for e in rec.get("r2")] == [1]
    rec.forget("r1")
    assert rec.get("r1") == []


def test_a_failing_node_is_recorded_and_raised_unchanged():
    rec = TraceRecorder()

    def bad(_s):
        raise Boom("it broke")

    with pytest.raises(Boom, match="it broke"):
        wrap(bad, rec)({"run_id": "r1"})
    [entry] = rec.get("r1")
    assert entry.status == "error" and entry.error.type == "Boom"
    assert entry.error.message == "it broke"
    assert rec.last_error("r1") == entry


def test_a_long_error_message_is_cut():
    rec = TraceRecorder()

    def bad(_s):
        raise Boom("x" * 5000)

    with pytest.raises(Boom):
        wrap(bad, rec)({"run_id": "r1"})
    assert len(rec.get("r1")[0].error.message) == MAX_ERROR_CHARS


def test_a_retryable_error_is_tried_again_and_the_attempts_are_traced():
    rec = TraceRecorder()
    calls = []

    def flaky(_s):
        calls.append(1)
        if len(calls) < 3:
            raise Boom(f"try {len(calls)}")
        return {"ok": True}

    out = wrap(flaky, rec, attempts=3, retry_on=(Boom,))({"run_id": "r1"})
    assert out == {"ok": True}
    got = [(e.attempt, e.status) for e in rec.get("r1")]
    assert got == [(1, "retry"), (2, "retry"), (3, "ok")]
    assert rec.last_error("r1") is None  # it recovered, so the run is not failed


def test_retries_stop_at_the_cap():
    rec = TraceRecorder()
    calls = []

    def always(_s):
        calls.append(1)
        raise Boom("still down")

    with pytest.raises(Boom):
        wrap(always, rec, attempts=3, retry_on=(Boom,))({"run_id": "r1"})
    assert len(calls) == 3
    assert [(e.attempt, e.status) for e in rec.get("r1")] == [
        (1, "retry"),
        (2, "retry"),
        (3, "error"),
    ]


def test_an_error_that_is_not_retryable_is_raised_at_once():
    rec = TraceRecorder()
    calls = []

    def bad(_s):
        calls.append(1)
        raise KeyError("bug")

    with pytest.raises(KeyError):
        wrap(bad, rec, attempts=5, retry_on=(Boom,))({"run_id": "r1"})
    assert len(calls) == 1 and rec.get("r1")[0].status == "error"


def test_the_wait_between_attempts_grows():
    rec = TraceRecorder()
    waits = []

    def always(_s):
        raise Boom("x")

    node = traced("n", always, rec, attempts=3, retry_on=(Boom,), delay=0.5, sleep=waits.append)
    with pytest.raises(Boom):
        node({"run_id": "r1"})
    assert waits == [0.5, 1.0]


def test_a_pause_is_recorded_as_paused_not_as_an_error_and_is_not_retried():
    rec = TraceRecorder()
    calls = []

    def pauses(_s):
        calls.append(1)
        raise Pause()

    node = wrap(pauses, rec, attempts=3, retry_on=(Exception,), passthrough=(Pause,))
    with pytest.raises(Pause):
        node({"run_id": "r1"})
    assert len(calls) == 1
    assert [e.status for e in rec.get("r1")] == ["paused"]
    assert rec.last_error("r1") is None


def test_one_run_cannot_grow_the_trace_without_limit():
    rec = TraceRecorder(max_entries=5)
    node = wrap(lambda s: {}, rec)
    for _ in range(20):
        node({"run_id": "r1"})
    assert len(rec.get("r1")) == 5


def test_attempts_below_one_is_refused():
    with pytest.raises(ValueError):
        traced("n", lambda s: {}, TraceRecorder(), attempts=0)


def test_each_attempt_is_logged(caplog):
    rec = TraceRecorder()
    with caplog.at_level(logging.INFO, logger="nudgeiq.graph"):
        wrap(lambda s: {}, rec)({"run_id": "r9"})
    assert "node=node_a run=r9 attempt=1 status=ok" in caplog.text


# ---- the variant graph ----------------------------------------------------------------------
pytest.importorskip("langgraph")

from app.api.main import create_app  # noqa: E402
from app.campaigns import CampaignService  # noqa: E402
from app.graph.variant_runs import (  # noqa: E402
    ReviewDecision,
    RunFailedError,
    RunStateError,
    VariantRunService,
)

PUSH = "t_push_still_there"
GOOD = "Hey {{name}}, your {{topItem}} is still here: {{link}}"


class Scripted:
    """Returns canned replies in order (the last one repeats); an Exception is raised."""

    name = "scripted-model"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def items(*texts):
    return json.dumps([{"text": t, "principle": "urgency", "tone": "brisk"} for t in texts])


@pytest.fixture
def svc(repo):
    return CampaignService(repo)


@pytest.fixture
def draft(svc, config, library):
    svc.create_draft(config)
    for t in library.templates:
        svc.save_template(config.use_case_id, t)
    for v in library.variants:
        svc.save_variant(config.use_case_id, v)
    return config.use_case_id


def runs_with(svc, *replies, attempts=2):
    return VariantRunService(
        svc,
        Scripted(*(replies or (items(GOOD, "A fresh wording for {{name}}"),))),
        llm_attempts=attempts,
        retry_delay=0.0,
    )


def shape(view):
    return [(e.node, e.attempt, e.status) for e in view.trace]


def test_a_run_has_one_entry_per_node_and_ends_paused_at_the_review(svc, draft):
    view = runs_with(svc).start(draft, PUSH, count=2)
    assert shape(view) == [
        ("prepare", 1, "ok"),
        ("draft_variants", 1, "ok"),
        ("check_drafts", 1, "ok"),
        ("save_pending", 1, "ok"),
        ("await_review", 1, "paused"),
    ]
    assert view.error is None and view.status == "awaiting_review"
    assert [e.seq for e in view.trace] == [1, 2, 3, 4, 5]


def test_the_trace_carries_each_nodes_note_and_keys(svc, draft):
    view = runs_with(svc).start(draft, PUSH, count=2)
    by_node = {e.node: e for e in view.trace}
    assert "kept" in by_node["check_drafts"].notes[0]
    assert "created_ids" in by_node["save_pending"].wrote
    assert by_node["draft_variants"].wrote == ["drafts", "steps"]


def test_after_the_review_the_trace_shows_the_replay_and_the_rest(svc, draft):
    runs = runs_with(svc)
    view = runs.start(draft, PUSH, count=2)
    ids = [v.variant_id for v in view.variants]
    done = runs.review(view.run_id, [ReviewDecision(variant_id=ids[0], action="approve")], draft)
    assert done.status == "completed"
    # await_review starts again from the top when resumed: paused, then ok
    assert [(e.node, e.status) for e in done.trace][4:] == [
        ("await_review", "paused"),
        ("await_review", "ok"),
        ("apply_review", "ok"),
        ("finish", "ok"),
    ]
    assert runs.get(view.run_id, draft).trace == done.trace  # reading again changes nothing


def test_every_graph_node_appears_in_a_full_run(svc, draft):
    runs = runs_with(svc)
    view = runs.start(draft, PUSH, count=2)
    done = runs.review(view.run_id, [], draft)
    assert {e.node for e in done.trace} == set(VARIANT_NODES)


def test_a_run_where_nothing_passes_has_no_review_entries(svc, draft):
    view = runs_with(svc, items("Hi {{nope}}")).start(draft, PUSH, count=2)
    assert [e.node for e in view.trace] == ["prepare", "draft_variants", "check_drafts", "finish"]
    assert view.status == "completed"


# ---- retries on the model call --------------------------------------------------------------
def test_a_model_error_that_goes_away_is_retried_and_the_run_still_works(svc, draft):
    client = Scripted(LLMError("rate limited"), items(GOOD))
    runs = VariantRunService(svc, client, llm_attempts=2, retry_delay=0.0)
    view = runs.start(draft, PUSH, count=1)
    assert client.calls == 2
    assert view.status == "awaiting_review" and view.error is None
    assert [(e.node, e.attempt, e.status) for e in view.trace][1:3] == [
        ("draft_variants", 1, "retry"),
        ("draft_variants", 2, "ok"),
    ]
    assert view.trace[1].error.type == "LLMError"


def test_a_model_that_keeps_failing_is_tried_the_set_number_of_times_then_raised(svc, draft):
    client = Scripted(LLMError("model is down"))
    runs = VariantRunService(svc, client, llm_attempts=3, retry_delay=0.0)
    with pytest.raises(LLMError, match="down"):
        runs.start(draft, PUSH)
    assert client.calls == 3
    assert runs._runs == {} and runs.trace.get("anything") == []


def test_a_bad_reply_counts_as_a_model_error_and_is_tried_again(svc, draft):
    client = Scripted("sorry, I cannot do that", items(GOOD))
    runs = VariantRunService(svc, client, llm_attempts=2, retry_delay=0.0)
    assert runs.start(draft, PUSH, count=1).status == "awaiting_review"
    assert client.calls == 2
    with pytest.raises(LLMReplyError):
        VariantRunService(svc, Scripted("nope"), llm_attempts=1).start(draft, PUSH)


def test_only_the_model_node_is_retried(svc, draft):
    client = Scripted(items(GOOD))
    runs = VariantRunService(svc, client, llm_attempts=3, retry_delay=0.0)
    with pytest.raises(LookupError):
        runs.start(draft, "t_ghost")  # prepare fails: once, no retries, no model call
    assert client.calls == 0


# ---- failures that are not the request's fault ----------------------------------------------
def test_an_unexpected_error_becomes_a_plain_failure_with_the_traceback_in_the_log(
    svc, draft, caplog
):
    runs = runs_with(svc, RuntimeError("secret internal detail: /srv/app/x.py"))
    with caplog.at_level(logging.WARNING, logger="nudgeiq.graph"):
        with pytest.raises(RunFailedError) as caught:
            runs.start(draft, PUSH)
    exc = caught.value
    assert exc.node == "draft_variants" and exc.error.type == "RuntimeError"
    assert "draft_variants" in str(exc) and "RuntimeError" in str(exc)
    assert "/srv/app" not in str(exc)  # the message does not leak the inner text
    assert "Traceback" in caplog.text and "variant run" in caplog.text
    assert runs._runs == {}  # nothing is left behind


def test_a_failure_while_applying_the_review_ends_the_run_as_error(svc, draft, monkeypatch):
    runs = runs_with(svc)
    view = runs.start(draft, PUSH, count=2)
    ids = [v.variant_id for v in view.variants]

    def broken(*_a, **_k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(svc, "approve_variant", broken)
    with pytest.raises(RunFailedError, match="apply_review"):
        runs.review(view.run_id, [ReviewDecision(variant_id=ids[0], action="approve")], draft)

    after = runs.get(view.run_id, draft)
    assert after.status == "error"
    assert after.error is not None and after.error.node == "apply_review"
    assert after.error.error.message == "database went away"
    assert sorted(after.pending) == sorted(ids)  # nothing was approved, all still pending
    with pytest.raises(RunStateError):  # a failed run is not waiting for a review
        runs.review(view.run_id, [], draft)


def test_old_runs_take_their_trace_with_them(svc, draft):
    runs = VariantRunService(
        svc, Scripted(items(GOOD)), max_runs=1, llm_attempts=1, retry_delay=0.0
    )
    first = runs.start(draft, PUSH, count=1)
    runs.review(first.run_id, [], draft)  # finished, so it may be dropped
    second = runs.start(draft, PUSH, count=1)
    assert runs.trace.get(first.run_id) == []
    assert runs.trace.get(second.run_id) != []


# ---- over HTTP ------------------------------------------------------------------------------
@pytest.fixture
def spring(repo, config, library):
    def build(llm):
        api = TestClient(create_app(repo, llm=llm))
        data = config.model_dump(mode="json")
        data["use_case_id"] = "spring"
        assert api.post("/campaigns", json=data).status_code == 201
        for t in library.templates:
            api.put(f"/campaigns/spring/templates/{t.template_id}", json=t.model_dump(mode="json"))
        return api, "/campaigns/spring"

    return build


def test_http_the_run_view_includes_the_trace(spring):
    api, base = spring(Scripted(items(GOOD)))
    body = api.post(f"{base}/variant-runs", json={"template_id": PUSH, "count": 1}).json()
    assert [e["node"] for e in body["trace"]][:2] == ["prepare", "draft_variants"]
    assert body["error"] is None
    again = api.get(f"{base}/variant-runs/{body['run_id']}").json()
    assert again["trace"] == body["trace"]


def test_http_an_unexpected_failure_is_a_500_with_a_plain_message(spring):
    api, base = spring(Scripted(RuntimeError("secret internal detail")))
    response = api.post(f"{base}/variant-runs", json={"template_id": PUSH})
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "draft_variants" in detail and "RuntimeError" in detail
    assert "secret internal detail" not in response.text and "Traceback" not in response.text


def test_http_a_review_of_a_finished_run_is_a_409(spring):
    api, base = spring(Scripted(items(GOOD)))
    run = api.post(f"{base}/variant-runs", json={"template_id": PUSH, "count": 1}).json()
    url = f"{base}/variant-runs/{run['run_id']}/review"
    assert api.post(url, json={"decisions": []}).status_code == 200
    assert api.post(url, json={"decisions": []}).status_code == 409
