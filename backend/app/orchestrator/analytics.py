"""Uplift broken down by channel, timing, offer, lever, cohort or variant (5B-7).

Each slice is the group of nudged decisions that share one value (say channel = "push"),
compared with the holdout group, who were never nudged. Rules, same as insights.py:

- Only decisions with at least one recorded outcome ("resolved") count towards a rate, so
  decisions still waiting for a customer response are not treated as failures.
- "Wait" decisions are left out of the comparison.

The comparison gives the difference in conversion (treated minus holdout) with a 95%
Newcombe-Wilson interval. That interval stays sensible with small groups and rates near 0 or
1, where the plain normal approximation does not. A slice is `enough_data` only when both
groups have at least `min_resolved` resolved decisions, and `significant` only when it also
has enough data and the interval excludes zero.

Reading it: holdout customers never get an action, so they have no channel, timing, offer,
lever or variant. Those slices are compared with the whole holdout group (cohort slices use
the holdout of the same cohort). The Master chose each action instead of assigning it at
random, so a difference between slices is a signal, not proof that the choice caused it.
"""

import math
from dataclasses import dataclass
from typing import Literal, get_args

from pydantic import BaseModel

from app.models import Decision, OutcomeType
from app.orchestrator.insights import GroupStats
from app.repository.base import Repository

Dimension = Literal["channel", "timing", "offer", "lever", "cohort", "variant"]
DIMENSIONS: tuple[str, ...] = get_args(Dimension)

DEFAULT_MIN_RESOLVED = 30
Z95 = 1.959964
NONE_KEY = "(none)"
CONFIDENCE = 0.95

CAVEAT = (
    "Holdout customers get no action, so action slices are compared with the whole holdout "
    "group (cohort slices with the same cohort's holdout). The Master chose each action "
    "rather than assigning it at random, so this shows association, not proof of cause."
)


class UpliftSlice(BaseModel):
    key: str
    treated: GroupStats
    baseline: GroupStats  # the holdout group this slice is compared with
    uplift_abs: float | None  # treated conversion - baseline conversion (None: a group is empty)
    uplift_rel: float | None  # uplift_abs / baseline conversion (None: baseline converts nobody)
    ci_low: float | None  # 95% interval on uplift_abs
    ci_high: float | None
    enough_data: bool
    significant: bool  # enough data AND the interval excludes zero


class UpliftReport(BaseModel):
    dimension: str
    campaign_id: str | None
    campaign_version: int | None
    baseline: str  # which holdout the slices were compared with
    min_resolved: int
    confidence: float
    caveat: str
    overall: UpliftSlice  # every treated decision in scope vs every holdout decision
    slices: list[UpliftSlice]
    waited: int  # eligible but no useful action; not part of any comparison


# ---------------------------------------------------------------------------- statistics
def wilson_interval(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for one proportion. An empty group gives (0, 1)."""
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def newcombe_interval(x1: int, n1: int, x2: int, n2: int, z: float = Z95) -> tuple[float, float]:
    """Interval for p1 - p2 built from the two Wilson intervals (both groups must be non-empty)."""
    p1, p2 = x1 / n1, x2 / n2
    l1, u1 = wilson_interval(x1, n1, z)
    l2, u2 = wilson_interval(x2, n2, z)
    diff = p1 - p2
    low = diff - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    high = diff + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return max(-1.0, low), min(1.0, high)


# ---------------------------------------------------------------------------- grouping
@dataclass(frozen=True)
class _Row:
    decision: Decision
    resolved: bool
    ordered: bool
    points: int


def _stats(rows: list[_Row]) -> GroupStats:
    resolved = [r for r in rows if r.resolved]
    g = GroupStats(
        decisions=len(rows),
        resolved=len(resolved),
        orders=sum(r.ordered for r in resolved),
    )
    if resolved:
        g.conversion = g.orders / g.resolved
        g.avg_reward = sum(r.points for r in resolved) / g.resolved
    return g


def _slice(
    key: str, treated_rows: list[_Row], base_rows: list[_Row], min_resolved: int
) -> UpliftSlice:
    treated, base = _stats(treated_rows), _stats(base_rows)
    diff = low = high = rel = None
    if treated.resolved and base.resolved:
        diff = treated.conversion - base.conversion
        low, high = newcombe_interval(treated.orders, treated.resolved, base.orders, base.resolved)
        rel = diff / base.conversion if base.conversion > 0 else None
    enough = treated.resolved >= min_resolved and base.resolved >= min_resolved
    significant = enough and low is not None and high is not None and (low > 0 or high < 0)
    return UpliftSlice(
        key=key,
        treated=treated,
        baseline=base,
        uplift_abs=diff,
        uplift_rel=rel,
        ci_low=low,
        ci_high=high,
        enough_data=enough,
        significant=significant,
    )


def _key_of(dimension: str, d: Decision, *, pooled: bool) -> str:
    if dimension == "cohort":
        return d.state.cohort_id
    if dimension == "variant":
        if d.variant_id is None:
            return NONE_KEY
        # variant ids are only unique inside one campaign, so pooled reports keep them apart
        return f"{d.use_case_id or NONE_KEY}:{d.variant_id}" if pooled else d.variant_id
    value = getattr(d.chosen, dimension)
    return NONE_KEY if value is None else str(value)


def uplift_by(
    repo: Repository,
    dimension: str,
    *,
    campaign_id: str | None = None,
    campaign_version: int | None = None,
    min_resolved: int = DEFAULT_MIN_RESOLVED,
) -> UpliftReport:
    """Uplift per value of `dimension`, optionally only for one campaign (and version)."""
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown dimension {dimension!r}; choose from {list(DIMENSIONS)}")
    if min_resolved < 1:
        raise ValueError("min_resolved must be at least 1")

    outcomes: dict[str, set[OutcomeType]] = {}
    # only processed outcomes: a pending one has no reward yet and a failed one never will
    for o in repo.list_outcomes(status="processed"):
        outcomes.setdefault(o.decision_id, set()).add(o.outcome_type)

    treated: list[_Row] = []
    holdout: list[_Row] = []
    waited = 0
    for d in repo.list_decisions(limit=None):
        if campaign_id is not None and d.use_case_id != campaign_id:
            continue
        if campaign_version is not None and d.campaign_version != campaign_version:
            continue
        if not d.is_holdout and d.chosen.is_wait:
            waited += 1
            continue
        seen = outcomes.get(d.decision_id)
        reward = repo.get_reward(d.decision_id) if seen else None
        row = _Row(
            decision=d,
            resolved=bool(seen),
            ordered=seen is not None and OutcomeType.ORDER_PLACED in seen,
            points=reward.points if reward else 0,
        )
        (holdout if d.is_holdout else treated).append(row)

    pooled = campaign_id is None
    groups: dict[str, list[_Row]] = {}
    for r in treated:
        groups.setdefault(_key_of(dimension, r.decision, pooled=pooled), []).append(r)

    by_cohort = dimension == "cohort"
    holdout_groups: dict[str, list[_Row]] = {}
    if by_cohort:
        for r in holdout:
            holdout_groups.setdefault(r.decision.state.cohort_id, []).append(r)
        for key in holdout_groups:  # a cohort with only holdout customers still shows up
            groups.setdefault(key, [])

    slices = [
        _slice(key, rows, holdout_groups.get(key, []) if by_cohort else holdout, min_resolved)
        for key, rows in groups.items()
    ]
    slices.sort(key=lambda s: (-s.treated.decisions, s.key))
    return UpliftReport(
        dimension=dimension,
        campaign_id=campaign_id,
        campaign_version=campaign_version,
        baseline="holdout of the same cohort" if by_cohort else "all holdout customers",
        min_resolved=min_resolved,
        confidence=CONFIDENCE,
        caveat=CAVEAT,
        overall=_slice("all", treated, holdout, min_resolved),
        slices=slices,
        waited=waited,
    )
