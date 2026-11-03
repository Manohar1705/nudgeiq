"""Shared enumerations. Every agent speaks these types."""

from enum import StrEnum


class Channel(StrEnum):
    PUSH = "push"
    EMAIL = "email"
    SMS = "sms"
    IN_APP = "in_app"
    WEB = "web"
    RETARGETING = "retargeting"


class OutcomeType(StrEnum):
    # positive
    ORDER_PLACED = "order_placed"
    CHECKOUT_RESTARTED = "checkout_restarted"
    CART_REOPENED = "cart_reopened"
    APP_OPENED = "app_opened"
    CTA_CLICKED = "cta_clicked"
    MESSAGE_OPENED = "message_opened"
    # negative
    REFUNDED = "refunded"
    UNSUBSCRIBED = "unsubscribed"
    BOUGHT_ELSEWHERE = "bought_elsewhere"
    IGNORED = "ignored"


class VariantStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class VariantSource(StrEnum):
    """Who wrote a variant. LLM-written ones still need a human to approve them."""

    HUMAN = "human"
    LLM = "llm"


class CampaignStatus(StrEnum):
    DRAFT = "draft"  # being edited, never sent
    ACTIVE = "active"  # live; locked against in-place edits
    ARCHIVED = "archived"  # retired; kept for history and reporting


class JourneyStatus(StrEnum):
    ACTIVE = "active"
    CONVERTED = "converted"
    EXITED = "exited"
    SUPPRESSED = "suppressed"
