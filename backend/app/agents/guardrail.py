"""Guardrail agent: hard rules the learned policy can never override.

Checks run in this order: entry rule, contact cap (all actions), then per action:
cool-down, allowed hours and weekdays, journey window. Times are evaluated in UTC
for now; per-customer local time is a later improvement.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.models import Action, CustomerState, Guardrails, OutcomeType, parse_timing


@dataclass(frozen=True)
class EntryResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExitResult:
    exit: bool
    reason: str = ""


@dataclass(frozen=True)
class FilterResult:
    allowed: list[Action]
    blocked: dict[str, str]  # action key -> reason
    global_block: str | None = None  # reason when every action is blocked


class GuardrailAgent:
    def __init__(self, rules: Guardrails) -> None:
        self.rules = rules

    def check_entry(self, state: CustomerState) -> EntryResult:
        r = self.rules
        reasons: list[str] = []
        if not state.contactable:
            reasons.append("customer is not contactable")
        if state.dnd:
            reasons.append("customer is on do-not-disturb")
        if state.cart_value <= r.min_cart_value:
            reasons.append(f"cart value {state.cart_value:.2f} not above {r.min_cart_value:g}")
        if state.abandoned_minutes < r.min_abandoned_minutes:
            reasons.append(
                f"abandoned {state.abandoned_minutes} min ago, need {r.min_abandoned_minutes}"
            )
        return EntryResult(allowed=not reasons, reasons=reasons)

    def check_exit(
        self, outcomes: Iterable[OutcomeType], days_since_abandonment: float
    ) -> ExitResult:
        seen = set(outcomes)
        if OutcomeType.ORDER_PLACED in seen:
            return ExitResult(True, "order placed")
        if OutcomeType.UNSUBSCRIBED in seen:
            return ExitResult(True, "customer unsubscribed")
        if days_since_abandonment >= self.rules.exit_after_days:
            return ExitResult(True, f"{self.rules.exit_after_days} days since abandonment")
        return ExitResult(False)

    def send_time(self, state: CustomerState, action: Action, now: datetime) -> datetime:
        """When the nudge would actually go out: T+offset, but never in the past."""
        offset = parse_timing(action.timing) if action.timing else timedelta(0)
        anchor = (
            state.abandoned_minutes
            if state.timing_anchor_minutes is None
            else state.timing_anchor_minutes
        )
        delay = max(timedelta(0), offset - timedelta(minutes=anchor))
        return now + delay

    def filter_actions(
        self, state: CustomerState, actions: list[Action], now: datetime
    ) -> FilterResult:
        r = self.rules
        if state.contacts_in_window >= r.max_contacts_per_window:
            cap_reason = (
                f"contact cap reached ({state.contacts_in_window}/"
                f"{r.max_contacts_per_window} in {r.window_days} days)"
            )
            return FilterResult([], {}, global_block=cap_reason)

        allowed: list[Action] = []
        blocked: dict[str, str] = {}
        cooldown = timedelta(hours=r.cooldown_hours)
        journey_end = timedelta(days=r.exit_after_days) - timedelta(minutes=state.abandoned_minutes)

        for action in actions:
            send_at = self.send_time(state, action, now)
            reason: str | None = None
            if state.last_contact_at and send_at - state.last_contact_at < cooldown:
                reason = f"inside {r.cooldown_hours}h cool-down"
            elif not (r.allowed_start_hour <= send_at.hour < r.allowed_end_hour):
                reason = f"send time {send_at:%H:%M} UTC outside allowed hours"
            elif send_at.weekday() not in r.allowed_weekdays:
                reason = f"send day {send_at:%A} not allowed"
            elif send_at - now > journey_end:
                reason = f"beyond the {r.exit_after_days}-day journey window"
            if reason:
                blocked[action.key] = reason
            else:
                allowed.append(action)
        return FilterResult(allowed, blocked)
