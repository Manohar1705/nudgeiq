"""The JSON-file repository must keep campaigns, templates and variants across a restart."""

import json
from datetime import UTC, datetime

from app.models import Campaign, VariantStatus
from app.repository import JsonFileRepository

T = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


def _fill(repo, config, library):
    cid = config.use_case_id
    repo.save_campaign(Campaign(campaign_id=cid, config=config, created_at=T, updated_at=T))
    for t in library.templates:
        repo.save_template(cid, t)
    for v in library.variants:
        repo.save_variant(cid, v)
    return cid


def test_everything_survives_flush_and_reload(tmp_path, config, library):
    path = tmp_path / "snap.json"
    repo = JsonFileRepository(path)
    cid = _fill(repo, config, library)
    repo.flush()

    again = JsonFileRepository(path)
    assert again.get_campaign(cid) == repo.get_campaign(cid)
    assert again.list_templates(cid) == library.templates  # same order too
    assert again.list_variants(cid) == library.variants


def test_an_edit_after_reload_persists(tmp_path, config, library):
    path = tmp_path / "snap.json"
    repo = JsonFileRepository(path)
    cid = _fill(repo, config, library)
    pending = repo.list_variants(cid, status=VariantStatus.PENDING)[0]
    repo.save_variant(
        cid, pending.model_copy(update={"status": VariantStatus.APPROVED, "reviewed_at": T})
    )
    repo.flush()

    again = JsonFileRepository(path)
    got = again.get_variant(cid, pending.variant_id)
    assert got.status == VariantStatus.APPROVED and got.reviewed_at == T
    assert [v.variant_id for v in again.list_variants(cid)] == [
        v.variant_id for v in library.variants
    ]


def test_a_snapshot_from_before_5b_still_loads(tmp_path, make_state):
    path = tmp_path / "old.json"
    old = {"customers": [make_state().model_dump(mode="json")], "contacts": {}}
    path.write_text(json.dumps(old), encoding="utf-8")
    repo = JsonFileRepository(path)
    assert repo.get_customer("c1") is not None
    assert repo.list_campaigns() == []
