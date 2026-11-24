"""Campaign runtime: keeps the orchestrator for the live campaign, and swaps it on reload.

Until now the API built one Orchestrator at start-up from the JSON files. This class builds it
from whichever campaign is ACTIVE in the repository instead, so a different campaign can go
live without restarting the server.

How it works:
  * `current()` gives the orchestrator for the live campaign, building it the first time.
  * `reload()` re-reads which campaign is live and swaps the orchestrator if it changed. It is
    explicit on purpose: call it after the live campaign changes. `activate()` and `archive()`
    here do the service call and the reload together, so the two cannot drift apart.
  * A reload with nothing live clears the orchestrator. `current()` then raises
    NoActiveCampaignError, which the API can turn into a clear 503.
  * Outcomes for a decision go to the campaign that made it (`orchestrator_for_decision`),
    so a late outcome is rewarded with the old campaign's rules and teaches the old campaign's
    learner, not the new one's.

The new orchestrator is built before the old one is touched. If building fails the exception
propagates and the previous orchestrator stays in place; the database has already changed by
then, so treat that as an error to fix, not as a normal state.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.campaigns import CampaignError, CampaignService
from app.graph.engine import check_engine, engine_from_env
from app.graph.trace import TraceRecorder
from app.models import Campaign
from app.orchestrator import DecisionNotFoundError, Dispatcher, LogDispatcher, Orchestrator
from app.orchestrator.orchestrator import DEFAULT_TRACED_DECISIONS
from app.repository.base import Repository


class NoActiveCampaignError(CampaignError, LookupError):
    """No campaign is live, so there is nothing to decide with."""


@dataclass(frozen=True)
class _Live:
    campaign_id: str
    version: int
    activated_at: datetime | None
    orchestrator: Orchestrator

    def matches(self, campaign: Campaign) -> bool:
        return (
            self.campaign_id == campaign.campaign_id
            and self.version == campaign.version
            and self.activated_at == campaign.activated_at
        )


class CampaignRuntime:
    def __init__(
        self,
        repo: Repository,
        service: CampaignService | None = None,
        *,
        dispatcher: Dispatcher | None = None,
        seed: int | None = None,
        id_factory: Callable[[], str] | None = None,
        engine: str | None = None,
    ) -> None:
        self.repo = repo
        # classic (default) or graph; None reads NUDGEIQ_ENGINE. A bad value fails at start-up.
        self._engine = check_engine(engine) if engine is not None else engine_from_env()
        self.service = service or CampaignService(repo)
        # One dispatcher for every campaign, so what was "sent" is in one place across swaps.
        self.dispatcher: Dispatcher = dispatcher or LogDispatcher()
        self._seed = seed
        self._id_factory = id_factory
        # One trace store for every campaign's orchestrator, so a decision's trace can still be
        # read after the live campaign is swapped (8e-4). Empty unless the engine is "graph".
        self.trace = TraceRecorder(max_runs=DEFAULT_TRACED_DECISIONS)
        self._lock = threading.RLock()
        self._live: _Live | None = None
        # Orchestrators of campaigns that are no longer live. They stay only so late
        # outcomes can still be applied to the campaign that made the decision.
        self._retired: dict[str, Orchestrator] = {}

    @property
    def engine(self) -> str:
        return self._engine

    # ------------------------------------------------------------------ the live campaign
    @property
    def is_ready(self) -> bool:
        return self._live is not None

    @property
    def active_campaign_id(self) -> str | None:
        live = self._live
        return live.campaign_id if live else None

    @property
    def active_version(self) -> int | None:
        live = self._live
        return live.version if live else None

    def current(self) -> Orchestrator:
        """The live campaign's orchestrator. Builds it on first use."""
        live = self._live
        if live is not None:
            return live.orchestrator
        orchestrator = self.reload()
        if orchestrator is None:
            raise NoActiveCampaignError("no campaign is active")
        return orchestrator

    def reload(self) -> Orchestrator | None:
        """Re-read which campaign is live. Returns its orchestrator, or None if none is live.

        If the same campaign and version are still live, the existing orchestrator is kept
        (and with it everything its learner holds in memory), so reloading twice is harmless.
        """
        with self._lock:
            campaign = self.service.get_active()  # raises if two are live: bad data
            if campaign is None:
                self._retire_current()
                return None
            live = self._live
            if live is not None and live.matches(campaign):
                return live.orchestrator
            orchestrator = self._build(campaign)  # may raise; the old one is untouched
            # Swap in one assignment, so a request never sees "nothing live" mid-swap.
            self._live = _Live(
                campaign.campaign_id, campaign.version, campaign.activated_at, orchestrator
            )
            if live is not None:
                self._retired[live.campaign_id] = live.orchestrator
            self._retired.pop(campaign.campaign_id, None)
            return orchestrator

    # ------------------------------------------------------------------ lifecycle + reload
    def activate(self, campaign_id: str, *, replace_active: bool = False) -> Campaign:
        """CampaignService.activate, then reload so the new campaign is the one serving."""
        with self._lock:
            campaign = self.service.activate(campaign_id, replace_active=replace_active)
            self.reload()
            return campaign

    def archive(self, campaign_id: str) -> Campaign:
        """CampaignService.archive, then reload (archiving the live campaign stops serving)."""
        with self._lock:
            campaign = self.service.archive(campaign_id)
            self.reload()
            return campaign

    # ------------------------------------------------------------------ routing
    def orchestrator_for(self, campaign_id: str) -> Orchestrator:
        """The orchestrator for a given campaign: the live one, or a retired one for old work."""
        with self._lock:
            live = self._live
            if live is not None and live.campaign_id == campaign_id:
                return live.orchestrator
            retired = self._retired.get(campaign_id)
            if retired is not None:
                return retired
            campaign = self.service.get(campaign_id)  # raises CampaignNotFoundError
            orchestrator = self._build(campaign)
            self._retired[campaign_id] = orchestrator
            return orchestrator

    def orchestrator_for_decision(self, decision_id: str) -> Orchestrator:
        """The orchestrator that should handle an outcome for this decision.

        Decisions made before campaigns were stamped have no campaign id; they go to the live
        campaign, as they always did.
        """
        decision = self.repo.get_decision(decision_id)
        if decision is None:
            raise DecisionNotFoundError(decision_id)
        if decision.use_case_id is None:
            return self.current()
        return self.orchestrator_for(decision.use_case_id)

    # ------------------------------------------------------------------ helpers
    def _build(self, campaign: Campaign) -> Orchestrator:
        return Orchestrator(
            self.repo,
            campaign.config,
            self.service.get_library(campaign.campaign_id),
            dispatcher=self.dispatcher,
            seed=self._seed,
            id_factory=self._id_factory,
            campaign_version=campaign.version,
            engine=self._engine,
            trace=self.trace,
        )

    def _retire_current(self) -> None:
        live = self._live
        if live is not None:
            self._retired[live.campaign_id] = live.orchestrator
            self._live = None
