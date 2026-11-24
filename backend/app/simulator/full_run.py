"""Milestone 2: does the learner still beat random once guardrails apply?

Every customer goes through the real orchestrator (eligibility, cap, cool-down, hours,
approved messages, holdout, retries). Both policies face the same rules; the 'random'
policy is the Master with exploration turned all the way up (random among LEGAL actions).

    python -m app.simulator.full_run --customers 6000 --seed 1
"""

import argparse
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.agents.master import MasterAgent
from app.config.loader import load_template_library, load_use_case
from app.models import CustomerState, Outcome, OutcomeType, UseCaseConfig
from app.orchestrator import Orchestrator
from app.repository import InMemoryRepository
from app.simulator.customers import generate_customers
from app.simulator.responses import ResponseModel

BASE = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)


@dataclass
class FullStats:
    name: str
    per_customer: list[float] = field(default_factory=list)  # expected reward, treated only
    treated: int = 0
    orders: int = 0
    control: int = 0
    control_orders: int = 0
    nudges: int = 0
    waits: int = 0
    violations: list[str] = field(default_factory=list)
    cells: int = 0

    def mean(self, start: float = 0.0, end: float = 1.0) -> float:
        a, b = int(len(self.per_customer) * start), int(len(self.per_customer) * end)
        part = self.per_customer[a:b]
        return sum(part) / len(part) if part else 0.0

    @property
    def conversion(self) -> float:
        return self.orders / self.treated if self.treated else 0.0

    @property
    def control_conversion(self) -> float:
        return self.control_orders / self.control if self.control else 0.0

    @property
    def uplift(self) -> float:
        base = self.control_conversion
        return (self.conversion - base) / base if base else 0.0


def realistic_customers(n: int, config: UseCaseConfig, seed: int) -> list[CustomerState]:
    """Same fake customers as Zip 2, plus missing apps, missing opt-ins and do-not-disturb."""
    rng = random.Random(seed + 1000)  # separate stream: Zip 2 customers stay unchanged
    out = []
    for c in generate_customers(n, config, seed=seed):
        out.append(
            c.model_copy(
                update={
                    "has_app": rng.random() < 0.8,
                    "sms_opt_in": rng.random() < 0.7,
                    "email_opt_in": rng.random() < 0.9,
                    "dnd": rng.random() < 0.03,
                }
            )
        )
    return out


def audit(repo: InMemoryRepository, config: UseCaseConfig) -> list[str]:
    """Check every contact ever scheduled against the hard rules."""
    rules = config.guardrails
    window = timedelta(days=rules.window_days)
    cooldown = timedelta(hours=rules.cooldown_hours)
    problems: list[str] = []
    for cid, times in repo.all_contacts().items():
        times = sorted(times)
        for i, t in enumerate(times):
            if not rules.allowed_start_hour <= t.hour < rules.allowed_end_hour:
                problems.append(f"{cid}: send at {t:%H:%M} outside allowed hours")
            if t.weekday() not in rules.allowed_weekdays:
                problems.append(f"{cid}: send on a blocked weekday")
            if i and t - times[i - 1] < cooldown:
                problems.append(f"{cid}: cool-down broken")
            if sum(1 for u in times[: i + 1] if t - u < window) > rules.max_contacts_per_window:
                problems.append(f"{cid}: contact cap exceeded")
    return problems


def simulate_full(
    name: str,
    customers: list[CustomerState],
    config: UseCaseConfig,
    learn: bool,
    seed: int,
    touches: int = 2,
) -> FullStats:
    repo = InMemoryRepository()
    master = MasterAgent(epsilon=config.learning.epsilon if learn else 1.0, seed=seed)
    orch = Orchestrator(
        repo, config, load_template_library(config.use_case_id), master=master, seed=seed
    )
    model = ResponseModel()
    rng = random.Random(seed)
    stats = FullStats(name)

    for state in customers:
        repo.save_customer(state)
        now = BASE + timedelta(minutes=rng.randrange(7 * 24 * 60))
        expected = 0.0
        ordered = False
        first_holdout = False
        for _touch in range(touches):
            decision = orch.decide(state.customer_id, now)
            first_holdout = decision.is_holdout
            if decision.chosen.is_wait:
                stats.waits += 1
            else:
                stats.nudges += 1
            if not decision.is_holdout:
                expected += model.expected_reward(decision.state, decision.chosen, config.rewards)

            happened = model.sample_outcomes(decision.state, decision.chosen, rng)
            at = (decision.scheduled_for or now) + timedelta(hours=1)
            result = None
            for kind in happened:
                result = orch.record_outcome(
                    Outcome(
                        decision_id=decision.decision_id,
                        customer_id=state.customer_id,
                        outcome_type=kind,
                        occurred_at=at,
                    )
                )
            if not happened:  # silence = timeout
                result = orch.record_outcome(
                    Outcome(
                        decision_id=decision.decision_id,
                        customer_id=state.customer_id,
                        outcome_type=OutcomeType.IGNORED,
                        occurred_at=at + timedelta(hours=48),
                    )
                )
            ordered = ordered or OutcomeType.ORDER_PLACED in happened
            if result is None or result.next_step == "closed":
                break
            now = (decision.scheduled_for or now) + timedelta(hours=50)

        if first_holdout:
            stats.control += 1
            stats.control_orders += ordered
        else:
            stats.treated += 1
            stats.orders += ordered
            stats.per_customer.append(expected)

    stats.violations = audit(repo, config)
    stats.cells = master.cells_learned
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--customers", type=int, default=6000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--touches", type=int, default=2)
    parser.add_argument("--use-case", default="target_cart")
    args = parser.parse_args()

    config = load_use_case(args.use_case)
    customers = realistic_customers(args.customers, config, args.seed)
    runs = [
        simulate_full("random (legal)", customers, config, False, args.seed, args.touches),
        simulate_full("learning agent", customers, config, True, args.seed, args.touches),
    ]
    print(
        f"\nUse case: {config.name}  |  customers: {args.customers:,}  |  max touches: "
        f"{args.touches}  |  guardrails ON\n"
    )
    print(
        f"{'policy':<16}{'first 20%':>10}{'last 20%':>10}{'conv.':>8}{'uplift':>8}"
        f"{'nudges':>9}{'waits':>8}{'rule breaks':>13}"
    )
    for s in runs:
        print(
            f"{s.name:<16}{s.mean(0, 0.2):>10.2f}{s.mean(0.8, 1):>10.2f}{s.conversion:>8.1%}"
            f"{s.uplift:>+8.0%}{s.nudges:>9,}{s.waits:>8,}{len(s.violations):>13}"
        )
    rnd, lrn = runs
    gain = (lrn.mean(0.8, 1) - rnd.mean(0.8, 1)) / rnd.mean(0.8, 1) if rnd.mean(0.8, 1) else 0.0
    ok = lrn.mean(0.8, 1) > rnd.mean(0.8, 1) * 1.1 and not lrn.violations and not rnd.violations
    print(
        f"\nHoldout control conversion: {lrn.control_conversion:.1%}  |  "
        f"segments x actions learned: {lrn.cells}"
    )
    print(
        f"Learning agent beats random-among-legal by {gain:+.0%}, with "
        f"{len(lrn.violations)} guardrail violations -> {'PASS' if ok else 'FAIL'}"
    )


if __name__ == "__main__":
    main()
