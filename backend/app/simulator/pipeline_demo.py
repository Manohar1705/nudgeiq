"""Shows the Zip 3 agents working together on a few hand-made customers.

    python -m app.simulator.pipeline_demo

This is a preview of the flow. The real orchestrator arrives in Zip 4.
"""

from datetime import UTC, datetime, timedelta

from app.agents.candidates import generate_candidates
from app.agents.guardrail import GuardrailAgent
from app.agents.master import MasterAgent
from app.agents.message import MessageAgent
from app.agents.state_agent import StateAgent, stable_holdout
from app.config.loader import load_template_library, load_use_case
from app.models import Channel, CustomerState
from app.repository import InMemoryRepository

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)  # a Monday, 10:00 UTC


def _customer(cid: str, **kw: object) -> CustomerState:
    base: dict[str, object] = {
        "customer_id": cid,
        "cohort_id": "cart_high_aov",
        "cart_value": 120.0,
        "abandoned_minutes": 35,
        "preferred_channel": Channel.PUSH,
        "signals": {
            "loyalty_tier": "circle",
            "card_holder": True,
            "fulfillment": "pickup",
            "first_name": "Customer",
            "top_item": "desk lamp",
            "home_store": "Store 214",
            "units_left": 3,
            "device": "ios",  # hidden: the 'device' signal is switched off
        },
    }
    base.update(kw)
    return CustomerState.model_validate(base)


def main() -> None:
    config = load_use_case("target_cart")
    repo = InMemoryRepository()
    holdout_id = next(f"holdout_{i}" for i in range(500) if stable_holdout(f"holdout_{i}", 0.1))

    people: list[tuple[str, CustomerState]] = [
        ("Normal customer", _customer("ann")),
        ("Hit the contact cap (4 in 30 days)", _customer("ben")),
        ("Contacted 10 hours ago (cool-down)", _customer("cara")),
        ("Do-not-disturb", _customer("dev", dnd=True)),
        ("Holdout control group", _customer(holdout_id)),
        (
            "Guest, no app, small cart",
            _customer(
                "fay",
                has_app=False,
                cart_value=25.0,
                signals={
                    "loyalty_tier": "none",
                    "card_holder": False,
                    "fulfillment": "ship",
                    "first_name": "Fay",
                    "top_item": "water bottle",
                    "home_store": "Store 9",
                    "units_left": 2,
                },
            ),
        ),
    ]
    for _, state in people:
        repo.add_customer(state)
    for days in (2, 5, 9, 14):
        repo.record_contact("ben", NOW - timedelta(days=days))
    repo.record_contact("cara", NOW - timedelta(hours=10))

    state_agent = StateAgent(repo, config)
    guardrail = GuardrailAgent(config.guardrails)
    message_agent = MessageAgent(load_template_library("target_cart"), seed=1)
    master = MasterAgent(epsilon=0.0, seed=1)

    for label, raw in people:
        cid = raw.customer_id
        state = state_agent.build_state(cid, NOW)
        result = generate_candidates(state, config, guardrail, NOW, message_agent.can_send)
        decision = master.decide(f"dec_{cid}", state, result.candidates)
        decision.steps = result.steps + decision.steps

        print(f"\n=== {label}  [{cid}] ===")
        for step in decision.steps:
            print(f"  {step.agent:<10} {step.note}")
        if decision.chosen.is_wait:
            print("  -> DECISION: wait (no message sent)")
        else:
            msg = message_agent.render(decision.chosen, state, state_agent.personalization(cid))
            print(f"  -> DECISION: {decision.chosen.key}")
            print(f"  -> MESSAGE ({msg.variant_id}): {msg.text.splitlines()[0][:110]}")


if __name__ == "__main__":
    main()
