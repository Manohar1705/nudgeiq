"""Create the tables:  python -m app.repository.init_db [postgresql://user:pw@host/db] [--seed]

Reads the address from the argument or the NUDGEIQ_DATABASE_URL environment variable.
With --seed it also copies the JSON use cases and templates into the database as campaigns
(campaigns that already exist are left alone, so this is safe to repeat).
"""

import os
import sys

from app.repository.postgres import PostgresRepository
from app.repository.seed import seed_all


def main() -> None:
    args = sys.argv[1:]
    seed = "--seed" in args
    positional = [a for a in args if not a.startswith("--")]
    dsn = positional[0] if positional else os.environ.get("NUDGEIQ_DATABASE_URL")
    if not dsn:
        sys.exit("Give a database address as an argument or set NUDGEIQ_DATABASE_URL.")
    repo = PostgresRepository(dsn)
    try:
        print(
            "Tables are ready (customers, contacts, journeys, decisions, outcomes, rewards, "
            "policies, campaigns, templates, variants)."
        )
        if seed:
            for r in seed_all(repo):
                if r.created:
                    print(
                        f"Seeded campaign {r.campaign_id}: "
                        f"{r.templates} templates, {r.variants} variants."
                    )
                else:
                    print(f"Campaign {r.campaign_id} already exists, left unchanged.")
    finally:
        repo.close()


if __name__ == "__main__":
    main()
