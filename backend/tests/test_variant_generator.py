"""5B-8b: drafting variants with a language model (the mock, and scripted replies)."""

import json

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.campaigns import (
    MAX_COUNT,
    CampaignNotFoundError,
    CampaignService,
    CampaignStateError,
    TemplateNotFoundError,
    VariantGenerator,
)
from app.campaigns.generator import build_prompt, clean_label, parse_reply
from app.llm import DraftBrief, LLMError, LLMReplyError, LLMRequest, MockLLMClient
from app.models import Channel, MessageVariant, VariantSource, VariantStatus

PUSH = "t_push_still_there"
SMS = "t_sms_low_stock"
EMAIL = "t_email_discount"
GOOD = "Hey {{name}}, your {{topItem}} is still here: {{link}}"


class Scripted:
    """Returns canned replies and remembers what it was asked."""

    name = "scripted-model"

    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> str:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def items(*texts, **extra) -> str:
    return json.dumps(
        [{"text": t, "principle": "urgency", "tone": "brisk", **extra} for t in texts]
    )


@pytest.fixture
def svc(repo):
    return CampaignService(repo)


@pytest.fixture
def draft(svc, config, library):
    """A draft with the sample templates and variants (so there are wordings to avoid)."""
    svc.create_draft(config)
    for t in library.templates:
        svc.save_template(config.use_case_id, t)
    for v in library.variants:
        svc.save_variant(config.use_case_id, v)
    return config.use_case_id


def gen(svc, client):
    return VariantGenerator(svc, client)


# ---- the mock -----------------------------------------------------------------------------
@pytest.mark.parametrize("tid", [PUSH, SMS, EMAIL, "t_email_bonus"])
def test_mock_drafts_pass_the_checks_for_every_template(svc, draft, tid):
    result = gen(svc, MockLLMClient()).generate(draft, tid, count=3)
    assert len(result.created) == 3 and result.skipped == []
    for v in result.created:
        assert v.status == VariantStatus.PENDING and v.source == VariantSource.LLM
        assert v.generated_by == "mock" and v.template_id == tid
        assert svc.repo.get_variant(draft, v.variant_id) == v
    assert len({v.text for v in result.created}) == 3


def test_mock_is_deterministic_and_varies_principles(svc, draft):
    a = gen(svc, MockLLMClient()).generate(draft, PUSH, count=3)
    texts = [v.text for v in a.created]
    assert len({v.principle for v in a.created}) == 3
    # a second ask gets different wordings, and the ids carry on
    b = gen(svc, MockLLMClient()).generate(draft, PUSH, count=3)
    assert not set(texts) & {v.text for v in b.created}
    ids = [v.variant_id for v in a.created + b.created]
    assert ids == [f"{PUSH}_llm_{n}" for n in range(1, 7)]


def test_mock_follows_requested_principles_and_tones(svc, draft):
    result = gen(svc, MockLLMClient()).generate(
        draft, PUSH, count=2, principles=["Social Proof", "authority"], tones=["Playful"]
    )
    assert {v.principle for v in result.created} == {"social_proof", "authority"}
    assert {v.tone for v in result.created} == {"playful"}


def test_mock_runs_out_of_new_wordings(svc, draft):
    client = MockLLMClient()
    total = 0
    for _ in range(6):
        total += len(gen(svc, client).generate(draft, PUSH, count=5).created)
    assert total == 15  # 5 principles x 3 closing lines, then nothing new
    assert gen(svc, client).generate(draft, PUSH, count=5).created == []


# ---- parsing ------------------------------------------------------------------------------
def test_parse_plain_fenced_and_chatty_replies():
    raw = items("a", "b")
    assert len(parse_reply(raw)) == 2
    assert len(parse_reply(f"```json\n{raw}\n```")) == 2
    assert len(parse_reply(f"Sure! Here you go:\n{raw}\nHope that helps.")) == 2
    assert len(parse_reply(json.dumps({"variants": json.loads(raw)}))) == 2


@pytest.mark.parametrize("raw", ["", "no json here", '{"text": "a"}', "[1, 2", "null"])
def test_parse_rejects_replies_without_a_list(raw):
    with pytest.raises(LLMReplyError):
        parse_reply(raw)


def test_clean_label():
    assert clean_label("  Social Proof! ") == "social_proof"
    assert clean_label("x" * 100) == "x" * 40
    assert clean_label("???") == ""


# ---- cleaning and checking what the model wrote ------------------------------------------
def test_good_drafts_are_stored_pending_whatever_the_model_claims(svc, draft):
    client = Scripted(items(GOOD, status="approved", source="human", variant_id="v_push_warm"))
    result = gen(svc, client).generate(draft, PUSH, count=1)
    v = result.created[0]
    assert v.status == VariantStatus.PENDING and v.source == VariantSource.LLM
    assert v.variant_id == f"{PUSH}_llm_1" and v.generated_by == "scripted-model"
    assert v.principle == "urgency" and v.tone == "brisk"
    assert result.model == "scripted-model" and result.requested == 1


def test_bad_drafts_are_skipped_with_a_reason_and_good_ones_still_saved(svc, draft):
    reply = json.dumps(
        [
            {"text": "Hi {{nickname}}, come back {{link}}", "principle": "x", "tone": "y"},
            {"text": "   ", "principle": "x", "tone": "y"},
            {"text": "y" * 1001, "principle": "x", "tone": "y"},
            "just a string",
            {"principle": "x"},
            {"text": GOOD, "principle": "x", "tone": "y"},
        ]
    )
    result = gen(svc, Scripted(reply)).generate(draft, PUSH, count=5)
    assert [v.text for v in result.created] == [GOOD]
    assert [s.code for s in result.skipped] == [
        "unknown_variable",
        "empty_body",
        "too_long",
        "invalid_item",
        "invalid_item",
    ]
    assert all(len(s.text) <= 200 for s in result.skipped)
    assert len(svc.repo.list_variants(draft, template_id=PUSH)) == 4 + 1  # 4 sample + 1 new


def test_sms_without_stop_is_skipped(svc, draft):
    reply = items(
        "Only {{units}} left of {{topItem}}: {{link}}", "Last {{units}} left. Reply STOP."
    )
    result = gen(svc, Scripted(reply)).generate(draft, SMS, count=3)
    assert [v.text for v in result.created] == ["Last {{units}} left. Reply STOP."]
    assert [s.code for s in result.skipped] == ["sms_missing_stop"]


def test_warnings_do_not_stop_a_draft_being_stored(svc, draft):
    long_sms = "Only {{units}} left of {{topItem}} at {{store}}. " + "x" * 200 + " Reply STOP."
    result = gen(svc, Scripted(items(long_sms))).generate(draft, SMS, count=1)
    assert len(result.created) == 1 and result.skipped == []


def test_duplicates_are_skipped_including_rejected_and_in_reply_ones(svc, draft, library):
    rejected = next(v for v in library.variants if v.status == VariantStatus.REJECTED)
    approved = next(
        v
        for v in library.variants
        if v.template_id == rejected.template_id and v.status == VariantStatus.APPROVED
    )
    tid = rejected.template_id
    reply = items(
        rejected.text,  # a wording a person already rejected
        "  " + approved.text.upper() + "  ",  # same wording, different case and spacing
        GOOD,
        GOOD.replace("Hey", "hey"),  # repeats the one just before it
    )
    result = gen(svc, Scripted(reply)).generate(draft, tid, count=5)
    assert [s.code for s in result.skipped] == ["duplicate"] * 3
    assert len(result.created) == 1


def test_only_count_drafts_are_kept(svc, draft):
    texts = [f"Hi {{{{name}}}}, option {i}: {{{{link}}}}" for i in range(5)]
    result = gen(svc, Scripted(items(*texts))).generate(draft, PUSH, count=2)
    assert len(result.created) == 2
    assert [s.code for s in result.skipped] == ["over_limit"] * 3


def test_missing_labels_become_unspecified(svc, draft):
    reply = json.dumps([{"text": GOOD}])
    v = gen(svc, Scripted(reply)).generate(draft, PUSH, count=1).created[0]
    assert v.principle == "unspecified" and v.tone == "unspecified"


def test_generated_variants_must_still_be_approved_by_a_person(svc, draft):
    v = gen(svc, MockLLMClient()).generate(draft, PUSH, count=1).created[0]
    assert v.variant_id not in {
        a.variant_id for a in svc.repo.list_variants(draft, status=VariantStatus.APPROVED)
    }
    with pytest.raises(Exception, match="approve endpoint"):
        svc.save_variant(draft, v.model_copy(update={"status": VariantStatus.APPROVED}))
    assert svc.approve_variant(draft, v.variant_id).variant.status == VariantStatus.APPROVED


# ---- failures and where it is allowed -----------------------------------------------------
def test_a_model_failure_stores_nothing(svc, draft):
    before = len(svc.repo.list_variants(draft))
    with pytest.raises(LLMError, match="down"):
        gen(svc, Scripted(LLMError("model is down"))).generate(draft, PUSH)
    with pytest.raises(LLMReplyError):
        gen(svc, Scripted("sorry, I cannot do that")).generate(draft, PUSH)
    assert len(svc.repo.list_variants(draft)) == before


def test_the_model_is_not_called_when_the_request_cannot_succeed(svc, draft):
    client = Scripted(items(GOOD))
    with pytest.raises(TemplateNotFoundError):
        gen(svc, client).generate(draft, "t_ghost")
    with pytest.raises(CampaignNotFoundError):
        gen(svc, client).generate("ghost", PUSH)
    svc.activate(draft)
    with pytest.raises(CampaignStateError, match="only a draft"):
        gen(svc, client).generate(draft, PUSH)
    svc.archive(draft)
    with pytest.raises(CampaignStateError):
        gen(svc, client).generate(draft, PUSH)
    assert client.requests == []


@pytest.mark.parametrize("count", [0, MAX_COUNT + 1, -1])
def test_count_is_bounded(svc, draft, count):
    with pytest.raises(ValueError, match="count"):
        gen(svc, Scripted(items(GOOD))).generate(draft, PUSH, count=count)


# ---- the prompt ---------------------------------------------------------------------------
def test_prompt_carries_the_brief():
    brief = DraftBrief(
        template_id=SMS,
        channel=Channel.SMS,
        body="Only {{units}} left. Reply STOP.",
        count=4,
        principles=["urgency"],
        tones=["brisk"],
        avoid=["old wording one", "old wording two"],
    )
    prompt = build_prompt(brief)
    for expected in (
        "exactly 4",
        "sms",
        "Only {{units}} left. Reply STOP.",
        "Reply STOP",
        "urgency",
        "brisk",
        "old wording one",
        "{{topItem}}",
        "JSON",
    ):
        assert expected in prompt


def test_prompt_only_mentions_stop_for_sms_and_trims_old_wordings():
    brief = DraftBrief(PUSH, Channel.PUSH, "Hi {{name}}", 2, avoid=[f"old {i}" for i in range(30)])
    prompt = build_prompt(brief)
    assert "STOP" not in prompt
    assert "old 29" in prompt and "old 0\n" not in prompt  # only the most recent are shown


def test_the_request_the_client_sees(svc, draft):
    client = Scripted(items(GOOD))
    gen(svc, client).generate(draft, PUSH, count=2, principles=["Urgency"])
    request = client.requests[0]
    assert request.brief.template_id == PUSH and request.brief.count == 2
    assert request.brief.principles == ["urgency"]
    assert len(request.brief.avoid) == 4  # the four sample push variants
    assert "JSON" in request.system and "exactly 2" in request.prompt


# ---- over HTTP ----------------------------------------------------------------------------
@pytest.fixture
def api(repo):
    """A server that uses the default mock model."""
    return TestClient(create_app(repo))


@pytest.fixture
def spring(api, config, library):
    data = config.model_dump(mode="json")
    data["use_case_id"] = "spring"
    assert api.post("/campaigns", json=data).status_code == 201
    for t in library.templates:
        assert (
            api.put(
                f"/campaigns/spring/templates/{t.template_id}", json=t.model_dump(mode="json")
            ).status_code
            == 200
        )
    return "/campaigns/spring/variants/generate"


def test_generate_over_http_with_the_default_mock(api, spring):
    response = api.post(spring, json={"template_id": PUSH, "count": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "mock" and body["requested"] == 2 and body["skipped"] == []
    assert [v["status"] for v in body["created"]] == ["pending", "pending"]
    assert [v["source"] for v in body["created"]] == ["llm", "llm"]
    stored = api.get("/campaigns/spring/variants?status=pending").json()
    assert {v["variant_id"] for v in stored} == {v["variant_id"] for v in body["created"]}


def test_default_count_is_three(api, spring):
    assert len(api.post(spring, json={"template_id": PUSH}).json()["created"]) == 3


def test_generate_request_errors(api, spring):
    assert api.post(spring, json={"template_id": "t_ghost"}).status_code == 404
    assert (
        api.post("/campaigns/ghost/variants/generate", json={"template_id": PUSH}).status_code
        == 404
    )
    live = api.post("/campaigns/target_cart/variants/generate", json={"template_id": PUSH})
    assert live.status_code == 409 and "only a draft" in live.json()["detail"]
    for bad in (
        {"template_id": PUSH, "count": 0},
        {"template_id": PUSH, "count": MAX_COUNT + 1},
        {"template_id": ""},
        {},
        {"template_id": PUSH, "colour": "red"},
    ):
        assert api.post(spring, json=bad).status_code == 422


def test_a_model_failure_is_502(repo, config, library):
    client = TestClient(create_app(repo, llm=Scripted(LLMError("rate limited"))))
    data = config.model_dump(mode="json")
    data["use_case_id"] = "spring"
    client.post("/campaigns", json=data)
    for t in library.templates:
        client.put(f"/campaigns/spring/templates/{t.template_id}", json=t.model_dump(mode="json"))
    response = client.post("/campaigns/spring/variants/generate", json={"template_id": PUSH})
    assert response.status_code == 502 and "rate limited" in response.json()["detail"]
    assert client.get("/campaigns/spring/variants").json() == []


def test_generate_review_activate_flow(api, spring):
    created = api.post(spring, json={"template_id": PUSH, "count": 2}).json()["created"]
    assert api.get("/campaigns/spring/validation").json()["can_activate"] is False
    first, second = (v["variant_id"] for v in created)
    assert api.post(f"/campaigns/spring/variants/{first}/approve").status_code == 200
    assert api.post(f"/campaigns/spring/variants/{second}/reject").status_code == 200
    assert api.get("/campaigns/spring/validation").json()["can_activate"] is True
    response = api.post("/campaigns/spring/activate", json={"replace_active": True})
    assert response.status_code == 200 and response.json()["status"] == "active"


def test_generated_variants_round_trip_through_the_model():
    v = MessageVariant.model_validate(
        {"variant_id": "a", "template_id": "t", "text": "x", "principle": "p", "tone": "t"}
    )
    assert v.status == VariantStatus.PENDING
