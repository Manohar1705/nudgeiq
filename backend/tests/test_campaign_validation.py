import pytest

from app.agents.message import MessageAgent, MessageError
from app.campaigns import (
    Severity,
    ValidationIssue,
    ValidationReport,
    validate_campaign,
    validate_config,
    validate_draft,
    validate_library,
)
from app.models import (
    Channel,
    Cohort,
    MessageTemplate,
    MessageVariant,
    TemplateLibrary,
    VariantStatus,
)

# ---- helpers ----------------------------------------------------------------------------


def with_space(config, **changes):
    """Copy a config with action-space fields replaced (skips re-validation on purpose)."""
    return config.model_copy(
        update={"action_space": config.action_space.model_copy(update=changes)}
    )


def with_guardrails(config, **changes):
    return config.model_copy(update={"guardrails": config.guardrails.model_copy(update=changes)})


def template(channel=Channel.PUSH, body="Hi {{name}}, {{link}}", template_id="t1", offers=None):
    return MessageTemplate(
        template_id=template_id,
        name=template_id,
        channel=channel,
        body=body,
        offers=offers or [],
    )


def variant(
    template_id="t1",
    text="Hi {{name}}, {{link}}",
    status=VariantStatus.APPROVED,
    variant_id="v1",
):
    return MessageVariant(
        variant_id=variant_id,
        template_id=template_id,
        text=text,
        principle="commitment",
        tone="warm",
        status=status,
    )


def lib(templates, variants):
    return TemplateLibrary(templates=templates, variants=variants)


def push_only(config):
    """A config with a single channel, so small libraries are enough."""
    return with_space(config, channels=[Channel.PUSH])


def codes(issues):
    return {i.code for i in issues}


def find(issues, code):
    return [i for i in issues if i.code == code]


# ---- the sample campaign ----------------------------------------------------------------


def test_sample_campaign_is_clean(config, library):
    report = validate_campaign(config, library)
    assert report.issues == []
    assert report.is_valid and report.can_activate


def test_config_alone_skips_content_checks(config):
    report = validate_campaign(config)
    assert report.issues == []


# ---- report behaviour -------------------------------------------------------------------


def test_warnings_do_not_block_activation():
    report = ValidationReport(
        issues=[ValidationIssue(severity=Severity.WARNING, code="x", path="p", message="m")]
    )
    assert report.is_valid and report.can_activate
    assert report.warnings and not report.errors


def test_errors_block_activation():
    report = ValidationReport(
        issues=[ValidationIssue(severity=Severity.ERROR, code="x", path="p", message="m")]
    )
    assert not report.can_activate
    assert report.codes() == {"x"}


# ---- identity, cohorts, signals ---------------------------------------------------------


@pytest.mark.parametrize("bad", ["Target Cart", "9lives", "cart-recovery", ""])
def test_bad_use_case_id(config, bad):
    issues = validate_config(config.model_copy(update={"use_case_id": bad}))
    assert "bad_id" in codes(issues)


def test_blank_name(config):
    issues = validate_config(config.model_copy(update={"name": "   "}))
    assert "empty_name" in codes(issues)


def test_no_cohorts(config):
    issues = validate_config(config.model_copy(update={"cohorts": []}))
    assert "no_cohorts" in codes(issues)


def test_duplicate_cohort_ids(config):
    twin = Cohort(cohort_id="a", label="A", size=5)
    issues = validate_config(config.model_copy(update={"cohorts": [twin, twin]}))
    assert find(issues, "duplicate")[0].severity == Severity.ERROR


def test_empty_cohort_is_a_warning(config):
    empty = Cohort(cohort_id="a", label="A", size=0)
    issues = validate_config(config.model_copy(update={"cohorts": [empty]}))
    assert find(issues, "empty_cohort")[0].severity == Severity.WARNING


def test_all_signals_off_warns(config):
    off = [s.model_copy(update={"enabled": False}) for s in config.context_signals]
    issues = validate_config(config.model_copy(update={"context_signals": off}))
    assert "no_signals" in codes(issues)


def test_duplicate_signal_ids(config):
    first = config.context_signals[0]
    issues = validate_config(config.model_copy(update={"context_signals": [first, first]}))
    assert "duplicate" in codes(issues)


# ---- action space -----------------------------------------------------------------------


@pytest.mark.parametrize("menu", ["levers", "channels", "offers", "timings"])
def test_empty_menu_is_an_error(config, menu):
    issues = validate_config(with_space(config, **{menu: []}))
    hit = find(issues, "empty_menu")
    assert hit and hit[0].path == f"action_space.{menu}"


def test_duplicate_menu_entries(config):
    issues = validate_config(with_space(config, offers=["percent_off", "percent_off"]))
    assert "duplicate" in codes(issues)


def test_blank_menu_entry(config):
    issues = validate_config(with_space(config, levers=["free_shipping", " "]))
    assert "blank_option" in codes(issues)


def test_unknown_eligibility_rule_is_an_error(config):
    issues = validate_config(with_space(config, eligibility={"sms": "no_such_rule"}))
    bad = find(issues, "unknown_rule")
    assert bad and bad[0].severity == Severity.ERROR
    assert bad[0].path == "action_space.eligibility.sms"


def test_every_shipped_rule_name_is_accepted(config):
    from app.agents.eligibility import PREDICATES

    for rule in PREDICATES:
        issues = validate_config(with_space(config, eligibility={"sms": rule}))
        assert "unknown_rule" not in codes(issues)


def test_eligibility_for_unknown_option_warns(config):
    issues = validate_config(with_space(config, eligibility={"carrier_pigeon": "has_app"}))
    assert find(issues, "orphan_rule")[0].severity == Severity.WARNING


# ---- guardrails -------------------------------------------------------------------------


def test_zero_contact_cap_blocks_everything(config):
    issues = validate_config(with_guardrails(config, max_contacts_per_window=0))
    assert "no_contacts_allowed" in codes(issues)


def test_no_weekdays_allowed(config):
    issues = validate_config(with_guardrails(config, allowed_weekdays=[]))
    assert "no_weekdays" in codes(issues)


def test_weekday_out_of_range(config):
    issues = validate_config(with_guardrails(config, allowed_weekdays=[0, 7]))
    assert "bad_weekday" in codes(issues)


def test_cooldown_longer_than_window_warns(config):
    issues = validate_config(with_guardrails(config, cooldown_hours=24 * 31, window_days=30))
    assert find(issues, "cooldown_exceeds_window")[0].severity == Severity.WARNING


def test_timing_past_journey_window_warns(config):
    cfg = with_space(config, timings=["T+1h", "T+20d"])
    issues = validate_config(with_guardrails(cfg, exit_after_days=14))
    late = find(issues, "unreachable_timing")
    assert len(late) == 1 and late[0].path == "action_space.timings[1]"


# ---- rewards and learning ---------------------------------------------------------------


def test_purchase_without_reward_warns(config):
    rewards = config.rewards.model_copy(update={"points": {}})
    issues = validate_config(config.model_copy(update={"rewards": rewards}))
    assert "no_purchase_reward" in codes(issues)


def test_no_holdout_warns(config):
    learning = config.learning.model_copy(update={"holdout_fraction": 0})
    issues = validate_config(config.model_copy(update={"learning": learning}))
    assert "no_holdout" in codes(issues)


# ---- templates --------------------------------------------------------------------------


def test_template_unknown_variable(config):
    t = template(body="Hi {{nickname}}")
    issues = validate_library(push_only(config), lib([t], [variant(text="Hi {{name}}")]))
    bad = find(issues, "unknown_variable")
    assert bad and bad[0].severity == Severity.ERROR
    assert bad[0].path == "templates[t1].body"


def test_sms_template_needs_stop(config):
    t = template(channel=Channel.SMS, body="Sale now {{link}}")
    v = variant(text="Sale now {{link}} Reply STOP to opt out.")
    issues = validate_library(with_space(config, channels=[Channel.SMS]), lib([t], [v]))
    assert "sms_missing_stop" in codes(issues)


def test_template_with_unknown_offer(config):
    t = template(offers=["mystery_offer"])
    issues = validate_library(push_only(config), lib([t], [variant()]))
    assert find(issues, "unknown_offer")[0].severity == Severity.ERROR


def test_template_for_unused_channel_warns(config):
    t = template(channel=Channel.EMAIL, body="Subject: Hi\n\nHi {{name}}")
    v = variant(text="Subject: Hi\n\nHi {{name}}")
    p = template(template_id="t2")
    pv = variant(template_id="t2", variant_id="v2")
    issues = validate_library(push_only(config), lib([t, p], [v, pv]))
    assert find(issues, "unused_channel")[0].severity == Severity.WARNING


def test_email_without_subject_warns(config):
    t = template(channel=Channel.EMAIL, body="Hi {{name}}")
    v = variant(text="Subject: Hi\n\nHi {{name}}")
    issues = validate_library(with_space(config, channels=[Channel.EMAIL]), lib([t], [v]))
    assert find(issues, "email_no_subject")[0].severity == Severity.WARNING


def test_blank_template_body(config):
    t = template(body="  ")
    issues = validate_library(push_only(config), lib([t], [variant()]))
    assert "empty_body" in codes(issues)


# ---- variants ---------------------------------------------------------------------------


def test_approved_variant_problems_are_errors(config):
    v = variant(text="Hi {{nickname}}", status=VariantStatus.APPROVED)
    issues = validate_library(push_only(config), lib([template()], [v]))
    assert find(issues, "unknown_variable")[0].severity == Severity.ERROR


def test_pending_variant_problems_are_warnings(config):
    good = variant(variant_id="v1")
    pending = variant(variant_id="v2", text="Hi {{nickname}}", status=VariantStatus.PENDING)
    issues = validate_library(push_only(config), lib([template()], [good, pending]))
    bad = find(issues, "unknown_variable")
    assert bad and all(i.severity == Severity.WARNING for i in bad)
    assert validate_campaign(push_only(config), lib([template()], [good, pending])).can_activate


def test_rejected_variants_are_ignored(config):
    good = variant(variant_id="v1")
    rejected = variant(variant_id="v2", text="Hi {{nickname}}", status=VariantStatus.REJECTED)
    issues = validate_library(push_only(config), lib([template()], [good, rejected]))
    assert issues == []


def test_approved_sms_without_stop_is_an_error(config):
    t = template(channel=Channel.SMS, body="Sale {{link}} Reply STOP to opt out.")
    v = variant(text="Sale {{link}}")
    issues = validate_library(with_space(config, channels=[Channel.SMS]), lib([t], [v]))
    bad = [i for i in issues if i.path == "variants[v1].text"]
    assert codes(bad) == {"sms_missing_stop"} and bad[0].severity == Severity.ERROR


def test_validator_and_message_agent_agree_on_stop(config):
    """The send-time check and the activation check use one rule."""
    t = template(channel=Channel.SMS, body="Sale {{link}} Reply STOP to opt out.")
    v = variant(text="Sale {{link}}")
    library = lib([t], [v])
    with pytest.raises(MessageError):
        MessageAgent(library)
    issues = validate_library(with_space(config, channels=[Channel.SMS]), library)
    assert "sms_missing_stop" in codes(issues)


def test_long_sms_warns_about_segments(config):
    body = "Reply STOP to opt out. " + "x" * 200
    t = template(channel=Channel.SMS, body=body)
    v = variant(text=body)
    issues = validate_library(with_space(config, channels=[Channel.SMS]), lib([t], [v]))
    long = find(issues, "sms_too_long")
    assert long and long[0].severity == Severity.WARNING


def test_accented_sms_uses_the_shorter_limit(config):
    body = "Café offer {{link}} Reply STOP to opt out. " + "é" * 40  # about 100 characters
    t = template(channel=Channel.SMS, body=body)
    v = variant(text=body)
    issues = validate_library(with_space(config, channels=[Channel.SMS]), lib([t], [v]))
    assert "sms_too_long" in codes(issues)


def test_short_sms_has_no_length_warning(config):
    body = "Sale {{link}} Reply STOP to opt out."
    t = template(channel=Channel.SMS, body=body)
    v = variant(text=body)
    issues = validate_library(with_space(config, channels=[Channel.SMS]), lib([t], [v]))
    assert issues == []


# ---- readiness --------------------------------------------------------------------------


def test_no_approved_variants_is_an_error(config):
    pending = variant(status=VariantStatus.PENDING)
    issues = validate_library(push_only(config), lib([template()], [pending]))
    assert find(issues, "no_approved_variants")[0].severity == Severity.ERROR


def test_empty_library_is_an_error(config):
    issues = validate_library(config, TemplateLibrary())
    assert "no_approved_variants" in codes(issues)


def test_channel_without_approved_variant_warns(config):
    # config uses push, email and sms; only push is ready
    issues = validate_library(config, lib([template()], [variant()]))
    not_ready = {i.path for i in find(issues, "channel_not_ready")}
    assert not_ready == {"action_space.channels.email", "action_space.channels.sms"}
    assert all(i.severity == Severity.WARNING for i in find(issues, "channel_not_ready"))


def test_offer_without_a_carrier_warns(config):
    # the only template carries bonus_points, so the other offers have no variant
    t = template(offers=["bonus_points"])
    issues = validate_library(push_only(config), lib([t], [variant()]))
    not_ready = {i.path for i in find(issues, "offer_not_ready")}
    assert not_ready == {"action_space.offers.free_shipping", "action_space.offers.percent_off"}


def test_template_without_offer_list_carries_any_offer(config):
    issues = validate_library(push_only(config), lib([template()], [variant()]))
    assert "offer_not_ready" not in codes(issues)


# ---- raw data (what the dashboard sends) ------------------------------------------------


def test_draft_with_valid_data_is_clean(config, library):
    report = validate_draft(config.model_dump(mode="json"), library.model_dump(mode="json"))
    assert report.issues == []


def test_draft_schema_errors_carry_field_paths(config):
    data = config.model_dump(mode="json")
    data["cohorts"][0]["size"] = -1
    report = validate_draft(data)
    bad = find(report.issues, "schema")
    assert bad and bad[0].path == "config.cohorts[0].size"
    assert not report.can_activate


def test_draft_reports_every_schema_error(config):
    data = config.model_dump(mode="json")
    del data["name"]
    data["guardrails"]["window_days"] = 0
    report = validate_draft(data)
    paths = {i.path for i in find(report.issues, "schema")}
    assert {"config.name", "config.guardrails.window_days"} <= paths


def test_draft_rejects_unknown_fields(config):
    data = config.model_dump(mode="json")
    data["surprise"] = 1
    report = validate_draft(data)
    assert any(i.path == "config.surprise" for i in report.errors)


def test_draft_library_schema_error(config, library):
    lib_data = library.model_dump(mode="json")
    lib_data["variants"][0]["template_id"] = "does_not_exist"
    report = validate_draft(config.model_dump(mode="json"), lib_data)
    assert any(i.code == "schema" and i.path.startswith("library") for i in report.issues)
    assert not report.can_activate


def test_draft_with_bad_config_still_checks_library_schema(config, library):
    data = config.model_dump(mode="json")
    del data["name"]
    lib_data = library.model_dump(mode="json")
    lib_data["variants"][0]["template_id"] = "does_not_exist"
    report = validate_draft(data, lib_data)
    prefixes = {i.path.split(".")[0] for i in report.issues}
    assert {"config", "library"} <= prefixes


def test_draft_runs_content_checks_when_data_parses(config, library):
    data = config.model_dump(mode="json")
    data["action_space"]["eligibility"] = {"sms": "no_such_rule"}
    report = validate_draft(data, library.model_dump(mode="json"))
    assert "unknown_rule" in report.codes()
