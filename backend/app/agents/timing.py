"""Timing agent: which send-time options are still reachable?"""

from datetime import timedelta

from app.models import CustomerState, UseCaseConfig, parse_timing


def propose_timings(state: CustomerState, config: UseCaseConfig) -> list[str]:
    """Drop timings whose moment has already passed (T+1h when 90 minutes have gone by)."""
    anchor = (
        state.abandoned_minutes
        if state.timing_anchor_minutes is None
        else state.timing_anchor_minutes
    )
    elapsed = timedelta(minutes=anchor)
    return [t for t in config.action_space.timings if parse_timing(t) >= elapsed]
