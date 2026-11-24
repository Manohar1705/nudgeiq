"""5B-8a: approving and rejecting variants, and keeping LLM variants behind review."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.campaigns import (
    CampaignNotFoundError,
    CampaignService,
    CampaignStateError,
    ReviewRequiredError,
    VariantApprovalBlockedError,
    VariantNotFoundError,
    VariantStateError,
    variant_approval_issues,
)
from app.models import (
    MessageVariant,
    VariantSource,
    VariantStatus,
)

PUSH = "t_push_still_there"
SMS = "t_sms_low_stock"
GOOD = "Hi {{name}}, your {{topItem}} is waiting: {{link}}"


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(minutes=1)
        return self.t


def variant(vid="v_new", tid=PUSH, text=GOOD, **kw) -> MessageVariant:
    base = dict(variant_id=vid, template_id=tid, text=text, principle="social_proof", tone="warm")
    base.update(kw)
    return MessageVariant(**base)


def llm(vid="v_llm", **kw) -> MessageVariant:
    return variant(vid, source=VariantSource.LLM, generated_by="mock-model", **kw)


@pytest.fixture
def svc(repo):
    return CampaignService(repo, clock=Clock())


@pytest.fixture
def draft(svc, config, library):
    """A draft with the sample templates and no variants."""
    svc.create_draft(config)
    for t in library.templates:
        svc.save_template(config.use_case_id, t)
    return config.use_case_id


# ---- the shared check ---------------------------------------------------------------------
def test_approval_issues_ignore_the_current_status(library):
    template = next(t for t in library.templates if t.template_id == PUSH)
    bad = variant(text="Hi {{nickname}}", status=VariantStatus.REJECTED)
    issues = variant_approval_issues(bad, template)
    assert [i.code for i in issues] == ["unknown_variable"]
    assert issues[0].severity.value == "error"  # an error because it is judged as approved
    assert variant_approval_issues(variant(), template) == []


# ---- approve ------------------------------------------------------------------------------
def test_approve_a_pending_variant(svc, draft):
    svc.save_variant(draft, variant())
    review = svc.approve_variant(draft, "v_new", note="  reads well  ")
    assert review.variant.status == VariantStatus.APPROVED
    assert review.variant.review_note == "reads well"
    assert review.variant.reviewed_at is not None and review.warnings == []
    stored = svc.repo.get_variant(draft, "v_new")
    assert stored == review.variant  # persisted, not just returned


def test_approve_without_a_note_and_with_a_blank_note(svc, draft):
    svc.save_variant(draft, variant("a"))
    svc.save_variant(draft, variant("b"))
    assert svc.approve_variant(draft, "a").variant.review_note is None
    assert svc.approve_variant(draft, "b", note="   ").variant.review_note is None


def test_approval_does_not_change_the_campaign_version(svc, draft):
    before = svc.get(draft).version
    svc.save_variant(draft, variant())
    svc.approve_variant(draft, "v_new")
    assert svc.get(draft).version == before


def test_approval_returns_warnings_but_is_allowed(svc, draft):
    long_sms = "Only {{units}} left of {{topItem}} at {{store}}. " + "x" * 200 + " Reply STOP."
    svc.save_variant(draft, variant("v_sms", SMS, long_sms))
    review = svc.approve_variant(draft, "v_sms")
    assert review.variant.status == VariantStatus.APPROVED
    assert [w.code for w in review.warnings] == ["sms_too_long"]


@pytest.mark.parametrize(
    ("tid", "text", "code"),
    [
        (PUSH, "Hi {{nickname}}", "unknown_variable"),
        (PUSH, "   ", "empty_body"),
        (SMS, "Only {{units}} left of {{topItem}}: {{link}}", "sms_missing_stop"),
    ],
)
def test_approval_is_blocked_by_errors_and_changes_nothing(svc, draft, tid, text, code):
    svc.save_variant(draft, variant("v_bad", tid, text))
    with pytest.raises(VariantApprovalBlockedError) as err:
        svc.approve_variant(draft, "v_bad")
    assert code in {i.code for i in err.value.issues}
    assert code in str(err.value)
    stored = svc.repo.get_variant(draft, "v_bad")
    assert stored.status == VariantStatus.PENDING and stored.reviewed_at is None


def test_only_a_pending_variant_can_be_approved(svc, draft):
    svc.save_variant(draft, variant("a"))
    svc.approve_variant(draft, "a")
    with pytest.raises(VariantStateError, match="approved"):
        svc.approve_variant(draft, "a")
    svc.save_variant(draft, variant("r"))
    svc.reject_variant(draft, "r")
    with pytest.raises(VariantStateError, match="rejected"):
        svc.approve_variant(draft, "r")


def test_approving_lets_a_campaign_be_activated(svc, draft):
    svc.save_variant(draft, llm())
    assert "no_approved_variants" in svc.validate(draft).codes()
    svc.approve_variant(draft, "v_llm")
    assert "no_approved_variants" not in svc.validate(draft).codes()
    assert svc.validate(draft).can_activate


# ---- reject -------------------------------------------------------------------------------
def test_reject_a_pending_variant(svc, draft):
    svc.save_variant(draft, variant())
    review = svc.reject_variant(draft, "v_new", note="too pushy")
    assert review.variant.status == VariantStatus.REJECTED
    assert review.variant.review_note == "too pushy" and review.variant.reviewed_at is not None
    assert review.warnings == []


def test_reject_withdraws_an_approved_variant(svc, draft):
    svc.save_variant(draft, variant())
    svc.approve_variant(draft, "v_new")
    withdrawn = svc.reject_variant(draft, "v_new", note="legal asked us to pull it")
    assert withdrawn.variant.status == VariantStatus.REJECTED
    assert "no_approved_variants" in svc.validate(draft).codes()


def test_rejecting_twice_is_refused(svc, draft):
    svc.save_variant(draft, variant())
    svc.reject_variant(draft, "v_new")
    with pytest.raises(VariantStateError, match="already rejected"):
        svc.reject_variant(draft, "v_new")


# ---- where review is allowed --------------------------------------------------------------
def test_unknown_variant_and_campaign(svc, draft):
    with pytest.raises(VariantNotFoundError):
        svc.approve_variant(draft, "ghost")
    with pytest.raises(VariantNotFoundError):
        svc.reject_variant(draft, "ghost")
    with pytest.raises(CampaignNotFoundError):
        svc.approve_variant("ghost", "v_new")


def test_review_is_refused_on_a_live_or_archived_campaign(svc, config, library):
    svc.create_draft(config)
    for t in library.templates:
        svc.save_template(config.use_case_id, t)
    for v in library.variants:
        svc.save_variant(config.use_case_id, v)
    cid = config.use_case_id
    svc.save_variant(cid, variant("pending_one"))
    svc.activate(cid)
    with pytest.raises(CampaignStateError, match="only a draft"):
        svc.approve_variant(cid, "pending_one")
    with pytest.raises(CampaignStateError):
        svc.reject_variant(cid, library.variants[0].variant_id)
    svc.archive(cid)
    with pytest.raises(CampaignStateError):
        svc.approve_variant(cid, "pending_one")


# ---- LLM variants cannot skip review ------------------------------------------------------
def test_an_llm_variant_can_be_saved_as_pending(svc, draft):
    svc.save_variant(draft, llm())
    assert svc.repo.get_variant(draft, "v_llm").status == VariantStatus.PENDING


def test_an_llm_variant_cannot_be_saved_as_approved(svc, draft):
    with pytest.raises(ReviewRequiredError, match="approve endpoint"):
        svc.save_variant(draft, llm(status=VariantStatus.APPROVED))
    assert svc.repo.get_variant(draft, "v_llm") is None
    svc.save_variant(draft, llm())  # already stored as pending
    with pytest.raises(ReviewRequiredError):
        svc.save_variant(draft, llm(status=VariantStatus.APPROVED))
    assert svc.repo.get_variant(draft, "v_llm").status == VariantStatus.PENDING


def test_an_approved_llm_variant_can_be_resaved_unchanged_but_not_reworded(svc, draft):
    svc.save_variant(draft, llm())
    approved = svc.approve_variant(draft, "v_llm").variant
    svc.save_variant(draft, approved.model_copy(update={"tone": "friendly"}))  # same text
    assert svc.repo.get_variant(draft, "v_llm").tone == "friendly"
    reworded = approved.model_copy(update={"text": "Buy now {{name}} {{link}}"})
    with pytest.raises(ReviewRequiredError):
        svc.save_variant(draft, reworded)
    back = reworded.model_copy(update={"status": VariantStatus.PENDING})
    svc.save_variant(draft, back)  # an edit goes back to review
    assert svc.repo.get_variant(draft, "v_llm").status == VariantStatus.PENDING


def test_an_llm_variant_cannot_be_relabelled_as_human(svc, draft):
    svc.save_variant(draft, llm())
    laundered = llm().model_copy(update={"source": VariantSource.HUMAN})
    with pytest.raises(ReviewRequiredError, match="relabelled"):
        svc.save_variant(draft, laundered)
    approved_human = laundered.model_copy(update={"status": VariantStatus.APPROVED})
    with pytest.raises(ReviewRequiredError):
        svc.save_variant(draft, approved_human)


def test_llm_variants_may_be_rejected_by_saving(svc, draft):
    svc.save_variant(draft, llm())
    svc.save_variant(draft, llm(status=VariantStatus.REJECTED))
    assert svc.repo.get_variant(draft, "v_llm").status == VariantStatus.REJECTED


def test_human_variants_can_still_be_saved_as_approved(svc, draft):
    svc.save_variant(draft, variant(status=VariantStatus.APPROVED))
    assert svc.repo.get_variant(draft, "v_new").status == VariantStatus.APPROVED


def test_clone_keeps_approved_llm_variants(svc, draft):
    svc.save_variant(draft, llm())
    svc.approve_variant(draft, "v_llm")
    svc.clone(draft, "spring")
    copied = svc.repo.get_variant("spring", "v_llm")
    assert copied.status == VariantStatus.APPROVED and copied.source == VariantSource.LLM


# ---- over HTTP ----------------------------------------------------------------------------
@pytest.fixture
def client(repo):
    return TestClient(create_app(repo))


@pytest.fixture
def spring(client, config, library):
    """A 'spring' draft with the sample templates and no variants."""
    data = config.model_dump(mode="json")
    data["use_case_id"] = "spring"
    assert client.post("/campaigns", json=data).status_code == 201
    for t in library.templates:
        url = f"/campaigns/spring/templates/{t.template_id}"
        assert client.put(url, json=t.model_dump(mode="json")).status_code == 200
    return "/campaigns/spring/variants"


def put(client, base, v: MessageVariant):
    return client.put(f"{base}/{v.variant_id}", json=v.model_dump(mode="json"))


def test_approve_over_http(client, spring):
    put(client, spring, llm())
    response = client.post(f"{spring}/v_llm/approve", json={"note": "ok"})
    assert response.status_code == 200
    body = response.json()
    assert body["variant"]["status"] == "approved" and body["variant"]["review_note"] == "ok"
    assert body["variant"]["reviewed_at"] and body["warnings"] == []
    listed = client.get(f"{spring}?status=approved").json()
    assert [v["variant_id"] for v in listed] == ["v_llm"]


def test_approve_and_reject_accept_an_empty_body(client, spring):
    put(client, spring, variant("a"))
    put(client, spring, variant("b"))
    assert client.post(f"{spring}/a/approve").status_code == 200
    r = client.post(f"{spring}/b/reject")
    assert r.status_code == 200 and r.json()["variant"]["status"] == "rejected"


def test_approve_returns_warnings(client, spring):
    long_sms = "Only {{units}} left of {{topItem}} at {{store}}. " + "x" * 200 + " Reply STOP."
    put(client, spring, variant("v_sms", SMS, long_sms))
    body = client.post(f"{spring}/v_sms/approve").json()
    assert [w["code"] for w in body["warnings"]] == ["sms_too_long"]


def test_blocked_approval_is_422_with_the_issues(client, spring):
    put(client, spring, variant("bad", text="Hi {{nickname}}"))
    response = client.post(f"{spring}/bad/approve")
    assert response.status_code == 422
    body = response.json()
    assert "cannot be approved" in body["detail"]
    assert body["issues"][0]["code"] == "unknown_variable"
    assert client.get(f"{spring}?status=pending").json()[0]["variant_id"] == "bad"


def test_review_error_codes(client, spring):
    put(client, spring, variant("a"))
    assert client.post(f"{spring}/ghost/approve").status_code == 404
    assert client.post("/campaigns/ghost/variants/a/approve").status_code == 404
    assert client.post(f"{spring}/a/approve").status_code == 200
    assert client.post(f"{spring}/a/approve").status_code == 409  # no longer pending
    assert client.post(f"{spring}/a/reject").status_code == 200
    assert client.post(f"{spring}/a/reject").status_code == 409  # already rejected
    assert client.post(f"{spring}/a/approve", json={"colour": "red"}).status_code == 422


def test_the_live_campaign_cannot_be_reviewed(client, library):
    first = library.variants[0].variant_id
    response = client.post(f"/campaigns/target_cart/variants/{first}/reject")
    assert response.status_code == 409 and "only a draft" in response.json()["detail"]


def test_put_cannot_approve_an_llm_variant(client, spring):
    response = put(client, spring, llm(status=VariantStatus.APPROVED))
    assert response.status_code == 422 and "approve endpoint" in response.json()["detail"]
    assert client.get(spring).json() == []  # nothing was stored


def test_review_flow_to_activation(client, spring):
    put(client, spring, llm("l1", tid=PUSH))
    put(client, spring, llm("l2", tid=PUSH))
    assert client.get("/campaigns/spring/validation").json()["can_activate"] is False
    assert client.post(f"{spring}/l1/approve").status_code == 200
    assert client.post(f"{spring}/l2/reject", json={"note": "off brand"}).status_code == 200
    assert client.get("/campaigns/spring/validation").json()["can_activate"] is True
    activated = client.post("/campaigns/spring/activate", json={"replace_active": True})
    assert activated.status_code == 200 and activated.json()["status"] == "active"
