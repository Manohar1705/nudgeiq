"""One full loop, printed step by step:  python -m app.simulator.loop_demo"""

from datetime import UTC, datetime, timedelta

from app.config.loader import load_template_library, load_use_case
from app.models import Channel, CustomerState, Decision, Outcome, OutcomeType
from app.orchestrator import Orchestrator, uplift_summary
from app.repository import InMemoryRepository

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)  # Monday 10:00 UTC


def _cust(cid: str, **kw: object) -> CustomerState:
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
            "first_name": cid.title(),
            "top_item": "desk lamp",
        },
    }
    base.update(kw)
    return CustomerState.model_validate(base)


def _show(title: str, d: Decision) -> None:
    print(f"\n--- {title} [{d.customer_id}] decision {d.decision_id}")
    for step in d.steps:
        print(f"  {step.agent:<12}{step.note}")
    print(f"  => chosen: {d.chosen.key}")
    if d.message:
        print(f"  => message: {d.message.splitlines()[0][:90]}")


def _outcome(d: Decision, kind: OutcomeType, at: datetime) -> Outcome:
    return Outcome(
        decision_id=d.decision_id, customer_id=d.customer_id, outcome_type=kind, occurred_at=at
    )


def main() -> None:
    config = load_use_case("target_cart")
    repo = InMemoryRepository()
    orch = Orchestrator(repo, config, load_template_library("target_cart"), seed=3)
    for name in ("ann", "ben", "cara"):
        repo.save_customer(_cust(name))

    # Ann: nudged, opens, clicks, orders -> journey converted, policy learns.
    d = orch.decide("ann", NOW)
    _show("Ann: first decision", d)
    for kind in (OutcomeType.MESSAGE_OPENED, OutcomeType.CTA_CLICKED, OutcomeType.ORDER_PLACED):
        r = orch.record_outcome(_outcome(d, kind, NOW + timedelta(hours=2)))
        print(
            f"  outcome {kind.value:<20} reward so far {r.reward.points:>3}  "
            f"journey={r.journey.status.value}  next={r.next_step}"
        )
    _show("Ann: asked again after ordering", orch.decide("ann", NOW + timedelta(days=1)))

    # Ben: ignores the nudge -> timeout -> retry; the cool-down then limits further nudges.
    d = orch.decide("ben", NOW)
    _show("Ben: first decision", d)
    r = orch.record_outcome(_outcome(d, OutcomeType.IGNORED, NOW + timedelta(hours=48)))
    print(f"  outcome ignored: reward {r.reward.points}  next={r.next_step}")
    _show(
        "Ben: retry 49h later (a later send is scheduled)",
        orch.decide("ben", NOW + timedelta(hours=49)),
    )
    _show(
        "Ben: second retry too soon (cool-down blocks it)",
        orch.decide("ben", NOW + timedelta(hours=75)),
    )

    # Cara: unsubscribes -> journey closed and customer no longer contactable.
    d = orch.decide("cara", NOW)
    r = orch.record_outcome(_outcome(d, OutcomeType.UNSUBSCRIBED, NOW + timedelta(hours=3)))
    print(f"\n--- Cara unsubscribed: journey={r.journey.status.value} ({r.journey.closed_reason})")
    cara = repo.get_customer("cara")
    print(f"  contactable now: {cara.contactable if cara else '?'}")

    print(f"\nPolicy: {orch.master.cells_learned} segment-action cells learned")
    print(f"Uplift summary: {uplift_summary(repo).model_dump()}")


if __name__ == "__main__":
    main()
