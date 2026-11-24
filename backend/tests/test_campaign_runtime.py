import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.api.runtime import CampaignRuntime, NoActiveCampaignError
from app.campaigns import (
    ActivationBlockedError,
    CampaignNotFoundError,
    CampaignService,
    CampaignStateError,
)
from app.models import Campaign, CampaignStatus, Decision
from app.orchestrator import DecisionNotFoundError, LogDispatcher
from app.repository import InMemoryRepository


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(minutes=1)
        return self.t


@pytest.fixture
def svc(repo):
    return CampaignService(repo, clock=Clock())


@pytest.fixture
def rt(repo, svc):
    return CampaignRuntime(repo, svc, seed=1)


def with_id(config, campaign_id):
    return config.model_copy(update={"use_case_id": campaign_id})


def make_ready(svc, config, library, campaign_id):
    """A valid draft with the sample templates and variants (not yet active)."""
    cfg = with_id(config, campaign_id)
    svc.create_draft(cfg)
    for t in library.templates:
        svc.save_template(campaign_id, t)
    for v in library.variants:
        svc.save_variant(campaign_id, v)


# ---- nothing live -----------------------------------------------------------------------


def test_nothing_live_raises_a_clear_error(rt):
    assert not rt.is_ready
    assert rt.active_campaign_id is None and rt.active_version is None
    with pytest.raises(NoActiveCampaignError):
        rt.current()


def test_no_active_error_is_a_campaign_error_and_a_lookup_error(rt):
    from app.campaigns import CampaignError

    with pytest.raises(CampaignError):
        rt.current()
    with pytest.raises(LookupError):
        rt.current()


def test_reload_with_nothing_live_returns_none(rt):
    assert rt.reload() is None


# ---- building and keeping the live orchestrator -----------------------------------------


def test_current_builds_the_live_campaign_on_first_use(rt, svc, config, library):
    make_ready(svc, config, library, "a")
    svc.activate("a")
    orch = rt.current()  # no explicit reload needed
    assert orch.config.use_case_id == "a"
    assert rt.is_ready and rt.active_campaign_id == "a" and rt.active_version == 1


def test_orchestrator_is_built_from_the_stored_campaign(rt, svc, config, library):
    make_ready(svc, config, library, "a")
    svc.activate("a")
    orch = rt.current()
    assert orch.config == svc.get("a").config
    assert orch.campaign_version == 1


def test_reload_keeps_the_same_orchestrator_when_nothing_changed(rt, svc, config, library):
    make_ready(svc, config, library, "a")
    svc.activate("a")
    first = rt.reload()
    assert rt.reload() is first
    assert rt.current() is first


def test_all_orchestrators_share_one_dispatcher(repo, svc, config, library):
    dispatcher = LogDispatcher()
    rt = CampaignRuntime(repo, svc, dispatcher=dispatcher, seed=1)
    make_ready(svc, config, library, "a")
    make_ready(svc, config, library, "b")
    rt.activate("a")
    assert rt.current().dispatcher is dispatcher
    rt.activate("b", replace_active=True)
    assert rt.current().dispatcher is dispatcher


# ---- swapping ---------------------------------------------------------------------------


def test_activate_swaps_the_live_orchestrator(rt, svc, config, library):
    make_ready(svc, config, library, "a")
    make_ready(svc, config, library, "b")
    rt.activate("a")
    first = rt.current()
    rt.activate("b", replace_active=True)
    second = rt.current()
    assert second is not first
    assert second.config.use_case_id == "b"
    assert rt.active_campaign_id == "b"
    assert svc.get("a").status == CampaignStatus.ARCHIVED


def test_reload_after_the_service_changes_the_live_campaign(rt, svc, config, library):
    make_ready(svc, config, library, "a")
    make_ready(svc, config, library, "b")
    svc.activate("a")
    assert rt.current().config.use_case_id == "a"
    svc.activate("b", replace_active=True)  # changed behind the runtime's back
    assert rt.current().config.use_case_id == "a"  # explicit reload: still the old one
    assert rt.reload().config.use_case_id == "b"
    assert rt.current().config.use_case_id == "b"


def test_a_refused_activation_leaves_the_live_campaign_serving(rt, svc, config, library):
    make_ready(svc, config, library, "a")
    svc.create_draft(with_id(config, "b"))  # no variants, so it cannot activate
    rt.activate("a")
    before = rt.current()
    with pytest.raises(ActivationBlockedError):
        rt.activate("b", replace_active=True)
    assert rt.current() is before
    assert rt.active_campaign_id == "a"


def test_second_activation_without_replace_is_refused(rt, svc, config, library):
    make_ready(svc, config, library, "a")
    make_ready(svc, config, library, "b")
    rt.activate("a")
    with pytest.raises(CampaignStateError):
        rt.activate("b")
    assert rt.active_campaign_id == "a"


def test_archiving_the_live_campaign_stops_serving(rt, svc, config, library):
    make_ready(svc, config, library, "a")
    rt.activate("a")
    rt.current()
    rt.archive("a")
    assert not rt.is_ready
    with pytest.raises(NoActiveCampaignError):
        rt.current()


def test_archiving_a_draft_does_not_disturb_serving(rt, svc, config, library):
    make_ready(svc, config, library, "a")
    svc.create_draft(with_id(config, "draft"))
    rt.activate("a")
    before = rt.current()
    rt.archive("draft")
    assert rt.current() is before


def test_two_live_campaigns_is_reported_not_guessed(rt, svc, config, library):
    make_ready(svc, config, library, "a")
    svc.activate("a")
    svc.repo.save_campaign(
        Campaign(
            campaign_id="b",
            status=CampaignStatus.ACTIVE,
            config=with_id(config, "b"),
            activated_at=datetime(2026, 11, 3, tzinfo=UTC),
        )
    )
    with pytest.raises(CampaignStateError):
        rt.reload()


# ---- decisions know which campaign made them --------------------------------------------


def decide_once(orch, repo, make_state, now, customer_id="c1"):
    repo.save_customer(make_state(customer_id=customer_id))
    return orch.decide(customer_id, now)


def test_decisions_are_stamped_with_campaign_and_version(
    rt, svc, repo, config, library, make_state, now
):
    make_ready(svc, config, library, "a")
    rt.activate("a")
    decision = decide_once(rt.current(), repo, make_state, now)
    assert decision.use_case_id == "a"
    assert decision.campaign_version == 1
    stored = repo.get_decision(decision.decision_id)
    assert stored.use_case_id == "a" and stored.campaign_version == 1


def test_outcomes_go_to_the_campaign_that_made_the_decision(
    rt, svc, repo, config, library, make_state, now
):
    make_ready(svc, config, library, "a")
    make_ready(svc, config, library, "b")
    rt.activate("a")
    old_orch = rt.current()
    decision = decide_once(old_orch, repo, make_state, now)

    rt.activate("b", replace_active=True)

    routed = rt.orchestrator_for_decision(decision.decision_id)
    assert routed.config.use_case_id == "a"
    assert routed is old_orch  # the retired orchestrator is kept, not rebuilt
    assert rt.current().config.use_case_id == "b"


def test_a_retired_campaign_is_rebuilt_when_the_runtime_restarts(
    repo, svc, config, library, make_state, now
):
    make_ready(svc, config, library, "a")
    make_ready(svc, config, library, "b")
    first = CampaignRuntime(repo, svc, seed=1)
    first.activate("a")
    decision = decide_once(first.current(), repo, make_state, now)
    first.activate("b", replace_active=True)

    restarted = CampaignRuntime(repo, svc, seed=1)  # new process: nothing cached
    assert restarted.orchestrator_for_decision(decision.decision_id).config.use_case_id == "a"
    assert restarted.current().config.use_case_id == "b"


def test_unknown_decision_is_not_found(rt, svc, config, library):
    make_ready(svc, config, library, "a")
    rt.activate("a")
    with pytest.raises(DecisionNotFoundError):
        rt.orchestrator_for_decision("nope")


def test_unknown_decision_is_not_found_even_with_nothing_live(rt):
    with pytest.raises(DecisionNotFoundError):
        rt.orchestrator_for_decision("nope")


def test_old_decisions_without_a_campaign_go_to_the_live_one(
    rt, svc, repo, config, library, make_state
):
    make_ready(svc, config, library, "a")
    rt.activate("a")
    repo.save_customer(make_state())
    repo.save_decision(Decision(decision_id="legacy", customer_id="c1", state=make_state()))
    assert rt.orchestrator_for_decision("legacy") is rt.current()


def test_orchestrator_for_an_unknown_campaign(rt):
    with pytest.raises(CampaignNotFoundError):
        rt.orchestrator_for("nope")


def test_orchestrator_for_the_live_campaign_is_the_current_one(rt, svc, config, library):
    make_ready(svc, config, library, "a")
    rt.activate("a")
    assert rt.orchestrator_for("a") is rt.current()


# ---- threads ----------------------------------------------------------------------------


def test_concurrent_first_requests_share_one_orchestrator(config, library):
    # in-memory only: the PostgreSQL repository shares one connection, which is not a
    # thread test of the runtime
    repo = InMemoryRepository()
    svc = CampaignService(repo, clock=Clock())
    rt = CampaignRuntime(repo, svc, seed=1)
    make_ready(svc, config, library, "a")
    svc.activate("a")
    seen: list[object] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            seen.append(rt.current())
        except Exception as exc:  # noqa: BLE001 - the test reports whatever went wrong
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len({id(o) for o in seen}) == 1
