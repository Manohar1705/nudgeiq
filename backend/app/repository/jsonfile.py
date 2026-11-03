"""JSON-file repository: the in-memory repository plus a snapshot file on disk.

Good enough to keep data between server restarts until the PostgreSQL version exists.
Call flush() to write; the API does this after every change.
"""

import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from app.models import (
    Campaign,
    CustomerState,
    Decision,
    Journey,
    MessageTemplate,
    MessageVariant,
    Outcome,
    OutcomeRecord,
    RewardRecord,
)
from app.repository.memory import InMemoryRepository


class JsonFileRepository(InMemoryRepository):
    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            self._load()

    def flush(self) -> None:
        data = {
            "customers": [c.model_dump(mode="json") for c in self._customers.values()],
            "contacts": {k: [t.isoformat() for t in v] for k, v in self._contacts.items()},
            "journeys": [j.model_dump(mode="json") for j in self._journeys.values()],
            "decisions": [d.model_dump(mode="json") for d in self._decisions.values()],
            "outcomes": [o.model_dump(mode="json") for o in self._outcomes],
            "rewards": [r.model_dump(mode="json") for r in self._rewards.values()],
            "policies": self._policies,
            "policy_versions": self._policy_versions,
            "policy_credits": self._policy_credits,
            "campaigns": [c.model_dump(mode="json") for c in self._campaigns.values()],
            "templates": {
                cid: [t.model_dump(mode="json") for t in items.values()]
                for cid, items in self._templates.items()
            },
            "variants": {
                cid: [v.model_dump(mode="json") for v in items.values()]
                for cid, items in self._variants.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: never leaves a half-written file

    def _load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for c in data.get("customers", []):
            self.save_customer(CustomerState.model_validate(c))
        contacts: defaultdict[str, list[datetime]] = defaultdict(list)
        for cid, times in data.get("contacts", {}).items():
            contacts[cid] = [datetime.fromisoformat(t) for t in times]
        self._contacts = dict(contacts)
        for j in data.get("journeys", []):
            self.save_journey(Journey.model_validate(j))
        for d in data.get("decisions", []):
            self.save_decision(Decision.model_validate(d))
        for o in data.get("outcomes", []):
            if "outcome" in o:  # written from 8d-3b on: the stored record, with its status
                self._insert_outcome(OutcomeRecord.model_validate(o))
            else:  # an older snapshot: a bare outcome, which was processed when it arrived
                self.save_outcome(Outcome.model_validate(o))
        for r in data.get("rewards", []):
            self.save_reward(RewardRecord.model_validate(r))
        self._policies = data.get("policies", {})
        # A snapshot from before 8d-3a has policies but no versions: start those at 1, because
        # "a policy exists" must never be version 0, which means "nothing stored".
        self._policy_versions = {uc: int(v) for uc, v in data.get("policy_versions", {}).items()}
        for use_case_id in self._policies:
            self._policy_versions.setdefault(use_case_id, 1)
        self._policy_credits = {
            did: float(points) for did, points in data.get("policy_credits", {}).items()
        }
        # Campaigns first, then templates, then variants: each one points at the one before.
        # A snapshot written before 5B has none of these keys, and loads fine without them.
        for c in data.get("campaigns", []):
            self.save_campaign(Campaign.model_validate(c))
        for cid, templates in data.get("templates", {}).items():
            for t in templates:
                self.save_template(cid, MessageTemplate.model_validate(t))
        for cid, variants in data.get("variants", {}).items():
            for v in variants:
                self.save_variant(cid, MessageVariant.model_validate(v))
