"""Shared state and graph shape for variant generation (step 8e-3).

The flow: check the request, ask the model, check each draft, save the good ones as PENDING,
then PAUSE until a person approves or rejects them, then apply what the person decided.

Everything that goes into this state is plain JSON (dicts, lists, strings, numbers). The graph
is paused with a checkpointer, and plain data is the safest thing to store in one. Variants are
kept as `model_dump(mode="json")` dicts and turned back into models only inside a node.

No langgraph import is needed in this file, so it works before the package is installed.
"""

import operator
from typing import Annotated, Any, TypedDict

START = "__start__"

# What a person can decide about a variant.
APPROVE = "approve"
REJECT = "reject"
ACTIONS = (APPROVE, REJECT)


class VariantGraphState(TypedDict, total=False):
    """What flows between the nodes. A node returns only the keys it changes.

    Only `steps` accumulates (operator.add), as in the decision graph.
    """

    # --- input (given when the run starts) ---
    run_id: str
    campaign_id: str
    template_id: str
    count: int
    principles: list[str]
    tones: list[str]

    # --- draft_variants ---
    drafts: list[Any]  # what the model proposed, parsed but not yet checked

    # --- check_drafts ---
    accepted: list[dict[str, Any]]  # variants that passed, as JSON dicts, not saved yet
    skipped: list[dict[str, Any]]  # drafts dropped, each with a code and message
    warnings: dict[str, list[dict[str, Any]]]  # variant_id -> advice for the reviewer

    # --- save_pending ---
    created_ids: list[str]  # variant ids saved as PENDING by this run

    # --- await_review / apply_review ---
    decisions: list[dict[str, Any]]  # what the person decided: variant_id, action, note
    reviewed: list[dict[str, Any]]  # the outcome of each decision

    # --- the trace ---
    steps: Annotated[list[dict[str, str]], operator.add]  # {"agent": ..., "note": ...}


VARIANT_NODES: tuple[str, ...] = (
    "prepare",
    "draft_variants",
    "check_drafts",
    "save_pending",
    "await_review",
    "apply_review",
    "finish",
)

# (from, to, condition). A node with two outgoing edges is a conditional branch.
VARIANT_EDGES: tuple[tuple[str, str, str], ...] = (
    (START, "prepare", "always"),
    ("prepare", "draft_variants", "always"),
    ("draft_variants", "check_drafts", "always"),
    ("check_drafts", "finish", "no draft passed the checks"),
    ("check_drafts", "save_pending", "some drafts passed"),
    ("save_pending", "await_review", "always"),
    ("await_review", "apply_review", "a person has decided"),
    ("apply_review", "finish", "always"),
)

# The state keys each node may write. The tests check the real nodes against this.
VARIANT_NODE_WRITES: dict[str, frozenset[str]] = {
    "prepare": frozenset({"steps"}),
    "draft_variants": frozenset({"drafts", "steps"}),
    "check_drafts": frozenset({"accepted", "skipped", "warnings", "steps"}),
    "save_pending": frozenset({"created_ids", "steps"}),
    "await_review": frozenset({"decisions", "steps"}),
    "apply_review": frozenset({"reviewed", "steps"}),
    "finish": frozenset({"steps"}),
}

# Nodes that change something outside the graph. Everything else only reads or calculates.
VARIANT_SIDE_EFFECTS: dict[str, str] = {
    "draft_variants": "calls the language model (costs money, may differ on every call)",
    "save_pending": "saves the new variants as PENDING",
    "apply_review": "approves or rejects variants, as the person decided",
}

# The one node where the graph stops and waits.
REVIEW_NODE = "await_review"


def variant_mermaid() -> str:
    """The graph as Mermaid text, generated from VARIANT_EDGES so the picture cannot drift."""
    lines = ["flowchart TD", f'    {START}(["start"])']
    for node in VARIANT_NODES:
        lines.append(f'    {node}["{node}"]')
    for src, dst, cond in VARIANT_EDGES:
        label = "" if cond == "always" else f'|"{cond}"|'
        lines.append(f"    {src} -->{label} {dst}")
    return "\n".join(lines) + "\n"
