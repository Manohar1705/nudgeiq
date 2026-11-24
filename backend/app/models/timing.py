"""Timing strings like 'T+1h' (1 hour after abandonment)."""

import re
from datetime import timedelta

_TIMING = re.compile(r"^T\+(\d+)(m|h|d)$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_timing(value: str) -> timedelta:
    """'T+45m' -> 45 minutes, 'T+24h' -> 24 hours, 'T+3d' -> 3 days."""
    match = _TIMING.match(value)
    if not match:
        raise ValueError(f"invalid timing '{value}' (expected e.g. T+45m, T+1h, T+3d)")
    return timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})
