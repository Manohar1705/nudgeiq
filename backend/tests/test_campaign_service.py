from datetime import UTC, datetime, timedelta

import pytest

from app.campaigns import (
    ActivationBlockedError,
    ActiveCampaignExistsError,
    CampaignError,
    CampaignExistsError,
    CampaignIdMismatchError,
    CampaignNotFoundError,
    CampaignService,
    CampaignStateError,
)
from app.models import Campaign, CampaignStatus, MessageTemplate, MessageVariant
from app.repository import InMemoryRepository


class Clock:
    """Every call returns a later time, so ordering and updated_at are easy to assert."""

    def __init__(self) -> None:
        self.t = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(minutes=1)
        return self.t


@pytest.fixture
def svc(repo):
    return CampaignService(repo, clock=Clock())


def with_id(config, campaign_id):
    return config.model_copy(update={"use_case_id": campaign_id})


def ready(svc, config, library, campaign_id=None):
    """A draft with the sample templates and variants, which validates with no issues."""
    cfg = with_id(config, campaign_id) if campaign_id else config
    campaign = svc.create_draft(cfg)
    for t in library.templates:
        svc.save_template(cfg.use_case_id, t)
    for v in library.variants:
        svc.save_variant(cfg.use_case_id, v)
    return campaign


# ---- create and read --------------------------------------------------------------------


def test_create_draft(svc, config):
    campaign = svc.create_draft(config)
    assert campaign.status == CampaignStatus.DRAFT
    assert campaign.version == 1
    assert campaign.campaign_id == config.use_case_id
    assert campaign.activated_at is None and campaign.archived_at is None
    assert svc.get(config.use_case_id) == campaign


def test_create_draft_twice_is_refused(svc, config):
    svc.create_draft(config)
    with pytest.raises(CampaignExistsError):
        svc.create_draft(config)


def test_a_draft_may_be_saved_with_validation_errors(svc, config):
    broken = config.model_copy(update={"name": "  "})
    svc.create_draft(broken)
    assert "empty_name" in svc.validate(config.use_case_id).codes()


def test_unknown_campaign_raises_not_found(svc, config, library):
    t = library.templates[0]
    calls = [
        lambda: svc.get("nope"),
        lambda: svc.validate("nope"),
        lambda: svc.get_library("nope"),
        lambda: svc.update_config("nope", config),
        lambda: svc.save_template("nope", t),
        lambda: svc.activate("nope"),
        lambda: svc.archive("nope"),
        lambda: svc.clone("nope", "copy"),
    ]
    for call in calls:
        with pytest.raises(CampaignNotFoundError):
            call()


def test_every_rule_failure_is_a_campaign_error(svc, config):
    svc.create_draft(config)
    with pytest.raises(CampaignError):
        svc.create_draft(config)
    with pytest.raises(CampaignError):
        svc.get("nope")


def test_list_campaigns_filters_by_status(svc, config, library):
    ready(svc, config, library)
    svc.create_draft(with_id(config, "other"))
    svc.activate(config.use_case_id)
    assert [c.campaign_id for c in svc.list_campaigns()] == [config.use_case_id, "other"]
    assert [c.campaign_id for c in svc.list_campaigns(CampaignStatus.DRAFT)] == ["other"]


def test_get_library_returns_what_was_saved(svc, config, library):
    ready(svc, config, library)
    got = svc.get_library(config.use_case_id)
    assert got.templates == library.templates
    assert got.variants == library.variants


# ---- editing drafts ---------------------------------------------------------------------


def test_update_config_bumps_version_and_time(svc, config):
    first = svc.create_draft(config)
    edited = config.model_copy(update={"name": "Renamed"})
    second = svc.update_config(config.use_case_id, edited)
    assert second.version == 2
    assert second.name == "Renamed"
    assert second.created_at == first.created_at
    assert second.updated_at > first.updated_at
    assert svc.get(config.use_case_id) == second


def test_each_edit_bumps_the_version_again(svc, config):
    svc.create_draft(config)
    svc.update_config(config.use_case_id, config)
    third = svc.update_config(config.use_case_id, config)
    assert third.version == 3


def test_update_config_cannot_change_the_id(svc, config):
    svc.create_draft(config)
    with pytest.raises(CampaignIdMismatchError):
        svc.update_config(config.use_case_id, with_id(config, "something_else"))
    assert svc.get(config.use_case_id).version == 1


def test_a_draft_can_be_edited_into_an_invalid_state(svc, config):
    svc.create_draft(config)
    svc.update_config(config.use_case_id, config.model_copy(update={"name": ""}))
    assert not svc.validate(config.use_case_id).can_activate


def test_template_and_variant_edits_do_not_bump_the_version(svc, config, library):
    ready(svc, config, library)
    assert svc.get(config.use_case_id).version == 1


def test_save_variant_needs_an_existing_template(svc, config):
    svc.create_draft(config)
    orphan = MessageVariant(
        variant_id="v1", template_id="missing", text="Hi", principle="p", tone="t"
    )
    with pytest.raises(KeyError):
        svc.save_variant(config.use_case_id, orphan)


# ---- only drafts are editable -----------------------------------------------------------


@pytest.fixture(params=["active", "archived"])
def locked(request, svc, config, library):
    ready(svc, config, library)
    if request.param == "active":
        svc.activate(config.use_case_id)
    else:
        svc.archive(config.use_case_id)
    return config.use_case_id


def test_locked_campaign_cannot_be_edited(svc, config, library, locked):
    template = MessageTemplate(template_id="x", name="x", channel="push", body="Hi {{name}}")
    variant = library.variants[0]
    with pytest.raises(CampaignStateError):
        svc.update_config(locked, config)
    with pytest.raises(CampaignStateError):
        svc.save_template(locked, template)
    with pytest.raises(CampaignStateError):
        svc.save_variant(locked, variant)


def test_locked_campaign_is_unchanged_after_a_refused_edit(svc, config, locked):
    before = svc.get(locked)
    with pytest.raises(CampaignStateError):
        svc.update_config(locked, config.model_copy(update={"name": "Hacked"}))
    assert svc.get(locked) == before


# ---- activation -------------------------------------------------------------------------


def test_activate_a_valid_draft(svc, config, library):
    ready(svc, config, library)
    live = svc.activate(config.use_case_id)
    assert live.status == CampaignStatus.ACTIVE
    assert live.activated_at is not None
    assert live.version == 1  # activating does not change the version
    assert svc.get(config.use_case_id) == live
    assert svc.get_active() == live


def test_activation_is_blocked_by_validation_errors(svc, config):
    svc.create_draft(config)  # no variants, so nothing can be sent
    with pytest.raises(ActivationBlockedError) as err:
        svc.activate(config.use_case_id)
    assert "no_approved_variants" in err.value.report.codes()
    assert not err.value.report.can_activate
    assert svc.get(config.use_case_id).status == CampaignStatus.DRAFT


def test_activation_ignores_warnings(svc, config, library):
    ready(svc, config, library)
    no_holdout = config.learning.model_copy(update={"holdout_fraction": 0})
    svc.update_config(config.use_case_id, config.model_copy(update={"learning": no_holdout}))
    assert "no_holdout" in svc.validate(config.use_case_id).codes()
    assert svc.activate(config.use_case_id).status == CampaignStatus.ACTIVE


def test_activation_blocked_message_counts_errors(svc, config):
    svc.create_draft(config)
    with pytest.raises(ActivationBlockedError, match="1 validation error"):
        svc.activate(config.use_case_id)


@pytest.mark.parametrize("final", ["active", "archived"])
def test_only_a_draft_can_be_activated(svc, config, library, final):
    ready(svc, config, library)
    svc.activate(config.use_case_id)
    if final == "archived":
        svc.archive(config.use_case_id)
    with pytest.raises(CampaignStateError):
        svc.activate(config.use_case_id)


# ---- the one-active rule ----------------------------------------------------------------


def test_second_activation_is_refused_by_default(svc, config, library):
    ready(svc, config, library)
    ready(svc, config, library, "other")
    svc.activate(config.use_case_id)
    with pytest.raises(ActiveCampaignExistsError) as err:
        svc.activate("other")
    assert err.value.active_id == config.use_case_id
    assert svc.get("other").status == CampaignStatus.DRAFT
    assert svc.get(config.use_case_id).status == CampaignStatus.ACTIVE


def test_replace_active_archives_the_old_one(svc, config, library):
    ready(svc, config, library)
    ready(svc, config, library, "other")
    svc.activate(config.use_case_id)
    live = svc.activate("other", replace_active=True)
    old = svc.get(config.use_case_id)
    assert old.status == CampaignStatus.ARCHIVED and old.archived_at is not None
    assert live.status == CampaignStatus.ACTIVE
    assert svc.get_active() == live


def test_failed_replacement_leaves_the_live_campaign_alone(svc, config, library):
    ready(svc, config, library)
    svc.create_draft(with_id(config, "other"))  # no variants, so it cannot activate
    svc.activate(config.use_case_id)
    with pytest.raises(ActivationBlockedError):
        svc.activate("other", replace_active=True)
    assert svc.get(config.use_case_id).status == CampaignStatus.ACTIVE
    assert svc.get_active().campaign_id == config.use_case_id


def test_get_active_is_none_when_nothing_is_live(svc, config):
    assert svc.get_active() is None
    svc.create_draft(config)
    assert svc.get_active() is None


def test_get_active_refuses_when_two_are_live(svc, config, library):
    ready(svc, config, library)
    svc.activate(config.use_case_id)
    when = datetime(2026, 11, 3, tzinfo=UTC)
    svc.repo.save_campaign(
        Campaign(
            campaign_id="other",
            status=CampaignStatus.ACTIVE,
            config=with_id(config, "other"),
            activated_at=when,
        )
    )
    with pytest.raises(CampaignStateError, match="more than one active"):
        svc.get_active()


# ---- archive ----------------------------------------------------------------------------


def test_archive_a_draft(svc, config):
    svc.create_draft(config)
    archived = svc.archive(config.use_case_id)
    assert archived.status == CampaignStatus.ARCHIVED
    assert archived.archived_at is not None
    assert svc.get(config.use_case_id) == archived


def test_archive_an_active_campaign(svc, config, library):
    ready(svc, config, library)
    svc.activate(config.use_case_id)
    archived = svc.archive(config.use_case_id)
    assert archived.status == CampaignStatus.ARCHIVED
    assert archived.activated_at is not None  # history is kept
    assert svc.get_active() is None


def test_archived_is_final(svc, config):
    svc.create_draft(config)
    svc.archive(config.use_case_id)
    with pytest.raises(CampaignStateError):
        svc.archive(config.use_case_id)


# ---- clone ------------------------------------------------------------------------------


def test_clone_copies_settings_templates_and_variants(svc, config, library):
    ready(svc, config, library)
    svc.activate(config.use_case_id)
    copy = svc.clone(config.use_case_id, "target_cart_v2")

    assert copy.campaign_id == "target_cart_v2"
    assert copy.config.use_case_id == "target_cart_v2"
    assert copy.status == CampaignStatus.DRAFT
    assert copy.version == 1
    assert copy.activated_at is None and copy.archived_at is None
    assert copy.name == f"{config.name} (copy)"
    assert copy.config.action_space == config.action_space

    cloned = svc.get_library("target_cart_v2")
    assert cloned.templates == library.templates
    assert cloned.variants == library.variants  # statuses are kept


def test_clone_leaves_the_source_untouched(svc, config, library):
    ready(svc, config, library)
    svc.activate(config.use_case_id)
    before = svc.get(config.use_case_id)
    svc.clone(config.use_case_id, "target_cart_v2")
    assert svc.get(config.use_case_id) == before
    assert svc.get_active() == before


def test_clone_with_a_chosen_name(svc, config, library):
    ready(svc, config, library)
    copy = svc.clone(config.use_case_id, "spring", name="Spring recovery")
    assert copy.name == "Spring recovery"


def test_clone_of_an_archived_campaign_is_allowed(svc, config, library):
    ready(svc, config, library)
    svc.archive(config.use_case_id)
    copy = svc.clone(config.use_case_id, "revived")
    assert copy.status == CampaignStatus.DRAFT


def test_clone_onto_an_existing_id_is_refused(svc, config, library):
    ready(svc, config, library)
    svc.create_draft(with_id(config, "taken"))
    with pytest.raises(CampaignExistsError):
        svc.clone(config.use_case_id, "taken")


def test_clone_with_a_bad_id_is_a_draft_that_cannot_activate(svc, config, library):
    ready(svc, config, library)
    copy = svc.clone(config.use_case_id, "Bad Id")
    assert copy.status == CampaignStatus.DRAFT
    with pytest.raises(ActivationBlockedError) as err:
        svc.activate("Bad Id")
    assert "bad_id" in err.value.report.codes()


def test_edit_a_clone_without_touching_the_source(svc, config, library):
    ready(svc, config, library)
    svc.activate(config.use_case_id)
    svc.clone(config.use_case_id, "v2")
    svc.update_config("v2", with_id(config, "v2").model_copy(update={"name": "Version two"}))
    assert svc.get("v2").name == "Version two"
    assert svc.get("v2").version == 2
    assert svc.get(config.use_case_id).name == config.name


# ---- the whole workflow -----------------------------------------------------------------


def test_clone_edit_and_swap_the_live_campaign(svc, config, library):
    """The intended way to change a live campaign."""
    ready(svc, config, library)
    svc.activate(config.use_case_id)

    svc.clone(config.use_case_id, "v2")
    svc.update_config("v2", with_id(config, "v2").model_copy(update={"name": "Improved"}))
    live = svc.activate("v2", replace_active=True)

    assert svc.get_active() == live
    assert live.campaign_id == "v2" and live.version == 2
    assert svc.get(config.use_case_id).status == CampaignStatus.ARCHIVED


# ---- persistence ------------------------------------------------------------------------


class CountingRepo(InMemoryRepository):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1


def test_every_change_is_flushed(config, library):
    repo = CountingRepo()
    svc = CampaignService(repo, clock=Clock())
    ready(svc, config, library)
    after_setup = repo.flushes
    assert after_setup > 0
    svc.activate(config.use_case_id)
    assert repo.flushes > after_setup
    seen = repo.flushes
    svc.archive(config.use_case_id)
    assert repo.flushes > seen
