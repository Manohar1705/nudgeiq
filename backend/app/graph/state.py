"""Shared state and graph shape for the decision pipeline (step 8e-1).

Step 8e-2: app/graph/builder.py builds the real LangGraph from GRAPH_EDGES (used only when
NUDGEIQ_ENGINE=graph) and is checked against Orchestrator.decide() by the equivalence tests.

The graph is a re-expression of Orchestrator.decide(); it must give the same Decision
(same fields, same trace steps in the same order) for the same inputs and seeds.
No langgraph import is needed in this file, so it works before the package is installed.
"""

import operator
from datetime import datetime
from typing import Annotated, TypedDict

from app.agents.message import RenderedMessage
from app.models import Action, CustomerState, Decision, DecisionStep, Journey, ScoredAction


class DecisionGraphState(TypedDict, total=False):
    """What flows between the nodes. A node returns only the keys it changes.

    Only `steps` accumulates (operator.add): each node appends its notes, which becomes
    Decision.steps. Every other key is overwritten by whichever node writes it last.
    """

    # --- input (given when the graph starts) ---
    customer_id: str
    now: datetime
    run_id: str  # 8e-4: names this run in the trace and the log; not part of the Decision

    # --- load_state ---
    state: CustomerState  # customer as the agents see it (clock-adjusted, retry anchor applied)
    journey: Journey
    bucket: str  # segment the Master learns under
    decision_id: str  # made once, in load_state, because the Master needs it before finalize

    # --- the trace: one list, every node appends ---
    steps: Annotated[list[DecisionStep], operator.add]

    # --- candidates ---
    entry_allowed: bool  # False = suppressed by the entry rule
    proposed: list[Action]  # channel x timing x offer x lever, minus actions without a variant
    candidates: list[Action]  # legal actions left after the guardrails
    blocked: list[str]  # action keys the guardrails removed (reasons live in `steps`)

    # --- master_select ---
    scored: list[ScoredAction]  # top-K kept for the record
    scored_total: int  # how many were scored before cutting to top-K
    chosen: Action  # Action.wait() when nothing is sent
    explored: bool

    # --- compose_message / dispatch ---
    rendered: RenderedMessage | None
    scheduled_for: datetime | None
    nudge_sent: bool

    # --- finalize (the output) ---
    decision: Decision


START = "__start__"

NODES: tuple[str, ...] = (
    "load_state",
    "entry_guardrail",
    "propose_actions",
    "guardrail_filter",
    "no_candidates",
    "master_select",
    "compose_message",
    "dispatch",
    "finalize",
)

# (from, to, condition). A node with two outgoing edges is a conditional branch.
GRAPH_EDGES: tuple[tuple[str, str, str], ...] = (
    (START, "load_state", "always"),
    ("load_state", "finalize", "journey is not active"),
    ("load_state", "entry_guardrail", "journey is active"),
    ("entry_guardrail", "no_candidates", "suppressed at entry"),
    ("entry_guardrail", "propose_actions", "entry rule passed"),
    ("propose_actions", "guardrail_filter", "always"),
    ("guardrail_filter", "no_candidates", "no legal candidates"),
    ("guardrail_filter", "master_select", "has legal candidates"),
    ("no_candidates", "finalize", "always"),
    ("master_select", "finalize", "wait chosen (holdout or Master picked wait)"),
    ("master_select", "compose_message", "a send was chosen"),
    ("compose_message", "finalize", "no message could be built: wait"),
    ("compose_message", "dispatch", "message built"),
    ("dispatch", "finalize", "always"),
)

# The state keys each node may write. 8e-2 tests the real nodes against this.
NODE_WRITES: dict[str, frozenset[str]] = {
    "load_state": frozenset({"state", "journey", "bucket", "decision_id", "steps"}),
    "entry_guardrail": frozenset({"entry_allowed", "steps"}),
    "propose_actions": frozenset({"proposed", "steps"}),
    "guardrail_filter": frozenset({"candidates", "blocked", "steps"}),
    "no_candidates": frozenset({"steps"}),
    "master_select": frozenset({"scored", "scored_total", "chosen", "explored", "steps"}),
    "compose_message": frozenset({"rendered", "chosen", "scheduled_for", "steps"}),
    "dispatch": frozenset({"nudge_sent", "steps"}),
    "finalize": frozenset({"decision"}),
}

# Nodes that change something outside the graph. Everything else is a pure calculation.
SIDE_EFFECTS: dict[str, str] = {
    "load_state": "saves a new Journey if the customer has none",
    "dispatch": "sends the message, records the contact, bumps journey.nudges_sent",
    "finalize": "saves the Decision (stamped with campaign id and version)",
}


def to_mermaid() -> str:
    """The graph as Mermaid text, generated from GRAPH_EDGES so the picture cannot drift."""
    lines = ["flowchart TD", f'    {START}(["start"])']
    for node in NODES:
        lines.append(f'    {node}["{node}"]')
    for src, dst, cond in GRAPH_EDGES:
        label = "" if cond == "always" else f'|"{cond}"|'
        lines.append(f"    {src} -->{label} {dst}")
    return "\n".join(lines) + "\n"
