"""Demo-only route: the fake customer reacts to a decision (POST /demo/respond).

It samples outcomes from the simulator's hidden response model and records them through the
real Orchestrator.record_outcome, so rewards, learning and uplift numbers behave as in
production. A decision that was held out only gets the natural (no-nudge) chance of ordering.
A decision that waited (and is not a holdout) has nothing to react to and records nothing.
"""

import random
from datetime import timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from app.api.runtime import CampaignRuntime
from app.models import Action, Outcome, OutcomeType, RewardRecord
from app.orchestrator import DecisionNotFoundError
from app.repository.base import Repository
from app.simulator.responses import ResponseModel


class RespondRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: str


class RespondResult(BaseModel):
    decision_id: str
    nudged: bool  # False when the decision was "wait" or a holdout
    outcomes: list[OutcomeType]
    reward: RewardRecord | None
    policy_updated: bool
    next_step: str | None


def create_demo_router(runtime: CampaignRuntime, store: Repository) -> APIRouter:
    router = APIRouter(prefix="/demo", tags=["demo"])
    model = ResponseModel()

    @router.post("/respond", response_model=RespondResult)
    def respond(req: RespondRequest) -> RespondResult:
        try:
            orch = runtime.orchestrator_for_decision(req.decision_id)
        except DecisionNotFoundError:
            raise HTTPException(404, f"decision not found: {req.decision_id}") from None
        decision = store.get_decision(req.decision_id)
        if decision is None:
            raise HTTPException(404, f"decision not found: {req.decision_id}")
        if store.list_outcomes(decision_id=decision.decision_id):
            raise HTTPException(409, "this decision already has outcomes")
        # A holdout decision is stored as "wait", so check it first: the control group must still
        # resolve (natural orders only), or uplift would have nothing to compare against.
        nudged = not decision.is_holdout and not decision.chosen.is_wait
        if not nudged and not decision.is_holdout:
            return RespondResult(
                decision_id=decision.decision_id,
                nudged=False,
                outcomes=[],
                reward=None,
                policy_updated=False,
                next_step=None,
            )
        action = decision.chosen if nudged else Action.wait()  # holdout: never messaged
        rng = random.Random(decision.decision_id)  # same decision, same reaction
        happened = model.sample_outcomes(decision.state, action, rng)
        at = (decision.scheduled_for or decision.created_at) + timedelta(hours=1)
        result = None
        for kind in happened:
            result = orch.record_outcome(
                Outcome(
                    decision_id=decision.decision_id,
                    customer_id=decision.customer_id,
                    outcome_type=kind,
                    occurred_at=at,
                )
            )
        if not happened:  # silence = timeout
            happened = [OutcomeType.IGNORED]
            result = orch.record_outcome(
                Outcome(
                    decision_id=decision.decision_id,
                    customer_id=decision.customer_id,
                    outcome_type=OutcomeType.IGNORED,
                    occurred_at=at + timedelta(hours=48),
                )
            )
        store.flush()
        assert result is not None
        return RespondResult(
            decision_id=decision.decision_id,
            nudged=nudged,
            outcomes=happened,
            reward=result.reward,
            policy_updated=result.policy_updated,
            next_step=result.next_step,
        )

    return router
