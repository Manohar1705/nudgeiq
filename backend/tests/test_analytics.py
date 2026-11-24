"""5B-7: uplift by channel, timing, offer, lever, cohort and variant."""

import pytest

from app.models import Channel
from app.orchestrator import DIMENSIONS, uplift_by, uplift_summary
from app.orchestrator.analytics import newcombe_interval, wilson_interval


# ----------------------------------------------------------------------- statistics
def test_wilson_matches_a_known_value():
    low, high = wilson_interval(5, 10)
    assert low == pytest.approx(0.2366, abs=1e-3)
    assert high == pytest.approx(0.7634, abs=1e-3)


def test_wilson_stays_inside_zero_and_one_at_the_extremes():
    assert wilson_interval(0, 20)[0] == 0.0
    assert wilson_interval(20, 20)[1] == 1.0
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_newcombe_difference_contains_the_estimate_and_is_wider_for_small_groups():
    low, high = newcombe_interval(50, 100, 30, 100)
    assert low < 0.20 < high and low > 0  # 20 points better, clearly above zero
    small_low, small_high = newcombe_interval(5, 10, 3, 10)
    assert (small_high - small_low) > (high - low)
    assert small_low < 0 < small_high  # same rates, far too little data to be sure


# ----------------------------------------------------------------------- grouping
def test_unknown_dimension_is_rejected(repo):
    with pytest.raises(ValueError, match="unknown dimension"):
        uplift_by(repo, "colour")
    with pytest.raises(ValueError, match="min_resolved"):
        uplift_by(repo, "channel", min_resolved=0)


def test_empty_repository_gives_empty_report(repo):
    for dim in DIMENSIONS:
        r = uplift_by(repo, dim)
        assert r.slices == [] and r.waited == 0
        assert r.overall.uplift_abs is None and not r.overall.enough_data


def test_by_channel_groups_and_compares_with_holdout(repo, many):
    many(6, 4, channel=Channel.PUSH)  # 60%
    many(1, 9, channel=Channel.EMAIL)  # 10%
    many(2, 8, kind="holdout")  # 20%
    r = uplift_by(repo, "channel", min_resolved=5)
    by = {s.key: s for s in r.slices}
    assert set(by) == {"push", "email"}
    assert by["push"].treated.conversion == pytest.approx(0.6)
    assert by["push"].baseline.conversion == pytest.approx(0.2)
    assert by["push"].uplift_abs == pytest.approx(0.4)
    assert by["push"].uplift_rel == pytest.approx(2.0)
    assert by["email"].uplift_abs == pytest.approx(-0.1)
    assert by["push"].treated.avg_reward == pytest.approx(24.0)  # 6 orders x 40 / 10
    assert r.overall.treated.decisions == 20 and r.overall.baseline.decisions == 10
    assert r.baseline == "all holdout customers" and "association" in r.caveat


def test_every_action_dimension_groups_by_its_own_value(repo, many):
    many(3, 1, timing="T+1h", offer="free_shipping", lever="loyalty_bonus")
    many(1, 3, timing="T+24h", offer=None, lever=None)
    many(1, 1, kind="holdout")
    assert {s.key for s in uplift_by(repo, "timing").slices} == {"T+1h", "T+24h"}
    assert {s.key for s in uplift_by(repo, "offer").slices} == {"free_shipping", "(none)"}
    assert {s.key for s in uplift_by(repo, "lever").slices} == {"loyalty_bonus", "(none)"}


def test_unresolved_and_wait_decisions_do_not_count_as_failures(repo, add, many):
    many(1, 1)  # resolved: 50%
    add(outcome=None)  # still waiting for the customer
    add("wait", outcome=None)
    many(1, 1, kind="holdout")
    r = uplift_by(repo, "channel")
    push = r.slices[0]
    assert push.treated.decisions == 3 and push.treated.resolved == 2
    assert push.treated.conversion == pytest.approx(0.5)
    assert r.waited == 1


def test_no_baseline_means_no_uplift_and_not_enough_data(repo, many):
    many(5, 5)
    s = uplift_by(repo, "channel", min_resolved=1).slices[0]
    assert s.baseline.resolved == 0
    assert s.uplift_abs is None and s.ci_low is None and not s.enough_data and not s.significant


def test_relative_uplift_is_none_when_the_holdout_never_converts(repo, many):
    many(5, 5)
    many(0, 10, kind="holdout")
    s = uplift_by(repo, "channel", min_resolved=5).slices[0]
    assert s.uplift_abs == pytest.approx(0.5) and s.uplift_rel is None
    assert s.ci_low is not None and s.ci_low > 0


# ----------------------------------------------------------------------- confidence
def test_small_groups_are_flagged_and_never_significant(repo, many):
    many(5, 0)  # 100%
    many(0, 5, kind="holdout")  # 0%
    s = uplift_by(repo, "channel").slices[0]  # default threshold is 30
    assert s.uplift_abs == pytest.approx(1.0)
    assert s.ci_low is not None and s.ci_high is not None  # numbers are still returned
    assert not s.enough_data and not s.significant


def test_large_clear_difference_is_significant(repo, many):
    many(60, 40)
    many(20, 80, kind="holdout")
    s = uplift_by(repo, "channel").slices[0]
    assert s.enough_data and s.significant
    assert s.ci_low is not None and s.ci_low > 0


def test_large_but_noisy_difference_is_enough_data_but_not_significant(repo, many):
    many(31, 69)
    many(28, 72, kind="holdout")
    s = uplift_by(repo, "channel").slices[0]
    assert s.enough_data and not s.significant
    assert s.ci_low is not None and s.ci_high is not None and s.ci_low < 0 < s.ci_high


def test_min_resolved_can_be_lowered(repo, many):
    many(8, 2)
    many(1, 9, kind="holdout")
    assert not uplift_by(repo, "channel").slices[0].enough_data
    s = uplift_by(repo, "channel", min_resolved=10).slices[0]
    assert s.enough_data and s.significant


# ----------------------------------------------------------------------- cohort
def test_cohort_slices_use_the_holdout_of_the_same_cohort(repo, many):
    many(8, 2, cohort="cart_high_aov")  # 80%
    many(2, 8, cohort="cart_low_aov")  # 20%
    many(2, 8, kind="holdout", cohort="cart_high_aov")  # 20%
    many(2, 8, kind="holdout", cohort="cart_low_aov")  # 20%
    r = uplift_by(repo, "cohort", min_resolved=5)
    by = {s.key: s for s in r.slices}
    assert by["cart_high_aov"].uplift_abs == pytest.approx(0.6)
    assert by["cart_low_aov"].uplift_abs == pytest.approx(0.0)
    assert r.baseline == "holdout of the same cohort"


def test_cohort_with_only_holdout_customers_still_shows_up(repo, many):
    many(1, 1, cohort="a")
    many(1, 1, kind="holdout", cohort="b")
    by = {s.key: s for s in uplift_by(repo, "cohort").slices}
    assert by["b"].treated.decisions == 0 and by["b"].uplift_abs is None


# ----------------------------------------------------------------------- variants and campaigns
def test_by_variant_keeps_unlabelled_decisions_apart(repo, many):
    many(3, 1, variant_id="v1")
    many(1, 3, variant_id="v2")
    many(1, 1, variant_id=None)
    many(1, 1, kind="holdout")
    keys = {s.key for s in uplift_by(repo, "variant", campaign_id="target_cart").slices}
    assert keys == {"v1", "v2", "(none)"}


def test_pooled_variant_report_separates_campaigns_with_the_same_variant_id(repo, many):
    many(2, 0, variant_id="v1", campaign="spring")
    many(0, 2, variant_id="v1", campaign="winter")
    keys = {s.key for s in uplift_by(repo, "variant").slices}
    assert keys == {"spring:v1", "winter:v1"}


def test_campaign_and_version_filters(repo, many):
    many(4, 0, campaign="spring", version=1)
    many(0, 4, campaign="spring", version=2)
    many(0, 4, campaign="winter", version=1)
    many(1, 3, kind="holdout", campaign="spring", version=1)
    assert uplift_by(repo, "channel").overall.treated.decisions == 12
    spring = uplift_by(repo, "channel", campaign_id="spring")
    assert spring.overall.treated.decisions == 8 and spring.campaign_id == "spring"
    v1 = uplift_by(repo, "channel", campaign_id="spring", campaign_version=1)
    assert v1.overall.treated.decisions == 4
    assert v1.overall.treated.conversion == 1.0 and v1.overall.baseline.decisions == 4


def test_uplift_summary_can_be_limited_to_one_campaign(repo, many):
    many(4, 0, campaign="spring")
    many(0, 4, campaign="winter")
    assert uplift_summary(repo).treated.decisions == 8
    only = uplift_summary(repo, campaign_id="spring")
    assert only.treated.decisions == 4 and only.treated.orders == 4


def test_breakdown_totals_match_the_pooled_summary(repo, many):
    many(4, 6, channel=Channel.PUSH)
    many(3, 7, channel=Channel.EMAIL)
    many(2, 8, kind="holdout")
    summary = uplift_summary(repo)
    report = uplift_by(repo, "channel")
    assert report.overall.treated.orders == summary.treated.orders
    assert sum(s.treated.orders for s in report.slices) == summary.treated.orders
    assert report.overall.treated.avg_reward == pytest.approx(summary.treated.avg_reward)
    assert report.overall.uplift_rel == pytest.approx(summary.uplift)
