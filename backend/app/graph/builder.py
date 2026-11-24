"""The LangGraph version of Orchestrator.decide() (step 8e-2).

build_decision_graph(orchestrator) returns a compiled graph. Its nodes call the same agents, in
the same order, as Orchestrator._decide_classic(), and write the same trace notes. The shape
(nodes, edges, who writes which state key) comes from app/graph/state.py.

Rules kept from the design (docs/langgraph-design.md):
  * only dispatch and finalize (and the first-time journey save inside load_state) change anything
    outside the graph;
  * no checkpointer, so a node is never replayed;
  * the trace agent names (guardrail, channel, timing, offer, message, master, orchestrator,
    dispatch) are unchanged.

Trace (8e-4): with a `recorder`, every node is wrapped by app/graph/trace.traced, so each run
leaves one entry per node (timing, outcome, state keys written, notes). It goes to the recorder
and the log, never into the Decision, so `Decision.steps` stays identical to the classic engine.
No node is retried: `dispatch` sends, and running it twice would send twice.
"""

from collections.abc import Callable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from app.agents.candidates import entry_check, filter_candidates, propose_actions
from app.agents.master import bucket_for
from app.agents.message import MessageError
from app.graph.state import GRAPH_EDGES, NODES, DecisionGraphState
from app.graph.trace import TraceRecorder, traced
from app.models import Action, Decision, DecisionStep, JourneyStatus
from app.orchestrator.orchestrator import TOP_K_CANDIDATES, Orchestrator

Update = dict[str, Any]
Node = Callable[[DecisionGraphState], Update]

# ---- routers: look at the state, name the next node. Pure, so they are easy to test. ----------
ROUTERS: dict[str, Callable[[DecisionGraphState], str]] = {
    "load_state": lambda s: (
        "entry_guardrail" if s["journey"].status == JourneyStatus.ACTIVE else "finalize"
    ),
    "entry_guardrail": lambda s: "propose_actions" if s["entry_allowed"] else "no_candidates",
    "guardrail_filter": lambda s: "master_select" if s["candidates"] else "no_candidates",
    "master_select": lambda s: "finalize" if s["chosen"].is_wait else "compose_message",
    "compose_message": lambda s: "dispatch" if s.get("rendered") is not None else "finalize",
}


def _step(agent: str, note: str) -> DecisionStep:
    return DecisionStep(agent=agent, note=note)


def make_nodes(orch: Orchestrator) -> dict[str, Node]:
    def load_state(s: DecisionGraphState) -> Update:
        now = s["now"]
        state = orch.state_agent.build_state(s["customer_id"], now)  # raises CustomerNotFoundError
        journey = orch._journey_for(state, now)
        update: dict[str, Any] = {
            "abandoned_minutes": orch._minutes_since_abandonment(journey, now)
        }
        if journey.nudges_sent > 0:  # retry round: timings count from now, not from abandonment
            update["timing_anchor_minutes"] = 0
        state = state.model_copy(update=update)
        steps: list[DecisionStep] = []
        if journey.status != JourneyStatus.ACTIVE:
            steps.append(_step("orchestrator", f"journey is {journey.status.value}: no nudge"))
        return {
            "state": state,
            "journey": journey,
            "bucket": bucket_for(state),
            "decision_id": orch._new_id(),
            "steps": steps,
        }

    def entry_guardrail(s: DecisionGraphState) -> Update:
        allowed, steps = entry_check(s["state"], orch.guardrail)
        return {"entry_allowed": allowed, "steps": steps}

    def propose(s: DecisionGraphState) -> Update:
        proposed, steps = propose_actions(s["state"], orch.config, orch.message_agent.can_send)
        return {"proposed": proposed, "steps": steps}

    def guardrail_filter(s: DecisionGraphState) -> Update:
        outcome = filter_candidates(s["state"], s["proposed"], orch.guardrail, s["now"])
        return {
            "candidates": outcome.candidates,
            "blocked": list(outcome.blocked),
            "steps": outcome.steps,
        }

    def no_candidates(s: DecisionGraphState) -> Update:
        return {"steps": [_step("master", "no legal candidates: waiting")]}

    def master_select(s: DecisionGraphState) -> Update:
        picked = orch.master.decide(s["decision_id"], s["state"], s["candidates"])
        steps = list(picked.steps)
        if len(picked.candidates) > TOP_K_CANDIDATES:
            steps.append(
                _step(
                    "orchestrator",
                    f"scored {len(picked.candidates)} candidates; top {TOP_K_CANDIDATES} stored",
                )
            )
        return {
            "scored": picked.candidates[:TOP_K_CANDIDATES],
            "scored_total": len(picked.candidates),
            "chosen": picked.chosen,
            "explored": picked.explored,
            "steps": steps,
        }

    def compose_message(s: DecisionGraphState) -> Update:
        state, chosen = s["state"], s["chosen"]
        try:
            rendered = orch.message_agent.render(
                chosen, state, orch.state_agent.personalization(state.customer_id)
            )
        except MessageError as exc:
            return {
                "chosen": Action.wait(),
                "rendered": None,
                "steps": [_step("message", f"could not build a message ({exc}): waiting")],
            }
        return {
            "rendered": rendered,
            "scheduled_for": orch.guardrail.send_time(state, chosen, s["now"]),
            "steps": [
                _step(
                    "message",
                    f"used approved variant {rendered.variant_id} ({rendered.selection})",
                )
            ],
        }

    def dispatch(s: DecisionGraphState) -> Update:
        state, chosen = s["state"], s["chosen"]
        rendered, send_at = s["rendered"], s["scheduled_for"]
        assert rendered is not None and send_at is not None  # the router only sends us here then
        orch.dispatcher.send(s["decision_id"], state.customer_id, rendered, send_at)
        orch.repo.record_contact(state.customer_id, send_at)
        channel = chosen.channel.value if chosen.channel else ""
        journey = s["journey"]
        orch.repo.save_journey(journey.model_copy(update={"nudges_sent": journey.nudges_sent + 1}))
        return {
            "nudge_sent": True,
            "steps": [_step("dispatch", f"{channel} send at {send_at:%Y-%m-%d %H:%M} UTC")],
        }

    def finalize(s: DecisionGraphState) -> Update:
        state, rendered = s["state"], s.get("rendered")
        decision = Decision(
            decision_id=s["decision_id"],
            customer_id=s["customer_id"],
            created_at=s["now"],
            state=state,
            is_holdout=state.is_holdout,
            bucket=s["bucket"],
            candidates=list(s.get("scored", [])),
            blocked=list(s.get("blocked", [])),
            chosen=s.get("chosen") or Action.wait(),
            explored=s.get("explored", False),
            scheduled_for=s.get("scheduled_for"),
            variant_id=rendered.variant_id if rendered else None,
            template_id=rendered.template_id if rendered else None,
            message=rendered.text if rendered else None,
            use_case_id=orch.config.use_case_id,
            campaign_version=orch.campaign_version,
            steps=list(s.get("steps", [])),
        )
        orch.repo.save_decision(decision)
        return {"decision": decision}

    return {
        "load_state": load_state,
        "entry_guardrail": entry_guardrail,
        "propose_actions": propose,
        "guardrail_filter": guardrail_filter,
        "no_candidates": no_candidates,
        "master_select": master_select,
        "compose_message": compose_message,
        "dispatch": dispatch,
        "finalize": finalize,
    }


def build_decision_graph(orch: Orchestrator, recorder: TraceRecorder | None = None) -> Any:
    """Compile the graph for one orchestrator. Wiring comes from GRAPH_EDGES.

    With a `recorder`, each node attempt is traced under the state's `run_id`. Without one the
    nodes run bare, as in 8e-2.
    """
    nodes = make_nodes(orch)
    assert set(nodes) == set(NODES), "builder and state.py disagree about the nodes"
    graph = StateGraph(DecisionGraphState)
    for name, fn in nodes.items():
        if recorder is not None:
            fn = traced(name, fn, recorder)  # one attempt, no retries: dispatch must not repeat
        # cast: langgraph's add_node overloads differ between versions; node behaviour is tested.
        graph.add_node(name, cast(Any, fn))
    for source in (START, *NODES):
        targets = [dst for src, dst, _ in GRAPH_EDGES if src == source]
        if source == "finalize":
            graph.add_edge("finalize", END)
        elif len(targets) == 1:
            graph.add_edge(source, targets[0])
        else:
            graph.add_conditional_edges(source, ROUTERS[source], {t: t for t in targets})
    return graph.compile()
