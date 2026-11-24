"""Draft message variants with a language model (5B-8b).

The model proposes, the system disposes. Whatever comes back is parsed, cleaned and checked,
and every variant that survives is stored as PENDING with source "llm". Nothing here can make a
variant sendable: a person still has to approve it (5B-8a).

What the generator does for each reply:
  * reads the reply as JSON, tolerating code fences and chatter around it
  * drops any draft that would fail validation as an approved variant (unknown {{variable}},
    blank text, SMS without STOP), with the reason, instead of failing the whole batch
  * drops drafts that repeat an existing variant of the same template (any status, so a
    rejected wording does not come back) or an earlier draft in the same reply
  * ignores any status the model claims and keeps at most `count` drafts

Only a draft campaign can be changed, so a live or archived one is refused before the model is
called. The model is never called for an unknown campaign or template either.

Step 8e-3: `generate` is split into small steps (prepare, request_drafts, screen, save) so the
variant graph in app/graph/variant_builder.py can run them one node at a time and pause for a
person before anything is approved. `generate` still calls them in order, so the plain endpoint
and the graph share one set of rules.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.agents.message import KNOWN_VARIABLES
from app.campaigns.service import CampaignError, CampaignService, CampaignStateError
from app.campaigns.validation import Severity, variant_approval_issues
from app.llm import DraftBrief, LLMClient, LLMReplyError, LLMRequest
from app.models import (
    Channel,
    MessageTemplate,
    MessageVariant,
    VariantSource,
    VariantStatus,
)

MAX_COUNT = 5
DEFAULT_COUNT = 3
MAX_TEXT_CHARS = 1000
MAX_LABEL_CHARS = 40
MAX_AVOID_TEXTS = 12  # how many existing wordings are shown to the model
UNSPECIFIED = "unspecified"


class TemplateNotFoundError(CampaignError, LookupError):
    pass


class SkippedDraft(BaseModel):
    text: str  # shortened for display
    code: str
    message: str


class GenerationResult(BaseModel):
    model: str
    requested: int
    created: list[MessageVariant]
    skipped: list[SkippedDraft]


# ---------------------------------------------------------------------------- prompt
_SYSTEM = (
    "You write short marketing message variants for a retail abandoned-cart campaign. "
    "Reply with a JSON array and nothing else. Each element is an object with the keys "
    '"text", "principle" and "tone". Never invent facts, prices or deadlines.'
)


def clean_label(value: object) -> str:
    """Principles and tones are stored as lowercase snake_case labels."""
    text = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    return text[:MAX_LABEL_CHARS].strip("_")


def build_prompt(brief: DraftBrief) -> str:
    lines = [
        f"Write exactly {brief.count} alternative versions of the {brief.channel.value} "
        "message below. Keep its meaning, its format and its call to action.",
        "",
        "Rules:",
        "- Use only these placeholders: "
        + ", ".join("{{" + v + "}}" for v in sorted(KNOWN_VARIABLES))
        + ". Do not invent others.",
        "- Each version must read clearly different from the others.",
    ]
    if brief.channel == Channel.SMS:
        lines.append('- It is an SMS: keep it short and include the words "Reply STOP".')
    if brief.principles:
        lines.append("- Use these persuasion principles: " + ", ".join(brief.principles) + ".")
    if brief.tones:
        lines.append("- Use these tones: " + ", ".join(brief.tones) + ".")
    lines += ["", "Message to vary:", brief.body]
    if brief.avoid:
        lines += ["", "Wordings that already exist; do not repeat them:"]
        lines += [f"- {t}" for t in brief.avoid[-MAX_AVOID_TEXTS:]]
    lines += [
        "",
        'Reply as JSON: [{"text": "...", "principle": "social_proof", "tone": "warm"}, ...]',
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------- parsing
def parse_reply(raw: str) -> list[Any]:
    """The list of drafts in a model reply. Raises LLMReplyError if there is none."""
    text = raw.strip()
    fenced = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", text, flags=re.S)
    if fenced:
        text = fenced.group(1)
    candidates = [text]
    start, end = text.find("["), text.rfind("]")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("variants"), list):
            data = data["variants"]
        if isinstance(data, list):
            return data
    raise LLMReplyError("the model's reply was not a JSON list of variants")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _skip(text: object, code: str, message: str) -> SkippedDraft:
    shown = text if isinstance(text, str) else json.dumps(text, default=str)
    return SkippedDraft(text=shown[:200], code=code, message=message)


# ---------------------------------------------------------------------------- generator
@dataclass(frozen=True)
class DraftPlan:
    """Everything the later steps need, read once from the stored campaign."""

    campaign_id: str
    template: MessageTemplate
    existing: list[MessageVariant]  # variants of this template, any status
    all_variants: list[MessageVariant]  # every variant of the campaign (for new ids)
    brief: DraftBrief
    count: int


@dataclass(frozen=True)
class ScreenResult:
    """Drafts that passed the checks (not saved yet) and the ones dropped, with reasons."""

    created: list[MessageVariant]
    skipped: list[SkippedDraft]


class VariantGenerator:
    def __init__(self, service: CampaignService, client: LLMClient) -> None:
        self.service = service
        self.client = client

    # ---- the steps (each one is a node in the variant graph) ------------------------------
    def prepare(
        self,
        campaign_id: str,
        template_id: str,
        *,
        count: int = DEFAULT_COUNT,
        principles: list[str] | None = None,
        tones: list[str] | None = None,
    ) -> DraftPlan:
        """Check the request and read what is needed. Never calls the model."""
        if not 1 <= count <= MAX_COUNT:
            raise ValueError(f"count must be between 1 and {MAX_COUNT}")

        campaign = self.service.get(campaign_id)
        if not campaign.is_editable:
            raise CampaignStateError(
                f"campaign '{campaign_id}' is {campaign.status.value}; only a draft can be "
                "edited (clone it to make changes)"
            )
        library = self.service.get_library(campaign_id)
        template = next((t for t in library.templates if t.template_id == template_id), None)
        if template is None:
            raise TemplateNotFoundError(template_id)
        existing = [v for v in library.variants if v.template_id == template_id]

        brief = DraftBrief(
            template_id=template_id,
            channel=template.channel,
            body=template.body,
            count=count,
            principles=[p for p in map(clean_label, principles or []) if p],
            tones=[t for t in map(clean_label, tones or []) if t],
            avoid=[v.text for v in existing],
        )
        return DraftPlan(campaign_id, template, existing, library.variants, brief, count)

    def request_drafts(self, plan: DraftPlan) -> list[Any]:
        """Ask the model and read its reply as a list of drafts. Raises LLMError."""
        raw = self.client.complete(LLMRequest(_SYSTEM, build_prompt(plan.brief), plan.brief))
        return parse_reply(raw)

    def screen(self, plan: DraftPlan, drafts: list[Any]) -> ScreenResult:
        """Apply the checks to each draft. Pure: nothing is saved here."""
        template, count = plan.template, plan.count
        seen = {_norm(v.text) for v in plan.existing}
        used_ids = {v.variant_id for v in plan.all_variants}
        created: list[MessageVariant] = []
        skipped: list[SkippedDraft] = []
        next_n = 1

        for item in drafts:
            if len(created) >= count:
                skipped.append(_skip(item, "over_limit", f"more than the {count} asked for"))
                continue
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                skipped.append(_skip(item, "invalid_item", "not an object with a text"))
                continue
            text = item["text"].strip()
            if not text:
                skipped.append(_skip(text, "empty_body", "the text is blank"))
                continue
            if len(text) > MAX_TEXT_CHARS:
                skipped.append(_skip(text, "too_long", f"over {MAX_TEXT_CHARS} characters"))
                continue
            if _norm(text) in seen:
                skipped.append(_skip(text, "duplicate", "repeats an existing or earlier wording"))
                continue

            while f"{template.template_id}_llm_{next_n}" in used_ids:
                next_n += 1
            variant = MessageVariant(
                variant_id=f"{template.template_id}_llm_{next_n}",
                template_id=template.template_id,
                text=text,
                principle=clean_label(item.get("principle", "")) or UNSPECIFIED,
                tone=clean_label(item.get("tone", "")) or UNSPECIFIED,
                status=VariantStatus.PENDING,  # whatever the model claims
                source=VariantSource.LLM,
                generated_by=self.client.name,
            )
            errors = [
                i
                for i in variant_approval_issues(variant, template)
                if i.severity == Severity.ERROR
            ]
            if errors:
                skipped.append(_skip(text, errors[0].code, errors[0].message))
                continue

            created.append(variant)
            used_ids.add(variant.variant_id)
            seen.add(_norm(text))

        return ScreenResult(created=created, skipped=skipped)

    def save(self, campaign_id: str, variants: list[MessageVariant]) -> None:
        """Store variants as they are (PENDING). Saving the same variant twice is harmless."""
        for variant in variants:
            self.service.save_variant(campaign_id, variant)

    # ---- the whole thing ------------------------------------------------------------------
    def generate(
        self,
        campaign_id: str,
        template_id: str,
        *,
        count: int = DEFAULT_COUNT,
        principles: list[str] | None = None,
        tones: list[str] | None = None,
    ) -> GenerationResult:
        plan = self.prepare(
            campaign_id, template_id, count=count, principles=principles, tones=tones
        )
        screened = self.screen(plan, self.request_drafts(plan))
        self.save(campaign_id, screened.created)
        return GenerationResult(
            model=self.client.name,
            requested=count,
            created=screened.created,
            skipped=screened.skipped,
        )
