"""Which decision engine runs.

  NUDGEIQ_ENGINE   classic (default): the plain Python flow in Orchestrator.decide().
                   graph: the same flow as a LangGraph (needs the langgraph package).

Both give the same decisions; classic stays the default until you choose otherwise.
A wrong value stops the server at start-up.
"""

import os
from collections.abc import Mapping

ENGINES = ("classic", "graph")


def check_engine(engine: str) -> str:
    if engine not in ENGINES:
        raise ValueError(f"NUDGEIQ_ENGINE is {engine!r}; choose one of {list(ENGINES)}")
    return engine


def engine_from_env(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    raw = (source.get("NUDGEIQ_ENGINE") or "").strip().lower()
    return check_engine(raw or "classic")
