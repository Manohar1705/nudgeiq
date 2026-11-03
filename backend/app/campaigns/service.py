"""Campaign service: the rules for creating, editing, cloning, activating and archiving.

The repository only stores. The validator only checks. This class decides what is allowed:

  * Only a DRAFT can be edited (its settings, templates and variants). An active campaign is
    what customers are living with, so it is never changed in place: clone it, edit the copy,
    then activate the copy.
  * A draft may be saved while it is still wrong. Activation is the gate: it needs a
    validation report with no errors (warnings are fine).
  * Only one campaign is ACTIVE at a time. Activating a second one is refused unless the
    caller says to replace the current one.
  * ARCHIVED is final. To reuse an archived campaign, clone it.
  * `version` goes up on every saved edit of the settings, as the Campaign model documents.
    Because only drafts can be edited, the version of an active campaign is the version of
    the exact settings that went live. Activating, archiving and editing templates or
    variants do not change it.

Review of variants (5B-8a): a variant becomes sendable only when a person approves it.
Approving or rejecting works on a draft only, like every other edit. Approval is refused
while the variant would fail validation as an approved one. A variant written by an LLM can
only become approved through `approve_variant`, never by saving it with status "approved".

Every change is flushed so the JSON-file repository writes it to disk.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.campaigns.validation import (
    Severity,
    ValidationIssue,
    ValidationReport,
    validate_campaign,
    variant_approval_issues,
)
from app.models import (
    Campaign,
    CampaignStatus,
    MessageTemplate,
    MessageVariant,
    TemplateLibrary,
    UseCaseConfig,
    VariantSource,
    VariantStatus,
)
from app.repository.base import Repository


class CampaignError(Exception):
    """Base class, so a caller can catch every campaign rule failure at once."""


class CampaignNotFoundError(CampaignError, LookupError):
    pass


class CampaignExistsError(CampaignError, ValueError):
    pass


class CampaignIdMismatchError(CampaignError, ValueError):
    """A campaign's id is fixed. Its config must keep the same use_case_id."""


class CampaignStateError(CampaignError, ValueError):
    """The campaign's current status does not allow this action."""


class ActiveCampaignExistsError(CampaignStateError):
    def __init__(self, active_id: str) -> None:
        super().__init__(
            f"campaign '{active_id}' is already active; archive it first "
            "or activate with replace_active=True"
        )
        self.active_id = active_id


class ActivationBlockedError(CampaignError, ValueError):
    """Validation found errors. The report says exactly what to fix."""

    def __init__(self, campaign_id: str, report: ValidationReport) -> None:
        count = len(report.errors)
        super().__init__(
            f"campaign '{campaign_id}' cannot be activated: {count} validation "
            f"error{'s' if count != 1 else ''}"
        )
        self.report = report


class VariantNotFoundError(CampaignError, LookupError):
    pass


class VariantStateError(CampaignStateError):
    """The variant's current status does not allow this review step."""


class VariantApprovalBlockedError(CampaignError, ValueError):
    """Approval refused: the variant would fail validation as an approved one."""

    def __init__(self, campaign_id: str, variant_id: str, issues: list[ValidationIssue]) -> None:
        self.campaign_id = campaign_id
        self.variant_id = variant_id
        self.issues = issues
        errors = [i for i in issues if i.severity == Severity.ERROR]
        super().__init__(
            f"variant '{variant_id}' cannot be approved: "
            + "; ".join(f"{i.code} ({i.message})" for i in errors)
        )


class ReviewRequiredError(CampaignError, ValueError):
    """An LLM-written variant tried to skip review (or lose its LLM label)."""


@dataclass(frozen=True)
class VariantReview:
    variant: MessageVariant
    warnings: list[ValidationIssue] = field(default_factory=list)


class CampaignService:
    def __init__(self, repo: Repository, clock: Callable[[], datetime] | None = None) -> None:
        self.repo = repo
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ reading
    def get(self, campaign_id: str) -> Campaign:
        campaign = self.repo.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(campaign_id)
        return campaign

    def list_campaigns(self, status: CampaignStatus | None = None) -> list[Campaign]:
        return self.repo.list_campaigns(status)

    def get_active(self) -> Campaign | None:
        """The one live campaign, or None. More than one means the data is wrong, so it raises."""
        active = self.repo.list_campaigns(CampaignStatus.ACTIVE)
        if len(active) > 1:
            ids = ", ".join(c.campaign_id for c in active)
            raise CampaignStateError(f"more than one active campaign: {ids}")
        return active[0] if active else None

    def get_library(self, campaign_id: str) -> TemplateLibrary:
        self.get(campaign_id)
        return TemplateLibrary(
            templates=self.repo.list_templates(campaign_id),
            variants=self.repo.list_variants(campaign_id),
        )

    def validate(self, campaign_id: str) -> ValidationReport:
        """The full report for a campaign as it is stored now. Works in any status."""
        campaign = self.get(campaign_id)
        return validate_campaign(campaign.config, self.get_library(campaign_id))

    # ------------------------------------------------------------------ drafts
    def create_draft(self, config: UseCaseConfig) -> Campaign:
        """Store a new draft (version 1). It may have validation errors; activation checks them."""
        if self.repo.get_campaign(config.use_case_id) is not None:
            raise CampaignExistsError(config.use_case_id)
        now = self._clock()
        campaign = Campaign(
            campaign_id=config.use_case_id,
            status=CampaignStatus.DRAFT,
            version=1,
            config=config,
            created_at=now,
            updated_at=now,
        )
        self._save(campaign)
        return campaign

    def update_config(self, campaign_id: str, config: UseCaseConfig) -> Campaign:
        """Replace a draft's settings and bump its version."""
        campaign = self._editable(campaign_id)
        if config.use_case_id != campaign_id:
            raise CampaignIdMismatchError(
                f"config.use_case_id '{config.use_case_id}' must stay '{campaign_id}'"
            )
        updated = campaign.model_copy(
            update={"config": config, "version": campaign.version + 1, "updated_at": self._clock()}
        )
        self._save(updated)
        return updated

    def save_template(self, campaign_id: str, template: MessageTemplate) -> None:
        """Add or replace a template on a draft. Does not change the version."""
        self._editable(campaign_id)
        self.repo.save_template(campaign_id, template)
        self.repo.flush()

    def save_variant(self, campaign_id: str, variant: MessageVariant) -> None:
        """Add or replace a variant on a draft. Raises KeyError if its template is missing.

        Raises ReviewRequiredError if an LLM-written variant would skip review: it cannot be
        saved as approved unless it is already approved with the same text, and it cannot be
        relabelled as human-written.
        """
        self._editable(campaign_id)
        self._guard_llm_review(campaign_id, variant)
        self.repo.save_variant(campaign_id, variant)
        self.repo.flush()

    # ------------------------------------------------------------------ review
    def approve_variant(
        self, campaign_id: str, variant_id: str, note: str | None = None
    ) -> VariantReview:
        """Approve a PENDING variant on a draft. Warnings are returned, errors block."""
        variant = self._reviewable(campaign_id, variant_id)
        if variant.status != VariantStatus.PENDING:
            raise VariantStateError(
                f"variant '{variant_id}' is {variant.status.value}; only a pending variant "
                "can be approved"
            )
        template = self.repo.get_template(campaign_id, variant.template_id)
        if template is None:
            raise VariantStateError(f"variant '{variant_id}' points to a missing template")
        issues = variant_approval_issues(variant, template)
        if any(i.severity == Severity.ERROR for i in issues):
            raise VariantApprovalBlockedError(campaign_id, variant_id, issues)
        saved = self._review(campaign_id, variant, VariantStatus.APPROVED, note)
        return VariantReview(saved, [i for i in issues if i.severity == Severity.WARNING])

    def reject_variant(
        self, campaign_id: str, variant_id: str, note: str | None = None
    ) -> VariantReview:
        """Reject a PENDING variant, or withdraw an APPROVED one, on a draft."""
        variant = self._reviewable(campaign_id, variant_id)
        if variant.status == VariantStatus.REJECTED:
            raise VariantStateError(f"variant '{variant_id}' is already rejected")
        return VariantReview(self._review(campaign_id, variant, VariantStatus.REJECTED, note))

    def clone(self, source_id: str, new_id: str, name: str | None = None) -> Campaign:
        """Copy any campaign, with its templates and variants, into a new draft.

        Variants keep their status, so a clone of a live campaign is ready to activate once
        edited. The learned policy is not copied: the new campaign starts learning fresh.
        """
        source = self.get(source_id)
        if self.repo.get_campaign(new_id) is not None:
            raise CampaignExistsError(new_id)
        data: dict[str, Any] = source.config.model_dump(mode="json")
        data["use_case_id"] = new_id
        data["name"] = name if name is not None else f"{source.config.name} (copy)"
        config = UseCaseConfig.model_validate(data)

        now = self._clock()
        self.repo.save_campaign(
            Campaign(
                campaign_id=new_id,
                status=CampaignStatus.DRAFT,
                version=1,
                config=config,
                created_at=now,
                updated_at=now,
            )
        )
        for template in self.repo.list_templates(source_id):
            self.repo.save_template(new_id, template)
        for variant in self.repo.list_variants(source_id):
            self.repo.save_variant(new_id, variant)
        self.repo.flush()
        return self.get(new_id)

    # ------------------------------------------------------------------ lifecycle
    def activate(self, campaign_id: str, *, replace_active: bool = False) -> Campaign:
        """Make a draft the live campaign.

        Order of checks: the status allows it, validation has no errors, then the one-active
        rule. Nothing is changed until all of them pass, so a refused activation leaves the
        current live campaign alone. When replacing, the old campaign is archived first, so a
        crash in between leaves no active campaign rather than two.
        """
        campaign = self.get(campaign_id)
        self._require_transition(campaign, CampaignStatus.ACTIVE)

        report = validate_campaign(campaign.config, self.get_library(campaign_id))
        if not report.can_activate:
            raise ActivationBlockedError(campaign_id, report)

        active = self.repo.list_campaigns(CampaignStatus.ACTIVE)
        others = [c for c in active if c.campaign_id != campaign_id]
        if others and not replace_active:
            raise ActiveCampaignExistsError(others[0].campaign_id)

        now = self._clock()
        for old in others:
            self._save(
                old.model_copy(
                    update={
                        "status": CampaignStatus.ARCHIVED,
                        "archived_at": now,
                        "updated_at": now,
                    }
                )
            )
        live = campaign.model_copy(
            update={"status": CampaignStatus.ACTIVE, "activated_at": now, "updated_at": now}
        )
        self._save(live)
        return live

    def archive(self, campaign_id: str) -> Campaign:
        """Retire a draft or an active campaign. Archived is final; clone to reuse."""
        campaign = self.get(campaign_id)
        self._require_transition(campaign, CampaignStatus.ARCHIVED)
        now = self._clock()
        archived = campaign.model_copy(
            update={"status": CampaignStatus.ARCHIVED, "archived_at": now, "updated_at": now}
        )
        self._save(archived)
        return archived

    # ------------------------------------------------------------------ helpers
    def _reviewable(self, campaign_id: str, variant_id: str) -> MessageVariant:
        self._editable(campaign_id)
        variant = self.repo.get_variant(campaign_id, variant_id)
        if variant is None:
            raise VariantNotFoundError(variant_id)
        return variant

    def _review(
        self,
        campaign_id: str,
        variant: MessageVariant,
        status: VariantStatus,
        note: str | None,
    ) -> MessageVariant:
        reviewed = variant.model_copy(
            update={
                "status": status,
                "reviewed_at": self._clock(),
                "review_note": (note or "").strip() or None,
            }
        )
        self.repo.save_variant(campaign_id, reviewed)
        self.repo.flush()
        return reviewed

    def _guard_llm_review(self, campaign_id: str, variant: MessageVariant) -> None:
        stored = self.repo.get_variant(campaign_id, variant.variant_id)
        if (
            stored is not None
            and stored.source == VariantSource.LLM
            and variant.source != VariantSource.LLM
        ):
            raise ReviewRequiredError(
                f"variant '{variant.variant_id}' was written by an LLM and cannot be "
                "relabelled as human-written"
            )
        if variant.source == VariantSource.LLM and variant.status == VariantStatus.APPROVED:
            unchanged = (
                stored is not None
                and stored.source == VariantSource.LLM
                and stored.status == VariantStatus.APPROVED
                and stored.text == variant.text
                and stored.template_id == variant.template_id
            )
            if not unchanged:
                raise ReviewRequiredError(
                    f"variant '{variant.variant_id}' was written by an LLM, so it has to be "
                    "approved with the approve endpoint, not saved as approved"
                )

    def _editable(self, campaign_id: str) -> Campaign:
        campaign = self.get(campaign_id)
        if not campaign.is_editable:
            raise CampaignStateError(
                f"campaign '{campaign_id}' is {campaign.status.value}; only a draft can be "
                "edited (clone it to make changes)"
            )
        return campaign

    @staticmethod
    def _require_transition(campaign: Campaign, target: CampaignStatus) -> None:
        if not campaign.can_become(target):
            raise CampaignStateError(
                f"campaign '{campaign.campaign_id}' is {campaign.status.value} "
                f"and cannot become {target.value}"
            )

    def _save(self, campaign: Campaign) -> None:
        self.repo.save_campaign(campaign)
        self.repo.flush()
