"""Fake customer generator."""

import random

from app.models import Channel, CustomerState, UseCaseConfig


def generate_customers(n: int, config: UseCaseConfig, seed: int = 0) -> list[CustomerState]:
    rng = random.Random(seed)
    cohort_ids = [c.cohort_id for c in config.cohorts]
    customers: list[CustomerState] = []
    for i in range(n):
        preferred = rng.choices(
            [Channel.PUSH, Channel.EMAIL, Channel.SMS, None], weights=[35, 30, 20, 15]
        )[0]
        customers.append(
            CustomerState(
                customer_id=f"cust_{i:06d}",
                cohort_id=rng.choice(cohort_ids),
                cart_value=round(rng.lognormvariate(4.3, 0.6), 2),
                abandoned_minutes=rng.randint(30, 600),
                preferred_channel=preferred,
                is_holdout=rng.random() < config.learning.holdout_fraction,
                signals={
                    "loyalty_tier": rng.choices(
                        ["none", "circle", "circle360"], weights=[55, 30, 15]
                    )[0],
                    "card_holder": rng.random() < 0.3,
                    "fulfillment": rng.choice(["ship", "pickup", "drive_up"]),
                },
            )
        )
    return customers
