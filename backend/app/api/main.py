"""FastAPI app. Run:  uvicorn app.api.main:app --reload   then open http://127.0.0.1:8000/docs

Storage: NUDGEIQ_DATABASE_URL=postgresql://... uses PostgreSQL; otherwise
NUDGEIQ_DATA_FILE=data.json keeps a JSON file; otherwise data lives in memory only.

Campaigns live in the repository. On the first start, when the repository holds no campaigns
at all, the JSON use case is copied in as the active campaign. After that the repository is the
source of truth, and the JSON files are never read again. The API serves whichever campaign is
active through a CampaignRuntime; with none active the decision endpoints answer 503.
"""

import os
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agents.state_agent import CustomerNotFoundError
from app.api.campaigns import create_campaign_router, install_campaign_error_handlers
from app.api.demo import create_demo_router
from app.api.hardening import install_hardening
from app.api.hardening import log as app_log
from app.api.runtime import CampaignRuntime, NoActiveCampaignError
from app.api.variant_runs import create_variant_run_router
from app.campaigns import CampaignNotFoundError, CampaignService
from app.graph.trace import DecisionTrace
from app.llm import LLMClient, llm_from_env
from app.models import (
    Campaign,
    CampaignStatus,
    CustomerState,
    Decision,
    Outcome,
    OutcomeKeyConflict,
    OutcomeResult,
    RewardRecord,
    ScheduledSend,
    SendStatus,
    TriggerEvent,
    UseCaseConfig,
)
from app.orchestrator import (
    DEFAULT_MIN_RESOLVED,
    DecisionNotFoundError,
    Dimension,
    GuardrailSummary,
    InvalidEventError,
    MessageFunnelRow,
    OutcomeMismatchError,
    ScheduledDispatcher,
    UpliftReport,
    UpliftSummary,
    guardrail_summary,
    message_funnel,
    uplift_by,
    uplift_summary,
)
from app.repository import (
    InMemoryRepository,
    JsonFileRepository,
    PostgresRepository,
    Repository,
    seed_campaign,
)
from app.simulator.customers import generate_customers


class DecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str
    # Pretend it is this moment (UTC). Useful because guardrails block sends outside 09-21 UTC.
    as_of: datetime | None = None


class OutcomeAccepted(BaseModel):
    """What POST /outcomes answers (202) when outcomes are processed in the background."""

    model_config = ConfigDict(extra="forbid")
    outcome_id: int
    decision_id: str
    status: str  # pending: a worker will process it | processed / failed: a repeated key
    queued: bool  # False: the queue was unreachable; it stays pending and the sweep queues it
    duplicate: bool  # True: this Idempotency-Key was stored before; nothing new was saved


class SeedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(default=100, ge=1, le=5000)
    seed: int = 0


class DecisionDetail(BaseModel):
    decision: Decision
    outcomes: list[Outcome]
    reward: RewardRecord | None


class CampaignSummary(BaseModel):
    use_case_id: str
    name: str
    action_space_size: int
    active_signals: list[str]
    active: bool  # True for the campaign this server is running
    status: CampaignStatus
    version: int

    @classmethod
    def of(cls, campaign: Campaign) -> "CampaignSummary":
        config = campaign.config
        return cls(
            use_case_id=campaign.campaign_id,
            name=campaign.name,
            action_space_size=config.action_space.size,
            active_signals=config.active_signals,
            active=campaign.status == CampaignStatus.ACTIVE,
            status=campaign.status,
            version=campaign.version,
        )


def scheduled_sends_enabled(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    source = os.environ if env is None else env
    return (source.get("NUDGEIQ_SCHEDULED_SENDS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def async_outcomes_enabled(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    source = os.environ if env is None else env
    return (source.get("NUDGEIQ_ASYNC_OUTCOMES") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def create_app(
    repo: Repository | None = None,
    use_case_id: str = "target_cart",
    llm: LLMClient | None = None,
    async_outcomes: bool | None = None,
) -> FastAPI:
    # Bad model settings fail here, before the repository is opened or seeded.
    llm_client: LLMClient = llm if llm is not None else llm_from_env()
    if repo is None:
        database_url = os.environ.get("NUDGEIQ_DATABASE_URL")
        data_file = os.environ.get("NUDGEIQ_DATA_FILE")
        if database_url:
            repo = PostgresRepository(database_url)
        elif data_file:
            repo = JsonFileRepository(data_file)
        else:
            repo = InMemoryRepository()
    store: Repository = repo

    # First start only: an empty repository gets the JSON use case as its active campaign.
    # A repository that already has campaigns is never touched, so this cannot undo edits or
    # create a second active campaign.
    if not store.list_campaigns():
        seed_campaign(store, use_case_id)

    # 8d-3b step 3c. None = read NUDGEIQ_ASYNC_OUTCOMES. An explicit value is for tests and skips
    # the PostgreSQL check below (the caller says there is no real worker to share a database).
    if async_outcomes is None:
        async_outcomes = async_outcomes_enabled()
        if async_outcomes and not isinstance(store, PostgresRepository):
            # Fail at start-up: only PostgreSQL is shared with the worker that processes them.
            raise ValueError(
                "NUDGEIQ_ASYNC_OUTCOMES is on, but the background worker reads outcomes from "
                "PostgreSQL. Set NUDGEIQ_DATABASE_URL, or turn NUDGEIQ_ASYNC_OUTCOMES off."
            )

    service = CampaignService(store)
    dispatcher: ScheduledDispatcher | None = None
    if scheduled_sends_enabled():
        # Fail at start-up, not at the first decision: only PostgreSQL is shared with the worker.
        if not isinstance(store, PostgresRepository):
            raise ValueError(
                "NUDGEIQ_SCHEDULED_SENDS is on, but the background worker reads the schedule from "
                "PostgreSQL. Set NUDGEIQ_DATABASE_URL, or turn NUDGEIQ_SCHEDULED_SENDS off."
            )
        dispatcher = ScheduledDispatcher(store)
    runtime = CampaignRuntime(store, service, dispatcher=dispatcher)
    runtime.reload()  # build the live campaign now so a broken one fails at start-up

    app = FastAPI(title="NudgeIQ", version="0.5.0")
    app.state.runtime = runtime
    app.state.service = service
    app.state.llm = llm_client
    install_hardening(app)
    app_log.info(
        "NudgeIQ started: storage=%s, model client=%s, outcomes=%s",
        type(store).__name__,
        type(llm_client).__name__,
        "background" if async_outcomes else "immediate",
    )

    @app.exception_handler(CustomerNotFoundError)
    async def _no_customer(_: Request, exc: CustomerNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": f"customer not found: {exc}"})

    @app.exception_handler(NoActiveCampaignError)
    async def _no_active(_: Request, exc: NoActiveCampaignError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "no campaign is active"})

    @app.exception_handler(CampaignNotFoundError)
    async def _no_campaign(_: Request, exc: CampaignNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": f"campaign not found: {exc}"})

    install_campaign_error_handlers(app)
    app.include_router(create_campaign_router(service, runtime, llm_client))
    app.include_router(create_variant_run_router(service, llm_client))
    app.include_router(create_demo_router(runtime, store))

    @app.get("/health")
    def health() -> dict[str, Any]:
        # The server is up even when no campaign is live; the status says which case it is.
        if not runtime.is_ready:
            return {"status": "no_active_campaign"}
        return {
            "status": "ok",
            "campaign_version": runtime.active_version,
            **runtime.current().policy_summary(),
        }

    # ---- campaigns: reading is here; create, edit, activate etc. are in app/api/campaigns.py
    @app.get("/campaigns", response_model=list[CampaignSummary])
    def campaigns() -> list[CampaignSummary]:
        return [CampaignSummary.of(c) for c in service.list_campaigns()]

    @app.get("/campaigns/{uid}", response_model=UseCaseConfig)
    def campaign(uid: str) -> UseCaseConfig:
        return service.get(uid).config  # unknown id -> 404 via the handler above

    # ---- customers (stand-in for the real customer source)
    @app.post("/customers", response_model=CustomerState)
    def upsert_customer(customer: CustomerState) -> CustomerState:
        store.save_customer(customer)
        store.flush()
        return customer

    @app.get("/customers", response_model=list[CustomerState])
    def customers(limit: int = 50) -> list[CustomerState]:
        return store.list_customers(limit=max(1, min(limit, 500)))

    @app.get("/customers/{customer_id}", response_model=CustomerState)
    def customer(customer_id: str) -> CustomerState:
        found = store.get_customer(customer_id)
        if found is None:
            raise HTTPException(404, f"customer not found: {customer_id}")
        return found

    @app.post("/demo/seed")
    def seed(req: SeedRequest) -> dict[str, Any]:
        people = generate_customers(req.count, runtime.current().config, seed=req.seed)
        for person in people:
            store.save_customer(person)
        store.flush()
        return {"created": len(people), "first_id": people[0].customer_id}

    # ---- the flows
    @app.post("/events", response_model=Decision)
    def events(event: TriggerEvent) -> Decision:
        try:
            decision = runtime.current().handle_event(event)
        except InvalidEventError as exc:
            raise HTTPException(422, str(exc)) from None
        store.flush()
        return decision

    @app.post("/decide", response_model=Decision)
    def decide(req: DecideRequest) -> Decision:
        decision = runtime.current().decide(req.customer_id, req.as_of)
        store.flush()
        return decision

    @app.post("/outcomes", response_model=OutcomeResult | OutcomeAccepted)
    def outcomes(
        outcome: Outcome,
        response: Response,
        idempotency_key: str | None = Header(default=None, max_length=200),
    ) -> OutcomeResult | OutcomeAccepted:
        """Immediate mode (default): reward, learning and journey are done before the answer.
        Background mode (NUDGEIQ_ASYNC_OUTCOMES on): the outcome is stored as 'pending', a worker
        is asked to process it, and the answer is 202. Send an `Idempotency-Key` header so a
        retried request is stored once."""
        try:
            # the campaign that made the decision handles its outcome, even after a swap
            orch = runtime.orchestrator_for_decision(outcome.decision_id)
            if not async_outcomes:
                result = orch.record_outcome(outcome)
                store.flush()
                return result
            saved = orch.accept_outcome(outcome, idempotency_key)
        except DecisionNotFoundError as exc:
            raise HTTPException(404, f"decision not found: {exc}") from None
        except OutcomeMismatchError as exc:
            raise HTTPException(422, str(exc)) from None
        except OutcomeKeyConflict as exc:
            raise HTTPException(409, str(exc)) from None
        store.flush()
        record = saved.record
        queued = False
        if record.status == "pending":  # also for a repeated key: the first queueing may be lost
            from app.worker import tasks  # only needed (and imported) in background mode

            queued = tasks.enqueue_outcome(record.outcome_id)
        response.status_code = 202
        return OutcomeAccepted(
            outcome_id=record.outcome_id,
            decision_id=outcome.decision_id,
            status=record.status,
            queued=queued,
            duplicate=not saved.created,
        )

    # ---- reading results
    @app.get("/decisions", response_model=list[Decision])
    def decisions(customer_id: str | None = None, limit: int = 50) -> list[Decision]:
        return store.list_decisions(customer_id=customer_id, limit=max(1, min(limit, 500)))

    @app.get("/decisions/{decision_id}", response_model=DecisionDetail)
    def decision_detail(decision_id: str) -> DecisionDetail:
        found = store.get_decision(decision_id)
        if found is None:
            raise HTTPException(404, f"decision not found: {decision_id}")
        return DecisionDetail(
            decision=found,
            outcomes=store.list_outcomes(decision_id=decision_id),
            reward=store.get_reward(decision_id),
        )

    @app.get("/decisions/{decision_id}/trace", response_model=DecisionTrace)
    def decision_trace(decision_id: str) -> DecisionTrace:
        """The graph engine's per-node trace of one decision (timing, outcome, keys, notes).

        Held in memory: empty with the classic engine, after a restart, or once the decision is
        among the oldest. It is not part of the Decision record, whose `steps` are the same
        with either engine.
        """
        if store.get_decision(decision_id) is None:
            raise HTTPException(404, f"decision not found: {decision_id}")
        return DecisionTrace(
            decision_id=decision_id, engine=runtime.engine, entries=runtime.trace.get(decision_id)
        )

    @app.get("/scheduled-sends", response_model=list[ScheduledSend])
    def scheduled_sends(status: SendStatus | None = None, limit: int = 50) -> list[ScheduledSend]:
        """Nudges waiting for their send time, and what happened to the ones that were due."""
        return store.list_scheduled_sends(status=status, limit=max(1, min(limit, 500)))

    @app.get("/scheduled-sends/{decision_id}", response_model=ScheduledSend)
    def scheduled_send(decision_id: str) -> ScheduledSend:
        found = store.get_scheduled_send(decision_id)
        if found is None:
            raise HTTPException(404, f"no scheduled send for decision: {decision_id}")
        return found

    def _check_campaign(campaign_id: str | None, campaign_version: int | None) -> None:
        if campaign_version is not None and campaign_id is None:
            raise HTTPException(422, "campaign_version needs campaign_id")
        if campaign_id is not None and store.get_campaign(campaign_id) is None:
            raise HTTPException(404, f"campaign not found: {campaign_id}")

    @app.get("/insights/uplift", response_model=UpliftSummary)
    def uplift(campaign_id: str | None = None) -> UpliftSummary:
        _check_campaign(campaign_id, None)
        return uplift_summary(store, campaign_id=campaign_id)

    @app.get("/insights/uplift/by/{dimension}", response_model=UpliftReport)
    def uplift_breakdown(
        dimension: Dimension,
        campaign_id: str | None = None,
        campaign_version: int | None = Query(default=None, ge=1),
        min_resolved: int = Query(default=DEFAULT_MIN_RESOLVED, ge=1),
    ) -> UpliftReport:
        _check_campaign(campaign_id, campaign_version)
        return uplift_by(
            store,
            dimension,
            campaign_id=campaign_id,
            campaign_version=campaign_version,
            min_resolved=min_resolved,
        )

    @app.get("/insights/messages", response_model=list[MessageFunnelRow])
    def insights_messages(campaign_id: str | None = None) -> list[MessageFunnelRow]:
        _check_campaign(campaign_id, None)
        return message_funnel(store, campaign_id=campaign_id)

    @app.get("/insights/guardrails", response_model=GuardrailSummary)
    def insights_guardrails(campaign_id: str | None = None) -> GuardrailSummary:
        _check_campaign(campaign_id, None)
        return guardrail_summary(store, campaign_id=campaign_id)

    return app


app = create_app()
