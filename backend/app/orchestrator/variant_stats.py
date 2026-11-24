"""Per-variant results read from stored decisions, for the epsilon-greedy selector (5B-9)."""

from collections.abc import Mapping

from app.agents.variant_selector import VariantRecord
from app.orchestrator.analytics import uplift_by
from app.repository.base import Repository


class CampaignVariantStats:
    """Per-variant results for one campaign version, re-read every `refresh_every` calls.

    Reading means scanning decisions, so the result is cached: a selection decision is made
    on numbers that can be up to `refresh_every` calls old. That is fine for a slow-moving
    estimate and keeps a send from costing a full scan.
    """

    def __init__(
        self,
        repo: Repository,
        campaign_id: str,
        campaign_version: int | None,
        *,
        refresh_every: int = 50,
    ) -> None:
        if refresh_every < 1:
            raise ValueError("refresh_every must be at least 1")
        self._repo = repo
        self._campaign_id = campaign_id
        self._version = campaign_version
        self._refresh_every = refresh_every
        self._calls = 0
        self._cache: dict[str, VariantRecord] = {}

    def __call__(self) -> Mapping[str, VariantRecord]:
        if self._calls % self._refresh_every == 0:
            self._cache = self._read()
        self._calls += 1
        return self._cache

    def _read(self) -> dict[str, VariantRecord]:
        report = uplift_by(
            self._repo,
            "variant",
            campaign_id=self._campaign_id,
            campaign_version=self._version,
        )
        return {
            s.key: VariantRecord(s.treated.resolved, s.treated.orders)
            for s in report.slices
            if s.key != "(none)"
        }
