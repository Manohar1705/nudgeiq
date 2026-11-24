"""Variant generation as a paused run (step 8e-3). Additive: the plain endpoints are unchanged.

  POST /campaigns/{id}/variant-runs                      start: generate, check, save as PENDING,
                                                         then wait for a person
  GET  /campaigns/{id}/variant-runs/{run_id}             where the run is, with the variants
  POST /campaigns/{id}/variant-runs/{run_id}/review      resume with approve / reject decisions

Status codes follow the plain endpoints: 404 unknown campaign, template, or run; 409 the campaign
is not a draft, or the run is not waiting; 422 the request is wrong (including a variant that is
not part of the run); 502 the model failed; 501 the langgraph package is not installed; 500 a node
failed for a reason the request cannot fix (the message names the node and the error type, never
a traceback; the traceback is in the server log).

A run that is not reviewed is harmless: its variants stay PENDING and can still be approved or
rejected one by one with the ordinary endpoints.
"""

import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.campaigns import GenerateRequest
from app.campaigns import CampaignError, CampaignService
from app.graph.variant_runs import (
    ReviewDecision,
    RunFailedError,
    RunNotFoundError,
    RunView,
    VariantRunService,
)
from app.llm import LLMClient


class RunReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # An empty list ends the run and leaves every variant pending.
    decisions: list[ReviewDecision] = Field(max_length=50)


def create_variant_run_router(service: CampaignService, llm: LLMClient) -> APIRouter:
    router = APIRouter(prefix="/campaigns", tags=["variant-runs"])
    holder: list[VariantRunService] = []
    mu = threading.Lock()

    def runs() -> VariantRunService:
        # Built on first use, so a server without langgraph still starts and serves the rest.
        with mu:
            if not holder:
                try:
                    holder.append(VariantRunService(service, llm))
                except ImportError:
                    raise HTTPException(
                        501, "variant runs need the langgraph package (pip install langgraph)"
                    ) from None
            return holder[0]

    @router.post("/{campaign_id}/variant-runs", response_model=RunView, status_code=201)
    def start(campaign_id: str, body: GenerateRequest) -> RunView:
        try:
            return runs().start(
                campaign_id,
                body.template_id,
                count=body.count,
                principles=body.principles,
                tones=body.tones,
            )
        except RunFailedError as exc:
            raise HTTPException(500, str(exc)) from None

    @router.get("/{campaign_id}/variant-runs/{run_id}", response_model=RunView)
    def get(campaign_id: str, run_id: str) -> RunView:
        try:
            return runs().get(run_id, campaign_id)
        except RunNotFoundError:
            raise HTTPException(404, f"run not found: {run_id}") from None

    @router.post("/{campaign_id}/variant-runs/{run_id}/review", response_model=RunView)
    def review(campaign_id: str, run_id: str, body: RunReviewRequest) -> RunView:
        try:
            return runs().review(run_id, body.decisions, campaign_id)
        except RunNotFoundError:
            raise HTTPException(404, f"run not found: {run_id}") from None
        except RunFailedError as exc:
            raise HTTPException(500, str(exc)) from None
        except CampaignError:
            raise  # e.g. "run is not waiting" (409): the app's error handlers answer these
        except ValueError as exc:  # a variant not in this run, or named twice
            raise HTTPException(422, str(exc)) from None

    return router
