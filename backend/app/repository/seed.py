"""One-time seed: copy the JSON use-case and template files into the repository.

From 5B on, campaigns live in the database. The JSON files under app/config are only the
starting point. Seeding never overwrites a campaign that is already stored, so it is safe to
run on every start-up and it can never undo an edit made through the API.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config.loader import list_use_cases, load_template_library, load_use_case
from app.models import Campaign, CampaignStatus
from app.repository.base import Repository


@dataclass(frozen=True)
class SeedResult:
    campaign_id: str
    created: bool  # False means it was already stored and nothing was touched
    templates: int = 0
    variants: int = 0


def seed_campaign(
    repo: Repository,
    use_case_id: str,
    *,
    use_case_dir: Path | None = None,
    template_dir: Path | None = None,
    now: datetime | None = None,
) -> SeedResult:
    """Store one use case as an ACTIVE campaign (version 1) with its templates and variants.

    Variants keep the status and source they have in the JSON file. Does nothing if a campaign
    with this id already exists.
    """
    existing = repo.get_campaign(use_case_id)
    if existing is not None:
        return SeedResult(use_case_id, created=False)

    # Load and validate both files before writing anything, so a bad file leaves no half-seed.
    config = load_use_case(use_case_id, use_case_dir)
    library = load_template_library(use_case_id, template_dir)

    when = now or datetime.now(UTC)
    repo.save_campaign(
        Campaign(
            campaign_id=config.use_case_id,
            status=CampaignStatus.ACTIVE,
            config=config,
            created_at=when,
            updated_at=when,
            activated_at=when,
        )
    )
    for template in library.templates:
        repo.save_template(config.use_case_id, template)
    for variant in library.variants:
        repo.save_variant(config.use_case_id, variant)
    repo.flush()
    return SeedResult(
        config.use_case_id,
        created=True,
        templates=len(library.templates),
        variants=len(library.variants),
    )


def seed_all(
    repo: Repository,
    *,
    use_case_dir: Path | None = None,
    template_dir: Path | None = None,
    now: datetime | None = None,
) -> list[SeedResult]:
    """Seed every use case that has a JSON config file."""
    return [
        seed_campaign(repo, uid, use_case_dir=use_case_dir, template_dir=template_dir, now=now)
        for uid in list_use_cases(use_case_dir)
    ]
