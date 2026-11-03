"""A campaign is a stored use case: the config plus its lifecycle state."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.config import UseCaseConfig
from app.models.enums import CampaignStatus

# Where a campaign may go next. Archived is final; to reuse one, clone it as a new draft.
ALLOWED_TRANSITIONS: dict[CampaignStatus, frozenset[CampaignStatus]] = {
    CampaignStatus.DRAFT: frozenset({CampaignStatus.ACTIVE, CampaignStatus.ARCHIVED}),
    CampaignStatus.ACTIVE: frozenset({CampaignStatus.ARCHIVED}),
    CampaignStatus.ARCHIVED: frozenset(),
}


def _now() -> datetime:
    return datetime.now(UTC)


class Campaign(BaseModel):
    """One use-case config with a status and a version.

    `campaign_id` is the same string as `config.use_case_id`, so the learned policy,
    the decisions and the templates all key on one id.
    """

    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    status: CampaignStatus = CampaignStatus.DRAFT
    version: int = Field(default=1, ge=1)  # bumped by the service on every saved edit
    config: UseCaseConfig
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    activated_at: datetime | None = None
    archived_at: datetime | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_editable(self) -> bool:
        """Only drafts can be edited in place."""
        return self.status == CampaignStatus.DRAFT

    def can_become(self, target: CampaignStatus) -> bool:
        return target in ALLOWED_TRANSITIONS[self.status]

    @model_validator(mode="after")
    def _consistent(self) -> "Campaign":
        if self.campaign_id != self.config.use_case_id:
            raise ValueError("campaign_id must equal config.use_case_id")
        if self.status == CampaignStatus.ACTIVE and self.activated_at is None:
            raise ValueError("an active campaign needs activated_at")
        if self.status == CampaignStatus.ARCHIVED and self.archived_at is None:
            raise ValueError("an archived campaign needs archived_at")
        if self.status != CampaignStatus.ARCHIVED and self.archived_at is not None:
            raise ValueError("only an archived campaign can have archived_at")
        return self
