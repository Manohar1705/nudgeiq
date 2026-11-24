"""Campaign management endpoints. The rules live in CampaignService; this only translates.

  POST /campaigns                              create a draft
  PUT  /campaigns/{id}                         replace a draft's settings
  POST /campaigns/{id}/clone                   copy any campaign into a new draft
  POST /campaigns/{id}/activate                make a draft live (optionally replacing the live one)
  POST /campaigns/{id}/archive                 retire a draft or the live campaign
  GET  /campaigns/{id}/validation              full validation report for the stored campaign
  POST /campaigns/validate                     dry run on raw JSON, nothing is saved
  GET  /campaigns/{id}/templates               list;  PUT .../templates/{template_id}  save (draft)
  GET  /campaigns/{id}/variants                list;  PUT .../variants/{variant_id}    save (draft)
  POST /campaigns/{id}/variants/generate               draft PENDING variants with the LLM (draft)
  POST /campaigns/{id}/variants/{variant_id}/approve   approve a pending variant (draft)
  POST /campaigns/{id}/variants/{variant_id}/reject    reject a pending one or withdraw an
                                                       approved one (draft)

The existing read endpoints (GET /campaigns and GET /campaigns/{id}) stay in main.py.

A draft with validation errors is saved normally: the response carries the report, and only
activation is blocked by it. Activate and archive go through the runtime, so the server starts
serving the new live campaign immediately.

Status codes: 404 unknown id, 409 the campaign's status or an existing id does not allow it,
422 the request itself is wrong, or activation or approval was blocked (the issues are in the body).
"""

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.runtime import CampaignRuntime
from app.campaigns import (
    DEFAULT_COUNT,
    MAX_COUNT,
    ActivationBlockedError,
    CampaignExistsError,
    CampaignIdMismatchError,
    CampaignService,
    CampaignStateError,
    GenerationResult,
    ReviewRequiredError,
    TemplateNotFoundError,
    ValidationIssue,
    ValidationReport,
    VariantApprovalBlockedError,
    VariantGenerator,
    VariantNotFoundError,
    VariantReview,
    validate_draft,
)
from app.llm import LLMClient, LLMError
from app.models import (
    Campaign,
    MessageTemplate,
    MessageVariant,
    UseCaseConfig,
    VariantStatus,
)


class ValidationOut(BaseModel):
    """A validation report in the shape a dashboard wants, with the counts spelled out."""

    can_activate: bool
    error_count: int
    warning_count: int
    issues: list[ValidationIssue]

    @classmethod
    def of(cls, report: ValidationReport) -> "ValidationOut":
        return cls(
            can_activate=report.can_activate,
            error_count=len(report.errors),
            warning_count=len(report.warnings),
            issues=report.issues,
        )


class CampaignDetail(BaseModel):
    campaign: Campaign
    validation: ValidationOut


class CloneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_id: str = Field(min_length=1)
    name: str | None = None


class ActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replace_active: bool = False


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=500)


class VariantReviewOut(BaseModel):
    variant: MessageVariant
    warnings: list[ValidationIssue]  # advice shown at approval time; empty for a rejection

    @classmethod
    def of(cls, review: VariantReview) -> "VariantReviewOut":
        return cls(variant=review.variant, warnings=review.warnings)


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: str = Field(min_length=1)
    count: int = Field(default=DEFAULT_COUNT, ge=1, le=MAX_COUNT)
    principles: list[str] = Field(default_factory=list, max_length=5)
    tones: list[str] = Field(default_factory=list, max_length=5)


class DryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any]
    library: dict[str, Any] | None = None


def install_campaign_error_handlers(app: FastAPI) -> None:
    """Turn the service's rule failures into HTTP responses.

    404 (unknown campaign) and 503 (nothing live) are handled in main.py.
    """

    @app.exception_handler(CampaignExistsError)
    async def _exists(_: Request, exc: CampaignExistsError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": f"campaign already exists: {exc}"})

    @app.exception_handler(CampaignStateError)
    async def _state(_: Request, exc: CampaignStateError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(CampaignIdMismatchError)
    async def _mismatch(_: Request, exc: CampaignIdMismatchError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(TemplateNotFoundError)
    async def _no_template(_: Request, exc: TemplateNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": f"template not found: {exc}"})

    @app.exception_handler(LLMError)
    async def _llm_failed(_: Request, exc: LLMError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": f"the model failed: {exc}"})

    @app.exception_handler(VariantNotFoundError)
    async def _no_variant(_: Request, exc: VariantNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": f"variant not found: {exc}"})

    @app.exception_handler(ReviewRequiredError)
    async def _review_required(_: Request, exc: ReviewRequiredError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(VariantApprovalBlockedError)
    async def _approval_blocked(_: Request, exc: VariantApprovalBlockedError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": str(exc),
                "issues": [i.model_dump(mode="json") for i in exc.issues],
            },
        )

    @app.exception_handler(ActivationBlockedError)
    async def _blocked(_: Request, exc: ActivationBlockedError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": str(exc),
                "validation": ValidationOut.of(exc.report).model_dump(mode="json"),
            },
        )


def create_campaign_router(
    service: CampaignService, runtime: CampaignRuntime, llm: LLMClient
) -> APIRouter:
    router = APIRouter(prefix="/campaigns", tags=["campaigns"])

    def detail(campaign: Campaign) -> CampaignDetail:
        report = service.validate(campaign.campaign_id)
        return CampaignDetail(campaign=campaign, validation=ValidationOut.of(report))

    # ---- campaigns ---------------------------------------------------------------------
    @router.post("", response_model=CampaignDetail, status_code=201)
    def create(config: UseCaseConfig) -> CampaignDetail:
        return detail(service.create_draft(config))

    @router.put("/{campaign_id}", response_model=CampaignDetail)
    def update(campaign_id: str, config: UseCaseConfig) -> CampaignDetail:
        return detail(service.update_config(campaign_id, config))

    @router.post("/{campaign_id}/clone", response_model=CampaignDetail, status_code=201)
    def clone(campaign_id: str, req: CloneRequest) -> CampaignDetail:
        return detail(service.clone(campaign_id, req.new_id, req.name))

    @router.post("/{campaign_id}/activate", response_model=Campaign)
    def activate(campaign_id: str, req: ActivateRequest | None = None) -> Campaign:
        replace = req.replace_active if req else False
        return runtime.activate(campaign_id, replace_active=replace)

    @router.post("/{campaign_id}/archive", response_model=Campaign)
    def archive(campaign_id: str) -> Campaign:
        return runtime.archive(campaign_id)

    # ---- validation --------------------------------------------------------------------
    @router.post("/validate", response_model=ValidationOut)
    def dry_run(req: DryRunRequest) -> ValidationOut:
        """Check raw JSON as a dashboard would send it. Nothing is stored."""
        return ValidationOut.of(validate_draft(req.config, req.library))

    @router.get("/{campaign_id}/validation", response_model=ValidationOut)
    def validation(campaign_id: str) -> ValidationOut:
        return ValidationOut.of(service.validate(campaign_id))

    # ---- templates ---------------------------------------------------------------------
    @router.get("/{campaign_id}/templates", response_model=list[MessageTemplate])
    def templates(campaign_id: str) -> list[MessageTemplate]:
        return service.get_library(campaign_id).templates

    @router.put("/{campaign_id}/templates/{template_id}", response_model=MessageTemplate)
    def save_template(
        campaign_id: str, template_id: str, template: MessageTemplate
    ) -> MessageTemplate:
        if template.template_id != template_id:
            raise HTTPException(422, "template_id in the body must match the URL")
        service.save_template(campaign_id, template)
        return template

    # ---- variants ----------------------------------------------------------------------
    @router.get("/{campaign_id}/variants", response_model=list[MessageVariant])
    def variants(
        campaign_id: str,
        template_id: str | None = None,
        status: VariantStatus | None = None,
    ) -> list[MessageVariant]:
        found = service.get_library(campaign_id).variants
        return [
            v
            for v in found
            if (template_id is None or v.template_id == template_id)
            and (status is None or v.status == status)
        ]

    @router.put("/{campaign_id}/variants/{variant_id}", response_model=MessageVariant)
    def save_variant(campaign_id: str, variant_id: str, variant: MessageVariant) -> MessageVariant:
        if variant.variant_id != variant_id:
            raise HTTPException(422, "variant_id in the body must match the URL")
        try:
            service.save_variant(campaign_id, variant)
        except KeyError as exc:
            raise HTTPException(422, str(exc.args[0])) from None
        return variant

    @router.post("/{campaign_id}/variants/generate", response_model=GenerationResult)
    def generate_variants(campaign_id: str, body: GenerateRequest) -> GenerationResult:
        return VariantGenerator(service, llm).generate(
            campaign_id,
            body.template_id,
            count=body.count,
            principles=body.principles,
            tones=body.tones,
        )

    @router.post("/{campaign_id}/variants/{variant_id}/approve", response_model=VariantReviewOut)
    def approve_variant(
        campaign_id: str, variant_id: str, body: ReviewRequest | None = None
    ) -> VariantReviewOut:
        note = body.note if body else None
        return VariantReviewOut.of(service.approve_variant(campaign_id, variant_id, note))

    @router.post("/{campaign_id}/variants/{variant_id}/reject", response_model=VariantReviewOut)
    def reject_variant(
        campaign_id: str, variant_id: str, body: ReviewRequest | None = None
    ) -> VariantReviewOut:
        note = body.note if body else None
        return VariantReviewOut.of(service.reject_variant(campaign_id, variant_id, note))

    return router
