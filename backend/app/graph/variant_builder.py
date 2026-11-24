"""The LangGraph for variant generation (step 8e-3).

build_variant_graph(service, client, checkpointer) returns a compiled graph. Its nodes call the
same VariantGenerator steps as the plain /variants/generate endpoint, so the rules are shared.
The shape (nodes, edges, who writes which state key) comes from app/graph/variant_state.py.

Rules this graph keeps (docs/langgraph-design.md):
  * Nothing becomes APPROVED unless a person says so. The graph never approves on its own: the
    only way to reach `apply_review` is for someone to resume it with decisions, and approving
    goes through CampaignService.approve_variant, which refuses what validation would refuse.
  * The pause needs a checkpointer, and a checkpointer can replay a node. So every node is
    safe to run twice: `prepare` and `check_drafts` only read, `save_pending` saves the same
    variants under the same ids, `apply_review` is refused by the service for a variant that
    is no longer pending, and `await_review` does nothing before it asks.
  * The model is called in exactly one node (`draft_variants`), and only after `prepare` has
    checked that the request can succeed.
  * Errors that the plain endpoint turns into HTTP codes (unknown campaign or template, not a
    draft, model failure) are raised unchanged, so they map to the same codes.
"""

from collections import Counter
from collections.abc import Callable
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.campaigns import CampaignError, CampaignService, Severity, variant_approval_issues
from app.campaigns.generator import VariantGenerator
from app.graph.trace import TraceRecorder, traced
from app.graph.variant_state import (
    ACTIONS,
    APPROVE,
    REJECT,
    VARIANT_EDGES,
    VARIANT_NODES,
    VariantGraphState,
)
from app.llm import LLMClient, LLMError
from app.models import MessageVariant

MODEL_NODE = "draft_variants"  # the only node that calls the language model

Update = dict[str, Any]
Node = Callable[[VariantGraphState], Update]

# ---- routers: look at the state, name the next node. Pure, so they are easy to test. ----------
ROUTERS: dict[str, Callable[[VariantGraphState], str]] = {
    "check_drafts": lambda s: "save_pending" if s.get("accepted") else "finish",
}


def _step(agent: str, note: str) -> dict[str, str]:
    return {"agent": agent, "note": note}


def clean_decisions(raw: Any) -> list[dict[str, Any]]:
    """The well-formed decisions in whatever a person sent back. Anything else is dropped.

    The run service checks the request properly before resuming; this is the second line, so a
    bad value can never reach the service calls.
    """
    items = raw.get("decisions") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if (
            isinstance(item, dict)
            and isinstance(item.get("variant_id"), str)
            and item.get("action") in ACTIONS
        ):
            note = item.get("note")
            out.append(
                {
                    "variant_id": item["variant_id"],
                    "action": item["action"],
                    "note": note if isinstance(note, str) else None,
                }
            )
    return out


def make_variant_nodes(service: CampaignService, client: LLMClient) -> dict[str, Node]:
    gen = VariantGenerator(service, client)

    def plan_for(s: VariantGraphState) -> Any:
        return gen.prepare(
            s["campaign_id"],
            s["template_id"],
            count=s["count"],
            principles=s.get("principles"),
            tones=s.get("tones"),
        )

    def prepare(s: VariantGraphState) -> Update:
        plan = plan_for(s)  # raises the same errors as the plain endpoint
        note = (
            f"draft campaign '{s['campaign_id']}', template '{s['template_id']}' "
            f"({plan.template.channel.value}), {len(plan.existing)} existing wordings"
        )
        return {"steps": [_step("campaign", note)]}

    def draft_variants(s: VariantGraphState) -> Update:
        drafts = gen.request_drafts(plan_for(s))  # raises LLMError / LLMReplyError
        return {
            "drafts": drafts,
            "steps": [_step("llm", f"{client.name} proposed {len(drafts)} drafts")],
        }

    def check_drafts(s: VariantGraphState) -> Update:
        plan = plan_for(s)
        screened = gen.screen(plan, s.get("drafts", []))
        warnings = {
            v.variant_id: [
                i.model_dump(mode="json")
                for i in variant_approval_issues(v, plan.template)
                if i.severity == Severity.WARNING
            ]
            for v in screened.created
        }
        total = len(screened.created) + len(screened.skipped)
        note = f"kept {len(screened.created)} of {total} drafts"
        if screened.skipped:
            reasons = Counter(x.code for x in screened.skipped)
            note += "; dropped: " + ", ".join(f"{c} x{n}" for c, n in sorted(reasons.items()))
        return {
            "accepted": [v.model_dump(mode="json") for v in screened.created],
            "skipped": [x.model_dump(mode="json") for x in screened.skipped],
            "warnings": warnings,
            "steps": [_step("guardrail", note)],
        }

    def save_pending(s: VariantGraphState) -> Update:
        variants = [MessageVariant.model_validate(d) for d in s["accepted"]]
        gen.save(s["campaign_id"], variants)
        return {
            "created_ids": [v.variant_id for v in variants],
            "steps": [_step("variants", f"saved {len(variants)} as pending (not sendable yet)")],
        }

    def await_review(s: VariantGraphState) -> Update:
        # Nothing may happen before this call: when a person resumes the run, this node starts
        # again from the top, and interrupt() then hands back what they sent.
        answer = interrupt(
            {
                "kind": "review_variants",
                "campaign_id": s["campaign_id"],
                "variant_ids": list(s.get("created_ids", [])),
            }
        )
        decisions = clean_decisions(answer)
        return {
            "decisions": decisions,
            "steps": [_step("review", f"a person sent {len(decisions)} decisions")],
        }

    def apply_review(s: VariantGraphState) -> Update:
        campaign_id = s["campaign_id"]
        in_run = set(s.get("created_ids", []))
        done: set[str] = set()
        results: list[dict[str, Any]] = []
        for decision in s.get("decisions", []):
            variant_id, action = decision["variant_id"], decision["action"]
            row: dict[str, Any] = {"variant_id": variant_id, "action": action}
            if variant_id not in in_run:
                results.append({**row, "ok": False, "error": "not part of this run"})
                continue
            if variant_id in done:
                results.append({**row, "ok": False, "error": "decided twice in one request"})
                continue
            done.add(variant_id)
            try:
                if action == APPROVE:
                    review = service.approve_variant(campaign_id, variant_id, decision["note"])
                else:
                    review = service.reject_variant(campaign_id, variant_id, decision["note"])
            except CampaignError as exc:  # blocked, wrong status, campaign no longer a draft...
                results.append({**row, "ok": False, "error": str(exc), "code": type(exc).__name__})
                continue
            results.append(
                {
                    **row,
                    "ok": True,
                    "status": review.variant.status.value,
                    "warnings": [w.model_dump(mode="json") for w in review.warnings],
                }
            )
        ok = [r for r in results if r["ok"]]
        approved = sum(1 for r in ok if r["action"] == APPROVE)
        rejected = sum(1 for r in ok if r["action"] == REJECT)
        failed = len(results) - len(ok)
        note = f"approved {approved}, rejected {rejected}"
        if failed:
            note += f", {failed} could not be applied"
        return {"reviewed": results, "steps": [_step("review", note)]}

    def finish(s: VariantGraphState) -> Update:
        created = list(s.get("created_ids", []))
        decided = {r["variant_id"] for r in s.get("reviewed", []) if r["ok"]}
        left = [v for v in created if v not in decided]
        if not created:
            note = "nothing to review: no draft passed the checks"
        else:
            note = f"run finished; {len(left)} of {len(created)} variants still pending"
        return {"steps": [_step("orchestrator", note)]}

    return {
        "prepare": prepare,
        "draft_variants": draft_variants,
        "check_drafts": check_drafts,
        "save_pending": save_pending,
        "await_review": await_review,
        "apply_review": apply_review,
        "finish": finish,
    }


def build_variant_graph(
    service: CampaignService,
    client: LLMClient,
    checkpointer: BaseCheckpointSaver[Any],
    recorder: TraceRecorder | None = None,
    *,
    llm_attempts: int = 1,
    retry_delay: float = 0.0,
) -> Any:
    """Compile the graph. A checkpointer is required: without one the pause cannot work.

    With a `recorder`, every node is wrapped so each attempt is traced (app/graph/trace.py).
    `llm_attempts` is how many times `draft_variants` may try the model (only for LLMError, the
    one node that can fail for outside reasons). Every other node runs once: its errors are
    rule failures or bugs, and running it again would give the same answer.
    """
    nodes = make_variant_nodes(service, client)
    assert set(nodes) == set(VARIANT_NODES), "builder and variant_state.py disagree about nodes"
    graph = StateGraph(VariantGraphState)
    for name, fn in nodes.items():
        if recorder is not None:
            is_model_node = name == MODEL_NODE
            fn = traced(
                name,
                fn,
                recorder,
                attempts=llm_attempts if is_model_node else 1,
                retry_on=(LLMError,) if is_model_node else (),
                passthrough=(GraphInterrupt,),  # the approval pause is not a failure
                delay=retry_delay,
            )
        # cast: langgraph's overloads for add_node differ between versions; the node contract
        # (state in, partial update out) is checked by the tests, not by the type checker.
        graph.add_node(name, cast(Any, fn))
    for source in (START, *VARIANT_NODES):
        targets = [dst for src, dst, _ in VARIANT_EDGES if src == source]
        if source == "finish":
            graph.add_edge("finish", END)
        elif len(targets) == 1:
            graph.add_edge(source, targets[0])
        else:
            graph.add_conditional_edges(source, ROUTERS[source], {t: t for t in targets})
    return graph.compile(checkpointer=checkpointer)
