"""Contract tests for the 5B campaign, template and variant methods.

Every repository must behave the same way, so these run against memory and PostgreSQL
(PostgreSQL needs NUDGEIQ_TEST_DSN, see conftest.py).
"""

from datetime import UTC, datetime

import pytest

from app.models import (
    Campaign,
    CampaignStatus,
    MessageTemplate,
    MessageVariant,
    VariantSource,
    VariantStatus,
)

T = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


@pytest.fixture
def campaign(config):
    return Campaign(campaign_id=config.use_case_id, config=config, created_at=T, updated_at=T)


def _other(config, cid, minute, status=CampaignStatus.DRAFT):
    """A second campaign with its own id (model_copy skips validation, so we set both ids)."""
    cfg = config.model_copy(update={"use_case_id": cid})
    extra = {}
    when = T.replace(minute=minute)
    if status == CampaignStatus.ACTIVE:
        extra["activated_at"] = when
    if status == CampaignStatus.ARCHIVED:
        extra["archived_at"] = when
    return Campaign(
        campaign_id=cid, config=cfg, status=status, created_at=when, updated_at=when, **extra
    )


def _template(tid="t1", body="Hi {{first_name}}"):
    return MessageTemplate(template_id=tid, name=tid, channel="push", body=body)


def _variant(vid="v1", tid="t1", **kw):
    base = dict(
        variant_id=vid, template_id=tid, text="Hi {{first_name}}", principle="p", tone="warm"
    )
    base.update(kw)
    return MessageVariant(**base)


# ---- campaigns ----------------------------------------------------------------------------
def test_campaign_round_trip(repo, campaign):
    repo.save_campaign(campaign)
    assert repo.get_campaign(campaign.campaign_id) == campaign


def test_missing_campaign_is_none(repo):
    assert repo.get_campaign("nope") is None


def test_save_campaign_is_an_upsert(repo, campaign):
    repo.save_campaign(campaign)
    edited = campaign.model_copy(update={"version": 2})
    repo.save_campaign(edited)
    assert repo.get_campaign(campaign.campaign_id).version == 2
    assert len(repo.list_campaigns()) == 1


def test_list_campaigns_oldest_first_and_filter_by_status(repo, config):
    draft = _other(config, "b_draft", 5)
    active = _other(config, "a_active", 1, CampaignStatus.ACTIVE)
    archived = _other(config, "c_archived", 3, CampaignStatus.ARCHIVED)
    for c in (draft, active, archived):
        repo.save_campaign(c)
    assert [c.campaign_id for c in repo.list_campaigns()] == ["a_active", "c_archived", "b_draft"]
    assert [c.campaign_id for c in repo.list_campaigns(CampaignStatus.ACTIVE)] == ["a_active"]
    assert [c.campaign_id for c in repo.list_campaigns(CampaignStatus.DRAFT)] == ["b_draft"]


def test_status_change_round_trips(repo, campaign):
    repo.save_campaign(campaign)
    live = campaign.model_copy(update={"status": CampaignStatus.ACTIVE, "activated_at": T})
    repo.save_campaign(live)
    got = repo.get_campaign(campaign.campaign_id)
    assert got.status == CampaignStatus.ACTIVE and got.activated_at == T


# ---- templates ----------------------------------------------------------------------------
def test_template_round_trip_and_order(repo, campaign):
    repo.save_campaign(campaign)
    cid = campaign.campaign_id
    repo.save_template(cid, _template("t2"))
    repo.save_template(cid, _template("t1"))
    assert repo.get_template(cid, "t1") == _template("t1")
    assert [t.template_id for t in repo.list_templates(cid)] == ["t2", "t1"]


def test_template_upsert_keeps_position(repo, campaign):
    repo.save_campaign(campaign)
    cid = campaign.campaign_id
    repo.save_template(cid, _template("t1"))
    repo.save_template(cid, _template("t2"))
    repo.save_template(cid, _template("t1", body="Changed {{first_name}}"))
    listed = repo.list_templates(cid)
    assert [t.template_id for t in listed] == ["t1", "t2"]
    assert listed[0].body == "Changed {{first_name}}"


def test_missing_template_is_none_and_unknown_campaign_lists_empty(repo, campaign):
    repo.save_campaign(campaign)
    assert repo.get_template(campaign.campaign_id, "nope") is None
    assert repo.list_templates("no_such_campaign") == []


def test_template_needs_an_existing_campaign(repo):
    with pytest.raises(KeyError):
        repo.save_template("no_such_campaign", _template())


def test_templates_are_scoped_to_their_campaign(repo, campaign, config):
    other = _other(config, "other", 9)
    repo.save_campaign(campaign)
    repo.save_campaign(other)
    repo.save_template(campaign.campaign_id, _template("t1"))
    repo.save_template("other", _template("t1", body="Other {{first_name}}"))
    assert repo.get_template(campaign.campaign_id, "t1").body == "Hi {{first_name}}"
    assert repo.get_template("other", "t1").body == "Other {{first_name}}"
    assert len(repo.list_templates("other")) == 1


# ---- variants -----------------------------------------------------------------------------
@pytest.fixture
def seeded(repo, campaign):
    repo.save_campaign(campaign)
    repo.save_template(campaign.campaign_id, _template("t1"))
    repo.save_template(campaign.campaign_id, _template("t2"))
    return campaign.campaign_id


def test_variant_round_trip_keeps_every_field(repo, seeded):
    v = _variant(
        "v1",
        status=VariantStatus.REJECTED,
        source=VariantSource.LLM,
        generated_by="some-model",
        created_at=T,
        reviewed_at=T.replace(minute=5),
        review_note="too pushy",
    )
    repo.save_variant(seeded, v)
    assert repo.get_variant(seeded, "v1") == v


def test_missing_variant_is_none(repo, seeded):
    assert repo.get_variant(seeded, "nope") is None
    assert repo.list_variants("no_such_campaign") == []


def test_variant_needs_campaign_and_template(repo, seeded):
    with pytest.raises(KeyError):
        repo.save_variant("no_such_campaign", _variant())
    with pytest.raises(KeyError):
        repo.save_variant(seeded, _variant("v9", tid="no_such_template"))


def test_variant_list_order_and_filters(repo, seeded):
    repo.save_variant(seeded, _variant("v1", "t1", status=VariantStatus.APPROVED))
    repo.save_variant(seeded, _variant("v2", "t2", status=VariantStatus.PENDING))
    repo.save_variant(seeded, _variant("v3", "t1", status=VariantStatus.PENDING))
    ids = lambda vs: [v.variant_id for v in vs]  # noqa: E731
    assert ids(repo.list_variants(seeded)) == ["v1", "v2", "v3"]
    assert ids(repo.list_variants(seeded, template_id="t1")) == ["v1", "v3"]
    assert ids(repo.list_variants(seeded, status=VariantStatus.PENDING)) == ["v2", "v3"]
    both = repo.list_variants(seeded, template_id="t1", status=VariantStatus.PENDING)
    assert ids(both) == ["v3"]


def test_approving_a_variant_keeps_its_position(repo, seeded):
    repo.save_variant(seeded, _variant("v1"))
    repo.save_variant(seeded, _variant("v2"))
    approved = _variant("v1", status=VariantStatus.APPROVED, reviewed_at=T)
    repo.save_variant(seeded, approved)
    listed = repo.list_variants(seeded)
    assert [v.variant_id for v in listed] == ["v1", "v2"]
    assert listed[0].status == VariantStatus.APPROVED


def test_variants_are_scoped_to_their_campaign(repo, seeded, config):
    repo.save_campaign(_other(config, "other", 9))
    repo.save_template("other", _template("t1"))
    repo.save_variant(seeded, _variant("v1"))
    repo.save_variant("other", _variant("v1", text="Other {{first_name}}"))
    assert repo.get_variant(seeded, "v1").text == "Hi {{first_name}}"
    assert repo.get_variant("other", "v1").text == "Other {{first_name}}"
    assert len(repo.list_variants("other")) == 1
