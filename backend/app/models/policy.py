"""Policy safety (8d-3a): the learned policy as validated data, and the one way to add to it.

The Master agent keeps, for every (bucket, action) pair, how many times it was tried and the total
reward. Several processes (the API, background workers) learn into the same stored policy, so the
arithmetic lives here, once, and every repository uses it:

* `parse_cells` refuses a malformed policy (negative count, NaN total, missing field...).
* `apply_credit` adds one decision's reward exactly once, and corrects it by the difference if
  the reward later changes. Repeating the same call changes nothing.
"""

import math
from dataclasses import dataclass
from typing import Any

Cells = dict[tuple[str, str], tuple[int, float]]


class PolicyError(ValueError):
    """A stored policy is malformed, or a reward cannot be learned (for example NaN)."""


@dataclass(frozen=True)
class StoredPolicy:
    """A policy as stored, with the version it had when it was read. Version 0 = nothing stored."""

    policy: dict[str, Any]
    version: int


@dataclass(frozen=True)
class PolicyCredit:
    """What `credit_policy` did. `policy` and `version` are the policy after the call."""

    policy: dict[str, Any]
    version: int
    changed: bool  # False when this decision's reward was already in the policy, unchanged


def parse_cells(data: Any) -> Cells:
    """Validate a stored policy and return its cells. Raises PolicyError, never a bare KeyError."""
    if not isinstance(data, dict) or not isinstance(data.get("cells"), list):
        raise PolicyError("policy must be an object with a 'cells' list")
    cells: Cells = {}
    for i, raw in enumerate(data["cells"]):
        if not isinstance(raw, dict):
            raise PolicyError(f"cell {i} is not an object")
        bucket, action = raw.get("bucket"), raw.get("action")
        if not isinstance(bucket, str) or not isinstance(action, str):
            raise PolicyError(f"cell {i} needs a text 'bucket' and 'action'")
        n, total = raw.get("n"), raw.get("total")
        if isinstance(n, bool) or not isinstance(n, int | float) or n != int(n) or n < 0:
            raise PolicyError(f"cell {i} ({bucket}, {action}): 'n' must be a whole number >= 0")
        if isinstance(total, bool) or not isinstance(total, int | float):
            raise PolicyError(f"cell {i} ({bucket}, {action}): 'total' must be a number")
        if not math.isfinite(total):
            raise PolicyError(f"cell {i} ({bucket}, {action}): 'total' is not finite")
        if (bucket, action) in cells:
            raise PolicyError(f"cell ({bucket}, {action}) appears twice")
        cells[(bucket, action)] = (int(n), float(total))
    return cells


def cells_to_policy(cells: Cells) -> dict[str, Any]:
    """The JSON-friendly form of the cells, sorted so equal policies compare equal."""
    return {
        "cells": [
            {"bucket": b, "action": a, "n": n, "total": total}
            for (b, a), (n, total) in sorted(cells.items())
        ]
    }


def check_reward(points: float) -> float:
    if isinstance(points, bool) or not math.isfinite(points):
        raise PolicyError(f"reward must be a finite number, got {points!r}")
    return float(points)


def apply_credit(
    cells: Cells, bucket: str, action_key: str, points: float, already: float | None
) -> bool:
    """Add one decision's reward to `cells` in place. Returns True if anything changed.

    `already` is what this decision has put into the policy before (None = never).
    * never learned: the cell counts one more try and gains `points`;
    * learned with other points: the total moves by the difference, the count stays (at least 1);
    * learned with the same points: nothing happens, so a repeated task is harmless.
    """
    points = check_reward(points)
    n, total = cells.get((bucket, action_key), (0, 0.0))
    if already is None:
        cells[(bucket, action_key)] = (n + 1, total + points)
        return True
    if points == already:
        return False
    cells[(bucket, action_key)] = (max(n, 1), total + (points - already))
    return True
