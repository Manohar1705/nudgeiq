"""Seeding the repository from the JSON files (runs against memory and PostgreSQL)."""

import json
from datetime import UTC, datetime

import pytest

from app.models import CampaignStatus
from app.repository import seed_all, seed_campaign

T = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


def test_seed_creates_an_active_campaign_with_templates_and_variants(repo, config, library):
    result = seed_campaign(repo, "target_cart", now=T)
    assert result.created
    assert result.templates == len(library.templates)
    assert result.variants == len(library.variants)

    campaign = repo.get_campaign("target_cart")
    assert campaign.status == CampaignStatus.ACTIVE
    assert campaign.version == 1
    assert campaign.activated_at == T and campaign.created_at == T
    assert campaign.config == config
    assert repo.list_templates("target_cart") == library.templates
    # created_at is stamped when the file is loaded, so compare everything else
    stored = [v.model_dump(exclude={"created_at"}) for v in repo.list_variants("target_cart")]
    assert stored == [v.model_dump(exclude={"created_at"}) for v in library.variants]


def test_seed_keeps_each_variants_status_from_the_file(repo, library):
    seed_campaign(repo, "target_cart")
    stored = {v.variant_id: v.status for v in repo.list_variants("target_cart")}
    assert stored == {v.variant_id: v.status for v in library.variants}
    assert len(set(stored.values())) > 1  # the sample file mixes approved, pending, rejected


def test_seed_twice_changes_nothing(repo, library):
    seed_campaign(repo, "target_cart", now=T)
    again = seed_campaign(repo, "target_cart", now=T.replace(hour=23))
    assert not again.created and again.templates == 0
    assert repo.get_campaign("target_cart").activated_at == T
    assert len(repo.list_templates("target_cart")) == len(library.templates)
    assert len(repo.list_variants("target_cart")) == len(library.variants)


def test_seed_never_overwrites_a_stored_campaign(repo):
    seed_campaign(repo, "target_cart")
    edited = repo.get_campaign("target_cart").model_copy(update={"version": 7})
    repo.save_campaign(edited)
    seed_campaign(repo, "target_cart")
    assert repo.get_campaign("target_cart").version == 7


def test_unknown_use_case_raises_and_stores_nothing(repo):
    with pytest.raises(FileNotFoundError):
        seed_campaign(repo, "does_not_exist")
    assert repo.list_campaigns() == []


def test_bad_template_file_leaves_no_half_seed(repo, tmp_path):
    (tmp_path / "target_cart.json").write_text(
        json.dumps(
            {
                "templates": [],
                "variants": [
                    {
                        "variant_id": "v",
                        "template_id": "ghost",
                        "text": "x",
                        "principle": "p",
                        "tone": "t",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        seed_campaign(repo, "target_cart", template_dir=tmp_path)
    assert repo.get_campaign("target_cart") is None


def test_seed_all_covers_every_use_case_file(repo):
    results = seed_all(repo, now=T)
    assert [r.campaign_id for r in results] == ["target_cart"]
    assert all(r.created for r in results)
    assert [c.campaign_id for c in repo.list_campaigns(CampaignStatus.ACTIVE)] == ["target_cart"]
