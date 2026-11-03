"""8d-3a: the learned policy is safe to share between processes and safe to repeat.

Repository tests use the `repo` fixture, so each one runs on memory and on PostgreSQL. The
multi-connection tests (real concurrency, row locking) need PostgreSQL and are skipped without
NUDGEIQ_TEST_DSN.
"""

import copy
import math
import os
import threading
from datetime import timedelta

import pytest

from app.agents.master import MasterAgent
from app.agents.state_agent import stable_holdout
from app.models import (
    Action,
    Channel,
    Outcome,
    OutcomeType,
    PolicyError,
    apply_credit,
    parse_cells,
)
from app.orchestrator import LogDispatcher, Orchestrator
from app.repository import InMemoryRepository, JsonFileRepository

UC = "target_cart"
BUCKET = "push|member|high"
ACTION = "push:t1h:none:none"


def cell(policy, bucket=BUCKET, action=ACTION):
    for c in policy["cells"]:
        if c["bucket"] == bucket and c["action"] == action:
            return c["n"], c["total"]
    return None


# ------------------------------------------------------------------ the arithmetic
def test_first_credit_adds_a_try_and_the_points():
    cells = {}
    assert apply_credit(cells, "b", "a", 5, None) is True
    assert cells == {("b", "a"): (1, 5.0)}


def test_the_same_credit_again_changes_nothing():
    cells = {("b", "a"): (1, 5.0)}
    assert apply_credit(cells, "b", "a", 5, 5) is False
    assert cells == {("b", "a"): (1, 5.0)}


def test_changed_points_move_the_total_by_the_difference_and_keep_the_count():
    cells = {("b", "a"): (3, 20.0)}
    assert apply_credit(cells, "b", "a", 8, 5) is True
    assert cells == {("b", "a"): (3, 23.0)}


def test_a_correction_never_leaves_a_count_of_zero():
    cells = {}
    apply_credit(cells, "b", "a", 8, 5)  # learned before, but the cell is gone
    assert cells == {("b", "a"): (1, 3.0)}


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, True])
def test_a_reward_that_is_not_a_finite_number_is_refused(bad):
    cells = {("b", "a"): (1, 5.0)}
    with pytest.raises(PolicyError):
        apply_credit(cells, "b", "a", bad, None)
    assert cells == {("b", "a"): (1, 5.0)}


@pytest.mark.parametrize(
    "policy",
    [
        None,
        [],
        {},
        {"cells": "x"},
        {"cells": [1]},
        {"cells": [{"bucket": "b"}]},
        {"cells": [{"bucket": "b", "action": "a", "total": 1}]},
        {"cells": [{"bucket": "b", "action": "a", "n": -1, "total": 0}]},
        {"cells": [{"bucket": "b", "action": "a", "n": 1.5, "total": 0}]},
        {"cells": [{"bucket": "b", "action": "a", "n": True, "total": 0}]},
        {"cells": [{"bucket": "b", "action": "a", "n": 1, "total": "5"}]},
        {"cells": [{"bucket": "b", "action": "a", "n": 1, "total": math.nan}]},
        {"cells": [{"bucket": "b", "action": "a", "n": 1, "total": math.inf}]},
        {"cells": [{"bucket": 1, "action": "a", "n": 1, "total": 0}]},
        {
            "cells": [
                {"bucket": "b", "action": "a", "n": 1, "total": 0},
                {"bucket": "b", "action": "a", "n": 2, "total": 0},
            ]
        },
    ],
)
def test_a_malformed_policy_is_refused_with_policy_error(policy):
    with pytest.raises(PolicyError):
        parse_cells(policy)


def test_a_valid_policy_round_trips():
    policy = {"cells": [{"bucket": "b", "action": "a", "n": 2, "total": 7.5}]}
    assert parse_cells(policy) == {("b", "a"): (2, 7.5)}
    master = MasterAgent()
    master.load_state(policy)
    assert master.export_state() == policy


# ------------------------------------------------------------------ the Master agent
def test_a_bad_policy_leaves_the_master_untouched():
    master = MasterAgent()
    master.update("b", Action(channel=Channel.PUSH, timing="T+1h"), 5)
    before = master.export_state()
    with pytest.raises(PolicyError):
        master.load_state({"cells": [{"bucket": "b", "action": "a", "n": -4, "total": 0}]})
    assert master.export_state() == before


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_the_master_refuses_a_reward_that_would_poison_a_cell(bad):
    master = MasterAgent()
    action = Action(channel=Channel.PUSH, timing="T+1h")
    with pytest.raises(PolicyError):
        master.update("b", action, bad)
    with pytest.raises(PolicyError):
        master.adjust("b", action, bad)
    assert master.cells_learned == 0


# ------------------------------------------------------------------ repositories
def test_nothing_stored_means_version_zero_and_no_policy(repo):
    assert repo.policy_version(UC) == 0
    assert repo.read_policy(UC) is None
    assert repo.load_policy(UC) is None


def test_a_first_credit_stores_one_try_at_version_one(repo):
    credit = repo.credit_policy(UC, "d1", BUCKET, ACTION, 5)
    assert credit.changed and credit.version == 1
    assert cell(credit.policy) == (1, 5.0)
    stored = repo.read_policy(UC)
    assert stored is not None and stored.version == 1 and stored.policy == credit.policy
    assert repo.policy_version(UC) == 1


def test_repeating_a_credit_adds_nothing_and_keeps_the_version(repo):
    repo.credit_policy(UC, "d1", BUCKET, ACTION, 5)
    again = repo.credit_policy(UC, "d1", BUCKET, ACTION, 5)
    assert not again.changed and again.version == 1
    assert cell(again.policy) == (1, 5.0)


def test_a_changed_reward_corrects_the_total_without_adding_a_try(repo):
    repo.credit_policy(UC, "d1", BUCKET, ACTION, 5)
    fixed = repo.credit_policy(UC, "d1", BUCKET, ACTION, 8)
    assert fixed.changed and fixed.version == 2
    assert cell(fixed.policy) == (1, 8.0)


def test_different_decisions_accumulate(repo):
    for i in range(5):
        repo.credit_policy(UC, f"d{i}", BUCKET, ACTION, 2)
    stored = repo.read_policy(UC)
    assert stored is not None and cell(stored.policy) == (5, 10.0) and stored.version == 5


def test_each_use_case_has_its_own_policy_and_version(repo):
    repo.credit_policy("a", "d1", BUCKET, ACTION, 5)
    repo.credit_policy("b", "d2", BUCKET, ACTION, 7)
    repo.credit_policy("b", "d3", BUCKET, ACTION, 7)
    assert repo.policy_version("a") == 1 and repo.policy_version("b") == 2
    stored_a = repo.read_policy("a")
    assert stored_a is not None and cell(stored_a.policy) == (1, 5.0)


def test_a_credit_does_not_need_the_decision_to_exist(repo):
    # The record of what was learned must outlive the decision, or a repeat would learn it again.
    repo.credit_policy(UC, "gone", BUCKET, ACTION, 5)
    assert not repo.credit_policy(UC, "gone", BUCKET, ACTION, 5).changed


def test_save_policy_replaces_the_policy_and_bumps_the_version(repo):
    repo.credit_policy(UC, "d1", BUCKET, ACTION, 5)
    repo.save_policy(UC, {"cells": []})
    assert repo.policy_version(UC) == 2
    stored = repo.read_policy(UC)
    assert stored is not None and stored.policy == {"cells": []}
    repo.save_policy("fresh", {"cells": []})
    assert repo.policy_version("fresh") >= 1  # "stored" is never version 0


def test_a_stored_copy_cannot_be_changed_from_outside(repo):
    credit = repo.credit_policy(UC, "d1", BUCKET, ACTION, 5)
    credit.policy["cells"].clear()
    loaded = repo.load_policy(UC)
    assert loaded is not None
    loaded["cells"].clear()
    stored = repo.read_policy(UC)
    assert stored is not None and cell(stored.policy) == (1, 5.0)


def test_a_malformed_stored_policy_stops_a_credit_and_changes_nothing(repo):
    repo.save_policy(UC, {"cells": [{"bucket": "b", "action": "a", "n": -4, "total": 0}]})
    version = repo.policy_version(UC)
    with pytest.raises(PolicyError):
        repo.credit_policy(UC, "d1", BUCKET, ACTION, 5)
    assert repo.policy_version(UC) == version
    stored = repo.read_policy(UC)
    assert stored is not None and cell(stored.policy, "b", "a") == (-4, 0)
    # Repair the policy: the same decision is now learned, exactly once (nothing was recorded).
    repo.save_policy(UC, {"cells": []})
    assert repo.credit_policy(UC, "d1", BUCKET, ACTION, 5).changed


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_reward_that_is_not_finite_stores_nothing(repo, bad):
    with pytest.raises(PolicyError):
        repo.credit_policy(UC, "d1", BUCKET, ACTION, bad)
    assert repo.policy_version(UC) == 0 and repo.read_policy(UC) is None
    assert repo.credit_policy(UC, "d1", BUCKET, ACTION, 5).changed  # not marked as learned


def test_legacy_points_count_as_already_learned(repo):
    """Upgrade: learned before credits existed, so only its reward row shows it."""
    repo.save_policy(UC, {"cells": [{"bucket": BUCKET, "action": ACTION, "n": 1, "total": 5.0}]})
    same = repo.credit_policy(UC, "old", BUCKET, ACTION, 5, legacy_points=5)
    assert not same.changed and cell(same.policy) == (1, 5.0)
    fixed = repo.credit_policy(UC, "old2", BUCKET, ACTION, 9, legacy_points=5)
    assert fixed.changed and cell(fixed.policy) == (1, 9.0)  # corrected, no extra try
    assert not repo.credit_policy(UC, "old2", BUCKET, ACTION, 9).changed  # now on record


def test_many_threads_crediting_the_same_decision_learn_it_once(repo):
    barrier = threading.Barrier(16)
    results = []

    def work():
        barrier.wait()
        results.append(repo.credit_policy(UC, "same", BUCKET, ACTION, 7).changed)

    threads = [threading.Thread(target=work) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
    stored = repo.read_policy(UC)
    assert stored is not None and cell(stored.policy) == (1, 7.0) and stored.version == 1


# ---- JSON file: versions and credits survive a restart
def test_the_json_file_keeps_versions_and_credits(tmp_path):
    path = tmp_path / "state.json"
    first = JsonFileRepository(path)
    first.credit_policy(UC, "d1", BUCKET, ACTION, 5)
    first.credit_policy(UC, "d1", BUCKET, ACTION, 8)
    first.flush()
    second = JsonFileRepository(path)
    assert second.policy_version(UC) == 2
    assert not second.credit_policy(UC, "d1", BUCKET, ACTION, 8).changed
    stored = second.read_policy(UC)
    assert stored is not None and cell(stored.policy) == (1, 8.0)


def test_a_snapshot_from_before_policy_versions_loads_at_version_one(tmp_path):
    path = tmp_path / "state.json"
    old = JsonFileRepository(path)
    old.save_policy(UC, {"cells": [{"bucket": BUCKET, "action": ACTION, "n": 2, "total": 9.0}]})
    old.flush()
    import json

    data = json.loads(path.read_text())
    del data["policy_versions"], data["policy_credits"]
    path.write_text(json.dumps(data))
    loaded = JsonFileRepository(path)
    assert loaded.policy_version(UC) == 1
    assert loaded.read_policy(UC) is not None


# ------------------------------------------------------------------ the orchestrator
def make_orch(repo, config, library, seed=7):
    return Orchestrator(repo, config, library, seed=seed, dispatcher=LogDispatcher())


def outcome_for(decision, kind, at):
    return Outcome(
        decision_id=decision.decision_id,
        customer_id=decision.customer_id,
        outcome_type=kind,
        occurred_at=at,
    )


def nudge(orch, repo, make_state, now, cid):
    repo.save_customer(make_state(customer_id=cid))
    d = orch.decide(cid, now)
    assert not d.chosen.is_wait, [s.note for s in d.steps]
    return d


def tries(policy):
    return sum(c["n"] for c in policy["cells"])


def total(policy):
    return sum(c["total"] for c in policy["cells"])


def test_two_orchestrators_on_one_repository_do_not_overwrite_each_other(
    repo, config, library, make_state, now
):
    a, b = make_orch(repo, config, library, 1), make_orch(repo, config, library, 2)
    d1, d2 = nudge(a, repo, make_state, now, "c1"), nudge(b, repo, make_state, now, "c2")
    a.record_outcome(outcome_for(d1, OutcomeType.ORDER_PLACED, now + timedelta(hours=1)))
    b.record_outcome(outcome_for(d2, OutcomeType.ORDER_PLACED, now + timedelta(hours=1)))
    stored = repo.read_policy(UC)
    assert stored is not None
    assert tries(stored.policy) == 2 and total(stored.policy) == 80
    assert stored.version == 2


def test_the_same_outcome_delivered_twice_is_learned_once(repo, config, library, make_state, now):
    orch = make_orch(repo, config, library)
    d = nudge(orch, repo, make_state, now, "c1")
    event = outcome_for(d, OutcomeType.ORDER_PLACED, now + timedelta(hours=1))
    first = orch.record_outcome(event)
    orch.record_outcome(event)
    assert first.policy_updated
    stored = repo.read_policy(UC)
    assert stored is not None and tries(stored.policy) == 1 and total(stored.policy) == 40
    assert stored.version == 1


def test_a_late_order_corrects_the_reward_without_adding_a_try(
    repo, config, library, make_state, now
):
    orch = make_orch(repo, config, library)
    d = nudge(orch, repo, make_state, now, "c1")
    orch.record_outcome(outcome_for(d, OutcomeType.MESSAGE_OPENED, now + timedelta(hours=1)))
    orch.record_outcome(outcome_for(d, OutcomeType.ORDER_PLACED, now + timedelta(hours=2)))
    stored = repo.read_policy(UC)
    assert stored is not None and tries(stored.policy) == 1 and total(stored.policy) == 42


def test_the_orchestrator_keeps_learning_what_the_other_process_learned(
    repo, config, library, make_state, now
):
    a, b = make_orch(repo, config, library, 1), make_orch(repo, config, library, 2)
    d = nudge(b, repo, make_state, now, "c1")
    b.record_outcome(outcome_for(d, OutcomeType.ORDER_PLACED, now + timedelta(hours=1)))
    assert a.master.cells_learned == 0
    repo.save_customer(make_state(customer_id="c2"))
    a.decide("c2", now)  # reloads first: it sees B's learning
    assert a.master.cells_learned == 1


def test_a_restart_loads_what_was_learned(repo, config, library, make_state, now):
    first = make_orch(repo, config, library)
    d = nudge(first, repo, make_state, now, "c1")
    first.record_outcome(outcome_for(d, OutcomeType.ORDER_PLACED, now + timedelta(hours=1)))
    second = make_orch(repo, config, library)
    assert second.master.export_state() == first.master.export_state()


def test_an_orchestrator_never_forgets_its_own_learning_when_the_version_is_unchanged(
    repo, config, library, make_state, now
):
    orch = make_orch(repo, config, library)
    d = nudge(orch, repo, make_state, now, "c1")
    orch.record_outcome(outcome_for(d, OutcomeType.ORDER_PLACED, now + timedelta(hours=1)))
    learned = copy.deepcopy(orch.master.export_state())
    repo.save_customer(make_state(customer_id="c2"))
    orch.decide("c2", now)
    assert orch.master.export_state() == learned


def test_a_policy_that_turns_bad_while_running_is_logged_once_and_decisions_go_on(
    repo, config, library, make_state, now, caplog
):
    orch = make_orch(repo, config, library)
    d = nudge(orch, repo, make_state, now, "c1")
    orch.record_outcome(outcome_for(d, OutcomeType.ORDER_PLACED, now + timedelta(hours=1)))
    kept = copy.deepcopy(orch.master.export_state())
    repo.save_policy(UC, {"cells": [{"bucket": "b", "action": "a", "n": -4, "total": 0}]})
    with caplog.at_level("ERROR", logger="nudgeiq"):
        for cid in ("c2", "c3", "c4"):
            repo.save_customer(make_state(customer_id=cid))
            orch.decide(cid, now)  # no exception
    assert len([r for r in caplog.records if "unusable" in r.getMessage()]) == 1
    assert orch.master.export_state() == kept  # still the last good policy
    # Fixing it is picked up on the next decision, and the complaints stop.
    repo.save_policy(UC, {"cells": [{"bucket": "b", "action": "a", "n": 3, "total": 9.0}]})
    caplog.clear()
    repo.save_customer(make_state(customer_id="c5"))
    orch.decide("c5", now)
    assert orch.master.export_state() == {
        "cells": [{"bucket": "b", "action": "a", "n": 3, "total": 9.0}]
    }
    assert not caplog.records


def test_starting_on_a_malformed_policy_fails_loudly(repo, config, library):
    repo.save_policy(UC, {"cells": [{"bucket": "b", "action": "a", "n": 1, "total": "x"}]})
    with pytest.raises(PolicyError):
        make_orch(repo, config, library)


def test_a_pretrained_master_given_to_an_empty_repository_is_kept(repo, config, library, now):
    master = MasterAgent(seed=1)
    master.update(BUCKET, Action(channel=Channel.PUSH, timing="T+1h"), 12)
    orch = Orchestrator(repo, config, library, master=master, dispatcher=LogDispatcher())
    stored = repo.read_policy(UC)
    assert stored is not None and tries(stored.policy) == 1
    assert orch.master.cells_learned == 1


def test_an_outcome_from_before_credits_existed_is_not_learned_again(
    repo, config, library, make_state, now
):
    """Upgrade: the reward row exists (and the policy has it) but no credit row does."""
    orch = make_orch(repo, config, library)
    d = nudge(orch, repo, make_state, now, "c1")
    orch.record_outcome(outcome_for(d, OutcomeType.MESSAGE_OPENED, now + timedelta(hours=1)))
    if isinstance(repo, InMemoryRepository):
        repo._policy_credits.clear()
    else:
        repo._run("DELETE FROM policy_credits")
    orch.record_outcome(outcome_for(d, OutcomeType.MESSAGE_OPENED, now + timedelta(hours=2)))
    stored = repo.read_policy(UC)
    assert stored is not None and tries(stored.policy) == 1 and total(stored.policy) == 2


def test_wait_and_holdout_decisions_never_touch_the_policy(repo, config, library, make_state, now):
    orch = make_orch(repo, config, library)
    fraction = config.learning.holdout_fraction
    cid = next(f"h{i}" for i in range(10_000) if stable_holdout(f"h{i}", fraction))
    repo.save_customer(make_state(customer_id=cid))
    d = orch.decide(cid, now)
    assert d.is_holdout
    result = orch.record_outcome(outcome_for(d, OutcomeType.ORDER_PLACED, now))
    assert not result.policy_updated
    assert repo.policy_version(UC) == 0


# ------------------------------------------------------------------ PostgreSQL only
@pytest.fixture
def pg_dsn():
    dsn = os.environ.get("NUDGEIQ_TEST_DSN")
    if not dsn:
        pytest.skip("set NUDGEIQ_TEST_DSN to run the PostgreSQL tests")
    from app.repository import PostgresRepository

    first = PostgresRepository(dsn)
    first.reset()
    first.close()
    return dsn


def test_pg_processes_crediting_at_once_lose_nothing(pg_dsn):
    """Each thread has its own connection, like separate worker processes."""
    from app.repository import PostgresRepository

    workers, per_worker = 8, 25
    barrier = threading.Barrier(workers)
    errors = []

    def work(w):
        repo = PostgresRepository(pg_dsn, create_schema=False)
        try:
            barrier.wait()
            for i in range(per_worker):
                repo.credit_policy(UC, f"w{w}-{i}", BUCKET, ACTION, 2)
                repo.credit_policy(UC, f"shared-{i}", BUCKET, ACTION, 7)  # all workers send these
        except Exception as exc:  # pragma: no cover - the assertion below reports it
            errors.append(exc)
        finally:
            repo.close()

    threads = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    check = PostgresRepository(pg_dsn, create_schema=False)
    stored = check.read_policy(UC)
    check.close()
    assert stored is not None
    own, shared = workers * per_worker, per_worker
    assert cell(stored.policy) == (own + shared, own * 2 + shared * 7)
    assert stored.version == own + shared  # one bump per distinct decision, none for repeats


def test_pg_a_writer_that_never_lets_go_makes_others_give_up_instead_of_hanging(
    pg_dsn, monkeypatch
):
    import psycopg

    from app.repository import PostgresRepository, postgres

    repo = PostgresRepository(pg_dsn, create_schema=False)
    repo.credit_policy(UC, "d0", BUCKET, ACTION, 1)
    monkeypatch.setattr(postgres, "POLICY_LOCK_TIMEOUT", "300ms")
    holder = psycopg.connect(pg_dsn, autocommit=True)
    try:
        with holder.transaction():
            holder.execute("SELECT 1 FROM policies WHERE use_case_id = %s FOR UPDATE", (UC,))
            with pytest.raises(psycopg.errors.LockNotAvailable):
                repo.credit_policy(UC, "d1", BUCKET, ACTION, 5)
        # The failed attempt left nothing behind, and the next one goes through.
        assert repo.credit_policy(UC, "d1", BUCKET, ACTION, 5).changed
        stored = repo.read_policy(UC)
        assert stored is not None and cell(stored.policy) == (2, 6.0)
    finally:
        holder.close()
        repo.close()


def test_pg_the_row_and_credit_tables_have_the_expected_shape(pg_dsn):
    from app.repository import PostgresRepository

    repo = PostgresRepository(pg_dsn, create_schema=False)
    cols = {
        r["column_name"]
        for r in repo._all(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name IN ('policies', 'policy_credits')"
        )
    }
    repo.close()
    assert {"version", "decision_id", "use_case_id", "points"} <= cols


def test_pg_creating_the_schema_twice_is_harmless(pg_dsn):
    from app.repository import PostgresRepository

    repo = PostgresRepository(pg_dsn)  # creates it again
    repo.credit_policy(UC, "d1", BUCKET, ACTION, 5)
    repo.create_schema()
    assert repo.policy_version(UC) == 1
    repo.close()
