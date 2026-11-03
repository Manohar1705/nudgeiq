"""8d-3b step 1: an outcome is stored the moment it arrives, with a status.

Nothing here needs Redis or Celery: this step has no worker code. Repository tests use the `repo`
fixture, so each one runs on memory and on PostgreSQL (skipped without NUDGEIQ_TEST_DSN).
"""

import os
import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.models import (
    Decision,
    Outcome,
    OutcomeKeyConflict,
    OutcomeType,
    RewardRecord,
)
from app.orchestrator import (
    DecisionNotFoundError,
    LogDispatcher,
    Orchestrator,
    OutcomeMismatchError,
)
from app.repository import InMemoryRepository, JsonFileRepository

T = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


@pytest.fixture
def seed(repo, make_state):
    """seed(n) stores customer c1 and decisions d1..dn (the foreign keys PostgreSQL checks)."""

    def _seed(n: int = 1) -> None:
        repo.save_customer(make_state(customer_id="c1"))
        for i in range(1, n + 1):
            repo.save_decision(
                Decision(decision_id=f"d{i}", customer_id="c1", state=make_state(), created_at=T)
            )

    return _seed


def out(kind=OutcomeType.CTA_CLICKED, decision_id="d1", at=T, **payload):
    return Outcome(
        decision_id=decision_id,
        customer_id="c1",
        outcome_type=kind,
        occurred_at=at,
        payload=payload,
    )


# ------------------------------------------------------------------ saving and reading back
def test_default_save_is_processed_as_before(repo, seed):
    seed()
    saved = repo.save_outcome(out(source="push"))
    assert saved.created is True
    rec = saved.record
    assert rec.status == "processed" and rec.processed_at is not None
    assert rec.attempts == 0 and rec.last_error == "" and rec.idempotency_key is None
    assert rec.outcome == out(source="push")
    assert repo.get_outcome(rec.outcome_id) == rec


def test_pending_save_has_no_processed_time(repo, seed):
    seed()
    rec = repo.save_outcome(out(), status="pending").record
    assert rec.status == "pending" and rec.processed_at is None
    assert (datetime.now(UTC) - rec.received_at) < timedelta(minutes=1)


def test_received_at_is_when_we_got_it_not_when_the_customer_acted(repo, seed):
    seed()
    long_ago = T - timedelta(days=3)
    rec = repo.save_outcome(out(at=long_ago), status="pending").record
    assert rec.outcome.occurred_at == long_ago
    assert rec.received_at > long_ago + timedelta(days=2)


def test_ids_are_unique_and_increasing(repo, seed):
    seed()
    ids = [repo.save_outcome(out()).record.outcome_id for _ in range(4)]
    assert ids == sorted(set(ids)) and len(ids) == 4


def test_unknown_id_is_none(repo):
    assert repo.get_outcome(987654) is None


def test_bad_status_is_refused(repo, seed):
    seed()
    with pytest.raises(ValueError, match="status"):
        repo.save_outcome(out(), status="done")  # type: ignore[arg-type]
    rec = repo.save_outcome(out(), status="pending").record
    with pytest.raises(ValueError, match="status"):
        repo.mark_outcome(rec.outcome_id, "done", T)  # type: ignore[arg-type]
    assert len(repo.list_outcomes()) == 1  # the refused save stored nothing


# ------------------------------------------------------------------ idempotency
def test_same_key_is_stored_once(repo, seed):
    seed()
    first = repo.save_outcome(out(), status="pending", idempotency_key="evt-1")
    again = repo.save_outcome(out(), status="pending", idempotency_key="evt-1")
    assert first.created is True and again.created is False
    assert again.record.outcome_id == first.record.outcome_id
    assert again.record.idempotency_key == "evt-1"
    assert len(repo.list_outcomes()) == 1


def test_a_repeat_does_not_reset_an_outcome_that_has_moved_on(repo, seed):
    seed()
    first = repo.save_outcome(out(), status="pending", idempotency_key="evt-1").record
    repo.mark_outcome(first.outcome_id, "processed", T)
    again = repo.save_outcome(out(), status="pending", idempotency_key="evt-1")
    assert again.created is False and again.record.status == "processed"


def test_different_keys_are_separate_outcomes(repo, seed):
    seed()
    repo.save_outcome(out(), idempotency_key="a")
    repo.save_outcome(out(), idempotency_key="b")
    assert len(repo.list_outcomes()) == 2


def test_no_key_means_no_dedupe(repo, seed):
    seed()
    repo.save_outcome(out())
    repo.save_outcome(out())
    assert len(repo.list_outcomes()) == 2


def test_key_reused_for_a_different_outcome_is_refused(repo, seed):
    seed(2)
    repo.save_outcome(out(OutcomeType.CTA_CLICKED), idempotency_key="evt-1")
    with pytest.raises(OutcomeKeyConflict):
        repo.save_outcome(out(OutcomeType.ORDER_PLACED), idempotency_key="evt-1")
    with pytest.raises(OutcomeKeyConflict):
        repo.save_outcome(out(OutcomeType.CTA_CLICKED, "d2"), idempotency_key="evt-1")
    assert len(repo.list_outcomes()) == 1  # nothing was lost or overwritten


def test_concurrent_deliveries_of_one_key_store_one_outcome(repo, seed):
    seed()
    results = []
    barrier = threading.Barrier(8)

    def deliver():
        barrier.wait()
        results.append(repo.save_outcome(out(), status="pending", idempotency_key="evt-1"))

    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r.created for r in results) == 1
    assert len({r.record.outcome_id for r in results}) == 1
    assert len(repo.list_outcomes()) == 1


# ------------------------------------------------------------------ list_outcomes and status
def test_list_outcomes_defaults_to_every_status(repo, seed):
    """Unchanged from before 8d-3b: a pending outcome is listed too. (Who must filter it out
    is decided in step 2; this test records the current behaviour so a change is deliberate.)"""
    seed()
    repo.save_outcome(out(OutcomeType.CTA_CLICKED), status="processed")
    repo.save_outcome(out(OutcomeType.ORDER_PLACED), status="pending")
    repo.save_outcome(out(OutcomeType.IGNORED), status="failed")
    assert len(repo.list_outcomes()) == 3
    assert len(repo.list_outcomes(decision_id="d1")) == 3
    assert len(repo.list_outcomes(customer_id="c1")) == 3


def test_list_outcomes_can_filter_by_status(repo, seed):
    seed()
    repo.save_outcome(out(OutcomeType.CTA_CLICKED), status="processed")
    repo.save_outcome(out(OutcomeType.ORDER_PLACED), status="pending")
    repo.save_outcome(out(OutcomeType.IGNORED), status="failed")
    kinds = lambda **kw: [o.outcome_type for o in repo.list_outcomes(**kw)]  # noqa: E731
    assert kinds(status="processed") == [OutcomeType.CTA_CLICKED]
    assert kinds(status="pending") == [OutcomeType.ORDER_PLACED]
    assert kinds(status="failed") == [OutcomeType.IGNORED]
    assert kinds(decision_id="d1", status="pending") == [OutcomeType.ORDER_PLACED]
    assert kinds(customer_id="c1", status="failed") == [OutcomeType.IGNORED]
    assert kinds(decision_id="nope", status="pending") == []


# ------------------------------------------------------------------ mark and count attempts
def test_mark_processed_sets_the_time_and_clears_the_error(repo, seed):
    seed()
    rec = repo.save_outcome(out(), status="pending").record
    repo.count_outcome_attempt(rec.outcome_id, "lock timeout")
    repo.mark_outcome(rec.outcome_id, "processed", T)
    done = repo.get_outcome(rec.outcome_id)
    assert done is not None
    assert done.status == "processed" and done.processed_at == T and done.last_error == ""
    assert done.attempts == 1  # the history of tries is kept


def test_mark_failed_keeps_the_reason_and_no_processed_time(repo, seed):
    seed()
    rec = repo.save_outcome(out(), status="pending").record
    repo.mark_outcome(rec.outcome_id, "failed", T, "payload is not valid")
    bad = repo.get_outcome(rec.outcome_id)
    assert bad is not None
    assert bad.status == "failed" and bad.processed_at is None
    assert bad.last_error == "payload is not valid"


def test_a_failed_outcome_can_be_put_back_to_pending(repo, seed):
    seed()
    rec = repo.save_outcome(out(), status="pending").record
    repo.mark_outcome(rec.outcome_id, "failed", T, "boom")
    repo.mark_outcome(rec.outcome_id, "pending", T)
    back = repo.get_outcome(rec.outcome_id)
    assert back is not None and back.status == "pending" and back.last_error == ""


def test_count_attempt_adds_one_and_keeps_the_latest_error(repo, seed):
    seed()
    rec = repo.save_outcome(out(), status="pending").record
    assert repo.count_outcome_attempt(rec.outcome_id, "lock timeout") == 1
    assert repo.count_outcome_attempt(rec.outcome_id, "lock timeout again") == 2
    now = repo.get_outcome(rec.outcome_id)
    assert now is not None and now.attempts == 2 and now.last_error == "lock timeout again"
    assert now.status == "pending"  # a retryable error does not change the status


def test_marking_an_unknown_outcome_raises(repo):
    with pytest.raises(KeyError):
        repo.mark_outcome(987654, "processed", T)
    with pytest.raises(KeyError):
        repo.count_outcome_attempt(987654, "x")


# ------------------------------------------------------------------ the pending lookup
def test_pending_lookup_finds_only_old_enough_pending_ones_oldest_first(repo, seed):
    seed()
    base = datetime.now(UTC) - timedelta(hours=1)
    a = repo.save_outcome(out(), status="pending", received_at=base + timedelta(minutes=1))
    b = repo.save_outcome(out(), status="pending", received_at=base)  # older: comes first
    c = repo.save_outcome(out(), status="pending", received_at=base + timedelta(minutes=30))
    repo.save_outcome(out(), status="processed", received_at=base)
    failed = repo.save_outcome(out(), status="pending", received_at=base)
    repo.mark_outcome(failed.record.outcome_id, "failed", T, "bad")

    cutoff = base + timedelta(minutes=10)
    found = repo.list_pending_outcomes(cutoff)
    assert [r.outcome_id for r in found] == [b.record.outcome_id, a.record.outcome_id]
    assert all(r.status == "pending" for r in found)

    everything = repo.list_pending_outcomes(base + timedelta(hours=2))
    assert [r.outcome_id for r in everything][-1] == c.record.outcome_id
    assert len(everything) == 3


def test_pending_lookup_includes_the_exact_cutoff_and_respects_limit(repo, seed):
    seed()
    at = datetime.now(UTC) - timedelta(minutes=5)
    for _ in range(3):
        repo.save_outcome(out(), status="pending", received_at=at)
    assert len(repo.list_pending_outcomes(at)) == 3
    assert len(repo.list_pending_outcomes(at, limit=2)) == 2
    assert repo.list_pending_outcomes(at - timedelta(seconds=1)) == []


def test_a_fresh_outcome_is_not_swept_yet(repo, seed):
    """The sweep asks for outcomes older than a few minutes; one that has just arrived is still
    on its way through the queue and is left alone."""
    seed()
    repo.save_outcome(out(), status="pending")
    assert repo.list_pending_outcomes(datetime.now(UTC) - timedelta(minutes=5)) == []


# ------------------------------------------------------------------ accept_outcome (orchestrator)
@pytest.fixture
def orch(repo, config, library):
    return Orchestrator(repo, config, library, seed=7, dispatcher=LogDispatcher())


@pytest.fixture
def decided(orch, repo, make_state, now):
    repo.save_customer(make_state(customer_id="c1"))
    d = orch.decide("c1", now)
    assert not d.chosen.is_wait
    return d


def of(decision, kind=OutcomeType.ORDER_PLACED):
    return Outcome(
        decision_id=decision.decision_id,
        customer_id=decision.customer_id,
        outcome_type=kind,
        occurred_at=T,
    )


def test_accept_stores_pending_and_does_nothing_else(orch, repo, decided):
    version_before = repo.policy_version(orch.config.use_case_id)
    journey_before = repo.get_journey("c1")
    saved = orch.accept_outcome(of(decided))
    assert saved.created and saved.record.status == "pending"
    # no reward, no learning, no journey change: that is the worker's job (step 2)
    assert repo.get_reward(decided.decision_id) is None
    assert repo.policy_version(orch.config.use_case_id) == version_before
    assert repo.get_journey("c1") == journey_before
    customer = repo.get_customer("c1")
    assert customer is not None and customer.contactable


def test_accept_refuses_an_unknown_decision_and_stores_nothing(orch, repo, decided):
    ghost = Outcome(
        decision_id="nope", customer_id="c1", outcome_type=OutcomeType.ORDER_PLACED, occurred_at=T
    )
    with pytest.raises(DecisionNotFoundError):
        orch.accept_outcome(ghost)
    assert repo.list_outcomes() == []


def test_accept_refuses_another_customers_outcome_and_stores_nothing(orch, repo, decided):
    wrong = of(decided).model_copy(update={"customer_id": "someone-else"})
    with pytest.raises(OutcomeMismatchError):
        orch.accept_outcome(wrong)
    assert repo.list_outcomes() == []


def test_accept_with_a_key_stores_a_repeat_once(orch, repo, decided):
    first = orch.accept_outcome(of(decided), idempotency_key="evt-9")
    again = orch.accept_outcome(of(decided), idempotency_key="evt-9")
    assert first.created and not again.created
    assert again.record.outcome_id == first.record.outcome_id
    assert len(repo.list_outcomes()) == 1
    with pytest.raises(OutcomeKeyConflict):
        orch.accept_outcome(of(decided, OutcomeType.CTA_CLICKED), idempotency_key="evt-9")


def test_the_synchronous_path_is_unchanged(orch, repo, decided):
    """record_outcome still rewards and learns at once. Since step 2 it stores the outcome as
    pending first, but it is marked processed before the call returns, so the sweep (step 3)
    never picks it up."""
    result = orch.record_outcome(of(decided))
    assert result.policy_updated and result.reward.points > 0
    assert repo.get_reward(decided.decision_id) is not None
    assert repo.list_pending_outcomes(datetime.now(UTC) + timedelta(days=1)) == []
    assert len(repo.list_outcomes(status="processed")) == 1


def test_the_synchronous_path_still_refuses_bad_outcomes(orch, repo, decided):
    with pytest.raises(DecisionNotFoundError):
        orch.record_outcome(
            Outcome(
                decision_id="nope",
                customer_id="c1",
                outcome_type=OutcomeType.ORDER_PLACED,
                occurred_at=T,
            )
        )
    wrong = of(decided).model_copy(update={"customer_id": "someone-else"})
    with pytest.raises(OutcomeMismatchError):
        orch.record_outcome(wrong)
    assert repo.list_outcomes() == []


# ------------------------------------------------------------------ the JSON snapshot
def test_json_snapshot_keeps_status_key_and_ids(tmp_path, make_state):
    path = tmp_path / "state.json"
    a = JsonFileRepository(path)
    a.save_customer(make_state(customer_id="c1"))
    a.save_decision(Decision(decision_id="d1", customer_id="c1", state=make_state(), created_at=T))
    p = a.save_outcome(out(), status="pending", idempotency_key="evt-1").record
    a.count_outcome_attempt(p.outcome_id, "lock timeout")
    q = a.save_outcome(out(OutcomeType.ORDER_PLACED)).record
    a.flush()

    b = JsonFileRepository(path)
    assert b.get_outcome(p.outcome_id) == a.get_outcome(p.outcome_id)
    assert b.get_outcome(q.outcome_id) == a.get_outcome(q.outcome_id)
    got = b.get_outcome(p.outcome_id)
    assert got is not None and got.status == "pending" and got.attempts == 1
    again = b.save_outcome(out(), status="pending", idempotency_key="evt-1")
    assert again.created is False and again.record.outcome_id == p.outcome_id  # key survived
    fresh = b.save_outcome(out())
    assert fresh.record.outcome_id > max(p.outcome_id, q.outcome_id)  # ids are never reused
    assert [r.outcome_id for r in b.list_pending_outcomes(datetime.now(UTC))] == [p.outcome_id]


def test_an_old_snapshot_of_bare_outcomes_loads_as_processed(tmp_path, make_state):
    """A file written before 8d-3b holds plain outcomes. They were processed when they arrived."""
    import json

    path = tmp_path / "old.json"
    old = JsonFileRepository(tmp_path / "scratch.json")
    old.save_customer(make_state(customer_id="c1"))
    old.save_decision(
        Decision(decision_id="d1", customer_id="c1", state=make_state(), created_at=T)
    )
    old.flush()
    data = json.loads((tmp_path / "scratch.json").read_text())
    data["outcomes"] = [out(OutcomeType.ORDER_PLACED).model_dump(mode="json")]
    data["rewards"] = [
        RewardRecord(decision_id="d1", customer_id="c1", points=40, reason="x").model_dump(
            mode="json"
        )
    ]
    path.write_text(json.dumps(data))

    loaded = JsonFileRepository(path)
    assert [o.outcome_type for o in loaded.list_outcomes()] == [OutcomeType.ORDER_PLACED]
    assert len(loaded.list_outcomes(status="processed")) == 1
    assert loaded.list_pending_outcomes(datetime.now(UTC) + timedelta(days=1)) == []


def test_memory_repository_is_still_the_base_of_the_json_one():
    assert issubclass(JsonFileRepository, InMemoryRepository)


# ------------------------------------------------------------------ PostgreSQL only
DSN = os.environ.get("NUDGEIQ_TEST_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="set NUDGEIQ_TEST_DSN to run PostgreSQL tests")


@needs_pg
def test_old_outcome_rows_become_processed_when_the_schema_is_upgraded(make_state):
    """Upgrade a database that has outcomes from before this step: put the table back into its
    old shape, add a row, run the schema again. The row must be 'processed', not 'pending', or
    the sweep would redo all of history."""
    from app.repository import PostgresRepository

    pg = PostgresRepository(DSN)
    try:
        pg.reset()
        pg.save_customer(make_state(customer_id="c1"))
        pg.save_decision(
            Decision(decision_id="d1", customer_id="c1", state=make_state(), created_at=T)
        )
        for column in (
            "status",
            "idempotency_key",
            "received_at",
            "processed_at",
            "attempts",
            "last_error",
        ):
            pg._run(f"ALTER TABLE outcomes DROP COLUMN {column}")  # noqa: SLF001
        pg._run(  # noqa: SLF001
            "INSERT INTO outcomes (decision_id, customer_id, outcome_type, occurred_at)"
            " VALUES ('d1', 'c1', 'order_placed', %s)",
            (T,),
        )
        pg.create_schema()  # the upgrade
        rows = pg.list_outcomes(status="processed")
        assert [o.outcome_type for o in rows] == [OutcomeType.ORDER_PLACED]
        assert pg.list_pending_outcomes(datetime.now(UTC) + timedelta(days=1)) == []
        old = pg._one("SELECT id FROM outcomes")  # noqa: SLF001
        assert old is not None
        rec = pg.get_outcome(old["id"])
        assert rec is not None
        assert rec.status == "processed" and rec.attempts == 0 and rec.idempotency_key is None
        pg.create_schema()  # and running it again changes nothing
        assert len(pg.list_outcomes(status="processed")) == 1
    finally:
        pg.reset()
        pg.close()


@needs_pg
def test_database_refuses_a_bad_status_and_a_duplicate_key(make_state):
    import psycopg

    from app.repository import PostgresRepository

    pg = PostgresRepository(DSN)
    try:
        pg.reset()
        pg.save_customer(make_state(customer_id="c1"))
        pg.save_decision(
            Decision(decision_id="d1", customer_id="c1", state=make_state(), created_at=T)
        )
        insert = (
            "INSERT INTO outcomes (decision_id, customer_id, outcome_type, occurred_at, {}) "
            "VALUES ('d1', 'c1', 'ignored', %s, {})"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            pg._run(insert.format("status", "'done'"), (T,))  # noqa: SLF001
        pg._run(insert.format("idempotency_key", "'k'"), (T,))  # noqa: SLF001
        with pytest.raises(psycopg.errors.UniqueViolation):
            pg._run(insert.format("idempotency_key", "'k'"), (T,))  # noqa: SLF001
        pg._run(insert.format("idempotency_key", "NULL"), (T,))  # noqa: SLF001
        pg._run(insert.format("idempotency_key", "NULL"), (T,))  # NULLs never collide  # noqa: SLF001
    finally:
        pg.reset()
        pg.close()


@needs_pg
def test_two_connections_delivering_one_key_store_one_outcome(make_state):
    """Real concurrency: separate connections (as the API and a worker would be), not threads
    sharing one connection."""
    from app.repository import PostgresRepository

    first = PostgresRepository(DSN)
    others = [PostgresRepository(DSN, create_schema=False) for _ in range(5)]
    try:
        first.reset()
        first.save_customer(make_state(customer_id="c1"))
        first.save_decision(
            Decision(decision_id="d1", customer_id="c1", state=make_state(), created_at=T)
        )
        results = []
        barrier = threading.Barrier(len(others))

        def deliver(r):
            barrier.wait()
            results.append(r.save_outcome(out(), status="pending", idempotency_key="evt-1"))

        threads = [threading.Thread(target=deliver, args=(r,)) for r in others]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 5 and sum(r.created for r in results) == 1
        assert len({r.record.outcome_id for r in results}) == 1
        assert len(first.list_outcomes()) == 1
    finally:
        first.reset()
        for r in others:
            r.close()
        first.close()
