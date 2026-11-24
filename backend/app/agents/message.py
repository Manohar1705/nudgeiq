"""Message agent: picks an APPROVED variant and fills in the customer's values.

The LLM never runs at decision time. Variants are written ahead of time (by an
LLM or a human), approved by a person, and only approved ones can be sent.
"""

import random
from dataclasses import dataclass

from app.agents.variant_selector import UniformSelector, VariantSelector
from app.models import (
    Action,
    Channel,
    CustomerState,
    MessageVariant,
    TemplateLibrary,
    VariantStatus,
)

KNOWN_VARIABLES = {"name", "topItem", "aov", "link", "bonusPts", "units", "store"}


def has_sms_opt_out(text: str) -> bool:
    """True if an SMS text tells the customer how to opt out. Shared with campaign validation."""
    return "STOP" in text


class MessageError(ValueError):
    pass


class NoApprovedVariantError(MessageError):
    pass


@dataclass(frozen=True)
class RenderedMessage:
    variant_id: str
    template_id: str
    channel: Channel
    text: str
    selection: str = ""  # why this variant was picked (for the decision trace)


def build_values(state: CustomerState, profile: dict[str, str] | None = None) -> dict[str, str]:
    profile = profile or {}
    return {
        "name": profile.get("first_name", "there"),
        "topItem": profile.get("top_item", "items"),
        "aov": f"{state.cart_value:.0f}",
        "link": f"https://example.com/cart/{state.customer_id}",
        "bonusPts": str(int(state.cart_value) * 5),
        "units": profile.get("units_left", "a few"),
        "store": profile.get("home_store", "your store"),
    }


class MessageAgent:
    def __init__(
        self,
        library: TemplateLibrary,
        seed: int | None = None,
        selector: VariantSelector | None = None,
    ) -> None:
        self.library = library
        self._rng = random.Random(seed)
        self.selector: VariantSelector = selector or UniformSelector(self._rng)
        self._channel_of = {t.template_id: t.channel for t in library.templates}
        self._offers_of = {t.template_id: t.offers for t in library.templates}
        self._validate_approved()

    def _validate_approved(self) -> None:
        """Fail early if an approved variant could never be sent safely."""
        for v in self.approved_variants():
            unknown = set(v.variables) - KNOWN_VARIABLES
            if unknown:
                raise MessageError(f"{v.variant_id} uses unknown variables {sorted(unknown)}")
            if self._channel_of[v.template_id] == Channel.SMS and not has_sms_opt_out(v.text):
                raise MessageError(f"{v.variant_id} is an SMS without an opt-out (STOP)")

    def approved_variants(
        self, channel: Channel | None = None, offer: str | None = None
    ) -> list[MessageVariant]:
        """Approved variants, optionally only those for a channel and able to carry an offer."""
        found = []
        for v in self.library.variants:
            if v.status != VariantStatus.APPROVED:
                continue
            if channel is not None and self._channel_of[v.template_id] != channel:
                continue
            allowed_offers = self._offers_of[v.template_id]
            if offer is not None and allowed_offers and offer not in allowed_offers:
                continue
            found.append(v)
        return found

    def can_send(self, channel: Channel, offer: str | None) -> bool:
        """True if an approved variant exists for this channel and offer."""
        return bool(self.approved_variants(channel, offer))

    def deliverable_channels(self) -> set[Channel]:
        return {self._channel_of[v.template_id] for v in self.approved_variants()}

    def render(
        self, action: Action, state: CustomerState, profile: dict[str, str] | None = None
    ) -> RenderedMessage:
        if action.channel is None:
            raise MessageError("cannot render a message for a wait action")
        options = self.approved_variants(action.channel, action.offer)
        if not options:
            raise NoApprovedVariantError(
                f"no approved variant for {action.channel.value} with offer {action.offer}"
            )
        choice = self.selector.choose(options)
        variant = choice.variant
        return RenderedMessage(
            variant_id=variant.variant_id,
            template_id=variant.template_id,
            channel=action.channel,
            text=variant.render(build_values(state, profile)),
            selection=choice.how,
        )
