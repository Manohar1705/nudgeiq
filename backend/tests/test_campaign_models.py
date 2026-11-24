from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.config.loader import load_template_library, load_use_case
from app.models import (
    Campaign,
    CampaignStatus,
    Channel,
    Decision,
    MessageVariant,
    VariantSource,
    VariantStatus,
)

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


def make_campaign(**kw):
    config = load_use_case("target_cart")
    base = dict(campaign_id=config.use_case_id, config=config)
    base.update(kw)
    return Campaign(**base)


def test_new_campaign_is_an_editable_version_one_draft():
    c = make_campaign()
    assert c.status == CampaignStatus.DRAFT
    assert c.version == 1
    assert c.is_editable
    assert c.name == c.config.name
    assert c.activated_at is None and c.archived_at is None


def test_campaign_id_must_match_the_config():
    with pytest.raises(ValidationError, match="campaign_id"):
        make_campaign(campaign_id="something_else")


def test_version_must_be_positive():
    with pytest.raises(ValidationError):
        make_campaign(version=0)


def test_status_and_timestamps_must_agree():
    with pytest.raises(ValidationError, match="activated_at"):
        make_campaign(status=CampaignStatus.ACTIVE)
    with pytest.raises(ValidationError, match="archived_at"):
        make_campaign(status=CampaignStatus.ARCHIVED)
    with pytest.raises(ValidationError, match="archived_at"):
        make_campaign(status=CampaignStatus.DRAFT, archived_at=NOW)
    active = make_campaign(status=CampaignStatus.ACTIVE, activated_at=NOW)
    assert not active.is_editable
    archived = make_campaign(status=CampaignStatus.ARCHIVED, activated_at=NOW, archived_at=NOW)
    assert not archived.is_editable


def test_allowed_status_transitions():
    draft = make_campaign()
    assert draft.can_become(CampaignStatus.ACTIVE)
    assert draft.can_become(CampaignStatus.ARCHIVED)
    assert not draft.can_become(CampaignStatus.DRAFT)
    active = make_campaign(status=CampaignStatus.ACTIVE, activated_at=NOW)
    assert active.can_become(CampaignStatus.ARCHIVED)
    assert not active.can_become(CampaignStatus.DRAFT)
    archived = make_campaign(status=CampaignStatus.ARCHIVED, archived_at=NOW)
    assert not any(archived.can_become(s) for s in CampaignStatus)


def test_campaign_survives_a_json_round_trip():
    c = make_campaign()
    again = Campaign.model_validate_json(c.model_dump_json())
    assert again == c


def test_campaign_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        make_campaign(surprise=1)


def test_variant_defaults_are_human_and_unreviewed():
    v = MessageVariant(variant_id="v1", template_id="t1", text="Hi", principle="p", tone="warm")
    assert v.status == VariantStatus.PENDING
    assert v.source == VariantSource.HUMAN
    assert v.generated_by is None and v.reviewed_at is None and v.review_note is None
    assert v.created_at.tzinfo is not None


def test_llm_variant_records_who_wrote_it():
    v = MessageVariant(
        variant_id="v2",
        template_id="t1",
        text="Hi",
        principle="p",
        tone="warm",
        source=VariantSource.LLM,
        generated_by="test-model",
    )
    assert v.source == VariantSource.LLM and v.status == VariantStatus.PENDING


def test_existing_template_files_still_load_unchanged():
    library = load_template_library("target_cart")
    assert library.variants
    assert all(v.source == VariantSource.HUMAN for v in library.variants)
    assert {t.channel for t in library.templates} >= {Channel.PUSH, Channel.SMS}


def test_decision_new_fields_default_to_none(make_state):
    d = Decision(decision_id="d1", customer_id="c1", state=make_state())
    assert d.use_case_id is None and d.campaign_version is None and d.template_id is None
    with pytest.raises(ValidationError):
        Decision(decision_id="d1", customer_id="c1", state=make_state(), campaign_version=0)
