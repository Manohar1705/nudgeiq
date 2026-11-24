"""Message templates and the LLM/human-approved variants built from them."""

import re
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import Channel, VariantSource, VariantStatus

_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _now() -> datetime:
    return datetime.now(UTC)


class MessageTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str
    name: str
    channel: Channel
    body: str
    # offers this template can carry; empty means it fits any offer
    offers: list[str] = Field(default_factory=list)

    @property
    def variables(self) -> list[str]:
        return sorted(set(_VAR.findall(self.body)))


class MessageVariant(BaseModel):
    """One tone/persuasion variant. Only APPROVED ones may be sent."""

    model_config = ConfigDict(extra="forbid")

    variant_id: str
    template_id: str
    text: str
    principle: str  # e.g. "social_proof", "authority"
    tone: str  # e.g. "warm", "authoritative"
    status: VariantStatus = VariantStatus.PENDING
    source: VariantSource = VariantSource.HUMAN
    generated_by: str | None = None  # model name when source is LLM
    created_at: datetime = Field(default_factory=_now)
    reviewed_at: datetime | None = None  # when a person approved or rejected it
    review_note: str | None = None  # e.g. why it was rejected

    @property
    def variables(self) -> list[str]:
        return sorted(set(_VAR.findall(self.text)))

    def render(self, values: dict[str, str]) -> str:
        def fill(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in values:
                raise KeyError(f"missing template variable: {name}")
            return values[name]

        return _VAR.sub(fill, self.text)


class TemplateLibrary(BaseModel):
    """All templates and variants for one use case."""

    model_config = ConfigDict(extra="forbid")

    templates: list[MessageTemplate] = Field(default_factory=list)
    variants: list[MessageVariant] = Field(default_factory=list)

    @model_validator(mode="after")
    def _links(self) -> "TemplateLibrary":
        template_ids = [t.template_id for t in self.templates]
        if len(set(template_ids)) != len(template_ids):
            raise ValueError("duplicate template_id")
        variant_ids = [v.variant_id for v in self.variants]
        if len(set(variant_ids)) != len(variant_ids):
            raise ValueError("duplicate variant_id")
        for v in self.variants:
            if v.template_id not in template_ids:
                raise ValueError(f"variant {v.variant_id} points to unknown template")
        return self
