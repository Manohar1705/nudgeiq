"""Variant generation as a graph with a human approval pause (step 8e-3).

Needs the langgraph package; skipped when it is not installed.
"""

import json

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("langgraph")

from app.api.main import create_app  # noqa: E402
from app.campaigns import (  # noqa: E402
    CampaignNotFoundError,
    CampaignService,
    CampaignStateError,
    TemplateNotFoundError,
    VariantGenerator,
)
from app.graph.variant_builder import ROUTERS, clean_decisions, make_variant_nodes  # noqa: E402
from app.graph.variant_runs import (  # noqa: E402
    ReviewDecision,
    RunNotFoundError,
    RunStateError,
    VariantRunService,
)
from app.graph.variant_state import (  # noqa: E402
    START,
    VARIANT_EDGES,
    VARIANT_NODE_WRITES,
    VARIANT_NODES,
    variant_mermaid,
)
from app.llm import LLMError, LLMReplyError, MockLLMClient  # noqa: E402
from app.models import VariantSource, VariantStatus  # noqa: E402
from app.repository import InMemoryRepository  # noqa: E402

PUSH = "t_push_still_there"
GOOD = "Hey {{name}}, your {{topItem}} is still here: {{link}}"


class Scripted:
    """Returns canned replies and remembers what it was asked."""

    name = "scripted-model"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def items(*texts):
    return json.dumps([{"text": t, "principle": "urgency", "tone": "brisk"} for t in texts])


def make_draft(repo, config, library):
    svc = CampaignService(repo)
    svc.create_draft(config)
    for t in library.templates:
        svc.save_template(config.use_case_id, t)
    for v in library.variants:
        svc.save_variant(config.use_case_id, v)
    return svc


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


def decide(variant_id, action, note=None):
    return ReviewDecision(variant_id=variant_id, action=action, note=note)


def fingerprint(variants):
    return [
        (
            v.variant_id,
            v.template_id,
            v.text,
            v.principle,
            v.tone,
            v.status,
            v.source,
            v.generated_by,
        )
        for v in variants
    ]


# ---- the graph does what the plain generator does ------------------------------------------
MESSY = items(GOOD, GOOD, "Hi {{nope}} come back", "", "x" * 2000, "A fresh wording for {{name}}")


@pytest.mark.parametrize(
    "client_factory",
    [lambda: MockLLMClient(), lambda: Scripted(MESSY)],
    ids=["mock", "messy-reply"],
)
def test_the_graph_creates_the_same_variants_as_the_plain_generator(
    config, library, client_factory
):
    cid = config.use_case_id
    plain_svc = make_draft(InMemoryRepository(), config, library)
    graph_svc = make_draft(InMemoryRepository(), config, library)

    plain = VariantGenerator(plain_svc, client_factory()).generate(
        cid, PUSH, count=3, principles=["Urgency"], tones=["Brisk"]
    )
    view = VariantRunService(graph_svc, client_factory()).start(
        cid, PUSH, count=3, principles=["Urgency"], tones=["Brisk"]
    )

    assert fingerprint(view.variants) == fingerprint(plain.created)
    assert view.skipped == plain.skipped
    assert fingerprint(graph_svc.repo.list_variants(cid)) == fingerprint(
        plain_svc.repo.list_variants(cid)
    )


# ---- the pause ------------------------------------------------------------------------------
def test_a_run_stops_for_a_person_and_nothing_is_approved(svc, draft):
    view = VariantRunService(svc, MockLLMClient()).start(draft, PUSH, count=3)

    assert view.status == "awaiting_review"
    assert len(view.variants) == 3
    assert view.pending == [v.variant_id for v in view.variants]
    for v in svc.repo.list_variants(draft):
        if v.source == VariantSource.LLM:
            assert v.status == VariantStatus.PENDING
    assert [s.agent for s in view.steps] == ["campaign", "llm", "guardrail", "variants"]


def test_approve_and_reject_through_the_run(svc, draft):
    runs = VariantRunService(svc, MockLLMClient())
    view = runs.start(draft, PUSH, count=3)
    a, b, c = (v.variant_id for v in view.variants)

    out = runs.review(
        view.run_id, [decide(a, "approve", "good"), decide(b, "reject", "too pushy")]
    )

    assert out.status == "completed"
    by_id = {v.variant_id: v for v in out.variants}
    assert by_id[a].status == VariantStatus.APPROVED
    assert by_id[a].review_note == "good" and by_id[a].reviewed_at is not None
    assert by_id[b].status == VariantStatus.REJECTED and by_id[b].review_note == "too pushy"
    assert by_id[c].status == VariantStatus.PENDING and out.pending == [c]
    assert [(r.variant_id, r.ok) for r in out.reviewed] == [(a, True), (b, True)]
    assert svc.repo.get_variant(draft, a).status == VariantStatus.APPROVED  # really stored
    assert "1 of 3" in out.steps[-1].note


def test_an_empty_review_ends_the_run_and_leaves_everything_pending(svc, draft):
    runs = VariantRunService(svc, MockLLMClient())
    view = runs.start(draft, PUSH, count=2)
    out = runs.review(view.run_id, [])
    assert out.status == "completed" and len(out.pending) == 2 and out.reviewed == []


def test_a_bad_review_is_refused_without_using_up_the_pause(svc, draft):
    runs = VariantRunService(svc, MockLLMClient())
    view = runs.start(draft, PUSH, count=2)
    a = view.variants[0].variant_id

    with pytest.raises(ValueError, match="not part of this run"):
        runs.review(view.run_id, [decide("ghost", "approve")])
    with pytest.raises(ValueError, match="twice"):
        runs.review(view.run_id, [decide(a, "approve"), decide(a, "reject")])
    # an existing variant that this run did not create is refused too
    with pytest.raises(ValueError, match="not part of this run"):
        runs.review(view.run_id, [decide("t_push_still_there_v1", "approve")])
    assert runs.get(view.run_id).status == "awaiting_review"

    assert runs.review(view.run_id, [decide(a, "approve")]).status == "completed"


def test_a_finished_run_cannot_be_reviewed_again(svc, draft):
    runs = VariantRunService(svc, MockLLMClient())
    view = runs.start(draft, PUSH, count=1)
    a = view.variants[0].variant_id
    runs.review(view.run_id, [decide(a, "approve")])
    with pytest.raises(RunStateError):
        runs.review(view.run_id, [decide(a, "reject")])
    assert svc.repo.get_variant(draft, a).status == VariantStatus.APPROVED


def test_what_happened_while_the_run_waited_is_reported_not_overwritten(svc, draft):
    runs = VariantRunService(svc, MockLLMClient())
    view = runs.start(draft, PUSH, count=2)
    a, b = (v.variant_id for v in view.variants)
    svc.approve_variant(draft, a)  # someone used the ordinary endpoint meanwhile

    out = runs.review(view.run_id, [decide(a, "approve"), decide(b, "approve")])

    first, second = out.reviewed
    assert not first.ok and first.code == "VariantStateError"
    assert second.ok and second.status == "approved"
    assert out.status == "completed"


def test_if_the_campaign_stopped_being_a_draft_nothing_is_approved(svc, draft):
    runs = VariantRunService(svc, MockLLMClient())
    view = runs.start(draft, PUSH, count=1)
    a = view.variants[0].variant_id
    svc.archive(draft)

    out = runs.review(view.run_id, [decide(a, "approve")])

    assert out.status == "completed"
    assert not out.reviewed[0].ok and out.reviewed[0].code == "CampaignStateError"
    assert svc.repo.get_variant(draft, a).status == VariantStatus.PENDING


# ---- no pause when there is nothing to review ----------------------------------------------
def test_when_no_draft_passes_the_checks_the_run_finishes_and_stores_nothing(svc, draft):
    before = len(svc.repo.list_variants(draft))
    view = VariantRunService(svc, Scripted(items("Hi {{nope}}", ""))).start(draft, PUSH)

    assert view.status == "completed"
    assert view.variants == [] and view.pending == []
    assert len(view.skipped) == 2
    assert "nothing to review" in view.steps[-1].note
    assert len(svc.repo.list_variants(draft)) == before


# ---- failures behave like the plain endpoint ------------------------------------------------
def test_a_model_failure_stores_nothing_and_leaves_no_run(svc, draft):
    before = len(svc.repo.list_variants(draft))
    runs = VariantRunService(svc, Scripted(LLMError("model is down")))
    with pytest.raises(LLMError, match="down"):
        runs.start(draft, PUSH)
    runs = VariantRunService(svc, Scripted("sorry, I cannot do that"))
    with pytest.raises(LLMReplyError):
        runs.start(draft, PUSH)
    assert len(svc.repo.list_variants(draft)) == before
    assert runs._runs == {}


def test_the_model_is_not_called_when_the_request_cannot_succeed(svc, draft):
    client = Scripted(items(GOOD))
    runs = VariantRunService(svc, client)
    with pytest.raises(TemplateNotFoundError):
        runs.start(draft, "t_ghost")
    with pytest.raises(CampaignNotFoundError):
        runs.start("ghost", PUSH)
    with pytest.raises(ValueError, match="count"):
        runs.start(draft, PUSH, count=0)
    svc.activate(draft)
    with pytest.raises(CampaignStateError, match="only a draft"):
        runs.start(draft, PUSH)
    assert client.requests == []


def test_unknown_runs_and_runs_of_other_campaigns_are_not_found(svc, draft):
    runs = VariantRunService(svc, MockLLMClient())
    view = runs.start(draft, PUSH, count=1)
    with pytest.raises(RunNotFoundError):
        runs.get("nope")
    with pytest.raises(RunNotFoundError):
        runs.get(view.run_id, "some_other_campaign")
    with pytest.raises(RunNotFoundError):
        runs.review("nope", [])
    assert runs.get(view.run_id, draft).run_id == view.run_id


def test_old_runs_are_dropped_but_a_waiting_run_never_is(svc, draft):
    finished = VariantRunService(svc, Scripted(items("Hi {{nope}}")), max_runs=2)
    for _ in range(4):
        finished.start(draft, PUSH)
    assert len(finished._runs) <= 2

    waiting = VariantRunService(svc, MockLLMClient(), max_runs=1)
    ids = [waiting.start(draft, PUSH, count=1).run_id for _ in range(3)]
    assert all(waiting.get(i).status == "awaiting_review" for i in ids)


# ---- the nodes ------------------------------------------------------------------------------
def test_each_node_writes_only_its_own_keys_and_is_safe_to_run_twice(svc, draft):
    nodes = make_variant_nodes(svc, MockLLMClient())
    state = {
        "campaign_id": draft,
        "template_id": PUSH,
        "count": 2,
        "principles": [],
        "tones": [],
        "steps": [],
    }

    def run(name):
        nonlocal state
        update = nodes[name](state)
        assert set(update) <= VARIANT_NODE_WRITES[name], name
        state = {**state, **{k: v for k, v in update.items() if k != "steps"}}
        return update

    for name in ("prepare", "draft_variants", "check_drafts", "save_pending"):
        run(name)
    stored = len(svc.repo.list_variants(draft))
    run("save_pending")  # a replay after a crash
    assert len(svc.repo.list_variants(draft)) == stored

    a, b = state["created_ids"]
    state["decisions"] = [
        {"variant_id": "ghost", "action": "approve", "note": None},
        {"variant_id": a, "action": "approve", "note": None},
        {"variant_id": a, "action": "reject", "note": None},
    ]
    run("apply_review")
    first, second, third = state["reviewed"]
    assert first["error"] == "not part of this run"
    assert second["ok"] is True
    assert third["error"] == "decided twice in one request"
    assert svc.repo.get_variant(draft, a).status == VariantStatus.APPROVED
    assert svc.repo.get_variant(draft, b).status == VariantStatus.PENDING

    run("finish")


def test_clean_decisions_keeps_only_well_formed_ones():
    assert clean_decisions(None) == [] and clean_decisions({"decisions": "x"}) == []
    raw = {
        "decisions": [
            {"variant_id": "a", "action": "approve", "note": "ok"},
            {"variant_id": "b", "action": "delete"},
            {"variant_id": 7, "action": "reject"},
            "junk",
            {"variant_id": "c", "action": "reject", "note": 5},
        ]
    }
    assert clean_decisions(raw) == [
        {"variant_id": "a", "action": "approve", "note": "ok"},
        {"variant_id": "c", "action": "reject", "note": None},
    ]


def test_the_graph_shape_hangs_together():
    nodes = set(VARIANT_NODES)
    assert set(VARIANT_NODE_WRITES) == nodes
    for src, dst, _ in VARIANT_EDGES:
        assert src in nodes | {START} and dst in nodes
    reachable, frontier = {START}, [START]
    while frontier:
        here = frontier.pop()
        for src, dst, _ in VARIANT_EDGES:
            if src == here and dst not in reachable:
                reachable.add(dst)
                frontier.append(dst)
    assert reachable == nodes | {START}
    assert {n for n in nodes if not any(s == n for s, _, _ in VARIANT_EDGES)} == {"finish"}
    text = variant_mermaid()
    assert all(n in text for n in nodes)
    assert ROUTERS["check_drafts"]({"accepted": []}) == "finish"
    assert ROUTERS["check_drafts"]({"accepted": [{"variant_id": "x"}]}) == "save_pending"


# ---- over HTTP ------------------------------------------------------------------------------
@pytest.fixture
def api(repo):
    return TestClient(create_app(repo))


@pytest.fixture
def spring(api, config, library):
    data = config.model_dump(mode="json")
    data["use_case_id"] = "spring"
    assert api.post("/campaigns", json=data).status_code == 201
    for t in library.templates:
        api.put(f"/campaigns/spring/templates/{t.template_id}", json=t.model_dump(mode="json"))
    return "/campaigns/spring"


def test_http_start_read_review(api, spring):
    started = api.post(f"{spring}/variant-runs", json={"template_id": PUSH, "count": 2})
    assert started.status_code == 201
    body = started.json()
    assert body["status"] == "awaiting_review" and body["campaign_id"] == "spring"
    a, b = (v["variant_id"] for v in body["variants"])
    assert api.get(f"{spring}/variants?status=approved").json() == []

    got = api.get(f"{spring}/variant-runs/{body['run_id']}")
    assert got.status_code == 200 and got.json()["status"] == "awaiting_review"

    done = api.post(
        f"{spring}/variant-runs/{body['run_id']}/review",
        json={
            "decisions": [
                {"variant_id": a, "action": "approve"},
                {"variant_id": b, "action": "reject", "note": "too pushy"},
            ]
        },
    )
    assert done.status_code == 200
    out = done.json()
    assert out["status"] == "completed" and out["pending"] == []
    assert {v["variant_id"]: v["status"] for v in out["variants"]} == {
        a: "approved",
        b: "rejected",
    }
    approved = api.get(f"{spring}/variants?status=approved").json()
    assert [v["variant_id"] for v in approved] == [a]

    again = api.post(f"{spring}/variant-runs/{body['run_id']}/review", json={"decisions": []})
    assert again.status_code == 409


def test_http_errors(api, spring):
    assert api.post(f"{spring}/variant-runs", json={"template_id": "t_ghost"}).status_code == 404
    assert (
        api.post("/campaigns/ghost/variant-runs", json={"template_id": PUSH}).status_code == 404
    )
    live = api.post("/campaigns/target_cart/variant-runs", json={"template_id": PUSH})
    assert live.status_code == 409 and "only a draft" in live.json()["detail"]
    for bad in (
        {"template_id": PUSH, "count": 0},
        {"template_id": ""},
        {"template_id": PUSH, "x": 1},
    ):
        assert api.post(f"{spring}/variant-runs", json=bad).status_code == 422

    assert api.get(f"{spring}/variant-runs/nope").status_code == 404
    nope = api.post(f"{spring}/variant-runs/nope/review", json={"decisions": []})
    assert nope.status_code == 404

    run = api.post(f"{spring}/variant-runs", json={"template_id": PUSH, "count": 1}).json()
    path = f"{spring}/variant-runs/{run['run_id']}"
    ghost = api.post(
        f"{path}/review", json={"decisions": [{"variant_id": "ghost", "action": "approve"}]}
    )
    assert ghost.status_code == 422 and "not part of this run" in ghost.json()["detail"]
    bad_action = api.post(
        f"{path}/review", json={"decisions": [{"variant_id": "x", "action": "publish"}]}
    )
    assert bad_action.status_code == 422
    assert api.get(path).json()["status"] == "awaiting_review"  # still waiting
    # a run is only visible under its own campaign
    assert api.get(f"/campaigns/target_cart/variant-runs/{run['run_id']}").status_code == 404


def test_http_model_failure_is_a_502(repo, config, library):
    client = TestClient(create_app(repo, llm=Scripted(LLMError("rate limited"))))
    data = config.model_dump(mode="json")
    data["use_case_id"] = "spring"
    client.post("/campaigns", json=data)
    for t in library.templates:
        client.put(f"/campaigns/spring/templates/{t.template_id}", json=t.model_dump(mode="json"))
    response = client.post("/campaigns/spring/variant-runs", json={"template_id": PUSH})
    assert response.status_code == 502 and "rate limited" in response.json()["detail"]
