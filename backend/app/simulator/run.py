"""Milestone script: does the learning agent beat random choice?

python -m app.simulator.run --customers 30000 --seed 1
"""

import argparse
import random
from dataclasses import dataclass, field

from app.agents.action_space import build_action_list
from app.agents.master import MasterAgent
from app.agents.reward import compute_reward
from app.config.loader import load_use_case
from app.models import CustomerState, OutcomeType, UseCaseConfig
from app.simulator.customers import generate_customers
from app.simulator.responses import ResponseModel


@dataclass
class RunStats:
    name: str
    rewards: list[float] = field(default_factory=list)
    expected: list[float] = field(default_factory=list)
    orders: int = 0
    treated: int = 0
    control_orders: int = 0
    control: int = 0

    def mean(self, values: list[float], start: float = 0.0, end: float = 1.0) -> float:
        a, b = int(len(values) * start), int(len(values) * end)
        part = values[a:b]
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


def simulate(
    name: str,
    customers: list[CustomerState],
    config: UseCaseConfig,
    learn: bool,
    seed: int,
) -> RunStats:
    """Run one policy over the customers. learn=False means choose at random."""
    actions = build_action_list(config)
    model = ResponseModel()
    rng = random.Random(seed)
    agent = MasterAgent(epsilon=config.learning.epsilon, seed=seed)
    stats = RunStats(name)

    for i, state in enumerate(customers):
        if state.is_holdout:  # control group: natural behaviour only
            stats.control += 1
            if rng.random() < model.natural_order_prob(state):
                stats.control_orders += 1
            continue

        if learn:
            pick = agent.select(state, actions)
            action, bucket = pick.action, pick.bucket
        else:
            action, bucket = rng.choice(actions), ""

        outcomes = model.sample_outcomes(state, action, rng)
        record = compute_reward(f"d{i}", state.customer_id, outcomes, config.rewards)
        if learn:
            agent.update(bucket, action, record.points)

        stats.treated += 1
        stats.orders += OutcomeType.ORDER_PLACED in outcomes
        stats.rewards.append(float(record.points))
        stats.expected.append(model.expected_reward(state, action, config.rewards))
    return stats


def oracle_expected(customers: list[CustomerState], config: UseCaseConfig) -> float:
    """Best possible average expected reward (knows the hidden rules). Upper bound."""
    actions = build_action_list(config)
    model = ResponseModel()
    best = [
        max(model.expected_reward(s, a, config.rewards) for a in actions)
        for s in customers
        if not s.is_holdout
    ]
    return sum(best) / len(best)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--customers", type=int, default=30000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--use-case", default="target_cart")
    args = parser.parse_args()

    config = load_use_case(args.use_case)
    customers = generate_customers(args.customers, config, seed=args.seed)
    random_run = simulate("random", customers, config, learn=False, seed=args.seed)
    learned = simulate("learning agent", customers, config, learn=True, seed=args.seed)
    best = oracle_expected(customers, config)

    print(f"\nUse case: {config.name}")
    print(f"Customers: {args.customers:,}  |  action menu: {config.action_space.size} actions")
    print(f"Treated: {learned.treated:,}  |  holdout control: {learned.control:,}\n")
    print("Average reward per customer (expected value, less noisy than a single draw)")
    header = f"{'policy':<16}{'first 20%':>11}{'middle':>9}{'last 20%':>10}"
    print(header + f"{'conversion':>12}{'uplift':>9}")
    for s in (random_run, learned):
        print(
            f"{s.name:<16}{s.mean(s.expected, 0, 0.2):>11.2f}{s.mean(s.expected, 0.4, 0.6):>9.2f}"
            f"{s.mean(s.expected, 0.8, 1.0):>10.2f}{s.conversion:>11.1%}{s.uplift:>+9.0%}"
        )
    print(f"{'perfect (oracle)':<16}{'':>11}{'':>9}{best:>10.2f}")
    print(f"{'control group':<16}{'':>11}{'':>9}{'':>10}{learned.control_conversion:>11.1%}")

    r_end = random_run.mean(random_run.expected, 0.8, 1.0)
    l_end = learned.mean(learned.expected, 0.8, 1.0)
    gain = (l_end - r_end) / r_end if r_end else 0.0
    verdict = "PASS" if l_end > r_end * 1.2 else "FAIL"
    print(f"\nLearning agent beats random by {gain:+.0%} at the end of the run -> {verdict}")


if __name__ == "__main__":
    main()
